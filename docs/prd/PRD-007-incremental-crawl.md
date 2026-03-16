# PRD-007: Incremental Crawl & Periodic Update

**Status:** Planned
**Phase:** 7 of 7
**Depends on:** PRD-001 (crawler pipeline), PRD-005 (CI/CD)
**Blocks:** Nothing

---

## Problem

All crawlers currently perform a full re-crawl on every run: every known repo is re-fetched, every SKILL.md re-downloaded, every web page re-scraped. As the corpus grows (14,000+ skills today, 100,000+ target), this becomes:

- **Slow:** full crawl takes hours; burns GitHub API rate limits (5,000 req/hr authenticated)
- **Wasteful:** >90% of repos haven't changed between runs; fetching them again yields no new data
- **Brittle:** long-running crawls are more likely to hit transient errors, partial state, or rate-limit windows
- **Expensive for metadata:** updating stars/pushed_at requires re-fetching every repo's GitHub API response even when the skill content is unchanged

Additionally, the current pipeline has no mechanism to:
- Detect when a known repo adds a **new** SKILL.md (e.g. a monorepo adds a second skill)
- Find **new repos** that published a SKILL.md since the last full crawl
- Skip awesome-list re-parses when the list itself hasn't changed

---

## Goals

- **Metadata refresh in minutes:** update stars, pushed_at, forks for all known repos without re-fetching content
- **Incremental skill update in hours:** only fetch repos whose content has changed since the last crawl
- **New skill discovery in hours:** find repos that published SKILL.md after the last crawl date
- **New SKILL.md detection:** for known repos that changed, detect newly added SKILL.md paths
- **Awesome-list change detection:** skip re-parsing when the list's commit SHA is unchanged
- **Persistent crawl state:** survives process crashes; enables safe resume and auditing
- **Rate-limit budget awareness:** allocate GitHub API quota across update modes efficiently

---

## Non-Goals

- Real-time / webhook-driven updates (GitHub webhooks require app registration)
- Streaming index updates (batch rebuild after crawl remains the norm)
- Removing deleted skills from the index (tombstone / stale detection is PRD-008)
- Crawling new registries discovered in the March 2026 web research (tracked separately in BACKLOG.md)

---

## Update Modes

Four discrete modes, run on different schedules:

| Mode | Trigger | Duration | API calls | What changes |
|------|---------|----------|-----------|--------------|
| **metadata** | Daily | ~2 min | 1 per unique repo | stars, pushed_at, forks — no content |
| **discover** | 3×/week | ~30 min | moderate | finds new repos published since last run |
| **incremental** | 3×/week | ~2 hr | high | re-fetches changed repos; detects new SKILL.md paths |
| **full** | Weekly | ~6 hr | exhaustive | complete re-crawl from scratch; resets state |

In normal operation: `metadata` runs daily, `discover + incremental` run together 3×/week, `full` runs once a week (Sunday CI).

---

## Functional Requirements

### F1 — Crawl State File

Each crawler maintains a per-source state file at `data/crawl_state/{source}.json`.

**Schema:**
```json
{
  "source": "marketplace",
  "last_full_crawl": "2026-03-01T00:00:00Z",
  "last_incremental": "2026-03-15T00:00:00Z",
  "last_metadata": "2026-03-15T20:00:00Z",
  "awesome_lists": {
    "ComposioHQ/awesome-claude-skills": {
      "last_sha": "a3f8c2...",
      "last_checked": "2026-03-15T00:00:00Z"
    }
  },
  "repos": {
    "anthropics/skills": {
      "pushed_at": "2026-03-10T12:00:00Z",
      "last_crawled": "2026-03-10T13:00:00Z",
      "stars": 2810,
      "skill_md_paths": ["skills/pptx/SKILL.md", "skills/git/SKILL.md"],
      "etag": "abc123"
    }
  }
}
```

State file writes are atomic (tmp → `os.replace()`). Missing state file means no prior run; behaves like `--mode full`.

`data/crawl_state/` is gitignored (machine-local). CI caches it between runs via GitHub Actions cache keyed on `data/crawl_state/`.

### F2 — Metadata Refresh Mode (`--mode metadata`)

Goal: update `stars`, `pushed_at`, `forks`, `default_branch` for all repos in the state file without touching SKILL.md content.

Algorithm:
1. Load state file; collect all `repo_full_name` keys
2. For each repo, call `GET /repos/{owner}/{repo}` with `If-None-Match: {etag}` header
   - HTTP 304: no change → update `last_metadata` timestamp only
   - HTTP 200: update `stars`, `pushed_at`, `etag` in state; write updated values to raw JSONL (patch mode — update matching `repo_url` records in-place)
3. Flush state file

This is already partially implemented by `pipeline/backfill_metadata.py`. Extend it to:
- Read/write the state file
- Use ETags for conditional requests
- Update raw JSONL records in-place (not just append)

**Rate limit budget:** ~1 req/repo. For 500 unique repos: 500 req ≈ 6 minutes at authenticated rate.

### F3 — Change Detection (`pushed_at` + ETag)

Applied during `incremental` mode before fetching content:

