# Crawler Rate-Limit Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the GitHub API quota the crawlers consume (and prove the cut with numbers) by fetching SKILL.md content from the free `raw.githubusercontent.com` CDN, reusing unchanged metadata via persisted ETags, and skipping content re-fetches when a file's blob SHA is unchanged.

**Architecture:** All five crawlers share four functions in `crawlers/base.py` (`fetch_skill_md`, `fetch_repo_metadata`, `find_skill_md_paths`, `github_get`). We improve those shared functions in place so every crawler benefits without per-crawler rewrites. A lightweight, thread-safe request counter in `base.py` plus a standalone eval harness (`crawlers/eval_cost.py`) measure *metered API calls per skill* before and after each change, so impact is quantified, not assumed.

**Tech Stack:** Python ≥3.10, `requests`, `pytest`, `unittest.mock`. No new third-party dependencies.

## Global Constraints

- Python ≥3.10; ruff line length 100.
- `scripts/` must never import from `crawlers/` or `pipeline/` — the eval harness lives in `crawlers/`, not `scripts/`.
- Tests must not hit the network: mock `crawlers.base.github_get` or `session.get` with `unittest.mock` (existing pattern in `tests/crawlers/test_base.py`).
- Existing public signatures of `fetch_skill_md`, `fetch_repo_metadata`, `find_skill_md_paths` must keep working for current callers — extend with optional params, never break positional calls.
- Commit style: conventional commits; end every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Run tests with `.venv/bin/python -m pytest` (the repo's venv); lint with `.venv/bin/ruff check crawlers/`.
- Reference analysis: `docs/crawler-rate-limit-strategy.md`. This plan implements options ① (raw content), ② (ETag persistence), ③ (blob-SHA content dedup). Options ④/⑤/⑦ remain in `BACKLOG.md` and are out of scope here. Option ⑥ (recursive tree) already exists in `find_skill_md_paths`.

---

## File Structure

- `crawlers/base.py` — **modify.** Add request counters; add raw-content fetch; add persistent ETag metadata cache; add blob-SHA content cache. All shared by every crawler.
- `crawlers/eval_cost.py` — **create.** Standalone harness: run a fixed sample of real repos through the fetch path and print metered-calls-per-skill. Run manually with `GITHUB_TOKEN`.
- `tests/crawlers/test_base.py` — **modify.** Add unit tests for counters, raw fetch, ETag cache, content cache (network mocked).
- `tests/crawlers/test_eval_cost.py` — **create.** Unit test the harness's aggregation logic with a mocked fetch path (no network).
- `docs/crawler-rate-limit-strategy.md` — **modify (final task).** Append a "Measured impact" section with before/after numbers.

Cache files (created at runtime, git-ignored under `data/`):
- `data/crawl_state/repo_meta_cache.json` — `{repo_full_name: {etag, pushed_at, stargazers_count, default_branch, topics, description}}`.
- `data/crawl_state/content_cache.json` — `{blob_sha: content_string}` for SKILL.md blobs already fetched.

---

## Phase 0 — Instrumentation & baseline

### Task 1: Request counters in base.py

**Files:**
- Modify: `crawlers/base.py` (add counters near the top, after `GITHUB_API`; call from `github_get`)
- Test: `tests/crawlers/test_base.py`

**Interfaces:**
- Produces: `reset_api_counters() -> None`, `get_api_counters() -> dict` (keys: `rest`, `search`, `raw_free`, `conditional_304`, `graphql`), `record_request(url: str, status_code: int) -> None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/crawlers/test_base.py`:

```python
class TestApiCounters:
    def test_categorizes_requests_by_kind(self):
        from crawlers.base import (
            reset_api_counters, get_api_counters, record_request,
        )
        reset_api_counters()
        record_request("https://api.github.com/repos/a/b", 200)
        record_request("https://api.github.com/search/code?q=x", 200)
        record_request("https://raw.githubusercontent.com/a/b/main/SKILL.md", 200)
        record_request("https://api.github.com/repos/a/b", 304)
        counters = get_api_counters()
        assert counters["rest"] == 1
        assert counters["search"] == 1
        assert counters["raw_free"] == 1
        assert counters["conditional_304"] == 1

    def test_reset_zeroes_counters(self):
        from crawlers.base import reset_api_counters, get_api_counters, record_request
        record_request("https://api.github.com/repos/a/b", 200)
        reset_api_counters()
        assert get_api_counters()["rest"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/crawlers/test_base.py::TestApiCounters -v`
Expected: FAIL with `ImportError: cannot import name 'reset_api_counters'`.

- [ ] **Step 3: Write minimal implementation**

In `crawlers/base.py`, after the `GITHUB_API = "https://api.github.com"` line, add:

```python
import threading

_api_counter_lock = threading.Lock()
_API_COUNTERS = {"rest": 0, "search": 0, "raw_free": 0, "conditional_304": 0, "graphql": 0}


def reset_api_counters() -> None:
    """Zero all API-call counters (call at the start of a measured run)."""
    with _api_counter_lock:
        for key in _API_COUNTERS:
            _API_COUNTERS[key] = 0


def get_api_counters() -> dict:
    """Return a snapshot of the API-call counters by category."""
    with _api_counter_lock:
        return dict(_API_COUNTERS)


def record_request(url: str, status_code: int) -> None:
    """Categorize one HTTP request for cost accounting.

    304 (conditional) and raw.githubusercontent.com requests are FREE — they do
    not consume the 5,000/hr REST quota. /search/* is metered at 10-30/min.
    """
    with _api_counter_lock:
        if status_code == 304:
            _API_COUNTERS["conditional_304"] += 1
        elif "raw.githubusercontent.com" in url:
            _API_COUNTERS["raw_free"] += 1
        elif "/graphql" in url:
            _API_COUNTERS["graphql"] += 1
        elif "/search/" in url:
            _API_COUNTERS["search"] += 1
        else:
            _API_COUNTERS["rest"] += 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/crawlers/test_base.py::TestApiCounters -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add crawlers/base.py tests/crawlers/test_base.py
git commit -m "feat(crawlers): add API-call counters for cost measurement

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2: Wire counters into github_get and add the eval harness

**Files:**
- Modify: `crawlers/base.py` (`github_get` — call `record_request` after each response)
- Create: `crawlers/eval_cost.py`
- Test: `tests/crawlers/test_eval_cost.py`

**Interfaces:**
- Consumes: `reset_api_counters`, `get_api_counters`, `fetch_repo_metadata`, `find_skill_md_paths`, `fetch_skill_md`.
- Produces: `crawlers.eval_cost.measure(session, repos: list[str]) -> dict` returning `{"skills": int, "metered": int, "free": int, "per_skill": float, "counters": dict}`.

- [ ] **Step 1: Write the failing test**

Create `tests/crawlers/test_eval_cost.py`:

```python
from unittest.mock import patch
from crawlers.eval_cost import measure


def test_measure_aggregates_per_skill_cost():
    # 2 repos, 1 SKILL.md each → 2 skills.
    with patch("crawlers.eval_cost.fetch_repo_metadata", return_value={"default_branch": "main"}), \
         patch("crawlers.eval_cost.find_skill_md_paths", return_value={"SKILL.md": "sha1"}), \
         patch("crawlers.eval_cost.fetch_skill_md", return_value="---\nname: x\n---\n"), \
         patch("crawlers.eval_cost.reset_api_counters"), \
         patch("crawlers.eval_cost.get_api_counters",
               return_value={"rest": 6, "search": 0, "raw_free": 2,
                             "conditional_304": 0, "graphql": 0}):
        result = measure(session=object(), repos=["a/b", "c/d"])
    assert result["skills"] == 2
    assert result["metered"] == 6          # rest + search
    assert result["free"] == 2             # raw_free + conditional_304
    assert result["per_skill"] == 3.0      # 6 metered / 2 skills
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/crawlers/test_eval_cost.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crawlers.eval_cost'`.

- [ ] **Step 3: Write minimal implementation**

Create `crawlers/eval_cost.py`:

```python
"""Measure the GitHub API cost of crawling a fixed sample of repos.

Run a representative set of repos through the shared fetch path and report
*metered API calls per skill*, so the effect of rate-limit optimisations can be
compared before/after. Hits the live API — needs GITHUB_TOKEN.

Usage:
    GITHUB_TOKEN=$(gh auth token) python -m crawlers.eval_cost
"""
from __future__ import annotations

import os
import sys

from crawlers.base import (
    fetch_repo_metadata,
    fetch_skill_md,
    find_skill_md_paths,
    get_api_counters,
    make_session,
    reset_api_counters,
)

# A fixed, representative sample: a large monorepo, a few single-skill repos.
SAMPLE_REPOS = [
    "anthropics/skills",
    "anthropics/claude-cookbooks",
    "dnakov/claude-skills",
    "obra/superpowers",
    "wong2/awesome-claude-skills",
]


def measure(session, repos: list[str]) -> dict:
    """Run repos through the fetch path under fresh counters; return a summary."""
    reset_api_counters()
    skills = 0
    for repo in repos:
        meta = fetch_repo_metadata(session, repo)
        paths = find_skill_md_paths(session, repo)
        for path in paths:
            content = fetch_skill_md(
                session, repo, path, meta.get("default_branch", "main")
            )
            if content:
                skills += 1
    counters = get_api_counters()
    metered = counters["rest"] + counters["search"]
    free = counters["raw_free"] + counters["conditional_304"]
    return {
        "skills": skills,
        "metered": metered,
        "free": free,
        "per_skill": round(metered / skills, 2) if skills else 0.0,
        "counters": counters,
    }


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Set GITHUB_TOKEN (e.g. GITHUB_TOKEN=$(gh auth token))", file=sys.stderr)
        return 1
    session = make_session(token)
    result = measure(session, SAMPLE_REPOS)
    print(
        f"skills={result['skills']}  metered={result['metered']}  "
        f"free={result['free']}  per_skill={result['per_skill']}\n"
        f"counters={result['counters']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Then wire the counter into `github_get`: find the response-handling block (around line 156-210, where `resp` holds the final response) and add a `record_request(url, resp.status_code)` call immediately after the request returns a usable response. Concretely, after the `for attempt, delay in enumerate(...)` loop obtains `resp` from `session.get(...)`, add `record_request(url, resp.status_code)` right after each `resp = session.get(...)` assignment inside `github_get`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/crawlers/test_eval_cost.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Capture the BASELINE measurement**

Run: `GITHUB_TOKEN=$(gh auth token) .venv/bin/python -m crawlers.eval_cost`
Record the printed `per_skill` and `counters` line in the PR description as "baseline (before ①②③)". Expected baseline: `raw_free=0`, `per_skill` ≈ 1.5–3 (Contents API + multi-branch fallback).

- [ ] **Step 6: Commit**

```bash
git add crawlers/base.py crawlers/eval_cost.py tests/crawlers/test_eval_cost.py
git commit -m "feat(crawlers): eval harness for per-skill API cost + github_get accounting

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 1 — Option ①: free content via raw.githubusercontent.com

### Task 3: Add `_fetch_skill_md_via_raw` and route `fetch_skill_md` through it

**Files:**
- Modify: `crawlers/base.py` (`fetch_skill_md` at line 864; add `RAW_BASE` const and `_fetch_skill_md_via_raw`; rename current body to `_fetch_skill_md_via_api`)
- Test: `tests/crawlers/test_base.py`

**Interfaces:**
- Consumes: `record_request`, `parse_frontmatter`, existing `decode_b64_utf8`.
- Produces: unchanged public signature `fetch_skill_md(session, repo_full_name, path="SKILL.md", default_branch="main", _depth=0) -> str | None`. New helper `_fetch_skill_md_via_raw(session, repo_full_name, path, default_branch) -> str | None`.

Behaviour: try raw first; if raw returns a body that looks like a real skill file (contains `---` frontmatter OR is longer than 200 chars), return it; otherwise fall back to the Contents-API implementation (which resolves symlinks and private repos).

- [ ] **Step 1: Write the failing tests**

Add to `tests/crawlers/test_base.py`:

```python
class TestFetchSkillMdRaw:
    def test_uses_raw_and_skips_api_on_success(self):
        from crawlers.base import fetch_skill_md
        raw_body = "---\nname: deploy\ndescription: x\n---\nbody"

        class Resp:
            status_code = 200
            text = raw_body

        mock_session = MagicMock()
        mock_session.get.return_value = Resp()
        with patch("crawlers.base.github_get") as mock_api:
            content = fetch_skill_md(mock_session, "user/repo", "SKILL.md", "main")

        assert content == raw_body
        mock_api.assert_not_called()              # Contents API never touched

    def test_falls_back_to_api_when_raw_404(self):
        from crawlers.base import fetch_skill_md

        class Resp404:
            status_code = 404
            text = "404: Not Found"

        mock_session = MagicMock()
        mock_session.get.return_value = Resp404()
        with patch("crawlers.base.github_get") as mock_api:
            mock_api.return_value = {"content": "LS0tCm5hbWU6IHkKLS0t"}  # base64 "---\nname: y\n---"
            content = fetch_skill_md(mock_session, "user/repo", "SKILL.md", "main")

        assert content is not None and "name: y" in content
        mock_api.assert_called()                  # fell back to API

    def test_falls_back_to_api_when_raw_looks_like_symlink(self):
        from crawlers.base import fetch_skill_md

        class RespSymlink:
            status_code = 200
            text = "../shared/SKILL.md"          # short, no frontmatter → suspicious

        mock_session = MagicMock()
        mock_session.get.return_value = RespSymlink()
        with patch("crawlers.base.github_get") as mock_api:
            mock_api.return_value = {"content": "LS0tCm5hbWU6IHoKLS0t"}  # "---\nname: z\n---"
            content = fetch_skill_md(mock_session, "user/repo", "SKILL.md", "main")

        assert content is not None and "name: z" in content
        mock_api.assert_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/crawlers/test_base.py::TestFetchSkillMdRaw -v`
Expected: FAIL — `test_uses_raw_and_skips_api_on_success` fails because the current `fetch_skill_md` calls `github_get` (the API) and ignores `session.get`.

- [ ] **Step 3: Write the implementation**

In `crawlers/base.py`, add near `GITHUB_API`:

```python
RAW_BASE = "https://raw.githubusercontent.com"
```

Rename the existing `fetch_skill_md` function body to a private helper by renaming `def fetch_skill_md(` (line 864) to `def _fetch_skill_md_via_api(` and its internal recursive self-call (line 925, `return fetch_skill_md(`) to `return _fetch_skill_md_via_api(`. Leave its logic otherwise unchanged.

Then add the raw helper and the new public `fetch_skill_md`:

```python
def _looks_like_skill_file(text: str) -> bool:
    """True if raw body is plausibly a real SKILL.md (not a symlink target/404)."""
    if "---" in text:               # has YAML frontmatter delimiter
        return True
    return len(text) > 200          # long enough to be real content, not a path


def _fetch_skill_md_via_raw(session, repo_full_name, path, default_branch="main") -> str | None:
    """Fetch SKILL.md from the free raw CDN. Returns None on any non-200 / miss."""
    branches = [default_branch]
    for fallback in ("main", "master"):
        if fallback not in branches:
            branches.append(fallback)
    for branch in branches:
        url = f"{RAW_BASE}/{repo_full_name}/{branch}/{path}"
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException:
            continue
        record_request(url, resp.status_code)
        if resp.status_code == 200 and _looks_like_skill_file(resp.text):
            return resp.text
    return None


def fetch_skill_md(
    session,
    repo_full_name: str,
    path: str = "SKILL.md",
    default_branch: str = "main",
    _depth: int = 0,
) -> str | None:
    """Fetch SKILL.md content, preferring the free raw CDN over the Contents API.

    raw.githubusercontent.com does not consume the 5,000/hr REST quota. The
    Contents API is used only as a fallback (raw miss, or a body that looks like
    a symlink target rather than a real skill file), where it also resolves
    symlinks and private repos.
    """
    raw = _fetch_skill_md_via_raw(session, repo_full_name, path, default_branch)
    if raw is not None:
        return raw
    return _fetch_skill_md_via_api(session, repo_full_name, path, default_branch, _depth)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/crawlers/test_base.py::TestFetchSkillMdRaw tests/crawlers/test_base.py::TestFetchSkillMd -v`
Expected: PASS (new tests + existing `TestFetchSkillMd` still green).

- [ ] **Step 5: Run the full crawler test suite + lint**

Run: `.venv/bin/python -m pytest tests/crawlers/ -q && .venv/bin/ruff check crawlers/`
Expected: all pass, lint clean.

- [ ] **Step 6: Measure ① impact**

Run: `GITHUB_TOKEN=$(gh auth token) .venv/bin/python -m crawlers.eval_cost`
Expected: `raw_free` now > 0 and `per_skill` (metered) dropped sharply versus baseline. Record the number.

- [ ] **Step 7: Commit**

```bash
git add crawlers/base.py tests/crawlers/test_base.py
git commit -m "perf(crawlers): fetch SKILL.md from raw CDN, API only as fallback

raw.githubusercontent.com does not consume the 5,000/hr REST quota; the
Contents API is kept as a fallback for symlinks/private repos. Eliminates
the dominant per-skill quota cost (option (1) of the rate-limit strategy).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 2 — Option ②: persistent ETag metadata cache

### Task 4: Load/save the metadata cache

**Files:**
- Modify: `crawlers/base.py` (add cache I/O helpers near the filter-cache block ~line 565)
- Test: `tests/crawlers/test_base.py`

**Interfaces:**
- Produces: `load_meta_cache(path: str) -> dict`, `save_meta_cache(cache: dict, path: str) -> None`. Cache shape: `{repo_full_name: {"etag": str, "pushed_at": str, "stargazers_count": int, "topics": list, "description": str, "default_branch": str}}`.

- [ ] **Step 1: Write the failing test**

```python
class TestMetaCacheIO:
    def test_roundtrip(self, tmp_path):
        from crawlers.base import load_meta_cache, save_meta_cache
        p = str(tmp_path / "repo_meta_cache.json")
        cache = {"a/b": {"etag": "W/\"x\"", "pushed_at": "2026-01-01T00:00:00Z",
                          "stargazers_count": 10, "topics": [], "description": "",
                          "default_branch": "main"}}
        save_meta_cache(cache, p)
        assert load_meta_cache(p) == cache

    def test_missing_file_returns_empty_dict(self, tmp_path):
        from crawlers.base import load_meta_cache
        assert load_meta_cache(str(tmp_path / "nope.json")) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/crawlers/test_base.py::TestMetaCacheIO -v`
Expected: FAIL with `ImportError: cannot import name 'load_meta_cache'`.

- [ ] **Step 3: Write the implementation**

Add to `crawlers/base.py`:

```python
def load_meta_cache(path: str) -> dict:
    """Load the persistent repo-metadata/ETag cache, or {} if absent/corrupt."""
    import pathlib
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_meta_cache(cache: dict, path: str) -> None:
    """Persist the repo-metadata/ETag cache atomically."""
    import pathlib
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)
    tmp.replace(p)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/crawlers/test_base.py::TestMetaCacheIO -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add crawlers/base.py tests/crawlers/test_base.py
