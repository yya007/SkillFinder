# PRD-005: CI/CD & Release Automation

**Status:** Planned
**Phase:** 5 of 5
**Depends on:** PRD-001, PRD-002 (pipeline scripts work), PRD-004 (release artifact defined)
**Blocks:** Nothing (final phase)

---

## Problem

The pre-built FAISS index is the core value delivery of SkillFinder. Without an automated pipeline that keeps it fresh, the index goes stale and search quality degrades. We need a zero-touch weekly rebuild that runs crawlers, embeds, builds the index, and publishes a GitHub Release that `update_index.py` can download.

## Goals

- Weekly automated rebuild (Monday 6AM UTC) via GitHub Actions
- Manual trigger (`workflow_dispatch`) for on-demand rebuilds
- Publish release artifacts to GitHub Releases with SHA256 checksums
- Alert on build failure (GitHub Actions notification)
- Full pipeline runs in under 90 minutes on `ubuntu-latest`
- Data quality gates: fail the release if skill count drops > 20% from previous

## Non-Goals

- Real-time or incremental updates (weekly is sufficient for v1)
- Multi-region CDN distribution (GitHub Releases is the CDN)
- Automated rollback to previous index on quality regression (manual rollback only for v1)

---

## Functional Requirements

### F1 — Weekly Crawl & Index Job

**Trigger:** `schedule: cron: '0 6 * * 1'` (Monday 6AM UTC) and `workflow_dispatch`

**Steps:**

1. **Checkout** repo
2. **Setup Python 3.11**
3. **Install dependencies** (`pip install -r requirements-ci.txt`)
4. **Crawl all sources** (parallel where possible):
   - `skillsmp_crawler.py` (longest, ~30min)
   - `clawhub_crawler.py` (~5min)
   - `skillhub_crawler.py` (~10min)
   - `marketplace_crawler.py` (~5min)
5. **Normalize** (`pipeline/normalize.py`)
6. **Quality gate — pre-embed**: assert `unified_skills.jsonl` has ≥ 8,000 records; fail fast if not
7. **Start Ollama + pull model**
8. **Embed** (`pipeline/embed.py`) via local Ollama
9. **Build index** (`pipeline/build_index.py`)
10. **Quality gate — post-build**: assert index skill count is within 80% of previous release count
11. **Package** (`tar -czf skill-finder-index-$(date +%Y%m%d).tar.gz data/index.faiss data/metadata.jsonl data/version.txt`)
11. **Publish GitHub Release** with release body including:
    - Skill count
    - SHA256 of the artifact
    - Source breakdown (N from SkillsMP, N from ClawHub, etc.)
    - Build timestamp

### F2 — Secrets Required

| Secret | Used by | Value |
|--------|---------|-------|
| `GITHUB_TOKEN` | All crawlers using GitHub API | Auto-provided by Actions |

No other secrets are needed. Embedding runs locally via Ollama in CI.

### F3 — Quality Gates

**Gate 1** (after normalize): `len(unified_skills) >= 8000`
- If fail: post failure annotation, do not proceed to embed

**Gate 2** (after build): current index count ≥ 80% of previous release count
- Previous count is read from the latest GitHub Release body
- If fail: post failure annotation with diff; stop release; leave previous release intact

Both gates run as Python assertions in the pipeline scripts, returning non-zero on failure.

### F4 — Parallelization

The four crawlers are independent and can run in parallel using GitHub Actions job matrix or background processes:

```yaml
- name: Crawl all sources (parallel)
  run: |
    python crawlers/skillsmp_crawler.py -o data/raw/skillsmp.jsonl &
    python crawlers/clawhub_crawler.py -o data/raw/clawhub.jsonl &
    python crawlers/skillhub_crawler.py -o data/raw/skillhub.jsonl &
    python crawlers/marketplace_crawler.py -o data/raw/marketplace.jsonl &
    wait
```

Note: SkillsMP crawler is I/O-bound (GitHub API rate limit), not CPU-bound, so parallelism helps.

---

## GitHub Actions Workflow

