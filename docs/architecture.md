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
  Embedder            Qwen3-Embedding-0.6B via Ollama (same model as runtime)
  Index Builder       FAISS IndexFlatIP, L2-normalized vectors
       ↓
  GitHub Release      skill-finder-index-YYYYMMDD.tar.gz (~100MB)


RUNTIME (User's machine, inside Claude Code)
──────────────────────────────────────────────────────────────
  User query + optional platform filter (claude_code / codex / openclaw)
       ↓
  search.py           Detect available embedding model
       ↓
  Embed query         Ollama (qwen3-embedding:0.6b) — required
       ↓
  FAISS search        Load data/index.faiss
       ↓
  Attribute filter    --platform, --source, --safety_only
       ↓
  JSON candidates     N×3 results (raw FAISS order) → Claude Code agent
       ↓
  Agent selects       Reads all candidates, proposes best ≤ N to user


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

- **`search.py`** — the main entry point. Embeds query via Ollama, loads `data/index.faiss`, applies attribute filters, returns a candidate pool in FAISS score order for the agent to evaluate.
- **`fetch_skill.py`** — fetches raw `SKILL.md` from a GitHub repo (tries `main`, `master`, subdirectory patterns).
- **`update_index.py`** — checks GitHub Releases API for a newer index, downloads and verifies SHA256, extracts into `data/`.

### Data (`data/`)

Not committed to git (too large). Downloaded by `update_index.py` or manually.

```
data/
├── index.faiss       # FAISS index (Qwen3-Embedding-0.6B)
├── metadata.jsonl    # Row-aligned skill metadata
└── version.txt       # Release date + skill count + SHA256
```

The same model (Qwen3-Embedding-0.6B via Ollama) is used in CI to build the index and at runtime to embed queries, so index/query compatibility is guaranteed by construction.

---

## Data Flow: Embedding Consistency

```
BUILD TIME (CI)                     QUERY TIME (user machine)
───────────────                     ─────────────────────────
Ollama qwen3-embedding:0.6b         Ollama qwen3-embedding:0.6b
  → data/embeddings.npy               → embed query vector
  → data/index.faiss                  → search data/index.faiss
  → data/metadata.jsonl
```

The same model binary runs in both environments. There is no version mismatch risk.

---

## Candidate Pool & Agent Selection

`search.py` returns `propose_n × 3` candidates sorted by raw FAISS cosine similarity — no re-ranking. The agent reads the full candidate pool and decides which skills to surface to the user (up to `propose_n`). This keeps the heuristic scoring logic out of the script and lets the agent apply its own judgment based on the full metadata context.

**Attribute filters** run before returning candidates and reduce the pool prior to agent evaluation:

| Flag | Values | Behavior |
|------|--------|----------|
| `--platform` | `claude_code`, `codex`, `openclaw` | Keep only skills with a matching key in `install_cmd` |
| `--source` | `skillsmp`, `clawhub`, `skillhub`, `marketplace` | Keep only skills from that registry |
| `--safety_only` | flag | Drop skills where `safety_flag: true` |

Multiple `--platform` values are OR'd (e.g., `--platform claude_code --platform openclaw` returns skills installable on either).

---

## Size Estimates

| Component | Size |
|-----------|------|
| FAISS index (20K skills × 1024 dims × float32) | ~80MB |
| Metadata JSONL (20K × ~500 bytes) | ~10MB |
| Compressed release artifact | ~50–70MB |
| Total user disk footprint | ~100MB |

---

## What's Not Included

- No web server, no database, no Docker
- Crawlers and pipeline scripts are CI-only and not distributed to users
- No telemetry or usage tracking
