---
name: crawl-sources
description: >
  Crawl all SkillFinder data sources (SkillsMP, ClawHub, SkillHub, Anthropic
  marketplace, GitHub topics) and write fresh raw JSONL files to data/raw/.
  Runs crawlers in parallel; supports incremental resume and per-source limits.
triggers:
  - crawl skill sources
  - refresh raw data
  - run crawlers
  - crawl skillsmp
  - crawl clawhub
  - crawl skillhub
  - crawl marketplace
  - crawl topic
  - re-crawl sources
  - update raw skills data
  - fetch new skills from registries
---

# crawl-sources

Crawl all skill registries and refresh the raw data files in `data/raw/`.

This skill covers the **crawl step only** — it does not normalize, embed, or
rebuild the index. Run `update-index` afterward to turn fresh raw data into a
searchable index.

## Prerequisites

- `GITHUB_TOKEN` env var set (required by SkillsMP, ClawHub, and topic crawlers)
- Python dependencies installed: `pip install -r requirements-dev.txt`

## Agent Instructions

When this skill triggers, determine scope from the user's request:

| User says | Scope |
|-----------|-------|
| "crawl everything" / "all sources" | Run all five crawlers (default) |
| "crawl skillsmp" / "crawl github" | SkillsMP only |
| "crawl clawhub" / "crawl openclaw" | ClawHub only |
| "crawl skillhub" | SkillHub only |
| "crawl marketplace" / "crawl anthropic" | Marketplace only |
| "crawl topics" / "crawl github topics" | Topic crawler only |
| "quick test" / "limit N" | Add `--limit N` to each crawler |

---

### Step 1 — Check prerequisites

```bash
echo "${GITHUB_TOKEN:0:4}..."
```

If empty, stop:
> "Set `GITHUB_TOKEN` before crawling: `export GITHUB_TOKEN=ghp_...`"

```bash
mkdir -p data/raw
```

---

### Step 2 — Run crawlers

**All sources (parallel — default):**

```bash
python -m crawlers.skillsmp_crawler -o data/raw/skillsmp.jsonl &
python -m crawlers.clawhub_crawler  -o data/raw/clawhub.jsonl &
python -m crawlers.skillhub_crawler -o data/raw/skillhub.jsonl &
python -m crawlers.marketplace_crawler -o data/raw/marketplace.jsonl &
python -m crawlers.topic_crawler    -o data/raw/topic.jsonl --data-dir data/raw &
wait
```

**Single-source variants** (use when user targets one source):

```bash
# SkillsMP (GitHub code search for SKILL.md files; ~30 min full run)
python -m crawlers.skillsmp_crawler -o data/raw/skillsmp.jsonl

# ClawHub / OpenClaw (awesome-list + org/topic discovery)
python -m crawlers.clawhub_crawler -o data/raw/clawhub.jsonl

# SkillHub (HTML scrape with pagination)
python -m crawlers.skillhub_crawler -o data/raw/skillhub.jsonl

# Anthropic official marketplace
python -m crawlers.marketplace_crawler -o data/raw/marketplace.jsonl

# GitHub topic tags (claude-skill, codex-skill, agent-skill, …)
python -m crawlers.topic_crawler -o data/raw/topic.jsonl --data-dir data/raw
```

**Useful flags (apply to any crawler):**

| Flag | Effect |
|------|--------|
| `--limit N` | Cap at N records — use for quick tests |
| `--mode incremental` | Skip repos already present in the output file (preferred over `--resume`) |
| `--mode full` | Complete re-crawl (default) |
| `--mode metadata` | Refresh stars/ETags only, skip content fetch |
| `--mode discover` | Only fetch repos pushed since last run (date-filtered search) |
| `--resume` | **Deprecated** — use `--mode incremental` instead |
| `--since YYYY-MM-DD` | Only include repos pushed after this date (SkillsMP) |
| `--log-level DEBUG` | Verbose output |
| `--filter-cache FILE` | Path to dedup cache (default: `data/filter_cache.jsonl`) |

**Orchestrator (runs all crawlers in the right order):**

```bash
python pipeline/update_crawl.py --mode incremental
python pipeline/update_crawl.py --mode full --sources clawhub,skillsmp
python pipeline/update_crawl.py --mode incremental --chain  # also runs normalize/embed/build
```

---

### Step 3 — Report record counts

After all crawlers finish:

```bash
for f in data/raw/skillsmp.jsonl data/raw/clawhub.jsonl data/raw/skillhub.jsonl data/raw/marketplace.jsonl data/raw/topic.jsonl; do
  [ -f "$f" ] && echo "$(wc -l < $f) $(basename $f)"
done
```

Report the counts to the user. Flag any source with 0 records as a potential failure.

---

### Step 4 — Suggest next step

If crawl succeeded, offer:
> "Raw data is refreshed. Run the `update-index` skill to normalize, embed, and rebuild the FAISS index — or run it now?"

If the user says yes, hand off to the `update-index` skill starting at its Step 3 (Backfill metadata).

---

### Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| SkillsMP returns 0 records | Rate limit or bad token | Check `GITHUB_TOKEN`; retry with `--limit 50` |
| ClawHub hangs | GitHub API slow | Add `--limit 200` for a quick run |
| SkillHub returns few records | Site structure changed | Run with `--log-level DEBUG` and inspect HTML |
| `filter_cache.jsonl` grows large | Normal — dedup cache | Safe to delete; will be rebuilt on next run |
