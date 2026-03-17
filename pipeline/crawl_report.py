"""pipeline/crawl_report.py — Raw-data quality check for crawled JSONL files.

Reads data/raw/*.jsonl and prints a per-source quality report with:
- Record count, missing-description rate, 0-star rate, median and P95 stars
- Star distribution histogram across all sources combined
- [WARN] summary lines for sources exceeding lenient thresholds

Always exits 0 — warnings are informational and never block the pipeline.

Usage:
    python pipeline/crawl_report.py [--data-dir data/raw]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Thresholds (lenient — informational only)
# ---------------------------------------------------------------------------
WARN_NO_DESC_PCT = 5.0   # warn if >5% records in a source lack description
WARN_ZERO_STARS_PCT = 15.0  # warn if >15% records in a source have 0 stars

# ---------------------------------------------------------------------------
# Star distribution buckets
# ---------------------------------------------------------------------------
BUCKETS: list[tuple[str, int, int]] = [
    ("0",        0,    0),
    ("1-9",      1,    9),
    ("10-49",   10,   49),
    ("50-499",  50,  499),
    ("500-4999", 500, 4999),
    ("5000+",  5000, 10**9),
]


def _fmt_stars(n: int) -> str:
    """Format a star count for display (e.g. 98000 -> '98k')."""
    if n >= 10_000:
        return f"{n // 1000}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _percentile(sorted_vals: list[int], p: float) -> int:
    """Return the p-th percentile of a sorted list (0–100)."""
    if not sorted_vals:
        return 0
    idx = min(int(len(sorted_vals) * p / 100), len(sorted_vals) - 1)
    return sorted_vals[idx]


def _median(sorted_vals: list[int]) -> int:
    if not sorted_vals:
        return 0
    mid = len(sorted_vals) // 2
    if len(sorted_vals) % 2 == 1:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) // 2


def load_raw(data_dir: str) -> dict[str, list[dict]]:
    """Load all *.jsonl files under data_dir, keyed by source name."""
    records_by_source: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(Path(data_dir).glob("*.jsonl")):
        source_name = path.stem  # e.g. "clawhub", "marketplace"
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Skip tombstone records
                if rec.get("tombstone"):
                    continue
                # Use source field if present, fall back to filename stem
                src = rec.get("source") or source_name
                records_by_source[src].append(rec)
    return records_by_source


def compute_stats(records: list[dict]) -> dict:
    """Compute quality stats for a list of raw records."""
    total = len(records)
    no_desc = 0
    zero_stars = 0
    star_values: list[int] = []

    for rec in records:
        desc = rec.get("description", "") or ""
        if isinstance(desc, list):
            desc = " ".join(str(d) for d in desc)
        if not str(desc).strip():
            no_desc += 1
        meta = rec.get("raw_metadata", {}) or {}
        stars = meta.get("stars") or 0
        try:
            stars = int(stars)
        except (TypeError, ValueError):
            stars = 0
        if stars == 0:
            zero_stars += 1
        star_values.append(stars)

    star_values.sort()
    return {
        "total": total,
        "no_desc": no_desc,
        "no_desc_pct": (no_desc / total * 100) if total else 0.0,
        "zero_stars": zero_stars,
        "zero_stars_pct": (zero_stars / total * 100) if total else 0.0,
        "median_stars": _median(star_values),
        "p95_stars": _percentile(star_values, 95),
        "star_values": star_values,
    }


def print_report(records_by_source: dict[str, list[dict]]) -> list[str]:
    """Print the quality report table and histogram.  Returns [WARN] lines."""
    source_order = sorted(records_by_source.keys())
    all_stats: dict[str, dict] = {src: compute_stats(records_by_source[src]) for src in source_order}

    # ------------------------------------------------------------------
    # Per-source table
    # ------------------------------------------------------------------
    col_source = max((len(s) for s in source_order), default=6)
    col_source = max(col_source, 6)
    header = (
        f"{'Source':<{col_source}}  {'Records':>8}  {'No-Desc':>8}  {'%No-Desc':>9}"
        f"  {'0-Stars':>8}  {'%0-Stars':>9}  {'Median★':>8}  {'P95★':>6}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)

    total_records = 0
    total_no_desc = 0
    total_zero_stars = 0
    all_star_values: list[int] = []

    for src in source_order:
        s = all_stats[src]
        total_records += s["total"]
        total_no_desc += s["no_desc"]
        total_zero_stars += s["zero_stars"]
        all_star_values.extend(s["star_values"])

        no_desc_flag = " ⚠" if s["no_desc_pct"] > WARN_NO_DESC_PCT else ""
        zero_flag = " ⚠" if s["zero_stars_pct"] > WARN_ZERO_STARS_PCT else ""

        print(
            f"{src:<{col_source}}  {s['total']:>8,}  {s['no_desc']:>8,}"
            f"  {s['no_desc_pct']:>7.1f}%{no_desc_flag:<2}"
            f"  {s['zero_stars']:>8,}  {s['zero_stars_pct']:>7.1f}%{zero_flag:<2}"
            f"  {_fmt_stars(s['median_stars']):>8}  {_fmt_stars(s['p95_stars']):>6}"
        )

    # Totals row
    if source_order:
        print(sep)
        all_star_values.sort()
        total_no_desc_pct = (total_no_desc / total_records * 100) if total_records else 0.0
        total_zero_pct = (total_zero_stars / total_records * 100) if total_records else 0.0
        print(
            f"{'TOTAL':<{col_source}}  {total_records:>8,}  {total_no_desc:>8,}"
            f"  {total_no_desc_pct:>7.1f}%   "
            f"  {total_zero_stars:>8,}  {total_zero_pct:>7.1f}%   "
            f"  {'':>8}  {'':>6}"
        )

    # ------------------------------------------------------------------
    # Star distribution histogram (all sources combined)
    # ------------------------------------------------------------------
    print()
    print("Star distribution (all sources combined):")
    bucket_counts: list[tuple[str, int]] = []
    for label, lo, hi in BUCKETS:
        count = sum(1 for v in all_star_values if lo <= v <= hi)
        bucket_counts.append((label, count))

    max_count = max((c for _, c in bucket_counts), default=1) or 1
    bar_width = 30
    for label, count in bucket_counts:
        bar_len = int(count / max_count * bar_width)
        bar = "█" * bar_len
        pct = (count / total_records * 100) if total_records else 0.0
        print(f"  {label:<10} {bar:<{bar_width}}  {count:>7,}  ({pct:.1f}%)")

    # ------------------------------------------------------------------
    # [WARN] summary
    # ------------------------------------------------------------------
    warn_lines: list[str] = []
    for src in source_order:
        s = all_stats[src]
        if s["no_desc_pct"] > WARN_NO_DESC_PCT:
            warn_lines.append(
                f"[WARN] {src}: {s['no_desc_pct']:.1f}% records missing description"
                f" ({s['no_desc']:,} / {s['total']:,})"
            )
        if s["zero_stars_pct"] > WARN_ZERO_STARS_PCT:
            warn_lines.append(
                f"[WARN] {src}: {s['zero_stars_pct']:.1f}% records have 0 stars"
                f" ({s['zero_stars']:,} / {s['total']:,})"
            )

    if warn_lines:
        print()
        for line in warn_lines:
            print(line)

    return warn_lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Print a quality report for raw crawler JSONL files.",
    )
    parser.add_argument(
        "--data-dir",
        default="data/raw",
        metavar="DIR",
        help="Directory containing raw *.jsonl files (default: data/raw)",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"[ERROR] data directory not found: {data_dir}", file=sys.stderr)
        return 0  # Always exit 0

    records_by_source = load_raw(str(data_dir))
    if not records_by_source:
        print(f"No records found in {data_dir}")
        return 0

    print_report(records_by_source)
    return 0  # Always exit 0 — warnings are informational only


if __name__ == "__main__":
    sys.exit(main())
