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
    fetch_repo_metadata,
    find_skill_md_paths,
    github_get,
    infer_platforms,
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
    "openai/skills",
    "google-gemini/gemini-skills",
    "vercel-labs/agent-skills",
    "awslabs/agent-plugins",
    "github/awesome-copilot",
]

# Repository-search queries used for broader community discovery.
# Each query is sent to GET /search/repositories.
REPO_SEARCH_QUERIES: list[str] = [
    "topic:claude-code-skills",
    "topic:claude-skills",
    "topic:agent-skills",
    "claude code skills in:name,description",
    "openclaw skills in:name,description",
]

# Repos whose owner is "anthropics" are flagged as official
_OFFICIAL_OWNER = "anthropics"


# ---------------------------------------------------------------------------
# GitHub Trees API helpers
# ---------------------------------------------------------------------------

def list_skill_dirs(session, repo_full_name: str) -> list[dict]:
    """Find all SKILL.md files in a repo and return enriched entry dicts.

    Uses find_skill_md_paths() (Trees API with Code Search fallback for
    truncated repos) to discover paths, then fetches each file's content
    via the Contents API.

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.

    Returns:
        List of dicts with keys: name, description, path, parent_repo_url,
        skill_md_content, official. Returns an empty list on error.
    """
    owner = repo_full_name.split("/")[0].lower() if "/" in repo_full_name else ""
    is_official = owner == _OFFICIAL_OWNER
    parent_repo_url = f"https://github.com/{repo_full_name}".lower()

    paths = find_skill_md_paths(session, repo_full_name)
    entries = []
    for path in paths:
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


