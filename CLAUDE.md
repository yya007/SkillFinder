# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**SkillFinder** is a local-first agent skill discovery engine distributed as a Claude Code skill. It ships a pre-built FAISS vector index covering 10,000+ curated skills from major registries (SkillsMP, ClawHub/OpenClaw, SkillHub, Anthropic official). Users describe a use case → local embedding search → top matches with metadata and install links.

See [`docs/architecture.md`](docs/architecture.md) for the full system design.

## Repository Layout

```
skill-finder/
├── CLAUDE.md                         # This file
├── README.md                         # User-facing docs
├── SKILL.md                          # Skill definition (agent trigger)
├── plugin.json                       # Marketplace manifest
│
├── scripts/                          # Runtime — shipped with skill
│   ├── search.py                     # Vector search entry point
│   ├── fetch_skill.py                # Fetch raw SKILL.md from GitHub
│   ├── update_index.py               # Pull latest index release
│   └── requirements.txt
│
├── data/                             # Pre-built index (downloaded, gitignored)
│   ├── qwen/index.faiss
│   ├── qwen/metadata.jsonl
│   ├── minilm/index.faiss
│   ├── minilm/metadata.jsonl
│   └── version.txt
│
├── crawlers/                         # CI/CD only — not shipped to users
│   ├── skillsmp_crawler.py
│   ├── clawhub_crawler.py
│   ├── skillhub_crawler.py
│   └── marketplace_crawler.py
│
├── pipeline/                         # CI/CD only — not shipped to users
│   ├── normalize.py
│   ├── embed.py
│   └── build_index.py
│
├── tests/
│   ├── test_search_quality.py        # Recall@5, MRR benchmarks
│   ├── test_integration.py
│   └── fixtures/test_queries.json    # 100 labeled query→skill pairs
│
├── docs/                             # Design and product docs
│   ├── architecture.md
│   ├── data-sources.md
│   ├── embedding-strategy.md
│   └── prd/                          # PRD series (implementation roadmap)
│       ├── PRD-001-crawler-pipeline.md
│       ├── PRD-002-embedding-indexing.md
│       ├── PRD-003-search-runtime.md
│       ├── PRD-004-skill-integration.md
│       └── PRD-005-ci-cd-release.md
│
└── .github/workflows/
    └── update-index.yml              # Weekly crawl → embed → publish
```

## Key Design Decisions

- **Local-first:** FAISS runs on-device; zero infrastructure, zero per-query cost.
- **Dual index:** Qwen3-Embedding-0.6B (preferred, via Ollama) and MiniLM (bundled fallback). The index used at search time must match the index used at build time — they are stored separately under `data/qwen/` and `data/minilm/`.
- **Stable IDs:** `sha256(normalized_repo_url)`. Never use sequential integers.
- **Crawlers are CI-only:** The `crawlers/` and `pipeline/` directories are not included in the skill artifact shipped to users. Only `scripts/` and `data/` ship.
- **Dedup canonical key:** lowercase GitHub repo URL, `.git` suffix stripped.

## Development Commands

```bash
# Install dev dependencies
pip install -r scripts/requirements.txt
pip install -r requirements-dev.txt   # adds sentence-transformers, faiss-cpu, pytest

# Run a search against a local index
python scripts/search.py "deploy kubernetes clusters" --top_k 5

# Fetch a specific skill's SKILL.md
python scripts/fetch_skill.py --repo https://github.com/user/repo

# Check for index updates
python scripts/update_index.py

# Run all crawlers locally (requires GITHUB_TOKEN)
GITHUB_TOKEN=... python crawlers/skillsmp_crawler.py -o data/raw/skillsmp.jsonl

# Full pipeline (build index from scratch)
python pipeline/normalize.py -o data/unified_skills.jsonl
python pipeline/embed.py
python pipeline/build_index.py

# Tests
pytest tests/test_integration.py -v
pytest tests/test_search_quality.py -v   # needs data/*/index.faiss present
```

## Quality Bar

- Recall@5 ≥ 80% on the 100-query labeled test suite in `tests/fixtures/test_queries.json`
- p95 search latency < 200ms on CPU
- All crawlers must handle rate limits gracefully (exponential backoff, respect Retry-After)
- No secrets committed — use environment variables (`GITHUB_TOKEN`, `OPENROUTER_API_KEY`)
