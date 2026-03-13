# SkillFinder

**Universal agent skill discovery — find the right skill in seconds.**

SkillFinder is a Claude Code skill that lets you search 10,000+ curated agent skills from all major registries using natural language. Everything runs locally: no API calls, no latency, no cost per query.

---

## What it does

Describe what you need → SkillFinder searches its pre-built vector index → returns ranked matches with quality scores and install commands:

```
You: find a skill for deploying kubernetes clusters
Claude: Here are the top matches:

1. k8s-deployer (⭐ 142, Rating: A, Safety: clean)
   Deploy and manage Kubernetes clusters with automated rollbacks and blue-green deployments.
   Install: /plugin install k8s-deployer

2. helm-chart-manager (⭐ 89, Rating: B, Safety: clean)
   ...
```

## Installation

```bash
# Clone into your skills directory
git clone https://github.com/yourusername/skill-finder ~/.claude/skills/skill-finder

# Install runtime dependencies
pip install -r ~/.claude/skills/skill-finder/scripts/requirements.txt

# Download the latest pre-built index (~100MB)
python ~/.claude/skills/skill-finder/scripts/update_index.py
```

That's it. Claude Code will auto-invoke SkillFinder when you ask to find or search for skills.

## Usage

### Natural language (recommended)

Just ask Claude:
- "find a skill for X"
- "is there a skill that does Y"
- "what skills are available for Kubernetes / Terraform / GitHub Actions"
- "compare skills for web scraping"

### CLI

```bash
# Search
python scripts/search.py "deploy kubernetes clusters" --top_k 5

# Get full SKILL.md for a specific result
python scripts/fetch_skill.py --repo https://github.com/user/k8s-deployer

# Update index to latest
python scripts/update_index.py
```

## Coverage

| Registry | Skills |
|----------|--------|
| SkillsMP | 400,000+ (top 10K by quality) |
| ClawHub / OpenClaw | 5,400+ |
| SkillHub | 7,000+ |
| Anthropic Official | ~50 |
| Community Marketplaces | ~500 |

After deduplication and quality filtering: **10,000–20,000 unique skills** in the default index.

## How it works

1. **Offline pipeline** (GitHub Actions, weekly): crawls all registries → deduplicates → embeds with Qwen3-Embedding-0.6B → builds FAISS index → publishes as a GitHub Release artifact.

2. **Runtime**: your query is embedded locally → FAISS nearest-neighbor search → re-ranked by quality and recency → results returned to agent.

3. **Deep dive**: agent can fetch the raw `SKILL.md` from any result's GitHub repo for detailed analysis before installing.

See [`docs/architecture.md`](docs/architecture.md) for the full technical design.

## Embedding models

| Tier | Model | When used |
|------|-------|-----------|
| 1 | Qwen3-Embedding-0.6B (Ollama) | Ollama running locally |
| 2 | all-MiniLM-L6-v2 (bundled) | Ollama not available |
| 3 | Qwen3-Embedding-8B (OpenRouter API) | Explicit API preference |

The index shipped with each tier is built with the same model used at query time.

## Requirements

- Python 3.10+
- `numpy`, `faiss-cpu`, `requests` (see `scripts/requirements.txt`)
- `sentence-transformers` — optional, for MiniLM fallback
- Ollama with `qwen3-embedding:0.6b` — optional, for best quality

## License

MIT
