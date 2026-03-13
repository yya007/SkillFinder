# Embedding Strategy

---

## Model Selection: Qwen3-Embedding-0.6B

The primary embedding model is **Qwen3-Embedding-0.6B**, served locally via Ollama.

**Why Qwen3:**
- Open-source, zero cost to run locally
- Instruction-aware: supports a query prefix that improves retrieval quality
- Supports Matryoshka Representation Learning (MRL) — can output 512 or 1024 dims from the same model
- 0.6B variant is fast on CPU (~1,000 embeddings/min)
- 8B variant (used in CI for index building) is #1 on MTEB multilingual leaderboard

**Dimensions:** 1024 (default). 512 via MRL is available for a smaller index if disk space is a constraint.

---

## Three-Tier Fallback

At query time, `search.py` probes for available embedding capability in order:

```
Tier 1: Local Ollama (qwen3-embedding:0.6b)
  Detection: `ollama list | grep qwen3-embedding` exits 0
  Index used: data/qwen/
  Cost: $0
  Quality: Best

Tier 2: Bundled sentence-transformers (all-MiniLM-L6-v2)
  Detection: `import sentence_transformers` succeeds
  Index used: data/minilm/
  Cost: $0
  Quality: Good (~80MB model, runs on CPU, always available offline)

Tier 3: OpenRouter API (qwen/qwen3-embedding-8b)
  Detection: OPENROUTER_API_KEY env var is set
  Index used: data/qwen/
  Cost: ~$0.00001/query
  Quality: Best (same space as Tier 1)
```

If none of the three tiers is available, `search.py` exits with a clear error message and installation instructions for Ollama.

---

## Query Instruction Prefix

Qwen3-Embedding is instruction-tuned. Wrap queries with:

```
Instruct: Given a description of a task or use case, retrieve the most relevant agent skill.
Query: {user_query}
```

Do **not** add this prefix to the documents being indexed — only to queries. This is standard asymmetric retrieval usage.

```python
QUERY_PREFIX = (
    "Instruct: Given a description of a task or use case, "
    "retrieve the most relevant agent skill.\nQuery: "
)

def embed_query(text: str, model) -> np.ndarray:
    return model.encode(QUERY_PREFIX + text)

def embed_document(text: str, model) -> np.ndarray:
    return model.encode(text)  # no prefix
```

---

## Build-Time vs Query-Time Model Mismatch

**The index must be built with the same model family used at query time.**

| Built with | Query with | Index dir | Compatible? |
|-----------|-----------|-----------|-------------|
| Qwen3-8B (CI) | Qwen3-0.6B (Ollama) | `data/qwen/` | ✅ Same embedding space |
| Qwen3-8B (CI) | Qwen3-0.6B (Ollama) | `data/minilm/` | ❌ Garbage results |
| MiniLM (CI) | MiniLM (local) | `data/minilm/` | ✅ |
| MiniLM (CI) | Qwen3-0.6B (Ollama) | `data/minilm/` | ❌ |

`search.py` enforces this: it detects which tier is active and loads the corresponding index directory. There is no user configuration needed.

---

## FAISS Index Configuration

```python
import faiss, numpy as np

def build_index(embeddings: np.ndarray) -> faiss.Index:
    d = embeddings.shape[1]  # 1024 for Qwen3, 384 for MiniLM
    faiss.normalize_L2(embeddings)

    if len(embeddings) < 50_000:
        index = faiss.IndexFlatIP(d)   # Exact search, cosine similarity
    else:
        nlist = 256
        quantizer = faiss.IndexFlatIP(d)
        index = faiss.IndexIVFFlat(quantizer, d, nlist, faiss.METRIC_INNER_PRODUCT)
        index.train(embeddings)

    index.add(embeddings)
    return index
```

For the expected scale of 10K–20K skills, `IndexFlatIP` (exact search) is always used. IVF is available if the index grows beyond 50K.

**Why inner product after L2 norm = cosine similarity:**

After `faiss.normalize_L2(v)`, all vectors have unit length. Inner product of two unit vectors equals their cosine similarity. This is the standard FAISS pattern for semantic search.

---

## CI Embedding (Pipeline)

The offline pipeline uses the larger **Qwen3-Embedding-8B** via OpenRouter for higher quality index vectors:

```python
# pipeline/embed.py
import openai  # OpenRouter is OpenAI-compatible

client = openai.OpenAI(
    api_key=os.environ["OPENROUTER_API_KEY"],
    base_url="https://openrouter.ai/api/v1",
)

def embed_batch(texts: list[str]) -> list[list[float]]:
    response = client.embeddings.create(
        model="qwen/qwen3-embedding-8b",
        input=texts,
    )
    return [r.embedding for r in response.data]
```

Cost at 20K skills, ~200 tokens avg per `embedding_text`: **≈ $0.04/rebuild**.

MiniLM embeddings are built in the same pipeline pass using `sentence-transformers` locally (no cost).

---

## Embedding Text Quality

The `embedding_text` field determines retrieval quality more than model choice. See [`data-sources.md`](data-sources.md) for the construction logic.

Key principles:
- Include trigger phrases from SKILL.md frontmatter — these are what users say
- Include categories — these help with broad queries like "kubernetes tools"
- Keep total length under 512 tokens — models degrade on very long inputs
- Never include installation commands or quality metadata in embedding text
