# PRD-002: Embedding & Index Building

**Status:** Planned
**Phase:** 2 of 5
**Depends on:** PRD-001 (unified_skills.jsonl)
**Blocks:** PRD-003

---

## Problem

`unified_skills.jsonl` contains 10K–20K skill records with rich text fields. We need to convert these into vector embeddings and build a FAISS index that enables sub-200ms semantic search at query time. The same model (Qwen3-Embedding-0.6B via Ollama) is used in CI and at runtime, so there is no index/query compatibility concern.

## Goals

- Embed all skills using Qwen3-Embedding-0.6B via Ollama running in CI
- Build a single FAISS index with row-aligned metadata
- Package into a versioned release artifact
- Total build time under 30 minutes in CI

## Non-Goals

- Multiple model variants or fallback indexes
- Query-time embedding (PRD-003)
- Distributing embedding infrastructure to users

---

## Functional Requirements

### F1 — Embedding (`pipeline/embed.py`)

- Start Ollama in CI and pull `qwen3-embedding:0.6b`
- Read `embedding_text` from each record in `data/unified_skills.jsonl`
- Embed via local Ollama API in batches of 64
- Do **not** apply the query instruction prefix — that is for queries only
- Write embeddings as `data/embeddings.npy` with shape `(N, 1024)`
- Also write `data/unified_skills_ordered.jsonl` preserving the exact iteration order (critical for row alignment with the index)

### F2 — Index Building (`pipeline/build_index.py`)

- Load `data/embeddings.npy` and `data/unified_skills_ordered.jsonl`
- L2-normalize all embedding vectors in-place
- Build FAISS index:
  - `N < 50,000`: `faiss.IndexFlatIP(1024)` — exact search
  - `N >= 50,000`: `faiss.IndexIVFFlat` with `nlist=256`
- Add all vectors; write `data/index.faiss`
- Write `data/metadata.jsonl` (same rows, same order as the index)
- Write `data/version.txt`:
  ```
  date: 2026-03-10
  skill_count: 14823
  index_sha256: abc123...
  metadata_sha256: def456...
  ```
- Package: `skill-finder-index-YYYYMMDD.tar.gz` containing `data/index.faiss`, `data/metadata.jsonl`, `data/version.txt`

### F3 — Artifact Verification

Before packaging:
- Assert `metadata.jsonl` line count == `index.ntotal`
- Spot-check: embed 5 test queries via Ollama, assert top result is in the expected category
- Compute SHA256 of `index.faiss` and `metadata.jsonl`; write to `version.txt`

---

## Technical Spec

### Ollama in CI

```yaml
# In GitHub Actions workflow
- name: Install Ollama
  run: |
    curl -fsSL https://ollama.com/install.sh | sh
    ollama serve &
    sleep 5
    ollama pull qwen3-embedding:0.6b
```

Ollama serves on `http://localhost:11434`. The embed script calls it directly.

### Embedding

```python
# pipeline/embed.py
import requests, numpy as np, json

OLLAMA_URL = "http://localhost:11434/api/embed"
MODEL = "qwen3-embedding:0.6b"
BATCH_SIZE = 64

def embed_batch(texts: list[str]) -> np.ndarray:
    resp = requests.post(OLLAMA_URL, json={"model": MODEL, "input": texts})
    resp.raise_for_status()
    return np.array(resp.json()["embeddings"], dtype=np.float32)

def embed_all(records: list[dict]) -> np.ndarray:
    texts = [r["embedding_text"] for r in records]
    all_vecs = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        all_vecs.append(embed_batch(batch))
        print(f"  {min(i + BATCH_SIZE, len(texts))}/{len(texts)}", end='\r')
    return np.vstack(all_vecs)
```

### Checkpoint / Resume

Write a checkpoint `.npy` every 1,000 records. On restart, `embed.py` reads the latest checkpoint and resumes from there. This handles transient Ollama failures without re-embedding from scratch.

```
data/embeddings_checkpoint_1000.npy
data/embeddings_checkpoint_2000.npy
...
data/embeddings.npy   # final complete file
```

### FAISS Row Alignment

The row order must be identical across all three outputs:
- `data/unified_skills_ordered.jsonl` — line N = skill N
- `data/embeddings.npy` — row N = embedding of skill N
- `data/index.faiss` — internal ID N = skill N
- `data/metadata.jsonl` — line N = metadata for skill N

`build_index.py` reads `data/unified_skills_ordered.jsonl` once and uses that ordering for all outputs without sorting or shuffling.

### Dependencies (`requirements-ci.txt` additions for this phase)

```
faiss-cpu>=1.8
numpy>=1.26
requests>=2.31   # already in crawler deps
```

No `openai`, no `sentence-transformers`.

---

## Cost

| Item | Estimate |
|------|----------|
| Ollama embedding (20K skills) | $0 |
| GitHub Actions compute (~20 min for embed + build) | Free tier |

---

## Success Criteria

| Metric | Target |
|--------|--------|
| Embedding coverage | 100% of records in unified_skills.jsonl |
| Row alignment check passes | Yes |
| FAISS index size (20K skills) | ~80MB uncompressed |
| Artifact compressed size | < 100MB |
| Spot-check search coherence | Top result for "deploy kubernetes" is k8s-related |
| Build time (CI, embed + index) | ≤ 20 min |

---

## Resolved Questions

**Ollama model caching in CI** — Resolved. The workflow caches `~/.ollama/models` using `actions/cache@v4` with key `ollama-qwen3-embedding-0.6b`. Subsequent runs restore the cached model (~600 MB) instead of re-downloading, cutting the Ollama setup step from ~3 min to ~10 s on a warm cache.

**`embeddings.npy` in release artifact** — Resolved. Excluded. The release tarball contains only `index.faiss`, `metadata.jsonl`, and `version.txt`. `embeddings.npy` (~80 MB) is a build-time intermediate; end users never need it. Developers who want to rebuild the index without re-embedding can download the artifact and run `build_index.py` on their own embeddings.
