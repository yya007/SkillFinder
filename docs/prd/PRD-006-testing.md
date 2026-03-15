# PRD-006 — Test Coverage & Quality Gates

## Status: Active

## Problem

The test suite had three blocking gaps:

1. **Merge conflicts in `scripts/search.py`** — git conflict markers caused a `SyntaxError` at import time, blocking all tests that depend on the search module (unit + integration).
2. **Missing `scripts/update_index.py`** — 32 tests in `test_update_index.py` could never run; the script existed only in a branch but was never merged to `main`.
3. **`pythonpath` not set in `pytest.ini`** — pytest could not resolve `from scripts.update_index import ...` even once the file existed, because the project root was not on `sys.path`.

After fixes: **350 tests pass, 0 failures** (20 deselected: `quality` and `network` marks require Ollama + real index).

---

## Test Architecture

```
tests/
├── conftest.py                        # shared fixtures (make_skill, tmp_data_dir, mock_ollama_embed)
├── crawlers/
│   ├── conftest.py
│   ├── test_base.py                   # URL normaliser, JSONL I/O, GitHub tree/search API, platform inference
│   ├── test_clawhub_crawler.py        # awesome-list parsing, monorepo dedup
│   ├── test_marketplace_crawler.py    # marketplace entry building, official skill marking
│   ├── test_skillhub_crawler.py       # HTML scraping, rank/score extraction
│   ├── test_skillsmp_crawler.py       # code-search sharding, filter cache, cross-shard dedup
│   └── test_dedup_across_sources.py   # URL variant dedup, multi-source merge
├── unit/
│   ├── test_normalize.py              # canonical_key, skill_id, build_embedding_text, quality filter
│   ├── test_embed.py                  # Ollama health check, embed_batch, checkpoint writing
│   ├── test_build_index.py            # L2 normalisation, IndexFlatIP vs IVFFlat, alignment check
│   ├── test_fetch_skill.py            # GitHub URL parsing, SKILL.md candidate URLs, fallbacks
│   ├── test_search.py                 # embed_query, load_index, apply_filters, format_results
│   └── test_update_index.py           # read_local_version, needs_update, verify_sha256,
│                                      # download_file, extract_artifact, parse_release_sha256, run_update
├── integration/
│   ├── test_pipeline.py               # normalize → embed → build_index end-to-end, row alignment
│   └── test_search_integration.py     # search against tiny in-memory FAISS index
└── quality/                           # @pytest.mark.quality — skipped without real index + Ollama
    └── test_search_quality.py         # Recall@30 ≥ 90% on 100-query labeled suite
```

### Markers

| Marker | Default | Requires |
|--------|---------|---------|
| *(none)* | Run | nothing |
| `slow` | Run | nothing |
| `network` | **Skip** | real network |
| `quality` | **Skip** | `data/index.faiss` + Ollama running |

Run all including skipped:
```bash
pytest -m ""                              # all 370 tests
pytest -m "not quality"                   # skip only quality benchmark
pytest -m "quality" --no-header -q       # quality benchmark only
```

---

## Coverage Targets

| Layer | Target | Rationale |
|-------|--------|-----------|
| `scripts/search.py` | ≥ 95% | Core user-facing path; every filter branch must be exercised |
| `scripts/update_index.py` | ≥ 90% | Atomic extraction and SHA mismatch paths are safety-critical |
| `scripts/fetch_skill.py` | ≥ 90% | Fallback URL logic affects every "fetch before install" flow |
| `crawlers/` | ≥ 80% | Crawlers are CI-only; happy path + rate-limit + dedup branches |
| `pipeline/` | ≥ 80% | Normalize/embed/build — alignment invariant must be tested |

Run coverage:
```bash
pytest --cov=scripts --cov=crawlers --cov=pipeline \
       --cov-report=term-missing --cov-report=html
```

---

## Gap Analysis (as of PRD-006)

