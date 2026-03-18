"""
pipeline/embed.py — Embedding pipeline for SkillFinder.

Reads unified_skills.jsonl, embeds each record's `embedding_text` field via
Ollama (Qwen3-Embedding-0.6B), and writes:
  - embeddings.npy              shape (N, 1024), float32
  - unified_skills_ordered.jsonl  same records in exact row-aligned order

Documents are embedded as-is (no query prefix). The query instruction prefix
is applied only at search time in scripts/search.py.

Incremental mode: pass existing embeddings.npy + ordered.jsonl as a cache.
Records whose ``id`` and ``embedding_text`` both match a cached entry are
reused directly — only new or changed skills hit Ollama.  Cache is keyed by
``id`` (stable sha256) and invalidated when ``embedding_text`` changes, so
description/category edits automatically trigger a re-embed.

Crash-resumable mode: pass --progress-file PATH to write a JSONL progress
file as each batch is embedded.  On restart the file is read to skip batches
already completed.  The progress file is deleted on successful completion.
This is independent of the --cache-embeddings mechanism.
"""

from __future__ import annotations

import base64
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
BATCH_SIZE: int = 128
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


def load_progress_file(
    progress_path: str,
) -> dict[int, np.ndarray]:
    """Load a JSONL progress file written by ``embed_all``.

    Each line must be a JSON object with keys ``batch_idx`` (int),
    ``record_ids`` (list[str]), and ``vectors_b64`` (base64-encoded float32
    numpy array).

    Returns a mapping ``{batch_idx: vectors_array}``.  Malformed lines are
    skipped with a warning.
    """
    result: dict[int, np.ndarray] = {}
    path = Path(progress_path)
    if not path.exists():
        return result
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                batch_idx: int = entry["batch_idx"]
                raw_bytes = base64.b64decode(entry["vectors_b64"])
                vecs = np.frombuffer(raw_bytes, dtype=np.float32).reshape(-1, DIM)
                result[batch_idx] = vecs
            except Exception as exc:
                logger.warning(
                    "Skipping malformed progress entry at line %d in %s: %s",
                    lineno, progress_path, exc,
                )
    logger.info("Loaded %d completed batches from progress file %s", len(result), progress_path)
    return result


