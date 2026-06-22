# Crawler Rate-Limit Strategy

**Status:** Proposed (2026-06-22)
**Context:** The crawl cannot fetch the full ~37k-skill corpus within GitHub's
rate limits. In CI this is fatal (see PRD-005 "Architecture amendment"); even
locally it makes a full crawl slow. This doc records where the API budget goes
and the options for cutting it, so the work can be picked off the backlog in
priority order.

## The constraint

GitHub limits per **authenticated user / token**:

| API | Limit |
|-----|-------|
| REST core (`/repos`, `/contents`, `/git/trees`, …) | **5,000 req/hr** |
| Search — code (`/search/code`) | **10 req/min** |
| Search — repos/commits (`/search/repositories`) | **30 req/min** |
| GraphQL | **5,000 points/hr** (separate pool; a 100-node query ≈ 1–2 points) |
| `raw.githubusercontent.com` | **not counted against the API quota** (CDN) |

The Actions `GITHUB_TOKEN` shares one 5,000/hr bucket across all five crawlers
running in parallel, and code search at 10/min forces 60s cooldowns. Fetching
~37k skills at ≥1 REST call each needs many hours of quota — impossible in a
runner, slow locally.

## Where the budget actually goes

Measured from run `27926298109` logs and the crawler code:

1. **SKILL.md content — the dominant cost.** `crawlers/base.py::fetch_skill_md`
   (line ~903) fetches each file via the **Contents API**
   (`/repos/{o}/{r}/contents/{path}`) and **tries up to three branches**
   (`default_branch` → `main` → `master`), so a single skill can cost **1–3**
   quota-charged calls. A moved/renamed file burns all three on 404s.
2. **Per-repo metadata.** `fetch_repo_metadata` (line ~629) → `/repos/{o}/{r}`
   for stars/pushed_at/default_branch — 1 call per repo.
3. **Discovery.** `search/code` in the skillsmp/topic/marketplace crawlers. Few
   calls, but the 10/min ceiling stalls the whole crawl in 60s cooldowns.
4. **Monorepo trees.** `git/trees/HEAD` (non-recursive) — 1 call per directory.

### Capabilities that already exist but are wasted

- **ETag conditional requests.** `fetch_repo_metadata_with_etag` (base.py ~658)
  and `github_get(..., etag=)` support `If-None-Match`; a **304 costs zero
  quota**. But ETags are **never persisted across runs**, and the bulk path
  calls the non-ETag `fetch_repo_metadata`.
- **Filter cache.** `load_filter_cache`/`add_to_filter_cache` skip repos
  previously *rejected* — a *negative* cache only. There is **no positive
  shared cache**, so the five crawlers independently re-fetch the same
  overlapping repos, and `normalize.py` only dedups *after* all the fetching.
- **`download_url` (= raw.githubusercontent.com, free).** Used only by
  `clawhub_crawler` (line ~109). The main `fetch_skill_md` path does not use it.

## Options (ranked by impact ÷ effort)

### ① Fetch file content via `raw.githubusercontent.com` — **high impact, low effort**
Move `fetch_skill_md` to `https://raw.githubusercontent.com/{repo}/{branch}/{path}`.
Raw is a CDN, unauthenticated, and **does not consume the 5,000/hr REST quota**.
Fall back to the Contents API only when needed (symlinks, private repos). Removes
the single largest cost center. Risk: raw gives no SHA/symlink metadata — keep an
API fallback for the symlink case (already handled in `fetch_skill_md`).

### ② Persist ETags + `pushed_at` across runs — **high impact, medium effort**
Store `{repo_url: (etag, pushed_at)}` in a cache file (committed, or restored via
`actions/cache`). On re-crawl: skip entirely when `pushed_at` is unchanged;
otherwise send `If-None-Match` → 304 = free. The ETag function already exists;
this is mostly persistence + wiring. Makes the steady-state incremental nearly
zero-quota — enough that **CI incremental crawling becomes viable**.

### ③ Dedup *before* fetch, shared positive cache — **high impact, medium effort**
Invert the pipeline: collect candidate repo URLs from all sources → dedup by
canonical URL → fetch each **once**. Today a repo found by marketplace + topic +
skillsmp is fetched 3×. Pair with a per-run in-memory (and optionally on-disk)
cache of fetched metadata/content keyed by canonical repo URL.

### ④ Cut `search/code` reliance — **medium impact, medium effort**
Cache the discovered repo list and run discovery with date filters
(`pushed:>last-run`) so search only surfaces *new* repos (few calls). Prefer
repo/topic search (30/min) over code search (10/min) where the query allows.
Removes the 60s-cooldown stalls that dominate wall-clock.

### ⑤ Batch metadata via GraphQL — **medium impact, medium effort**
Replace per-repo `/repos/{o}/{r}` REST calls with one GraphQL query for ≤100
repos (stars/pushed_at/default_branch), ~1–2 points of the *separate* 5,000-point
GraphQL pool. ~100× fewer metadata calls and a different quota bucket.

### ⑥ Monorepos: one recursive tree fetch instead of N — **medium impact, low-ish effort**
`git/trees/{sha}?recursive=1` (1 call) enumerates every `SKILL.md` path in a
monorepo; fetch each via raw (free, per ①). Replaces 1 Contents call per file.
Big for `anthropics/skills`-style repos.

### ⑦ Raise the ceiling: multiple tokens / GitHub App — **brute force, fallback**
REST is 5,000/hr *per token*; rotating N tokens ≈ N× headroom, a GitHub App
installation gets 15,000/hr. Search stays per-user. Ops overhead (secret
management); use only if ①–⑥ are insufficient.

### ⑧ Skip GitHub for some content: shallow `git clone` → read from disk — **niche**
A shallow clone reads `SKILL.md` from disk with **0 API calls**; already the
model for SkillHub (HTTP scrape). Good for a known set of large monorepos.

## Recommended sequencing

1. **① raw content** — biggest single reduction, smallest change.
2. **② ETag/`pushed_at` persistence** — makes weekly incrementals near-free.
3. **③ dedup-before-fetch** — removes cross-crawler redundancy.
4. **⑥ recursive tree** — stacks on ① for monorepos.
5. **⑤ GraphQL metadata** / **④ search reduction** — further headroom.
6. **⑦ multi-token** — only if still constrained after the above.

①+②+③ together plausibly cut API usage by an order of magnitude and could make
CI incremental crawling reliable, narrowing the cases that require a local full
rebuild.

## Affected code

- `crawlers/base.py` — `fetch_skill_md` (①, ⑥), `fetch_repo_metadata*` (②, ⑤),
  `github_get` (②), filter cache → positive cache (③).
- `crawlers/*_crawler.py` — discovery (④), call sites of the above.
- `pipeline/normalize.py` — dedup currently happens here, *after* fetch (③).
