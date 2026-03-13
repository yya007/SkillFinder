"""
crawlers/skillsmp_crawler.py — GitHub Code Search crawler for SkillsMP.

Strategy: Use GitHub Code Search API to find repos that contain a SKILL.md
file.  Because the API caps results at 1,000 per query, we shard by pushed-
date ranges so that no single shard exceeds the cap.

Output: data/raw/skillsmp.jsonl (one JSON record per line)

CLI:
    python crawlers/skillsmp_crawler.py -o data/raw/skillsmp.jsonl \\
        [--token TOKEN] [--limit N] [--since YYYY-MM-DD] [--resume]

The GITHUB_TOKEN environment variable is used when --token is not supplied.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import re
import sys
import time
from datetime import date, timedelta
from typing import Iterator

# Ensure project root is on sys.path when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml

from crawlers.base import (
    GITHUB_API,
    extract_github_url,
    github_get,
    load_existing_urls,
    make_session,
    write_jsonl,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Date-range sharding configuration
# ---------------------------------------------------------------------------

# Start of the epoch for sharding.  Skills pushed before this date are
# uncommon enough that a single shard is sufficient.
_SHARD_EPOCH = date(2022, 1, 1)

# Width of each date shard in days.  Narrower = more API calls but each
# shard is less likely to hit the 1,000-result cap.
_SHARD_WIDTH_DAYS = 90

# Search query base — finds any file named SKILL.md (case-insensitive on GH)
_BASE_QUERY = "filename:SKILL.md"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_skill_repos(
    session,
    query_extra: str = "",
    per_page: int = 30,
    limit: int = None,
) -> Iterator[dict]:
    """Search GitHub for repos containing filename:SKILL.md.

    Yields raw GitHub search result items (each has a 'repository' sub-dict).

    Uses date-range sharding over `pushed` dates to exceed the 1,000-result
    cap that the GitHub Code Search API enforces per query.  Each shard covers
    _SHARD_WIDTH_DAYS days from _SHARD_EPOCH up to today.

    Rate limits are respected automatically via github_get().

    Args:
        session:     A requests.Session from make_session().
        query_extra: Additional query terms appended to the base query.
        per_page:    Results per page (max 100).
        limit:       Stop after yielding this many items total (None = no limit).

    Yields:
        GitHub Code Search result items (dicts with at minimum a 'repository'
        key containing the repo metadata).
    """
    per_page = min(per_page, 100)
    yielded = 0
    seen_repo_ids: set[int] = set()

    # Build list of date shards from epoch to today
    today = date.today()
    shards: list[tuple[date, date]] = []
    cursor = _SHARD_EPOCH
    while cursor < today:
        end = min(cursor + timedelta(days=_SHARD_WIDTH_DAYS - 1), today)
        shards.append((cursor, end))
        cursor = end + timedelta(days=1)

    # Also add a catch-all shard for very old repos pushed before the epoch
    shards.insert(0, (date(2008, 1, 1), _SHARD_EPOCH - timedelta(days=1)))

    base_query = _BASE_QUERY
    if query_extra:
        base_query = f"{base_query} {query_extra}"

    for shard_start, shard_end in shards:
        if limit is not None and yielded >= limit:
            return

        date_filter = f"pushed:{shard_start.isoformat()}..{shard_end.isoformat()}"
        query = f"{base_query} {date_filter}"
        logger.debug("Shard query: %s", query)

        page = 1
        while True:
            if limit is not None and yielded >= limit:
                return

            params = {
                "q": query,
                "per_page": per_page,
                "page": page,
            }
            try:
                data = github_get(
                    session,
                    f"{GITHUB_API}/search/code",
                    params=params,
                )
            except RuntimeError as exc:
                logger.error("Search failed for shard %s..%s page %d: %s",
                             shard_start, shard_end, page, exc)
                break

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                if limit is not None and yielded >= limit:
                    return

                repo = item.get("repository", {})
                repo_id = repo.get("id")

                # Deduplicate: a repo can appear multiple times if it has
                # multiple SKILL.md files at different paths
                if repo_id and repo_id in seen_repo_ids:
                    continue
                if repo_id:
                    seen_repo_ids.add(repo_id)

                yield item
                yielded += 1

            # Check if there are more pages
            total_count = data.get("total_count", 0)
            if page * per_page >= total_count or page * per_page >= 1000:
                # GitHub caps at 1,000 results; move to next shard
                break

            page += 1

            # GitHub Search API: 30 requests/minute — add a small delay
            # between pages to stay well under the limit
            time.sleep(2)


def fetch_repo_metadata(session, repo_full_name: str) -> dict:
    """Fetch metadata for a single repo from the GitHub Repos API.

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.

    Returns:
        Dict with keys: stargazers_count, pushed_at, topics, description,
        default_branch.  Returns an empty dict on error.
    """
    try:
        data = github_get(session, f"{GITHUB_API}/repos/{repo_full_name}")
    except RuntimeError as exc:
        logger.warning("Could not fetch metadata for %s: %s", repo_full_name, exc)
        return {}

    return {
        "stargazers_count": data.get("stargazers_count", 0),
        "pushed_at": data.get("pushed_at", ""),
        "topics": data.get("topics", []),
        "description": data.get("description", ""),
        "default_branch": data.get("default_branch", "main"),
    }


def _fetch_skill_md(session, repo_full_name: str, default_branch: str = "main") -> str | None:
    """Fetch raw SKILL.md content for a repo, trying HEAD first then main/master."""
    branches_to_try = [default_branch]
    for fallback in ("main", "master"):
        if fallback not in branches_to_try:
            branches_to_try.append(fallback)

    for branch in branches_to_try:
        url = f"{GITHUB_API}/repos/{repo_full_name}/contents/SKILL.md"
        try:
            data = github_get(session, url, params={"ref": branch})
            encoded = data.get("content", "")
            # GitHub returns base64-encoded content with newlines
            return base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
        except RuntimeError:
            continue

    return None


def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from a SKILL.md string.

    Returns a dict with 'name', 'description', 'triggers' keys (all optional).
    Returns an empty dict if no frontmatter is present or parsing fails.
    """
    if not content or not content.startswith("---"):
        return {}

    # Find the closing ---
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return {}

    yaml_text = content[3: end_match.start() + 3]
    try:
        fm = yaml.safe_load(yaml_text)
        if not isinstance(fm, dict):
            return {}
        return fm
    except yaml.YAMLError:
        return {}


