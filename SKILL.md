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
  - update the skill index
  - rebuild the skill index
  - refresh skill index
  - how many skills are indexed
metadata:
  openclaw:
    requires:
      bins: [ollama, python3]
      env: []
    note: "Run `ollama pull qwen3-embedding:0.6b && python scripts/update_index.py` after install."
---

# skill-finder

Search <!-- stats:skill-count:start -->14,000+<!-- stats:skill-count:end --> curated agent skills by describing what you want to do.
Results are ranked by semantic similarity and include install commands.

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
3. Top matches are returned with name, description, and install command.

The index runs entirely on-device — no network requests are made during search.

## Agent Instructions

When this skill triggers, check whether the user wants to **search for skills** or **rebuild/update the index**, then follow the appropriate workflow below.

---

### Workflow A — Index Rebuild

Trigger phrases: "update the skill index", "rebuild the skill index", "refresh skill index", "how many skills are indexed"

Run each step in order. Stop and report any failure immediately.

**Step 1 — Backfill missing metadata (fast, safe to skip if already done)**
```bash
python -m pipeline.backfill_metadata data/raw/marketplace.jsonl data/raw/skillhub.jsonl data/raw/skillsmp.jsonl data/raw/clawhub.jsonl
```

**Step 2 — Normalize and deduplicate**
```bash
python pipeline/normalize.py data/raw/skillsmp.jsonl data/raw/clawhub.jsonl data/raw/skillhub.jsonl data/raw/marketplace.jsonl -o data/unified_skills.jsonl
```

**Step 3 — Embed** (requires Ollama running with `qwen3-embedding:0.6b`)
```bash
python pipeline/embed.py
```

**Step 4 — Build FAISS index**
```bash
python pipeline/build_index.py --embeddings data/embeddings.npy --skills data/unified_skills.jsonl --out-index data/index.faiss --out-meta data/metadata.jsonl --out-version data/version.txt
```

**Step 5 — Refresh docs**
```bash
python pipeline/update_docs.py
```

After Step 5, report the new skill count from `data/version.txt` and show the updated star distribution from README.md.

---

### Workflow B — Skill Search

When this skill triggers, follow this workflow exactly.

**Work silently.** Do all internal steps (query reformulation, search, parsing, reranking) without narrating them to the user. Only output the final results and the follow-up offer. Never say things like "I'm reformulating your query…" or "Running Tier 1 search…"

**Note:** Skills are third-party code — always check the Skill link before installing anything from an unfamiliar author.

> **Platform default:** Use `--platform claude_code` when running as Claude Code,
> `--platform openclaw` when running as OpenClaw, `--platform codex` when running
> as Codex. Always honour an explicit platform request from the user.

### Step 0 — Check availability

Before reformulating the query, verify the index and Ollama are available by running:
```bash
python scripts/search.py "test" --json
```
If this fails with `OllamaNotAvailableError` or `FileNotFoundError`, offer the user:
> "Local search is unavailable. I can fall back to a GitHub code search (unranked,
>  no quality signals). Set GITHUB_TOKEN for best results. Continue?"

If yes, add `--remote` to all search commands in Steps 2–3.

### Step 1 — Reformulate the query

Before searching, rewrite the user's request into a concise, keyword-rich search
query that improves semantic recall. Rules:

