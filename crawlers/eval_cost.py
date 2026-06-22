"""Measure the GitHub API cost of crawling a fixed sample of repos.

Run a representative set of repos through the shared fetch path and report
*metered API calls per skill*, so the effect of rate-limit optimisations can be
compared before/after across all crawlers. Hits the live API — needs GITHUB_TOKEN.

Usage:
    GITHUB_TOKEN=$(gh auth token) python -m crawlers.eval_cost
"""
from __future__ import annotations

import os
import sys

from crawlers.base import (
    fetch_repo_metadata,
    fetch_repo_metadata_cached,
    fetch_skill_md,
    fetch_skill_md_cached,
    find_skill_md_paths,
    get_api_counters,
    make_session,
    reset_api_counters,
)

# A fixed, verified sample: a large monorepo and single-skill repos that
# are confirmed to contain SKILL.md files.
SAMPLE_REPOS = [
    "anthropics/skills",
    "dnakov/claude-skills",
    "obra/superpowers",
]


def measure(session, repos: list[str]) -> dict:
    """Run repos through the fetch path under fresh counters; return a summary."""
    reset_api_counters()
    skills = 0
    for repo in repos:
        meta = fetch_repo_metadata(session, repo)
        paths = find_skill_md_paths(session, repo)
        for path in paths:
            content = fetch_skill_md(
                session, repo, path, meta.get("default_branch", "main")
            )
            if content:
                skills += 1
    counters = get_api_counters()
    metered = counters["rest"] + counters["search"]
    free = counters["raw_free"] + counters["conditional_304"]
    return {
        "skills": skills,
        "metered": metered,
        "free": free,
        "per_skill": round(metered / skills, 2) if skills else 0.0,
        "counters": counters,
    }


def measure_cached(session, repos: list[str], meta_cache: dict, content_cache: dict) -> dict:
    """Run repos through the cache-aware fetch path; return a summary.

    Uses fetch_repo_metadata_cached and fetch_skill_md_cached so a warm run
    reflects 304/blob-SHA savings.  Returns the same dict shape as measure().
    """
    reset_api_counters()
    skills = 0
    for repo in repos:
        meta = fetch_repo_metadata_cached(session, repo, meta_cache)
        paths = find_skill_md_paths(session, repo)
        for path, blob_sha in paths.items():
            content = fetch_skill_md_cached(
                session,
                repo,
                path,
                blob_sha,
                meta.get("default_branch", "main"),
                content_cache,
            )
            if content:
                skills += 1
    counters = get_api_counters()
    metered = counters["rest"] + counters["search"]
    free = counters["raw_free"] + counters["conditional_304"]
    return {
        "skills": skills,
        "metered": metered,
        "free": free,
        "per_skill": round(metered / skills, 2) if skills else 0.0,
        "counters": counters,
    }


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Set GITHUB_TOKEN (e.g. GITHUB_TOKEN=$(gh auth token))", file=sys.stderr)
        return 1
    session = make_session(token)
    result = measure(session, SAMPLE_REPOS)
    print(
        f"skills={result['skills']}  metered={result['metered']}  "
        f"free={result['free']}  per_skill={result['per_skill']}\n"
        f"counters={result['counters']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
