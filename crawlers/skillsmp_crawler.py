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
from typing import Iterator

# Ensure project root is on sys.path when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml

from crawlers.base import (
    GITHUB_API,
    extract_github_url,
    fetch_repo_metadata,
    github_get,
    infer_platforms,
    load_existing_urls,
    load_filter_cache,
    make_session,
    write_jsonl,
)

logger = logging.getLogger(__name__)

# Base search query — finds files named SKILL.md.
_BASE_QUERY = "filename:SKILL.md"

# File-size shards to bypass GitHub Code Search's 1000-result hard cap.
# Each shard covers a disjoint byte range of SKILL.md files; combining all
# four shards collects the full result set without hitting the cap.
SIZE_SHARDS = [
    "size:1..500",
    "size:501..2000",
    "size:2001..10000",
    "size:>10000",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_skill_repos(
    session,
    query_extra: str = "",
    per_page: int = 30,
    limit: int = None,
    since: str = None,
) -> Iterator[dict]:
    """Search GitHub for repos containing filename:SKILL.md.

    Yields raw GitHub search result items (each has a 'repository' sub-dict).

    Deduplicates within a single call by repo ID (a repo with multiple
    SKILL.md files at different paths appears only once).  Cross-shard
    deduplication by full_name is done in run().

    Rate limits are respected automatically via github_get().

    Args:
        session:     A requests.Session from make_session().
        query_extra: Additional query terms appended to the base query
                     (e.g. a ``size:`` shard qualifier).
        per_page:    Results per page (max 100).
        limit:       Stop after yielding this many items (None = no limit).
        since:       Accepted but unused (code search has no date qualifier).

    Yields:
        GitHub Code Search result items (dicts with at minimum a 'repository'
        key containing the repo metadata).
    """
    per_page = min(per_page, 100)
    yielded = 0
    seen_repo_ids: set[int] = set()

    if since:
        logger.warning(
            "--since is not supported by GitHub Code Search and will be ignored."
        )

    query = _BASE_QUERY
    if query_extra:
        query = f"{query} {query_extra}"

    logger.debug("Search query: %s", query)

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
            logger.error("Search failed on page %d: %s", page, exc)
            break

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            if limit is not None and yielded >= limit:
                return

            repo = item.get("repository", {})
            repo_id = repo.get("id")

            # Deduplicate within a shard: a repo with multiple SKILL.md files
            # may appear multiple times; yield only the first occurrence.
            if repo_id and repo_id in seen_repo_ids:
                continue
            if repo_id:
                seen_repo_ids.add(repo_id)

            yield item
            yielded += 1

        total_count = data.get("total_count", 0)
        logger.debug("Page %d: %d items (total_count=%d)", page, len(items), total_count)
        if page * per_page >= total_count:
            break

        page += 1

        # GitHub Search API: 10 requests/minute for authenticated users —
        # add a small delay between pages to stay well under the limit
        time.sleep(2)


def _fetch_skill_md(
    session,
    repo_full_name: str,
    path: str = "SKILL.md",
    default_branch: str = "main",
) -> str | None:
    """Fetch raw SKILL.md content using the path reported by code search.

    Uses the exact path from the search result item so we don't blindly assume
    the file is at the repo root.  Falls back to main/master when the default
    branch 404s.  Uses session.get() directly (not github_get) to avoid
    wasting retry quota on expected 404s.
    """
    branches_to_try = [default_branch]
    for fallback in ("main", "master"):
        if fallback not in branches_to_try:
            branches_to_try.append(fallback)

    for branch in branches_to_try:
        url = f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}"
        try:
            resp = session.get(url, params={"ref": branch}, timeout=30)
        except Exception as exc:
            logger.debug("Network error fetching %s@%s: %s", path, branch, exc)
            continue

        if resp.status_code == 200:
            try:
                encoded = resp.json().get("content", "")
                return base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
            except Exception:
                continue
        # 404 means file not at this path/branch — try next branch silently

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


