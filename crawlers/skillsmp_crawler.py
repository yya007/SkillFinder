"""
crawlers/skillsmp_crawler.py — GitHub Code Search crawler for SkillsMP.

Strategy: Use GitHub Code Search API to find repos that contain a SKILL.md
file.  Because the API hard-caps results at 1,000 per query, we shard across
two orthogonal dimensions — file size and repo star count — so that each of
the 16 resulting cells stays well under the cap.  An overflow warning is
logged whenever a cell's total_count still hits 1,000, indicating that cell
may need finer subdivision in a future release.

Output: data/raw/skillsmp.jsonl (one JSON record per line)

CLI:
    python crawlers/skillsmp_crawler.py -o data/raw/skillsmp.jsonl \\
        [--token TOKEN] [--limit N] [--since YYYY-MM-DD] [--resume]

The GITHUB_TOKEN environment variable is used when --token is not supplied.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import os
import sys
import time
from typing import Iterator

# Ensure project root is on sys.path when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crawlers.base import (
    GITHUB_API,
    extract_github_url,
    fetch_repo_metadata,
    fetch_skill_md,
    github_get,
    infer_platforms,
    load_existing_records,
    load_filter_cache,
    make_session,
    parse_frontmatter,
    write_jsonl,
)

logger = logging.getLogger(__name__)

# Base search query — finds files named SKILL.md.
_BASE_QUERY = "filename:SKILL.md"

# Additional base queries to run with the same SIZE_SHARDS × STAR_SHARDS grid.
# marketplace.json discovers marketplace-format repos; path:.agents/skills covers
# the Gemini CLI / Codex convention for SKILL.md placement.
_EXTRA_BASE_QUERIES: list[str] = [
    "filename:marketplace.json",
    "path:.agents/skills filename:SKILL.md",
]

# ---------------------------------------------------------------------------
# Two-dimensional sharding to stay under GitHub Code Search's 1,000-result
# hard cap.  Each (size, stars) pair forms a disjoint query cell; the
# cross-product gives 16 cells with a theoretical maximum of 16,000 results.
#
# If any cell still reports total_count >= 1,000 at runtime, a WARNING is
# logged — that cell needs finer subdivision in the next release.
# ---------------------------------------------------------------------------

# Primary axis: SKILL.md file size in bytes.
SIZE_SHARDS = [
    "size:1..500",
    "size:501..2000",
    "size:2001..10000",
    "size:>10000",
]

# Secondary axis: repo star count.
# Zero-star repos are separated into their own bucket because they make up the
# majority of GitHub repos and would dominate a combined "stars:0..10" cell.
STAR_SHARDS = [
    "stars:0",
    "stars:1..10",
    "stars:11..100",
    "stars:>100",
]

# Threshold at which we warn that a cell may be truncated.
_OVERFLOW_THRESHOLD = 950


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_skill_repos(
    session,
    query_extra: str = "",
    per_page: int = 30,
    limit: int = None,
    since: str = None,
    base_query: str = _BASE_QUERY,
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

    query = base_query
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

        # Warn on first page if this cell is at or near the hard cap — results
        # beyond position 1,000 are silently dropped by GitHub regardless of
        # pagination.  Finer sharding is needed for the flagged cell.
        if page == 1 and total_count >= _OVERFLOW_THRESHOLD:
            logger.warning(
                "Shard overflow detected (total_count=%d >= %d) for query %r. "
                "Results are capped at 1,000; consider adding finer shards.",
                total_count, _OVERFLOW_THRESHOLD, query,
            )

        if page * per_page >= total_count:
            break

        page += 1

        # GitHub Search API: 10 requests/minute for authenticated users —
        # add a small delay between pages to stay well under the limit
        time.sleep(2)


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
        frontmatter = parse_frontmatter(skill_md_content)

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
    mode: str = "full",
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
        mode:              Crawl mode: full, incremental, metadata, or discover.
                           incremental behaves like resume=True.

    Returns:
        Number of records written.
    """
    # Resolve mode: incremental aliases resume behaviour
    if mode == "incremental":
        resume = True

    session = make_session(token=token)

    # Load filter cache
    filter_cache: set[str] = set()
    if filter_cache_path:
        filter_cache = load_filter_cache(filter_cache_path)
        logger.info("Filter cache loaded: %d entries", len(filter_cache))

    # Load existing records for resume mode, keyed by skill_md_url (or repo_url
    # for legacy records without skill_md_url).  Using skill_md_url as the key
    # allows multiple SKILL.md files from the same monorepo to be tracked
    # independently — a repo_url-keyed set would block all-but-first.
    existing_records: dict[str, dict] = {}
    if resume:
        existing_records = load_existing_records(output_path)
        logger.info("Resume mode: %d skill keys already in output", len(existing_records))

    # Dedup across size shards and base queries.
    # Key: (full_name, skill_path) — allows multiple SKILL.md files from the
    # same monorepo to be indexed in one run (fixes the monorepo dedup bug
    # where seen_full_names keyed by full_name alone dropped all-but-first).
    seen_skill_keys: set[tuple[str, str]] = set()

    batch: list[dict] = []
    batch_size = 50
    written = 0

    shard_cells = list(itertools.product(SIZE_SHARDS, STAR_SHARDS))
    all_base_queries = [_BASE_QUERY] + _EXTRA_BASE_QUERIES
    logger.info(
        "Starting crawl: %d shard cells (%d size × %d star buckets) × %d base queries",
        len(shard_cells), len(SIZE_SHARDS), len(STAR_SHARDS), len(all_base_queries),
    )

    for base_query in all_base_queries:
        logger.info("Starting sharded search for base query: %s", base_query)
        for size_shard, star_shard in shard_cells:
            shard = f"{size_shard} {star_shard}"
            logger.info("Searching shard: %s [base: %s]", shard, base_query)
            for item in search_skill_repos(
                session, query_extra=shard, per_page=30, limit=limit, since=since,
                base_query=base_query,
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

                # Determine skill path before dedup so the key includes the path
                if base_query == "filename:marketplace.json":
                    skill_path = "SKILL.md"
                else:
                    skill_path = item.get("path", "SKILL.md")

                # Dedup across shards and base queries by (full_name, skill_path) —
                # allows monorepos with multiple SKILL.md files to be indexed fully.
                skill_run_key = (full_name, skill_path)
                if skill_run_key in seen_skill_keys:
                    continue
                seen_skill_keys.add(skill_run_key)

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

                # Fetch enriched repo metadata (stars, pushed_at, default_branch)
                meta = fetch_repo_metadata(session, full_name)
                repo_enriched = {**repo, **meta}

                default_branch = meta.get("default_branch", "main")

                # skill_md_url is always set from the resolved skill_path
                skill_md_url = (
                    f"https://github.com/{full_name}/blob/{default_branch}/{skill_path}"
                )

                # Resume check: keyed by skill_md_url (monorepo-safe) with
                # repo_url fallback for legacy records without skill_md_url.
                if resume:
                    if skill_md_url in existing_records or repo_url in existing_records:
                        logger.debug("Skipping already-crawled skill (resume): %s", skill_md_url)
                        continue

                # Fetch SKILL.md content.  For marketplace.json queries the search
                # result path is the JSON file, not a SKILL.md — fall back to the
                # repo root so we can still validate via frontmatter name check.
                skill_content = fetch_skill_md(
                    session, full_name, path=skill_path, default_branch=default_branch
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

                if len(batch) >= batch_size:
                    written += write_jsonl(batch, output_path, append=(written > 0 or resume))
                    batch = []

            # After each cell, flush any accumulated batch before moving to next cell
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
        "--mode",
        choices=["full", "incremental", "metadata", "discover"],
        default="full",
        help="Crawl mode: full=complete re-crawl, incremental=changed repos only, metadata=stars/ETags only, discover=new repos since last run",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="[Deprecated] Alias for --mode incremental",
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

    # Deprecated flag aliases
    if args.resume:
        import warnings
        warnings.warn("--resume is deprecated, use --mode incremental", DeprecationWarning)
        if args.mode == "full":
            args.mode = "incremental"

    filter_cache_path = args.filter_cache or None

    try:
        count = run(
            output_path=args.output,
            token=token,
            limit=args.limit,
            since=args.since,
            resume=args.resume,
            filter_cache_path=filter_cache_path,
            mode=args.mode,
        )
        print(f"Done: {count} records written to {args.output}")
        return 0
    except Exception as exc:
        logger.error("Crawler failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
