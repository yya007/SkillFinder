"""
scripts/fetch_skill.py

Fetch a SKILL.md file from a GitHub repository by trying a prioritised list of
raw-content URLs.  Supports YAML frontmatter parsing and optional file output.

Usage:
    python scripts/fetch_skill.py --repo https://github.com/owner/repo
    python scripts/fetch_skill.py --repo https://github.com/owner/repo --output ./SKILL.md
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Optional
from urllib.parse import urlparse

import requests
import yaml


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class FetchError(Exception):
    """Raised when SKILL.md cannot be fetched from any candidate URL.

    Attributes
    ----------
    attempted_urls:
        List of URLs that were tried before giving up.
    """

    def __init__(self, message: str, attempted_urls: list[str]) -> None:
        super().__init__(message)
        self.attempted_urls: list[str] = attempted_urls


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------

def parse_github_url(url: str) -> tuple[str, str]:
    """Parse a GitHub repository URL and return ``(owner, repo)``.

    Accepts URLs with or without ``.git`` suffix, trailing slashes, or extra
    path segments (e.g. ``/tree/main/subdir``).

    Parameters
    ----------
    url:
        A GitHub repository URL, e.g. ``https://github.com/owner/repo``.

    Returns
    -------
    tuple[str, str]
        ``(owner, repo)`` both lowercased.

    Raises
    ------
    ValueError
        For non-GitHub URLs, URLs that lack an owner or repo segment, or
        empty strings.
    """
    if not url:
        raise ValueError("URL must not be empty.")

    parsed = urlparse(url)

    if parsed.hostname is None or "github.com" not in parsed.hostname.lower():
        raise ValueError(f"Not a GitHub URL: {url!r}")

    # Strip leading slash and split path segments
    path = parsed.path.lstrip("/")
    segments = [s for s in path.split("/") if s]

    if len(segments) < 2:
        raise ValueError(
            f"GitHub URL must include owner and repo, got: {url!r}"
        )

    owner = segments[0].lower()
    repo = segments[1].lower().removesuffix(".git")

    return owner, repo


def candidate_urls(owner: str, repo: str) -> list[str]:
    """Return an ordered list of raw-content URLs to try for SKILL.md.

    Parameters
    ----------
    owner:
        Repository owner (case-sensitive on GitHub, but caller should pass
        the value returned by :func:`parse_github_url`).
    repo:
        Repository name.

    Returns
    -------
    list[str]
        Six URLs in priority order:
        1. ``main/SKILL.md``           — root, main branch
        2. ``master/SKILL.md``         — root, master branch
        3. ``main/.claude/SKILL.md``   — Claude Code convention
        4. ``master/.claude/SKILL.md``
        5. ``main/.agent/SKILL.md``    — Codex / OpenAI Agents convention
        6. ``master/.agent/SKILL.md``
    """
    base = f"https://raw.githubusercontent.com/{owner}/{repo}"
    return [
        f"{base}/main/SKILL.md",
        f"{base}/master/SKILL.md",
        f"{base}/main/.claude/SKILL.md",
        f"{base}/master/.claude/SKILL.md",
        f"{base}/main/.agent/SKILL.md",
        f"{base}/master/.agent/SKILL.md",
    ]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _discover_skill_md_paths(
    owner: str, repo: str, timeout: int = 10
) -> list[str]:
    """Use the GitHub git-tree API to find SKILL.md at any path in the repo.

    Returns raw-content URLs for any file whose name is ``SKILL.md``
    (case-insensitive) that isn't already in :func:`candidate_urls`.
    Returns an empty list on any error (API unavailable, private repo, etc.).
    """
    known = set(candidate_urls(owner, repo))
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1",
            timeout=timeout,
        )
        if resp.status_code != 200:
            return []
        tree = resp.json().get("tree", [])
        base = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD"
        extras = []
        for item in tree:
            path = item.get("path", "")
            if path.upper().endswith("SKILL.MD"):
                url = f"{base}/{path}"
                if url not in known:
                    extras.append(url)
        return extras
    except Exception:
        return []


def fetch_skill_md(repo_url: str, timeout: int = 10) -> tuple[str, str]:
    """Fetch SKILL.md content from a GitHub repository.

    Tries each URL from :func:`candidate_urls` in order, then falls back to
    the GitHub git-tree API to discover SKILL.md at arbitrary paths.

    Parameters
    ----------
    repo_url:
        GitHub repository URL.
    timeout:
        HTTP request timeout in seconds.

    Returns
    -------
    tuple[str, str]
        ``(content, resolved_url)`` where *resolved_url* is the URL that
        returned HTTP 200.

    Raises
    ------
    FetchError
        If all candidate URLs and API-discovered URLs fail.
    """
    owner, repo = parse_github_url(repo_url)
    urls = candidate_urls(owner, repo)
    attempted: list[str] = []

    for url in urls:
        attempted.append(url)
        try:
            response = requests.get(url, timeout=timeout)
            if response.status_code == 200:
                return response.text, url
        except requests.RequestException:
            pass

    # All well-known paths failed — ask GitHub where SKILL.md actually lives.
    for url in _discover_skill_md_paths(owner, repo, timeout=timeout):
        attempted.append(url)
        try:
            response = requests.get(url, timeout=timeout)
            if response.status_code == 200:
                return response.text, url
        except requests.RequestException:
            pass

    raise FetchError(
        f"Could not fetch SKILL.md for {repo_url!r}. Tried {len(attempted)} URL(s).",
        attempted_urls=attempted,
    )


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)^---\n(.*)", re.DOTALL | re.MULTILINE)


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from *content*.

    Parameters
    ----------
    content:
        Raw SKILL.md text, possibly starting with ``---\\n...\\n---\\n``.

    Returns
    -------
    tuple[dict, str]
        ``(frontmatter_dict, body)``

        * If no frontmatter delimiters are found, returns ``({}, content)``.
        * If delimiters are present but the YAML block is empty, returns
          ``({}, body)``.

    Raises
    ------
    ValueError
        If frontmatter delimiters are present but the YAML is syntactically
        invalid.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        # No frontmatter delimiters found
        return {}, content

    yaml_block = match.group(1)
    body = match.group(2)

    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML frontmatter: {exc}") from exc

    if parsed is None:
        # Empty YAML block (e.g. "---\n---\n")
        return {}, body

    return parsed, body


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------

def fetch_and_output(
    repo_url: str,
    output_path: Optional[str] = None,
    timeout: int = 10,
) -> str:
    """Fetch SKILL.md and optionally write it to a file.

    Parameters
    ----------
    repo_url:
        GitHub repository URL.
    output_path:
        If given, the content is written to this path and the path string is
        returned.  If ``None``, the content string is returned directly.
    timeout:
        HTTP request timeout in seconds.

    Returns
    -------
    str
        The content string when *output_path* is ``None``, or *output_path*
        when a file was written.

    Raises
    ------
    FetchError
        Propagated from :func:`fetch_skill_md` when all URLs fail.
    """
    content, resolved_url = fetch_skill_md(repo_url, timeout=timeout)

    if output_path is not None:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return output_path

    return content


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch SKILL.md from a GitHub repository.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="URL",
        help="GitHub repository URL, e.g. https://github.com/owner/repo",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="Write SKILL.md to this file path (default: print to stdout)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        result = fetch_and_output(args.repo, output_path=args.output)
        if args.output:
            print(f"Written to {result}")
        else:
            print(result)
    except FetchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(f"Attempted URLs:", file=sys.stderr)
        for url in exc.attempted_urls:
            print(f"  {url}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