```yaml
# .github/workflows/update-index.yml
name: Update Skill Index

on:
  schedule:
    - cron: '0 6 * * 1'
  workflow_dispatch:
    inputs:
      force_rebuild:
        description: 'Force rebuild even if no new data'
        type: boolean
        default: false

jobs:
  build-index:
    runs-on: ubuntu-latest
    timeout-minutes: 90

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -r requirements-ci.txt

      - name: Crawl all sources (parallel)
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          mkdir -p data/raw
          python crawlers/skillsmp_crawler.py -o data/raw/skillsmp.jsonl &
          python crawlers/clawhub_crawler.py -o data/raw/clawhub.jsonl &
          python crawlers/skillhub_crawler.py -o data/raw/skillhub.jsonl &
          python crawlers/marketplace_crawler.py -o data/raw/marketplace.jsonl &
          wait

      - name: Normalize and quality gate
        run: |
          python pipeline/normalize.py -o data/unified_skills.jsonl
          COUNT=$(wc -l < data/unified_skills.jsonl)
          echo "Unified skill count: $COUNT"
          [ "$COUNT" -ge 8000 ] || (echo "Quality gate failed: only $COUNT skills" && exit 1)

      - name: Install Ollama and pull model
        run: |
          curl -fsSL https://ollama.com/install.sh | sh
          ollama serve &
          sleep 5
          ollama pull qwen3-embedding:0.6b

      - name: Embed (Qwen3-Embedding-0.6B via Ollama)
        run: python pipeline/embed.py

      - name: Build FAISS index
        run: python pipeline/build_index.py

      - name: Quality gate — count regression check
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: python pipeline/check_regression.py

      - name: Package artifact
        run: |
          DATE=$(date +%Y%m%d)
          tar -czf skill-finder-index-${DATE}.tar.gz \
            data/index.faiss data/metadata.jsonl data/version.txt
          SHA=$(sha256sum skill-finder-index-${DATE}.tar.gz | awk '{print $1}')
          echo "ARTIFACT=skill-finder-index-${DATE}.tar.gz" >> $GITHUB_ENV
          echo "SHA256=$SHA" >> $GITHUB_ENV
          echo "DATE=$DATE" >> $GITHUB_ENV

      - name: Read stats for release body
        run: |
          COUNT=$(python -c "import json; lines=open('data/unified_skills.jsonl').readlines(); print(len(lines))")
          echo "SKILL_COUNT=$COUNT" >> $GITHUB_ENV

      - name: Publish GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          tag_name: index-${{ env.DATE }}
          name: Skill Index ${{ env.DATE }}
          body: |
            ## Skill Index ${{ env.DATE }}

            **Skills indexed:** ${{ env.SKILL_COUNT }}
            **SHA256:** `${{ env.SHA256 }}`

            ### Install / Update
            ```bash
            python scripts/update_index.py
            ```
          files: ${{ env.ARTIFACT }}
          token: ${{ secrets.GITHUB_TOKEN }}
```

### Regression Check Script (`pipeline/check_regression.py`)

```python
# pipeline/check_regression.py
import json, os, subprocess, sys

# Get previous release skill count from GitHub API
result = subprocess.run(
    ["gh", "release", "view", "--json", "body"],
    capture_output=True, text=True
)
if result.returncode != 0:
    print("No previous release found, skipping regression check.")
    sys.exit(0)

body = json.loads(result.stdout)["body"]
# Parse "Skills indexed: N" from release body
for line in body.splitlines():
    if "Skills indexed:" in line:
        prev_count = int(line.split(":")[-1].strip().replace(",", ""))
        break
else:
    print("Could not parse previous count, skipping.")
    sys.exit(0)

current_count = sum(1 for _ in open("data/unified_skills.jsonl"))
threshold = int(prev_count * 0.80)

if current_count < threshold:
    print(f"FAIL: Skill count dropped from {prev_count} to {current_count} ({threshold} threshold)")
    sys.exit(1)

print(f"OK: {current_count} skills (prev: {prev_count})")
```

---

## CI Dependencies (`requirements-ci.txt`)

```
# Crawler deps
requests>=2.31
pyyaml>=6.0
beautifulsoup4>=4.12
lxml>=5.0

# Embedding + Index
numpy>=1.26
faiss-cpu>=1.8
```

Ollama is installed as a system dependency via the install script, not as a Python package.

---

## Monitoring & Alerting

GitHub Actions sends email on workflow failure to the repo owner. No additional alerting infrastructure needed for v1.

If the weekly job fails consistently:
1. Check `Actions` tab for error output
2. Common causes: GitHub API rate limit changes, SkillHub scraping broken, Ollama install script URL changed, model name changed

---

## Release Versioning

Tags follow the format `index-YYYYMMDD` (e.g., `index-20260310`). This is not SemVer — the index is a data artifact, not a software release. `update_index.py` compares dates to determine if an update is available.

---

## Success Criteria

| Metric | Target |
|--------|--------|
| Weekly job success rate | ≥ 95% over rolling 12 weeks |
| Pipeline runtime | ≤ 90 min |
| Release artifact size | < 100MB |
| Quality gate catches regressions | 100% of >20% drops |
| `update_index.py` download + verify success rate | ≥ 99% |
| Time from crawl to published release | ≤ 2 hours |

---

## Open Questions

- GitHub Actions `ubuntu-latest` has 14GB RAM. FAISS index build for 20K skills × 1024 dims peaks at ~2GB — should be fine. Verify for larger index variants.
- `softprops/action-gh-release@v2` is a third-party action. Review its source or pin to a specific commit SHA to avoid supply-chain risk.
- Should old releases be cleaned up automatically (e.g., keep last 4 weekly releases)? Leaving them all means users can pin to a specific index date.
