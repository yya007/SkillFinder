"""
crawlers/base.py — Shared utilities for all SkillFinder crawlers.

Public API:
  GITHUB_API         - Base URL for GitHub REST API
  make_session()     - Build a requests.Session with retry adapter and auth
  github_get()       - Rate-limit-aware GET for GitHub API URLs
  extract_github_url() - Normalize any URL to a canonical GitHub repo URL
  write_jsonl()      - Write records to a JSONL file
  load_existing_urls() - Load repo_urls already present in an output file
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
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

        # Proactive rate-limit check on every response
        _maybe_wait_for_reset(resp)

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
                break

        if resp.status_code == 200:
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
    """Sleep until X-RateLimit-Reset (plus safety margin)."""
    reset_str = resp.headers.get("X-RateLimit-Reset")
    if reset_str:
        try:
            reset_ts = int(reset_str)
            wait = max(0, reset_ts - time.time()) + 5
            logger.info("Rate limit hit (%s); sleeping %.0fs until reset", label, wait)
            time.sleep(wait)
            return
        except ValueError:
            pass

    # Fallback: sleep 60s if no reset header
    logger.info("Rate limit hit (%s); no reset header, sleeping 60s", label)
    time.sleep(60)


# ---------------------------------------------------------------------------
# URL normalizer
# ---------------------------------------------------------------------------

_GITHUB_HOST = "github.com"

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

    return f"https://github.com/{owner}/{repo}".lower()


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
