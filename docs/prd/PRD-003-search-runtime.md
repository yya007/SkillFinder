# PRD-003: Search Runtime

**Status:** Planned
**Phase:** 3 of 5
**Depends on:** PRD-002 (FAISS indexes in data/)
**Blocks:** PRD-004 (SKILL.md references these scripts)

---

## Problem

We have a FAISS index. We need the runtime search layer that users and the agent actually call: a script that takes a natural language query plus optional attribute filters, embeds locally via Ollama, searches the index, and returns a candidate pool — in raw FAISS score order — for the agent to read and evaluate. Re-ranking is the agent's job, not the script's.

## Goals

- Sub-200ms search latency on CPU with local Ollama embedding
- Attribute-based filtering: platform (claude_code / codex / openclaw), source, safety
- Return `propose_n × 3` candidates so the agent has enough to make a good selection
- Structured JSON output suitable for agent consumption
- `fetch_skill.py` for on-demand full SKILL.md retrieval
- `update_index.py` for pulling the latest release artifact

## Non-Goals

- Web server or persistent daemon
- Interactive TUI
- Search history or caching (out of scope for v1)

---

## Functional Requirements

### F1 — Search (`scripts/search.py`)

**CLI:**
```
python scripts/search.py "<query>" [--propose N] [--platform PLATFORM [...]] [--source SOURCE] [--safety_only] [--json]
```

Flags:
- `query` (positional, required): natural language search query
- `--propose N` (default: 10): how many results the agent should propose to the user; script returns `N × 3` candidates
- `--platform PLATFORM` (repeatable): filter to skills installable on this platform; values: `claude_code`, `codex`, `openclaw`; multiple flags are OR'd
- `--source SOURCE` (repeatable): filter to skills from this registry; values: `skillsmp`, `clawhub`, `skillhub`, `marketplace`
- `--safety_only` (flag): exclude skills where `safety_flag: true`
- `--json` (default: true): output as JSON array; `--no-json` for human-readable

**Flow:**

1. Parse arguments; derive `candidate_count = propose_n * 3`
2. Verify Ollama is reachable (`GET http://localhost:11434/`) — if not, print install instructions and exit 1
3. Load `data/index.faiss` and `data/metadata.jsonl`
4. Embed query via Ollama (`qwen3-embedding:0.6b`) with instruction prefix applied
5. L2-normalize query vector
6. Run `index.search(query_vec, candidate_count * 2)` to get an oversized pool (extra headroom for filtering)
7. Load metadata for candidate IDs
8. Apply attribute filters in order:
   - `--platform`: keep records where `install_cmd` has at least one matching key
   - `--source`: keep records where `source` list contains at least one matching value
   - `--safety_only`: drop records where `safety_flag: true`
9. Take first `candidate_count` passing records (FAISS score order preserved)
10. Output as JSON array

**Output JSON schema:**
```json
[
  {
    "sim_score": 0.94,
    "name": "docker-compose-manager",
    "description": "...",
    "stars": 234,
    "skillhub_rank": "A",
    "skillhub_score": 8.1,
    "safety_scan": "clean",
    "safety_flag": false,
    "source": ["skillsmp", "clawhub"],
    "categories": ["devops", "docker"],
    "repo_url": "https://github.com/...",
    "install_cmd": {
      "claude_code": "/plugin install docker-compose-manager",
      "openclaw": "clawhub install docker-compose-manager"
    },
    "last_updated": "2026-01-20"
  }
]
```

`sim_score` is the raw FAISS cosine similarity (0–1). There is no `rank` field — the agent determines rank based on its own reading of the candidates.

### F2 — Fetch Skill (`scripts/fetch_skill.py`)

**CLI:**
```
python scripts/fetch_skill.py --repo <github_url> [--output /tmp/skill.md]
```

**Flow:**

1. Parse GitHub URL to extract `owner/repo`
2. Attempt to fetch `SKILL.md` at:
   - `https://raw.githubusercontent.com/{owner}/{repo}/main/SKILL.md`
   - `https://raw.githubusercontent.com/{owner}/{repo}/master/SKILL.md`
   - `https://raw.githubusercontent.com/{owner}/{repo}/main/skills/*/SKILL.md` (try common subdirs)
3. Parse YAML frontmatter from the fetched content
4. If `--output` given, write to file; else print to stdout

On failure: print clear error with the URLs attempted, exit 1.

### F3 — Update Index (`scripts/update_index.py`)

**CLI:**
```
python scripts/update_index.py [--check] [--force]
```

Flags:
- `--check`: only report whether an update is available, don't download
- `--force`: download even if local version appears current

