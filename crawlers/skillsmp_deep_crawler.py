"""
crawlers/skillsmp_deep_crawler.py — Triple-sharded GitHub Code Search crawler.

Extends the standard SkillsMP crawler with a third sharding axis (repo
pushed-date) to collect up to TARGET_PER_CELL results per (size, star) cell,
bypassing GitHub Code Search's hard 1,000-result cap per query.

Sharding axes:
    size × star × pushed_date  →  4 × 4 × 10 = 160 sub-queries
    Each sub-query capped at 1,000 → theoretical max: 160,000 unique repos.
    Per (size, star) cell target: 10,000 (configurable via --target).

Resume / reuse:
    A JSON state file (--state, default data/deep_crawl_state.json) tracks,
    for each (size, star) cell:
      - how many unique repos have been collected so far
      - which date shards have been fully exhausted (no more results)
    On restart, already-exhausted date shards are skipped and already-seen
    repo URLs are loaded from the output file to avoid duplicates.

CLI:
    python -m crawlers.skillsmp_deep_crawler -o data/raw/skillsmp.jsonl \\
        [--state data/deep_crawl_state.json] [--target 10000] \\
        [--token TOKEN] [--limit-per-cell N] [--resume] [--cell SIZE STAR]

The GITHUB_TOKEN environment variable is used when --token is not supplied.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import pathlib
import sys
import time
from typing import Iterator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crawlers.base import (
    GITHUB_API,
    extract_github_url,
    fetch_repo_metadata,
    fetch_skill_md,
    github_get,
    load_existing_urls,
    load_filter_cache,
    make_session,
    write_jsonl,
)
from crawlers.skillsmp_crawler import (
    SIZE_SHARDS,
    STAR_SHARDS,
    _BASE_QUERY,
    _OVERFLOW_THRESHOLD,
    build_raw_record,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Third sharding axis: repo push date
# ---------------------------------------------------------------------------

# Ten non-overlapping date ranges.  Each (size, star, date) sub-query is
# independently capped at 1,000 results → 10 × 1,000 = 10,000 per cell.
DATE_SHARDS: list[str] = [
    "pushed:<2018-01-01",
    "pushed:2018-01-01..2019-01-01",
    "pushed:2019-01-01..2020-01-01",
    "pushed:2020-01-01..2021-01-01",
    "pushed:2021-01-01..2022-01-01",
    "pushed:2022-01-01..2023-01-01",
    "pushed:2023-01-01..2024-01-01",
    "pushed:2024-01-01..2025-01-01",
    "pushed:2025-01-01..2026-01-01",
    "pushed:>=2026-01-01",
]

DEFAULT_TARGET = 10_000
DEFAULT_STATE_PATH = "data/deep_crawl_state.json"

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def load_state(state_path: str) -> dict:
    """Load the per-cell crawl state from a JSON file.

    Returns a dict keyed by cell_key (e.g. "size:1..500|stars:0").
    Each value is:
        {
          "collected": int,           # unique repos collected for this cell
          "exhausted_date_shards": [] # date shards with no more results
        }
    Returns an empty dict if the file does not exist.
    """
    p = pathlib.Path(state_path)
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load state from %s: %s — starting fresh", state_path, exc)
        return {}


def save_state(state: dict, state_path: str) -> None:
    """Persist the per-cell state dict to disk (atomic write via temp file)."""
    p = pathlib.Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        tmp.replace(p)
    except OSError as exc:
        logger.error("Failed to save state to %s: %s", state_path, exc)


def cell_key(size_shard: str, star_shard: str) -> str:
    return f"{size_shard}|{star_shard}"


def get_cell_state(state: dict, key: str) -> dict:
    """Return the mutable state sub-dict for a cell, creating it if absent."""
    if key not in state:
        state[key] = {"collected": 0, "exhausted_date_shards": []}
    return state[key]


# ---------------------------------------------------------------------------
# Search with overflow detection (mirrors skillsmp_crawler logic)
# ---------------------------------------------------------------------------


def _search_date_shard(
    session,
    query_extra: str,
    per_page: int = 100,
    limit: int | None = None,
) -> Iterator[dict]:
    """Yield raw GitHub Code Search items for a single (size, star, date) sub-query.

    Handles pagination, intra-shard repo deduplication, rate-limit waits,
    and overflow detection.  Mirrors search_skill_repos() in the standard
    crawler but always uses per_page=100 for efficiency.
    """
    per_page = min(per_page, 100)
    yielded = 0
    seen_repo_ids: set[int] = set()
    query = f"{_BASE_QUERY} {query_extra}"

    logger.debug("Sub-query: %s", query)

    page = 1
    while True:
        if limit is not None and yielded >= limit:
            return

        params = {"q": query, "per_page": per_page, "page": page}
        try:
            data = github_get(session, f"{GITHUB_API}/search/code", params=params)
        except RuntimeError as exc:
            logger.error("Search failed on page %d for %r: %s", page, query, exc)
            break

        items = data.get("items", [])
        if not items:
            break

        total_count = data.get("total_count", 0)
        if page == 1 and total_count >= _OVERFLOW_THRESHOLD:
            logger.warning(
                "Sub-query overflow (total_count=%d) for %r — "
                "results capped at 1,000; consider a finer date range.",
                total_count,
                query,
            )

        for item in items:
            if limit is not None and yielded >= limit:
                return
            repo = item.get("repository", {})
            repo_id = repo.get("id")
            if repo_id and repo_id in seen_repo_ids:
                continue
            if repo_id:
                seen_repo_ids.add(repo_id)
            yield item
            yielded += 1

        logger.debug("Page %d: %d items (total_count=%d)", page, len(items), total_count)
        if page * per_page >= total_count:
            break

        page += 1
        time.sleep(2)  # Code Search: 10 req/min for authenticated users


# ---------------------------------------------------------------------------
# Core orchestrator
# ---------------------------------------------------------------------------


def run(  # noqa: PLR0912, PLR0915
    output_path: str,
    state_path: str = DEFAULT_STATE_PATH,
    target_per_cell: int = DEFAULT_TARGET,
    token: str | None = None,
    limit_per_cell: int | None = None,
    resume: bool = False,
    filter_cache_path: str | None = None,
    only_cell: tuple[str, str] | None = None,
) -> int:
    """Run the deep crawl end to end.

    For each (size, star) cell:
      1. Skip if already collected >= target_per_cell.
      2. For each date shard not yet exhausted for this cell:
         a. Issue the triple-sharded sub-query.
         b. For each result, fetch SKILL.md and emit a raw record.
         c. Mark the date shard exhausted if sub-query returned < 950 items.
      3. Flush batch, update state, and move to the next cell.

    Args:
        output_path:      Destination JSONL file (appended to if resume=True).
        state_path:       JSON file tracking per-cell crawl progress.
        target_per_cell:  Stop collecting for a cell once this many unique
                          repos have been emitted (default: 10,000).
        token:            GitHub personal access token.
        limit_per_cell:   Hard cap on sub-query results per cell (for testing).
        resume:           Load existing state and output to skip already-done work.
        filter_cache_path: Shared filter-cache JSONL; repos in cache are skipped.
        only_cell:        If set, process only this (size_shard, star_shard) pair.

    Returns:
        Total number of new records written in this run.
    """
    session = make_session(token=token)

    # Load filter cache
    filter_cache: set[str] = set()
    if filter_cache_path:
        filter_cache = load_filter_cache(filter_cache_path)
        logger.info("Filter cache loaded: %d entries", len(filter_cache))

    # Load existing repo URLs to avoid duplicates
    existing_urls: set[str] = set()
    if resume:
        existing_urls = load_existing_urls(output_path)
        logger.info("Resume mode: %d repos already in output", len(existing_urls))

    # Load per-cell state
    state: dict = {}
    if resume:
        state = load_state(state_path)
        logger.info("State loaded: %d cells tracked", len(state))

    # Build list of cells to process
    if only_cell is not None:
        cells = [only_cell]
    else:
        cells = list(itertools.product(SIZE_SHARDS, STAR_SHARDS))

    logger.info(
        "Deep crawl: %d cells, target %d per cell, %d date shards each",
        len(cells), target_per_cell, len(DATE_SHARDS),
    )

    # Cross-run dedup: full_name seen in this run (across all cells/date-shards)
    seen_full_names: set[str] = set()
    # Pre-populate from existing output so we never re-fetch repos collected
    # in a previous run (even if they came from a different cell).
    for url in existing_urls:
        # url is "https://github.com/owner/repo" — extract "owner/repo"
        parts = url.removeprefix("https://github.com/").split("/")
        if len(parts) == 2:
            seen_full_names.add("/".join(parts))

    total_written = 0
    batch: list[dict] = []
    batch_size = 50
    first_write = not resume  # overwrite on a fresh run, append on resume

    def _flush(force: bool = False) -> None:
        nonlocal batch, total_written, first_write
        if not batch:
            return
        if force or len(batch) >= batch_size:
            append = not first_write or total_written > 0 or resume
            written = write_jsonl(batch, output_path, append=append)
            total_written += written
            first_write = False
            batch = []

    for size_shard, star_shard in cells:
        key = cell_key(size_shard, star_shard)
        cell_st = get_cell_state(state, key)

        already = cell_st["collected"]
        if already >= target_per_cell:
            logger.info("Cell %s: already at target (%d). Skipping.", key, already)
            continue

        exhausted = set(cell_st.get("exhausted_date_shards", []))
        remaining_target = target_per_cell - already
        logger.info(
            "Cell %s: need %d more (have %d), %d/%d date shards exhausted",
            key, remaining_target, already, len(exhausted), len(DATE_SHARDS),
        )

        cell_new = 0  # new records collected for this cell in this run

        for date_shard in DATE_SHARDS:
            if remaining_target - cell_new <= 0:
                break
            if date_shard in exhausted:
                logger.debug("Cell %s | %s: exhausted, skipping", key, date_shard)
                continue

            query_extra = f"{size_shard} {star_shard} {date_shard}"
            logger.info("Cell %s | %s: querying", key, date_shard)

            date_shard_count = 0  # items yielded by this date shard sub-query

            for item in _search_date_shard(
                session,
                query_extra=query_extra,
                limit=limit_per_cell,
            ):
                date_shard_count += 1
                repo = item.get("repository", {})
                if not repo:
                    continue

                owner = repo.get("owner", {})
                owner_login = owner.get("login", "") if isinstance(owner, dict) else str(owner)
                repo_name = repo.get("name", "")
                full_name = repo.get("full_name", f"{owner_login}/{repo_name}")

                # Cross-cell dedup
                if full_name in seen_full_names:
                    continue
                seen_full_names.add(full_name)

                html_url = repo.get("html_url", f"https://github.com/{full_name}")
                repo_url = extract_github_url(html_url)
                if not repo_url:
                    continue

                if repo_url in filter_cache:
                    logger.debug("Skipping %s: in filter cache", repo_url)
                    continue
                if repo_url in existing_urls:
                    logger.debug("Skipping %s: already in output", repo_url)
                    continue

                # Fetch enriched metadata
                meta = fetch_repo_metadata(session, full_name)
                repo_enriched = {**repo, **meta}

                default_branch = meta.get("default_branch", "main")
                skill_path = item.get("path", "SKILL.md")
                skill_content = fetch_skill_md(
                    session, full_name, path=skill_path, default_branch=default_branch
                )
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
                cell_new += 1

                _flush()

                if remaining_target - cell_new <= 0:
                    break

            # Mark date shard exhausted if it returned fewer results than the
            # overflow threshold (meaning we got everything it had).
            if date_shard_count < _OVERFLOW_THRESHOLD:
                exhausted.add(date_shard)
                logger.debug("Cell %s | %s: exhausted (%d items)", key, date_shard, date_shard_count)

        # Flush any remaining batch for this cell
        _flush(force=True)

        # Persist state after each cell so a restart wastes no work
        cell_st["collected"] = already + cell_new
        cell_st["exhausted_date_shards"] = sorted(exhausted)
        save_state(state, state_path)

        logger.info(
            "Cell %s done: +%d new (total %d / %d)",
            key, cell_new, cell_st["collected"], target_per_cell,
        )

    logger.info("Deep crawl finished: %d new records written to %s", total_written, output_path)
    return total_written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Triple-sharded GitHub Code Search crawler (size × stars × pushed-date). "
            f"Collects up to --target repos per (size, star) cell by using {len(DATE_SHARDS)} "
            "date-range sub-queries, each independently capped at 1,000 by GitHub."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Full deep crawl (all 16 cells, target 10k each):\n"
            "  python -m crawlers.skillsmp_deep_crawler -o data/raw/skillsmp.jsonl\n\n"
            "  # Resume a previous run:\n"
            "  python -m crawlers.skillsmp_deep_crawler -o data/raw/skillsmp.jsonl --resume\n\n"
            "  # Single cell, limit 500 per date shard (for testing):\n"
            "  python -m crawlers.skillsmp_deep_crawler -o /tmp/test.jsonl \\\n"
            "      --cell 'size:1..500' 'stars:0' --limit-per-cell 500\n"
        ),
    )
    parser.add_argument(
        "-o", "--output", required=True, metavar="PATH",
        help="Output JSONL file path.",
    )
    parser.add_argument(
        "--state", default=DEFAULT_STATE_PATH, metavar="PATH",
        help=f"Per-cell state JSON file (default: {DEFAULT_STATE_PATH}).",
    )
    parser.add_argument(
        "--target", type=int, default=DEFAULT_TARGET, metavar="N",
        help=f"Target results per (size, star) cell (default: {DEFAULT_TARGET}).",
    )
    parser.add_argument(
        "--token", default=None, metavar="TOKEN",
        help="GitHub personal access token (overrides GITHUB_TOKEN env var).",
    )
    parser.add_argument(
        "--limit-per-cell", type=int, default=None, metavar="N",
        help="Hard cap on items per date-shard sub-query (useful for testing).",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Load state and output file to skip already-collected repos.",
    )
    parser.add_argument(
        "--cell", nargs=2, metavar=("SIZE_SHARD", "STAR_SHARD"),
        help=(
            "Process only this one cell, e.g. 'size:1..500' 'stars:0'. "
            "Must match values from SIZE_SHARDS and STAR_SHARDS exactly."
        ),
    )
    parser.add_argument(
        "--filter-cache", default="data/filter_cache.jsonl", metavar="PATH",
        help="Shared filter-cache JSONL (pass empty string to disable).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable DEBUG logging.",
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
            "No GitHub token provided — unauthenticated requests are heavily rate-limited."
        )

    only_cell: tuple[str, str] | None = None
    if args.cell:
        size_s, star_s = args.cell
        if size_s not in SIZE_SHARDS:
            logger.error(
                "Unknown size shard %r. Valid values: %s", size_s, SIZE_SHARDS
            )
            return 1
        if star_s not in STAR_SHARDS:
            logger.error(
                "Unknown star shard %r. Valid values: %s", star_s, STAR_SHARDS
            )
            return 1
        only_cell = (size_s, star_s)

    filter_cache_path = args.filter_cache or None

    try:
        count = run(
            output_path=args.output,
            state_path=args.state,
            target_per_cell=args.target,
            token=token,
            limit_per_cell=args.limit_per_cell,
            resume=args.resume,
            filter_cache_path=filter_cache_path,
            only_cell=only_cell,
        )
        print(f"Done: {count} new records written to {args.output}")
        return 0
    except Exception as exc:
        logger.error("Deep crawl failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
