"""
crawlers/base.py — Shared utilities for all SkillFinder crawlers.

Public API:
  GITHUB_API                    - Base URL for GitHub REST API
  make_session()                - Build a requests.Session with retry adapter and auth
  github_get()                  - Rate-limit-aware GET for GitHub API URLs
  extract_github_url()          - Normalize any URL to a canonical GitHub repo URL
  fetch_repo_metadata()         - Fetch stars/pushed_at/topics/branch for a repo
  fetch_repo_metadata_with_etag() - ETag-aware version; returns (dict|None, etag|None)
  fetch_repo_metadata_cached()   - Like fetch_repo_metadata_with_etag but with a persistent cache (304 = zero quota)
  fetch_commit_sha()            - Fetch HEAD commit SHA for a repo (one API call)
  find_skill_md_paths()         - Find all SKILL.md paths → {path: blob_sha} dict
  load_filter_cache()           - Load set of filtered-out canonical URLs
  add_to_filter_cache()         - Append a filtered URL+reason to the cache file
  infer_platforms()             - Infer target platforms from frontmatter and source
  write_jsonl()                 - Write records to a JSONL file
  load_existing_urls()          - Load repo_urls already present in an output file
  load_crawl_state()            - Load per-source crawl state from data/crawl_state/
  save_crawl_state()            - Atomically save crawl state to data/crawl_state/
  make_tombstone()              - Create a tombstone record for a deleted skill
  decode_b64_utf8()             - Decode a base64 string to UTF-8
  parse_frontmatter()           - Extract YAML frontmatter from a SKILL.md string
  fetch_skill_md()              - Fetch raw SKILL.md content (rate-limit-aware, symlink-safe)
  fetch_skill_md_cached()       - Fetch SKILL.md, skipping when blob SHA is cached
  load_content_cache()          - Load persistent blob-SHA -> content cache
  save_content_cache()          - Persist blob-SHA -> content cache
  find_skill_md_paths_cached()  - Find SKILL.md paths, skipping Trees API when pushed_at is unchanged
  load_tree_cache()             - Load persistent pushed_at -> {path: sha} tree cache
  save_tree_cache()             - Persist pushed_at -> {path: sha} tree cache
"""

from __future__ import annotations

import json
import logging
import threading
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

_api_counter_lock = threading.Lock()
_API_COUNTERS = {"rest": 0, "search": 0, "raw_free": 0, "conditional_304": 0, "graphql": 0}


def reset_api_counters() -> None:
    """Zero all API-call counters (call at the start of a measured run)."""
    with _api_counter_lock:
        for key in _API_COUNTERS:
            _API_COUNTERS[key] = 0


def get_api_counters() -> dict:
    """Return a snapshot of the API-call counters by category."""
    with _api_counter_lock:
        return dict(_API_COUNTERS)


