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
metadata:
  openclaw:
    requires:
      bins: [ollama, python3]
      env: []
    note: "Run `ollama pull qwen3-embedding:0.6b && python scripts/update_index.py` after install."
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

> **Platform default:** Use `--platform claude_code` when running as Claude Code,
> `--platform openclaw` when running as OpenClaw, `--platform codex` when running
> as Codex. Always honour an explicit platform request from the user.

### Step 1 — Reformulate the query

Before searching, rewrite the user's request into a concise, keyword-rich search
query that improves semantic recall. Rules:

- Expand abbreviations (e.g. "k8s" → "kubernetes", "CI" → "continuous integration").
- Drop conversational filler ("I want to", "help me", "a skill that").
- Add closely related technical terms the index is likely to contain (e.g. "deploy
  kubernetes" → "kubernetes deployment orchestration kubectl cluster").
- Keep it to one sentence, ≤15 words.

Use this reformulated query in all subsequent search calls. You do not need to
show the reformulated query to the user unless it differs significantly from what
they typed.

### Step 2 — Run the search (with tiered fallback)

Run searches in order, stopping as soon as you have ≥3 results with `sim_score ≥ 0.55`:

**Tier 1 — Default (quality-first, platform-specific):**
```bash
python scripts/search.py "<reformulated query>" --json --propose 10 --platform <your_platform> --min_stars 10
```

**Tier 2 — Relax star threshold (if Tier 1 returns fewer than 3 results above 0.55):**
```bash
python scripts/search.py "<reformulated query>" --json --propose 10 --platform <your_platform>
```

**Tier 3 — Cross-platform, no star filter (if Tier 2 still returns fewer than 3):**
```bash
python scripts/search.py "<reformulated query>" --json --propose 10
```

Rules:
- `--propose 10` returns up to 30 candidates for you to rerank down to ≤10.
- Use the platform default from the callout above. If the user explicitly asks for a skill on a different platform (e.g. "find an OpenClaw skill for…"), override with `--platform openclaw` or `--platform codex` and skip the platform-specific tiers.
- **ClawHub-sourced skills are OpenClaw-only** — they carry no `claude_code` install command and are excluded from Tier 1 and Tier 2 when `--platform claude_code` is active. They will appear in Tier 3 (cross-platform). When presenting them, always label them *(openclaw only — not installable in Claude Code)*.
- If the user specifies a star threshold explicitly, use it in place of `--min_stars 10`.
- For Tier 2/3 results, note the relaxed filter in the response (see Step 5).

### Step 3 — Parse and threshold-check

Parse the JSON output. Each result includes a `sim_score` field (0.0–1.0) for
**internal use only — never show it to the user**.

If all results across all tiers have `sim_score < 0.4`, warn the user:
> "I didn't find strong matches for that query. Try rephrasing with more specific keywords."

### Step 4 — Rerank

Read each result's `description` against the user's stated intent. Prefer:
1. Higher semantic relevance to the user's actual task
2. Higher `quality.stars` (more popular = more vetted by the community)
3. Source preference order: `marketplace` > `skillsmp` > `skillhub` > `clawhub`

**Platform-source alignment rule:** `clawhub` is the OpenClaw registry. When the
user asked for `claude_code` (or any platform other than `openclaw`), treat
`clawhub`-only sourced skills as last resort — rank them below all other sources
regardless of star count, and only include them if no better-sourced alternatives
exist. If a skill's source includes both `clawhub` and another registry (e.g.
`marketplace`), use the non-clawhub source for ranking.

Select the top ≤5 results to present.

### Step 5 — Present results

Use this exact format:

```
Found N skills for "<query>":

1. **pptx** ⭐ 2,810 stars — `marketplace` (official Anthropic)
   Convert any .pptx file — read slides, extract text, generate presentations…
   Install: `/skill install pptx`
   Skill: https://github.com/anthropics/skills/blob/main/skills/pptx/SKILL.md

2. **pptx-skill** ⭐ 126 stars — `skillsmp`
   Convert HTML slides to PowerPoint (PPTX) files…
   Install: `/plugin install pptx-skill`
   Skill: https://github.com/vkehfdl1/slides-grab/blob/main/SKILL.md
```

Use `skill_md_url` from the result for the `Skill:` link. Fall back to `repo_url` only if `skill_md_url` is empty.

Rules for `install_cmd` (always use the verbatim value from the `install_cmd` field — never guess or construct it yourself):

| Source | Install command | Reliability |
|--------|----------------|-------------|
| `marketplace` (official `anthropics/*`) | `/skill install <name>` | Reliable — name is registered in the Anthropic registry |
| `skillsmp` / community `marketplace` | `/plugin install <name>` | Best-effort — name must match the skill's identifier in the plugin registry |
| `skillhub` / `install_cmd` is empty | No install command | Say: "No direct install available; see Skill link." |

After every install command, add: _"If this command fails, visit the Skill link above and follow the repository's own install instructions."_

**Platform labeling:** Check each result's `platforms` list before presenting it:
- If `platforms` does **not** include `claude_code`, append a note after the source label. Examples:
  - `clawhub` *(openclaw only — not installable in Claude Code)*
  - `skillhub` *(codex only — not installable in Claude Code)*
- Only show the `claude_code` entry from `install_cmd` by default. Mention other platforms exist only if the user asked.

**Do NOT show `sim_score` to the user.**

If results came from Tier 2 or Tier 3 fallback, add a note after the list:
> "These results include skills with fewer stars / from additional platforms because
> top-rated Claude Code-only matches were limited for this query."

### Step 6 — Offer next step

End every response with:

> "Want me to fetch the full SKILL.md for any of these before you install?"
