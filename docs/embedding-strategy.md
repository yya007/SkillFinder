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

Ollama supports batched input via `"input": [...]`. Use batch size 64–128 to stay within Ollama's default request limits.

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

For the expected 10K–20K scale, `IndexFlatIP` is always used. `IndexIVFFlat` is available if the corpus grows beyond 50K.

**Why inner product after L2 norm = cosine similarity:** after `faiss.normalize_L2(v)`, all vectors have unit length, so inner product equals cosine similarity. This is the standard FAISS pattern for semantic search.

---

## Embedding Text Quality

The `embedding_text` field determines retrieval quality more than any model parameter. See [`data-sources.md`](data-sources.md) for construction logic.

Key principles:
- Include trigger phrases from SKILL.md frontmatter — these match natural user queries
- Include categories — help with broad queries like "kubernetes tools"
- Keep total length under 512 tokens — model quality degrades on longer inputs
- Never include installation commands or quality metadata in embedding text