def record_request(url: str, status_code: int) -> None:
    """Categorize one HTTP request for cost accounting.

    304 (conditional) and raw.githubusercontent.com requests are FREE — they do
    not consume the 5,000/hr REST quota. /search/* is metered at 10-30/min.
    """
    with _api_counter_lock:
        if status_code == 304:
            _API_COUNTERS["conditional_304"] += 1
        elif "raw.githubusercontent.com" in url:
            _API_COUNTERS["raw_free"] += 1
        elif "/graphql" in url:
            _API_COUNTERS["graphql"] += 1
        elif "/search/" in url:
            _API_COUNTERS["search"] += 1
        else:
            _API_COUNTERS["rest"] += 1


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string (e.g. '2026-03-16T12:00:00Z')."""
    from datetime import datetime, timezone
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

_USER_AGENT = "SkillFinder-Crawler/1.0 (+https://github.com/skillfinder/skillfinder)"

# Exponential backoff delays (seconds): attempt 1→1s, 2→4s, 3→16s
_BACKOFF_DELAYS = (1, 4, 16)

# Maximum number of consecutive rate-limit retries for a single request before
# giving up.  Without this bound a persistent 429 (GitHub's secondary/abuse
# rate limit, which several parallel crawlers sharing one token trip easily in
# CI) loops forever — the original cause of the 90-minute CI hang.
_MAX_RATELIMIT_RETRIES = 5

# Hard ceiling on any single rate-limit cooldown.  Secondary limits clear in
# seconds-to-a-minute and the search-API window resets every 60s, so a wait
# longer than this means a stale or far-future reset header — cap it so one
# request can't stall a crawler (and, in CI, the whole job) for ~an hour.
_MAX_WAIT_SECONDS = 120


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

def github_get(session: requests.Session, url: str, params: dict = None, timeout: int = 30, etag: str = None) -> dict | None:
    """GET a GitHub API URL with automatic rate-limit handling.

    Behaviour:
    - After every response, if X-RateLimit-Remaining < 5, sleep until
      X-RateLimit-Reset (plus a 5-second safety margin).
    - On 429 or 403 (with zero remaining quota), sleep until reset then retry.
    - Retries up to 3 times with exponential backoff (1s, 4s, 16s) for
      network errors and retriable HTTP errors.
    - Raises RuntimeError if all retries are exhausted on a non-200 response.
    - When ``etag`` is provided, sends an ``If-None-Match`` header and returns
      ``None`` on HTTP 304 (Not Modified).

    Args:
        session:  A requests.Session (from make_session()).
        url:      Full GitHub API URL.
        params:   Optional query string parameters.
        timeout:  Per-request timeout in seconds.
        etag:     Optional ETag value from a previous response; when set, a
                  304 Not Modified response causes the function to return None.

    Returns:
        Parsed JSON response body as a dict (or list wrapped in dict under
        special cases — callers should know what shape to expect).
        Returns None if etag was provided and the server returned 304.

    Raises:
        RuntimeError: if a non-200/304 status persists after all retries.
    """
    last_exc: Exception | None = None

    extra_headers: dict = {}
    if etag:
        extra_headers["If-None-Match"] = etag

    for attempt, delay in enumerate([0] + list(_BACKOFF_DELAYS)):
        if delay:
            logger.debug("Retrying %s in %ss (attempt %d)", url, delay, attempt)
            time.sleep(delay)

        try:
            resp = session.get(url, params=params, timeout=timeout, headers=extra_headers)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("Network error on %s: %s", url, exc)
            continue

        record_request(url, resp.status_code)

        # ETag 304: resource unchanged
        if resp.status_code == 304:
            logger.debug("ETag 304 for %s: not modified", url)
            return None

        # Rate-limit responses (429 always; 403 only when primary quota is
        # exhausted): wait — honoring Retry-After for secondary/abuse limits,
        # else X-RateLimit-Reset — then retry. The loop is BOUNDED: a
        # persistently throttled request raises after _MAX_RATELIMIT_RETRIES
        # so it can never hang the crawler (or the CI job) indefinitely.
        rl_retries = 0
        while resp.status_code in (429, 403):
            remaining = int(resp.headers.get("X-RateLimit-Remaining", "1"))
            if resp.status_code == 403 and remaining > 0:
                # 403 with quota remaining = permanent error (private repo,
                # bad token, org restriction). Raise immediately — no point
                # burning retries on a definitive access denial.
                raise RuntimeError(
                    f"Permanent 403 from {url}: {resp.text[:200]}"
                )

            if rl_retries >= _MAX_RATELIMIT_RETRIES:
                raise RuntimeError(
                    f"Rate limited on {url} after {rl_retries} retries "
                    f"(HTTP {resp.status_code}); giving up to avoid hanging"
                )

            _wait_for_reset(resp, label=url)
            rl_retries += 1
            try:
                resp = session.get(url, params=params, timeout=timeout, headers=extra_headers)
            except requests.RequestException as exc:
                last_exc = exc
                break
            record_request(url, resp.status_code)

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


def _rate_limit_wait_seconds(resp: requests.Response) -> float:
    """Compute how long to wait before retrying a rate-limited request.

    Precedence:
      1. ``Retry-After`` (seconds or HTTP-date) — authoritative for GitHub's
         secondary/abuse rate limits, which do NOT move X-RateLimit-Reset.
      2. ``X-RateLimit-Reset`` (epoch seconds) — primary quota window.
      3. A 60s default.

    The result is always clamped to ``_MAX_WAIT_SECONDS`` so a stale or
    far-future header can't stall a crawler for a long time.
    """
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        # Delta-seconds form.
        try:
            return min(int(retry_after) + 1, _MAX_WAIT_SECONDS)
        except ValueError:
            # HTTP-date form (e.g. "Wed, 21 Oct 2026 07:28:00 GMT").
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(retry_after)
                secs = dt.timestamp() - time.time()
                return min(max(0.0, secs) + 1, _MAX_WAIT_SECONDS)
            except (TypeError, ValueError):
                pass

    reset_str = resp.headers.get("X-RateLimit-Reset")
    if reset_str:
        try:
            return min(max(0.0, int(reset_str) - time.time()) + 5, _MAX_WAIT_SECONDS)
        except ValueError:
            pass

    return min(60.0, _MAX_WAIT_SECONDS)


def _wait_for_reset(resp: requests.Response, label: str = "") -> None:
    """Sleep for the computed rate-limit cooldown, printing an ETA countdown."""
    import sys

    wait = _rate_limit_wait_seconds(resp)

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


def load_existing_records(path: str) -> dict[str, dict]:
    """Load existing raw JSONL → dict keyed by skill_md_url (preferred) or repo_url.

    Used by crawlers in --update mode to check staleness.
    Returns empty dict if file does not exist.

    Args:
        path: Path to an existing JSONL file.

    Returns:
        Dict mapping skill_md_url (if non-empty in raw_metadata) or repo_url → full record.
        When a repo has multiple SKILL.md files, each file gets its own entry (keyed by
        skill_md_url). Repos without skill_md_url use repo_url as key.
    """
    import pathlib
    p = pathlib.Path(path)
    if not p.exists():
        return {}

    records: dict[str, dict] = {}
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                skill_md_url = record.get("raw_metadata", {}).get("skill_md_url", "")
                key = skill_md_url if skill_md_url else record.get("repo_url", "")
                if key:
                    records[key] = record
            except (json.JSONDecodeError, AttributeError):
                continue
    return records


# ---------------------------------------------------------------------------
# Trees API helper — find SKILL.md paths
# ---------------------------------------------------------------------------

def find_skill_md_paths(session, repo_full_name: str) -> dict[str, str]:
    """Return all paths to SKILL.md files in a repo mapped to their blob SHAs.

    Primary: recursive Trees API (one request, fast).
    Fallback: Code Search API scoped to the repo — used when the tree is
    truncated (GitHub caps the recursive tree at 100,000 nodes, so very large
    monorepos like openclaw/skills silently omit files beyond that point).

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.

    Returns:
        Dict mapping path → blob SHA (e.g. {"SKILL.md": "abc123", "skills/context/SKILL.md": "def456"}).
        Empty dict if the repo is not found or has no SKILL.md files.
        Blob SHAs may be empty strings for paths discovered via code search fallback.
    """
    url = f"{GITHUB_API}/repos/{repo_full_name}/git/trees/HEAD"
    try:
        data = github_get(session, url, params={"recursive": "1"})
    except RuntimeError as exc:
        logger.debug("Could not fetch tree for %s: %s", repo_full_name, exc)
        return {}

    if not data or "tree" not in data:
        return {}

    paths = {
        item["path"]: item.get("sha", "")
        for item in data["tree"]
        if item.get("type") == "blob" and item.get("path", "").endswith("SKILL.md")
    }

    if data.get("truncated"):
        logger.debug(
            "Tree truncated for %s (%d paths so far); falling back to code search",
            repo_full_name, len(paths),
        )
        paths = _find_skill_md_via_search(session, repo_full_name)

    return paths


def find_skill_md_paths_cached(
    session,
    repo_full_name: str,
    pushed_at: str,
    tree_cache: dict,
) -> dict[str, str]:
    """Return SKILL.md paths for a repo, skipping the Trees API call when unchanged.

    Uses ``pushed_at`` as a freshness key.  If the repo's ``pushed_at`` timestamp
    matches what is stored in ``tree_cache``, the cached ``{path: sha}`` mapping is
    returned immediately without any API call.  Otherwise ``find_skill_md_paths`` is
    called and the result is stored back into ``tree_cache`` (mutated in place).

    An empty or falsy ``pushed_at`` always calls the API and never caches the result
    because we cannot prove freshness without a timestamp.

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.
        pushed_at:      The repo's ``pushed_at`` ISO-8601 string from the metadata API.
                        Pass ``""`` (or any falsy value) to force a live fetch.
        tree_cache:     Mutable dict that persists across calls within a crawl run.
                        Shape: ``{repo_full_name: {"pushed_at": str, "paths": dict}}``.

    Returns:
        Dict mapping SKILL.md path → blob SHA (same contract as ``find_skill_md_paths``).
    """
    if pushed_at and tree_cache.get(repo_full_name, {}).get("pushed_at") == pushed_at:
        return tree_cache[repo_full_name]["paths"]

    paths = find_skill_md_paths(session, repo_full_name)
    if pushed_at:
        tree_cache[repo_full_name] = {"pushed_at": pushed_at, "paths": paths}
    return paths


def load_tree_cache(path: str) -> dict:
    """Load the persistent Trees-API path cache, or {} if absent/corrupt.

    Thin alias for ``load_meta_cache`` — same JSON format, different file.
    Cache shape: ``{repo_full_name: {"pushed_at": str, "paths": {path: sha}}}``.
    """
    return load_meta_cache(path)


def save_tree_cache(cache: dict, path: str) -> None:
    """Persist the Trees-API path cache atomically.

    Thin alias for ``save_meta_cache`` — same atomic-write semantics, different file.
    """
    save_meta_cache(cache, path)


def _find_skill_md_via_search(session, repo_full_name: str) -> dict[str, str]:
    """Find SKILL.md paths via Code Search API, scoped to one repo.

    Used as a fallback when the Trees API returns a truncated result.
    Code Search paginates properly and is not subject to the 100k-node cap.
    Capped at GitHub's 1000-result search limit per query.

    Returns a dict mapping path → "" (blob SHAs not available from code search).
    """
    import time as _time

    paths: dict[str, str] = {}
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
            if p and p not in paths:
                paths[p] = ""  # no blob SHA from code search API

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

def load_meta_cache(path: str) -> dict:
    """Load the persistent repo-metadata/ETag cache, or {} if absent/corrupt."""
    import pathlib
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_meta_cache(cache: dict, path: str) -> None:
    """Persist the repo-metadata/ETag cache atomically."""
    import pathlib
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)
    tmp.replace(p)


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

    if data is None:
        return {}

    return {
        "stargazers_count": data.get("stargazers_count", 0),
        "pushed_at": data.get("pushed_at", ""),
        "topics": data.get("topics", []),
        "description": data.get("description", ""),
        "default_branch": data.get("default_branch", "main"),
    }


def fetch_repo_metadata_with_etag(
    session,
    repo_full_name: str,
    etag: str = None,
) -> tuple[dict | None, str | None]:
    """Fetch metadata for a single repo with ETag support.

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.
        etag:           Optional ETag from a previous response.  When provided
                        and the resource is unchanged, (None, new_etag) is
                        returned.

    Returns:
        (metadata_dict, new_etag) where:
          - metadata_dict is None on 304 Not Modified (resource unchanged).
          - metadata_dict is {} on other errors.
          - new_etag is the ETag from the response header, or None.
    """
    url = f"{GITHUB_API}/repos/{repo_full_name}"
    headers: dict = {}
    if etag:
        headers["If-None-Match"] = etag

    rl_retries = 0
    while True:
        try:
            resp = session.get(url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            logger.warning("Could not fetch metadata for %s: %s", repo_full_name, exc)
            return {}, None

        record_request(url, resp.status_code)

        # Honor rate limits like github_get: wait for reset and retry on 429 or
        # quota-exhausted 403, bounded by _MAX_RATELIMIT_RETRIES. A 403 with
        # quota remaining is a permanent error (private/forbidden), not a throttle.
        if resp.status_code in (429, 403):
            remaining = int(resp.headers.get("X-RateLimit-Remaining", "1"))
            if resp.status_code == 403 and remaining > 0:
                break
            if rl_retries >= _MAX_RATELIMIT_RETRIES:
                break
            _wait_for_reset(resp, label=url)
            rl_retries += 1
            continue
        break

    new_etag = resp.headers.get("ETag")

    if resp.status_code == 304:
        logger.debug("ETag 304 for %s: metadata unchanged", repo_full_name)
        return None, new_etag

    if resp.status_code != 200:
        logger.warning("HTTP %s fetching metadata for %s", resp.status_code, repo_full_name)
        return {}, None

    try:
        data = resp.json()
    except ValueError:
        return {}, None

    return {
        "stargazers_count": data.get("stargazers_count", 0),
        "pushed_at": data.get("pushed_at", ""),
        "topics": data.get("topics", []),
        "description": data.get("description", ""),
        "default_branch": data.get("default_branch", "main"),
    }, new_etag


def fetch_repo_metadata_cached(session, repo_full_name: str, cache: dict) -> dict:
    """Like fetch_repo_metadata, but uses a persistent ETag cache.

    On a 304 (resource unchanged) the cached metadata is returned and NO quota is
    spent on the body. ``cache`` is mutated in place; persist it with
    save_meta_cache() after the crawl.
    """
    entry = cache.get(repo_full_name, {})
    etag = entry.get("etag")
    meta, new_etag = fetch_repo_metadata_with_etag(session, repo_full_name, etag)

    if meta is None:  # 304 Not Modified — reuse cached metadata
        return {k: v for k, v in entry.items() if k != "etag"}

    if meta:  # 200 — refresh cache
        cache[repo_full_name] = {
            "etag": new_etag or "",
            "pushed_at": meta.get("pushed_at", ""),
            "stargazers_count": meta.get("stargazers_count", 0),
            "topics": meta.get("topics", []),
            "description": meta.get("description", ""),
            "default_branch": meta.get("default_branch", "main"),
        }
    return meta


def fetch_commit_sha(session, repo_full_name: str) -> str | None:
    """Fetch the HEAD commit SHA for a repo (one API call).

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.

    Returns:
        The SHA string or None on error.
    """
    try:
        data = github_get(session, f"{GITHUB_API}/repos/{repo_full_name}/commits/HEAD")
        if data is None:
            return None
        return data.get("sha")
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# Platform inference
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Crawl state persistence
# ---------------------------------------------------------------------------

def load_crawl_state(source: str, state_dir: str = "data/crawl_state") -> dict:
    """Load per-source crawl state from data/crawl_state/{source}.json.

    Returns empty state dict if file doesn't exist (first run).

    Args:
        source:    Crawler source name (e.g. "skillsmp", "clawhub").
        state_dir: Directory to read state files from.

    Returns:
        State dict with at minimum keys: source, repos, awesome_lists.
    """
    import pathlib
    path = pathlib.Path(state_dir) / f"{source}.json"
    if not path.exists():
        return {"source": source, "repos": {}, "awesome_lists": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"source": source, "repos": {}, "awesome_lists": {}}


def save_crawl_state(state: dict, source: str, state_dir: str = "data/crawl_state") -> None:
    """Atomically save crawl state to data/crawl_state/{source}.json.

    Uses a write-then-rename pattern to avoid partial writes.

    Args:
        state:     State dict to persist.
        source:    Crawler source name (used to derive filename).
        state_dir: Directory to write state files to.
    """
    import pathlib
    import os
    p = pathlib.Path(state_dir)
    p.mkdir(parents=True, exist_ok=True)
    dest = p / f"{source}.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, dest)


def make_tombstone(repo_url: str, skill_md_url: str, source: str) -> dict:
    """Create a tombstone record for a deleted skill.

    Tombstone records are written to the raw JSONL output when a SKILL.md
    path that was present in a previous crawl run is no longer found in the
    current tree.  The normalize pipeline skips tombstone records, preventing
    deleted skills from persisting in the index.

    Args:
        repo_url:     Canonical GitHub repo URL.
        skill_md_url: URL to the deleted SKILL.md blob.
        source:       Crawler source name.

    Returns:
        Tombstone record dict.
    """
    return {
        "repo_url": repo_url,
        "skill_md_url": skill_md_url,
        "source": source,
        "tombstone": True,
        "deleted_at": _utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# SKILL.md content helpers
# ---------------------------------------------------------------------------

def decode_b64_utf8(encoded: str) -> str:
    """Decode a base64-encoded string to UTF-8, replacing invalid bytes.

    Strips embedded newlines (as returned by the GitHub Contents API) before
    decoding.

    Args:
        encoded: Base64-encoded string (may contain newlines).

    Returns:
        Decoded string, with invalid bytes replaced by the U+FFFD character.
    """
    import base64
    return base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")


def parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from a SKILL.md string.

    Strips leading HTML comments (``<!-- ... -->``) before the opening ``---``
    so that files starting with copyright headers are parsed correctly.  All
    five crawlers previously had their own copy of this logic; this canonical
    version is the superset.

    Args:
        content: Raw SKILL.md file content.

    Returns:
        Dict of frontmatter fields, or an empty dict if no frontmatter is
        present or parsing fails.
    """
    import re
    import yaml

    if not content:
        return {}
    # Strip leading HTML comments (e.g. <!-- Copyright 2025 Acme Corp. -->)
    stripped = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL).lstrip()
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


