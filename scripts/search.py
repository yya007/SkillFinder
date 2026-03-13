"""
scripts/search.py — SkillFinder vector search runtime.

Embeds a natural language query via local Ollama, searches the pre-built FAISS
index, applies attribute filters, and returns a ranked candidate pool for the
agent to review.

Usage:
    python scripts/search.py "deploy kubernetes clusters" --propose 10
    python scripts/search.py "web scraping" --platform claude_code --safety_only
    python scripts/search.py "ci/cd pipeline" --propose 5 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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

def check_ollama(url: str = OLLAMA_URL) -> None:
    """Verify Ollama is reachable.

    GETs the base URL. Raises OllamaNotAvailableError if the connection fails.
    """
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
    except (requests.ConnectionError, requests.Timeout, requests.HTTPError):
        raise OllamaNotAvailableError(
            "Ollama is not running or not installed. "
            "Install it at https://ollama.com/install, then run: "
            "ollama pull qwen3-embedding:0.6b"
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
    safety_only: bool,
) -> list[dict]:
    """Filter a list of candidate skill dicts.

    Filters are AND-ed together; multi-value filters (platforms, sources)
    are OR-ed internally. Input order is preserved.

    - platforms: keep skills where install_cmd has at least one matching key
    - sources: keep skills where skill["source"] contains at least one match
    - safety_only: exclude skills where quality.safety_flag is True
    """
    result = []
    for skill in candidates:
        # Safety filter — check quality.safety_flag
        if safety_only and skill.get("quality", {}).get("safety_flag") is True:
            continue

        # Platform filter
        if platforms:
            available = set(skill.get("install_cmd", {}).keys())
            if not available.intersection(platforms):
                continue

        # Source filter
        if sources:
            skill_sources = set(skill.get("source", []))
            if not skill_sources.intersection(sources):
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
    safety_only: bool = False,
    ollama_url: str = OLLAMA_URL,
) -> list[dict]:
    """Search the FAISS index and return filtered candidates.

    Returns up to propose_n * 3 results, ordered by FAISS similarity score
    (descending). sim_score is clamped to [0.0, 1.0].
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
    filtered = apply_filters(candidates, platforms=platforms, sources=sources, safety_only=safety_only)

    # Return up to propose_n * 3 results
    return filtered[:candidate_count]


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_results(results: list[dict], as_json: bool = False) -> str:
    """Format search results as JSON or human-readable text.

    JSON mode: returns a JSON array where each item includes sim_score, name,
    description, repo_url, install_cmd, quality, and safety_flag (flattened
    from quality as a top-level key).

    Human-readable mode: one result per block, includes name and description.
    """
    if as_json:
        output_items = []
        for r in results:
            item: dict = {}
            item["sim_score"] = r.get("sim_score", 0.0)
            item["name"] = r.get("name", "")
            item["description"] = r.get("description", "")
            item["repo_url"] = r.get("repo_url", "")
            item["install_cmd"] = r.get("install_cmd", {})
            item["quality"] = r.get("quality", {})
            # Flatten safety_flag as a top-level key
            item["safety_flag"] = r.get("quality", {}).get("safety_flag", False)
            output_items.append(item)
        return json.dumps(output_items, indent=2)
    else:
        if not results:
            return "No results found."
        lines = []
        for i, r in enumerate(results, 1):
            name = r.get("name", "(unknown)")
            description = r.get("description", "")
            sim_score = r.get("sim_score", 0.0)
            repo_url = r.get("repo_url", "")
            lines.append(f"{i}. {name}  (score: {sim_score:.3f})")
            if description:
                lines.append(f"   {description}")
            if repo_url:
                lines.append(f"   {repo_url}")
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
        "--safety_only",
        action="store_true",
        default=False,
        help="Exclude skills where safety_flag is True.",
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
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # Check Ollama availability
    try:
        check_ollama()
    except OllamaNotAvailableError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Load index
    index_path = os.path.join(args.data, "index.faiss")
    metadata_path = os.path.join(args.data, "metadata.jsonl")
    try:
        index, metadata = load_index(index_path, metadata_path)
    except FileNotFoundError as exc:
        print(
            f"Error: {exc}\n"
            "Run `python scripts/update_index.py` to download the latest index.",
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
        safety_only=args.safety_only,
    )

    # Format and print
    print(format_results(results, as_json=args.as_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
