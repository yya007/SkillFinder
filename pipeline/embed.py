"""
pipeline/embed.py — Embedding pipeline for SkillFinder.

Reads unified_skills.jsonl, embeds each record's `embedding_text` field via
Ollama (Qwen3-Embedding-0.6B), and writes:
  - embeddings.npy              shape (N, 1024), float32
  - unified_skills_ordered.jsonl  same records in exact row-aligned order

Documents are embedded as-is (no query prefix). The query instruction prefix
is applied only at search time in scripts/search.py.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants (imported by tests)
# ---------------------------------------------------------------------------

OLLAMA_URL: str = "http://localhost:11434"
MODEL: str = "qwen3-embedding:0.6b"
BATCH_SIZE: int = 32
DIM: int = 1024
CHECKPOINT_EVERY: int = 100  # save checkpoint every N batches


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OllamaError(Exception):
    """Raised when Ollama is unavailable or returns an unexpected response."""


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def check_ollama_available(url: str = OLLAMA_URL) -> bool:
    """Return True if Ollama is reachable at *url*, False otherwise (never raises)."""
    try:
        resp = requests.get(f"{url}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def embed_batch(
    texts: list[str],
    model: str = MODEL,
    ollama_url: str = OLLAMA_URL,
) -> np.ndarray:
    """Embed a list of texts via Ollama and return a float32 array of shape (N, DIM).

    Documents are passed as-is — no query instruction prefix is applied here.

    Raises:
        OllamaError: on connection failure, non-200 response, or malformed JSON.
    """
    try:
        response = requests.post(
            f"{ollama_url}/api/embed",
            json={"model": model, "input": texts},
            timeout=120,
        )
    except requests.RequestException as exc:
        raise OllamaError(f"Failed to connect to Ollama at {ollama_url}: {exc}") from exc

    if response.status_code != 200:
        raise OllamaError(
            f"Ollama returned HTTP {response.status_code}: {response.text}"
        )

    try:
        data = response.json()
        embeddings = data["embeddings"]
    except (KeyError, ValueError) as exc:
        raise OllamaError(
            f"Ollama response missing 'embeddings' key or invalid JSON: {exc}"
        ) from exc

    return np.array(embeddings, dtype=np.float32)


def embed_all(
    records: list[dict],
    ollama_url: str = OLLAMA_URL,
    batch_size: int = BATCH_SIZE,
    checkpoint_dir: str | None = None,
) -> np.ndarray:
    """Embed all records and return a float32 array of shape (N, DIM).

    Args:
        records:        List of unified skill dicts, each must have `embedding_text`.
        ollama_url:     Ollama base URL.
        batch_size:     Number of texts per Ollama request.
        checkpoint_dir: If given, save partial embeddings every CHECKPOINT_EVERY
                        batches as ``embeddings_checkpoint_<batch_idx>.npy``.

    Raises:
        ValueError: if any record is missing the `embedding_text` field.
        OllamaError: propagated from embed_batch on Ollama failure.
    """
    # Validate all records up-front so we fail fast.
    for i, record in enumerate(records):
        if "embedding_text" not in record:
            raise ValueError(
                f"Record at index {i} is missing required 'embedding_text' field: {record}"
            )

    all_vecs: list[np.ndarray] = []
    total = len(records)
    num_batches = (total + batch_size - 1) // batch_size

    ckpt_path = Path(checkpoint_dir) if checkpoint_dir is not None else None

    for batch_idx in range(num_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, total)
        batch_texts = [records[j]["embedding_text"] for j in range(start, end)]

        vecs = embed_batch(batch_texts, model=MODEL, ollama_url=ollama_url)
        all_vecs.append(vecs)

        logger.info("Embedded %d/%d records", end, total)

        # Write checkpoint every CHECKPOINT_EVERY batches and at the final batch.
        # Saves only the current batch's vectors; a resume routine concatenates files.
        is_last = (batch_idx == num_batches - 1)
        if ckpt_path is not None and ((batch_idx + 1) % CHECKPOINT_EVERY == 0 or is_last):
            ckpt_file = ckpt_path / f"embeddings_checkpoint_{batch_idx + 1}.npy"
            np.save(str(ckpt_file), vecs)
            logger.info("Saved checkpoint: %s", ckpt_file)

    return np.vstack(all_vecs) if all_vecs else np.empty((0, DIM), dtype=np.float32)


def run_embed(
    input_path: str,
    output_embeddings: str,
    output_ordered: str,
    ollama_url: str = OLLAMA_URL,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Read input JSONL, embed all records, and write outputs.

    Writes:
      - *output_embeddings*: numpy array, shape (N, 1024), float32, via np.save.
      - *output_ordered*: JSONL file with records in exactly the same row order.

    Args:
        input_path:         Path to ``unified_skills.jsonl``.
        output_embeddings:  Destination path for ``embeddings.npy``.
        output_ordered:     Destination path for row-aligned ``ordered.jsonl``.
        ollama_url:         Ollama base URL.
        batch_size:         Texts per Ollama request.

    Returns:
        Total number of records embedded.

    Raises:
        FileNotFoundError: if *input_path* does not exist.
        OllamaError:       propagated from embed_batch.
    """
    input_file = Path(input_path)
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    records: list[dict] = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    embeddings = embed_all(records, ollama_url=ollama_url, batch_size=batch_size)

    # Save embeddings array.
    np.save(output_embeddings, embeddings)

    # Save records in the same order — row N in embeddings == line N here.
    with open(output_ordered, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")

    return len(records)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Embed unified_skills.jsonl via Ollama.")
    parser.add_argument("--input", default="data/unified_skills.jsonl")
    parser.add_argument("--output-embeddings", default="data/embeddings.npy")
    parser.add_argument("--output-ordered", default="data/unified_skills_ordered.jsonl")
    parser.add_argument("--ollama-url", default=OLLAMA_URL)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    if not check_ollama_available(args.ollama_url):
        raise SystemExit(
            f"Ollama is not available at {args.ollama_url}. "
            "Start Ollama and pull qwen3-embedding:0.6b, then retry."
        )

    n = run_embed(
        input_path=args.input,
        output_embeddings=args.output_embeddings,
        output_ordered=args.output_ordered,
        ollama_url=args.ollama_url,
        batch_size=args.batch_size,
    )
    print(f"Done. Embedded {n} records.")
