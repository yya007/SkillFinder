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

### SHA256 verification — force-bypass warning
**Priority: medium** *(from review issue 6, now fixed)*
`--force` skips SHA verification. Add a visible stderr warning when this path is
taken so it isn't silently used in scripts.

**File:** `scripts/update_index.py`, `run_update()`.

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

## Reliability

### Add CI cleanup steps on workflow failure
**Priority: low** *(review issue 14)*
Staging directories (`/tmp/prev-index`, `data/.new/`) are not pruned when a CI
phase fails mid-run. On a retry, stale cached files can cause confusing behavior.
Add a cleanup step with `if: always()`.

**File:** `.github/workflows/update-index.yml`.

### Add fallback for incremental update past IVF_THRESHOLD
**Priority: medium** *(review issue 9)*
At 50k+ vectors, incremental updates are blocked and a full rebuild is required.
There is no option to rebuild just the FAISS index from cached `embeddings.npy`
(avoiding the 60-min re-embed). Document as a known limitation; consider a
`--rebuild-index-only` mode.

**File:** `pipeline/incremental_update.py`.

---

## Developer Experience

### Document monorepo ID change as a breaking change
**Priority: medium** *(review issue 8)*
Skills in monorepos (e.g. `anthropics/skills`) now key their SHA256 `id` on
`skill_md_url` rather than `repo_url`. This silently changes IDs of existing
records. Add a migration note to PRD-005 or a CHANGELOG entry; consider a grace
period or explicit versioning for the ID scheme.

**File:** `pipeline/normalize.py:379`, `docs/prd/PRD-005-ci-cd-release.md`.

### Add progress output when auto-starting Ollama
**Priority: medium** *(review issue 10)*
`ensure_ollama()` polls silently for 10 seconds. Add a `print("Starting Ollama…")`
before the loop and a confirmation when ready.

**File:** `scripts/search.py`, `ensure_ollama()`.

### Report line numbers on JSON parse errors in incremental_update.py
**Priority: low** *(review issue 11)*
`json.loads(line)` in `load_existing_ids()` and `find_new_skills()` raises
uncontextualized exceptions on corrupt JSONL. Wrap in try/except and include line
numbers.

**File:** `pipeline/incremental_update.py`.

### Align SKILL.md example output with format_results() actual output
**Priority: low** *(review issue 12)*
The Step 5 example in `SKILL.md` uses inline formatting that differs from what
`--no-json` actually produces. Update one to match the other.

**Files:** `SKILL.md:73-87`, `scripts/search.py:335-345`.

### Document incremental_update.py in CLAUDE.md
**Priority: low** *(review issue 13)*
`CLAUDE.md` only lists the full three-step pipeline. Add a section explaining
incremental vs. full rebuild and when to use each.

**File:** `CLAUDE.md`.