- Expand abbreviations (e.g. "k8s" → "kubernetes", "CI" → "continuous integration").
- Drop conversational filler ("I want to", "help me", "a skill that").
- Add closely related technical terms the index is likely to contain (e.g. "deploy
  kubernetes" → "kubernetes deployment orchestration kubectl cluster").
- Keep it concise — around 10–15 words is a good target.

**For specific queries** (e.g. "deploy k8s with helm rollback"), a single reformulation
is sufficient.

**For ambiguous or broad queries** (spans multiple technical domains, or uses generic
terms like "productivity", "AI", "automation"), generating 2–3 alternative reformulations
from different angles is optional but helpful. If you do, run all reformulations in
parallel through the tier logic, deduplicate results by `id`, then rerank the merged pool.

You do not need to show reformulated queries to the user unless they differ significantly
from what the user typed.

### Step 2 — Run the search (with tiered fallback)

Run searches in order, stopping as soon as you have ≥3 good matches:

**Tier 1 — Default (quality-first, platform-specific):**
```bash
python scripts/search.py "<reformulated query>" --json --propose 5 --platform <your_platform> --min_stars 10
```

**Tier 2 — Relax star threshold (if Tier 1 returns fewer than 3 good matches):**
```bash
python scripts/search.py "<reformulated query>" --json --propose 5 --platform <your_platform>
```

**Tier 3 — Cross-platform, no star filter (if Tier 2 still returns fewer than 3):**
```bash
python scripts/search.py "<reformulated query>" --json --propose 5
```

Rules:
- `--propose 5` is the default; scale up to `--propose 10` for broad queries or if the user asks for more options. If the user specifies a number of results (e.g. "give me 10"), use that directly.
- Use the platform default from the callout above. If the user explicitly asks for a skill on a different platform (e.g. "find an OpenClaw skill for…"), override with `--platform openclaw` or `--platform codex` and skip the platform-specific tiers.
- **ClawHub-sourced skills are OpenClaw-only** — they carry no `claude_code` install command and are excluded from Tier 1 and Tier 2 when `--platform claude_code` is active. They will appear in Tier 3 (cross-platform). When presenting them, always label them *(openclaw only — not installable in Claude Code)*.
- If the user specifies a star threshold explicitly (e.g. "only well-known skills" or "min 50 stars"), use that in place of `--min_stars 10`.
- If the user asks for fewer results (e.g. "just the top 3"), adjust the presented count accordingly.
- For Tier 2/3 results, note the relaxed filter in the response (see Step 5).

### Step 3 — Parse and threshold-check

Parse the JSON output. Each result includes a `sim_score` field (0.0–1.0) for
**internal use only — never show it to the user**.

Use ~0.4 as a soft threshold for "no strong matches." If all results across all
tiers have `sim_score < 0.4`, warn the user:
> "I didn't find strong matches for that query. Try rephrasing with more specific keywords."

### Step 4 — Rerank

Read each result's `description` against the user's stated intent. Prefer:

1. **Semantic relevance** to the user's actual task (primary signal)
2. **Stars** (`quality.stars`) — higher is more community-vetted
3. **Source** — weak tiebreaker only: when relevance and stars are similar, prefer officially vetted skills. Do not penalize or demote skills solely because of their source registry.

**Platform-source alignment rule:** `clawhub` is the OpenClaw registry. When the
user asked for `claude_code` (or any platform other than `openclaw`), treat
`clawhub`-only sourced skills as last resort — rank them below all other sources
regardless of star count, and only include them if no better-sourced alternatives
exist. If a skill's source includes both `clawhub` and another registry (e.g.
`marketplace`), use the non-clawhub source for ranking.

Present 3–5 results by default. Presenting more than 5 is optional — only do it if the user explicitly asks for more options or specifies a higher count.

### Step 5 — Present results

Use this exact format:

```
Found N skills for "<user's original query>":

1. **pptx** ⭐ 2,810 stars [official]
   Convert any .pptx file — read slides, extract text, generate presentations…
   Install: `/skill install pptx` _(if this fails, see Skill link below)_
   Skill: https://github.com/anthropics/skills/blob/main/skills/pptx/SKILL.md

2. **pptx-skill** ⭐ 126 stars
   Convert HTML slides to PowerPoint (PPTX) files…
   Install: `/plugin install pptx-skill` _(if this fails, see Skill link below)_
   Skill: https://github.com/vkehfdl1/slides-grab/blob/main/SKILL.md

> The skill index is updated periodically via web crawl and may not include the most recently published skills.
```

Use `skill_md_url` from the result for the `Skill:` link. Fall back to `repo_url` only if `skill_md_url` is empty.

**Badges and labels:**
- Add `[official]` only for `is_official: true` (anthropics/* marketplace skills).
- Append *(openclaw only — not installable in Claude Code)* when `platforms` does not include `claude_code`.
- Do **not** show raw source registry names (`skillsmp`, `clawhub`, `topic`, `skillhub`, `marketplace`) in the output.

**Install commands** (always use the verbatim value from the `install_cmd` field — never guess or construct it yourself):

| Condition | Install line |
|-----------|-------------|
| `is_official: true` (`anthropics/*`) | `Install: /skill install <name> _(if this fails, see Skill link below)_` |
| Community skill with `install_cmd` | `Install: /plugin install <name> _(if this fails, see Skill link below)_` |
| `install_cmd` is empty | `No direct install — see Skill link.` |

**Do NOT show `sim_score` to the user.**

Only show the `claude_code` entry from `install_cmd` by default. Mention other platforms exist only if the user asked.

If results came from Tier 2 or Tier 3 fallback, add a note after the freshness disclaimer:
> "These results include skills with fewer stars / from additional platforms because top-rated Claude Code-only matches were limited for this query."

### Step 6 — Offer next step

End every response with:

> "Want me to fetch the full SKILL.md for any of these before you install?"
