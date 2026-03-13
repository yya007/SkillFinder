# Test Plan

SkillFinder follows strict Test-Driven Development. All tests are written before implementation. A test file going from red to green is the definition of "done" for each module.

---

## Philosophy

- **Tests define the contract.** The function signatures and behavior described in test files are authoritative. If implementation and test disagree, fix the implementation.
- **No mocking the unit under test.** External I/O (Ollama, GitHub HTTP, filesystem) is injected or patched; internal logic is never mocked.
- **Fixtures are the source of truth for data.** All test data lives in `tests/fixtures/`; no inline data blobs in test files except simple one-liners.
- **Quality tests require real infrastructure** (Ollama running, `data/index.faiss` present) and are skipped gracefully when unavailable.

---

## Test Pyramid

```
                  ┌───────────────┐
                  │  quality/     │  ← Recall@30, real index, real Ollama
                  │  (2 files)    │    skipped in CI without index
                ┌─┴───────────────┴─┐
                │  integration/     │  ← Pipeline end-to-end, mocked Ollama
                │  (2 files)        │
              ┌─┴───────────────────┴─┐
              │  unit/                │  ← Pure logic, no I/O
              │  (5 files, ~120 cases)│
              └───────────────────────┘
```

---

## Module → Test File Mapping

| Module | Unit test | Integration test |
|--------|-----------|-----------------|
| `pipeline/normalize.py` | `tests/unit/test_normalize.py` | `tests/integration/test_pipeline.py` |
| `pipeline/embed.py` | `tests/unit/test_embed.py` | `tests/integration/test_pipeline.py` |
| `pipeline/build_index.py` | `tests/unit/test_build_index.py` | `tests/integration/test_pipeline.py` |
| `scripts/search.py` | `tests/unit/test_search.py` | `tests/integration/test_search_integration.py` |
| `scripts/fetch_skill.py` | `tests/unit/test_fetch_skill.py` | — |
| `scripts/update_index.py` | `tests/unit/test_update_index.py` | — |

---

## API Contracts

These are the exact public interfaces that tests import against. Implementation must match.

### `pipeline/normalize.py`

```python
CURATED_SOURCES: set[str]                          # {"clawhub", "skillhub", "marketplace"}

canonical_key(repo_url: str) -> str                # lowercase, strip .git, strip trailing /
skill_id(repo_url: str) -> str                     # sha256(canonical_key).hexdigest()
build_embedding_text(skill: dict) -> str           # "{name}. {desc} Categories: ... [Use when: ...]"
merge_records(records: list[dict]) -> dict         # merge by priority rules
build_install_cmds(merged: dict) -> dict[str, str] # generate install_cmd from sources
passes_quality_filter(skill: dict) -> bool         # stars≥2 OR curated OR skillhub A/S
apply_safety_flag(skill: dict) -> dict             # set quality.safety_flag from clawhub scan
normalize(raw_paths, output_path, min_skills) -> int

class QualityGateError(Exception): ...
```

### `pipeline/embed.py`

```python
OLLAMA_URL: str  MODEL: str  BATCH_SIZE: int  DIM: int  CHECKPOINT_EVERY: int

check_ollama_available(url) -> bool
embed_batch(texts, model, ollama_url) -> np.ndarray   # shape (N, 1024), float32
embed_all(records, ollama_url, batch_size, checkpoint_dir) -> np.ndarray
run_embed(input_path, output_embeddings, output_ordered, ...) -> int

class OllamaError(Exception): ...
```

### `pipeline/build_index.py`

```python
DIM: int  IVF_THRESHOLD: int  IVF_NLIST: int

l2_normalize(embeddings) -> np.ndarray         # in-place, returns same array
build_index(embeddings) -> faiss.Index         # IndexFlatIP < 50K, IVFFlat >= 50K
verify_alignment(index, metadata) -> None      # raises AlignmentError on mismatch
sha256_file(path) -> str
write_version_txt(path, date, skill_count, index_sha256, metadata_sha256) -> None
read_version_txt(path) -> dict
run_build_index(embeddings_path, ordered_skills_path, output_index,
                output_metadata, output_version, date) -> dict

class AlignmentError(Exception): ...
```

### `scripts/search.py`

