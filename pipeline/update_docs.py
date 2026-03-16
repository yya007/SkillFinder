"""
pipeline/update_docs.py — Refresh auto-generated stats sections in docs.

Reads data/metadata.jsonl and data/version.txt, then replaces sentinel blocks
in README.md, SKILL.md, and docs/data-sources.md with current numbers.

Sentinel format (HTML comments, invisible in rendered Markdown):
    <!-- stats:NAME:start -->
    ...auto-generated content...
    <!-- stats:NAME:end -->

Usage:
    python pipeline/update_docs.py
    python pipeline/update_docs.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent

# Files that contain sentinel blocks
TARGET_FILES = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "skill" / "SKILL.md",
    REPO_ROOT / "docs" / "data-sources.md",
]

SENTINEL_RE = re.compile(
    r"(<!-- stats:(?P<name>[^:]+):start -->)"
    r".*?"
    r"(<!-- stats:(?P=name):end -->)",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

STAR_BUCKETS = [
    ("0",         lambda s: s == 0),
    ("1–9",       lambda s: 1 <= s <= 9),
    ("10–49",     lambda s: 10 <= s <= 49),
    ("50–99",     lambda s: 50 <= s <= 99),
    ("100–499",   lambda s: 100 <= s <= 499),
    ("500–999",   lambda s: 500 <= s <= 999),
    ("1k–5k",     lambda s: 1000 <= s <= 4999),
    ("5k+",       lambda s: s >= 5000),
]


def compute_stats(metadata_path: Path) -> dict:
    """Read metadata.jsonl and return index stats dict."""
    buckets: dict[str, int] = {label: 0 for label, _ in STAR_BUCKETS}
    source_counts: Counter = Counter()
    total = 0

    with metadata_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            total += 1
            stars = r.get("quality", {}).get("stars", 0) or 0
            for label, pred in STAR_BUCKETS:
                if pred(stars):
                    buckets[label] += 1
                    break
            for src in r.get("source", []):
                source_counts[src] += 1

    return {"total": total, "buckets": buckets, "sources": dict(source_counts)}


# ---------------------------------------------------------------------------
# Block renderers
# ---------------------------------------------------------------------------

def _bar(pct: float, width: int = 20) -> str:
    filled = max(1, round(pct / 100 * width)) if pct > 0 else 0
    return "█" * filled + "░" * (width - filled)


def render_distribution_table(stats: dict) -> str:
    total = stats["total"]
    buckets = stats["buckets"]
    lines = [
        "| Stars | Skills | Distribution |",
        "|-------|-------:|:-------------|",
    ]
    for label, count in buckets.items():
        if count == 0:
            continue
        pct = count / total * 100
        bar = _bar(pct)
        lines.append(f"| {label} | {count:,} | {bar} {pct:.0f}% |")
    lines.append(f"| **Total** | **{total:,}** | |")
    return "\n".join(lines)


def render_skill_count(stats: dict) -> str:
    total = stats["total"]
    # Round down to nearest 500 for a conservative public-facing number
    rounded = (total // 500) * 500
    return f"{rounded:,}+"


def render_source_table(stats: dict) -> str:
    sources = stats["sources"]
    total = stats["total"]
    lines = [
        "| Registry | Crawler | Skills in index |",
        "|----------|---------|----------------:|",
    ]
    crawler_map = {
        "skillsmp":    ("SkillsMP (GitHub code search)", "`skillsmp_crawler.py`"),
        "clawhub":     ("ClawHub / OpenClaw",            "`clawhub_crawler.py`"),
        "skillhub":    ("SkillHub",                      "`skillhub_crawler.py`"),
        "marketplace": ("Anthropic official marketplace", "`marketplace_crawler.py`"),
        "topic":       ("GitHub topics",                 "`topic_crawler.py`"),
    }
    for src, (name, crawler) in crawler_map.items():
        count = sources.get(src, 0)
        lines.append(f"| {name} | {crawler} | {count:,} |")
    lines.append(f"| **Total (after dedup)** | | **{total:,}** |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sentinel replacement
# ---------------------------------------------------------------------------

RENDERERS: dict[str, callable] = {
    "skill-count":         render_skill_count,
    "index-distribution":  render_distribution_table,
    "coverage-table":      render_source_table,
}


def update_file(path: Path, stats: dict, dry_run: bool = False) -> int:
    """Replace all sentinel blocks in path. Returns number of blocks updated."""
    text = path.read_text(encoding="utf-8")
    updated = 0

    def replacer(m: re.Match) -> str:
        nonlocal updated
        name = m.group("name")
        renderer = RENDERERS.get(name)
        if renderer is None:
            log.warning("%s: unknown sentinel %r — skipping", path.name, name)
            return m.group(0)
        content = renderer(stats)
        updated += 1
        # Inline sentinel: start and end tags are on the same original line
        # (no newline between start tag and end tag in the original).
        # Use compact form: <!-- start -->value<!-- end -->
        full_match = m.group(0)
        is_inline = "\n" not in full_match or full_match.count("\n") <= 1
        if is_inline:
            return f"<!-- stats:{name}:start -->{content}<!-- stats:{name}:end -->"
        return (
            f"<!-- stats:{name}:start -->\n"
            f"{content}\n"
            f"<!-- stats:{name}:end -->"
        )

    new_text = SENTINEL_RE.sub(replacer, text)

    if new_text == text:
        log.info("%s: no changes", path.name)
        return 0

    if dry_run:
        log.info("%s: would update %d sentinel(s) (dry run)", path.name, updated)
        return updated

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
    log.info("%s: updated %d sentinel(s)", path.name, updated)
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh auto-generated stats in docs from data/metadata.jsonl.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--metadata",
        default=str(REPO_ROOT / "skill" / "data" / "metadata.jsonl"),
        metavar="PATH",
        help="Path to metadata.jsonl.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)-8s %(message)s",
        stream=sys.stderr,
    )

    meta_path = Path(args.metadata)
    if not meta_path.exists():
        log.error("metadata.jsonl not found: %s", meta_path)
        return 1

    log.info("Computing stats from %s …", meta_path)
    stats = compute_stats(meta_path)
    log.info("Total: %d skills, sources: %s", stats["total"], stats["sources"])

    total_updated = 0
    for fpath in TARGET_FILES:
        if not fpath.exists():
            log.warning("File not found, skipping: %s", fpath)
            continue
        total_updated += update_file(fpath, stats, dry_run=args.dry_run)

    log.info("Done. %d sentinel block(s) updated.", total_updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