**Flow:**

1. Read `data/version.txt` for current index date (or "none" if no index)
2. Query GitHub Releases API for the `skill-finder` repo's latest release
3. If latest release tag > current version (or `--force`):
   a. Print: "Downloading index YYYYMMDD (Xmb)..."
   b. Download `.tar.gz` to a temp file
   c. Verify SHA256 against value in release body
   d. Extract to `data/` (atomic: extract to `data/.new/`, then rename)
   e. Print: "Updated from {old} to {new}. {N} skills indexed."
4. If already current: print "Index is up to date (YYYYMMDD, N skills)."

---

## Technical Spec

### Attribute Filtering

```python
def apply_filters(
    candidates: list[dict],
    platforms: list[str],   # e.g. ["claude_code", "openclaw"]
    sources: list[str],     # e.g. ["clawhub"]
    safety_only: bool,
) -> list[dict]:
    result = []
    for skill in candidates:
        if safety_only and skill.get("safety_flag"):
            continue
        if platforms:
            available = set(skill.get("install_cmd", {}).keys())
            if not available.intersection(platforms):
                continue
        if sources:
            skill_sources = set(skill.get("source", []))
            if not skill_sources.intersection(sources):
                continue
        result.append(skill)
    return result
```

Filters are applied after FAISS search and before truncating to `candidate_count`. FAISS score order is preserved throughout — no sorting is applied by the script.

### Index Loading

Load the index once and keep it in memory for the lifetime of the process (`search.py` is short-lived). Do not implement lazy loading or caching — the startup cost is acceptable.

```python
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

def load_index() -> tuple[faiss.Index, list[dict]]:
    index = faiss.read_index(os.path.join(DATA_DIR, "index.faiss"))
    metadata = []
    with open(os.path.join(DATA_DIR, "metadata.jsonl")) as f:
        for line in f:
            metadata.append(json.loads(line))
    assert index.ntotal == len(metadata), "Index/metadata row count mismatch"
    return index, metadata
```

### Startup Time Budget

| Step | Target |
|------|--------|
| Import + index load | < 800ms |
| Query embedding (Ollama, warm) | < 50ms |
| FAISS search (20K skills) | < 5ms |
| Attribute filter + JSON output | < 5ms |
| **Total p95 (warm Ollama)** | **< 200ms** (excluding index load) |

Note: Ollama cold-start (first call after model load) can take 2–5s. This is a one-time cost per Ollama session, not per query.

Index load is one-time startup cost; the agent can treat it as initialization.

### Dependencies

```
# scripts/requirements.txt
numpy>=1.26
faiss-cpu>=1.8
requests>=2.31
```

Ollama is a system dependency, not a Python package. Installation instructions: https://ollama.com/install — then `ollama pull qwen3-embedding:0.6b`.

---

## Success Criteria

| Metric | Target |
|--------|--------|
| Search latency (Ollama warm, p95) | < 200ms |
| Expected skill in candidates (Recall@30) | ≥ 90% |
| `--platform` filter correctly excludes non-matching skills | 100% |
| `--safety_only` never returns `safety_flag: true` records | 100% |
| Works with no internet (local Ollama + local index) | Yes |
| Index download SHA256 verified | Yes |

---

## Test Plan

`tests/test_integration.py`:
- Requires `data/*/index.faiss` to be present (skip if not)
- Run 10 known queries, assert top result matches expected category
- Assert output JSON is valid and contains all required fields

`tests/test_search_quality.py`:
- Load `tests/fixtures/test_queries.json` (100 labeled query → expected skill pairs)
- Run all queries through `search.py` with `--propose 10` (returns 30 candidates)
- Compute Recall@30 (expected skill appears anywhere in candidate pool)
- Print per-query misses for debugging
- Exit non-zero if Recall@30 < 0.90

`tests/fixtures/test_queries.json` format:
```json
[
  {
    "query": "deploy kubernetes clusters with automated rollbacks",
    "expected_skill_id": "a3f8c2...",
    "expected_skill_name": "kubernetes-deployer",
    "category": "devops"
  }
]
```

---

## Open Questions

- Should `update_index.py` verify the download against a GPG signature in addition to SHA256? This would prevent a compromised GitHub Release from being accepted.
- What should `search.py` do when attribute filters are so restrictive that fewer than `propose_n` candidates pass? Options: return whatever passed (with a count warning in the JSON), or relax filters and annotate results. Leaning toward return-what-passed + warning field.
- Should platform detection (which platform the user is on) be automatic, defaulting `--platform` to `claude_code`, or always explicit? Auto-default seems better UX.
