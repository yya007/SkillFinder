"""
crawlers/clawhub_crawler.py — ClawHub / OpenClaw skill crawler.

Strategy: parse the VoltAgent/awesome-openclaw-skills README.md from GitHub.
It contains a curated list of skills with GitHub links.

Output: data/raw/clawhub.jsonl
Each record conforms to the raw record schema defined in PRD-001.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import logging
import os
import re
import sys
from typing import Iterator

import yaml

# ---------------------------------------------------------------------------
# Lazy import of base helpers (written by companion agent)
# ---------------------------------------------------------------------------
from crawlers.base import (
    GITHUB_API,
    add_to_filter_cache,
    extract_github_url,
    fetch_repo_metadata,
    find_skill_md_paths,
    github_get,
    infer_platforms,
    load_filter_cache,
    make_session,
    write_jsonl,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AWESOME_LIST_REPOS: list[tuple[str, str]] = [
    ("VoltAgent/awesome-openclaw-skills", "README.md"),   # existing
    ("ComposioHQ/awesome-claude-skills", "README.md"),
    ("hesreallyhim/awesome-claude-code", "README.md"),    # also has THE_RESOURCES_TABLE.csv — README only for now
    ("VoltAgent/awesome-agent-skills", "README.md"),
    ("skillmatic-ai/awesome-agent-skills", "README.md"),
    ("heilcheng/awesome-agent-skills", "README.md"),
    ("sickn33/antigravity-awesome-skills", "README.md"),
]

# Repository-search queries for broader OpenClaw org / topic coverage.
_OPENCLAW_QUERIES = [
    "org:openclaw",
    "topic:openclaw",
    "topic:openclaw-skill",
]

# GitHub Contents API endpoint for a file
_CONTENTS_URL = "https://api.github.com/repos/{repo}/contents/{path}"

# Regex: matches lines of the form:
#   - [name](url) — description
#   - [name](url) - description
#   - [name](url) – description  (en-dash)
_SKILL_LINE_RE = re.compile(
    r"^\s*-\s+\[(?P<name>[^\]]+)\]\((?P<url>https?://[^)]+)\)"  # - [name](url)
    r"\s*(?:—|–|-)\s*"                                           # separator
    r"(?P<description>.+)$"                                      # description
)

# Regex: ## Category heading
_HEADING_RE = re.compile(r"^##\s+(?P<category>.+)$")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SKILL.md helpers
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from a SKILL.md string."""
    if not content or not content.startswith("---"):
        return {}
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


def _fetch_skill_md(
    session,
    repo_full_name: str,
    path: str = "SKILL.md",
    default_branch: str = "main",
) -> str | None:
    """Fetch raw SKILL.md content from a specific path in a GitHub repo."""
    url = f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}"
    try:
        resp = session.get(url, params={"ref": default_branch}, timeout=30)
    except Exception as exc:
        log.debug("Network error fetching %s from %s: %s", path, repo_full_name, exc)
        return None
    if resp.status_code == 200:
        try:
            encoded = resp.json().get("content", "")
            return base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
        except Exception:
            return None
    return None


def _extract_subtree_hint(url: str) -> str | None:
    """Extract the subdirectory hint from URLs like .../tree/branch/subpath.

    Returns the subpath string (e.g. "skills/context-optimization") or None.
    """
    m = re.search(r"/tree/[^/]+/(.+)", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_awesome_readme(session, repo: str, path: str) -> str:
    """Fetch raw README from a GitHub repo via GitHub Contents API.

    Args:
        session: A requests.Session from make_session().
        repo:    Full repo name, e.g. "VoltAgent/awesome-openclaw-skills".
        path:    File path within the repo, e.g. "README.md".

    Returns the decoded text content.

    Raises:
        RuntimeError: if the API call fails or the content cannot be decoded.
    """
    url = _CONTENTS_URL.format(repo=repo, path=path)
    data = github_get(session, url)

    # Prefer download_url: it serves the raw file without the 1 MB inline
    # base64 limit, ensuring we always receive the complete README even for
    # large awesome lists (e.g. VoltAgent/awesome-openclaw-skills > 1 MB).
    download_url = data.get("download_url")
    if download_url:
        resp = session.get(download_url, timeout=60)
        resp.raise_for_status()
        return resp.text

    # Fallback: decode inline base64 (works for files < 1 MB)
    encoding = data.get("encoding", "")
    content_raw = data.get("content", "")
    if encoding == "base64":
        try:
            return base64.b64decode(content_raw).decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"Failed to decode README content: {exc}") from exc

    raise RuntimeError(
        f"Unexpected encoding '{encoding}' from GitHub Contents API for {repo}/{path}"
    )


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def parse_awesome_readme(content: str) -> list[dict]:
    """Parse the README and extract skills.

    Returns a list of dicts, each with:
        name        (str)  — skill name from the markdown link text
        url         (str)  — URL from the markdown link href
        description (str)  — text after the dash separator
        category    (str)  — the ## heading above this entry

    Parsing rules:
    - ## headings set the current category.
    - Lines matching ``- [name](url) — description`` become skill entries.
    - The dash separator may be —, –, or -.
    - Lines that don't match the skill format are skipped.
    - The url must start with ``http`` (enforced by regex).
    """
    skills: list[dict] = []
    current_category: str = ""

    for line in content.splitlines():
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            current_category = heading_match.group("category").strip()
            continue

        skill_match = _SKILL_LINE_RE.match(line)
        if skill_match:
            skills.append(
                {
                    "name": skill_match.group("name").strip(),
                    "url": skill_match.group("url").strip(),
                    "description": skill_match.group("description").strip(),
                    "category": current_category,
                }
            )

    log.info("Parsed %d skill entries from README", len(skills))
    return skills