def build_raw_record(repo: dict, skill_md_content: str = None) -> dict:
    """Convert a GitHub repo dict and optional SKILL.md content into a raw record.

    Args:
        repo:             GitHub repo object (from the search result's 'repository'
                          key, or from the Repos API directly).
        skill_md_content: Raw text of SKILL.md, or None if unavailable.

    Returns:
        Raw record dict conforming to the crawler schema.  The repo_url is
        ALWAYS the GitHub URL, never a SkillsMP-specific URL.
    """
    owner = repo.get("owner", {})
    if isinstance(owner, dict):
        owner_login = owner.get("login", "")
    else:
        owner_login = str(owner)

    repo_name = repo.get("name", "")
    full_name = repo.get("full_name", f"{owner_login}/{repo_name}")

    # repo_url must always be the canonical GitHub URL
    repo_url = f"https://github.com/{full_name}".lower()

    frontmatter: dict = {}
    if skill_md_content:
        frontmatter = _parse_frontmatter(skill_md_content)

    name = frontmatter.get("name") or repo_name
    description = (
        frontmatter.get("description")
        or repo.get("description")
        or ""
    )
    triggers = frontmatter.get("triggers") or []
    if not isinstance(triggers, list):
        triggers = [str(triggers)]

    return {
        "repo_url": repo_url,
        "name": name,
        "description": description,
        "source": "skillsmp",
        "raw_metadata": {
            "stars": repo.get("stargazers_count", 0),
            "pushed_at": repo.get("pushed_at", ""),
            "topics": repo.get("topics", []),
            "default_branch": repo.get("default_branch", "main"),
            "triggers": triggers,
        },
    }