git commit -m "feat(crawlers): persistent repo-metadata/ETag cache I/O

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 5: `fetch_repo_metadata_cached` — reuse unchanged metadata via 304

**Files:**
- Modify: `crawlers/base.py` (new function using existing `fetch_repo_metadata_with_etag`)
- Test: `tests/crawlers/test_base.py`

**Interfaces:**
- Consumes: `fetch_repo_metadata_with_etag(session, repo, etag) -> (dict|None, str|None)`, `record_request`.
- Produces: `fetch_repo_metadata_cached(session, repo_full_name, cache: dict) -> dict`. Mutates `cache[repo_full_name]` in place; returns the metadata dict (same shape as `fetch_repo_metadata`).

- [ ] **Step 1: Write the failing tests**

```python
class TestFetchRepoMetadataCached:
    def test_returns_cached_on_304(self):
        from crawlers.base import fetch_repo_metadata_cached
        cache = {"a/b": {"etag": "W/\"v1\"", "stargazers_count": 42,
                         "pushed_at": "2026-01-01T00:00:00Z", "topics": [],
                         "description": "", "default_branch": "main"}}
        with patch("crawlers.base.fetch_repo_metadata_with_etag",
                   return_value=(None, "W/\"v1\"")) as mock_fetch:
            meta = fetch_repo_metadata_cached(MagicMock(), "a/b", cache)
        mock_fetch.assert_called_once()
        assert mock_fetch.call_args.args[1] == "a/b"      # called with the repo
        assert mock_fetch.call_args.args[2] == "W/\"v1\""  # ...and its cached etag
        assert meta["stargazers_count"] == 42             # served from cache, no re-download

    def test_updates_cache_on_200(self):
        from crawlers.base import fetch_repo_metadata_cached
        cache = {}
        fresh = {"stargazers_count": 7, "pushed_at": "2026-02-02T00:00:00Z",
                 "topics": ["ai"], "description": "d", "default_branch": "main"}
        with patch("crawlers.base.fetch_repo_metadata_with_etag",
                   return_value=(fresh, "W/\"v2\"")):
            meta = fetch_repo_metadata_cached(MagicMock(), "c/d", cache)
        assert meta["stargazers_count"] == 7
        assert cache["c/d"]["etag"] == "W/\"v2\""
        assert cache["c/d"]["stargazers_count"] == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/crawlers/test_base.py::TestFetchRepoMetadataCached -v`
