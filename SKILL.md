---
name: skill-finder
description: >
  Discover and recommend agent skills from SkillsMP, ClawHub, SkillHub, and the
  Anthropic marketplace. Search a local FAISS index with a natural language
  description of your task; get back ranked matches with install commands.
triggers:
  - find a skill
  - find skills
  - search for a skill
  - recommend a skill
  - install a skill
  - what skill should I use
  - discover agent skills
  - skill for
---

# skill-finder

Search 10,000+ curated agent skills by describing what you want to do.
Results are ranked by semantic similarity and include install commands.

## Safety Notice

> **Skills are third-party code.**
> SkillFinder indexes publicly available repositories but does **not** vet,
> audit, or endorse any skill. Before installing a skill, review its source
> repository to understand what it does and whether you trust it.
> Never install a skill from an unknown author without reading its code.

## Usage

```
/skill-finder
```

Then describe your task in plain language, e.g.:

- "deploy kubernetes clusters with rollback"
- "scrape web pages with JavaScript rendering"
- "write and run SQL migrations"

## How It Works

1. Your description is embedded locally using Qwen3-Embedding-0.6B via Ollama.
2. The embedding is compared against a pre-built FAISS index of 10,000+ skills.
3. Top matches are returned with name, description, source registry, and install command.

The index runs entirely on-device — no network requests are made during search.

## Agent Instructions

When this skill triggers, follow this workflow exactly:

### Step 1 — Run the search

```bash
python scripts/search.py "<user query>" --json --propose 10 --platform claude_code
```

- Default: always pass `--platform claude_code` — SkillFinder is a Claude Code skill.
- If the user mentions they're searching for OpenClaw skills, use `--platform openclaw` instead.
- If the user mentions Codex, use `--platform codex`.
- If the user explicitly asks for results across all platforms, omit `--platform`.
- `--propose 10` returns up to 30 candidates for you to rerank down to ≤10.

### Step 2 — Parse and threshold-check

Parse the JSON output. Each result includes a `sim_score` field (0.0–1.0) for **internal use only — never show it to the user**.

If all results have `sim_score < 0.4`, warn the user:
> "I didn't find strong matches for that query. Try rephrasing with more specific keywords."

### Step 3 — Rerank

Read each result's `description` against the user's stated intent. Prefer:
1. Higher `quality.stars` (more popular = more vetted by the community)
2. Curated sources: `marketplace` > `clawhub` > `skillsmp` > `skillhub`

Select the top ≤5 results to present.

### Step 4 — Present results

Use this exact format:

```
Found N skills for "<query>":

1. **pptx** ⭐ 2,810 stars — `marketplace` (official Anthropic)
   Convert any .pptx file — read slides, extract text, generate presentations…
   Install: `/skill install pptx`
   Repo: https://github.com/anthropics/skills

2. **pptx-skill** ⭐ 126 stars — `skillsmp`
   Convert HTML slides to PowerPoint (PPTX) files…
   Install: `/plugin install pptx-skill`
   Repo: https://github.com/vkehfdl1/slides-grab
```

Rules for `install_cmd` (always use the verbatim value from the `install_cmd` field — never guess or construct it yourself):

| Source | Install command |
|--------|----------------|
| `marketplace` | `/skill install <name>` |
| `skillsmp` / `clawhub` | `/plugin install <name>` |
| `skillhub` | No install command. Say: "SkillHub metadata only — no direct install; see repo." |
| `install_cmd` is empty | Same: "No direct install available; see repo." |

**Do NOT show `sim_score` to the user.**

### Step 5 — Offer next step

End every response with:

> "Want me to fetch the full SKILL.md for any of these before you install?"