def fetch_skill_content(
    session,
    repo_full_name: str,
    path: str,
    _depth: int = 0,
) -> Optional[str]:
    """Fetch the raw text of a file via the GitHub Contents API.

    GET /repos/{full_name}/contents/{path}

    Resolves symlinks (GitHub returns ``"type": "symlink"`` with a ``target``
    field instead of base64 ``content``) up to one level deep.

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.
        path:           Path to the file within the repo.

    Returns:
        Decoded file content as a string, or None on any error.
    """
    if _depth > 1:
        return None
    import posixpath
    url = f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}"
    try:
        data = github_get(session, url)
    except RuntimeError as exc:
        logger.warning("Could not fetch %s/%s: %s", repo_full_name, path, exc)
        return None

    if data is None:
        return None

    if data.get("type") == "symlink":
        target = data.get("target", "")
        if not target:
            return None
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        return fetch_skill_content(session, repo_full_name, resolved, _depth + 1)

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
    has no frontmatter or parsing fails.  Tolerates files that begin with
    HTML comments (e.g. copyright headers) before the opening ``---``.
    """
    if not content:
        return {}
    # Strip leading HTML comments and blank lines so files that start with
    # <!-- ... --> before the YAML block are handled correctly.
    stripped = re.sub(r"^(\s*<!--.*?-->\s*)+", "", content, flags=re.DOTALL)
    if not stripped.startswith("---"):
        return {}

    end_match = re.search(r"\n---\s*\n", stripped[3:])
    if not end_match:
        return {}

    yaml_text = stripped[3: end_match.start() + 3]
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
               skill_md_content (str or None), official (bool),
               default_branch (str, optional).

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

    is_official = entry.get("official", False)
    path = entry.get("path", "")
    default_branch = entry.get("default_branch", "main")
    skill_md_url = f"{parent_url}/blob/{default_branch}/{path}" if path else ""
    platforms = infer_platforms(frontmatter, "marketplace")

    return {
        "repo_url": repo_url,
        "name": name,
        "description": description,
        "source": "marketplace",
        "raw_metadata": {
            "official": is_official,
            "path": path,
            "parent_repo": parent_url,
            "skill_md_url": skill_md_url,
            "platforms": platforms,
            "stars": entry.get("stars", 0),
            "pushed_at": entry.get("pushed_at", ""),
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


def _discover_via_repo_search(session) -> list[str]:
    """Discover community skill repos via GitHub Repository Search API.

    Queries each entry in REPO_SEARCH_QUERIES and collects up to 100 repos
    per query.  Returns a deduplicated list of "{owner}/{repo}" strings.
    """
    repos: list[str] = []
    seen: set[str] = set()

    for query in REPO_SEARCH_QUERIES:
        try:
            data = github_get(
                session,
                f"{GITHUB_API}/search/repositories",
                params={"q": query, "per_page": 100},
            )
        except RuntimeError as exc:
            logger.warning("Repo search failed for %r: %s", query, exc)
            continue

        for item in data.get("items", []):
            html_url = item.get("html_url", "")
            normalized = extract_github_url(html_url)
            if normalized:
                full_name = normalized.removeprefix("https://github.com/")
                if full_name not in seen:
                    seen.add(full_name)
                    repos.append(full_name)

    logger.info("Repo search discovered %d repos", len(repos))
    return repos


def _discover_marketplace_repos(session, limit: int = 50) -> list[str]:
    """Discover additional repos that have a marketplace.json at root.

    Combines GitHub Code Search (filename:marketplace.json) with broader
    Repository Search (topic/keyword queries).  Returns a deduplicated list
    of "{owner}/{repo}" strings.
    """
    # Method 1: code search for marketplace.json
    code_search_repos: list[str] = []
    try:
        data = github_get(
            session,
            f"{GITHUB_API}/search/code",
            params={
                "q": "filename:marketplace.json path:/",
                "per_page": min(limit, 100),
            },
        )
        seen_code: set[str] = set()
        for item in data.get("items", []):
            repo = item.get("repository", {})
            full_name = repo.get("full_name", "")
            if full_name and full_name not in seen_code:
                seen_code.add(full_name)
                code_search_repos.append(full_name)
    except RuntimeError as exc:
        logger.warning("marketplace.json code search failed: %s", exc)

    # Method 2: repository search by topic/keyword
    repo_search_repos = _discover_via_repo_search(session)

    # Combine, deduplicate
    seen: set[str] = set()
    combined: list[str] = []
    for repo in code_search_repos + repo_search_repos:
        if repo not in seen:
            seen.add(repo)
            combined.append(repo)

    logger.info("Discovered %d additional marketplace repos total", len(combined))
    return combined


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run(
    output_path: str,
    token: str = None,
    limit: int = None,
    resume: bool = False,
    mode: str = "full",
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
        mode:        Crawl mode: full, incremental, metadata, or discover.
                     incremental behaves like resume=True.

    Returns:
        Number of records written.
    """
    # Resolve mode: incremental aliases resume behaviour
    if mode == "incremental":
        resume = True
    session = make_session(token=token)

    # For marketplace, dedup by (parent_repo, path) — multiple skills in the same
    # registry repo share the same repo_url, so repo_url alone is too coarse.
    seen_paths: set[str] = set()
    if resume:
        try:
            from pathlib import Path as _Path
            import json as _json
            for line in _Path(output_path).open(encoding="utf-8", errors="replace"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                    meta = rec.get("raw_metadata", {})
                    key = f"{meta.get('parent_repo', '')}#{meta.get('path', '')}"
                    seen_paths.add(key)
                except Exception:
                    pass
        except (FileNotFoundError, OSError):
            pass
        logger.info("Resume mode: %d skills already in output", len(seen_paths))

    # Build combined list: known targets + discovered extras (only with auth to avoid rate limits)
    all_repos = list(TARGET_REPOS)
    if token:
        discovered = _discover_marketplace_repos(session)
        for repo in discovered:
            if repo not in all_repos:
                all_repos.append(repo)
    batch: list[dict] = []
    batch_size = 20
    written = 0

    for repo_full_name in all_repos:
        if limit is not None and written + len(batch) >= limit:
            break

        logger.info("Processing repo: %s", repo_full_name)
        repo_meta = fetch_repo_metadata(session, repo_full_name)
        default_branch = repo_meta.get("default_branch", "main")
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

            entry["default_branch"] = default_branch
            entry["stars"] = repo_meta.get("stargazers_count", 0)
            entry["pushed_at"] = repo_meta.get("pushed_at", "")
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

    try:
        count = run(
            output_path=args.output,
            token=token,
            limit=args.limit,
            resume=args.resume,
            mode=args.mode,
        )
        print(f"Done: {count} records written to {args.output}")
        return 0
    except Exception as exc:
        logger.error("Crawler failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
