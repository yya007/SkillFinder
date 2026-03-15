# PRD-004: Skill Definition & Agent Integration

**Status:** Planned
**Phase:** 4 of 5
**Depends on:** PRD-003 (scripts must work)
**Blocks:** PRD-005

---

## Problem

SkillFinder needs to be packaged as a proper Claude Code skill with a `SKILL.md` definition that triggers correctly, a `plugin.json` for marketplace distribution, and clear behavior guidelines so the agent handles edge cases well (no results, security warnings, cross-platform installs).

## Goals

- Write `SKILL.md` with triggers that cover the full range of user intent
- Define `plugin.json` for Claude Code Skills Marketplace
- Specify agent behavior: how to present results, handle warnings, handle failures
- Define the installation experience for end users

## Non-Goals

- Building a web UI or browser extension
- Supporting other agent platforms (Codex, OpenClaw) as first-class hosts — users of those platforms can still find skills, but SkillFinder itself is a Claude Code skill

---

## Functional Requirements

### F1 — SKILL.md

The `SKILL.md` at the repo root is what Claude Code reads to decide when to invoke SkillFinder and how to use it.

**Trigger conditions (when Claude should invoke SkillFinder):**
- "find a skill for X" / "search for skills that do X"
- "is there a skill that does Y" / "does a skill exist for Y"
- "what skills are available for [topic]"
- "recommend a skill for X" / "suggest skills for X"
- "compare skills for X"
- "I want to install a skill for X but don't know which one"
- "discover skills" / "browse skills for [category]"

**When NOT to invoke (to avoid false positives):**
- User asks about a specific skill by name → direct GitHub lookup, not search
- User asks how to write/create a skill → refer to SKILL.md docs, not SkillFinder
- "skill" used in a non-agent context (e.g., "SQL skills", "interview skills")

**Agent behavior:**
1. Parse user intent: extract the semantic query, desired platform (if stated), and how many results the user wants (default: up to 10)
2. Translate any platform mentions: "Claude Code skill" → `--platform claude_code`, "Codex skill" → `--platform codex`, "OpenClaw skill" → `--platform openclaw`
3. Run `search.py "<query>" --propose N [--platform P] [--safety_only]` where N is what the user asked for (or 10 if unspecified)
4. Read the full candidate pool returned (up to N×3 records); evaluate each against the user's intent — consider description, categories, quality signals, safety, and platform compatibility
5. Select the best ≤ N candidates based on agent judgment; present them in ranked order (not FAISS order)
6. For each presented result: show name, description, quality indicators, install command for the relevant platform(s)
7. Warn prominently if `safety_flag: true` — show warning before the install command
8. If `skillhub_rank` is C or missing, note that quality is unverified
9. Offer to fetch the full `SKILL.md` for any result before the user installs
10. If the candidate pool is empty: suggest the user create their own skill; link to SKILL.md format docs
11. If search fails (index not found): run `update_index.py`, then retry once

**Result presentation format:**

```
Found 4 skills for "deploy kubernetes" (reviewed 30 candidates):

1. **kubernetes-deployer** ⭐ 142 | Rating: A | Safety: ✅ clean
   Deploy and manage Kubernetes clusters with automated rollbacks and blue-green deployments.
   Install (Claude Code): `/plugin install k8s-deployer`
   Repo: https://github.com/user/k8s-deployer

2. **helm-chart-manager** ⭐ 89 | Rating: B | Safety: ✅ clean
   ...

Want me to fetch the full SKILL.md for any of these before installing?
```

**Safety warning format:**
```
⚠️ Warning: kubernetes-deployer has a security flag from ClawHub's scan.
Review the repository carefully before installing.
```

### F2 — plugin.json

Standard Claude Code Skills Marketplace manifest:

```json
{
  "name": "skill-finder",
  "version": "1.0.0",
  "description": "Search and discover 10,000+ agent skills from all major registries. Find the right skill for any task in seconds.",
  "author": "your-username",
  "license": "MIT",
  "homepage": "https://github.com/your-username/skill-finder",
  "skill": "SKILL.md",
  "scripts": {
    "search": "scripts/search.py",
    "fetch": "scripts/fetch_skill.py",
    "update": "scripts/update_index.py"
  },
  "requirements": "scripts/requirements.txt",
  "postinstall": "python scripts/update_index.py",
  "categories": ["utilities", "discovery", "search"],
  "keywords": ["skills", "plugins", "search", "discovery", "registry"]
}
```

The `postinstall` hook automatically downloads the pre-built index on first install.

### F3 — Installation Experience

**First-time install:**

```bash
/plugin install skill-finder
```

Expected output:
```
Installing skill-finder...
Installing dependencies from scripts/requirements.txt...
Running post-install: downloading pre-built index...
Downloading index 2026-03-10 (87MB)...
✓ Index downloaded: 14,823 skills indexed.

SkillFinder is ready. Try: "find a skill for kubernetes deployment"
```

