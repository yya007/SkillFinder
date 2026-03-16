"""
pipeline/backfill_metadata.py — Backfill missing GitHub metadata in raw JSONL files.

For each record where raw_metadata.stars is None (not fetched at crawl time),
calls the GitHub API to retrieve stargazers_count, pushed_at, and default_branch,
then writes the enriched records back to the same file atomically.

Deduplicates GitHub API calls: each unique repo_url is fetched at most once.

Usage:
    python pipeline/backfill_metadata.py data/raw/marketplace.jsonl [...]
    python pipeline/backfill_metadata.py data/raw/*.jsonl --token ghp_xxx
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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

    # Find which records need backfill (stars is None/missing)
    needs_backfill = [
        (i, r) for i, r in enumerate(records)
        if r.get("raw_metadata", {}).get("stars") is None
    ]

    if not needs_backfill:
        log.info("%s: all %d records already have stars — skipping", path, len(records))
        return len(records), 0

    log.info("%s: %d / %d records need backfill", path, len(needs_backfill), len(records))

    # Deduplicate GitHub API calls by repo_url
    repo_cache: dict[str, dict] = {}
    updated = 0

    for idx, (i, rec) in enumerate(needs_backfill):
        repo_url = rec.get("repo_url", "")
        full_name = _repo_full_name(repo_url)
        if not full_name:
            log.debug("Skipping non-GitHub URL: %s", repo_url)
            continue

        if full_name not in repo_cache:
            log.debug("[%d/%d] Fetching %s", idx + 1, len(needs_backfill), full_name)
            meta = fetch_repo_metadata(session, full_name)
            repo_cache[full_name] = meta

        meta = repo_cache[full_name]
        if not meta:
            continue

        raw = rec.setdefault("raw_metadata", {})
        raw["stars"] = meta.get("stargazers_count", 0)
        raw["pushed_at"] = meta.get("pushed_at", "")
        if "default_branch" not in raw:
            raw["default_branch"] = meta.get("default_branch", "main")
        updated += 1

        if (idx + 1) % 100 == 0:
            log.info("  %d / %d done (%d unique repos fetched so far)",
                     idx + 1, len(needs_backfill), len(repo_cache))

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
