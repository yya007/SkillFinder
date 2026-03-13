# SkillFinder: Unified Agent Skill Discovery Engine

## Implementation Spec

---

## 1. Problem

400,000+ agent skills exist across multiple registries (Claude Code, Codex, OpenClaw), all using the SKILL.md standard. No unified search exists. Users cannot discover the best skill for a given use case without manually browsing multiple sites.

## 2. Solution

**SkillFinder** is a skill that ships with a pre-built FAISS vector index covering all major skill registries. User describes a use case → local embedding search over 10K+ curated skills → returns top matches with metadata and install links → optionally fetches full SKILL.md for deeper analysis.

## 3. Why a Skill (Not a Standalone API)

- Zero infrastructure — drop into `~/.claude/skills/`
- $0 per query (local embeddings + FAISS)
- <100ms search latency, works fully offline
- Agent auto-invokes via SKILL.md triggers
- Trade-off: ships ~50–150MB index artifact, needs periodic updates

---

## 4. Data Sources

### 4.1 Source Registry

| Source | Est. Skills | Access Method | Quality Signal |
|--------|-------------|---------------|----------------|
| **SkillsMP** (skillsmp.com) | 400,000+ | GitHub API (`filename:SKILL.md`) + web scraping | Stars, quality indicators |
| **ClawHub** (OpenClaw registry) | 5,400+ | CLI API (`clawhub`) + REST | VirusTotal scan, categories |
| **SkillHub** (skillhub.club) | 7,000+ | Web scraping | S/A/B/C rank (5 dimensions: Practicality, Clarity, Automation, Quality, Impact) |
| **Anthropic Official** (github.com/anthropics/skills) | ~50 | GitHub API | Official, production-ready |
| **Community Marketplaces** (GitHub) | ~500 | GitHub API, parse `marketplace.json` | Stars, forks, recency |
| **awesome-openclaw-skills** (VoltAgent/awesome-openclaw-skills) | 5,400+ | Parse README.md — each line is `name - description` | Categorized, filtered |

### 4.2 Deduplication

Canonical key: **normalized GitHub repo URL** (lowercase, strip `.git`).

When a skill appears in multiple registries, merge metadata with this priority:
1. SkillHub AI rating → `quality_score`
2. SkillsMP star count → `popularity`
3. ClawHub security scan → `safety_flag`
4. Original repo README → `description`
5. SKILL.md frontmatter → `name`, `description`, `triggers`

### 4.3 Target Index Size

After dedup and quality filtering (min 2 stars OR present in a curated registry): **10,000–20,000 unique skills**. Full unfiltered index available as optional download.

---

## 5. Architecture

```
OFFLINE PIPELINE (GitHub Actions, weekly)
──────────────────────────────────────────
Crawlers (per source)
  → Parser & Normalizer
    → Deduper & Quality Filter
      → Embedding Engine (Qwen3-Embedding-0.6B)
        → FAISS Index Builder
          → Publish to GitHub Releases (.tar.gz)

RUNTIME (The Skill)
──────────────────────────────────────────
User query
  → search.py loads FAISS index + metadata
    → Embed query (local Ollama or fallback)
      → FAISS top-K nearest neighbors
        → Re-rank by quality/recency
          → Return JSON results to agent

DEEP DIVE (on-demand)
──────────────────────────────────────────
User wants details on a specific result
  → fetch_skill.py fetches raw SKILL.md from GitHub
    → Agent reads and analyzes full skill content
```

---

## 6. Data Schema

Each skill record in `metadata.jsonl`:

```json
{
  "id": "sha256_of_repo_url",
  "name": "kubernetes-deployer",
  "description": "Deploy and manage Kubernetes clusters with automated rollbacks, blue-green deployments, and health checking.",
  "repo_url": "https://github.com/user/k8s-deployer",
  "source": ["skillsmp", "clawhub"],
  "categories": ["devops", "kubernetes", "deployment"],
  "install_cmd": {
    "claude_code": "/plugin install k8s-deployer@marketplace-name",
    "codex": "cp SKILL.md ~/.codex/skills/",
    "openclaw": "clawhub install k8s-deployer"
  },
  "quality": {
    "stars": 142,
    "skillhub_rank": "A",
    "skillhub_score": 8.4,
    "safety_scan": "clean",
    "last_updated": "2026-02-15"
  },
  "embedding_text": "kubernetes-deployer. Deploy and manage Kubernetes clusters with automated rollbacks, blue-green deployments, and health checking. Categories: devops, kubernetes, deployment. Use when: deploying to k8s, managing kubernetes, container orchestration, helm charts, kubectl automation."
}
```