# ---------------------------------------------------------------------------
# Record builder
# ---------------------------------------------------------------------------

def build_raw_record(
    item: dict,
    stars: int = 0,
    pushed_at: str = "",
    skill_md_url: str = "",
    frontmatter: dict = None,
    vetted: bool = False,
) -> dict | None:
    """Build a raw record from a parsed awesome-list entry.

    CRITICAL: extract_github_url(item["url"]) must succeed.
    If the URL is not a GitHub URL, returns None (skip this record).

    Args:
        item:        Parsed dict with keys: name, url, description, category.
        stars:       GitHub star count (from fetch_repo_metadata).
        pushed_at:   Last pushed timestamp (from fetch_repo_metadata).
        skill_md_url: Direct URL to SKILL.md blob on GitHub.
        frontmatter: Parsed SKILL.md frontmatter dict (for platform detection).

    Returns:
        A raw record dict or None if the URL cannot be resolved to a GitHub repo URL.
    """
    repo_url = extract_github_url(item["url"])
    if repo_url is None:
        log.debug("Skipping non-GitHub URL: %s", item["url"])
        return None

    fm = frontmatter or {}
    platforms = infer_platforms(fm, "clawhub")

    return {
        "repo_url": repo_url,
        "name": item["name"],
        "description": item["description"],
        "source": "clawhub",
        "raw_metadata": {
            "categories": [item["category"]] if item["category"] else [],
            "stars": stars,
            "pushed_at": pushed_at,
            "skill_md_url": skill_md_url,
            "platforms": platforms,
            "safety_scan": True if vetted else None,
            "safety_scan_date": datetime.date.today().isoformat() if vetted else None,
        },
    }


# ---------------------------------------------------------------------------
# OpenClaw org / topic discovery
# ---------------------------------------------------------------------------

def _discover_openclaw_repos(session, limit: int = 500) -> list[str]:
    """Discover repos in the openclaw org and with openclaw topics via GitHub search.

    Queries each entry in _OPENCLAW_QUERIES against the repository search API,
    collecting up to `limit` unique "{owner}/{repo}" full names.

    Args:
        session: A requests.Session from make_session().
        limit:   Maximum number of unique repos to return.

    Returns:
        Deduplicated list of full repo names, at most `limit` entries.
    """
    seen: set[str] = set()
    results: list[str] = []

    for query in _OPENCLAW_QUERIES:
        page = 1
        while len(results) < limit:
            try:
                data = github_get(
                    session,
                    f"{GITHUB_API}/search/repositories",
                    params={"q": query, "per_page": 100, "page": page},
                )
            except RuntimeError as exc:
                log.warning("OpenClaw repo search failed for %r (page %d): %s", query, page, exc)
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

    log.info("OpenClaw discovery: found %d repos across %d queries", len(results), len(_OPENCLAW_QUERIES))
    return results[:limit]


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def _iter_records(items: list[dict]) -> Iterator[dict]:
    """Yield raw records built from parsed items, skipping non-GitHub entries."""
    for item in items:
        record = build_raw_record(item)
        if record is not None:
            yield record