```python
QUERY_PREFIX: str

embed_query(text, ollama_url, model) -> np.ndarray   # prefix applied, L2-normalized
check_ollama(url) -> None                            # raises OllamaNotAvailableError
load_index(index_path, metadata_path) -> tuple[faiss.Index, list[dict]]
apply_filters(candidates, platforms, sources, safety_only) -> list[dict]
search(query, index, metadata, propose_n, platforms, sources, safety_only, ollama_url) -> list[dict]
format_results(results, as_json) -> str

class OllamaNotAvailableError(Exception): ...
class AlignmentError(Exception): ...
```

### `scripts/fetch_skill.py`

```python
parse_github_url(url) -> tuple[str, str]      # (owner, repo)
candidate_urls(owner, repo) -> list[str]      # ordered URL list to try
fetch_skill_md(repo_url, timeout) -> tuple[str, str]   # (content, resolved_url)
parse_frontmatter(content) -> tuple[dict, str]          # (frontmatter, body)
fetch_and_output(repo_url, output_path, timeout) -> str

class FetchError(Exception):
    attempted_urls: list[str]
```

### `scripts/update_index.py`

```python
read_local_version(data_dir) -> Optional[str]   # date string or None
get_latest_release(repo) -> dict                 # {tag_name, assets: [{name, url, size}], body}
needs_update(local_version, latest_tag) -> bool  # compare date strings
verify_sha256(filepath, expected_hex) -> bool
download_file(url, dest_path, token) -> None
extract_artifact(tar_path, data_dir) -> None     # atomic: extract to .new/, rename
parse_release_sha256(release_body) -> Optional[str]
run_update(data_dir, repo, token, force, check_only) -> dict   # {status, message}
```

---

## Fixtures

All fixtures live in `tests/fixtures/`.

### Skill domains covered

| Skill name | Domain | In sources |
|---|---|---|
| `kubernetes-deployer` | devops, k8s | skillsmp + clawhub + skillhub |
| `docker-compose-manager` | devops, docker | skillsmp + skillhub |
| `github-actions-runner` | ci-cd | skillsmp + clawhub |
| `terraform-manager` | devops, iac | skillsmp |
| `web-scraper-pro` | web | clawhub + skillhub |
| `sql-query-optimizer` | database | skillhub |
| `git-commit-assistant` | git | marketplace |
| `aws-lambda-deployer` | cloud, serverless | skillsmp + clawhub |
| `markdown-formatter` | writing | marketplace |
| `python-test-runner` | testing | skillsmp |
| `api-mock-server` | testing, api | clawhub |
| `slack-notifier` | communication | skillsmp |

### Overlap design (for dedup testing)

- `kubernetes-deployer`: in skillsmp (142 stars), clawhub (has safety scan), skillhub (rank A, score 8.4)
- `aws-lambda-deployer`: in skillsmp (67 stars), clawhub (no safety scan yet)
- `docker-compose-manager`: in skillsmp (89 stars), skillhub (rank B, score 6.2)
- `github-actions-runner`: in skillsmp (34 stars), clawhub

### `test_queries.json` — 20 labeled queries

See `tests/fixtures/test_queries.json`. Format:
```json
{"query": "...", "expected_skill_name": "...", "category": "..."}
```

---

## Running Tests

```bash
# Unit tests only (no external dependencies)
pytest tests/unit/ -v

# Unit + integration (needs faiss-cpu, numpy; mocks Ollama)
pytest tests/unit/ tests/integration/ -v

# Full suite including quality (needs Ollama + data/index.faiss)
pytest tests/ -v

# With coverage
pytest tests/unit/ tests/integration/ --cov=pipeline --cov=scripts --cov-report=term-missing

# Skip slow tests
pytest tests/ -v -m "not slow"
```

---

## Coverage Targets

| Module | Target line coverage |
|--------|---------------------|
| `pipeline/normalize.py` | 95% |
| `pipeline/embed.py` | 85% (Ollama I/O paths partially mocked) |
| `pipeline/build_index.py` | 90% |
| `scripts/search.py` | 90% |
| `scripts/fetch_skill.py` | 90% |
| `scripts/update_index.py` | 85% |

---

## CI Strategy

- Unit and integration tests run on every push (no Ollama, no index required)
- Quality tests run only when `data/index.faiss` is present (skipped otherwise via `pytest.mark.skipif`)
- `pytest -m "not quality"` is the default CI command
