"""
crawlers/base.py — Shared utilities for all SkillFinder crawlers.

Public API:
  GITHUB_API            - Base URL for GitHub REST API
  make_session()        - Build a requests.Session with retry adapter and auth
  github_get()          - Rate-limit-aware GET for GitHub API URLs
  extract_github_url()  - Normalize any URL to a canonical GitHub repo URL
  fetch_repo_metadata() - Fetch stars/pushed_at/topics/branch for a repo
  find_skill_md_paths() - Find all SKILL.md paths in a repo via Trees API
  load_filter_cache()   - Load set of filtered-out canonical URLs
  add_to_filter_cache() - Append a filtered URL+reason to the cache file
  infer_platforms()     - Infer target platforms from frontmatter and source
  write_jsonl()         - Write records to a JSONL file
  load_existing_urls()  - Load repo_urls already present in an output file
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

_USER_AGENT = "SkillFinder-Crawler/1.0 (+https://github.com/skillfinder/skillfinder)"

# Exponential backoff delays (seconds): attempt 1→1s, 2→4s, 3→16s
_BACKOFF_DELAYS = (1, 4, 16)


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

def make_session(token: str = None) -> requests.Session:
    """Create a requests.Session with User-Agent, optional Authorization, and retry adapter.

    The retry adapter handles low-level transport errors (connection reset,
    read timeout) with up to 3 retries and exponential backoff.  Application-
    level rate-limit handling (429/403) is done in github_get().

    Args:
        token: GitHub personal access token (Bearer auth).  Pass None to
               make unauthenticated requests (60 req/hr rate limit).

    Returns:
        A configured requests.Session ready to use.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})

    if token:
        session.headers["Authorization"] = f"Bearer {token}"

    # Accept GitHub v3 JSON
    session.headers["Accept"] = "application/vnd.github+json"
    session.headers["X-GitHub-Api-Version"] = "2022-11-28"

    # Transport-level retry (does NOT retry on 4xx/5xx — that's handled by
    # github_get() so we can respect Retry-After / RateLimit-Reset headers)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


# ---------------------------------------------------------------------------
# Rate-limit-aware GET
# ---------------------------------------------------------------------------

def github_get(session: requests.Session, url: str, params: dict = None, timeout: int = 30) -> dict:
    """GET a GitHub API URL with automatic rate-limit handling.

    Behaviour:
    - After every response, if X-RateLimit-Remaining < 5, sleep until
      X-RateLimit-Reset (plus a 5-second safety margin).
    - On 429 or 403 (with zero remaining quota), sleep until reset then retry.
    - Retries up to 3 times with exponential backoff (1s, 4s, 16s) for
      network errors and retriable HTTP errors.
    - Raises RuntimeError if all retries are exhausted on a non-200 response.

    Args:
        session:  A requests.Session (from make_session()).
        url:      Full GitHub API URL.
        params:   Optional query string parameters.
        timeout:  Per-request timeout in seconds.

    Returns:
        Parsed JSON response body as a dict (or list wrapped in dict under
        special cases — callers should know what shape to expect).

    Raises:
        RuntimeError: if a non-200 status persists after all retries.
    """
    last_exc: Exception | None = None

    for attempt, delay in enumerate([0] + list(_BACKOFF_DELAYS)):
        if delay:
            logger.debug("Retrying %s in %ss (attempt %d)", url, delay, attempt)
            time.sleep(delay)

        try:
            resp = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("Network error on %s: %s", url, exc)
            continue

        # Rate-limit responses (429/403 with quota exhausted): sleep until reset
        # and retry immediately without consuming a backoff attempt slot.
        while resp.status_code in (429, 403):
            remaining = int(resp.headers.get("X-RateLimit-Remaining", "1"))
            if remaining == 0 or resp.status_code == 429:
                _wait_for_reset(resp, label=url)
                try:
                    resp = session.get(url, params=params, timeout=timeout)
                except requests.RequestException as exc:
                    last_exc = exc
                    break
            else:
                # 403 with quota remaining = permanent error (private repo,
                # bad token, org restriction). Raise immediately — no point
                # burning backoff retries on a definitive access denial.
                raise RuntimeError(
                    f"Permanent 403 from {url}: {resp.text[:200]}"
                )

        if resp.status_code == 200:
            # Proactive check: if remaining quota is critically low, sleep
            # before returning so the *next* call doesn't hit the wall.
            _maybe_wait_for_reset(resp)
            try:
                return resp.json()
            except ValueError as exc:
                raise RuntimeError(f"Invalid JSON from {url}: {exc}") from exc

        # Any other non-200: log and retry with backoff
        logger.warning("HTTP %s from %s", resp.status_code, url)
        last_exc = RuntimeError(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}")

    raise RuntimeError(
        f"Failed to GET {url} after {len(_BACKOFF_DELAYS) + 1} attempts"
    ) from last_exc


