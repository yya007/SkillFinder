"""
pipeline/update_release_log.py — append the current release's stats to the log.

Maintains a per-release history of skill counts and dates so each index release
is auditable over time. Reads the just-built `data/version.txt` (date,
skill_count, embed_model, index_sha256) and `data/metadata.jsonl` (per-source
breakdown), then upserts a row keyed by release date into:

  - data/release_log.jsonl  — canonical, one JSON object per release (machine-readable)
  - docs/release-log.md      — rendered Markdown table (human-readable)

Idempotent: re-running for the same release date replaces that date's row, so
running it twice in a day (e.g. a rebuild) does not create duplicates.

Run it as the final step of an index release (after build_index.py):

    python pipeline/update_release_log.py
    python pipeline/update_release_log.py --npm-version 0.1.2   # also record the npm version

Environment: none required. No network access.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Ensure `import pipeline.*` works when run as a script (python pipeline/foo.py).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_VERSION = REPO_ROOT / "data" / "version.txt"
DEFAULT_METADATA = REPO_ROOT / "data" / "metadata.jsonl"
DEFAULT_LOG = REPO_ROOT / "data" / "release_log.jsonl"
DEFAULT_MD = REPO_ROOT / "docs" / "release-log.md"


def parse_version_txt(path: Path) -> dict:
    """Parse the `key: value` lines of version.txt into a dict."""
    out: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            out[key.strip()] = val.strip()
    return out


def source_breakdown(metadata_path: Path) -> dict[str, int]:
    """Per-source skill counts from metadata.jsonl (membership-counted).

    Reuses pipeline.update_docs.compute_stats so the breakdown matches the
    numbers rendered into README/SKILL.md. Returns {} if metadata is absent.
    """
    if not metadata_path.exists():
        return {}
    from pipeline.update_docs import compute_stats

    return compute_stats(metadata_path)["sources"]


def load_log(log_path: Path) -> list[dict]:
    """Load existing release-log records (JSONL), or [] if absent."""
    if not log_path.exists():
        return []
    records = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def upsert(records: list[dict], entry: dict) -> list[dict]:
    """Replace any existing record with the same date, else append; sort by date."""
    kept = [r for r in records if r.get("date") != entry["date"]]
    kept.append(entry)
    kept.sort(key=lambda r: r.get("date", ""))
    return kept


def render_markdown(records: list[dict]) -> str:
    """Render the release log as a Markdown table (newest first)."""
    lines = [
        "# Release log",
        "",
        "Per-release skill-count history. Maintained by "
        "`pipeline/update_release_log.py` (run as the final step of an index "
        "release). Newest first.",
        "",
        "| Date | Skills | Δ | Sources | Embed model | npm |",
        "|------|-------:|--:|---------|-------------|-----|",
    ]
    by_date = sorted(records, key=lambda r: r.get("date", ""))
    prev = None
    deltas: dict[str, str] = {}
    for r in by_date:
        cur = r.get("skill_count")
        if prev is not None and isinstance(cur, int):
            d = cur - prev
            deltas[r["date"]] = f"{d:+,}"
        else:
            deltas[r["date"]] = "—"
        if isinstance(cur, int):
            prev = cur
    for r in sorted(records, key=lambda r: r.get("date", ""), reverse=True):
        srcs = r.get("sources") or {}
        srcs_str = " · ".join(
            f"{k} {v:,}" for k, v in sorted(srcs.items(), key=lambda kv: -kv[1])
        ) if srcs else "—"
        count = r.get("skill_count")
        count_str = f"{count:,}" if isinstance(count, int) else str(count)
        lines.append(
            f"| {r.get('date', '?')} | {count_str} | {deltas.get(r.get('date'), '—')} "
            f"| {srcs_str} | {r.get('embed_model') or '—'} | {r.get('npm_version') or '—'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append the current release to the release log.")
    parser.add_argument("--version", default=str(DEFAULT_VERSION))
    parser.add_argument("--metadata", default=str(DEFAULT_METADATA))
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--md", default=str(DEFAULT_MD))
    parser.add_argument("--npm-version", default=None, help="Record the npm package version too.")
    args = parser.parse_args(argv)

    version_path = Path(args.version)
    if not version_path.exists():
        print(f"Error: {version_path} not found — run build_index.py first.", file=sys.stderr)
        return 1

    v = parse_version_txt(version_path)
    try:
        skill_count = int(v.get("skill_count", ""))
    except ValueError:
        print(f"Error: version.txt has no valid skill_count: {v}", file=sys.stderr)
        return 1

    entry = {
        "date": v.get("date", ""),
        "skill_count": skill_count,
        "sources": source_breakdown(Path(args.metadata)),
        "embed_model": v.get("embed_model") or None,
        "index_sha256": v.get("index_sha256") or None,
        "npm_version": args.npm_version,
    }
    if not entry["date"]:
        print("Error: version.txt has no date.", file=sys.stderr)
        return 1

    log_path = Path(args.log)
    records = upsert(load_log(log_path), entry)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    md_path = Path(args.md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(records), encoding="utf-8")

    print(
        f"Release log updated: {entry['date']} — {skill_count:,} skills "
        f"({len(records)} releases logged). Wrote {log_path} and {md_path}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
