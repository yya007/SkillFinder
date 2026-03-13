# PRD-003: Search Runtime

**Status:** Planned
**Phase:** 3 of 5
**Depends on:** PRD-002 (FAISS indexes in data/)
**Blocks:** PRD-004 (SKILL.md references these scripts)

---

## Problem

We have a FAISS index. We need the runtime search layer that users and the agent actually call: a script that takes a natural language query, embeds it locally, searches the index, re-ranks results, and returns structured JSON — all in under 200ms and with zero required configuration.

## Goals

- Sub-200ms search latency on CPU with local Ollama embedding
- Zero required configuration — auto-detect available embedding tier
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
python scripts/search.py "<query>" [--top_k N] [--min_quality FLOAT] [--json]
```

Flags:
- `query` (positional, required): natural language search query
- `--top_k N` (default: 5): number of results to return
- `--min_quality FLOAT` (default: 0.0): minimum normalized quality score (0–1)
- `--json` (default: true): output as JSON array; `--no-json` for human-readable

**Flow:**

1. Parse arguments
2. Detect embedding tier (in order):
   a. Run `ollama list`; if `qwen3-embedding` appears → Tier 1, use `data/qwen/`
   b. Try `import sentence_transformers` → Tier 2, use `data/minilm/`
   c. Check `OPENROUTER_API_KEY` env var → Tier 3, use `data/qwen/`
   d. None available → print install instructions, exit 1
3. Load FAISS index and metadata from the appropriate `data/{model}/` directory
4. Embed query with selected model (apply instruction prefix for Qwen3)
5. L2-normalize query vector
6. Run `index.search(query_vec, top_k * 3)` to get candidates
7. Load metadata for candidate IDs
8. Apply `min_quality` threshold
9. Re-rank: `score = sim*0.7 + quality_norm*0.2 + recency*0.1`
10. Return top `top_k` as JSON

**Output JSON schema:**
```json
[
  {
    "rank": 1,
    "name": "docker-compose-manager",
    "description": "...",
    "score": 0.94,
    "stars": 234,
    "skillhub_rank": "A",
    "safety": "clean",
    "safety_flag": false,
    "repo_url": "https://github.com/...",
    "install": {
      "claude_code": "/plugin install ...",
      "openclaw": "clawhub install ..."
    },
    "last_updated": "2026-01-20"
  }
]
```

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

### Re-ranking

```python
def normalize_quality(skill: dict) -> float:
    """Returns 0–1 quality score from multiple signals."""
    score = 0.0
    # Stars: log scale, cap at 1000
    stars = skill.get("quality", {}).get("stars", 0)
    score += min(math.log1p(stars) / math.log1p(1000), 1.0) * 0.5
    # SkillHub score: 0–10 scale
    sh_score = skill.get("quality", {}).get("skillhub_score")
    if sh_score is not None:
        score += (sh_score / 10.0) * 0.5
    elif skill.get("quality", {}).get("skillhub_rank") in ("S", "A"):
        score += 0.4
    return min(score, 1.0)

def recency_score(last_updated: str) -> float:
    """1.0 = updated today, decays to 0.0 at 2 years old."""
    if not last_updated:
        return 0.3
    days = (datetime.date.today() - datetime.date.fromisoformat(last_updated)).days
    return max(0.0, 1.0 - days / 730)

def rerank(candidates: list[dict], sim_scores: list[float]) -> list[dict]:
    for skill, sim in zip(candidates, sim_scores):
        q = normalize_quality(skill)
        r = recency_score(skill.get("quality", {}).get("last_updated"))
        skill["_final_score"] = sim * 0.70 + q * 0.20 + r * 0.10
    return sorted(candidates, key=lambda x: x["_final_score"], reverse=True)
```

### Index Loading

Load the index once and keep it in memory for the lifetime of the process (search.py is short-lived). Do not implement lazy loading or caching — the startup cost is acceptable.

```python
def load_index(model_dir: str) -> tuple[faiss.Index, list[dict]]:
    index = faiss.read_index(os.path.join(model_dir, "index.faiss"))
    metadata = []
    with open(os.path.join(model_dir, "metadata.jsonl")) as f:
        for line in f:
            metadata.append(json.loads(line))
    assert index.ntotal == len(metadata), "Index/metadata row count mismatch"
    return index, metadata
```

### Startup Time Budget

| Step | Target |
|------|--------|
| Import + index load | < 800ms |
| Query embedding (Ollama) | < 50ms |
| FAISS search (20K skills) | < 5ms |
| Re-ranking + JSON output | < 5ms |
| **Total p95** | **< 200ms** (excluding index load) |

Index load is one-time startup cost; the agent can treat it as initialization.

### Dependencies

```
# scripts/requirements.txt
numpy>=1.26
faiss-cpu>=1.8
requests>=2.31
```

`sentence-transformers` is optional (Tier 2) and not listed in `requirements.txt` to keep the mandatory install minimal. Print a helpful message if it's not installed and Tier 2 is attempted.

---

## Success Criteria

| Metric | Target |
|--------|--------|
| Search latency (Tier 1 / Ollama, p95) | < 200ms |
| Search latency (Tier 2 / MiniLM, p95) | < 500ms |
| Recall@5 on test suite | ≥ 80% |
| MRR on test suite | ≥ 0.65 |
| Works with no internet (Tier 1 or 2) | Yes |
| Works with no Ollama (Tier 2) | Yes |
| Index download SHA256 verified | Yes |

---

## Test Plan

`tests/test_integration.py`:
- Requires `data/*/index.faiss` to be present (skip if not)
- Run 10 known queries, assert top result matches expected category
- Assert output JSON is valid and contains all required fields

`tests/test_search_quality.py`:
- Load `tests/fixtures/test_queries.json` (100 labeled query → expected skill pairs)
- Run all queries through `search.py`
- Compute Recall@5 (expected skill appears in top 5) and MRR
- Print per-query results for debugging misses
- Exit non-zero if Recall@5 < 0.80

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
- Ollama cold-start: the first embedding call after Ollama loads the model can take 2–5s. Should `search.py` print a "Loading model..." message if Tier 1 is slow to respond?
