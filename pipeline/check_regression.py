"""
pipeline/check_regression.py — Quality gate: detect skill count regressions.

Compares the current normalized skill count against the previous GitHub Release.
Exits non-zero if the count has dropped more than *threshold* (default 20%),
stopping the CI pipeline before a degraded release is published.

Usage:
    python pipeline/check_regression.py
    python pipeline/check_regression.py --threshold 0.15   # 15% max drop

Environment:
    GH_TOKEN or GITHUB_TOKEN: used by the `gh` CLI for the release API.
    GITHUB_REPOSITORY: repo slug (default: yya007/skill-finder).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.environ.get("GITHUB_REPOSITORY", "yya007/skill-finder")


def latest_index_tag(tags: list[str]) -> str | None:
    """Return the newest ``index-*`` release tag, or None if there is none.

    The repo's Releases list mixes data releases (``index-YYYYMMDD``) with npm
    releases (``v0.1.1``). Only the former carry a skill count, so npm tags must
    be ignored even when they are more recent. ``index-YYYYMMDD`` sorts
    lexicographically in chronological order, so a plain max() picks the newest.
    """
    index_tags = [t for t in tags if t.startswith("index-")]
    return max(index_tags) if index_tags else None


def parse_skill_count(body: str) -> int | None:
    """Extract the skill count from a release body, or None if absent."""
    # Match "**Skills indexed:** 14,823" or "Skills indexed: 14823"
    m = re.search(r"Skills indexed[^0-9]*([0-9,]+)", body)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _gh_json(args: list[str]):
    """Run a `gh` command expecting JSON on stdout; return parsed JSON or None."""
    try:
        result = subprocess.run(
            ["gh", *args, "--repo", REPO],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def get_previous_skill_count() -> int | None:
    """Read the skill count from the most recent *index* GitHub Release body.

    Scoped to ``index-*`` releases so npm releases (``v0.1.x``), which have no
    skill count, can never be mistaken for the previous index. Returns None if
    no index release exists or the count cannot be parsed.
    """
    releases = _gh_json(["release", "list", "--json", "tagName"])
    if not releases:
        return None
    tag = latest_index_tag([r.get("tagName", "") for r in releases])
    if tag is None:
        return None

    view = _gh_json(["release", "view", tag, "--json", "body"])
    if not view:
        return None
    return parse_skill_count(view.get("body", "") or "")


def check_regression(
    current_path: str = "data/unified_skills.jsonl",
    threshold: float = 0.20,
) -> tuple[bool, str]:
    """Check for a regression in skill count.

    Returns:
        (passed: bool, message: str)
    """
    try:
        with open(current_path) as f:
            current_count = sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return False, f"Skill file not found: {current_path}"

    prev_count = get_previous_skill_count()

    if prev_count is None:
        return (
            True,
            f"No previous release found — skipping regression check. Current: {current_count}.",
        )

    drop = (prev_count - current_count) / max(prev_count, 1)

    if drop > threshold:
        return (
            False,
            f"FAIL: Skill count dropped {drop * 100:.1f}% "
            f"({prev_count} → {current_count}, threshold: {threshold * 100:.0f}%). "
            "Aborting release to protect users from a degraded index.",
        )

    return (
        True,
        f"OK: {current_count} skills (prev: {prev_count}, delta: {drop * 100:+.1f}%).",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quality gate: fail if skill count dropped more than threshold."
    )
    parser.add_argument(
        "--current",
        default="data/unified_skills.jsonl",
        help="Path to the newly normalized skills JSONL.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.20,
        help="Maximum allowed drop fraction (default: 0.20 = 20%%).",
    )
    args = parser.parse_args(argv)

    passed, message = check_regression(args.current, args.threshold)
    print(message)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
