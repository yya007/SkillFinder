"""
pipeline/normalize.py — Normalize and deduplicate raw crawler records.

Steps:
  1. Load all raw JSONL files from crawlers (skillsmp, clawhub, skillhub, marketplace).
  2. Compute canonical URL keys and group records by them.
  3. Merge metadata using priority rules.
  4. Apply quality filter (drop low-quality records without description or signals).
  5. Build embedding_text and assign stable SHA-256 IDs.
  6. Write unified JSONL output; raise QualityGateError if count < min_skills.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CURATED_SOURCES: set[str] = {"clawhub", "skillhub", "marketplace"}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class QualityGateError(Exception):
    """Raised when the normalized output contains fewer skills than min_skills."""


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

# Known GitHub owner/repo renames — kept in sync with crawlers/base.py
# so that old and new URLs produce the same canonical dedup key.
_GITHUB_REDIRECTS: dict[str, str] = {
    # Specific repo renames must come before general org renames
    "github.com/clawdbot/clawdbot": "github.com/openclaw/openclaw",
    "github.com/moltbot/moltbot": "github.com/openclaw/openclaw",
    # General org renames
    "github.com/clawdbot/": "github.com/openclaw/",
    "github.com/moltbot/": "github.com/openclaw/",
}


def canonical_key(repo_url: str) -> str:
    """Return a stable dedup key for a repo URL.

    Normalisation steps (order matters):
      1. Raise ValueError / TypeError for empty or None input.
      2. Lowercase the URL.
      3. Strip trailing slashes.
      4. Remove a trailing ``.git`` suffix.
      5. Apply known GitHub owner/repo renames.

    Examples::

        canonical_key("https://GitHub.COM/User/Repo/")
        # → "https://github.com/user/repo"
    """
    if repo_url is None:
        raise TypeError("repo_url must not be None")
    if not repo_url:
        raise ValueError("repo_url must not be empty")

    url = repo_url.lower().rstrip("/").removesuffix(".git")
    for old, new in _GITHUB_REDIRECTS.items():
        if old in url:
            url = url.replace(old, new, 1)
            break
    return url


def skill_id(repo_url: str) -> str:
    """Return a stable 64-character hex SHA-256 identifier for a skill."""
    return hashlib.sha256(canonical_key(repo_url).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Embedding text construction
# ---------------------------------------------------------------------------

def build_embedding_text(skill: dict) -> str:
    """Build the text passage used for embedding a skill record.

    Format::

        "{name}. {description} Categories: {cat1, cat2}. Platforms: {p1, p2}. Triggers: {t1, t2}."

    Platforms and triggers are included to improve recall for queries like
    "claude code skill for X" or trigger-phrase matches.

    Raises:
        ValueError: if name or description are missing/empty.
    """
    name = skill.get("name", "")
    description = skill.get("description", "")

    if not name:
        raise ValueError("skill must have a non-empty 'name'")
    if not description:
        raise ValueError("skill must have a non-empty 'description'")

    categories = skill.get("categories", [])
    platforms = skill.get("platforms", [])
    triggers = skill.get("triggers", [])

    parts = [f"{name}. {description}"]
    if categories:
        parts.append(f"Categories: {', '.join(categories)}.")
    if platforms:
        parts.append(f"Platforms: {', '.join(platforms)}.")
    if triggers:
        parts.append(f"Triggers: {', '.join(triggers[:5])}.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Record merging
# ---------------------------------------------------------------------------

def merge_records(records: list[dict], dedup_key: str | None = None) -> dict:
    """Merge multiple raw records for the same canonical URL into one unified record.

    Priority rules:
    - ``source``: collect all unique source names as a list.
    - ``stars``: take the maximum value across records.
    - ``pushed_at`` / ``last_updated``: take the latest (lexicographic max of ISO dates).
    - ``skillhub_rank`` / ``skillhub_score``: take from the ``skillhub`` record.
    - ``categories``: union of all values across records.
    - ``description``: prefer the longest non-empty description.
    - ``name``: take from the first record with a non-empty name.
    - ``repo_url``: use the canonical form.

    Args:
        records:   One or more raw crawler records sharing the same dedup key.
        dedup_key: The canonical key used to group these records. When provided
                   (e.g. a ``skill_md_url`` for monorepo skills), it is used
                   as the basis for the stable SHA-256 ``id`` so that two skills
                   in the same monorepo never collide.  Defaults to
                   ``canonical_key(records[0]["repo_url"])``.

    Raises:
        ValueError: if records is empty.
    """
    if not records:
        raise ValueError("records must not be empty")

    # Use canonical URL from the first record (all should share the same key)
    canon_url = canonical_key(records[0]["repo_url"])
    # Stable ID key: prefer the caller-supplied dedup_key (e.g. skill_md_url for
    # monorepo skills) so that every skill in a monorepo gets a unique ID.
    id_key = dedup_key if dedup_key is not None else canon_url

    # Collect sources (preserve insertion order, deduplicate)
    sources_seen: list[str] = []
    sources_set: set[str] = set()

    stars = 0
    last_updated: str | None = None
    skillhub_rank: str | None = None
    skillhub_score: float | None = None
    categories: set[str] = set()
    triggers: list[str] = []
    description = ""
    name = ""
    platforms: set[str] = set()
    skill_md_url: str = ""
    safety_scan: bool | None = None
    safety_scan_date: str | None = None
    is_official = False  # True only for anthropics/* marketplace skills

    for rec in records:
        src = rec.get("source", "")
        if src and src not in sources_set:
            sources_seen.append(src)
            sources_set.add(src)

        if not name:
            name = rec.get("name", "")

        # Prefer longest non-empty description
        rec_desc = rec.get("description", "") or ""
        if len(rec_desc) > len(description):
            description = rec_desc

        meta: dict[str, Any] = rec.get("raw_metadata", {}) or {}

        # Stars — take maximum
        rec_stars = meta.get("stars")
        if rec_stars is not None and rec_stars > stars:
            stars = rec_stars

        # Last updated — take latest ISO date string
        rec_pushed = meta.get("pushed_at")
        if rec_pushed:
            if last_updated is None or rec_pushed > last_updated:
                last_updated = rec_pushed

        # Categories — union across all records
        for cat in meta.get("categories", []):
            if cat:
                categories.add(cat)
        for topic in meta.get("topics", []):
            if topic:
                categories.add(topic)

        # Triggers — collect from any source, deduplicate while preserving order
        for trigger in meta.get("triggers", []):
            if trigger and trigger not in triggers:
                triggers.append(trigger)

        # Official marketplace flag — set if any marketplace record is from anthropics/*
        if src == "marketplace" and meta.get("official"):
            is_official = True

        # Safety scan — prefer clawhub
        if src == "clawhub" and meta.get("safety_scan") is not None:
            safety_scan = meta["safety_scan"]
            if safety_scan_date is None:
                safety_scan_date = meta.get("safety_scan_date")

        # SkillHub rank and score
        if src == "skillhub":
            if meta.get("rank") is not None:
                skillhub_rank = meta["rank"]
            if meta.get("overall_score") is not None:
                skillhub_score = meta["overall_score"]

        # Platforms — union across all records
        for plat in meta.get("platforms", []):
            if plat:
                platforms.add(plat)

        # skill_md_url — take first non-empty value
        if not skill_md_url:
            candidate = meta.get("skill_md_url", "")
            if candidate:
                skill_md_url = candidate

    return {
        "id": skill_id(id_key),
        "repo_url": canon_url,
        "name": name,
        "description": description,
        "source": sources_seen,
        "categories": sorted(categories),
        "triggers": triggers,
        "platforms": sorted(platforms),
        "skill_md_url": skill_md_url,
        "safety_scan": safety_scan,
        "safety_scan_date": safety_scan_date,
        "is_official": is_official,
        "install_cmd": {},          # populated separately by build_install_cmds
        "quality": {
            "stars": stars,
            "skillhub_rank": skillhub_rank,
            "skillhub_score": skillhub_score,
            "last_updated": last_updated,
        },
        "embedding_text": "",       # populated after merge by build_embedding_text
    }


# ---------------------------------------------------------------------------
# Install command generation
# ---------------------------------------------------------------------------

def build_install_cmds(merged: dict) -> dict[str, str]:
    """Generate platform → install command mapping from merged source list.

    Rules (later sources can overwrite earlier for the same platform key):
    - ``skillsmp``             → ``claude_code: /plugin install {name}``
    - ``clawhub``              → ``openclaw: clawhub install {name}`` only.
                                  No claude_code command: clawhub is the OpenClaw registry;
                                  /plugin install pulls from SkillsMP and would silently fail.
    - ``marketplace`` official → ``claude_code: /skill install {name}``
                                  (only anthropics/* repos; these are registered in the
                                  Anthropic official marketplace)
    - ``marketplace`` community→ ``claude_code: /plugin install {name}``
                                  (community repos like alirezarezvani/claude-skills are not
                                  registered in the official marketplace; /skill install fails)
    - ``skillhub``             → (metadata only — no install command)
    """
    name = merged["name"]
    sources: list[str] = merged.get("source", [])
    is_official: bool = merged.get("is_official", False)
    cmds: dict[str, str] = {}

    for src in sources:
        if src == "skillsmp":
            cmds["claude_code"] = f"/plugin install {name}"
        elif src == "clawhub":
            cmds["openclaw"] = f"clawhub install {name}"
        elif src == "marketplace":
            if is_official:
                cmds["claude_code"] = f"/skill install {name}"
            else:
                cmds["claude_code"] = f"/plugin install {name}"
        elif src == "topic":
            cmds["claude_code"] = f"/plugin install {name}"
        # skillhub: no install command

    if "codex" in merged.get("platforms", []):
        cmds["codex"] = "cp SKILL.md ~/.codex/skills/"

    return cmds


# ---------------------------------------------------------------------------
# Quality filter
# ---------------------------------------------------------------------------

def passes_quality_filter(skill: dict, min_stars: int = 10) -> bool:
    """Return True if the merged skill record passes the quality bar.

    A skill passes if ALL of the following hold:
    1. ``quality.stars`` >= min_stars  (default 10)
    2. Non-empty description (always required)
    """
    description = skill.get("description", "")
    if not description:
        return False

    quality = skill.get("quality", {})
    stars = quality.get("stars", 0) or 0

    return stars >= min_stars


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def normalize(
    raw_paths: list[str],
    output_path: str,
    min_skills: int = 1,
    min_stars: int = 10,
) -> int:
    """Run the full normalization pipeline.

    Args:
        raw_paths:   Paths to raw crawler JSONL files.
        output_path: Where to write the unified JSONL output.
        min_skills:  Minimum number of output records; raise QualityGateError
                     if the count is below this threshold.
        min_stars:   Minimum GitHub star count for a skill to pass the quality
                     filter (default 10).

    Returns:
        Number of records written to output_path.

    Raises:
        ValueError:        if an input file is mostly malformed JSON (> 5 bad
                           lines and > 10%), indicating corruption rather than a
                           clipped tail. Isolated bad lines, and inputs that do
                           not exist, are skipped with a warning.
        QualityGateError:  if the output record count is below min_skills.
    """
    # ------------------------------------------------------------------ load
    # key: canonical URL  →  value: list of raw records sharing that URL
    groups: dict[str, list[dict]] = {}
    total_raw = 0

    for path in raw_paths:
        p = Path(path)
        if not p.exists():
            # A crawler killed mid-run (the topic and clawhub crawlers write
            # their output only once, at the end) leaves its file unwritten.
            # Skip it rather than aborting — same best-effort contract as below.
            # If *every* input is missing the >=min_skills quality gate fails.
            logger.warning("Input file not found, skipping: %s", path)
            continue
        # Tolerate malformed lines rather than aborting the whole run. In CI
        # each crawler is wrapped in `timeout`; a SIGTERM mid-write leaves a
        # truncated final line, and the pipeline is designed to "proceed with
        # whatever data was written" (the >=8,000 quality gate is the backstop
        # for wholesale failure). We still fail loudly if a file is *mostly*
        # unparseable, which signals real corruption rather than a clipped tail.
        file_lines = 0
        file_malformed = 0
        with p.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                file_lines += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    file_malformed += 1
                    logger.warning(
                        "Skipping malformed JSON in %s:%d: %s", path, lineno, exc
                    )
                    continue
                # Skip tombstone records
                if record.get("tombstone"):
                    logger.debug("Skipping tombstone record: %s", record.get("skill_md_url", record.get("repo_url", "")))
                    continue
                total_raw += 1
                repo_url = record.get("repo_url", "")
                if not repo_url:
                    logger.warning("Skipping record at %s:%d: missing repo_url", path, lineno)
                    continue
                # For monorepo sources (ClawHub, Marketplace) every skill shares
                # the same repo_url but has a unique skill_md_url.  Use
                # skill_md_url as the dedup key when available so each skill
                # becomes its own record rather than collapsing into one.
                skill_md_url = record.get("raw_metadata", {}).get("skill_md_url", "")
                key = canonical_key(skill_md_url) if skill_md_url else canonical_key(repo_url)
                groups.setdefault(key, []).append(record)

        if file_malformed:
            # A clipped tail is a line or two; a high ratio means the file is
            # corrupt (bad encoding, half-flushed buffer) and should not pass
            # silently as a healthy-but-small crawl.
            ratio = file_malformed / file_lines if file_lines else 1.0
            logger.warning(
                "%s: skipped %d/%d malformed lines (%.1f%%)",
                path, file_malformed, file_lines, ratio * 100,
            )
            # Require both an absolute floor and a high ratio: a couple of bad
            # lines in a short crawl is still just a clipped tail, not corruption.
            if file_malformed > 5 and ratio > 0.10:
                raise ValueError(
                    f"{path}: {file_malformed}/{file_lines} lines "
                    f"({ratio * 100:.1f}%) are malformed JSON — file looks "
                    f"corrupt, not just truncated"
                )

    # ----------------------------------------------------------------- merge
    output_records: list[dict] = []
    dropped_no_desc: dict[str, int] = defaultdict(int)
    dropped_low_stars_zero: dict[str, int] = defaultdict(int)
    dropped_low_stars_low: dict[str, int] = defaultdict(int)

    for group_key, recs in groups.items():
        merged = merge_records(recs, dedup_key=group_key)

        # Quality gate: drop records that don't meet the bar, collecting stats
        _desc = merged.get("description", "")
        _stars = (merged.get("quality", {}) or {}).get("stars", 0) or 0
        _srcs = merged.get("source", []) or ["unknown"]
        if not _desc:
            for _s in _srcs:
                dropped_no_desc[_s] += 1
            continue
        if _stars < min_stars:
            for _s in _srcs:
                if _stars == 0:
                    dropped_low_stars_zero[_s] += 1
                else:
                    dropped_low_stars_low[_s] += 1
            continue

        # Build install commands, then discard is_official (pipeline-only flag)
        merged["install_cmd"] = build_install_cmds(merged)
        merged.pop("is_official", None)

        # Build embedding text (skip records that still lack name/description
        # after merging — shouldn't happen after quality filter, but be safe)
        try:
            merged["embedding_text"] = build_embedding_text(merged)
        except ValueError:
            continue

        output_records.append(merged)

    # --------------------------------------------------------------- quality stats
    _total_no_desc = sum(dropped_no_desc.values())
    _total_ls_zero = sum(dropped_low_stars_zero.values())
    _total_ls_low = sum(dropped_low_stars_low.values())
    logger.info("Normalize quality stats:")
    logger.info("  Raw records loaded: %d (across %d files)", total_raw, len(raw_paths))
    logger.info(
        "  Dedup: %d raw -> %d unique groups (%d dupes removed)",
        total_raw, len(groups), max(0, total_raw - len(groups)),
    )
    logger.info(
        "  Dropped (no description): %d -- %s",
        _total_no_desc, dict(dropped_no_desc),
    )
    logger.info(
        "  Dropped (low stars, 0-star): %d -- %s",
        _total_ls_zero, dict(dropped_low_stars_zero),
    )
    logger.info(
        "  Dropped (low stars, 1-%d): %d -- %s",
        min_stars - 1, _total_ls_low, dict(dropped_low_stars_low),
    )
    logger.info("  Passed quality filter: %d", len(output_records))

    # --------------------------------------------------------------- quality gate
    count = len(output_records)
    if count < min_skills:
        raise QualityGateError(
            f"Quality gate failed: produced {count} skills, "
            f"but min_skills={min_skills} required."
        )

    # ----------------------------------------------------------------- write
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for record in output_records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    return count


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Normalize raw crawler JSONL files into a unified skill index."
    )
    parser.add_argument(
        "raw_paths",
        nargs="+",
        metavar="RAW_FILE",
        help="One or more raw crawler JSONL files.",
    )
    parser.add_argument(
        "-o", "--output",
        default="data/unified_skills.jsonl",
        metavar="OUTPUT",
        help="Output path for unified JSONL (default: data/unified_skills.jsonl).",
    )
    parser.add_argument(
        "--min-skills",
        type=int,
        default=1,
        metavar="N",
        help="Minimum number of output records before raising an error (default: 1).",
    )
    parser.add_argument(
        "--min-stars",
        type=int,
        default=10,
        metavar="N",
        help="Minimum GitHub star count to pass quality filter (default: 10).",
    )
    args = parser.parse_args()

    try:
        count = normalize(args.raw_paths, args.output, min_skills=args.min_skills, min_stars=args.min_stars)
        print(f"Wrote {count} skills to {args.output}")
    except (FileNotFoundError, QualityGateError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
