# PRD-001: Data Crawler Pipeline

**Status:** Implemented (active development)
**Phase:** 1 of 7
**Depends on:** Nothing
**Blocks:** PRD-002
**See also:** PRD-007 (incremental crawl & periodic update)

---

## Problem

SkillFinder needs a corpus of agent skills to index. Skills are scattered across many sources with different access methods, formats, and quality signals. We need a reliable, automated pipeline to collect them all and output a unified, deduplicated dataset.

## Goals

- Crawl all source registries and output raw JSONL per source
- Normalize and deduplicate into a single `unified_skills.jsonl`
- Apply quality filtering (configurable `--min-stars`, default 10)
- Run unattended in GitHub Actions
- Support incremental updates to avoid full re-crawls (see PRD-007)

## Non-Goals

- Real-time / webhook-driven crawling (PRD-007 covers periodic incremental)
- Building the embedding index (PRD-002)
- Distributing the crawler to end users (CI-only code)

---

## Functional Requirements

### F1 — SkillsMP Crawler

- Use GitHub Code Search API with query `filename:SKILL.md`
- Paginate through all results using `Link` header navigation
- For each result repo: fetch raw `SKILL.md`, parse YAML frontmatter, fetch repo `stargazers_count` + `pushed_at` + `topics`
- Handle GitHub rate limits: 30 req/min (search), 5,000 req/hr (general)
  - Read `X-RateLimit-Remaining` and `X-RateLimit-Reset` on every response
  - Back off proactively when remaining < 10; never hard-sleep without checking
- Output: `data/raw/skillsmp.jsonl`

### F2 — ClawHub / OpenClaw Crawler

- Parse `VoltAgent/awesome-openclaw-skills` README.md from GitHub
  - Extract category headers (`## Category`)
  - Extract skill lines: `- [name](url) — description`
- If ClawHub REST API responds: fetch per-skill safety scan data
- Output: `data/raw/clawhub.jsonl`

### F3 — SkillHub Crawler

- Scrape skillhub.club skill listing and detail pages
- Extract per skill: name, description, repo_url, overall rank (S/A/B/C), dimension scores (Practicality, Clarity, Automation, Quality, Impact)
- Respect `robots.txt`; add 1–2s inter-request delay
- Output: `data/raw/skillhub.jsonl`

### F4 — Topic Crawler

- Search GitHub repos tagged with skill-related topics
- Topics covered: `claude-skill`, `claude-code-skill`, `claude-skills`, `claude-code-skills`, `openclaw-skill`, `openclaw-skills`, `codex-skill`, `codex-skills`, `agent-skill`, `agent-skills`
- Source tag: `"topic"` — requires ≥10 stars to pass quality filter
- Output: `data/raw/topic.jsonl`

### F5 — Marketplace Crawler

- Target repos: `anthropics/skills`, `daymade/claude-code-skills`, `mhattingpete/claude-skills-marketplace`, `alirezarezvani/claude-skills`, plus any repo with `marketplace.json` at root discovered via GitHub Search
- Parse `marketplace.json` arrays; read `SKILL.md` at each listed path
- Fetches `stargazers_count` and `pushed_at` per repo and stores in `raw_metadata`
- Output: `data/raw/marketplace.jsonl`

**Pending (PRD-007 backlog):** Add high-priority official org repos — `openai/skills`, `google-gemini/gemini-skills`, `vercel-labs/agent-skills`, `awslabs/agent-plugins`, `github/awesome-copilot`.

### F6 — Normalizer

Input: all `data/raw/*.jsonl` files
Output: `data/unified_skills.jsonl`

Steps:
1. Load all records, tag each with `source` field
2. Compute canonical key: `lower(repo_url).rstrip('/').removesuffix('.git')`
3. For monorepo skills (multiple SKILL.md per repo): use `sha256(skill_md_url)` as ID; fall back to `sha256(canonical_repo_url)` when `skill_md_url` absent
4. Group by canonical key; merge metadata using priority rules in [`data-sources.md`](../data-sources.md)
5. Build `embedding_text` from merged record
6. Apply quality filter: `stars >= min_stars` (configurable via `--min-stars`, default 10) AND non-empty description
7. Write output with one JSON object per line

### F7 — Metadata Backfill

`pipeline/backfill_metadata.py` — patches `stars` and `pushed_at` into existing raw JSONL without re-crawling content. Uses `repo_url` to deduplicate GitHub API calls (one call per unique repo, not per record). Atomic file write. See PRD-007 for the full incremental update design.

