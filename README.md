<div align="center">

# SkillFinder

**Find the right agent skill in seconds — local search, no API needed.**

[![npm](https://img.shields.io/npm/v/@yya007/skill-finder?label=npm&color=cb3837)](https://www.npmjs.com/package/@yya007/skill-finder)
[![Stars](https://img.shields.io/github/stars/yya007/SkillFinder?style=social)](https://github.com/yya007/SkillFinder)
[![MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Index updated](https://img.shields.io/badge/index-2026--03--17-blue)](data/version.txt)

</div>

SkillFinder searches <!-- stats:skill-count:start -->33,500+<!-- stats:skill-count:end --> curated agent skills from all major registries using natural language. Everything runs locally: no API calls, no latency, no cost per query. Works with any agent that supports **SKILL.md** — Claude Code, OpenClaw, Codex, and more.

```
You: /skill-finder deploy kubernetes clusters with rollback

Agent: Found 3 skills for "deploying kubernetes clusters":

1. **k8s-deployer** ⭐ 142 stars — `skillsmp`
   Deploy and manage Kubernetes clusters with automated rollbacks and blue-green deployments.
   Install: `/plugin install k8s-deployer`
   If this command fails, visit the Skill link and follow the repository's own install instructions.
   Skill: https://github.com/user/k8s-deployer/blob/main/SKILL.md

2. **helm-chart-manager** ⭐ 89 stars — `skillsmp`
   Manage Helm chart lifecycle: install, upgrade, diff, and rollback.
   Install: `/plugin install helm-chart-manager`
   If this command fails, visit the Skill link and follow the repository's own install instructions.
   Skill: https://github.com/user/helm-chart-manager/blob/main/SKILL.md

Want me to fetch the full SKILL.md for any of these before you install?
```

Or just describe what you need — the skill also triggers automatically from natural language:
_"find a skill for deploying kubernetes clusters"_, _"is there a skill for SQL migrations"_, etc.

---

## Why not just Google it?

The [Agent Skills](https://agentskills.io) open standard is supported by Claude Code, Cursor, VS Code Copilot, GitHub Copilot, OpenAI Codex, Gemini CLI, Goose, Roo Code, and other tools. Thousands of `SKILL.md` files exist across GitHub — with no unified way to find them.

**Searching the web manually:**

```
$ # Google: "kubernetes deploy claude code skill"
→ 2,840,000 results — blog posts, Stack Overflow, unrelated GitHub repos
→ No quality signals: is this repo maintained? 5 stars or 5,000?
→ No install commands visible in results
→ GitHub code search requires login; finds files, not skills as units
→ May take 20–30 minutes to find 3 relevant options — if they exist at all
```

**SkillFinder:**

```
You: /skill-finder deploy kubernetes clusters with rollback

Agent: Found 3 skills for "deploying kubernetes clusters":

1. k8s-deployer  ⭐ 142
   Deploy and manage Kubernetes clusters with rollbacks and blue-green deploys.
   Install: /plugin install k8s-deployer

2. helm-chart-manager  ⭐ 89
   Manage Helm chart lifecycle: install, upgrade, diff, and rollback.
   Install: /plugin install helm-chart-manager

3. terraform-k8s  ⭐ 61
   Provision Kubernetes infrastructure on AWS/GCP/Azure via Terraform.
   Install: /plugin install terraform-k8s
```

Or query the index directly from the CLI:

```bash
$ python scripts/search.py "deploy kubernetes clusters" --no-json --propose 5
```

Results in **< 200 ms**, ranked by semantic relevance and community trust, install commands included.

---

## For end users — install and search

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com/install) installed locally

### Install

**Option 1 — npx (easiest, auto-detects your agent):**

```bash
npx skills add yya007/SkillFinder
```

Uses the [`skills`](https://skills.sh) CLI — detects which agents you have installed and copies files to the right directory automatically.

**Option 2 — npm:**

```bash
npm install -g @yya007/skill-finder
```

Then copy to your agent's skills directory:

| Agent | Skills directory |
|-------|-----------------|
| Claude Code | `~/.claude/skills/skill-finder` |
| OpenClaw | `~/.openclaw/skills/skill-finder` |
| Codex | `~/.codex/skills/skill-finder` |

```bash
# Copy once (pick your agent's dir from the table above):
cp -r "$(npm root -g)/@yya007/skill-finder" ~/.claude/skills/skill-finder
```

**Finish setup (all platforms):**

```bash
cd ~/.claude/skills/skill-finder   # or your platform's skills dir
pip install -r scripts/requirements.txt
ollama pull qwen3-embedding:0.6b
```

That's it. Ask your agent: _"find a skill for X"_ or invoke the skill explicitly  _"/skill-finder"_

> **OpenClaw:** ClawHub listing coming soon. Until then, install via npm or
> `git clone https://github.com/yya007/SkillFinder ~/.openclaw/skills/skill-finder`.

<details>
<summary>Alternative: git clone</summary>

```bash
# Claude Code
git clone https://github.com/yya007/SkillFinder ~/.claude/skills/skill-finder

# Codex
git clone https://github.com/yya007/SkillFinder ~/.codex/skills/skill-finder

# OpenClaw
git clone https://github.com/yya007/SkillFinder ~/.openclaw/skills/skill-finder
```

Then run the finish-setup block above.

</details>

### Usage — natural language (recommended)

When using an agent (Claude Code, Codex, OpenClaw, etc.), just describe what you need:

- "find a skill for deploying kubernetes clusters"
- "is there a skill that writes and runs SQL migrations"
- "what skills are available for web scraping"
- "compare skills for Terraform infrastructure"

### Usage — CLI

> **CLI vs agent:** `scripts/search.py` returns a raw vector-similarity ranking.
> The agent layer (query expansion, tiered fallback, reranking by intent) is not
> replicated here. Use the CLI for scripting or development; use the agent
> integration for discovery in normal use.

```bash
cd ~/.claude/skills/skill-finder

# Search all skills (all platforms, recommended)
python scripts/search.py "deploy kubernetes clusters" --no-json

# Optionally filter to a specific platform
python scripts/search.py "deploy kubernetes clusters" --platform claude_code

# Require skills that passed ClawHub safety scan
python scripts/search.py "web scraping" --safety_only

# Filter by minimum star count
python scripts/search.py "ci/cd pipeline" --min_stars 50

# Human-readable output instead of JSON
python scripts/search.py "pptx presentation" --no-json --propose 5

# Fetch a specific skill's full SKILL.md before installing
python scripts/fetch_skill.py --repo https://github.com/user/k8s-deployer
```

The `--platform` flag is optional and accepts: `claude_code`, `openclaw`, `codex`.

---

## What gets indexed (and what gets filtered out)

SkillFinder indexes skills from five sources: **SkillsMP** (GitHub code search), **ClawHub/OpenClaw** (awesome list + org/topic search), **SkillHub** (web scrape), the **Anthropic official marketplace**, and **GitHub topic tags** (`claude-skill`, `codex-skill`, `agent-skill`, etc.).

A skill is **kept** if it passes both of the following:
- Has a non-empty description (from SKILL.md frontmatter or README)
- ≥ 10 GitHub stars

A skill is **dropped** if either condition is missing — registry membership and SkillHub ratings do not override the star threshold.

Safety: ClawHub records carry a `safety_scan` result from VirusTotal. Other sources do not. Always review a skill's repository before installing it.

---

## For developers — build the index from scratch

Use this if you want to run the full crawl-embed-index pipeline locally, add a new registry, or contribute to the project.

### Additional prerequisites

- A GitHub personal access token with `public_repo` read scope (for crawlers)
- Ollama with `qwen3-embedding:0.6b` (same model used at runtime)

### Setup

```bash
git clone https://github.com/yya007/SkillFinder
cd SkillFinder
pip install -r requirements-dev.txt
export GITHUB_TOKEN=ghp_your_token_here
```

### Step 1 — Crawl registries

Each crawler writes a raw JSONL file to `data/raw/`. Run them independently; they handle rate limits automatically.

```bash
# SkillsMP (GitHub code search for SKILL.md files)
python -m crawlers.skillsmp_crawler -o data/raw/skillsmp.jsonl

# ClawHub / OpenClaw (awesome list + org/topic discovery, requires GITHUB_TOKEN)
python -m crawlers.clawhub_crawler -o data/raw/clawhub.jsonl

# GitHub topic search (claude-skill, codex-skill, agent-skill, etc.)
python -m crawlers.topic_crawler -o data/raw/topic.jsonl --data-dir data/raw

# SkillHub
python -m crawlers.skillhub_crawler -o data/raw/skillhub.jsonl

# Anthropic official marketplace
python -m crawlers.marketplace_crawler -o data/raw/marketplace.jsonl
```

Each crawler accepts `--limit N` (cap records for testing) and `--log-level DEBUG`.

### Step 2 — Normalize and deduplicate

```bash
python pipeline/normalize.py -o data/unified_skills.jsonl
```

Merges all raw sources, deduplicates by canonical repo URL, applies quality filters, and builds embedding text.

### Step 3 — Embed

```bash
python pipeline/embed.py
```

Calls local Ollama (`qwen3-embedding:0.6b`) to embed all skills. Writes `data/embeddings.npy`.

### Step 4 — Build FAISS index

```bash
python pipeline/build_index.py
```

Produces `data/index.faiss` and `data/metadata.jsonl`. These are the runtime index files committed to the repo and also published as a GitHub Release artifact for weekly updates.

### Run tests

```bash
# Unit + integration tests (no Ollama or network required)
pytest tests/ -v

# Full quality benchmark (requires data/index.faiss and Ollama running)
pytest tests/quality/ -v -m quality
```

### Contributing

1. Fork the repo and create a feature branch.
2. Add or update tests for any changed behaviour.
3. Run `pytest tests/ -v` — all tests must pass.
4. Open a pull request with a clear description of the change.

---

## Coverage

<!-- stats:coverage-table:start -->
| Registry | Crawler | Skills in index |
|----------|---------|----------------:|
| SkillsMP (GitHub code search) | `skillsmp_crawler.py` | 386 |
| ClawHub / OpenClaw | `clawhub_crawler.py` | 4,605 |
| SkillHub | `skillhub_crawler.py` | 4,421 |
| Anthropic official marketplace | `marketplace_crawler.py` | 28,185 |
| GitHub topics | `topic_crawler.py` | 0 |
| **Total (after dedup)** | | **33,827** |
<!-- stats:coverage-table:end -->

## Star Distribution

<!-- stats:index-distribution:start -->
| Stars | Skills | Distribution |
|-------|-------:|:-------------|
| 10–49 | 1,956 | █░░░░░░░░░░░░░░░░░░░ 6% |
| 50–99 | 1,254 | █░░░░░░░░░░░░░░░░░░░ 4% |
| 100–499 | 15,824 | █████████░░░░░░░░░░░ 47% |
| 500–999 | 970 | █░░░░░░░░░░░░░░░░░░░ 3% |
| 1k–5k | 8,715 | █████░░░░░░░░░░░░░░░ 26% |
| 5k+ | 5,108 | ███░░░░░░░░░░░░░░░░░ 15% |
| **Total** | **33,827** | |
<!-- stats:index-distribution:end -->

> **Not shown:** skills with 0–9 stars are dropped from the index regardless of which registry they come from. A non-empty description is also required.

---

## How it works

1. **Weekly CI pipeline** (GitHub Actions): crawls all registries → deduplicates → embeds with Qwen3-Embedding-0.6B via Ollama → builds FAISS index → commits the updated index to the repo and publishes a GitHub Release artifact for users who want to pull updates manually via `update_index.py`.

2. **Runtime** (your machine): query is embedded locally via Ollama → FAISS nearest-neighbor search (< 200 ms on CPU) → candidate pool returned to the agent → agent reranks and presents the best matches.

3. **Deep dive**: the agent can fetch the raw `SKILL.md` from any result's repo before you install it.

The same embedding model (`qwen3-embedding:0.6b`) is used in CI and at runtime — the index is always compatible.

See [`docs/architecture.md`](docs/architecture.md) for the full technical design.

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Python | 3.10+ |
| numpy | ≥ 1.26 |
| faiss-cpu | ≥ 1.8 |
| requests | ≥ 2.31 |
| pyyaml | ≥ 6.0 |
| Ollama | latest |
| qwen3-embedding:0.6b | via Ollama |

---

## Acknowledgements

SkillFinder would not be possible without the registries and communities that host and curate agent skills:

| Source | What it provides |
|--------|-----------------|
| **[SkillsMP](https://github.com/topics/skill-finder) / GitHub** | Open-indexed `SKILL.md` files discovered via GitHub code search and topic tags |
| **[ClawHub](https://github.com/ClawHub/awesome-openclaw) / OpenClaw** | Community-curated awesome list of OpenClaw skills with safety scan metadata |
| **[SkillHub](https://skillhub.dev)** | Ranked and editorially reviewed skill registry |
| **[Anthropic marketplace](https://claude.ai/skills)** | Official Claude skills maintained by Anthropic |

Thank you to every skill author who publishes their work openly.

---

## Star this repo

If SkillFinder saves you time, please **star this repo** — it helps others discover the project and motivates continued development.

[![Star on GitHub](https://img.shields.io/github/stars/yya007/SkillFinder?style=social)](https://github.com/yya007/SkillFinder)

Also consider starring the skills you find useful — it's the best way to support their authors.

---

## License

[MIT](LICENSE) © yya007
