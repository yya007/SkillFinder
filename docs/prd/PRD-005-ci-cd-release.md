# PRD-005: CI/CD & Release Automation

**Status:** Implemented (amended 2026-06-22 — see "Architecture amendment" below)
**Phase:** 5 of 5
**Depends on:** PRD-001, PRD-002 (pipeline scripts work), PRD-004 (release artifact defined)
**Blocks:** Nothing (final phase)

---

## Architecture amendment (2026-06-22)

The original design (below) assumed CI could perform a **from-scratch full crawl +
rebuild** weekly. In practice it never could: all crawlers share the single Actions
`GITHUB_TOKEN` (5,000 REST req/hr, ~10/min code-search), so fetching the ~37k-skill
corpus needs many hours of API quota — far beyond any runner budget. Every scheduled
run since launch was cancelled or failed; a manual run on 2026-06-22 produced only
1,641 skills in 45 minutes (gate needs ≥ 8,000).

**Revised model — hybrid:**

- **Full index builds are done locally and published manually.** A maintainer runs the
  crawlers in `--mode incremental` on cached `data/raw/`, then normalize → embed
  (Ollama) → `build_index.py`, and publishes the `index-YYYYMMDD` GitHub Release (see
  the `update-index` skill). This is how the index has always actually been seeded.
- **CI (`update-index.yml`) is incremental-only.** It downloads the latest `index-*`
  release and *appends* newly-discovered skills, with a 60-minute timeout. It is a
  deliberate **green no-op** when an append is impossible — no index release yet, or the
  index is an `IndexIVFScalarQuantizer` (≥ `IVF_THRESHOLD` vectors) whose centroids
  cannot accept incremental adds. Because the production corpus is > 30k, CI is
  effectively dormant until a local rebuild lowers the count or the index type changes;
  it exists to avoid weekly false-failures and to self-activate for smaller corpora.
- **npm publish is automated (`npm-publish.yml`).** When a fresh index lands on
  `master` (a `data/version.txt` change from a merged local rebuild), this workflow
  patch-bumps the package, publishes `@yya007/skill-finder` to npm, tags `v<version>`,
  and stamps the release log — all on GitHub runners, no local machine. It bumps from
  the *published* npm version (self-healing across runs) and requires the `NPM_TOKEN`
  secret (a granular npm token with "Bypass 2FA"). `workflow_dispatch` supports a
  `dry_run` (default true) to test without publishing. The index build stays manual
  (local); only the npm release step is hands-off. The `npm-release` skill remains for
  ad-hoc/manual publishes.

The functional requirements below describe the original full-rebuild-in-CI design and
are retained for history; F5 (incremental strategy) is what CI now implements.

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

- Real-time updates (weekly cadence is sufficient)
- Multi-region CDN distribution (GitHub Releases is the CDN)
- Automated rollback to previous index on quality regression (manual rollback only for v1)
- Updating or deleting existing skills in the index without a full rebuild

---

## Functional Requirements

### F1 — Weekly Crawl & Index Job

**Trigger:** `schedule: cron: '0 6 * * 1'` (Monday 6AM UTC) and `workflow_dispatch`

**Steps:**

1. **Checkout** repo
2. **Setup Python 3.11**
3. **Install dependencies** (`pip install -r requirements-dev.txt`)
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

### F5 — Incremental vs Full Rebuild Strategy

The embedding step is the pipeline bottleneck (~60 min for 20K skills). Most weekly runs add only a small fraction of new skills, making a full re-embed wasteful. The pipeline automatically selects the cheaper path.

#### Decision rule

After normalization, the workflow compares the new skill count against the previous release count:

| Condition | Path |
|-----------|------|
| No previous release | Full rebuild |
| `force_rebuild = true` (manual input) | Full rebuild |
| `\|new - prev\| / prev ≥ 20%` | Full rebuild |
| `\|new - prev\| / prev < 20%` | **Incremental** |

#### Incremental path (`pipeline/incremental_update.py`)

1. Download the previous release artifact and extract to `data/`.
2. Load existing skill IDs from `data/metadata.jsonl`.
3. Find skills in `unified_skills.jsonl` whose ID is not in the existing set.
4. Embed only the new skills via Ollama.
5. L2-normalise and call `index.add(new_vecs)` on the loaded `IndexFlatIP`.
6. Append new rows to `metadata.jsonl`; overwrite `version.txt`.

