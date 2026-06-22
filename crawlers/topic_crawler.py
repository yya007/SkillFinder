"""
crawlers/topic_crawler.py — GitHub topic-based skill repo discovery.

Strategy: Search GitHub for repos tagged with skill-related topics.
For each discovered repo, walk the tree for SKILL.md files and build records.

Dedup: Load repo_url values from existing raw JSONL files (--data-dir) at
startup to skip repos already covered by other crawlers (saves API quota).

Output: data/raw/topic.jsonl
Each record conforms to the raw record schema defined in PRD-001.

Source tag: "topic" (not in CURATED_SOURCES → requires stars >= 10 to pass
quality filter in pipeline/normalize.py).

CLI:
    python -m crawlers.topic_crawler -o data/raw/topic.jsonl \\
        [--token TOKEN] [--limit N] [--resume] \\
        [--data-dir data/raw] [--filter-cache data/filter_cache.jsonl]

The GITHUB_TOKEN environment variable is used when --token is not supplied.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crawlers.base import (
    GITHUB_API,
    _utc_now_iso,
    add_to_filter_cache,
    fetch_repo_metadata_batch,
    fetch_repo_metadata_cached,
    fetch_skill_md_cached,
    find_skill_md_paths_cached,
    github_get,
    infer_platforms,
    load_content_cache,
    load_crawl_state,
    load_filter_cache,
    load_meta_cache,
    load_tree_cache,
    make_session,
    parse_frontmatter,
    save_content_cache,
    save_crawl_state,
    save_meta_cache,
    save_tree_cache,
    write_jsonl,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Topic queries
# ---------------------------------------------------------------------------

# Note: openclaw* topics and org:openclaw are handled by clawhub_crawler.
TOPIC_QUERIES: list[str] = [
    # Claude Code skills
    "topic:claude-skill",
    "topic:claude-code-skill",
    "topic:claude-skills",
    "topic:claude-code-skills",
    # OpenClaw / OpenClaw agent
    "topic:openclaw-skill",
    "topic:openclaw-skills",
    # Codex skills
    "topic:codex-skill",
    "topic:codex-skills",
    # Generic agent skills
    "topic:agent-skill",
    "topic:agent-skills",
    # Extended ecosystem
    "topic:claude-code-plugins",
    "topic:gemini-skills",
    "topic:gemini-cli-skills",
    "topic:opencode-skills",
    "topic:antigravity-skills",
    "topic:cursor-skills",
    "topic:skill-md",
    "topic:agent-plugins",
    "topic:kiro-skill",
    "topic:roo-code-skill",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_topic_repos(session, limit: int = 1000, since: str | None = None) -> list[str]:
    """Search GitHub for repos matching TOPIC_QUERIES.

    Paginates each query (up to 1000 results per query as GitHub allows).
    Returns a deduplicated list of "{owner}/{repo}" full names, at most `limit`.

    Args:
        session: A requests.Session from make_session().
        limit:   Total maximum unique repos to return across all queries.
        since:   Optional ISO-8601 timestamp.  When provided, appends
                 ``pushed:><since>`` to every query so only repos pushed after
                 that time are returned — dramatically reducing API quota on
                 incremental/discover re-runs.

    Returns:
        Deduplicated list of full repo names.
    """
    seen: set[str] = set()
    results: list[str] = []

    for query in TOPIC_QUERIES:
        effective_query = f"{query} pushed:>{since}" if since else query
        page = 1
        while len(results) < limit:
            try:
                data = github_get(
                    session,
                    f"{GITHUB_API}/search/repositories",
                    params={"q": effective_query, "per_page": 100, "page": page},
                )
            except RuntimeError as exc:
                log.warning("Topic repo search failed for %r (page %d): %s", effective_query, page, exc)
                break

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                fn = item.get("full_name", "")
                if fn and fn not in seen:
                    seen.add(fn)
                    results.append(fn)

            if len(items) < 100:
                break
            page += 1

    log.info("Topic discovery: %d unique repos found across %d queries", len(results), len(TOPIC_QUERIES))
    return results[:limit]


def _load_existing_repo_urls(raw_dirs: list[str]) -> set[str]:
    """Load all repo_url values from JSONL files in the given directories.

    Used to skip repos already covered by other crawlers (skillsmp, clawhub, etc.).

    Args:
        raw_dirs: List of directory paths containing raw *.jsonl crawler output.

    Returns:
        Set of canonical (lowercased, .git-stripped) repo URL strings.
    """
    import json as _json

    existing: set[str] = set()
    for raw_dir in raw_dirs:
        p = Path(raw_dir)
        if not p.is_dir():
            log.debug("existing_raw_dir not found or not a directory: %s", raw_dir)
            continue
        for jsonl_file in p.glob("*.jsonl"):
            try:
                for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                        url = rec.get("repo_url", "")
                        if url:
                            existing.add(url.lower().rstrip("/").removesuffix(".git"))
                    except Exception:
                        pass
            except OSError as exc:
                log.warning("Could not read %s: %s", jsonl_file, exc)

    log.info("Loaded %d existing repo URLs from %d raw dir(s)", len(existing), len(raw_dirs))
    return existing


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run(
    output_path: str,
    token: str = None,
    limit: int = None,
    resume: bool = False,
    filter_cache_path: str = None,
    existing_raw_dirs: list[str] = None,
    mode: str = "full",
) -> int:
    """Run the topic crawler.

    Args:
        output_path:       Path to the output JSONL file.
        token:             Optional GitHub personal access token.
        limit:             Stop after writing this many records (for testing).
        resume:            If True, skip skills already present in output_path.
        filter_cache_path: Path to shared filter cache JSONL file.  Pass None
                           to disable filter-cache behaviour.
        existing_raw_dirs: List of directories containing other raw crawler
                           output. Repos found there are skipped to avoid
                           re-crawling already-covered repos.
        mode:              Crawl mode: full, incremental, metadata, or discover.
                           incremental behaves like resume=True.

    Returns:
        Number of new records written.
    """
    # Resolve mode: incremental aliases resume behaviour
    if mode == "incremental":
        resume = True
    import json as _json

    # Load per-source crawl state for date-filter support
    crawl_state = load_crawl_state("topic")
    run_started = _utc_now_iso()

    session = make_session(token=token)

    # Load ETag metadata cache, blob-SHA content cache, and Trees-path cache
    meta_cache = load_meta_cache("data/crawl_state/repo_meta_cache.json")
    content_cache = load_content_cache("data/crawl_state/content_cache.json")
    tree_cache = load_tree_cache("data/crawl_state/tree_cache.json")

    # Load filter cache (repos known to have no SKILL.md)
    filter_cache: set[str] = set()
    if filter_cache_path:
        filter_cache = load_filter_cache(filter_cache_path)
        log.info("Filter cache loaded: %d entries", len(filter_cache))

    # Load already-covered repos from other crawlers for dedup
    already_covered: set[str] = set()
    if existing_raw_dirs:
        already_covered = _load_existing_repo_urls(existing_raw_dirs)

    # Resume: load skill_md_url (or repo_url) from existing output
    existing_skill_keys: set[str] = set()
    if resume:
        p = Path(output_path)
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                    smu = r.get("raw_metadata", {}).get("skill_md_url", "")
                    if smu:
                        existing_skill_keys.add(smu)
                    else:
                        existing_skill_keys.add(r["repo_url"])
                except Exception:
                    pass
        log.info("Resume mode: %d skill keys already in output", len(existing_skill_keys))

    # Discover repos via topic search, using date-filter on incremental/discover runs
    since = crawl_state.get("last_discovery_at") if mode in ("incremental", "discover") else None
    discovered = _discover_topic_repos(session, limit=1000, since=since)

    # Bulk-fetch metadata for all discovered repos via a single GraphQL batch.
    # Each chunk of ≤100 repos costs one GraphQL POST (separate 5k-point/hr pool).
    # Repos absent from the result fall back to the per-repo cached REST path below.
    batch_meta = fetch_repo_metadata_batch(session, discovered)

    # Cache (meta, skill_md_paths) per repo to avoid repeated API calls
    _repo_cache: dict[str, tuple[dict, list[str]]] = {}

    # Track (repo_url, skill_path) pairs written in this run to avoid dups
    seen_skill_keys: set[tuple[str, str]] = set()

    records: list[dict] = []

    for full_name in discovered:
        if limit is not None and len(records) >= limit:
            log.info("Reached limit of %d records; stopping.", limit)
            break

        repo_url = f"https://github.com/{full_name}"
        canon_url = repo_url.lower()

        # Skip repos already covered by other crawlers
        if canon_url in already_covered:
            log.debug("Skipping %s: already covered by another crawler", full_name)
            continue

        # Skip repos with no SKILL.md (from filter cache)
        if canon_url in filter_cache or repo_url in filter_cache:
            log.debug("Skipping %s: in filter cache", full_name)
            continue

        # Fetch metadata + SKILL.md paths
        if full_name not in _repo_cache:
            try:
                # Use GraphQL batch result when available; fall back to per-repo REST
                meta = batch_meta.get(full_name) or fetch_repo_metadata_cached(
                    session, full_name, meta_cache
                )
            except RuntimeError as exc:
                log.warning("Could not fetch metadata for %s: %s", full_name, exc)
                continue
            skill_md_paths = find_skill_md_paths_cached(
                session, full_name, meta.get("pushed_at", ""), tree_cache
            )
            _repo_cache[full_name] = (meta, skill_md_paths)
            if not skill_md_paths:
                if filter_cache_path:
                    add_to_filter_cache(filter_cache_path, repo_url, "no_skill_md")
                    filter_cache.add(repo_url)
        else:
            meta, skill_md_paths = _repo_cache[full_name]

        if not skill_md_paths:
            continue

        default_branch = meta.get("default_branch", "main")
        repo_name = full_name.split("/")[-1]

        for skill_path in skill_md_paths:
            if limit is not None and len(records) >= limit:
                break

            skill_md_url = f"{repo_url}/blob/{default_branch}/{skill_path}"

            # Dedup within this run
            skill_run_key = (repo_url, skill_path)
            if skill_run_key in seen_skill_keys:
                log.debug("Skipping duplicate skill: %s/%s", repo_url, skill_path)
                continue
            if resume:
                already_seen = skill_md_url in existing_skill_keys or (
                    len(skill_md_paths) == 1 and repo_url in existing_skill_keys
                )
                if already_seen:
                    log.debug("Skipping already-crawled skill (resume): %s", skill_md_url)
                    continue
            seen_skill_keys.add(skill_run_key)

            # Fetch SKILL.md for name/description/platform detection
            fm: dict = {}
            skill_content = fetch_skill_md_cached(
                session,
                full_name,
                skill_path,
                skill_md_paths[skill_path],
                default_branch,
                content_cache,
            )
            if skill_content:
                fm = parse_frontmatter(skill_content)

            # Derive name/description from frontmatter or path/repo
            skill_dir = skill_path.rsplit("/SKILL.md", 1)[0] if "/" in skill_path else ""
            name = fm.get("name") or (skill_dir.split("/")[-1] if skill_dir else repo_name)
            description = fm.get("description", "")
            platforms = infer_platforms(fm, "topic")

            record = {
                "repo_url": repo_url,
                "name": name,
                "description": description,
                "source": "topic",
                "raw_metadata": {
                    "stars": meta.get("stargazers_count", 0),
                    "pushed_at": meta.get("pushed_at", ""),
                    "skill_md_url": skill_md_url,
                    "platforms": platforms,
                    "topics": meta.get("topics", []),
                },
            }
            records.append(record)

    save_meta_cache(meta_cache, "data/crawl_state/repo_meta_cache.json")
    save_content_cache(content_cache, "data/crawl_state/content_cache.json")
    save_tree_cache(tree_cache, "data/crawl_state/tree_cache.json")

    # Persist discovery timestamp so next incremental/discover run can filter by it.
    # Use run_started (not now) so repos pushed *during* this crawl aren't missed.
    # Only advance the window when discovery actually returned repos: a transient
    # failure (rate-limit/empty search) must NOT push last_discovery_at forward, or
    # the next incremental run would silently skip repos changed in the gap.
    if discovered:
        crawl_state["last_discovery_at"] = run_started
        save_crawl_state(crawl_state, "topic")

    written = write_jsonl(records, output_path, append=resume)
    log.info("Topic crawler done: %d records written to %s", written, output_path)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Discover GitHub repos via skill-related topics and emit raw JSONL records.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "-o", "--output",
        required=True,
        metavar="PATH",
        help="Output JSONL file path (e.g. data/raw/topic.jsonl)",
    )
    p.add_argument(
        "--token",
        default=None,
        metavar="TOKEN",
        help="GitHub personal access token (or set GITHUB_TOKEN env var)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after writing N records (for testing)",
    )
    p.add_argument(
        "--mode",
        choices=["full", "incremental", "metadata", "discover"],
        default="full",
        help="Crawl mode: full=complete re-crawl, incremental=changed repos only, metadata=stars/ETags only, discover=new repos since last run",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="[Deprecated] Alias for --mode incremental",
    )
    p.add_argument(
        "--filter-cache",
        default="data/filter_cache.jsonl",
        metavar="PATH",
        help="Shared filter-cache JSONL file (pass empty string to disable)",
    )
    p.add_argument(
        "--data-dir",
        default=None,
        metavar="DIR",
        action="append",
        dest="data_dirs",
        help="Directory of existing raw JSONL files for dedup (may be repeated)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        stream=sys.stderr,
    )

    token = args.token or os.environ.get("GITHUB_TOKEN")
    filter_cache_path = args.filter_cache or None

    # Deprecated flag aliases
    if args.resume:
        import warnings
        warnings.warn("--resume is deprecated, use --mode incremental", DeprecationWarning)
        if args.mode == "full":
            args.mode = "incremental"

    try:
        count = run(
            output_path=args.output,
            token=token,
            limit=args.limit,
            resume=args.resume,
            filter_cache_path=filter_cache_path,
            existing_raw_dirs=args.data_dirs,
            mode=args.mode,
        )
        print(f"Wrote {count} records to {args.output}", file=sys.stderr)
        return 0
    except Exception as exc:
        log.error("Topic crawler failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
