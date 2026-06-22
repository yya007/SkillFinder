# SkillFinder Backlog

Tracked improvements, identified during code review or planning.
Items are grouped by theme; severity noted where applicable.

---

## Security & Correctness

### Implement real safety scanner for ClawHub records
**Priority: medium**
`safety_scan_date` is plumbed through the pipeline but `safety_scan` is only ever set
to `True` for `VoltAgent/awesome-openclaw-skills` records (curated list trust). There is
no actual scan logic that checks SKILL.md content for dangerous patterns. A real
implementation should inspect trigger blocks for `shell: true`, suspicious `curl | sh`
patterns, and escalating permission requests.

**Files:** `crawlers/clawhub_crawler.py` (post-fetch scan), `pipeline/normalize.py` (optional normalize-time scan).

---

### Heuristic safety scan for SkillsMP/SkillHub records
**Priority: low**
Only ClawHub-originated records carry a `safety_scan` result; SkillsMP and SkillHub
records always have `safety_scan: null`. Consider running a lightweight heuristic scan
(check for suspicious patterns in SKILL.md triggers, no `shell: true` escalations, etc.)
at normalize time so the flag has broader coverage.

**Files:** `pipeline/normalize.py`, `SKILL.md` (Step 5 — surface scan date to agent).

---

## Features

### crawl-sources SKILL.md GITHUB_TOKEN check blocks SkillHub
**Priority: low**
`skills/crawl-sources/SKILL.md` checks for a GITHUB_TOKEN before running any crawler,
but SkillHub (`skillhub_crawler.py`) uses HTTP scraping only and does not need a GitHub
token. The check incorrectly blocks SkillHub crawls when no token is present.

**File:** `skills/crawl-sources/SKILL.md`.

---

### Root SKILL.md / skills/update-index/SKILL.md duplication
**Priority: low**
The root `SKILL.md` Workflow B (full update) and `skills/update-index/SKILL.md` define
similar step sequences independently. Changes to one are not reflected in the other,
creating maintenance split-brain risk.

**Files:** `SKILL.md`, `skills/update-index/SKILL.md`.

---

### package.json postinstall uses brittle node -e string
**Priority: low**
The `postinstall` script in `package.json` uses a multi-line `node -e '...'` string with
manual escaping for newlines and quotes. This is fragile and breaks with certain npm
versions or shells. Extract the logic into a dedicated `scripts/postinstall.js` file.

**File:** `package.json`.

---

### npm/PyPI skill package crawler
**Priority: low**
Skills distributed as npm packages (e.g. `@scope/claude-skill-foo`) or PyPI packages
are not discoverable via GitHub code search or registry crawlers. A future crawler
could query the npm registry (`https://registry.npmjs.org/-/v1/search?text=claude-skill`)
and PyPI JSON API (`https://pypi.org/search/?q=claude-skill`) to find packaged skills.

**Prerequisite:** Define a packaging convention (must include SKILL.md at package root or
declare `"skillfinder": true` in package.json / pyproject.toml).

**File:** `crawlers/npm_crawler.py`, `crawlers/pypi_crawler.py` (new files, not yet planned).

---

## Reliability

### Add CI cleanup steps on workflow failure
**Priority: low** *(review issue 14)*
Staging directories (`/tmp/prev-index`, `data/.new/`) are not pruned when a CI
phase fails mid-run. On a retry, stale cached files can cause confusing behavior.
Add a cleanup step with `if: always()`.

**File:** `.github/workflows/update-index.yml`.

---

### Deep crawler state file not atomic on Windows
**Priority: low**
`crawlers/skillsmp_deep_crawler.py` saves state via `tmp → os.replace()`. On
POSIX this is atomic; on Windows `os.replace()` can fail if the destination is
open. Low risk for current Linux CI usage, but worth noting.

---

## Developer Experience

### Report line numbers on JSON parse errors in incremental_update.py
**Priority: low** *(review issue 11)*
`json.loads(line)` in `load_existing_ids()` and `find_new_skills()` raises
uncontextualized exceptions on corrupt JSONL. Wrap in try/except and include line
numbers.

**File:** `pipeline/incremental_update.py`.

---

### Align SKILL.md example output with format_results() actual output
**Priority: low** *(review issue 12)*
The Step 5 example in `SKILL.md` uses inline formatting that differs from what
`--no-json` actually produces. Update one to match the other.

**Files:** `SKILL.md:73-87`, `scripts/search.py:335-345`.

---

### Document incremental_update.py in CLAUDE.md
**Priority: low** *(review issue 13)*
`CLAUDE.md` only lists the full three-step pipeline. Add a section explaining
incremental vs. full rebuild and when to use each.

