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
1. Run `search.py` with the user's query (paraphrase if needed to be more descriptive)
2. Present top 3–5 results in a readable format (not raw JSON)
3. For each result, show: name, description, quality indicators, install command
4. Warn prominently if `safety_flag: true`
5. If `skillhub_rank` is C or lower, note that quality is unverified
6. Offer to fetch the full `SKILL.md` for any result before installing
7. If no results (empty array): suggest the user create their own skill; link to SKILL.md format docs
8. If search fails (index not found): run `update_index.py`, then retry once

**Result presentation format:**

```
Found 3 skills for "deploy kubernetes":

1. **kubernetes-deployer** ⭐ 142 | Rating: A | Safety: ✅ clean
   Deploy and manage Kubernetes clusters with automated rollbacks and blue-green deployments.
   Install: `/plugin install k8s-deployer`
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

For each search result, show the most relevant install command for the detected context:

- Claude Code context (always): `/plugin install <name>`
- If result has `install_cmd.openclaw`: also show `clawhub install <name>`

Do not show install commands for platforms the user hasn't mentioned. Avoid overwhelming users with every platform's command.

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

### Search
```bash
python skills/skill-finder/scripts/search.py "deploy kubernetes clusters"
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

1. Show top 3–5 results with name, description, quality indicators, install command
2. Warn if `safety_flag: true` — highlight prominently before showing install cmd
3. Offer to fetch full SKILL.md before the user installs
4. If no good match: suggest the user create their own skill

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

## Open Questions

- How should the agent handle queries with strong quality filters ("only S-rated skills for terraform")? Should it pass `--min_quality` automatically or ask the user?
- For the `plugin.json` `postinstall` field — is this a standard Claude Code Skills Marketplace feature? Need to verify the actual postinstall mechanism.
