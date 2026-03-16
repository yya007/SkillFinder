"""
scripts/update_index.py — Pull the latest SkillFinder index release.

Downloads the pre-built FAISS index from the latest GitHub Release and
extracts it into the local data/ directory atomically.

Usage:
    python scripts/update_index.py              # update if newer available
    python scripts/update_index.py --check      # report status, no download
    python scripts/update_index.py --force      # download even if up to date
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import tarfile
import tempfile
from datetime import date
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO = "yya007/SkillFinder"
GITHUB_API = "https://api.github.com"
RELEASE_URL = f"{GITHUB_API}/repos/{REPO}/releases/latest"
_DEFAULT_DATA_DIR = str(Path(__file__).parent / ".." / "data")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_local_version(data_dir: str) -> Optional[str]:
    """Read the local index date from data_dir/version.txt.

    Returns the date string "YYYY-MM-DD", or None if version.txt is absent.
    Raises ValueError if version.txt exists but lacks a valid ``date:`` field.
    """
    version_path = Path(data_dir) / "version.txt"
    if not version_path.exists():
        return None

    content = version_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("date:"):
            raw = line.split(":", 1)[1].strip()
            # Validate format YYYY-MM-DD
            try:
                date.fromisoformat(raw)
            except ValueError:
                raise ValueError(
                    f"version.txt contains an invalid date: {raw!r}. Expected YYYY-MM-DD."
                )
            return raw

    raise ValueError(
        "version.txt exists but has no 'date:' field. "
        "Run a full re-install or delete data/ and retry."
    )


def get_latest_release() -> dict:
    """Fetch the latest GitHub Release metadata for this repo.

    Returns the full release dict (tag_name, assets, body, ...).
    Raises RuntimeError on non-200 responses.
    """
    try:
        resp = requests.get(
            RELEASE_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to reach GitHub API: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub API returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()


def needs_update(local_version: Optional[str], tag_name: str) -> bool:
    """Return True if the release tag is newer than the local index.

    Args:
        local_version: Local date string "YYYY-MM-DD", or None (no index).
        tag_name:      GitHub tag, e.g. "index-20260310" or "20260310".

    Returns:
        True if an update should be downloaded.
    """
    if local_version is None:
        return True

    # Extract YYYYMMDD from tag (strip "index-" prefix if present)
    raw = tag_name.removeprefix("index-").replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        # Unknown tag format — assume update needed
        return True

    release_date_str = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        release_date = date.fromisoformat(release_date_str)
        local_date = date.fromisoformat(local_version)
    except ValueError:
        return True

    return release_date > local_date


def parse_release_sha256(body: str) -> Optional[str]:
    """Extract the SHA256 checksum from a GitHub Release body.

    Looks for patterns like:
        **SHA256:** `abc123def456`
        SHA256: abc123def456

    Returns the hex string, or None if not found.
    """
    if not body:
        return None
    # Try bold/backtick style first: **SHA256:** `<hash>`
    m = re.search(r"\*\*SHA256:\*\*\s*`([0-9a-fA-F]+)`", body)
    if m:
        return m.group(1)
    # Plain style: SHA256: <hash>
    m = re.search(r"SHA256:\s*([0-9a-fA-F]{8,})", body)
    if m:
        return m.group(1)
    return None


def verify_sha256(path: str, expected: str) -> bool:
    """Verify the SHA256 checksum of a file.

    Args:
        path:     Path to the file to verify.
        expected: Expected hex digest (case-insensitive).

    Returns:
        True if the checksum matches, False otherwise.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    sha = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest().lower() == expected.lower()


def download_file(url: str, dest: str) -> None:
    """Download a file from url and write it to dest.

    Raises:
        RuntimeError: If the HTTP response is not 200.
        requests.RequestException: On network errors.
    """
    resp = requests.get(url, stream=True, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Download failed (HTTP {resp.status_code}) from {url}"
        )
    with Path(dest).open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=65536):
            fh.write(chunk)