**Update:**
```
/plugin update skill-finder
```

The update flow runs `update_index.py` which checks the GitHub release and downloads if a newer version exists.

### F4 — Cross-Platform Install Commands

For each result, show install commands only for platforms relevant to the user's context:

- If the user mentioned a platform (or `--platform` was passed): show only that platform's command
- Default (no platform specified): show `claude_code` command; mention other platforms if the skill supports them
- Never dump all platform commands at once — show one primary install command and optionally note others exist

Examples:
- "find a Codex skill for X" → show only `codex` install path
- "find a skill for X" (no platform) → show Claude Code install; add "(also available on OpenClaw)" if applicable

---

## SKILL.md Content

```markdown
---
name: skill-finder
description: >
  Search and discover agent skills across all major registries
  (SkillsMP, ClawHub/OpenClaw, SkillHub, Anthropic official, and
  community marketplaces). Use when the user asks to find, search,
  recommend, discover, or compare skills for a specific task or use case.
  Covers 10,000+ curated skills for Claude Code, OpenAI Codex, and OpenClaw.
triggers:
  - find a skill for
  - search for skills
  - is there a skill that
  - what skills are available for
  - recommend a skill
  - discover skills
  - skill for [task]
---

# SkillFinder — Universal Agent Skill Discovery

## When to Use

- User asks "find a skill for X" or "is there a skill that does Y"
- User wants to compare or choose between skills for a use case
- User asks "what skills are available for [topic/category]"
- User wants to install a skill but doesn't know which one to pick

## Do Not Use When

- User asks about a specific named skill → look it up directly
- User wants to write a new skill → refer to SKILL.md documentation
- "Skill" is used in a non-agent context (e.g., "Python skills", "soft skills")

## Usage

### Search (basic)
```bash
python skills/skill-finder/scripts/search.py "deploy kubernetes clusters"
```

### Search with platform filter
```bash
# Claude Code skills only
python skills/skill-finder/scripts/search.py "deploy kubernetes" --platform claude_code

# Multiple platforms (OR)
python skills/skill-finder/scripts/search.py "web scraping" --platform claude_code --platform openclaw

# Codex skills, exclude flagged
python skills/skill-finder/scripts/search.py "git automation" --platform codex --safety_only
```

### Request more results
```bash
# User asks for 15 → returns 45 candidates for agent to review
python skills/skill-finder/scripts/search.py "terraform" --propose 15
```

### Fetch full SKILL.md for a result
```bash
python skills/skill-finder/scripts/fetch_skill.py \
  --repo "https://github.com/user/k8s-deployer"
```

### Update index to latest
```bash
python skills/skill-finder/scripts/update_index.py
```

## How to Present Results

1. Read all candidates returned by search.py (up to propose_n × 3)
2. Select the best ≤ propose_n based on your own judgment (description relevance, quality, safety, platform fit)
3. Present your selections ranked by how well they match the user's intent — not by FAISS score order
4. Warn if `safety_flag: true` — highlight prominently before showing install cmd
5. Show install command only for the platform(s) relevant to the user's context
6. Offer to fetch full SKILL.md before the user installs
7. If no good match in the candidate pool: say so and suggest the user create their own skill

## Error Handling

- Index not found: run `update_index.py`, then retry the search once
- Search returns empty: tell the user no matching skills were found; suggest creating one
- Network error in fetch_skill.py: report which URL was attempted; suggest manual GitHub visit
```

---

## Success Criteria

| Metric | Target |
|--------|--------|
| Trigger precision (invoke when appropriate) | ≥ 95% of skill-search queries |
| Trigger recall (don't invoke when not appropriate) | False positive rate < 5% |
| Result presentation includes all required fields | 100% |
| Safety flag shown before install command | 100% of flagged results |
| Postinstall index download completes | Success on clean install |

---

## Resolved Questions

**Agent handling of quality filter queries** — Resolved. The `--min_stars N` flag (implemented in `search.py`) handles star-count filtering. The agent defaults to `--min_stars 10` to filter obvious noise, with a fallback retry omitting `--min_stars` if the filtered result set is empty. Qualitative quality signals (SkillHub ratings like "S", "A") are passed as metadata in the JSON output for agent-side reranking rather than as a hard CLI filter, since thresholds vary by registry.

**`plugin.json` postinstall mechanism** — Resolved. The Claude Code plugin spec (`.claude-plugin/plugin.json`) does not have a standard `postinstall` hook. Install-time setup (downloading the FAISS index) is handled via explicit user instructions in the README: `pip install -r scripts/requirements.txt && python scripts/update_index.py`. A `plugin.json` manifest has been created at the repo root with an `install.steps` field documenting these commands for marketplace tooling that supports it.