**Limitations of incremental path:**
- Cannot update changed descriptions for existing skills (full rebuild required).
- Cannot remove skills deleted upstream (full rebuild required).
- Only works with `IndexFlatIP` (< 50k vectors). Once the index exceeds 50k skills and switches to `IndexIVFFlat`, a full rebuild is always required.

#### Full rebuild path

Runs the complete pipeline: `embed.py` (all skills) → `build_index.py`. Triggered by the conditions above or when the incremental path raises `IncrementalError`.

#### Estimated time savings

| Index size | Full rebuild | Incremental (5% new) | Savings |
|------------|-------------|----------------------|---------|
| 10K skills | ~30 min | ~2 min | ~93% |
| 20K skills | ~60 min | ~3 min | ~95% |

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
        run: pip install -r requirements-dev.txt

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

## CI Dependencies (`requirements-dev.txt`)

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

## F6 — npm Package Release

SkillFinder is also published to the npm registry as `@yya007/skill-finder`. This provides a lighter-weight install path for end users who only need the runtime files and not the full repository (crawlers, pipeline, tests, docs).

### What gets published

The `files` field in `package.json` limits the published package to:

```
scripts/        ← search.py, fetch_skill.py, update_index.py, requirements.txt
SKILL.md        ← agent skill definition
plugin.json     ← OpenClaw plugin manifest
```

Data files (`data/index.faiss`, `data/metadata.jsonl`) are **excluded** from the npm package — users download the latest index via `python scripts/update_index.py` after install. This avoids bloating the package (FAISS index can exceed npm's 100 MB unpack limit as the index grows).

### Release workflow

```bash
# Preview what would be published (no changes)
./scripts/npm-release.sh

# Patch release (e.g. 1.0.0 → 1.0.1)
./scripts/npm-release.sh patch

# Minor or major
./scripts/npm-release.sh minor
./scripts/npm-release.sh major
```

`scripts/npm-release.sh` handles: login check → dry-run preview → version bump → `npm publish --access public` → optional git tag + push.

### npm / GitHub release versioning alignment

| Artifact | Tag format | Versioned by |
|----------|-----------|-------------|
| GitHub Release (index) | `index-YYYYMMDD` | Data date |
| npm package | `1.x.y` (SemVer) | Code/skill changes |

The two versioning schemes are independent. A data release does not trigger an npm bump; a npm bump does not require a new index build.

### Pre-publish checklist

1. `npm whoami` confirms you are logged in as `yya007`.
2. `npm publish --dry-run` shows only `scripts/`, `SKILL.md`, `plugin.json`.
3. `plugin.json` `version` and `package.json` `version` are in sync.
4. All unit tests pass (`pytest`).

---

## Known Breaking Changes

### Monorepo skill ID scheme (v1.0)
Skills in monorepos (e.g. `anthropics/skills`) previously keyed their `id` on
`sha256(repo_url)`. They now key on `sha256(skill_md_url)`, giving each skill in a
monorepo a distinct stable ID. IDs for all multi-skill repos changed in this release.

**Impact:** Any downstream system that stored SkillFinder IDs and expected them to be
stable across index rebuilds will see these IDs as new/unknown records. There is no
automatic migration — a fresh index download is required.

---

## Resolved Questions

**RAM for FAISS index build** — Resolved. Peak memory for 20K skills × 1024 dims is ~2 GB (embeddings array) + ~200 MB (FAISS index in RAM) = ~2.2 GB total. `ubuntu-latest` provides 14 GB; no issue. If the index grows beyond 200K skills the `IndexIVFFlat` path (already implemented in `build_index.py`) keeps memory bounded, and a larger runner can be selected via `runs-on`.

**`softprops/action-gh-release` supply-chain risk** — Resolved. The workflow pins to the exact commit SHA `b25b93d384199fc0fc8c2e126b2d937a0cbeb2ae` (v2) rather than the mutable `@v2` tag. This ensures the action cannot be silently changed upstream. SHA should be re-evaluated when intentionally upgrading the action.

**Automatic old release cleanup** — Resolved. Phase 10 of the workflow deletes all `index-*` releases beyond the 3 most recent using `gh release delete --cleanup-tag`. Keeping 3 (≈ 3 weeks of history) gives users a grace window to pull a working artifact if the latest fails verification, without accumulating unbounded storage.