def build_raw_record(repo: dict, skill_md_content: str = None, skill_md_url: str = "") -> dict | None:
    """Convert a GitHub repo dict and optional SKILL.md content into a raw record.

    Returns None if the SKILL.md has no ``name`` field in its frontmatter —
    this filters out the large volume of false positives (files named *skill.md
    that are not agent skills) that GitHub Code Search returns.

    Args:
        repo:             GitHub repo object (from the search result's 'repository'
                          key, or from the Repos API directly).
        skill_md_content: Raw text of SKILL.md, or None if unavailable.
        skill_md_url:     Direct URL to SKILL.md blob on GitHub.

    Returns:
        Raw record dict conforming to the crawler schema, or None if the file
        lacks a frontmatter ``name`` field (i.e. not a real agent skill).
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

    # Require a name in the SKILL.md frontmatter — repos without one are false
    # positives (incidental files named *skill.md, not real agent skills).
    name = frontmatter.get("name", "")
    if not name:
        return None

    description = (
        frontmatter.get("description")
        or repo.get("description")
        or ""
    )

    platforms = infer_platforms(frontmatter, "skillsmp")

    return {
        "repo_url": repo_url,
        "name": name,
        "description": description,
        "source": "skillsmp",
        "raw_metadata": {
            "stars": repo.get("stargazers_count", 0),
            "pushed_at": repo.get("pushed_at", ""),
            "default_branch": repo.get("default_branch", "main"),
            "skill_md_url": skill_md_url,
            "platforms": platforms,
        },
    }


def run(
    output_path: str,
    token: str = None,
    limit: int = None,
    since: str = None,
    resume: bool = False,
    filter_cache_path: str = None,
) -> int:
    """Run the SkillsMP crawler end to end.

    Searches GitHub for SKILL.md files across SIZE_SHARDS (to bypass the
    1000-result cap), fetches SKILL.md content and repo metadata for each
    result, and writes raw records to output_path.

    Args:
        output_path:       Destination JSONL file.
        token:             GitHub personal access token.
        limit:             Per-shard result limit (None = unlimited).  Total
                           output may be up to ``len(SIZE_SHARDS) * limit``.
        since:             Accepted but unused (code search has no date qualifier).
        resume:            If True, skip repos whose URLs are already in output_path.
        filter_cache_path: Path to the shared filter cache JSONL file.  When
                           set, repos in the cache are skipped and newly
                           rejected repos are added to it.  Pass None to
                           disable filter-cache behaviour.

    Returns:
        Number of records written.
    """
    session = make_session(token=token)

    # Load filter cache
    filter_cache: set[str] = set()
    if filter_cache_path:
        filter_cache = load_filter_cache(filter_cache_path)
        logger.info("Filter cache loaded: %d entries", len(filter_cache))

    existing_urls: set[str] = set()
    if resume:
        existing_urls = load_existing_urls(output_path)
        logger.info("Resume mode: skipping %d already-collected repos", len(existing_urls))

    # Dedup across size shards — a repo with SKILL.md files of different
    # sizes can appear in multiple shards.
    seen_full_names: set[str] = set()

    batch: list[dict] = []
    batch_size = 50
    written = 0

    for shard in SIZE_SHARDS:
        logger.info("Searching shard: %s", shard)
        for item in search_skill_repos(
            session, query_extra=shard, per_page=30, limit=limit, since=since
        ):
            repo = item.get("repository", {})
            if not repo:
                continue

            owner_login = ""
            owner = repo.get("owner", {})
            if isinstance(owner, dict):
                owner_login = owner.get("login", "")
            repo_name = repo.get("name", "")
            full_name = repo.get("full_name", f"{owner_login}/{repo_name}")

            # Dedup across shards
            if full_name in seen_full_names:
                continue
            seen_full_names.add(full_name)

            # Verify the repo is actually on GitHub
            html_url = repo.get("html_url", f"https://github.com/{full_name}")
            repo_url = extract_github_url(html_url)
            if not repo_url:
                logger.debug("Skipping non-GitHub repo: %s", html_url)
                continue

            # Check filter cache
            if repo_url in filter_cache:
                logger.debug("Skipping %s: in filter cache", repo_url)
                continue

            if repo_url in existing_urls:
                logger.debug("Skipping already-seen repo: %s", repo_url)
                continue

            # Fetch enriched repo metadata (stars, pushed_at, default_branch)
            meta = fetch_repo_metadata(session, full_name)
            repo_enriched = {**repo, **meta}

            # Fetch SKILL.md content using the exact path from the search result
            default_branch = meta.get("default_branch", "main")
            skill_path = item.get("path", "SKILL.md")
            skill_content = _fetch_skill_md(
                session, full_name, path=skill_path, default_branch=default_branch
            )

            # skill_md_url is always set from the code-search path — the file
            # is known to exist even if content fetch fails (e.g. network hiccup).
            skill_md_url = (
                f"https://github.com/{full_name}/blob/{default_branch}/{skill_path}"
            )
            record = build_raw_record(
                repo_enriched,
                skill_md_content=skill_content,
                skill_md_url=skill_md_url,
            )
            if record is None:
                logger.debug("Skipping %s: no 'name' in SKILL.md frontmatter", full_name)
                if filter_cache_path:
                    from crawlers.base import add_to_filter_cache
                    add_to_filter_cache(filter_cache_path, repo_url, "not_a_skill")
                    filter_cache.add(repo_url)
                continue
            batch.append(record)
            existing_urls.add(repo_url)

            if len(batch) >= batch_size:
                written += write_jsonl(batch, output_path, append=(written > 0 or resume))
                batch = []

        # After each shard, flush any accumulated batch before moving to next shard
        if batch:
            written += write_jsonl(batch, output_path, append=(written > 0 or resume))
            batch = []

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
        "--filter-cache",
        default="data/filter_cache.jsonl",
        metavar="PATH",
        help="Shared filter-cache JSONL file (pass empty string to disable)",
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

    filter_cache_path = args.filter_cache or None

    try:
        count = run(
            output_path=args.output,
            token=token,
            limit=args.limit,
            since=args.since,
            resume=args.resume,
            filter_cache_path=filter_cache_path,
        )
        print(f"Done: {count} records written to {args.output}")
        return 0
    except Exception as exc:
        logger.error("Crawler failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
