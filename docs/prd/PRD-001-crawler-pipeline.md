# PRD-001: Data Crawler Pipeline

**Status:** Planned
**Phase:** 1 of 5
**Depends on:** Nothing
**Blocks:** PRD-002

---

## Problem

SkillFinder needs a corpus of agent skills to index. Skills are scattered across four distinct sources with different access methods, formats, and quality signals. We need a reliable, automated pipeline to collect them all and output a unified, deduplicated dataset.

## Goals

- Crawl all four source registries and output raw JSONL per source
- Normalize and deduplicate into a single `unified_skills.jsonl`
- Apply quality filtering to produce a 10K–20K skill corpus
- Run unattended in GitHub Actions; complete in under 60 minutes

## Non-Goals

- Real-time / incremental crawling (weekly full rebuild is sufficient)
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

### F4 — Marketplace Crawler

- Target repos: `anthropics/skills`, `daymade/claude-code-skills`, `mhattingpete/claude-skills-marketplace`, `alirezarezvani/claude-skills`, plus any repo with `marketplace.json` at root discovered via GitHub Search
- Parse `marketplace.json` arrays; read `SKILL.md` at each listed path
- Output: `data/raw/marketplace.jsonl`

### F5 — Normalizer

Input: all `data/raw/*.jsonl` files
Output: `data/unified_skills.jsonl`

Steps:
1. Load all records, tag each with `source` field
2. Compute canonical key: `lower(repo_url).rstrip('/').removesuffix('.git')`
3. Group by canonical key; merge metadata using priority rules in [`data-sources.md`](../data-sources.md)
4. Build `embedding_text` from merged record
5. Apply quality filter (drop records with no description and zero stars and no curated registry presence)
6. Assign `id = sha256(canonical_key)`
7. Write output with one JSON object per line

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

| Metric | Target |
|--------|--------|
| SkillsMP records collected | ≥ 50,000 raw |
| ClawHub records collected | ≥ 5,000 raw |
| SkillHub records collected | ≥ 5,000 raw |
| Total unique skills after dedup | ≥ 10,000 |
| Skills passing quality filter | ≥ 8,000 |
| Pipeline runtime | ≤ 60 min in CI |
| Skills with no description | 0 in `unified_skills.jsonl` |
| Crawler crash rate | < 1% of source records |

---

## Implementation Order

1. `pipeline/normalize.py` first — it defines the output contract; write tests against it with fixture data
2. `crawlers/marketplace_crawler.py` — simplest (known repos, structured data)
3. `crawlers/skillsmp_crawler.py` — most data, needs rate limit handling
4. `crawlers/clawhub_crawler.py` — parse README format
5. `crawlers/skillhub_crawler.py` — web scraping, most fragile

---

## Open Questions

- GitHub Code Search caps at 1,000 results per query. Strategy for SkillsMP: shard by date range (`pushed:>2025-01-01`) and by language filter. Need to validate this gets full coverage.
- SkillHub may add bot detection. Have a plan for rotating user agents or using Playwright if needed.
- ClawHub REST API: is it public? Need to test if it's accessible without auth.
