# PRD-006 — Test Coverage & Quality Gates

## Status: Active

---

## Overview

SkillFinder maintains a layered test suite that covers the full pipeline — from crawlers through
normalization, embedding, index build, and search — using only local tools (no network, no Ollama)
in the standard `pytest` run.

Tests are organized into three categories:

- **Unit tests** (`tests/unit/`): pure-logic tests, all external I/O patched.
- **Crawler tests** (`tests/crawlers/`): crawler modules with GitHub API calls mocked.
- **Integration tests** (`tests/integration/`): end-to-end pipeline with a tiny in-memory FAISS index.
- **Quality benchmarks** (`tests/quality/`): Recall@30 evaluation requiring a real index + Ollama
  (excluded from CI).

---

## Test Architecture

```
tests/
├── conftest.py                        # shared fixtures (make_skill, tmp_data_dir, mock_ollama_embed)
├── crawlers/
│   ├── conftest.py                    # make_github_repo, mock_session, SAMPLE_SKILL_MD
│   ├── test_base.py                   # URL normaliser, JSONL I/O, GitHub tree/search API, platform inference
│   ├── test_clawhub_crawler.py        # awesome-list parsing, monorepo dedup
│   ├── test_marketplace_crawler.py    # marketplace entry building, official skill marking
│   ├── test_skillhub_crawler.py       # HTML scraping, rank/score extraction
│   ├── test_skillsmp_crawler.py       # code-search sharding, filter cache, cross-shard dedup
│   ├── test_skillsmp_deep_crawler.py  # state persistence, exhaustion logic, resume, early stop
│   └── test_dedup_across_sources.py   # URL variant dedup, multi-source merge
├── unit/
│   ├── test_normalize.py              # canonical_key, skill_id, build_embedding_text, quality filter
│   ├── test_embed.py                  # Ollama health check, embed_batch, checkpoint writing
│   ├── test_build_index.py            # L2 normalisation, IndexFlatIP vs IVFFlat, alignment check
│   ├── test_fetch_skill.py            # GitHub URL parsing, SKILL.md candidate URLs, fallbacks
│   ├── test_incremental_update.py     # model-mismatch guard, IVFFlat guard, append alignment
│   ├── test_search.py                 # embed_query, load_index, apply_filters, format_results
│   └── test_update_index.py           # read_local_version, needs_update, verify_sha256,
│                                      # download_file, extract_artifact, parse_release_sha256, run_update
├── integration/
│   ├── test_pipeline.py               # normalize → embed → build_index end-to-end, row alignment
│   └── test_search_integration.py     # search against tiny in-memory FAISS index
└── quality/                           # @pytest.mark.quality — skipped without real index + Ollama
    └── test_search_quality.py         # Recall@30 ≥ 90% on 100-query labeled suite
```

### Key fixtures

| Fixture | File | Purpose |
|---------|------|---------|
| `make_skill(...)` | `conftest.py` | Build a fully-populated unified skill record |
| `tmp_data_dir` | `conftest.py` | Temp directory with a tiny 3-skill FAISS index |
| `mock_ollama_embed` | `conftest.py` | Deterministic embed_batch stub |
| `mock_session` | `crawlers/conftest.py` | `MagicMock` session for crawler unit tests |
| `make_github_repo(...)` | `crawlers/conftest.py` | Fake GitHub API repo dict |

---

## Running Tests

```bash
# Standard run — unit + crawler + integration (no Ollama, no network)
pytest

# Verbose with test names
pytest -v

# Run a specific file
pytest tests/unit/test_incremental_update.py -v

# Run all marks including network (requires GitHub token)
pytest -m ""

# Quality benchmark only (requires data/index.faiss + Ollama running)
ollama pull qwen3-embedding:0.6b
pytest tests/quality/ -v -m quality

# Coverage report
pytest --cov=scripts --cov=crawlers --cov=pipeline \
       --cov-report=term-missing --cov-report=html
```

---

## Markers

| Marker | Default | Requires |
|--------|---------|---------|
| *(none)* | Run | nothing |
| `slow` | Run | nothing |
| `network` | **Skip** | real network |
| `quality` | **Skip** | `data/index.faiss` + Ollama running |

---

## Coverage Targets

| Layer | Target | Rationale |
|-------|--------|-----------|
| `scripts/search.py` | ≥ 95% | Core user-facing path; every filter branch must be exercised |
| `scripts/update_index.py` | ≥ 90% | Atomic extraction and SHA mismatch paths are safety-critical |
| `scripts/fetch_skill.py` | ≥ 90% | Fallback URL logic affects every "fetch before install" flow |
| `crawlers/` | ≥ 80% | Crawlers are CI-only; happy path + rate-limit + dedup branches |
| `pipeline/` | ≥ 80% | Normalize/embed/build — alignment invariant must be tested |

---

## Remaining Gaps

| Gap | Risk | Suggested test |
|-----|------|---------------|
| `pipeline/incremental_update.py` — version.txt missing (no model check) | Low | Verify warning logged, update proceeds |
| `crawlers/skillsmp_deep_crawler.py` — overflow WARNING log | Low | Mock `total_count >= 950`, assert WARNING emitted |
| Multi-platform install path in `SKILL.md` | Low | Smoke-test the platform-default logic (needs agent harness) |

---

## Quality Benchmark

`tests/quality/test_search_quality.py` evaluates Recall@30 against 100 labeled queries in
`tests/fixtures/test_queries.json`.

**Target:** Recall@30 ≥ 90% (the correct skill appears in the top-30 candidates for at least 90
of 100 queries).

Run manually (requires real index + Ollama):

```bash
ollama pull qwen3-embedding:0.6b
pytest tests/quality/ -v -m quality
```

This benchmark is intentionally excluded from CI to avoid the Ollama dependency in the build
pipeline. It should be run locally after any change to the embedding strategy, index build, or
query prefix.
