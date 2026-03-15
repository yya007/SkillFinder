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
