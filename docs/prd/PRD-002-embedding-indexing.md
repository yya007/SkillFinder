# PRD-002: Embedding & Index Building

**Status:** Planned
**Phase:** 2 of 5
**Depends on:** PRD-001 (unified_skills.jsonl)
**Blocks:** PRD-003

---

## Problem

`unified_skills.jsonl` contains 10K–20K skill records with rich text fields. We need to convert these into vector embeddings and build a FAISS index that enables sub-200ms semantic search at query time — and we need to do this for two models (Qwen3 and MiniLM) so both tiers of the runtime fallback work.

## Goals

- Embed all skills in `unified_skills.jsonl` using Qwen3-Embedding-8B (primary) and MiniLM (fallback)
- Build two FAISS indexes (one per model) with row-aligned metadata files
- Package both into a versioned release artifact
- Total build time under 30 minutes in CI

## Non-Goals

- Query-time embedding (covered in PRD-003)
- Distributing embedding infrastructure to users
- Supporting other embedding models at this stage

---

## Functional Requirements

### F1 — Embedding (`pipeline/embed.py`)

- Read `embedding_text` from each record in `data/unified_skills.jsonl`
- Produce Qwen3-Embedding-8B embeddings via OpenRouter API
  - Batch size: 512 texts per API call (stay within token limits)
  - Model: `qwen/qwen3-embedding-8b`
  - Dimension: 1024
  - Apply `QUERY_PREFIX` only to queries, **not** to documents at index time
- Produce MiniLM-L6-v2 embeddings locally via `sentence-transformers`
  - Model: `all-MiniLM-L6-v2`
  - Dimension: 384
- Save embeddings as `.npy` arrays with shape `(N, D)` where N = skill count
- Output: `data/qwen/embeddings.npy`, `data/minilm/embeddings.npy`
- Also write `data/unified_skills_ordered.jsonl` — the metadata rows in the exact order they were embedded (critical for row alignment)

### F2 — Index Building (`pipeline/build_index.py`)

- Load `data/{model}/embeddings.npy` and `data/unified_skills_ordered.jsonl`
- L2-normalize all embedding vectors in-place
- Build FAISS index:
  - `len(records) < 50,000`: use `faiss.IndexFlatIP(d)` — exact search
  - `len(records) >= 50,000`: use `faiss.IndexIVFFlat` with `nlist=256`, train on all vectors
- Add all vectors to the index
- Write `data/{model}/index.faiss`
- Write `data/{model}/metadata.jsonl` (same rows in same order as the index)
- Write `data/version.txt` with format:
  ```
  date: 2026-03-10
  skill_count: 14823
  qwen_sha256: abc123...
  minilm_sha256: def456...
  ```
- Create release artifact: `skill-finder-index-YYYYMMDD.tar.gz` containing both model directories and `version.txt`

### F3 — Artifact Verification

Before packaging, verify:
- Row count in `metadata.jsonl` == `index.ntotal` for each model
- Spot-check: embed 10 test queries, confirm results are non-empty and coherent
- Compute SHA256 of `index.faiss` and `metadata.jsonl`; write to `version.txt`

---

## Technical Spec

### Embedding Batching

```python
# pipeline/embed.py
BATCH_SIZE = 512

def embed_all_qwen(texts: list[str], api_key: str) -> np.ndarray:
    client = openai.OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1"
    )
    all_embeddings = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        response = client.embeddings.create(
            model="qwen/qwen3-embedding-8b",
            input=batch,
        )
        all_embeddings.extend([r.embedding for r in response.data])
        print(f"  Embedded {min(i + BATCH_SIZE, len(texts))}/{len(texts)}", end='\r')
    return np.array(all_embeddings, dtype=np.float32)
```

### Checkpoint / Resume

For the embedding step, write a `.npy` checkpoint every 1,000 records so a CI job can resume after a transient API failure without re-embedding from scratch:

```
data/qwen/embeddings_checkpoint_10000.npy   # first 10K done
data/qwen/embeddings_checkpoint_11000.npy   # first 11K done
...
```

On startup, `embed.py` checks for existing checkpoints and resumes from the latest.

### FAISS Row Alignment

This is the most critical correctness constraint. The row order must be identical across:
- `data/unified_skills_ordered.jsonl` (line N → skill N)
- `data/qwen/embeddings.npy` (row N → embedding of skill N)
- `data/qwen/index.faiss` (internal ID N → skill N)
- `data/qwen/metadata.jsonl` (line N → metadata for skill N)
- Same for `data/minilm/*`

`build_index.py` reads `data/unified_skills_ordered.jsonl` once and ensures all outputs use the same order. It does not sort or shuffle.

### Dependencies

```
faiss-cpu>=1.8
numpy>=1.26
openai>=1.30        # OpenRouter API (OpenAI-compatible)
sentence-transformers>=3.0
```

---

## Cost

| Item | Estimate |
|------|----------|
| Qwen3-8B embeddings, 20K skills × 200 tokens avg | ~$0.04 |
| MiniLM embeddings | $0 (local CPU) |
| GitHub Actions compute (ubuntu-latest, ~30 min) | Free tier |

---

## Success Criteria

| Metric | Target |
|--------|--------|
| Embedding coverage | 100% of records in unified_skills.jsonl |
| Row alignment check | Passes for both models |
| FAISS index size (qwen, 20K skills) | ~80MB uncompressed |
| Artifact compressed size | < 150MB |
| Spot-check search coherence | Top result for "deploy kubernetes" is k8s-related |
| Build time (CI) | ≤ 30 min |

---

## Open Questions

- OpenRouter enforces a token limit per request (currently 128K). Need to measure average token count of `embedding_text` to ensure batches of 512 texts stay under limit.
- Should the MiniLM index use a different quality filter threshold? MiniLM retrieval quality is lower; it may make sense to reduce the corpus to the top 5K highest-quality skills for the MiniLM index.