The `embedding_text` field is what gets vectorized. It concatenates name + description + categories + trigger phrases to give the embedding model maximum semantic surface area.

---

## 7. Embedding Strategy

### Model: Qwen3-Embedding-0.6B

- Open-source, runs locally via Ollama, zero cost
- Instruction-aware, supports MRL for flexible output dimensions
- 0.6B is fast on CPU (~1000 embeddings/min)
- #1 on MTEB multilingual leaderboard (8B variant)

### Embedding dimensions: 1024 (or 512 via MRL for smaller index)

### Query-time instruction prefix:
```
Instruct: Given a description of a task or use case, retrieve the most relevant agent skill.\nQuery: {user_query}
```

### Three-tier fallback for query embedding:

```
Tier 1: Local Ollama (qwen3-embedding:0.6b)
  → Best quality, zero cost
  → Check: ollama list | grep qwen3-embedding

Tier 2: Bundled sentence-transformers (all-MiniLM-L6-v2)
  → ~80MB, runs on CPU, works offline
  → Lower quality but always available

Tier 3: API call (OpenRouter or DeepInfra)
  → Qwen3-Embedding-8B at $0.01/M tokens
  → Check: OPENROUTER_API_KEY env var
```

**Critical:** Index must be built with the same model used at query time. Ship separate index files per model (`data/qwen/` and `data/minilm/`). The search script auto-detects which model is available and loads the corresponding index.

---

## 8. Crawler Specifications

### 8.1 SkillsMP Crawler (`crawlers/skillsmp_crawler.py`)

```
Method: GitHub Search API
Query: "filename:SKILL.md" (paginated, 1000 results/query)
For each repo:
  1. Fetch SKILL.md raw content via GitHub raw URL
  2. Parse YAML frontmatter (name, description)
  3. Fetch repo metadata (stars, last_updated, topics)
  4. Parse README.md first paragraph as supplemental description
Rate limit: 30 req/min (search), 5000 req/hr (general) with auth token
Output: data/raw/skillsmp.jsonl
```

### 8.2 ClawHub / OpenClaw Crawler (`crawlers/clawhub_crawler.py`)

```
Method: Parse VoltAgent/awesome-openclaw-skills README.md
Format: Category headers (##) followed by lines of "name - description"
Also try: ClawHub REST API if accessible, scrape clawhub pages as fallback
Output: data/raw/clawhub.jsonl
```

### 8.3 SkillHub Crawler (`crawlers/skillhub_crawler.py`)

```
Method: Web scraping of skillhub.club
Extract: name, description, repo_url, AI rank (S/A/B/C), 
         dimension scores (Practicality, Clarity, Automation, Quality, Impact)
Output: data/raw/skillhub.jsonl
```

### 8.4 Marketplace Crawler (`crawlers/marketplace_crawler.py`)

```
Target repos:
  - anthropics/skills (official)
  - daymade/claude-code-skills
  - mhattingpete/claude-skills-marketplace
  - alirezarezvani/claude-skills
  - Any repo with marketplace.json

Method: Clone/fetch each repo, parse marketplace.json + SKILL.md frontmatter
Output: data/raw/marketplace.jsonl
```

---

## 9. Pipeline Scripts

### 9.1 Normalize & Deduplicate (`pipeline/normalize.py`)

```
Input: data/raw/*.jsonl (all crawler outputs)
Steps:
  1. Load all records
  2. Canonical key: normalized GitHub repo URL (lowercase, strip .git)
  3. Merge metadata across sources (priority per §4.2)
  4. Build embedding_text: "{name}. {description}. Categories: {categories}. Use when: {triggers}."
  5. Quality filter:
     - Remove: no description
     - Remove: <2 stars AND not in any curated registry
     - Flag: security warnings from ClawHub
  6. Assign stable ID: sha256(canonical_repo_url)
Output: data/unified_skills.jsonl
```

### 9.2 Embed (`pipeline/embed.py`)

```
Input: data/unified_skills.jsonl
Steps:
  1. Extract embedding_text from each record
  2. Batch embed via:
     A) Local Ollama (preferred): ollama pull qwen3-embedding:0.6b
     B) API fallback: OpenRouter at $0.01/M tokens
  3. Also build MiniLM embeddings for fallback index
Output: data/qwen/embeddings.npy, data/minilm/embeddings.npy
```

