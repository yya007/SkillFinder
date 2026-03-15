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


def get_previous_skill_count() -> int | None:
    """Read the skill count from the most recent GitHub Release body.

    Returns None if no previous release exists or the count cannot be parsed.
    """
    try:
        result = subprocess.run(
            ["gh", "release", "view", "--json", "body", "--repo", REPO],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None

    try:
        body = json.loads(result.stdout).get("body", "")
    except (json.JSONDecodeError, AttributeError):
        return None

    # Match "**Skills indexed:** 14,823" or "Skills indexed: 14823"
    m = re.search(r"Skills indexed[^0-9]*([0-9,]+)", body)
    if not m:
        return None

    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


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