### Covered
- All `scripts/` modules: search filtering, Ollama lifecycle, update orchestration, fetch fallbacks
- All crawler modules: URL normalisation, dedup, filter cache, rate-limit mock, cross-shard dedup
- Pipeline: normalize → embed → build_index row-alignment invariant
- Integration: tiny in-memory index built in `conftest.py`, exercised by search tests

### Not yet covered (backlog)

| Gap | Risk | Suggested test |
|-----|------|---------------|
| `skillsmp_deep_crawler.py` state persistence | Medium | Write state, simulate resume, verify skipped cells |
| `skillsmp_deep_crawler.py` overflow warning | Low | Mock `total_count >= 950`, assert WARNING logged |
| `pipeline/incremental_update.py` | High | Embed-model mismatch → raises; append-then-reload alignment |
| `pipeline/incremental_update.py` IVFFlat guard | High | Inject index with `ntotal >= IVF_THRESHOLD`, assert IncrementalError |
| `scripts/update_index.py` SHA mismatch path | Medium | `verify_sha256` returns False → status == "error" |
| `scripts/update_index.py` no tar.gz asset | Medium | Release body with only `.zip` asset → status == "error" |
| Multi-platform install path in `SKILL.md` | Low | Smoke-test the platform-default logic (needs agent harness) |

---

## Implementation Plan

### Phase 1 — Incremental update tests (priority: high)

File: `tests/unit/test_incremental_update.py`

Tests to add:
- `test_embed_model_mismatch_raises` — version.txt reports different model, `IncrementalError` raised
- `test_ivfflat_guard_raises` — inject index with `ntotal >= IVF_THRESHOLD`, assert `IncrementalError`
- `test_append_preserves_alignment` — append 3 new skills, verify `index.ntotal == len(metadata_rows)`
- `test_no_new_skills_is_noop` — all IDs already in index, `new_count == 0`, index unchanged

### Phase 2 — Deep crawler tests (priority: medium)

File: `tests/crawlers/test_skillsmp_deep_crawler.py`

Tests to add:
- `test_load_state_missing_file` — returns empty dict
- `test_save_and_reload_state` — round-trip through JSON
- `test_cell_marked_exhausted_when_under_threshold` — `date_shard_count < 950` → added to `exhausted_date_shards`
- `test_cell_not_marked_exhausted_at_overflow` — `date_shard_count >= 950` → not exhausted
- `test_resume_skips_exhausted_date_shards` — state file pre-loaded, exhausted shards produce no API calls
- `test_target_reached_stops_early` — cell_new hits target mid-shard, remaining date shards skipped

### Phase 3 — update_index edge cases (priority: medium)

Extend `tests/unit/test_update_index.py`:
- `test_sha_mismatch_returns_error` — patch `verify_sha256` → False, result status == "error"
- `test_no_tar_gz_asset_returns_error` — release with no `.tar.gz` asset, status == "error"
- `test_check_only_with_available_update` — returns status == "update_available", no download
- `test_extraction_path_traversal_rejected` — tar member with `../` prefix is ignored

### Phase 4 — Coverage enforcement in CI

Add to `.github/workflows/update-index.yml`:
```yaml
- name: Run tests with coverage
  run: |
    pytest --cov=scripts --cov=crawlers --cov=pipeline \
           --cov-fail-under=80 --cov-report=xml
- name: Upload coverage
  uses: codecov/codecov-action@v4
```

---

## Quality Benchmark

`tests/quality/test_search_quality.py` evaluates Recall@30 against 100 labeled queries in `tests/fixtures/test_queries.json`.

**Target:** Recall@30 ≥ 90% (the correct skill appears in the top-30 candidates for at least 90 of 100 queries).

Run manually (requires real index + Ollama):
```bash
ollama pull qwen3-embedding:0.6b
pytest tests/quality/ -v -m quality
```

This benchmark is intentionally excluded from CI to avoid the Ollama dependency in the build pipeline. It should be run locally after any change to the embedding strategy, index build, or query prefix.
