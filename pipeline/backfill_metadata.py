"""
pipeline/backfill_metadata.py — Backfill missing GitHub metadata in raw JSONL files.

Two modes of operation:

Stars backfill (default):
  For each record where raw_metadata.stars is None or 0 (not fetched at crawl
  time, or crawled before the GitHub metadata fetch completed), calls the GitHub
  API to retrieve stargazers_count, pushed_at, and default_branch, then writes
  the enriched records back to the same file atomically.
  Deduplicates GitHub API calls: each unique repo_url is fetched at most once.

Description backfill (--descriptions flag):
  For each record with an empty description, re-fetches the SKILL.md file from
  GitHub and re-parses its frontmatter.  Handles files that begin with HTML
  comments (e.g. copyright headers) and symlinked SKILL.md files.  Falls back
  to the GitHub repository description if the SKILL.md has no description.

Usage:
    python pipeline/backfill_metadata.py data/raw/*.jsonl --token ghp_xxx
    python pipeline/backfill_metadata.py data/raw/*.jsonl --descriptions
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from crawlers.base import fetch_repo_metadata, make_session

log = logging.getLogger(__name__)


def _repo_full_name(repo_url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub URL, or None if not GitHub."""
    url = repo_url.rstrip("/")
    if not url.startswith("https://github.com/"):
        return None
    parts = url.removeprefix("https://github.com/").split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def backfill_file(path: str, session, dry_run: bool = False) -> tuple[int, int]:
    """Backfill missing GitHub metadata in one JSONL file.

    Returns:
        (total_records, records_updated)
    """
    p = Path(path)
    records = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    # Find which records need backfill: stars is None/missing OR zero
    # (zero-star records are crawled before the GitHub metadata fetch completes,
    # or from sources that don't query the API at crawl time)
    needs_backfill = [
        (i, r) for i, r in enumerate(records)
        if not r.get("raw_metadata", {}).get("stars")
    ]

    if not needs_backfill:
        log.info("%s: all %d records already have stars — skipping", path, len(records))
        return len(records), 0

    log.info("%s: %d / %d records need backfill", path, len(needs_backfill), len(records))

    # Collect unique repo full names to fetch (deduplicate API calls)
    unique_repos: set[str] = set()
    for _, rec in needs_backfill:
        fn = _repo_full_name(rec.get("repo_url", ""))
        if fn:
            unique_repos.add(fn)

    # Parallel fetch with a rate-limit-aware semaphore (max 4 concurrent requests)
    _semaphore = threading.Semaphore(4)
    repo_cache: dict[str, dict] = {}
    repo_cache_lock = threading.Lock()

    def _fetch_one(full_name: str) -> tuple[str, dict]:
        with _semaphore:
            meta = fetch_repo_metadata(session, full_name)
        return full_name, meta

    log.info("Fetching metadata for %d unique repos (parallel, max 4 workers)", len(unique_repos))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_one, fn): fn for fn in unique_repos}
        done = 0
        for fut in as_completed(futures):
            full_name, meta = fut.result()
            with repo_cache_lock:
                repo_cache[full_name] = meta
            done += 1
            if done % 50 == 0:
                log.info("  %d / %d repos fetched", done, len(unique_repos))

    # Apply fetched metadata back to records
    updated = 0
    for _, rec in needs_backfill:
        repo_url = rec.get("repo_url", "")
        full_name = _repo_full_name(repo_url)
        if not full_name:
            log.debug("Skipping non-GitHub URL: %s", repo_url)
            continue

        meta = repo_cache.get(full_name)
        if not meta:
            continue

        raw = rec.setdefault("raw_metadata", {})
        raw["stars"] = meta.get("stargazers_count", 0)
        raw["pushed_at"] = meta.get("pushed_at", "")
        if "default_branch" not in raw:
            raw["default_branch"] = meta.get("default_branch", "main")
        updated += 1

    if dry_run:
        log.info("Dry run — not writing back %d updates to %s", updated, path)
        return len(records), updated

    # Atomic write: tmp → replace
    tmp = p.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(p)

    log.info("%s: wrote %d records (%d updated)", path, len(records), updated)
    return len(records), updated