### 9.3 Build Index (`pipeline/build_index.py`)

```
Input: embeddings.npy + unified_skills.jsonl
Steps:
  1. Load embeddings, normalize vectors (L2 norm for cosine similarity)
  2. Build FAISS index:
     - <50K skills: IndexFlatIP (exact search)
     - >50K skills: IndexIVFFlat with nlist=256
  3. Save index.faiss, metadata.jsonl (row-aligned), version.txt
  4. Package: skill-finder-index-YYYYMMDD.tar.gz
Output: data/{qwen,minilm}/index.faiss + metadata.jsonl

Size estimate:
  20K skills × 1024 dims × 4 bytes ≈ 80MB (FAISS)
  20K skills × ~500 bytes metadata ≈ 10MB (JSONL)
  Compressed total: ~50-80MB
```

---

## 10. Runtime Scripts

### 10.1 Search (`scripts/search.py`)

```
CLI: python search.py "deploy kubernetes clusters" [--top_k 5] [--min_quality 0]

Flow:
  1. Parse args
  2. Load FAISS index + metadata from data/
  3. Generate query embedding:
     a. Try Ollama (qwen3-embedding:0.6b) → load data/qwen/index.faiss
     b. Fallback: sentence-transformers (MiniLM) → load data/minilm/index.faiss
     c. Fallback: OpenRouter API → load data/qwen/index.faiss
  4. FAISS search for top_k * 3 candidates
  5. Post-filter by min_quality threshold
  6. Re-rank: similarity * 0.7 + quality_normalized * 0.2 + recency * 0.1
  7. Print top_k results as JSON to stdout

Output JSON:
[
  {
    "rank": 1,
    "name": "docker-compose-manager",
    "description": "...",
    "score": 0.94,
    "stars": 234,
    "rating": "A",
    "safety": "clean",
    "repo_url": "https://github.com/...",
    "install": {
      "claude_code": "/plugin install ...",
      "codex": "cp SKILL.md ~/.codex/skills/...",
      "openclaw": "clawhub install ..."
    }
  }
]

Dependencies: numpy, faiss-cpu, requests, sentence-transformers (optional)
```

### 10.2 Fetch Skill (`scripts/fetch_skill.py`)

```
CLI: python fetch_skill.py --repo "https://github.com/user/repo" [--output /tmp/skill.md]

Flow:
  1. Construct raw GitHub URL:
     https://raw.githubusercontent.com/{owner}/{repo}/main/SKILL.md
  2. Try /main/, /master/, /skills/*/SKILL.md
  3. Parse frontmatter + full content
  4. If repo has marketplace.json, also extract plugin metadata
  5. Print to stdout or save to --output path
```

### 10.3 Update Index (`scripts/update_index.py`)

```
CLI: python update_index.py

Flow:
  1. Read data/version.txt for current index date
  2. Check GitHub Releases API for latest release
  3. If newer, download .tar.gz, verify SHA256
  4. Extract into data/
  5. Print summary: "Updated from 2026-03-01 to 2026-03-08, 12,345 skills indexed"
```

---

## 11. SKILL.md Definition

```markdown
---
name: skill-finder
description: >
  Search and discover agent skills across all major registries
  (SkillsMP, ClawHub/OpenClaw, SkillHub, Anthropic official, and
  community marketplaces). Use when the user asks to find, search,
  recommend, or discover skills for a specific task. Covers 10,000+
  curated skills for Claude Code, OpenAI Codex, and OpenClaw.
---

# SkillFinder — Universal Agent Skill Discovery

## When to Use
- User asks "find a skill for X" or "is there a skill that does Y"
- User wants to compare skills for a use case
- User asks "what skills are available for [category]"
- User wants to install a skill but doesn't know which one

## How to Use

### Quick Search
python skills/skill-finder/scripts/search.py "deploy kubernetes clusters"

### Deep Dive
python skills/skill-finder/scripts/fetch_skill.py \
  --repo "https://github.com/user/k8s-deployer"

### Update Index
python skills/skill-finder/scripts/update_index.py

## Output Handling
- Present top 3-5 results ranked by relevance and quality
- Show: name, description, quality indicators, install command per platform, repo link
- Warn if a skill has no security scan
- Suggest user review full SKILL.md before installing
- If no good match found, suggest the user create their own skill
```

---

## 12. CI/CD: GitHub Actions