---

## Technical Spec

### Raw Record Schema (common fields)

Each crawler outputs JSONL where every line has at minimum:

```json
{
  "repo_url": "https://github.com/user/repo",
  "name": "...",
  "description": "...",
  "source": "skillsmp",
  "raw_metadata": { ... }
}
```

Source-specific fields live in `raw_metadata`.

### Crawler Interface

Each crawler is a standalone Python script with a common CLI:

```
python crawlers/{name}_crawler.py -o data/raw/{name}.jsonl [--limit N] [--since YYYY-MM-DD]
```

Flags:
- `-o` / `--output`: output file path (required)
- `--limit N`: stop after N records (for testing)
- `--since YYYY-MM-DD`: only include skills updated after this date (where supported)
- `--resume`: skip repos already in the output file (resume interrupted run)

### Error Handling

- Network errors: retry up to 3 times with exponential backoff (1s, 4s, 16s)
- Parse errors: log warning, skip record, continue
- Rate limit errors (429, 403 with `X-RateLimit-Remaining: 0`): sleep until `X-RateLimit-Reset`, then retry
- Fatal errors: non-zero exit code, clear error message to stderr

### Dependencies

```
requests>=2.31
pyyaml>=6.0
beautifulsoup4>=4.12  # SkillHub scraper
lxml>=5.0             # faster HTML parser for bs4
```

---

## Success Criteria

| Metric | Current | Target |
|--------|---------|--------|
| SkillsMP records collected | 1,583 raw | ≥ 50,000 raw |
| ClawHub records collected | 2,009 raw | ≥ 5,000 raw |
| SkillHub records collected | 141 raw (fix in progress) | ≥ 5,000 raw |
| Marketplace records collected | 13,020 raw | ≥ 20,000 raw |
| Total unique skills after dedup + filter | 14,306 | ≥ 20,000 |
| Skills with no description | 0 | 0 |
| Crawler crash rate | < 1% | < 1% |
| stars field populated in all raw records | Yes (after backfill) | Yes |

---

## Implementation Order

1. ✅ `pipeline/normalize.py` — defines output contract
2. ✅ `crawlers/marketplace_crawler.py` — known repos, structured data
3. ✅ `crawlers/skillsmp_crawler.py` — GitHub code search with size+star sharding
4. ✅ `crawlers/clawhub_crawler.py` — awesome list + org/topic discovery
5. ✅ `crawlers/skillhub_crawler.py` — web scraping with category pagination
6. ✅ `crawlers/topic_crawler.py` — GitHub topic tag search
7. ✅ `pipeline/backfill_metadata.py` — stars/pushed_at patch without re-crawl
8. ✅ `pipeline/update_docs.py` — auto-refresh stats in README/SKILL.md
9. 🔲 PRD-007: incremental crawl state, `--mode` flags, new registries

---

## Resolved Questions

**GitHub Code Search 1,000-result cap** — Resolved. The SkillsMP crawler shards by `SKILL.md` file size (`size:<=500`, `size:501..2000`, `size:2001..10000`, `size:>10000`) crossed with star buckets (`stars:0`, `stars:1..10`, `stars:11..100`, `stars:>100`). Each cell is a disjoint range. Cross-shard deduplication is applied before writing output.

**SkillHub category pagination** — Resolved. The site uses `?category=<slug>&page=N` pagination. The crawler calls `discover_categories()` at startup to extract all category slugs from the index page, then crawls each category separately. Deduplication across categories is handled via `seen_urls` set.

**Marketplace stars missing** — Resolved. `marketplace_crawler.py` now passes `stargazers_count` and `pushed_at` from `fetch_repo_metadata()` into `raw_metadata`. Existing records can be backfilled with `pipeline/backfill_metadata.py`.

**Quality filter flexibility** — Resolved. `pipeline/normalize.py` accepts `--min-stars N` (default 10). Curated-source bypass removed; star count is the single quality signal alongside non-empty description.

**SkillHub bot detection** — Resolved. The crawler respects `robots.txt` (via `urllib.robotparser`) and sets a descriptive `User-Agent`. No Playwright required at current crawl volume.

**ClawHub REST API authentication** — Resolved. ClawHub is indexed via the GitHub API (searching the `openclaw/clawhub` repository), not a separate ClawHub REST API. Standard `GITHUB_TOKEN` is sufficient.
