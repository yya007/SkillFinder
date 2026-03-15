#!/usr/bin/env bash
# scripts/trial_crawl.sh — Quick trial run of all crawlers into data/trial/
#
# Usage:
#   GITHUB_TOKEN=ghp_... ./scripts/trial_crawl.sh
#   GITHUB_TOKEN=ghp_... ./scripts/trial_crawl.sh --limit 10
#
# Output goes to data/trial/ (gitignored via data/).
# Useful for verifying new fields and crawler behaviour without a full run.

set -euo pipefail

LIMIT=${2:-5}
if [[ "${1:-}" == "--limit" ]]; then
    LIMIT=${2:-5}
fi

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    echo "Error: GITHUB_TOKEN is not set." >&2
    echo "Usage: GITHUB_TOKEN=ghp_... $0 [--limit N]" >&2
    exit 1
fi

OUTDIR="data/trial"
mkdir -p "$OUTDIR"

echo "==> Trial crawl into $OUTDIR/ (limit=$LIMIT per crawler)"
echo ""

echo "[1/3] ClawHub..."
python -m crawlers.clawhub_crawler \
    -o "$OUTDIR/clawhub.jsonl" \
    --token "$GITHUB_TOKEN" \
    --limit "$LIMIT" \
    --log-level INFO
echo ""

echo "[2/3] SkillsMP..."
python -m crawlers.skillsmp_crawler \
    -o "$OUTDIR/skillsmp.jsonl" \
    --token "$GITHUB_TOKEN" \
    --limit "$LIMIT" \
    -v 2>&1 | grep -v "^.*DEBUG.*$" || true
echo ""

echo "[3/3] SkillHub..."
python -m crawlers.skillhub_crawler \
    -o "$OUTDIR/skillhub.jsonl" \
    --token "$GITHUB_TOKEN" \
    --limit "$LIMIT" \
    --log-level INFO
echo ""

echo "==> Output files:"
ls -lh "$OUTDIR/"*.jsonl 2>/dev/null || echo "  (no files written)"
echo ""

echo "==> Record counts:"
for f in "$OUTDIR/"*.jsonl; do
    [[ -f "$f" ]] && printf "  %-30s %d records\n" "$(basename "$f")" "$(wc -l < "$f")"
done
echo ""

echo "==> Sample fields from each file:"
python - <<'PYEOF'
import json, glob, os

for path in sorted(glob.glob("data/trial/*.jsonl")):
    name = os.path.basename(path)
    lines = [l.strip() for l in open(path) if l.strip()]
    if not lines:
        print(f"  {name}: (empty)")
        continue
    r = json.loads(lines[0])
    meta = r.get("raw_metadata", {})
    print(f"  {name} — first record:")
    print(f"    name:        {r.get('name')}")
    print(f"    stars:       {meta.get('stars')}")
    print(f"    skill_md_url:{meta.get('skill_md_url') or '(none)'}")
    print(f"    platforms:   {meta.get('platforms')}")
    print()
PYEOF
