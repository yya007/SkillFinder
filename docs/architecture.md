# Architecture

SkillFinder has two distinct halves that never meet at runtime: an **offline indexing pipeline** (runs in CI) and a **runtime skill** (runs on the user's machine).

---

## System Overview

```
OFFLINE PIPELINE (GitHub Actions, weekly)
──────────────────────────────────────────────────────────────
  Crawlers            Fetch SKILL.md + metadata from all sources
       ↓
  Normalizer          Deduplicate, merge metadata, quality filter
       ↓
  Embedder            Qwen3-Embedding-0.6B via OpenRouter API
       ↓              (also produces MiniLM fallback embeddings)
  Index Builder       FAISS IndexFlatIP, L2-normalized vectors
       ↓
  GitHub Release      skill-finder-index-YYYYMMDD.tar.gz (~100MB)


RUNTIME (User's machine, inside Claude Code)
──────────────────────────────────────────────────────────────
  User query
       ↓
  search.py           Detect available embedding model
       ↓
  Embed query         Tier 1: Ollama / Tier 2: MiniLM / Tier 3: API
       ↓
  FAISS search        Load matching index (qwen/ or minilm/)
       ↓
  Re-rank             similarity×0.7 + quality×0.2 + recency×0.1
       ↓
  JSON results        Returned to Claude Code agent


ON-DEMAND (when user wants full skill details)
──────────────────────────────────────────────────────────────
  fetch_skill.py      Fetch raw SKILL.md from GitHub repo URL
       ↓
  Agent reads         Full content analysis before install
```

---

## Component Responsibilities

### Crawlers (`crawlers/`)

Each source has its own crawler module. Crawlers are responsible only for fetching and outputting raw, source-specific JSONL. No normalization happens here.

| Crawler | Source | Method |
|---------|--------|--------|
| `skillsmp_crawler.py` | SkillsMP / GitHub | GitHub Search API (`filename:SKILL.md`) |
| `clawhub_crawler.py` | ClawHub / OpenClaw | Parse VoltAgent/awesome-openclaw-skills README |
| `skillhub_crawler.py` | SkillHub | Web scraping (skillhub.club) |
| `marketplace_crawler.py` | Anthropic + community | Clone repos, parse `marketplace.json` |

Output: `data/raw/{source}.jsonl`

### Pipeline (`pipeline/`)

Three sequential scripts that transform raw crawler output into a deployable index:

1. **`normalize.py`** — loads all `data/raw/*.jsonl`, deduplicates on canonical repo URL, merges metadata per priority rules (see [`data-sources.md`](data-sources.md)), builds `embedding_text`, applies quality filter, outputs `data/unified_skills.jsonl`.

2. **`embed.py`** — reads `embedding_text` from each record, batch-embeds via Qwen3-Embedding-8B (OpenRouter, for CI) and all-MiniLM-L6-v2, writes `data/qwen/embeddings.npy` and `data/minilm/embeddings.npy`.

3. **`build_index.py`** — loads embeddings, L2-normalizes, builds `faiss.IndexFlatIP`, writes `data/{qwen,minilm}/index.faiss` + `metadata.jsonl` (row-aligned with the index). Packages everything into a versioned `.tar.gz`.

### Scripts (`scripts/`)

These ship with the skill and run on the user's machine.

- **`search.py`** — the main entry point. Detects available embedding model, loads matching FAISS index, searches, re-ranks, returns JSON.
- **`fetch_skill.py`** — fetches raw `SKILL.md` from a GitHub repo (tries `main`, `master`, subdirectory patterns).
- **`update_index.py`** — checks GitHub Releases API for a newer index, downloads and verifies SHA256, extracts into `data/`.

### Data (`data/`)

Not committed to git (too large). Downloaded by `update_index.py` or manually.

```
data/
├── qwen/
│   ├── index.faiss       # FAISS index built with Qwen3 embeddings
│   └── metadata.jsonl    # Row-aligned skill metadata
├── minilm/
│   ├── index.faiss       # FAISS index built with MiniLM embeddings
│   └── metadata.jsonl    # Same records, same row order
└── version.txt           # Release date + skill count + SHA256
```

**Critical invariant:** The FAISS index in `data/qwen/` was built with Qwen3 embeddings; it must only be searched with Qwen3 query embeddings. Mixing models produces garbage results. `search.py` enforces this by selecting the index directory based on which embedding model is actually used.

---

## Data Flow: Embedding Consistency

```
BUILD TIME                          QUERY TIME
──────────                          ──────────
Qwen3-8B (OpenRouter)               Qwen3-0.6b (Ollama) ──→ data/qwen/index.faiss
  → data/qwen/embeddings.npy              OR
  → data/qwen/index.faiss           OpenRouter API       ──→ data/qwen/index.faiss

MiniLM (sentence-transformers)      MiniLM (bundled)     ──→ data/minilm/index.faiss
  → data/minilm/embeddings.npy
  → data/minilm/index.faiss
```

The Qwen3 variants (0.6B vs 8B) produce embeddings in the same space — the 0.6B model is a distilled version of the 8B, and both use the same instruction-tuned embedding protocol. This allows building the index with the more powerful 8B (better quality) and searching with the cheaper 0.6B locally.

---

## Re-ranking Formula

After FAISS returns `top_k × 3` candidates:

```python
final_score = (
    similarity_score * 0.70 +    # FAISS cosine similarity
    quality_normalized * 0.20 +  # stars + skillhub_score, normalized 0-1
    recency_score * 0.10         # days since last_updated, decayed
)
```

Then apply `min_quality` threshold filter, take top `top_k`.

---

## Size Estimates

| Component | Size |
|-----------|------|
| FAISS index (20K skills × 1024 dims × float32) | ~80MB |
| Metadata JSONL (20K × ~500 bytes) | ~10MB |
| Compressed release artifact | ~50–80MB |
| MiniLM index (20K × 384 dims × float32) | ~30MB |
| Total user disk footprint | ~100–200MB |

---

## What's Not Included

- No web server, no database, no Docker
- Crawlers and pipeline scripts are CI-only and not distributed to users
- No telemetry or usage tracking