def run(
    output_path: str,
    token: str = None,
    limit: int = None,
    since: str = None,
    resume: bool = False,
) -> int:
    """Run the SkillsMP crawler end to end.

    Searches GitHub for SKILL.md files, fetches SKILL.md content and repo
    metadata for each result, and writes raw records to output_path.

    Args:
        output_path: Destination JSONL file.
        token:       GitHub personal access token.
        limit:       Maximum number of records to write (None = unlimited).
        since:       Only include repos pushed after this ISO date (YYYY-MM-DD).
        resume:      If True, skip repos whose URLs are already in output_path.

    Returns:
        Number of records written.
    """
    session = make_session(token=token)

    existing_urls: set[str] = set()
    if resume:
        existing_urls = load_existing_urls(output_path)
        logger.info("Resume mode: skipping %d already-collected repos", len(existing_urls))

    query_extra = ""
    if since:
        query_extra = f"pushed:>{since}"

    batch: list[dict] = []
    batch_size = 50
    written = 0

    for item in search_skill_repos(session, query_extra=query_extra, per_page=30, limit=limit):
        repo = item.get("repository", {})

        if not repo:
            continue

        owner_login = ""
        owner = repo.get("owner", {})
        if isinstance(owner, dict):
            owner_login = owner.get("login", "")
        repo_name = repo.get("name", "")
        full_name = repo.get("full_name", f"{owner_login}/{repo_name}")

        # Verify the repo is actually on GitHub (skip any non-GitHub results)
        html_url = repo.get("html_url", f"https://github.com/{full_name}")
        repo_url = extract_github_url(html_url)
        if not repo_url:
            logger.debug("Skipping non-GitHub repo: %s", html_url)
            continue

        if repo_url in existing_urls:
            logger.debug("Skipping already-seen repo: %s", repo_url)
            continue

        # Fetch enriched repo metadata (stars, topics, pushed_at)
        meta = fetch_repo_metadata(session, full_name)
        repo_enriched = {**repo, **meta}

        # Fetch SKILL.md content
        default_branch = meta.get("default_branch", "main")
        skill_content = _fetch_skill_md(session, full_name, default_branch=default_branch)

        record = build_raw_record(repo_enriched, skill_md_content=skill_content)
        batch.append(record)
        existing_urls.add(repo_url)

        if len(batch) >= batch_size:
            written += write_jsonl(batch, output_path, append=(written > 0 or resume))
            batch = []

        if limit is not None and written + len(batch) >= limit:
            break

    # Flush remaining records
    if batch:
        written += write_jsonl(batch, output_path, append=(written > 0 or resume))

    logger.info("SkillsMP crawler finished: %d records written to %s", written, output_path)
    return written


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl GitHub for SKILL.md repos (SkillsMP source)"
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        metavar="PATH",
        help="Output JSONL file path (e.g. data/raw/skillsmp.jsonl)",
    )
    parser.add_argument(
        "--token",
        default=None,
        metavar="TOKEN",
        help="GitHub personal access token (overrides GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after writing N records (useful for testing)",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="YYYY-MM-DD",
        help="Only include repos pushed after this date",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip repos already present in the output file",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token = args.token or os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.warning(
            "No GitHub token provided. Unauthenticated requests are heavily rate-limited."
        )

    try:
        count = run(
            output_path=args.output,
            token=token,
            limit=args.limit,
            since=args.since,
            resume=args.resume,
        )
        print(f"Done: {count} records written to {args.output}")
        return 0
    except Exception as exc:
        logger.error("Crawler failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
