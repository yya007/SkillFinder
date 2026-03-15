"""
scripts/update_index.py — Download and apply the latest pre-built SkillFinder index.

Checks GitHub Releases for a newer index artifact, downloads it, verifies the
SHA256 checksum, and atomically extracts it into the local data/ directory.

Usage:
    python scripts/update_index.py           # update if newer available
    python scripts/update_index.py --check   # check only, no download
    python scripts/update_index.py --force   # force download even if current
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime
from typing import Optional

import requests

REPO = "yya007/skill-finder"
GITHUB_API = "https://api.github.com"

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def read_local_version(data_dir: str) -> Optional[str]:
    """Read the local index date from version.txt.

    Returns the date string (YYYY-MM-DD) or None if version.txt is missing.
    Raises ValueError if version.txt exists but contains no valid 'date:' field.
    """
    version_path = os.path.join(data_dir, "version.txt")
    if not os.path.exists(version_path):
        return None

    with open(version_path) as f:
        for line in f:
            if line.startswith("date:"):
                date_str = line.split(":", 1)[1].strip()
                try:
                    datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    raise ValueError(
                        f"Invalid date format in version.txt: {date_str!r}. Expected YYYY-MM-DD."
                    )
                return date_str

    raise ValueError("version.txt exists but contains no 'date:' field.")


def get_latest_release(repo: str = REPO) -> dict:
    """Fetch the latest GitHub Release for *repo*.

    Returns the raw GitHub API release dict.
    Raises RuntimeError on HTTP errors.
    """
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    resp = requests.get(
        url,
        timeout=30,
        headers={"Accept": "application/vnd.github+json"},
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub API error {resp.status_code} for {url}: {resp.text[:200]}"
        )
    return resp.json()


def needs_update(local_version: Optional[str], tag_name: str) -> bool:
    """Return True if *tag_name* is newer than *local_version*.

    local_version: "YYYY-MM-DD" or None (None always returns True)
    tag_name:      "index-YYYYMMDD" or plain "YYYYMMDD"
    """
    if local_version is None:
        return True

    date_part = tag_name.removeprefix("index-")
    try:
        tag_date = datetime.strptime(date_part, "%Y%m%d").date()
        local_date = datetime.strptime(local_version, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(
            f"Cannot parse date from release tag {tag_name!r}. "
            "Expected format: 'index-YYYYMMDD'. "
            "Check the release tagging process or use --force to bypass."
        )

    return tag_date > local_date


def verify_sha256(file_path: str, expected_hash: str) -> bool:
    """Verify the SHA256 of *file_path* against *expected_hash*.

    Comparison is case-insensitive.
    Raises FileNotFoundError if the file does not exist.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest().lower() == expected_hash.lower()


def download_file(url: str, dest_path: str) -> None:
    """Download *url* to *dest_path* using chunked streaming.

    Raises RuntimeError on non-200 responses.
    """
    resp = requests.get(url, stream=True, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(f"Download failed (HTTP {resp.status_code}): {url}")

    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)


def extract_artifact(tar_path: str, data_dir: str) -> None:
    """Extract the index tarball into *data_dir* atomically.

    Extracts to a ``data_dir/.new/`` staging directory first, then moves each
    file into place — no partial writes to the live data/ directory.
    Cleans up the staging dir on success or failure.

    Only index.faiss, metadata.jsonl, and version.txt are extracted (path
    traversal attempts are silently skipped).
    """
    staging = os.path.join(data_dir, ".new")
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(staging, exist_ok=True)

    safe_names = {"index.faiss", "metadata.jsonl", "version.txt"}
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            for member in tar.getmembers():
                name = os.path.basename(member.name)
                if name not in safe_names:
                    continue
                extracted = tar.extractfile(member)
                if extracted is not None:
                    dest = os.path.join(staging, name)
                    with open(dest, "wb") as out:
                        out.write(extracted.read())

        # Move files atomically from staging into data_dir
        for name in safe_names:
            src = os.path.join(staging, name)
            dst = os.path.join(data_dir, name)
            if os.path.exists(src):
                shutil.move(src, dst)

        # Verify all required files are now present; partial extraction is worse than no update
        missing = [name for name in safe_names if not os.path.exists(os.path.join(data_dir, name))]
        if missing:
            raise RuntimeError(
                f"Extraction incomplete — missing required file(s): {', '.join(sorted(missing))}. "
                "The release artifact may be corrupt. Run update_index.py again to retry."
            )
    finally:
        if os.path.exists(staging):
            shutil.rmtree(staging)


def parse_release_sha256(body: str) -> Optional[str]:
    """Extract a SHA256 hex string from a GitHub Release body.

    Recognises:
      **SHA256:** `abc123...`
      SHA256: abc123...

    Returns the hash string or None if not found.
    """
    m = re.search(r"\*\*SHA256:\*\*\s*`([a-fA-F0-9]+)`", body)
    if m:
        return m.group(1)
    m = re.search(r"SHA256:\s*([a-fA-F0-9]+)", body)
    if m:
        return m.group(1)
    return None


def run_update(
    data_dir: str = _DATA_DIR,
    check_only: bool = False,
    force: bool = False,
) -> dict:
    """Orchestrate the update process.

    Returns dict with keys:
      - "status":  "up_to_date" | "update_available" | "updated" | "error"
      - "message": human-readable description
    """
    try:
        release = get_latest_release()
        tag_name = release["tag_name"]
        local_version = read_local_version(data_dir)

        if not force and not needs_update(local_version, tag_name):
            return {
                "status": "up_to_date",
                "message": f"Index is current ({local_version}). Latest: {tag_name}.",
            }

        if check_only:
            return {
                "status": "update_available",
                "message": f"Update available: {tag_name} (local: {local_version}).",
            }

        # Locate the .tar.gz asset
        assets = release.get("assets", [])
        tar_assets = [a for a in assets if a["name"].endswith(".tar.gz")]
        if not tar_assets:
            return {
                "status": "error",
                "message": "No .tar.gz asset found in the latest release.",
            }

        asset = tar_assets[0]
        download_url = asset["browser_download_url"]

        # Download to a temp file, then verify + extract
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            size_mb = asset.get("size", 0) // 1_000_000
            print(f"Downloading {asset['name']} ({size_mb} MB)...")
            download_file(download_url, tmp_path)

            expected_sha = parse_release_sha256(release.get("body", ""))
            if not expected_sha and not force:
                raise RuntimeError(
                    "SHA256 checksum not found in release body. "
                    "The release artifact cannot be verified and will not be installed. "
                    "Use --force to bypass (not recommended)."
                )
            if expected_sha:
                print("Verifying SHA256...")
            if not verify_sha256(tmp_path, expected_sha):
                return {
                    "status": "error",
                    "message": "SHA256 verification failed. Download may be corrupt or tampered.",
                }

            print(f"Extracting to {data_dir}...")
            os.makedirs(data_dir, exist_ok=True)
            extract_artifact(tmp_path, data_dir)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        return {
            "status": "updated",
            "message": f"Successfully updated to {tag_name}.",
        }

    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download the latest SkillFinder index from GitHub Releases."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for updates without downloading.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force download even if the local index is already current.",
    )
    parser.add_argument(
        "--data",
        default=_DATA_DIR,
        metavar="DATA_DIR",
        help="Path to data directory (default: ../data relative to this script).",
    )
    args = parser.parse_args(argv)

    result = run_update(data_dir=args.data, check_only=args.check, force=args.force)
    print(result["message"])
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