def run(
    output_path: str,
    token: str = None,
    limit: int = None,
    resume: bool = False,
    filter_cache_path: str = None,
) -> int:
    """Run the ClawHub crawler.

    Args:
        output_path:       Path to the output JSONL file.
        token:             Optional GitHub personal access token.
        limit:             Stop after writing this many records (for testing).
        resume:            If True, skip skills already present in output_path.
        filter_cache_path: Path to shared filter cache JSONL file.  Pass None
                           to disable filter-cache behaviour.

    Returns:
        Number of new records written.
    """
    import json as _json
    from pathlib import Path as _Path

    session = make_session(token=token)

    # Load filter cache
    filter_cache: set[str] = set()
    if filter_cache_path:
        filter_cache = load_filter_cache(filter_cache_path)
        log.info("Filter cache loaded: %d entries", len(filter_cache))

    # --- fetch and parse all awesome list READMEs ---
    items: list[dict] = []
    for awesome_repo, awesome_path in AWESOME_LIST_REPOS:
        log.info("Fetching README from %s/%s", awesome_repo, awesome_path)
        try:
            readme_content = fetch_awesome_readme(session, awesome_repo, awesome_path)
        except RuntimeError as exc:
            log.warning("Failed to fetch %s/%s: %s", awesome_repo, awesome_path, exc)
            continue
        for parsed_item in parse_awesome_readme(readme_content):
            parsed_item["_source_repo"] = awesome_repo
            items.append(parsed_item)

    # --- resume: load skill_md_url (preferred) or repo_url from existing output ---
    existing_skill_keys: set[str] = set()
    if resume:
        p = _Path(output_path)
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
                        # Legacy records without skill_md_url: fall back to repo_url.
                        # Only safe for non-monorepos (single-SKILL.md repos).
                        existing_skill_keys.add(r["repo_url"])
                except Exception:
                    pass
        log.info("Resume mode: %d skill keys already in output", len(existing_skill_keys))

    # Cache (meta dict, skill_md_paths list) per base repo_url to avoid
    # repeated API calls for monorepos (e.g. openclaw/skills with 800+ entries).
    _repo_cache: dict[str, tuple[dict, list[str]]] = {}

    # Track (repo_url, skill_path) pairs written in this run to avoid dups.
    seen_skill_keys: set[tuple[str, str]] = set()

    # --- build records ---
    records: list[dict] = []
    for item in items:
        # Build basic record first (validates GitHub URL)
        _vetted = item.get("_source_repo") == "VoltAgent/awesome-openclaw-skills"
        basic = build_raw_record(item, vetted=_vetted)
        if basic is None:
            continue

        repo_url = basic["repo_url"]

        # Check filter cache (repos with no SKILL.md at all)
        if repo_url in filter_cache:
            log.debug("Skipping %s: in filter cache", repo_url)
            continue

        # Extract subtree hint from URLs like .../tree/main/skills/author/name
        # before touching the API — if we have a hint we can skip the Trees API
        # entirely and construct the exact path directly.
        full_name = repo_url.removeprefix("https://github.com/")
        subtree_hint = _extract_subtree_hint(item.get("url", ""))
        skill_md_paths: list[str] | None = None  # populated only in slow path

        if subtree_hint:
            # Fast path: path is known from the URL — no Trees API needed.
            # This avoids the 100k-node truncation problem for large monorepos
            # like openclaw/skills which has 13k+ files.
            if full_name not in _repo_cache:
                meta = fetch_repo_metadata(session, full_name)
                _repo_cache[full_name] = (meta, None)  # None = paths not fetched
            else:
                meta, _ = _repo_cache[full_name]
            skill_path = f"{subtree_hint}/SKILL.md"
        else:
            # Slow path: enumerate via Trees API (with Code Search fallback for
            # truncated repos) and pick the single/best matching path.
            if full_name not in _repo_cache:
                meta = fetch_repo_metadata(session, full_name)
                skill_md_paths = find_skill_md_paths(session, full_name)
                _repo_cache[full_name] = (meta, skill_md_paths)
                if not skill_md_paths:
                    if filter_cache_path:
                        add_to_filter_cache(filter_cache_path, repo_url, "no_skill_md")
                        filter_cache.add(repo_url)
            else:
                meta, skill_md_paths = _repo_cache[full_name]
                if skill_md_paths is None:
                    skill_md_paths = {}

            if not skill_md_paths:
                continue
            elif len(skill_md_paths) == 1:
                skill_path = next(iter(skill_md_paths))
            else:
                log.debug("Multiple SKILL.md paths but no subtree hint for %s; skipping", repo_url)
                continue

        default_branch = meta.get("default_branch", "main")

        skill_md_url = f"{repo_url}/blob/{default_branch}/{skill_path}"

        # Dedup: within this run use (repo_url, skill_path); for resume use skill_md_url/repo_url.
        skill_run_key = (repo_url, skill_path)
        if skill_run_key in seen_skill_keys:
            log.debug("Skipping duplicate skill: %s/%s", repo_url, skill_path)
            continue
        if resume:
            # Primary check: exact skill_md_url match (works for monorepos too).
            # Fallback: repo_url match, but only for non-monorepos (single SKILL.md)
            # to avoid blocking all skills from a monorepo when one was crawled.
            already_seen = skill_md_url in existing_skill_keys or (
                skill_md_paths is not None
                and len(skill_md_paths) == 1
                and repo_url in existing_skill_keys
            )
            if already_seen:
                log.debug("Skipping already-crawled skill (resume): %s", skill_md_url)
                continue
        seen_skill_keys.add(skill_run_key)

        # Fetch SKILL.md content for platform detection
        fm: dict = {}
        skill_content = _fetch_skill_md(
            session, full_name, path=skill_path, default_branch=default_branch
        )
        if skill_content:
            fm = _parse_frontmatter(skill_content)

        # Rebuild with enriched data
        record = build_raw_record(
            item,
            stars=meta.get("stargazers_count", 0),
            pushed_at=meta.get("pushed_at", ""),
            skill_md_url=skill_md_url,
            frontmatter=fm,
            vetted=_vetted,
        )
        if record is None:
            continue

        records.append(record)

        if limit is not None and len(records) >= limit:
            log.info("Reached limit of %d records; stopping.", limit)
            break

    # --- discover additional OpenClaw org / topic repos (requires auth to avoid rate limits) ---
    discovered = _discover_openclaw_repos(session, limit=500) if token else []
    log.info("Processing %d discovered OpenClaw repos", len(discovered))

    # Already-processed repos (from awesome list phase)
    already_processed_repos: set[str] = {ru for (ru, _) in seen_skill_keys}

    for full_name in discovered:
        if limit is not None and len(records) >= limit:
            break

        repo_url = f"https://github.com/{full_name}"

        # Skip repos already covered by the awesome list
        if repo_url in already_processed_repos:
            log.debug("Skipping %s: already processed from awesome list", repo_url)
            continue

        # Skip repos with no SKILL.md (from filter cache)
        if repo_url in filter_cache:
            log.debug("Skipping %s: in filter cache", repo_url)
            continue

        # Fetch metadata + SKILL.md paths (reuse cache if available)
        if full_name in _repo_cache:
            meta, skill_md_paths = _repo_cache[full_name]
            if skill_md_paths is None:
                skill_md_paths = []
        else:
            meta = fetch_repo_metadata(session, full_name)
            skill_md_paths = find_skill_md_paths(session, full_name)
            _repo_cache[full_name] = (meta, skill_md_paths)
            if not skill_md_paths:
                if filter_cache_path:
                    add_to_filter_cache(filter_cache_path, repo_url, "no_skill_md")
                    filter_cache.add(repo_url)

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
            skill_content = _fetch_skill_md(
                session, full_name, path=skill_path, default_branch=default_branch
            )
            if skill_content:
                fm = _parse_frontmatter(skill_content)

            # Derive name from frontmatter or path/repo
            skill_dir = skill_path.rsplit("/SKILL.md", 1)[0] if "/" in skill_path else ""
            item_name = fm.get("name") or (skill_dir.split("/")[-1] if skill_dir else repo_name)
            item_description = fm.get("description", "")

            item = {
                "name": item_name,
                "url": repo_url,
                "description": item_description,
                "category": "",
            }

            record = build_raw_record(
                item,
                stars=meta.get("stargazers_count", 0),
                pushed_at=meta.get("pushed_at", ""),
                skill_md_url=skill_md_url,
                frontmatter=fm,
            )
            if record is None:
                continue

            records.append(record)

    # --- write ---
    written = write_jsonl(records, output_path, append=resume)
    log.info("ClawHub crawler done: %d records written to %s", written, output_path)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Crawl VoltAgent/awesome-openclaw-skills and emit raw JSONL records.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "-o", "--output",
        required=True,
        metavar="PATH",
        help="Output JSONL file path (e.g. data/raw/clawhub.jsonl)",
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
        "--resume",
        action="store_true",
        help="Skip repos already present in the output file",
    )
    p.add_argument(
        "--filter-cache",
        default="data/filter_cache.jsonl",
        metavar="PATH",
        help="Shared filter-cache JSONL file (pass empty string to disable)",
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

    try:
        count = run(
            output_path=args.output,
            token=token,
            limit=args.limit,
            resume=args.resume,
            filter_cache_path=filter_cache_path,
        )
        print(f"Wrote {count} records to {args.output}", file=sys.stderr)
        return 0
    except Exception as exc:
        log.error("ClawHub crawler failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
