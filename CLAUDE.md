# CLAUDE.md

SkillFinder — local-first agent skill discovery (FAISS + Qwen3-Embedding-0.6B via Ollama).
Ships as a skill for Claude Code, OpenClaw, and Codex.

## Commands

| Task | Command |
|------|---------|
| Install dev deps | `pip install -r requirements-dev.txt` |
| Lint | `ruff check skill/scripts/ pipeline/ crawlers/` |
| Test (unit, no network) | `pytest` |
| Run a search | `python skill/scripts/search.py "deploy k8s" --no-json` |
| Crawl (requires GITHUB_TOKEN) | `python -m crawlers.skillsmp_crawler -o data/raw/skillsmp.jsonl` |
| Full pipeline | `pipeline/normalize.py → embed.py → build_index.py` |

## Conventions

- **Stable IDs:** `sha256(skill_md_url)` for monorepo skills (multiple SKILL.md files in one repo, e.g. `anthropics/skills`), else `sha256(canonical_repo_url)`. Never sequential integers. **Note:** IDs for monorepo skills changed in this release — see BACKLOG.md §"Document monorepo ID change".
- **Canonical URL:** lowercase, `.git` suffix stripped.
- **Platform values:** exactly `claude_code`, `openclaw`, `codex` — no other strings.
- **Source values:** exactly `skillsmp`, `clawhub`, `skillhub`, `marketplace`.
- **No cross-imports:** `skill/scripts/` must never import from `crawlers/` or `pipeline/`.
- **Schema first:** add new metadata fields to `docs/data-sources.md` before code.

## Layout

| Directory | Purpose |
|-----------|---------|
| `skill/` | **Deployable unit** — sparse-clone or `cp` this to install |
| `skill/scripts/` | Runtime Python scripts — shipped to end users |
| `skill/skills/` | Companion sub-skills (update-index, crawl-sources, npm-release) |
| `skill/data/` | Committed index files (`index.faiss`, `metadata.jsonl`, `version.txt`) |
| `data/` | CI intermediates only — all gitignored, never committed |
| `crawlers/` | CI only — not shipped to users |
| `pipeline/` | CI only — not shipped to users |
| `tests/` | Unit + integration (`tests/quality/` needs real index + Ollama) |
| `docs/` | Architecture, data schema, PRDs |

## Gotchas

- `skill/data/index.faiss`, `skill/data/metadata.jsonl`, and `skill/data/version.txt` are committed to the repo. Root `data/` contains only CI intermediates (all gitignored). Run `python skill/scripts/update_index.py` to pull the latest weekly release.
- FAISS index row N **must** correspond to `metadata.jsonl` line N — never reorder either file independently.
- `ollama pull qwen3-embedding:0.6b` must be run before any search or embed.
- ClawHub = OpenClaw registry. `/plugin install` targets SkillsMP — it silently fails for ClawHub skills.
- Never commit `GITHUB_TOKEN`; crawlers read it from the environment.

## Deeper docs

- Architecture & data flow → `docs/architecture.md`
- Full metadata schema → `docs/data-sources.md`
- Agent trigger behavior → `skill/SKILL.md § Agent Instructions`
- CI/CD & release pipeline → `docs/prd/PRD-005-ci-cd-release.md`
