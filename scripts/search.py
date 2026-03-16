"""
scripts/search.py — SkillFinder vector search runtime.

Embeds a natural language query via local Ollama, searches the pre-built FAISS
index, applies attribute filters, and returns a ranked candidate pool for the
agent to review.

Usage:
    python scripts/search.py "deploy kubernetes clusters" --propose 10
    python scripts/search.py "web scraping" --platform claude_code
    python scripts/search.py "ci/cd pipeline" --propose 5 --json

NOTE: Running this script directly returns a raw candidate pool. The full
SkillFinder experience (query reformulation, tiered fallback, semantic
reranking) is provided by the agent workflow in SKILL.md. Direct CLI use
is for developers and power users; results will be less refined.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Optional

import faiss
import numpy as np
import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUERY_PREFIX = (
    "Instruct: Given a description of a task or use case, "
    "retrieve the most relevant agent skill.\nQuery: "
)

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen3-embedding:0.6b"
DIM = 1024

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OllamaNotAvailableError(Exception):
    """Raised when the local Ollama server is not reachable."""


class AlignmentError(Exception):
    """Raised when the FAISS index row count does not match metadata row count."""


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def _is_ollama_running(url: str = OLLAMA_URL) -> bool:
    """Return True if Ollama API is reachable at *url*."""
    try:
        requests.get(f"{url}/api/tags", timeout=1).raise_for_status()
        return True
    except Exception:
        return False


def ensure_ollama(url: str = OLLAMA_URL) -> "Optional[subprocess.Popen[bytes]]":
    """Ensure Ollama is running, starting it if necessary.

    Returns the Popen process if Ollama was started by this call (caller
    should terminate it when done), or None if it was already running.

    Raises OllamaNotAvailableError if Ollama cannot be started.
    """
    if _is_ollama_running(url):
        return None

    # Try to start Ollama in the background
    print("Starting Ollama...", file=sys.stderr, flush=True)
    try:
        proc = subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise OllamaNotAvailableError(
            "Ollama is not installed. "
            "Install it at https://ollama.com/install, then run: "
            "ollama pull qwen3-embedding:0.6b"
        )

    # Wait up to 10 s for it to become ready
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _is_ollama_running(url):
            return proc
        time.sleep(0.5)

    proc.terminate()
    raise OllamaNotAvailableError(
        "Ollama was started but did not become ready within 10 seconds."
    )


def embed_query(
    text: str,
    ollama_url: str = OLLAMA_URL,
    model: str = MODEL,
) -> np.ndarray:
    """Embed a single query string, returning an L2-normalised float32 vector.

    The QUERY_PREFIX is prepended before sending to Ollama. Shape: (DIM,).
    Raises OllamaNotAvailableError on connection failure.
    """
    full_text = QUERY_PREFIX + text
    embed_url = ollama_url.rstrip("/") + "/api/embed"
    try:
        resp = requests.post(
            embed_url,
            json={"model": model, "input": [full_text]},
            timeout=30,
        )
    except (requests.ConnectionError, requests.Timeout):
        raise OllamaNotAvailableError(
            "Ollama is not running or not installed. "
            "Install it at https://ollama.com/install, then run: "
            "ollama pull qwen3-embedding:0.6b"
        )
    resp.raise_for_status()
    data = resp.json()
    embeddings = data.get("embeddings") or data.get("embedding")
    if not embeddings:
        raise OllamaNotAvailableError(
            f"Unexpected Ollama response shape (no 'embeddings' key): {list(data.keys())}"
        )
    vec = np.array(embeddings[0], dtype=np.float32)
    # L2-normalize
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


# ---------------------------------------------------------------------------
# Index loading
# ---------------------------------------------------------------------------

def load_index(
    index_path: str,
    metadata_path: str,
) -> tuple[faiss.Index, list[dict]]:
    """Load a FAISS index and its paired metadata JSONL file.

    Raises FileNotFoundError if either path is missing.
    Raises AlignmentError if the vector count and metadata row count differ.
    """
    if not os.path.exists(index_path):
        raise FileNotFoundError(f"FAISS index not found: {index_path}")
    if not os.path.exists(metadata_path):
        raise FileNotFoundError(f"Metadata file not found: {metadata_path}")

    index = faiss.read_index(index_path)

    metadata: list[dict] = []
    with open(metadata_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                metadata.append(json.loads(line))

    if index.ntotal != len(metadata):
        raise AlignmentError(
            f"Index/metadata row count mismatch: "
            f"index has {index.ntotal} vectors but metadata has {len(metadata)} rows."
        )

    return index, metadata


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def apply_filters(
    candidates: list[dict],
    platforms: list[str],
    sources: list[str],
    min_stars: int = 0,
    safety_only: bool = False,
) -> list[dict]:
    """Filter a list of candidate skill dicts.

    Filters are AND-ed together; multi-value filters (platforms, sources)
    are OR-ed internally. Input order is preserved.

    - platforms: keep skills where the ``platforms`` list has at least one matching value
    - sources: keep skills where skill["source"] contains at least one match
    - min_stars: keep skills with quality.stars >= min_stars (0 = no filter)
    - safety_only: keep only skills where safety_scan is True (ClawHub safety scan passed)
    """
    result = []
    for skill in candidates:
        # Platform filter — uses the normalized ``platforms`` list field so that
        # SkillHub records (which have install_cmd: {}) are not silently excluded.
        if platforms and not any(p in skill.get("platforms", []) for p in platforms):
            continue

        # Source filter
        if sources:
            skill_sources = set(skill.get("source", []))
            if not skill_sources.intersection(sources):
                continue

        # Stars filter
        if min_stars and skill.get("quality", {}).get("stars", 0) < min_stars:
            continue

        # Safety scan filter
        if safety_only and skill.get("safety_scan") is not True:
            continue

        result.append(skill)
    return result


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(
    query: str,
    index: faiss.Index,
    metadata: list[dict],
    propose_n: int = 5,
    platforms: Optional[list[str]] = None,
    sources: Optional[list[str]] = None,
    min_stars: int = 0,
    safety_only: bool = False,
    ollama_url: str = OLLAMA_URL,
) -> list[dict]:
    """Search the FAISS index and return filtered candidates.

    Returns up to propose_n * 3 results, ordered by FAISS similarity score
    (descending). sim_score is clamped to [0.0, 1.0] and is for agent-internal
    use only (threshold checks, reranking) — do not expose to end users.
    """
    if platforms is None:
        platforms = []
    if sources is None:
        sources = []

    # How many vectors to retrieve from FAISS (oversample for filter headroom)
    candidate_count = propose_n * 3
    k = candidate_count * 2

    # Clamp k to the actual index size
    k = min(k, index.ntotal)

    # Embed query
    query_vec = embed_query(query, ollama_url=ollama_url)

    # FAISS expects a 2D array with shape (n_queries, dim)
    query_matrix = query_vec.reshape(1, -1).astype(np.float32)

    scores, indices = index.search(query_matrix, k)

    # Build candidate list with sim_score
    candidates: list[dict] = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0:
            # FAISS returns -1 when fewer results than k are available
            continue
        skill = dict(metadata[idx])
        # Clamp cosine similarity to [0.0, 1.0]
        skill["sim_score"] = float(max(0.0, min(1.0, score)))
        candidates.append(skill)

    # Apply attribute filters
    filtered = apply_filters(
        candidates,
        platforms=platforms,
        sources=sources,
        min_stars=min_stars,
        safety_only=safety_only,
    )

    # Return up to propose_n * 3 results
    return filtered[:candidate_count]


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_results(results: list[dict], as_json: bool = False) -> str:
    """Format search results as JSON or human-readable text.

    JSON mode: returns an object with a ``results`` array (sim_score, name,
    description, repo_url, install_cmd, quality per item).

    Human-readable mode: prints one result block per skill with name,
    description, and repo URL.
    """
    if as_json:
        output_items = []
        for r in results:
            item: dict = {}
            item["sim_score"] = r.get("sim_score", 0.0)
            item["name"] = r.get("name", "")
            item["description"] = r.get("description", "")
            item["repo_url"] = r.get("repo_url", "")
            item["skill_md_url"] = r.get("skill_md_url", "")
            item["source"] = r.get("source", [])
            item["platforms"] = r.get("platforms", [])
            item["install_cmd"] = r.get("install_cmd", {})
            item["quality"] = r.get("quality", {})
            output_items.append(item)
        return json.dumps({"results": output_items}, indent=2)
    else:
        if not results:
            return "No results found."
        lines: list[str] = []
        for i, r in enumerate(results, 1):
            name = r.get("name", "(unknown)")
            description = r.get("description", "")
            skill_url = r.get("skill_md_url", "") or r.get("repo_url", "")
            stars = r.get("quality", {}).get("stars", 0) or 0
            star_str = f"  ⭐ {stars:,}" if stars else ""
            lines.append(f"{i}. {name}{star_str}")
            if description:
                lines.append(f"   {description}")
            if skill_url:
                lines.append(f"   Skill: {skill_url}")
            install = r.get("install_cmd", {})
            for platform, cmd in install.items():
                lines.append(f"   [{platform}] {cmd}")
            lines.append("")
        return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search the SkillFinder index for relevant agent skills.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "query",
        help="Natural language description of the task or use case.",
    )
    parser.add_argument(
        "--propose",
        type=int,
        default=10,
        metavar="N",
        help="Number of skills to propose (script returns N*3 candidates). Default: 10.",
    )
    parser.add_argument(
        "--platform",
        action="append",
        default=[],
        dest="platforms",
        metavar="PLATFORM",
        help=(
            "Filter to skills installable on this platform "
            "(claude_code, codex, openclaw). Repeatable; values are OR-ed."
        ),
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        dest="sources",
        metavar="SOURCE",
        help=(
            "Filter to skills from this registry "
            "(skillsmp, clawhub, skillhub, marketplace). Repeatable; values are OR-ed."
        ),
    )
    parser.add_argument(
        "--min_stars",
        type=int,
        default=0,
        metavar="N",
        help="Only return skills with at least this many GitHub stars (default: 0 = no filter).",
    )
    parser.add_argument(
        "--safety_only",
        action="store_true",
        default=False,
        help="Only return skills that passed the ClawHub safety scan.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        dest="as_json",
        help="Output results as JSON (default).",
    )
    parser.add_argument(
        "--no-json",
        action="store_false",
        dest="as_json",
        help="Output results as human-readable text.",
    )
    parser.add_argument(
        "--data",
        default=_DATA_DIR,
        metavar="DATA_DIR",
        help="Path to directory containing index.faiss and metadata.jsonl.",
    )
    parser.epilog = (
        "NOTE: This script returns a raw candidate pool. For best results invoke "
        "SkillFinder through your agent (Claude Code, OpenClaw, or Codex), which "
        "adds query reformulation, tiered fallback, and semantic reranking."
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # Start Ollama if not already running; remember the process so we can stop it
    try:
        ollama_proc = ensure_ollama()
    except OllamaNotAvailableError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        # Load index
        index_path = os.path.join(args.data, "index.faiss")
        metadata_path = os.path.join(args.data, "metadata.jsonl")
        try:
            index, metadata = load_index(index_path, metadata_path)
        except FileNotFoundError as exc:
            print(
                f"Error: {exc}\n"
                "The index files should be included in the repository.\n"
                "If you used a shallow clone, try: git fetch --unshallow\n"
                "To download the latest index manually: python scripts/update_index.py",
                file=sys.stderr,
            )
            return 1
        except AlignmentError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        # Run search
        results = search(
            query=args.query,
            index=index,
            metadata=metadata,
            propose_n=args.propose,
            platforms=args.platforms,
            sources=args.sources,
            min_stars=args.min_stars,
            safety_only=args.safety_only,
        )

        # Format and print
        print(format_results(results, as_json=args.as_json))
        return 0
    finally:
        if ollama_proc is not None:
            ollama_proc.terminate()
            try:
                ollama_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ollama_proc.kill()


if __name__ == "__main__":
    sys.exit(main())
