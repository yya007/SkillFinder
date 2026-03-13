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
# Search (returns 30 candidates, Claude proposes best 10)
python scripts/search.py "deploy kubernetes clusters" --propose 10

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

1. **Offline pipeline** (GitHub Actions, weekly): crawls all registries → deduplicates → embeds with Qwen3-Embedding-0.6B via Ollama → builds FAISS index → publishes as a GitHub Release artifact.

2. **Runtime**: your query is embedded locally via Ollama → FAISS nearest-neighbor search → candidate pool returned to Claude → Claude selects and ranks the best matches.

3. **Deep dive**: agent can fetch the raw `SKILL.md` from any result's GitHub repo for detailed analysis before installing.

See [`docs/architecture.md`](docs/architecture.md) for the full technical design.

## Embedding model

**Qwen3-Embedding-0.6B via Ollama** — the same model is used to build the index (CI) and embed queries (runtime), so there is no compatibility complexity.

## Requirements

- Python 3.10+
- `numpy`, `faiss-cpu`, `requests` (see `scripts/requirements.txt`)
- [Ollama](https://ollama.com/install) with `qwen3-embedding:0.6b` pulled — required

## License

MIT