Expected: FAIL with `ImportError: cannot import name 'fetch_repo_metadata_cached'`.

- [ ] **Step 3: Write the implementation**

```python
def fetch_repo_metadata_cached(session, repo_full_name: str, cache: dict) -> dict:
    """Like fetch_repo_metadata, but uses a persistent ETag cache.

    On a 304 (resource unchanged) the cached metadata is returned and NO quota is
    spent on the body. ``cache`` is mutated in place; persist it with
    save_meta_cache() after the crawl.
    """
    entry = cache.get(repo_full_name, {})
    etag = entry.get("etag")
    meta, new_etag = fetch_repo_metadata_with_etag(session, repo_full_name, etag)

    if meta is None:  # 304 Not Modified — reuse cached metadata
        return {k: v for k, v in entry.items() if k != "etag"}

    if meta:  # 200 — refresh cache
        cache[repo_full_name] = {
            "etag": new_etag or "",
            "pushed_at": meta.get("pushed_at", ""),
            "stargazers_count": meta.get("stargazers_count", 0),
            "topics": meta.get("topics", []),
            "description": meta.get("description", ""),
            "default_branch": meta.get("default_branch", "main"),
        }
    return meta
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/crawlers/test_base.py::TestFetchRepoMetadataCached -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add crawlers/base.py tests/crawlers/test_base.py
git commit -m "perf(crawlers): ETag-cached repo metadata (304 = zero quota)

Wires the existing fetch_repo_metadata_with_etag into a persistent cache so
re-crawls of unchanged repos cost nothing (option (2)).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3 — Option ③: blob-SHA content cache

### Task 6: `fetch_skill_md_cached` — skip fetch when the blob SHA is unchanged

**Files:**
- Modify: `crawlers/base.py` (new wrapper around `fetch_skill_md`)
- Test: `tests/crawlers/test_base.py`

**Interfaces:**
- Consumes: `fetch_skill_md`, the `{path: blob_sha}` map from `find_skill_md_paths`.
- Produces: `fetch_skill_md_cached(session, repo_full_name, path, blob_sha, default_branch, content_cache: dict) -> str | None`. `content_cache` maps `blob_sha -> content`. When `blob_sha` is non-empty and present, returns cached content with **no** network call (works across crawlers and across runs because the blob SHA uniquely identifies content).

- [ ] **Step 1: Write the failing tests**

```python
class TestFetchSkillMdCached:
    def test_cache_hit_skips_fetch(self):
        from crawlers.base import fetch_skill_md_cached
        cache = {"sha123": "---\nname: cached\n---"}
        with patch("crawlers.base.fetch_skill_md") as mock_fetch:
            content = fetch_skill_md_cached(
                MagicMock(), "u/r", "SKILL.md", "sha123", "main", cache)
        assert content == "---\nname: cached\n---"
        mock_fetch.assert_not_called()

    def test_cache_miss_fetches_and_stores(self):
        from crawlers.base import fetch_skill_md_cached
        cache = {}
        with patch("crawlers.base.fetch_skill_md", return_value="---\nname: new\n---"):
            content = fetch_skill_md_cached(
                MagicMock(), "u/r", "SKILL.md", "sha999", "main", cache)
        assert content == "---\nname: new\n---"
        assert cache["sha999"] == "---\nname: new\n---"

    def test_empty_sha_always_fetches(self):
        # Code-search fallback yields "" SHAs — never cache-key on empty.
        from crawlers.base import fetch_skill_md_cached
        cache = {"": "WRONG"}
        with patch("crawlers.base.fetch_skill_md", return_value="real") as mock_fetch:
            content = fetch_skill_md_cached(MagicMock(), "u/r", "SKILL.md", "", "main", cache)
        assert content == "real"
        mock_fetch.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/crawlers/test_base.py::TestFetchSkillMdCached -v`
Expected: FAIL with `ImportError: cannot import name 'fetch_skill_md_cached'`.

- [ ] **Step 3: Write the implementation**

```python
def fetch_skill_md_cached(
    session,
    repo_full_name: str,
    path: str,
    blob_sha: str,
    default_branch: str,
    content_cache: dict,
) -> str | None:
    """Fetch SKILL.md, reusing cached content when the blob SHA is unchanged.

    A git blob SHA uniquely identifies file content, so a hit means the file is
    byte-identical to a previously-fetched one (in this repo, another repo, or a
    prior run) — return it with no network call. Empty SHAs (code-search
    fallback) are never cached. ``content_cache`` maps blob_sha -> content.
    """
    if blob_sha and blob_sha in content_cache:
        return content_cache[blob_sha]
    content = fetch_skill_md(session, repo_full_name, path, default_branch)
    if blob_sha and content is not None:
        content_cache[blob_sha] = content
    return content
