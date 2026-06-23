"""
pipeline/render_release_notes.py — render a standardized GitHub Release body.

Produces a consistent Markdown body for an npm (`v<version>`) release from
`data/version.txt` (skill count, date, embed model) and `data/release_log.jsonl`
(per-source breakdown + previous count for the delta). The `npm-publish.yml`
workflow calls this so every automated release has the same structure.

Usage:
    python pipeline/render_release_notes.py --npm-version 0.1.3
    python pipeline/render_release_notes.py --npm-version 0.1.3 --highlights "Fixed X; added Y"

Prints Markdown to stdout. No network access.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.update_release_log import load_log, parse_version_txt  # noqa: E402

REPO = "yya007/SkillFinder"


def _delta(records: list[dict], date: str, count: int) -> str:
    """'+N' vs the most recent prior release, or 'first release'."""
    prior = [r for r in records if r.get("date", "") < date and isinstance(r.get("skill_count"), int)]
    if not prior:
        return "first release"
    prev = max(prior, key=lambda r: r["date"])["skill_count"]
    return f"{count - prev:+,} since the previous release"


def render(version: str, version_txt: Path, log_path: Path, highlights: str | None) -> str:
    v = parse_version_txt(version_txt)
    count = int(v.get("skill_count", "0") or "0")
    date = v.get("date", "")
    model = v.get("embed_model") or "qwen3-embedding:0.6b"

    records = load_log(log_path)
    cur = next((r for r in records if r.get("date") == date), {})
    sources = cur.get("sources") or {}
    sources_str = " · ".join(
        f"{k} {n:,}" for k, n in sorted(sources.items(), key=lambda kv: -kv[1])
    ) if sources else "—"

    if not highlights:
        highlights = f"Index refresh — now **{count:,} skills** ({_delta(records, date, count)})."

    return f"""\
**`@yya007/skill-finder` v{version}** — local-first semantic search over {count:,} agent skills.

|  |  |
|--|--|
| **Skills indexed** | {count:,} ({_delta(records, date, count)}) |
| **Index built** | {date} · `{model}` |
| **Sources** | {sources_str} |

### What's new

{highlights}

### Install / update

**Claude Code** — plugin marketplace:

```
/plugin marketplace add {REPO}
/plugin install skill-finder@skillfinder
```

**Codex · OpenClaw · other agents** — skills directory:

```bash
npx skills add {REPO}          # auto-detects your agent
# or: npm install -g @yya007/skill-finder  (then copy into your agent's skills dir)
```

**Finish setup (all platforms):**

```bash
pip install numpy faiss-cpu requests pyyaml
ollama pull qwen3-embedding:0.6b
```

📜 [Full release history](https://github.com/{REPO}/blob/master/docs/release-log.md) · 📦 [npm](https://www.npmjs.com/package/@yya007/skill-finder)
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a standardized npm release body.")
    parser.add_argument("--npm-version", required=True)
    parser.add_argument("--version-file", default=str(REPO_ROOT / "data" / "version.txt"))
    parser.add_argument("--log", default=str(REPO_ROOT / "data" / "release_log.jsonl"))
    parser.add_argument("--highlights", default=None)
    args = parser.parse_args(argv)

    vf = Path(args.version_file)
    if not vf.exists():
        print(f"Error: {vf} not found.", file=sys.stderr)
        return 1
    sys.stdout.write(render(args.npm_version, vf, Path(args.log), args.highlights))
    return 0


if __name__ == "__main__":
    sys.exit(main())