**File:** `CLAUDE.md`.

---

## Code Quality

### Remove dead CURATED_SOURCES constant from normalize.py
**Priority: low**
After removing curated-source bypass from `passes_quality_filter()`,
`CURATED_SOURCES` (line 28) is defined but never referenced. Remove it.

**File:** `pipeline/normalize.py:28`.

---

## Performance — Crawl rate-limit efficiency

See `docs/crawler-rate-limit-strategy.md` for the full analysis (where the
5,000/hr budget goes, the constraint table, and impact/effort per option). The
current crawl cannot fetch the full corpus within GitHub's limits, which is why
CI is incremental-only and full builds are local (PRD-005 amendment). Items
below are ordered by impact ÷ effort.

### ① Fetch SKILL.md via raw.githubusercontent.com (free, not rate-limited)
**Priority: high**
`fetch_skill_md` (`crawlers/base.py:903`) pulls each file through the Contents
API and tries up to 3 branches — 1–3 quota-charged calls per skill, the dominant
cost. `raw.githubusercontent.com/{repo}/{branch}/{path}` is a CDN that does not
consume the REST quota. Move the happy path to raw; keep the Contents API only as
a fallback for symlinks/private repos.

**Files:** `crawlers/base.py` (`fetch_skill_md`).

---

### ② Persist ETags + pushed_at across runs (304 = zero quota)
**Priority: high**
`fetch_repo_metadata_with_etag` and `github_get(..., etag=)` already support
`If-None-Match`, but ETags are never persisted and the bulk path uses the
non-ETag fetch. Persist `{repo_url: (etag, pushed_at)}` (committed or via
`actions/cache`); skip unchanged repos by `pushed_at`, else send the ETag for a
free 304. Makes weekly incrementals near-zero-quota — likely enough to let CI
crawl incrementally again.

**Files:** `crawlers/base.py` (`fetch_repo_metadata*`, `github_get`), crawler call sites.

---

### ③ Dedup before fetch + shared positive cache
**Priority: high**
Five crawlers independently re-fetch overlapping repos; `normalize.py` only
dedups *after* fetching. Collect candidate repo URLs from all sources → dedup by
canonical URL → fetch each once, backed by a per-run cache keyed by canonical
repo URL (a positive counterpart to the existing filter cache).

**Files:** crawler discovery, `crawlers/base.py`, `pipeline/normalize.py`.

---

### ⑥ Monorepos: one recursive tree fetch instead of N Contents calls
**Priority: medium**
`git/trees/{sha}?recursive=1` (1 call) enumerates every SKILL.md path in a
monorepo; fetch each via raw (free, per ①). Replaces 1 Contents call per file.

**Files:** `crawlers/base.py`.

---

### ⑤ Batch repo metadata via GraphQL (≤100 repos/call)
**Priority: medium**
Replace per-repo `/repos/{o}/{r}` REST calls with a GraphQL query (stars/
pushed_at/default_branch) for up to 100 repos at once — ~1–2 points of the
separate 5,000-point/hr GraphQL pool.

**Files:** `crawlers/base.py` (new GraphQL helper), crawler call sites.

---

### ④ Reduce search/code reliance (the 10/min stall)
**Priority: medium**
Cache the discovered repo list; run discovery with date filters
(`pushed:>last-run`) so search surfaces only new repos. Prefer repo/topic search
(30/min) over code search (10/min) where possible.

**Files:** `crawlers/skillsmp_crawler.py`, `crawlers/topic_crawler.py`, `crawlers/marketplace_crawler.py`.

---

### ⑦ Raise the ceiling: multiple tokens / GitHub App (fallback)
**Priority: low**
REST is 5,000/hr per token; rotating N tokens ≈ N× headroom, a GitHub App
installation gets 15,000/hr (search stays per-user). Brute force with ops
overhead — only if ①–⑥ are insufficient.

**Files:** `crawlers/base.py` (session/token rotation).

### ⑥b Cache find_skill_md_paths by repo + HEAD SHA
**Priority: medium** *(follow-up from the ①②③ impact measurement)*
After ①②③, the residual metered cost per repo is the recursive Trees call in
`find_skill_md_paths` (one metered call per repo every run, even when nothing
changed). Cache the `{path: blob_sha}` result keyed by `repo + HEAD commit SHA`
(one cheap `fetch_commit_sha` call, or reuse the ETag-cached metadata's known
HEAD), and skip the Trees call when HEAD is unchanged. This is what makes a warm
run approach zero metered cost *per repo*, not just per skill.

**Files:** `crawlers/base.py` (`find_skill_md_paths`, `fetch_commit_sha`).
