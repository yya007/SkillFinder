# Embedding Strategy

---

## Model: Qwen3-Embedding-0.6B via Ollama

The sole embedding model is **Qwen3-Embedding-0.6B**, served locally via Ollama — at both index-build time (CI) and query time (user's machine). Using the same model for both guarantees index/query compatibility by construction.

**Why Qwen3:**
- Open-source, zero cost to run
- Instruction-aware: supports a query prefix that improves retrieval quality
- Supports Matryoshka Representation Learning (MRL) — can output 512 or 1024 dims
- 0.6B is fast on CPU (~1,000 embeddings/min) and on GitHub Actions runners

**Dimensions:** 1024 (default). 512 via MRL is an option if disk space becomes a constraint.

**Requirement:** Ollama must be installed and `qwen3-embedding:0.6b` must be pulled. If Ollama is not available, `search.py` exits with clear installation instructions — there is no fallback.

---

## Query Instruction Prefix

Qwen3-Embedding is instruction-tuned. Apply this prefix to queries only — never to documents at index time:

```python
QUERY_PREFIX = (
    "Instruct: Given a description of a task or use case, "
    "retrieve the most relevant agent skill.\nQuery: "
)

def embed_query(text: str) -> np.ndarray:
    return ollama_embed(QUERY_PREFIX + text)

def embed_document(text: str) -> np.ndarray:
    return ollama_embed(text)  # no prefix
```

This is standard asymmetric retrieval: queries and documents are embedded differently, which consistently improves recall on instruction-tuned models.

---

## Ollama API

Both `pipeline/embed.py` (CI) and `scripts/search.py` (runtime) call the same local Ollama endpoint:

```python
import requests, numpy as np

OLLAMA_URL = "http://localhost:11434/api/embed"
MODEL = "qwen3-embedding:0.6b"

def ollama_embed(texts: list[str] | str) -> np.ndarray:
    if isinstance(texts, str):
        texts = [texts]
    resp = requests.post(OLLAMA_URL, json={"model": MODEL, "input": texts})
    resp.raise_for_status()
    return np.array(resp.json()["embeddings"], dtype=np.float32)
```

Ollama supports batched input via `"input": [...]`. `embed.py` uses `BATCH_SIZE = 128`.

---

## Incremental Embedding

`embed.py` supports an incremental mode that reuses cached vectors for unchanged skills, avoiding redundant Ollama calls on full rebuilds.

**How it works:**

Pass the previous run's outputs as a cache:

```bash
python pipeline/embed.py \
  --cache-embeddings data/prev_embeddings.npy \
  --cache-ordered    data/prev_ordered.jsonl
```

A skill is a **cache hit** if and only if both its `id` and `embedding_text` match the cached entry. Any change to `embedding_text` (description, categories, name) triggers a re-embed. Skills absent from the cache are always embedded.

**Cache invalidation rules:**
- `id` changed → always a miss (e.g. monorepo ID migration)
- `embedding_text` changed → miss; skill is re-embedded with updated content
- Both match → hit; cached vector is copied into the output at the correct row

**Row alignment guarantee:** cached and newly-embedded vectors are assembled in input order — row N in `embeddings.npy` always matches line N in `ordered.jsonl`, regardless of how cache hits and misses are interleaved.

**Without cache flags**, `embed.py` behaves identically to before — all records are embedded via Ollama.

---

## FAISS Index Configuration

```python
import faiss, numpy as np

DIM = 1024  # Qwen3-Embedding-0.6B output dimension

def build_index(embeddings: np.ndarray) -> faiss.Index:
    faiss.normalize_L2(embeddings)

    if len(embeddings) < 50_000:
        index = faiss.IndexFlatIP(DIM)   # Exact search, cosine similarity
    else:
        quantizer = faiss.IndexFlatIP(DIM)
        index = faiss.IndexIVFFlat(quantizer, DIM, 256, faiss.METRIC_INNER_PRODUCT)
        index.train(embeddings)

    index.add(embeddings)
    return index
```

**Index type by corpus size:**

| Skills | Index type | Notes |
|--------|-----------|-------|
| < 30K | `IndexScalarQuantizer` (SQ8) | Exact search, ~4× smaller than float32 |
| ≥ 30K | `IndexIVFScalarQuantizer` (IVF+SQ8) | Approximate search, same compression |

SQ8 quantizes each float32 dimension to 1 byte using a learned per-dimension scale, reducing index size ~4× (~34 MB at 33K skills vs ~138 MB flat float32) with ~99% recall. Both types require a `train()` call.

**Why inner product after L2 norm = cosine similarity:** after `faiss.normalize_L2(v)`, all vectors have unit length, so inner product equals cosine similarity. This is the standard FAISS pattern for semantic search.

---

## Embedding Text Quality

The `embedding_text` field determines retrieval quality more than any model parameter. See [`data-sources.md`](data-sources.md) for construction logic.

Key principles:
- Include trigger phrases from SKILL.md frontmatter — these match natural user queries
- Include categories — help with broad queries like "kubernetes tools"
- Keep total length under 512 tokens — model quality degrades on longer inputs
- Never include installation commands or quality metadata in embedding text