```
for repo in state.repos:
    if repo.pushed_at <= state.last_incremental:
        skip  # unchanged
    else:
        fetch_content(repo)  # changed since last run
```

ETag-based conditional requests (`If-None-Match`) are used for all GitHub API calls that support it. A 304 response costs 1 req but returns no body, saving bandwidth and secondary-rate-limit points.

### F4 — Incremental Repo Update (`--mode incremental`)

For repos whose `pushed_at` is newer than `last_incremental`:

1. Fetch the repo's file tree: `GET /repos/{owner}/{repo}/git/trees/HEAD?recursive=1`
2. Extract all paths matching `**/SKILL.md` or `**/AGENTS.md`
3. Compare against `state.repos[repo].skill_md_paths`:
   - **New paths:** fetch content, add new records to raw JSONL
   - **Existing paths:** fetch content only if the tree blob SHA differs from state
   - **Removed paths:** mark records as stale (do not delete yet — PRD-008)
4. Update `skill_md_paths` and blob SHAs in state

For skill content, store the blob SHA per path:
```json
"skill_md_paths": {
  "skills/pptx/SKILL.md": "sha:d4a9f3...",
  "skills/git/SKILL.md":  "sha:b1c2d4..."
}
```
A changed blob SHA means the file was edited → re-fetch + update record.

### F5 — New Repo Discovery (`--mode discover`)

For GitHub-search-based crawlers (SkillsMP, topic crawler):

1. Add `pushed:>{last_discover_date}` to the search query:
   ```
   filename:SKILL.md pushed:>2026-03-01
   ```
2. Collect all returned repos not already in state
3. Crawl new repos fully (fetch SKILL.md, repo metadata)
4. Add to raw JSONL and state

For awesome-list-based crawlers (ClawHub, marketplace):

1. Fetch the awesome list repo's latest commit SHA: `GET /repos/{owner}/{repo}/commits/HEAD`
2. Compare against `state.awesome_lists[repo].last_sha`
3. If unchanged: skip entirely
4. If changed: fetch README, parse diff against known entries, crawl only new/changed entries

For web-scraped registries (SkillHub, skills.sh):

1. Fetch the sitemap or paginated listing with `If-Modified-Since: {last_crawl}` headers
2. Only scrape pages that returned HTTP 200 (modified); skip 304s

### F6 — CLI Interface

All crawlers gain a `--mode` flag:

```
python -m crawlers.{name}_crawler -o data/raw/{name}.jsonl --mode full|incremental|metadata|discover
```

Additional flags:
- `--state data/crawl_state/{name}.json` — state file path (default: auto-derived from source name)
- `--since YYYY-MM-DD` — override the "last run" date (useful for backfills)
- `--force-repos owner/repo,...` — force-refresh specific repos regardless of pushed_at

Backward-compatible: omitting `--mode` defaults to `full` (current behavior).

### F7 — Unified Update Orchestrator

New script `pipeline/update_crawl.py` orchestrates all crawlers in the right mode:

```
python pipeline/update_crawl.py --mode metadata|incremental|discover|full [--sources skillsmp,clawhub,...]
```

Runs crawlers in parallel where possible (SkillsMP and topic crawler share the GitHub search quota, so they run sequentially; others can be parallelized). After crawling, optionally chains into normalize → backfill → embed → build_index → update_docs.

---

## Technical Spec

### State File — Blob SHA Tracking

GitHub Trees API returns per-file blob SHAs. Store them to detect file-level changes without fetching content:

```json
"skill_md_paths": {
  "SKILL.md": "blob:a1b2c3d4e5f6..."
}
```

On incremental run: fetch tree → compare blob SHAs → fetch only files with changed SHAs. This reduces content fetches to only actually-changed files.

### Rate Limit Allocation by Mode

| Mode | Search req/run | Content req/run | Metadata req/run | Total |
|------|--------------|----------------|-----------------|-------|
| metadata | 0 | 0 | ~500 | ~500 |
| discover | ~200 | ~50 (new only) | ~50 | ~300 |
| incremental | 0 | ~300 (changed) | 0 | ~300 |
| full | ~2,000 | ~5,000 | ~5,000 | ~12,000 |

GitHub authenticated rate limits: 5,000 req/hr (REST), 30 req/min (Search). Full mode spans multiple hours; uses `X-RateLimit-Reset` to sleep between windows.

### Raw JSONL Update Strategy

Two append strategies:

**Append-only (current):** new records are appended; duplicates resolved at normalize time. Simple but grows files unboundedly.

**Patch-in-place (new for metadata mode):** for metadata-only updates, find the existing line by `repo_url`, update `raw_metadata.stars` / `raw_metadata.pushed_at` in memory, rewrite file atomically. Avoids growing raw files with duplicate records that differ only in star count.

Patch-in-place is implemented in `pipeline/backfill_metadata.py` (already exists). Extend it to also handle blob SHA updates.

### Awesome List Diffing

```python
def diff_awesome_list(old_entries: list[str], new_entries: list[str]) -> tuple[list[str], list[str]]:
    """Returns (new_urls, removed_urls)."""
    old_set = set(old_entries)
    new_set = set(new_entries)
    return list(new_set - old_set), list(old_set - new_set)
```

