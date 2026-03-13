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
import logging
import re
import sys
from typing import Iterator

# ---------------------------------------------------------------------------
# Lazy import of base helpers (written by companion agent)
# ---------------------------------------------------------------------------
from crawlers.base import (
    extract_github_url,
    github_get,
    load_existing_urls,
    make_session,
    write_jsonl,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AWESOME_LIST_REPO = "VoltAgent/awesome-openclaw-skills"
AWESOME_LIST_PATH = "README.md"

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
# Fetch
# ---------------------------------------------------------------------------

def fetch_awesome_readme(session) -> str:
    """Fetch raw README.md from VoltAgent/awesome-openclaw-skills via GitHub Contents API.

    Returns the decoded text content.

    Raises:
        RuntimeError: if the API call fails or the content cannot be decoded.
    """
    url = _CONTENTS_URL.format(repo=AWESOME_LIST_REPO, path=AWESOME_LIST_PATH)
    data = github_get(session, url)

    encoding = data.get("encoding", "")
    content_raw = data.get("content", "")

    if encoding == "base64":
        try:
            return base64.b64decode(content_raw).decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"Failed to decode README content: {exc}") from exc

    # Fallback: try download_url if content not base64-encoded inline
    download_url = data.get("download_url")
    if download_url:
        resp = session.get(download_url, timeout=30)
        resp.raise_for_status()
        return resp.text

    raise RuntimeError(
        f"Unexpected encoding '{encoding}' from GitHub Contents API for {AWESOME_LIST_REPO}/{AWESOME_LIST_PATH}"
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

def build_raw_record(item: dict, safety_scan: str = "unknown") -> dict | None:
    """Build a raw record from a parsed awesome-list entry.

    CRITICAL: extract_github_url(item["url"]) must succeed.
    If the URL is not a GitHub URL, returns None (skip this record).

    Args:
        item:        Parsed dict with keys: name, url, description, category.
        safety_scan: VirusTotal / scan result string ("clean", "unknown", or warning text).

    Returns:
        A raw record dict or None if the URL cannot be resolved to a GitHub repo URL.
    """
    repo_url = extract_github_url(item["url"])
    if repo_url is None:
        log.debug("Skipping non-GitHub URL: %s", item["url"])
        return None

    return {
        "repo_url": repo_url,
        "name": item["name"],
        "description": item["description"],
        "source": "clawhub",
        "raw_metadata": {
            "categories": [item["category"]] if item["category"] else [],
            "safety_scan": safety_scan,
        },
    }


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def _iter_records(items: list[dict]) -> Iterator[dict]:
    """Yield raw records built from parsed items, skipping non-GitHub entries."""
    for item in items:
        record = build_raw_record(item)
        if record is not None:
            yield record


def run(output_path: str, token: str = None, limit: int = None, resume: bool = False) -> int:
    """Run the ClawHub crawler.

    Args:
        output_path: Path to the output JSONL file.
        token:       Optional GitHub personal access token for higher rate limits.
        limit:       Stop after writing this many records (useful for testing).
        resume:      If True, skip repos already present in output_path.

    Returns:
        Number of new records written.
    """
    session = make_session(token=token)

    # --- fetch README ---
    log.info("Fetching README from %s/%s", AWESOME_LIST_REPO, AWESOME_LIST_PATH)
    readme_content = fetch_awesome_readme(session)

    # --- parse ---
    items = parse_awesome_readme(readme_content)

    # --- resume: load already-crawled URLs ---
    existing_urls: set[str] = set()
    if resume:
        existing_urls = load_existing_urls(output_path)
        log.info("Resume mode: %d URLs already in output", len(existing_urls))

    # --- build records ---
    records: list[dict] = []
    for item in items:
        record = build_raw_record(item)
        if record is None:
            continue

        # Dedup against already-written records (resume support)
        if record["repo_url"] in existing_urls:
            log.debug("Skipping already-crawled repo: %s", record["repo_url"])
            continue

        records.append(record)

        if limit is not None and len(records) >= limit:
            log.info("Reached limit of %d records; stopping.", limit)
            break

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

    # Allow token via environment variable as well
    import os
    token = args.token or os.environ.get("GITHUB_TOKEN")

    try:
        count = run(
            output_path=args.output,
            token=token,
            limit=args.limit,
            resume=args.resume,
        )
        print(f"Wrote {count} records to {args.output}", file=sys.stderr)
        return 0
    except Exception as exc:
        log.error("ClawHub crawler failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