def backfill_descriptions(path: str, session, dry_run: bool = False) -> tuple[int, int]:
    """Re-fetch and re-parse SKILL.md for records with an empty description.

    Uses the updated ``_parse_frontmatter`` from each crawler so that files
    starting with HTML comments (e.g. copyright headers) are handled correctly,
    and symlinked SKILL.md files are resolved and read.

    Returns:
        (total_records, records_updated)
    """
    import base64
    import posixpath
    import re

    import yaml

    def _parse_frontmatter_fixed(content: str) -> dict:
        """Local copy of the fixed frontmatter parser (strips leading HTML comments)."""
        if not content:
            return {}
        stripped = re.sub(r"^(\s*<!--.*?-->\s*)+", "", content, flags=re.DOTALL)
        if not stripped.startswith("---"):
            return {}
        end_match = re.search(r"\n---\s*\n", stripped[3:])
        if not end_match:
            return {}
        yaml_text = stripped[3: end_match.start() + 3]
        try:
            fm = yaml.safe_load(yaml_text)
            return fm if isinstance(fm, dict) else {}
        except yaml.YAMLError:
            return {}

    def _fetch_content(repo_full_name: str, path: str, branch: str, depth: int = 0) -> str | None:
        """Fetch file content via Contents API, resolving symlinks."""
        if depth > 1:
            return None
        from crawlers.base import github_get
        url = f"https://api.github.com/repos/{repo_full_name}/contents/{path}"
        try:
            data = github_get(session, url, params={"ref": branch})
        except RuntimeError:
            return None
        if data is None:
            return None
        if data.get("type") == "symlink":
            target = data.get("target", "")
            if not target:
                return None
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            return _fetch_content(repo_full_name, resolved, branch, depth + 1)
        try:
            encoded = data.get("content", "")
            return base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
        except Exception:
            return None

    p = Path(path)
    records = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    needs_backfill = [
        (i, r) for i, r in enumerate(records)
        if not r.get("description")
    ]

    if not needs_backfill:
        log.info("%s: all %d records already have descriptions — skipping", path, len(records))
        return len(records), 0

    log.info("%s: %d / %d records need description backfill", path, len(needs_backfill), len(records))

    updated = 0
    for i, (idx, rec) in enumerate(needs_backfill):
        meta = rec.get("raw_metadata", {})
        skill_md_url = meta.get("skill_md_url", "")
        repo_url = rec.get("repo_url", "")

        if not skill_md_url or not repo_url:
            continue

        # Extract owner/repo and branch from skill_md_url
        # e.g. https://github.com/owner/repo/blob/main/path/to/SKILL.md
        parts = skill_md_url.removeprefix("https://github.com/").split("/")
        if len(parts) < 4 or parts[2] != "blob":
            continue
        repo_full_name = f"{parts[0]}/{parts[1]}"
        branch = parts[3]
        skill_path = "/".join(parts[4:])

        content = _fetch_content(repo_full_name, skill_path, branch)
        desc = ""
        if content:
            fm = _parse_frontmatter_fixed(content)
            desc = fm.get("description", "")

        # Fallback: use GitHub repo description if SKILL.md has none
        if not desc:
            from crawlers.base import fetch_repo_metadata
            meta_gh = fetch_repo_metadata(session, repo_full_name)
            desc = meta_gh.get("description", "")

        if desc:
            records[idx]["description"] = desc
            updated += 1
            log.debug("Updated description for %s (%s)", rec.get("name"), repo_url)

        if (i + 1) % 100 == 0:
            log.info("  %d / %d processed, %d updated so far", i + 1, len(needs_backfill), updated)

    if dry_run:
        log.info("Dry run — not writing back %d description updates to %s", updated, path)
        return len(records), updated

    tmp = p.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(p)

    log.info("%s: wrote %d records (%d description updates)", path, len(records), updated)
    return len(records), updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill missing GitHub metadata (stars, pushed_at) in raw JSONL files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="JSONL",
        help="Raw crawler JSONL files to backfill.",
    )
    parser.add_argument(
        "--token",
        default=None,
        metavar="TOKEN",
        help="GitHub personal access token (or set GITHUB_TOKEN env var).",
    )
    parser.add_argument(
        "--descriptions",
        action="store_true",
        help="Re-fetch SKILL.md for records with empty descriptions (fixes HTML comment / symlink issues).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch metadata and report what would change, but don't write files.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        stream=sys.stderr,
    )

    token = args.token or os.environ.get("GITHUB_TOKEN")
    if not token:
        log.warning("No GITHUB_TOKEN — unauthenticated requests are rate-limited to 60/hr")

    session = make_session(token=token)

    total_records = total_updated = 0
    for fpath in args.files:
        try:
            if args.descriptions:
                n, u = backfill_descriptions(fpath, session, dry_run=args.dry_run)
            else:
                n, u = backfill_file(fpath, session, dry_run=args.dry_run)
            total_records += n
            total_updated += u
        except FileNotFoundError:
            log.error("File not found: %s", fpath)
            return 1

    log.info("Done. %d / %d records updated across %d file(s).",
             total_updated, total_records, len(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