Only crawl `new_urls`. Log `removed_urls` for future stale detection (PRD-008).

---

## Data Sources: Update Frequency by Source

| Source | Metadata | Discovery | Incremental | Full |
|--------|---------|-----------|-------------|------|
| SkillsMP | daily | 3×/week (`pushed:>DATE`) | 3×/week | weekly |
| ClawHub awesome list | daily (stars) | on SHA change | on SHA change | monthly |
| SkillHub | — | 3×/week | 3×/week | monthly |
| Marketplace repos | daily | on SHA change | on SHA change | monthly |
| Topic crawler | — | 3×/week | — | weekly |
| New awesome lists (PRD-007b) | daily | on SHA change | on SHA change | monthly |

---

## New Registries Backlog (March 2026 Research)

Web research identified the following sources not yet crawled. Prioritized for future implementation:

### High Priority — Official Org Repos (extend `marketplace_crawler.py`)
- `openai/skills` — Official Codex skills catalog
- `google-gemini/gemini-skills` — Official Gemini CLI skills
- `vercel-labs/agent-skills` — Vercel official skills
- `awslabs/agent-plugins` — AWS official; released Feb 2026
- `github/awesome-copilot` — 25k stars; has `skills/` + `marketplace.json`

### High Priority — Awesome Lists to Parse
- `ComposioHQ/awesome-claude-skills` (44k stars)
- `hesreallyhim/awesome-claude-code` (28k stars; also has `THE_RESOURCES_TABLE.csv`)
- `VoltAgent/awesome-agent-skills` (11k stars; 500+ cross-platform)
- `skillmatic-ai/awesome-agent-skills` — references Microsoft, Supabase, HuggingFace orgs
- `heilcheng/awesome-agent-skills` — Claude, Codex, Antigravity, Copilot, VS Code
- `sickn33/antigravity-awesome-skills` — 1,000+ Antigravity/Claude Code/Cursor

### High Priority — New GitHub Topic Tags (extend `topic_crawler.py`)

Add to `TOPIC_QUERIES`:
```python
"topic:claude-code-plugins",
"topic:gemini-skills",
"topic:gemini-cli-skills",
"topic:opencode-skills",
"topic:antigravity-skills",
"topic:cursor-skills",
"topic:skill-md",
"topic:agent-plugins",
"topic:kiro-skill",
"topic:roo-code-skill",
```

### High Priority — New Filename Patterns (extend `skillsmp_crawler.py`)
- `filename:AGENTS.md` — OpenAI Codex alternative to SKILL.md
- `filename:marketplace.json` — discovers more marketplace-format repos
- `path:.agents/skills filename:SKILL.md` — Gemini CLI / Codex directory convention

### Medium Priority — Web Registries
- `skills.sh` (88k skills) — check `skills.sh/llms.txt` for structured index
- `agentskill.sh` (110k skills) — check `agentskill-sh/agentskill-mcp` for API
- `agentskills.io/llms.txt` — the standard body's own index
- `claude-plugins.dev` — auto-indexes GitHub SKILL.md files

### Low Priority
- npm packages tagged `agent-skill` / `skill-md`
- PyPI `agent-skills` / `agent-skill` packages
- MCP registries (Smithery, Glama) — different schema, future expansion

---

## Success Criteria

| Metric | Target |
|--------|--------|
| Metadata refresh runtime | ≤ 5 min for 500 repos |
| Incremental update runtime | ≤ 2 hr for 15,000 skills |
| GitHub API calls saved vs. full crawl | ≥ 80% reduction in incremental mode |
| New repos discovered per discover run | ≥ 50 (when ecosystem is active) |
| State file corruption rate | 0% (atomic writes) |
| Missed updates (changed repo not re-crawled) | < 1% |

---

## Implementation Order

1. **State file read/write** — add to `crawlers/base.py` as shared utilities (`load_crawl_state`, `save_crawl_state`)
2. **`--mode metadata`** — extend `backfill_metadata.py` to read/write state and use ETags
3. **`--mode discover`** — add `pushed:>DATE` filter to `skillsmp_crawler.py` and `topic_crawler.py`; add SHA tracking to awesome-list crawlers
4. **`--mode incremental`** — add blob-SHA tracking + Trees API comparison to all crawlers
5. **`pipeline/update_crawl.py`** — orchestrator script
6. **New registries** — add high-priority sources from the backlog above

---

## Open Questions

| # | Question | Owner |
|---|----------|-------|
| 1 | Should `data/crawl_state/` be committed or CI-cached? Committing gives history but pollutes git log. | TBD |
| 2 | `skills.sh` and `agentskill.sh` claim 88k–110k skills each. Do they expose a public JSON index or require scraping? | Research needed |
| 3 | `filename:AGENTS.md` may return non-skill repos (generic agent docs). Need a heuristic to filter: check frontmatter for `name`+`description`, or check if `SKILL.md` also exists in the same repo. | TBD |
| 4 | Stale detection (repo deleted, SKILL.md removed) — should the index tombstone these or silently drop on next full rebuild? | PRD-008 |
