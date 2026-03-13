"""
crawlers/marketplace_crawler.py — Known Anthropic/official marketplace repos.

Strategy: For each repo in TARGET_REPOS, walk the full directory tree via
the GitHub Trees API and find every SKILL.md file.  Parse frontmatter.
Also discover additional repos that host a marketplace.json at their root
via GitHub Code Search.

Key dedup rule: the repo_url for each skill is the PARENT repo URL
(e.g. https://github.com/anthropics/skills), UNLESS the SKILL.md frontmatter
contains a 'repo_url' field pointing to the skill's own dedicated repo.

Output: data/raw/marketplace.jsonl (one JSON record per line)

CLI:
    python crawlers/marketplace_crawler.py -o data/raw/marketplace.jsonl \\
        [--token TOKEN] [--limit N] [--resume]

The GITHUB_TOKEN environment variable is used when --token is not supplied.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
from typing import Optional

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
# Known target repos
# ---------------------------------------------------------------------------

TARGET_REPOS: list[str] = [
    "anthropics/skills",
    "daymade/claude-code-skills",
    "mhattingpete/claude-skills-marketplace",
    "alirezarezvani/claude-skills",
]

# Repos whose owner is "anthropics" are flagged as official
_OFFICIAL_OWNER = "anthropics"


# ---------------------------------------------------------------------------
# GitHub Trees API helpers
# ---------------------------------------------------------------------------

def list_skill_dirs(session, repo_full_name: str) -> list[dict]:
    """Find all SKILL.md files in a repo and return enriched entry dicts.

    Uses the GitHub Trees API to discover SKILL.md files, then fetches each
    file's content via the Contents API.

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.

    Returns:
        List of dicts with keys: name, description, path, parent_repo_url,
        skill_md_content, official. Returns an empty list on error.
    """
    url = f"{GITHUB_API}/repos/{repo_full_name}/git/trees/HEAD"
    try:
        data = github_get(session, url, params={"recursive": "1"})
    except RuntimeError as exc:
        logger.warning("Could not fetch tree for %s: %s", repo_full_name, exc)
        return []

    owner = repo_full_name.split("/")[0].lower() if "/" in repo_full_name else ""
    is_official = owner == _OFFICIAL_OWNER
    parent_repo_url = f"https://github.com/{repo_full_name}".lower()

    tree = data.get("tree", [])
    entries = []
    for item in tree:
        path = item.get("path", "")
        if not (path.upper().endswith("SKILL.MD") and item.get("type") == "blob"):
            continue

        content = fetch_skill_content(session, repo_full_name, path)
        name = _derive_name_from_path(path, repo_full_name)

        entries.append({
            "name": name,
            "description": "",
            "path": path,
            "parent_repo_url": parent_repo_url,
            "skill_md_content": content,
            "official": is_official,
        })

    logger.debug("Found %d SKILL.md file(s) in %s", len(entries), repo_full_name)
    return entries


def fetch_skill_content(session, repo_full_name: str, path: str) -> Optional[str]:
    """Fetch the raw text of a file via the GitHub Contents API.

    GET /repos/{full_name}/contents/{path}

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.
        path:           Path to the file within the repo.

    Returns:
        Decoded file content as a string, or None on any error.
    """
    url = f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}"
    try:
        data = github_get(session, url)
    except RuntimeError as exc:
        logger.warning("Could not fetch %s/%s: %s", repo_full_name, path, exc)
        return None

    encoded = data.get("content", "")
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Base64 decode error for %s/%s: %s", repo_full_name, path, exc)
        return None


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from a SKILL.md string.

    Returns a dict of the frontmatter fields, or an empty dict if the file
    has no frontmatter or parsing fails.
    """
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


# ---------------------------------------------------------------------------
# Record builder
# ---------------------------------------------------------------------------

def build_raw_record(entry: dict) -> dict:
    """Build a raw record from a marketplace skill entry dict.

    The repo_url is determined as follows (in priority order):
    1. If skill_md_content frontmatter has a 'repo_url' field that is a valid
       GitHub URL, use that (it points to the skill's own dedicated repo).
    2. Otherwise use the parent_repo_url from the entry dict.

    Args:
        entry: Dict with keys: name, description, path, parent_repo_url,
               skill_md_content (str or None), official (bool).

    Returns:
        Raw record dict conforming to the crawler schema.
    """
    content = entry.get("skill_md_content") or ""
    frontmatter = _parse_frontmatter(content) if content else {}

    parent_url = entry.get("parent_repo_url", "")
    fm_repo_url = frontmatter.get("repo_url", "")
    if fm_repo_url:
        normalized = extract_github_url(str(fm_repo_url))
        repo_url = normalized if normalized else parent_url
    else:
        repo_url = parent_url

    name = frontmatter.get("name") or entry.get("name", "")
    description = frontmatter.get("description") or entry.get("description", "")
    triggers = frontmatter.get("triggers") or []
    if not isinstance(triggers, list):
        triggers = [str(triggers)]

    is_official = entry.get("official", False)

    return {
        "repo_url": repo_url,
        "name": name,
        "description": description,
        "source": "marketplace",
        "raw_metadata": {
            "triggers": triggers,
            "official": is_official,
            "path": entry.get("path", ""),
            "parent_repo": parent_url,
        },
    }


def _derive_name_from_path(path: str, repo_full_name: str) -> str:
    """Derive a human-readable skill name from its file path."""
    # e.g.  "kubernetes-deployer/SKILL.md"  → "kubernetes-deployer"
    #       "SKILL.md"                       → repo name part of full_name
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if len(parts) >= 2:
        # The directory containing SKILL.md is the skill's canonical name
        return parts[-2]
    # Top-level SKILL.md — use the repo name
    return repo_full_name.split("/")[-1] if "/" in repo_full_name else repo_full_name


