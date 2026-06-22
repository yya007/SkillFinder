# CLAUDE.md

SkillFinder — local-first agent skill discovery (FAISS + Qwen3-Embedding-0.6B via Ollama).
Ships as a skill for Claude Code, OpenClaw, and Codex.

## Commands

| Task | Command |
|------|---------|
| Install dev deps | `pip install -r requirements-dev.txt` |
| Lint | `ruff check scripts/ pipeline/ crawlers/` |
| Test (unit, no network) | `pytest` |
| Run a search | `python scripts/search.py "deploy k8s" --no-json` |
| Crawl (requires GITHUB_TOKEN) | `python -m crawlers.skillsmp_crawler -o data/raw/skillsmp.jsonl > data/logs/skillsmp.log 2>&1` |
| Full pipeline | `pipeline/normalize.py → embed.py → build_index.py → update_docs.py → update_release_log.py` |

## Git Workflow

- **Never push directly to `master`.** All changes must go through a pull request.
- Create a feature branch, open a PR, and merge via GitHub.

## Conventions

- **Stable IDs:** `sha256(skill_md_url)` for monorepo skills (multiple SKILL.md files in one repo, e.g. `anthropics/skills`), else `sha256(canonical_repo_url)`. Never sequential integers. **Note:** IDs for monorepo skills changed in this release — see BACKLOG.md §"Document monorepo ID change".
- **Canonical URL:** lowercase, `.git` suffix stripped.
- **Platform values:** exactly `claude_code`, `openclaw`, `codex` — no other strings.
- **Source values:** exactly `skillsmp`, `clawhub`, `skillhub`, `marketplace`.
- **No cross-imports:** `scripts/` must never import from `crawlers/` or `pipeline/`.
- **Schema first:** add new metadata fields to `docs/data-sources.md` before code.

## Layout

| Directory | Purpose |
|-----------|---------|
| `scripts/` | Runtime — shipped with skill to users |
| `data/` | Index files — gitignored, downloaded at install time |
| `data/logs/` | Crawler log files — redirect crawler output here |
| `crawlers/` | CI only — not shipped to users |
| `pipeline/` | CI only — not shipped to users |
| `tests/` | Unit + integration (`tests/quality/` needs real index + Ollama) |
| `docs/` | Architecture, data schema, PRDs |

## Gotchas

- `data/index.faiss`, `data/metadata.jsonl`, and `data/release_log.jsonl` (per-release skill-count history; rendered to `docs/release-log.md` by `pipeline/update_release_log.py`) are committed to the repo. Pipeline intermediates (`data/raw/`, `data/embeddings.npy`, etc.) are gitignored. Run `python scripts/update_index.py` to pull the latest weekly release.
- FAISS index row N **must** correspond to `metadata.jsonl` line N — never reorder either file independently.
- `ollama pull qwen3-embedding:0.6b` must be run before any search or embed.
- ClawHub = OpenClaw registry. `/plugin install` targets SkillsMP — it silently fails for ClawHub skills.
- Never commit `GITHUB_TOKEN`; crawlers read it from the environment.

## Deeper docs

- Architecture & data flow → `docs/architecture.md`
- Full metadata schema → `docs/data-sources.md`
- Agent trigger behavior → `SKILL.md § Agent Instructions`
- CI/CD & release pipeline → `docs/prd/PRD-005-ci-cd-release.md`