def _fetch_skill_md_via_api(
    session,
    repo_full_name: str,
    path: str = "SKILL.md",
    default_branch: str = "main",
    _depth: int = 0,
) -> str | None:
    """Fetch raw SKILL.md content via the GitHub Contents API.

    This is the canonical, superset implementation replacing the five per-crawler
    ``_fetch_skill_md`` / ``fetch_skill_content`` helpers.  Key improvements:

    * Uses ``github_get()`` (not ``session.get()`` directly) so rate-limit
      handling, backoff, and retry logic are applied consistently.
    * Resolves symlinks: when the Contents API returns ``"type": "symlink"``,
      the target path is resolved relative to the file's directory and fetched
      recursively (max depth 1 to prevent infinite loops).
    * Branch fallback: tries ``default_branch`` first, then ``"main"``, then
      ``"master"``.  This matches the behaviour previously only available in
      ``skillsmp_crawler._fetch_skill_md``.

    Args:
        session:        A requests.Session from make_session().
        repo_full_name: "{owner}/{repo}" string.
        path:           Path to the file within the repo (default "SKILL.md").
        default_branch: Branch to try first.
        _depth:         Internal recursion guard — do not set manually.

    Returns:
        Decoded file content as a string, or None on any error.
    """
    import posixpath

    branches_to_try = [default_branch]
    for fallback in ("main", "master"):
        if fallback not in branches_to_try:
            branches_to_try.append(fallback)

    for branch in branches_to_try:
        url = f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}"
        try:
            data = github_get(session, url, params={"ref": branch})
        except RuntimeError as exc:
            logger.debug(
                "Could not fetch %s@%s from %s: %s", path, branch, repo_full_name, exc
            )
            continue

        if data is None:
            continue

        # Resolve symlinks (max one level of recursion)
        if data.get("type") == "symlink" and _depth == 0:
            target = data.get("target", "")
            if target:
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(path), target)
                )
                logger.debug(
                    "Symlink at %s in %s → %s", path, repo_full_name, resolved
                )
                return _fetch_skill_md_via_api(
                    session, repo_full_name, resolved, branch, _depth=1
                )

        encoded = data.get("content", "")
        if not encoded:
            return None
        try:
            return decode_b64_utf8(encoded)
        except Exception as exc:
            logger.debug(
                "Base64 decode error for %s/%s: %s", repo_full_name, path, exc
            )
            continue

    return None


