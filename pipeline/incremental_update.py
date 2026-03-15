"""
pipeline/incremental_update.py — Append new skills to an existing FAISS index.

Used by the CI pipeline when the delta between the new crawl and the existing
index is small (< 20%). Only new skills (by ID) are embedded and appended —
existing skills are untouched, avoiding the ~60-minute full re-embed cost.

Limitations:
  - Cannot update changed descriptions or metadata for existing skills.
  - Cannot remove skills deleted upstream.
  - Only works with IndexFlatIP (< 50k vectors). IVFFlat requires full rebuild.
  Use full rebuild for any of the above.

Steps:
  1. Load existing skill IDs from metadata.jsonl.
  2. Load new unified_skills.jsonl and identify skills not yet in the index.
  3. Embed only the new skills via Ollama.
  4. L2-normalise and append vectors to the existing FAISS index.
  5. Append new metadata rows to metadata.jsonl.
  6. Update version.txt with the new count and checksums.

Usage:
    python pipeline/incremental_update.py \\
        --new-skills data/unified_skills.jsonl \\
        --index      data/index.faiss \\
        --metadata   data/metadata.jsonl \\
        --version    data/version.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date as _date
from pathlib import Path

import faiss
import numpy as np

from pipeline.build_index import IVF_THRESHOLD, l2_normalize, sha256_file, write_version_txt
from pipeline.embed import MODEL, OLLAMA_URL, check_ollama_available, embed_all

logger = logging.getLogger(__name__)


class IncrementalError(Exception):
    """Raised when the index cannot be updated incrementally."""


def load_existing_ids(metadata_path: str) -> set[str]:
    """Return the set of skill IDs already recorded in metadata.jsonl."""
    ids: set[str] = set()
    with open(metadata_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                if "id" in rec:
                    ids.add(rec["id"])
    return ids


def find_new_skills(new_skills_path: str, existing_ids: set[str]) -> list[dict]:
    """Return skills from *new_skills_path* whose ID is not in *existing_ids*."""
    new_skills: list[dict] = []
    with open(new_skills_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("id") not in existing_ids:
                new_skills.append(rec)
    return new_skills


def run_incremental_update(
    new_skills_path: str,
    index_path: str,
    metadata_path: str,
    version_path: str,
    ollama_url: str = OLLAMA_URL,
) -> dict:
    """Append new skills to the existing FAISS index.

    Returns:
        dict with keys:
          - "added"   (int): number of skills added
          - "total"   (int): new total skill count
          - "skipped" (str | None): reason if no update was needed, else None

    Raises:
        FileNotFoundError: if index, metadata, or new-skills file is missing.
        IncrementalError:  if the existing index type requires a full rebuild.
        OllamaError:       if embedding fails.
    """
    for path in (new_skills_path, index_path, metadata_path):
        if not Path(path).exists():
            raise FileNotFoundError(f"Required file not found: {path}")

    existing_ids = load_existing_ids(metadata_path)
    new_skills = find_new_skills(new_skills_path, existing_ids)

    if not new_skills:
        logger.info("No new skills found — index is already up to date.")
        return {"added": 0, "total": len(existing_ids), "skipped": "no_new_skills"}

    logger.info(
        "Found %d new skills to embed and append (existing: %d).",
        len(new_skills),
        len(existing_ids),
    )

    # Verify the existing index was built with the same embedding model
    try:
        from pipeline.build_index import read_version_txt
        existing_version = read_version_txt(version_path)
        existing_model = existing_version.get("embed_model", "")
        if existing_model and existing_model != MODEL:
            raise IncrementalError(
                f"Embedding model mismatch: index was built with '{existing_model}' "
                f"but current model is '{MODEL}'. "
                "Mixing embedding spaces produces silently incorrect results. "
                "Run a full rebuild to resolve this."
            )
    except FileNotFoundError:
        logger.warning(
            "version.txt not found — cannot verify embedding model compatibility. "
            "Proceeding; run a full rebuild if search quality degrades."
        )

    # Load and validate the existing FAISS index
    index = faiss.read_index(index_path)
    if index.ntotal >= IVF_THRESHOLD:
        raise IncrementalError(
            f"Index has {index.ntotal} vectors (≥ IVF_THRESHOLD {IVF_THRESHOLD}). "
            "Large indices require a full rebuild rather than an incremental append. "
            "Run:\n"
            "  python pipeline/embed.py --input data/unified_skills.jsonl "
            "--output-embeddings data/embeddings.npy "
            "--output-ordered data/unified_skills_ordered.jsonl\n"
            "  python pipeline/build_index.py --embeddings data/embeddings.npy "
            "--skills data/unified_skills_ordered.jsonl "
            "--out-index data/index.faiss --out-meta data/metadata.jsonl "
            "--out-version data/version.txt --embed-model qwen3-embedding:0.6b"
        )

    # Embed only the new skills
    logger.info("Embedding %d new skills via Ollama...", len(new_skills))
    new_vecs = embed_all(new_skills, ollama_url=ollama_url)
    new_vecs = new_vecs.astype(np.float32)
    l2_normalize(new_vecs)

    # Append to the FAISS index and persist
    index.add(new_vecs)
    faiss.write_index(index, index_path)

    # Append new metadata rows
    with open(metadata_path, "a") as f:
        for skill in new_skills:
            f.write(json.dumps(skill, ensure_ascii=False) + "\n")

    total = len(existing_ids) + len(new_skills)

    # Update version.txt
    index_sha = sha256_file(index_path)
    meta_sha = sha256_file(metadata_path)
    write_version_txt(
        version_path,
        date=str(_date.today()),
        skill_count=total,
        index_sha256=index_sha,
        metadata_sha256=meta_sha,
        embed_model=MODEL,
    )

    logger.info(
        "Incremental update complete: added %d skills, total now %d.",
        len(new_skills),
        total,
    )
    return {"added": len(new_skills), "total": total, "skipped": None}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Append new skills to an existing FAISS index (incremental update)."
    )
    parser.add_argument("--new-skills", default="data/unified_skills.jsonl",
                        help="Path to newly normalized unified_skills.jsonl.")
    parser.add_argument("--index", default="data/index.faiss",
                        help="Path to existing index.faiss (updated in-place).")
    parser.add_argument("--metadata", default="data/metadata.jsonl",
                        help="Path to existing metadata.jsonl (appended in-place).")
    parser.add_argument("--version", default="data/version.txt",
                        help="Path to version.txt (overwritten with new stats).")
    parser.add_argument("--ollama-url", default=OLLAMA_URL)
    args = parser.parse_args()

    if not check_ollama_available(args.ollama_url):
        print(f"Error: Ollama not available at {args.ollama_url}", file=sys.stderr)
        sys.exit(1)

    try:
        result = run_incremental_update(
            new_skills_path=args.new_skills,
            index_path=args.index,
            metadata_path=args.metadata,
            version_path=args.version,
            ollama_url=args.ollama_url,
        )
        if result["skipped"]:
            print(f"No update needed: {result['skipped']}. Total: {result['total']} skills.")
        else:
            print(f"Done. Added {result['added']} skills. Total: {result['total']}.")
    except IncrementalError as exc:
        print(f"Incremental update not possible: {exc}", file=sys.stderr)
        print(
            "Run a full rebuild instead:\n"
            "  python pipeline/embed.py && python pipeline/build_index.py",
            file=sys.stderr,
        )
        sys.exit(2)