def extract_artifact(tar_path: str, data_dir: str) -> None:
    """Extract a tar.gz index artifact into data_dir atomically.

    Extracts to a temporary ``data_dir/.new/`` staging directory first,
    then moves each file into place.  No partial writes are visible to
    readers, and ``.new/`` is always removed on completion.

    Args:
        tar_path: Path to the downloaded .tar.gz file.
        data_dir: Destination directory (created if absent).
    """
    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    staging = data_path / ".new"

    # Clean up any leftover staging dir from a previous failed run
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            # Extract only safe members (no absolute paths, no ../  traversal)
            for member in tar.getmembers():
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    continue
                # Extract flat: strip any directory prefix, put files at staging root
                member.name = member_path.name
                tar.extract(member, path=staging, filter="data")

        # Verify required index files were extracted
        required = {"index.faiss", "metadata.jsonl", "version.txt"}
        extracted = {f.name for f in staging.iterdir()}
        missing = required - extracted
        if missing:
            raise RuntimeError(
                f"Extraction incomplete — missing: {', '.join(sorted(missing))}. "
                "The release artifact may be corrupt."
            )

        # Move staged files into data_dir
        for staged_file in staging.iterdir():
            staged_file.replace(data_path / staged_file.name)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def run_update(
    data_dir: str = _DEFAULT_DATA_DIR,
    check_only: bool = False,
    force: bool = False,
) -> dict:
    """Orchestrate an index update end to end.

    Args:
        data_dir:   Directory containing (or to receive) index files.
        check_only: If True, report status without downloading anything.
        force:      If True, download even if the local index is current.

    Returns:
        Dict with "status" ("up_to_date" | "update_available" | "updated" | "error")
        and "message".
    """
    try:
        local_version = read_local_version(data_dir)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    try:
        release = get_latest_release()
    except RuntimeError as exc:
        return {"status": "error", "message": str(exc)}

    tag = release.get("tag_name", "")

    if not force and not needs_update(local_version, tag):
        return {
            "status": "up_to_date",
            "message": f"Local index ({local_version}) matches latest release ({tag}).",
        }

    if check_only:
        return {
            "status": "update_available",
            "message": f"New release available: {tag} (local: {local_version}).",
        }

    # Find the tar.gz asset
    assets = release.get("assets", [])
    tar_asset = next(
        (a for a in assets if a.get("name", "").endswith(".tar.gz")), None
    )
    if not tar_asset:
        return {
            "status": "error",
            "message": f"No .tar.gz asset found in release {tag}.",
        }

    url = tar_asset["browser_download_url"]
    expected_sha = parse_release_sha256(release.get("body", ""))
    if not expected_sha and not force:
        return {
            "status": "error",
            "message": (
                "SHA256 checksum not found in release body — cannot verify download. "
                "Use --force to bypass (not recommended)."
            ),
        }
    if not expected_sha and force:
        print(
            "Warning: SHA256 checksum not found in release body — "
            "skipping integrity verification (--force). "
            "Only use this if you trust the release source.",
            file=sys.stderr,
            flush=True,
        )

    # Download to a temp file
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        download_file(url, tmp_path)

        if expected_sha and not verify_sha256(tmp_path, expected_sha):
            return {
                "status": "error",
                "message": "SHA256 checksum mismatch — download may be corrupt.",
            }

        extract_artifact(tmp_path, data_dir)
    except (OSError, RuntimeError, requests.RequestException) as exc:
        return {"status": "error", "message": str(exc)}
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return {
        "status": "updated",
        "message": f"Index updated to {tag}.",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the latest SkillFinder index from GitHub Releases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for updates without downloading.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force download even if local index is current.",
    )
    parser.add_argument(
        "--data",
        default=_DEFAULT_DATA_DIR,
        metavar="DIR",
        help="Path to the data directory (default: data/ next to this script).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    result = run_update(
        data_dir=args.data,
        check_only=args.check,
        force=args.force,
    )
    status = result.get("status", "error")
    msg = result.get("message", "")
    print(f"[{status}] {msg}")
    return 0 if status in ("up_to_date", "updated", "update_available") else 1


if __name__ == "__main__":
    sys.exit(main())