```

Also add JSON I/O for the content cache (reuse the same pattern as Task 4) — add `load_content_cache(path)` / `save_content_cache(cache, path)` as thin aliases that call `load_meta_cache` / `save_meta_cache` (same JSON-dict shape), or duplicate the two small functions with content-specific names for clarity:

```python
def load_content_cache(path: str) -> dict:
    """Load the persistent blob-SHA -> content cache, or {} if absent/corrupt."""
    return load_meta_cache(path)


def save_content_cache(cache: dict, path: str) -> None:
    """Persist the blob-SHA -> content cache atomically."""
    save_meta_cache(cache, path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/crawlers/test_base.py::TestFetchSkillMdCached -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add crawlers/base.py tests/crawlers/test_base.py
git commit -m "perf(crawlers): blob-SHA content cache to skip unchanged fetches

A git blob SHA identifies content uniquely, so repeated/unchanged SKILL.md
files (across crawlers and runs) are served from cache with no fetch (option
(3)).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 4 — Reference integration in one crawler

### Task 7: Wire the caches through the topic crawler end-to-end

**Files:**
- Modify: `crawlers/topic_crawler.py` (the fetch loop around lines 280-321)
- Test: `tests/crawlers/test_topic_crawler.py`

**Interfaces:**
- Consumes: `load_meta_cache`, `save_meta_cache`, `fetch_repo_metadata_cached`, `load_content_cache`, `save_content_cache`, `fetch_skill_md_cached`, `find_skill_md_paths`.
- Produces: topic crawler that loads both caches at start, uses the cached fetchers, and saves both caches at the end. Cache paths default under `data/crawl_state/`.

This task is the template the other four crawlers follow (skillsmp, skillhub, clawhub, marketplace — call sites listed in the strategy doc). Do them as follow-up commits using the same pattern; this task proves the integration and its measurement.

- [ ] **Step 1: Write the failing test**

In `tests/crawlers/test_topic_crawler.py`, add a test that the crawl loads/saves caches and uses `fetch_skill_md_cached`. Match the file's existing mocking style (inspect the top of `tests/crawlers/test_topic_crawler.py` first). Skeleton:

```python
def test_topic_crawl_uses_caches(tmp_path, monkeypatch):
    import crawlers.topic_crawler as tc
    calls = {"saved_meta": 0, "saved_content": 0}
    monkeypatch.setattr(tc, "load_meta_cache", lambda p: {})
    monkeypatch.setattr(tc, "load_content_cache", lambda p: {})
    monkeypatch.setattr(tc, "save_meta_cache",
                        lambda c, p: calls.__setitem__("saved_meta", calls["saved_meta"] + 1))
    monkeypatch.setattr(tc, "save_content_cache",
                        lambda c, p: calls.__setitem__("saved_content", calls["saved_content"] + 1))
    monkeypatch.setattr(tc, "fetch_repo_metadata_cached",
                        lambda s, r, c: {"stargazers_count": 50, "default_branch": "main",
                                         "pushed_at": "", "topics": [], "description": ""})
    monkeypatch.setattr(tc, "find_skill_md_paths", lambda s, r: {"SKILL.md": "sha1"})
    monkeypatch.setattr(tc, "fetch_skill_md_cached", lambda *a, **k: "---\nname: t\n---")
    # ... drive a minimal crawl over one discovered repo (use the file's existing
    #     discovery-mock helper) and assert both caches were saved once.
    assert calls["saved_meta"] == 1
    assert calls["saved_content"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/crawlers/test_topic_crawler.py::test_topic_crawl_uses_caches -v`
Expected: FAIL (crawler doesn't import/use the cache functions yet).

- [ ] **Step 3: Write the implementation**

In `crawlers/topic_crawler.py`:
1. Add imports: `from crawlers.base import (load_meta_cache, save_meta_cache, fetch_repo_metadata_cached, load_content_cache, save_content_cache, fetch_skill_md_cached, find_skill_md_paths)`.
2. At the start of the crawl function, load both caches:
   ```python
   meta_cache = load_meta_cache("data/crawl_state/repo_meta_cache.json")
   content_cache = load_content_cache("data/crawl_state/content_cache.json")
   ```
3. Replace `meta = fetch_repo_metadata(session, full_name)` (line ~280) with
   `meta = fetch_repo_metadata_cached(session, full_name, meta_cache)`.
4. Replace the per-path content fetch (line ~321, `fetch_skill_md(...)`) with
   `fetch_skill_md_cached(session, full_name, path, skill_md_paths[path], meta.get("default_branch", "main"), content_cache)`
   (the `{path: blob_sha}` map is already returned by `find_skill_md_paths`).
5. After the crawl loop finishes (before returning), persist both caches:
   ```python
   save_meta_cache(meta_cache, "data/crawl_state/repo_meta_cache.json")
   save_content_cache(content_cache, "data/crawl_state/content_cache.json")
   ```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/crawlers/test_topic_crawler.py -q`
Expected: PASS (new test + existing topic tests green).

- [ ] **Step 5: Full suite + lint**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/ruff check crawlers/`
Expected: all green, lint clean.

- [ ] **Step 6: Commit**

```bash
git add crawlers/topic_crawler.py tests/crawlers/test_topic_crawler.py
git commit -m "perf(topic-crawler): use ETag + blob-SHA caches end-to-end

Reference integration of options (2)/(3); the other four crawlers follow the
same pattern.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 5 — Evaluation & write-up

### Task 8: Measure end-to-end and record the impact

**Files:**
- Modify: `docs/crawler-rate-limit-strategy.md` (append a "Measured impact" section)

- [ ] **Step 1: Cold-cache run (first run, no caches)**

```bash
rm -f data/crawl_state/repo_meta_cache.json data/crawl_state/content_cache.json
GITHUB_TOKEN=$(gh auth token) .venv/bin/python -m crawlers.eval_cost
```
Record `metered` / `per_skill` / `counters`. Expected: `raw_free` dominant, `rest` only for metadata + tree.

- [ ] **Step 2: Warm-cache run (immediately re-run)**

```bash
GITHUB_TOKEN=$(gh auth token) .venv/bin/python -m crawlers.eval_cost
```

> The eval harness uses the raw fetch and metadata as-is; to exercise the caches in the harness, temporarily point `measure()` at `fetch_repo_metadata_cached` / `fetch_skill_md_cached` with a persisted cache, OR measure via a real topic-crawler `--mode incremental` run with `reset_api_counters()`/`get_api_counters()` wrapped around it. Record the warm-cache `metered`/`per_skill`. Expected: `conditional_304` > 0 and metered calls drop further on the second run.

- [ ] **Step 3: Write the results**

Append to `docs/crawler-rate-limit-strategy.md`:

```markdown
## Measured impact (2026-06-22)

Sample: `crawlers/eval_cost.py` over SAMPLE_REPOS.

| Stage | metered calls/skill | free calls/skill | notes |
|-------|--------------------:|-----------------:|-------|
| Baseline (Contents API)        | <fill> | 0      | 1–3 calls/skill, multi-branch |
| ① raw content                  | <fill> | <fill> | content now free |
| ②+③ caches, cold run           | <fill> | <fill> | metadata + tree only |
| ②+③ caches, warm run           | <fill> | <fill> | 304s + SHA hits |

Conclusion: <one line on the reduction factor and whether CI incremental
crawling is now within budget>.
```

Fill every `<fill>` from Steps 1-2.

- [ ] **Step 4: Commit**

```bash
git add docs/crawler-rate-limit-strategy.md
git commit -m "docs: record measured impact of crawler rate-limit optimisations

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 9: Open the PR

- [ ] **Step 1: Push and open PR**

```bash
git push -u origin perf/crawler-rate-limit-efficiency
gh pr create --base master \
  --title "perf(crawlers): raw-CDN content + ETag/blob-SHA caches (rate-limit options ①②③)" \
  --body "Implements options ①②③ from docs/crawler-rate-limit-strategy.md with a measurement harness. See the 'Measured impact' table for before/after. Tests: \`pytest tests/crawlers\` green; lint clean. Verification includes baseline vs cold vs warm eval runs."
```

- [ ] **Step 2: Self-review the staged diff** (`git diff master...HEAD`) and run `/code-review` before requesting review.

---

## Follow-on (not in this plan — already in BACKLOG.md)

- Wire the caches into the remaining four crawlers (skillsmp, skillhub, clawhub, marketplace) using Task 7 as the template.
- Option ④ (reduce search/code via date filters), ⑤ (GraphQL batched metadata), ⑦ (multi-token / GitHub App).
- Consider whether the warm-cache numbers make **CI incremental crawling** viable again (would relax the PRD-005 "incremental-only is dormant on IVF" constraint).