```yaml
# .github/workflows/update-index.yml
name: Update Skill Index
on:
  schedule:
    - cron: '0 6 * * 1'  # Weekly Monday 6AM UTC
  workflow_dispatch:

jobs:
  crawl-and-index:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt

      - name: Crawl all sources
        env: { GITHUB_TOKEN: '${{ secrets.GITHUB_TOKEN }}' }
        run: |
          python crawlers/skillsmp_crawler.py -o data/raw/skillsmp.jsonl
          python crawlers/clawhub_crawler.py -o data/raw/clawhub.jsonl
          python crawlers/skillhub_crawler.py -o data/raw/skillhub.jsonl
          python crawlers/marketplace_crawler.py -o data/raw/marketplace.jsonl

      - name: Normalize, embed, build index
        env: { OPENROUTER_API_KEY: '${{ secrets.OPENROUTER_API_KEY }}' }
        run: |
          python pipeline/normalize.py -o data/unified_skills.jsonl
          python pipeline/embed.py --model qwen3-embedding-8b --api openrouter
          python pipeline/build_index.py

      - name: Package and release
        run: |
          tar -czf skill-finder-index-$(date +%Y%m%d).tar.gz \
            data/index.faiss data/metadata.jsonl data/version.txt
      - uses: softprops/action-gh-release@v1
        with:
          tag_name: index-$(date +%Y%m%d)
          files: skill-finder-index-*.tar.gz
```

---

## 13. File Structure

```
skill-finder/
├── SKILL.md                          # Skill definition (§11)
├── plugin.json                       # Plugin manifest for marketplace
├── README.md                         # User documentation
│
├── scripts/                          # Runtime (shipped with skill)
│   ├── search.py                     # §10.1
│   ├── fetch_skill.py                # §10.2
│   ├── update_index.py               # §10.3
│   └── requirements.txt              # numpy, faiss-cpu, requests
│
├── data/                             # Pre-built index (downloaded)
│   ├── qwen/
│   │   ├── index.faiss
│   │   └── metadata.jsonl
│   ├── minilm/
│   │   ├── index.faiss
│   │   └── metadata.jsonl
│   └── version.txt
│
├── crawlers/                         # CI/CD only (not shipped)
│   ├── skillsmp_crawler.py           # §8.1
│   ├── clawhub_crawler.py            # §8.2
│   ├── skillhub_crawler.py           # §8.3
│   └── marketplace_crawler.py        # §8.4
│
├── pipeline/                         # CI/CD only (not shipped)
│   ├── normalize.py                  # §9.1
│   ├── embed.py                      # §9.2
│   └── build_index.py                # §9.3
│
├── tests/
│   ├── test_search_quality.py        # Recall@5, MRR benchmarks
│   ├── test_integration.py           # End-to-end
│   └── fixtures/
│       └── test_queries.json         # 100 labeled query→skill pairs
│
└── .github/workflows/
    └── update-index.yml              # §12
```

---

## 14. Cost Summary

| Item | Cost |
|------|------|
| Embedding 20K skills (weekly rebuild, OpenRouter) | ~$0.04/week |
| GitHub Actions compute | Free tier |
| Per-query search (local) | $0 |
| Per-query search (API fallback) | ~$0.00001 |
| User disk space | ~100-200MB |
| **Total monthly operating** | **< $1** |

---

## 15. Key API References

```python
# GitHub Search API
# GET https://api.github.com/search/code?q=filename:SKILL.md
# Headers: Authorization: token {GITHUB_TOKEN}
# Rate limit: 30 req/min (search), 5000 req/hr (general)

# OpenRouter Embedding API
# POST https://openrouter.ai/api/v1/embeddings
# Body: { "model": "qwen/qwen3-embedding-8b", "input": ["text"] }
# Cost: $0.01/M tokens

# Ollama Local Embedding
# curl http://localhost:11434/api/embed \
#   -d '{"model": "qwen3-embedding:0.6b", "input": "query text"}'

# FAISS Quick Reference
import faiss, numpy as np
index = faiss.IndexFlatIP(1024)       # Inner product = cosine after L2 norm
faiss.normalize_L2(vectors)
index.add(vectors)
faiss.write_index(index, "index.faiss")
# Search
index = faiss.read_index("index.faiss")
faiss.normalize_L2(query_vec)
scores, ids = index.search(query_vec, k=10)
```

---

## 16. Success Criteria

- Recall@5 ≥ 80% on 100-query labeled test suite
- Search latency < 200ms (local, p95)
- Index covers ≥ 10,000 unique quality skills
- Works fully offline with bundled MiniLM model
- Zero-config installation for Claude Code users