def embed_all(
    records: list[dict],
    ollama_url: str = OLLAMA_URL,
    batch_size: int = BATCH_SIZE,
    checkpoint_dir: str | None = None,
    progress_path: str | None = None,
    progress_start_idx: int = 0,
) -> np.ndarray:
    """Embed all records and return a float32 array of shape (N, DIM).

    Args:
        records:            List of unified skill dicts, each must have `embedding_text`.
        ollama_url:         Ollama base URL.
        batch_size:         Number of texts per Ollama request.
        checkpoint_dir:     If given, save partial embeddings every CHECKPOINT_EVERY
                            batches as ``embeddings_checkpoint_<batch_idx>.npy``.
        progress_path:      If given, append a JSON line per completed batch to this
                            file for crash recovery.  Already-completed batches
                            (batch_idx < progress_start_idx) are skipped entirely.
        progress_start_idx: First batch index that still needs embedding.  Batches
                            0 … progress_start_idx-1 must have been restored by the
                            caller and are expected to be prepended to the result via
                            the returned array.  ``run_embed`` handles this.

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
    prog_file = open(progress_path, "a", encoding="utf-8") if progress_path is not None else None  # noqa: SIM115

    try:
        for batch_idx in range(num_batches):
            # Skip batches that were already completed before this (sub-)run started.
            if batch_idx < progress_start_idx:
                continue

            start = batch_idx * batch_size
            end = min(start + batch_size, total)
            batch_texts = [records[j]["embedding_text"] for j in range(start, end)]
            batch_ids = [records[j].get("id", "") for j in range(start, end)]

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

            # Append progress entry so a crash can be recovered from here.
            if prog_file is not None:
                vectors_b64 = base64.b64encode(vecs.astype(np.float32).tobytes()).decode("ascii")
                entry = {
                    "batch_idx": batch_idx,
                    "record_ids": batch_ids,
                    "vectors_b64": vectors_b64,
                }
                prog_file.write(json.dumps(entry) + "\n")
                prog_file.flush()
    finally:
        if prog_file is not None:
            prog_file.close()

    return np.vstack(all_vecs) if all_vecs else np.empty((0, DIM), dtype=np.float32)


def load_embedding_cache(
    embeddings_path: str,
    ordered_path: str,
) -> dict[str, tuple[np.ndarray, str]]:
    """Load existing embeddings into a per-skill cache.

    Reads *embeddings_path* (numpy array) and *ordered_path* (JSONL) and
    returns a mapping ``{id: (vector, embedding_text)}``.

    Cache hits require both the ``id`` and the ``embedding_text`` to match —
    if ``embedding_text`` has changed the entry is treated as a miss and the
    skill will be re-embedded.

    Returns an empty dict if either file is missing or unreadable.
    """
    cache: dict[str, tuple[np.ndarray, str]] = {}
    try:
        vecs = np.load(embeddings_path)
        with open(ordered_path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                line = line.strip()
                if not line or i >= len(vecs):
                    continue
                rec = json.loads(line)
                rid = rec.get("id", "")
                if rid:
                    cache[rid] = (vecs[i], rec.get("embedding_text", ""))
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Could not load embedding cache from %s: %s", embeddings_path, exc)
    logger.info("Loaded %d cached embeddings", len(cache))
    return cache


def run_embed(
    input_path: str,
    output_embeddings: str,
    output_ordered: str,
    ollama_url: str = OLLAMA_URL,
    batch_size: int = BATCH_SIZE,
    cache_embeddings: str | None = None,
    cache_ordered: str | None = None,
    progress_path: str | None = None,
) -> int:
    """Read input JSONL, embed all records, and write outputs.

    Writes:
      - *output_embeddings*: numpy array, shape (N, 1024), float32, via np.save.
      - *output_ordered*: JSONL file with records in exactly the same row order.

    Incremental mode: provide *cache_embeddings* + *cache_ordered* (the
    previous run's outputs).  Any skill whose ``id`` and ``embedding_text``
    match a cached entry is reused directly — only new or changed skills are
    sent to Ollama.

    Crash-resumable mode: provide *progress_path*.  After each batch is
    embedded a JSON line is appended to that file.  On restart the file is
    read to find already-completed batches which are then skipped.  The file
    is deleted on successful completion.  This mechanism is independent of
    the cache mechanism above.

    Args:
        input_path:         Path to ``unified_skills.jsonl``.
        output_embeddings:  Destination path for ``embeddings.npy``.
        output_ordered:     Destination path for row-aligned ``ordered.jsonl``.
        ollama_url:         Ollama base URL.
        batch_size:         Texts per Ollama request.
        cache_embeddings:   Optional path to a previous ``embeddings.npy``.
        cache_ordered:      Optional path to the matching previous ordered JSONL.
        progress_path:      Optional path for the within-run crash-recovery file.

    Returns:
        Total number of records in the output (cached + newly embedded).

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

    # Load cache if both paths were provided.
    cache: dict[str, tuple[np.ndarray, str]] = {}
    if cache_embeddings and cache_ordered:
        cache = load_embedding_cache(cache_embeddings, cache_ordered)

    # Partition records: cache hits vs. records that need embedding.
    # A cache hit requires the same id AND the same embedding_text.
    to_embed_records: list[dict] = []
    for rec in records:
        rid = rec.get("id", "")
        cached = cache.get(rid)
        if rid and cached is not None and cached[1] == rec.get("embedding_text", ""):
            pass  # cache hit — reuse existing vector
        else:
            to_embed_records.append(rec)

    n_cached = len(records) - len(to_embed_records)
    logger.info(
        "Embedding: %d cached, %d new/changed (total %d)",
        n_cached, len(to_embed_records), len(records),
    )

    # ---------------------------------------------------------------------------
    # Crash-recovery: load progress file and determine which batches are done.
    # ---------------------------------------------------------------------------
    completed_batches: dict[int, np.ndarray] = {}
    progress_start_idx: int = 0
    if progress_path is not None:
        completed_batches = load_progress_file(progress_path)
        if completed_batches:
            # Batches are 0-based; we want the first index NOT yet completed.
            progress_start_idx = max(completed_batches.keys()) + 1
            logger.info(
                "Resuming from batch %d (%d batches already done)",
                progress_start_idx, len(completed_batches),
            )

    # ---------------------------------------------------------------------------
    # Embed only the records that need it.
    # ---------------------------------------------------------------------------
    new_vecs: np.ndarray | None = None
    if to_embed_records:
        # Reconstruct vectors for already-completed batches so we can prepend them.
        if completed_batches:
            pre_vecs_list = [completed_batches[i] for i in sorted(completed_batches.keys())]
            pre_vecs = np.vstack(pre_vecs_list)
        else:
            pre_vecs = np.empty((0, DIM), dtype=np.float32)

        # embed_all receives the full to_embed_records list but skips
        # batches 0 … progress_start_idx-1 internally.
        fresh_vecs = embed_all(
            to_embed_records,
            ollama_url=ollama_url,
            batch_size=batch_size,
            progress_path=progress_path,
            progress_start_idx=progress_start_idx,
        )

        if pre_vecs.shape[0] > 0:
            new_vecs = np.vstack([pre_vecs, fresh_vecs])
        else:
            new_vecs = fresh_vecs

    # Assemble final embeddings array in input order.
    all_vecs = np.empty((len(records), DIM), dtype=np.float32)
    new_idx = 0
    for i, rec in enumerate(records):
        rid = rec.get("id", "")
        cached = cache.get(rid)
        if rid and cached is not None and cached[1] == rec.get("embedding_text", ""):
            all_vecs[i] = cached[0]
        else:
            all_vecs[i] = new_vecs[new_idx]
            new_idx += 1

    # Save embeddings array.
    np.save(output_embeddings, all_vecs)

    # Save records in the same order — row N in embeddings == line N here.
    with open(output_ordered, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")

    # Delete the progress file now that the run completed successfully.
    if progress_path is not None:
        try:
            Path(progress_path).unlink(missing_ok=True)
            logger.info("Deleted progress file %s", progress_path)
        except OSError as exc:
            logger.warning("Could not delete progress file %s: %s", progress_path, exc)

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
    parser.add_argument(
        "--cache-embeddings",
        default=None,
        metavar="PATH",
        help="Existing embeddings.npy to reuse vectors from (incremental mode).",
    )
    parser.add_argument(
        "--cache-ordered",
        default=None,
        metavar="PATH",
        help="Existing ordered JSONL matching --cache-embeddings (incremental mode).",
    )
    parser.add_argument(
        "--progress-file",
        default=None,
        metavar="PATH",
        help=(
            "Path to a JSONL progress file for crash recovery.  After each batch "
            "a JSON line is appended.  On restart completed batches are skipped. "
            "The file is deleted on successful completion."
        ),
    )
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
        cache_embeddings=args.cache_embeddings,
        cache_ordered=args.cache_ordered,
        progress_path=args.progress_file,
    )
    print(f"Done. Embedded {n} records.")