def _maybe_wait_for_reset(resp: requests.Response) -> None:
    """Proactively sleep if X-RateLimit-Remaining is critically low."""
    remaining_str = resp.headers.get("X-RateLimit-Remaining")
    if remaining_str is None:
        return
    try:
        remaining = int(remaining_str)
    except ValueError:
        return

    if remaining < 5:
        _wait_for_reset(resp, label=resp.url)


def _wait_for_reset(resp: requests.Response, label: str = "") -> None:
    """Sleep until X-RateLimit-Reset (plus safety margin), printing an ETA countdown."""
    import sys

    reset_str = resp.headers.get("X-RateLimit-Reset")
    if reset_str:
        try:
            reset_ts = int(reset_str)
            wait = max(0, reset_ts - time.time()) + 5
        except ValueError:
            wait = 60.0
    else:
        wait = 60.0

    logger.info("Rate limit hit (%s); waiting %.0fs until reset", label, wait)

    deadline = time.monotonic() + wait
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        print(f"\r  rate-limit cooldown: {remaining:.0f}s remaining…  ", end="", flush=True, file=sys.stderr)
        time.sleep(min(1.0, remaining))

    print(f"\r  rate-limit cooldown: done.{' ' * 20}", file=sys.stderr)


# ---------------------------------------------------------------------------
# URL normalizer
# ---------------------------------------------------------------------------

_GITHUB_HOST = "github.com"

# Known GitHub owner/repo renames — applied during URL normalization so that
# old and new names produce the same canonical key.  Specific renames must
# come before general org renames so they match first.
_GITHUB_REDIRECTS: dict[str, str] = {
    # Specific repo renames
    "github.com/clawdbot/clawdbot": "github.com/openclaw/openclaw",
    "github.com/moltbot/moltbot": "github.com/openclaw/openclaw",
    # General org renames
    "github.com/clawdbot/": "github.com/openclaw/",
    "github.com/moltbot/": "github.com/openclaw/",
}

# Path segments that indicate we're inside a repo subtree, not the repo root
_SUBTREE_SEGMENTS = frozenset(
    ["tree", "blob", "commits", "issues", "pulls", "wiki", "releases",
     "actions", "projects", "discussions", "compare", "tags", "branches",
     "graphs", "network", "pulse", "security", "settings"]
)


def extract_github_url(url: str) -> str | None:
    """Normalize a URL to a canonical GitHub repo URL.

    Rules:
    - Returns None if the URL is not on github.com or owner/repo cannot
      be extracted.
    - Strips .git suffix, trailing slashes, and any path segments beyond
      the repo name (e.g., /tree/main/subdir).
    - Lowercases the result.

    Examples::

        extract_github_url("https://github.com/user/repo.git")
        # → "https://github.com/user/repo"

        extract_github_url("https://github.com/user/repo/tree/main/subdir")
        # → "https://github.com/user/repo"

        extract_github_url("https://notgithub.com/user/repo")
        # → None
    """
    if not url:
        return None

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    # Must be github.com (with or without www.)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host != _GITHUB_HOST:
        return None

    # Split path into non-empty segments
    path_parts = [p for p in parsed.path.split("/") if p]

    # Need at least owner + repo
    if len(path_parts) < 2:
        return None

    owner = path_parts[0]
    repo = path_parts[1]

    # Strip .git suffix from repo name
    if repo.lower().endswith(".git"):
        repo = repo[:-4]

    if not owner or not repo:
        return None

    result = f"https://github.com/{owner}/{repo}".lower()

    # Apply known GitHub renames (e.g. clawdbot/ → openclaw/)
    for old, new in _GITHUB_REDIRECTS.items():
        if old in result:
            result = result.replace(old, new, 1)
            break

    return result


# ---------------------------------------------------------------------------
# JSONL I/O helpers
# ---------------------------------------------------------------------------