def _looks_like_skill_file(text: str) -> bool:
    """True if raw body is plausibly a real SKILL.md (not a symlink target/404)."""
    if "---" in text:               # has YAML frontmatter delimiter
        return True
    return len(text) > 200          # long enough to be real content, not a path


def _fetch_skill_md_via_raw(session, repo_full_name, path, default_branch="main") -> str | None:
    """Fetch SKILL.md from the free raw CDN. Returns None on any non-200 / miss."""
    branches = [default_branch]
    for fallback in ("main", "master"):
        if fallback not in branches:
            branches.append(fallback)
    for branch in branches:
        url = f"{RAW_BASE}/{repo_full_name}/{branch}/{path}"
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException:
            continue
        record_request(url, resp.status_code)
        if resp.status_code == 200 and _looks_like_skill_file(resp.text):
            return resp.text
    return None


def fetch_skill_md(
    session,
    repo_full_name: str,
    path: str = "SKILL.md",
    default_branch: str = "main",
    _depth: int = 0,
) -> str | None:
    """Fetch SKILL.md content, preferring the free raw CDN over the Contents API.

    raw.githubusercontent.com does not consume the 5,000/hr REST quota. The
    Contents API is used only as a fallback (raw miss, or a body that looks like
    a symlink target rather than a real skill file), where it also resolves
    symlinks and private repos.
    """
    raw = _fetch_skill_md_via_raw(session, repo_full_name, path, default_branch)
    if raw is not None:
        return raw
    return _fetch_skill_md_via_api(session, repo_full_name, path, default_branch, _depth)


def fetch_skill_md_cached(
    session,
    repo_full_name: str,
    path: str,
    blob_sha: str,
    default_branch: str,
    content_cache: dict,
) -> str | None:
    """Fetch SKILL.md, reusing cached content when the blob SHA is unchanged.

    A git blob SHA uniquely identifies file content, so a hit means the file is
    byte-identical to a previously-fetched one (in this repo, another repo, or a
    prior run) — return it with no network call. Empty SHAs (code-search
    fallback) are never cached. ``content_cache`` maps blob_sha -> content.
    """
    if blob_sha and blob_sha in content_cache:
        return content_cache[blob_sha]
    content = fetch_skill_md(session, repo_full_name, path, default_branch)
    if blob_sha and content is not None:
        content_cache[blob_sha] = content
    return content


def load_content_cache(path: str) -> dict:
    """Load the persistent blob-SHA -> content cache, or {} if absent/corrupt."""
    return load_meta_cache(path)


def save_content_cache(cache: dict, path: str) -> None:
    """Persist the blob-SHA -> content cache atomically."""
    save_meta_cache(cache, path)


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
