# SkillFinder Backlog

Tracked improvements, identified during code review or planning.
Items are grouped by theme; severity noted where applicable.

---

## Security & Correctness

### Update safety check
**Priority: high**
The `safety_scan` flag in the index is a pass/fail boolean sourced from ClawHub's
automated scanner. It has two gaps:

1. **Staleness** — a skill scanned months ago is still flagged `safety_scan: true`
   even if the repo has since changed. Add a `safety_scan_date` field to metadata
   and surface it in search output so users can judge freshness.
2. **Coverage** — only ClawHub-originated records carry a scan result; SkillsMP
   and SkillHub records always have `safety_scan: null`. Consider running a
   lightweight heuristic scan (check for suspicious patterns in SKILL.md triggers,
   no `shell: true` escalations, etc.) at normalize time so the flag has broader
   coverage.

**Files:** `pipeline/normalize.py`, `scripts/search.py` (format output),
`SKILL.md` (Step 5 — surface scan date to agent).

---

## Features

### Remote search fallback
**Priority: medium**
When the local index is missing or Ollama is unavailable, fall back to a
lightweight remote search API rather than failing hard. Options:

- Hosted SkillFinder API (future): POST query → ranked results, no local model needed.
- GitHub code search fallback: query `filename:SKILL.md <terms>` via GitHub API,
  return top results with basic metadata. No embedding quality, but better than nothing.

Scope:
- Add `--remote` flag to `scripts/search.py` to opt in explicitly.
- In `SKILL.md` agent instructions, add a Step 0 check: if index or Ollama is
  unavailable after `ensure_ollama()` fails, offer the user a remote fallback.
- Remote results should be clearly labeled `(remote — unranked, no quality signals)`.

**Files:** `scripts/search.py`, `SKILL.md` (Step 0 / error path).

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

### Add fallback for incremental update past IVF_THRESHOLD
**Priority: medium** *(review issue 9)*
At 50k+ vectors, incremental updates are blocked and a full rebuild is required.
There is no option to rebuild just the FAISS index from cached `embeddings.npy`
(avoiding the 60-min re-embed). Document as a known limitation; consider a
`--rebuild-index-only` mode.

**File:** `pipeline/incremental_update.py`.

---

### Deep crawler state file not atomic on Windows
**Priority: low**
`crawlers/skillsmp_deep_crawler.py` saves state via `tmp → os.replace()`. On
POSIX this is atomic; on Windows `os.replace()` can fail if the destination is
open. Low risk for current Linux CI usage, but worth noting.

---

## Developer Experience

### Document monorepo ID change as a breaking change
**Priority: medium** *(review issue 8)*
Skills in monorepos (e.g. `anthropics/skills`) now key their SHA256 `id` on
`skill_md_url` rather than `repo_url`. This silently changes IDs of existing
records. Add a migration note to PRD-005 or a CHANGELOG entry; consider a grace
period or explicit versioning for the ID scheme.

**File:** `pipeline/normalize.py:379`, `docs/prd/PRD-005-ci-cd-release.md`.

---

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

## Testing

### Crawler integration tests require live GitHub API
**Priority: medium**
Tests in `tests/test_integration.py` that touch crawlers hit the real GitHub
API, are `@pytest.mark.network`, and are excluded by default. Consider a
record/replay fixture (using `responses` or `pytest-recording`) so crawler
logic can be tested in CI without network access.

**File:** `tests/test_integration.py`, `requirements-dev.txt`.

---