def write_jsonl(records: list[dict], output_path: str, append: bool = False) -> int:
    """Write records to a JSONL file.

    Args:
        records:     List of dicts to serialise, one per line.
        output_path: Destination file path.  Parent directories are created
                     automatically.
        append:      If True, open in append mode; otherwise overwrite.

    Returns:
        Number of records written.
    """
    import pathlib
    pathlib.Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    mode = "a" if append else "w"
    count = 0
    with open(output_path, mode, encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def load_existing_urls(output_path: str) -> set[str]:
    """Load the set of repo_urls already written to output_path.

    Used by crawlers with --resume support to skip repos that were already
    collected in a previous (possibly interrupted) run.

    Args:
        output_path: Path to an existing JSONL file.  If the file does not
                     exist, an empty set is returned.

    Returns:
        Set of repo_url strings found in the file.
    """
    import pathlib
    path = pathlib.Path(output_path)
    if not path.exists():
        return set()

    urls: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                repo_url = record.get("repo_url")
                if repo_url:
                    urls.add(repo_url)
            except (json.JSONDecodeError, AttributeError):
                continue
    return urls


# ---------------------------------------------------------------------------
# Trees API helper — find SKILL.md paths
# ---------------------------------------------------------------------------

def find_skill_md_paths(session, repo_full_name: str) -> list[str]:
    """Return all paths to SKILL.md files in a repo.

    Primary: recursive Trees API (one request, fast).
    Fallback: Code Search API scoped to the repo — used when the tree is
    truncated (GitHub caps the recursive tree at 100,000 nodes, so very large
    monorepos like openclaw/skills silently omit files beyond that point).

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.

    Returns:
        List of paths (e.g. ["SKILL.md", "skills/context/SKILL.md"]).
        Empty list if the repo is not found or has no SKILL.md files.
    """
    url = f"{GITHUB_API}/repos/{repo_full_name}/git/trees/HEAD"
    try:
        data = github_get(session, url, params={"recursive": "1"})
    except RuntimeError as exc:
        logger.debug("Could not fetch tree for %s: %s", repo_full_name, exc)
        return []

    if not data or "tree" not in data:
        return []

    paths = [
        item["path"]
        for item in data["tree"]
        if item.get("type") == "blob" and item.get("path", "").endswith("SKILL.md")
    ]

    if data.get("truncated"):
        logger.debug(
            "Tree truncated for %s (%d paths so far); falling back to code search",
            repo_full_name, len(paths),
        )
        paths = _find_skill_md_via_search(session, repo_full_name)

    return paths


def _find_skill_md_via_search(session, repo_full_name: str) -> list[str]:
    """Find SKILL.md paths via Code Search API, scoped to one repo.

    Used as a fallback when the Trees API returns a truncated result.
    Code Search paginates properly and is not subject to the 100k-node cap.
    Capped at GitHub's 1000-result search limit per query.
    """
    import time as _time

    paths: list[str] = []
    seen: set[str] = set()
    page = 1

    while True:
        params = {
            "q": f"filename:SKILL.md repo:{repo_full_name}",
            "per_page": 100,
            "page": page,
        }
        try:
            data = github_get(session, f"{GITHUB_API}/search/code", params=params)
        except RuntimeError as exc:
            logger.warning("Code search fallback failed for %s: %s", repo_full_name, exc)
            break

        items = data.get("items", [])
        for item in items:
            p = item.get("path", "")
            if p and p not in seen:
                seen.add(p)
                paths.append(p)

        total = data.get("total_count", 0)
        if not items or page * 100 >= min(total, 1000):
            break

        page += 1
        _time.sleep(2)  # Code Search rate limit: 10 req/min authenticated

    logger.debug("Code search found %d SKILL.md paths in %s", len(paths), repo_full_name)
    return paths


# ---------------------------------------------------------------------------
# Filter cache
# ---------------------------------------------------------------------------

def load_filter_cache(path: str) -> set[str]:
    """Load the set of canonical repo URLs that have been filtered out.

    Args:
        path: Path to the filter cache JSONL file.  If the file does not
              exist, an empty set is returned.

    Returns:
        Set of canonical repo URL strings from the cache.
    """
    import pathlib
    p = pathlib.Path(path)
    if not p.exists():
        return set()

    urls: set[str] = set()
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                url = record.get("url")
                if url:
                    urls.add(url)
            except (json.JSONDecodeError, AttributeError):
                continue
    return urls


def add_to_filter_cache(path: str, repo_url: str, reason: str) -> None:
    """Append a filtered repo URL and reason to the cache file.

    Args:
        path:     Path to the filter cache JSONL file.
        repo_url: Canonical GitHub repo URL.
        reason:   Why this repo was filtered (e.g. "no_skill_md",
                  "stars_below_threshold", "not_a_skill").
    """
    import pathlib
    from datetime import datetime, timezone
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "url": repo_url,
        "reason": reason,
        "filtered_at": (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        ),
    }
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# GitHub repo metadata
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Platform inference
# ---------------------------------------------------------------------------

def infer_platforms(frontmatter: dict, source: str) -> list[str]:
    """Infer target platforms from frontmatter and source.

    Priority:
    1. Explicit ``platforms`` list in frontmatter.
    2. Codex-specific keys (``codex_context``, ``target == "codex"``).
    3. Source-based defaults.

    Args:
        frontmatter: Parsed YAML frontmatter dict (may be empty).
        source:      Crawler source name (skillsmp, clawhub, marketplace, skillhub).

    Returns:
        List of platform strings.
    """
    if "platforms" in frontmatter:
        raw = frontmatter["platforms"]
        if isinstance(raw, list):
            return [str(p) for p in raw if p]
    if "codex_context" in frontmatter or frontmatter.get("target") == "codex":
        return ["codex"]
    defaults = {
        "skillsmp": ["claude_code"],
        "clawhub": ["openclaw"],   # ClawHub is OpenClaw's registry; claude_code only if frontmatter says so
        "marketplace": ["claude_code"],
        "skillhub": ["claude_code"],
    }
    return defaults.get(source, ["claude_code"])
