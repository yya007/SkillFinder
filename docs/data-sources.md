# Data Sources

---

## Source Registry

| Source | Est. Skills | Access Method | Quality Signal |
|--------|-------------|---------------|----------------|
| **SkillsMP** (skillsmp.com) | 400,000+ | GitHub Search API (`filename:SKILL.md`) + raw content fetch | Stars, last_updated |
| **ClawHub / OpenClaw** | 5,400+ | Parse `VoltAgent/awesome-openclaw-skills` README.md; ClawHub REST API as secondary | VirusTotal scan result, categories |
| **SkillHub** (skillhub.club) | 7,000+ | Web scraping | S/A/B/C rank across 5 dimensions |
| **Anthropic Official** | ~50 | GitHub API (`anthropics/skills` + known marketplace repos) | Official, production-tested |
| **Community Marketplaces** | ~500 | Clone repos, parse `marketplace.json` | Stars, forks, recency |

---

## Crawler Details

### SkillsMP (`crawlers/skillsmp_crawler.py`)

Uses GitHub Code Search API with query `filename:SKILL.md`. Paginates through results (max 1,000 per query — use additional filters like `language:Markdown` or date ranges to get past the 1K cap). For each repo:

1. Fetch `SKILL.md` raw content
2. Parse YAML frontmatter (`name`, `description`, `triggers`)
3. Fetch repo metadata: `stargazers_count`, `pushed_at`, `topics`
4. Optionally parse `README.md` first paragraph for supplemental description

Rate limits: 30 req/min (search endpoint), 5,000 req/hr (general). Requires `GITHUB_TOKEN`. Implement exponential backoff on 403/429 and respect `X-RateLimit-Reset` header.

Output: `data/raw/skillsmp.jsonl`

### ClawHub / OpenClaw (`crawlers/clawhub_crawler.py`)

Primary: parse `VoltAgent/awesome-openclaw-skills` README.md. Format is category headers (`## Category`) followed by lines of `- [name](url) — description`. Extract name, URL, description, category.

Secondary: if ClawHub REST API is accessible, query it directly. Tertiary: scrape clawhub.io skill pages.

Each record that comes from ClawHub includes `safety_scan` field from VirusTotal results.

Output: `data/raw/clawhub.jsonl`

### SkillHub (`crawlers/skillhub_crawler.py`)

Web scrape skillhub.club. Extract per skill:
- `name`, `description`, `repo_url`
- `rank`: S / A / B / C (overall)
- `dimension_scores`: dict of `{Practicality, Clarity, Automation, Quality, Impact}` → float 0–10

Be respectful: add 1–2s delay between requests, honor `robots.txt`.

Output: `data/raw/skillhub.jsonl`

### Marketplace (`crawlers/marketplace_crawler.py`)

Known target repos:
- `anthropics/skills` (official Anthropic skills)
- `daymade/claude-code-skills`
- `mhattingpete/claude-skills-marketplace`
- `alirezarezvani/claude-skills`
- Any public repo with a `marketplace.json` at root (discovered via GitHub Search)

For each: clone or fetch at ref, parse `marketplace.json` (array of `{name, description, path}` entries), then read the `SKILL.md` at each path.

Output: `data/raw/marketplace.jsonl`

---

## Normalization & Deduplication (`pipeline/normalize.py`)

### Canonical Key

```python
import re, hashlib

def canonical_key(repo_url: str) -> str:
    url = repo_url.lower().strip()
    url = re.sub(r'\.git$', '', url)
    url = re.sub(r'/$', '', url)
    return url

def skill_id(repo_url: str) -> str:
    return hashlib.sha256(canonical_key(repo_url).encode()).hexdigest()
```

### Metadata Merge Priority

When a skill appears in multiple registries, merge with this priority (first wins):

| Field | Priority source |
|-------|----------------|
| `quality.skillhub_rank` / `skillhub_score` | SkillHub |
| `quality.stars` / `popularity` | SkillsMP (GitHub stars) |
| `quality.safety_scan` | ClawHub (VirusTotal) |
| `description` | SKILL.md frontmatter → README.md first paragraph → source listing |
| `name` | SKILL.md frontmatter → repo name |
| `categories` | Union of all sources |
| `install_cmd` | Generated from `source` field |

### Quality Filter

A skill passes the filter if it meets **any** of these:
- `stars >= 2`
- Present in SkillHub, ClawHub curated list, or Anthropic official repos
- `skillhub_rank` is S or A

A skill is **flagged but retained** (with `safety_flag: true`) if:
- ClawHub reports a security warning

A skill is **dropped** if:
- No description (after all merge attempts)
- `stars == 0` AND not in any curated registry AND no SkillHub rating

### `embedding_text` Construction

```python
def build_embedding_text(skill: dict) -> str:
    parts = [
        skill["name"] + ".",
        skill["description"],
        f"Categories: {', '.join(skill.get('categories', []))}.",
    ]
    if skill.get("triggers"):
        parts.append(f"Use when: {'; '.join(skill['triggers'])}.")
    return " ".join(parts)
```

This gives the embedding model maximum semantic surface area: name + description + categories + trigger phrases.

---

## Data Schema

Each record in `data/unified_skills.jsonl` and `data/{model}/metadata.jsonl`:

```json
{
  "id": "a3f8c2...",
  "name": "kubernetes-deployer",
  "description": "Deploy and manage Kubernetes clusters with automated rollbacks, blue-green deployments, and health checking.",
  "repo_url": "https://github.com/user/k8s-deployer",
  "source": ["skillsmp", "clawhub"],
  "categories": ["devops", "kubernetes", "deployment"],
  "install_cmd": {
    "claude_code": "/plugin install k8s-deployer",
    "openclaw": "clawhub install k8s-deployer"
  },
  "quality": {
    "stars": 142,
    "skillhub_rank": "A",
    "skillhub_score": 8.4,
    "safety_scan": "clean",
    "safety_flag": false,
    "last_updated": "2026-02-15"
  },
  "embedding_text": "kubernetes-deployer. Deploy and manage Kubernetes clusters with automated rollbacks, blue-green deployments, and health checking. Categories: devops, kubernetes, deployment. Use when: deploying to k8s, managing kubernetes, container orchestration."
}
```

The `metadata.jsonl` files inside `data/qwen/` and `data/minilm/` are identical to each other and must maintain the same row order as their corresponding FAISS index (row N in the index ↔ line N in `metadata.jsonl`).

---

## Target Index Size

| Filter | Skill count |
|--------|-------------|
| Raw (all sources, before dedup) | ~420,000 |
| After deduplication | ~100,000 |
| After quality filter | **10,000–20,000** (default index) |
| Optional: unfiltered index | ~100,000 (large download, separate release) |