# ---------------------------------------------------------------------------
# Repo-level metadata fetch
# ---------------------------------------------------------------------------

def _fetch_repo_meta(session, repo_full_name: str) -> dict:
    """Fetch top-level metadata for a repo."""
    try:
        data = github_get(session, f"{GITHUB_API}/repos/{repo_full_name}")
    except RuntimeError as exc:
        logger.warning("Could not fetch repo metadata for %s: %s", repo_full_name, exc)
        return {}
    return {
        "stargazers_count": data.get("stargazers_count", 0),
        "pushed_at": data.get("pushed_at", ""),
        "topics": data.get("topics", []),
        "description": data.get("description", ""),
        "default_branch": data.get("default_branch", "main"),
    }


# ---------------------------------------------------------------------------
# marketplace.json discovery
# ---------------------------------------------------------------------------

def _fetch_marketplace_json(session, repo_full_name: str, default_branch: str = "main") -> list[dict]:
    """Try to fetch and parse a marketplace.json at the root of a repo.

    Returns a list of entry dicts (each may have name, description, path).
    Returns an empty list if the file doesn't exist or can't be parsed.
    """
    url = f"{GITHUB_API}/repos/{repo_full_name}/contents/marketplace.json"
    try:
        data = github_get(session, url, params={"ref": default_branch})
    except RuntimeError:
        return []

    encoded = data.get("content", "")
    if not encoded:
        return []

    try:
        raw_bytes = base64.b64decode(encoded.replace("\n", ""))
        entries = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        if isinstance(entries, list):
            return entries
        return []
    except Exception as exc:
        logger.warning("Could not parse marketplace.json in %s: %s", repo_full_name, exc)
        return []


def _discover_marketplace_repos(session, limit: int = 50) -> list[str]:
    """Discover additional repos that have a marketplace.json at root.

    Uses GitHub Code Search.  Returns a list of "{owner}/{repo}" strings.
    """
    from crawlers.base import github_get as _gh_get
    try:
        data = _gh_get(
            session,
            f"{GITHUB_API}/search/code",
            params={
                "q": "filename:marketplace.json path:/",
                "per_page": min(limit, 100),
            },
        )
    except RuntimeError as exc:
        logger.warning("marketplace.json discovery search failed: %s", exc)
        return []

    repos = []
    seen: set[str] = set()
    for item in data.get("items", []):
        repo = item.get("repository", {})
        full_name = repo.get("full_name", "")
        if full_name and full_name not in seen:
            seen.add(full_name)
            repos.append(full_name)

    logger.info("Discovered %d additional marketplace repos", len(repos))
    return repos


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run(
    output_path: str,
    token: str = None,
    limit: int = None,
    resume: bool = False,
) -> int:
    """Run the marketplace crawler end to end.

    Iterates over TARGET_REPOS and discovered marketplace repos, finds all
    SKILL.md files in each, parses frontmatter, and writes raw records to
    output_path.

    Args:
        output_path: Destination JSONL file.
        token:       GitHub personal access token.
        limit:       Maximum number of records to write (None = unlimited).
        resume:      If True, skip repo_urls already present in output_path.

    Returns:
        Number of records written.
    """
    session = make_session(token=token)

    existing_urls: set[str] = set()
    if resume:
        existing_urls = load_existing_urls(output_path)
        logger.info("Resume mode: skipping %d already-collected skills", len(existing_urls))

    # Build combined list: known targets + discovered extras (only with auth to avoid rate limits)
    all_repos = list(TARGET_REPOS)
    if token:
        discovered = _discover_marketplace_repos(session)
        for repo in discovered:
            if repo not in all_repos:
                all_repos.append(repo)

    # For marketplace, dedup by (parent_repo_url, path) since multiple skills
    # from the same registry repo share the same repo_url.
    seen_paths: set[str] = set()
    batch: list[dict] = []
    batch_size = 20
    written = 0

    for repo_full_name in all_repos:
        if limit is not None and written + len(batch) >= limit:
            break

        logger.info("Processing repo: %s", repo_full_name)
        entries = list_skill_dirs(session, repo_full_name)

        if not entries:
            logger.info("No SKILL.md files found in %s, skipping", repo_full_name)
            continue

        for entry in entries:
            if limit is not None and written + len(batch) >= limit:
                break

            # Dedup by (parent_repo_url, path) within this run
            dedup_key = f"{entry.get('parent_repo_url', '')}#{entry.get('path', '')}"
            if dedup_key in seen_paths:
                logger.debug("Skipping already-seen entry: %s", dedup_key)
                continue
            seen_paths.add(dedup_key)

            record = build_raw_record(entry)
            batch.append(record)

            if len(batch) >= batch_size:
                written += write_jsonl(batch, output_path, append=(written > 0 or resume))
                batch = []

    # Flush remaining records
    if batch:
        written += write_jsonl(batch, output_path, append=(written > 0 or resume))

    logger.info(
        "Marketplace crawler finished: %d records written to %s", written, output_path
    )
    return written


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl known marketplace repos for skills"
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        metavar="PATH",
        help="Output JSONL file path (e.g. data/raw/marketplace.jsonl)",
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
        "--resume",
        action="store_true",
        help="Skip skills already present in the output file",
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
            resume=args.resume,
        )
        print(f"Done: {count} records written to {args.output}")
        return 0
    except Exception as exc:
        logger.error("Crawler failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
