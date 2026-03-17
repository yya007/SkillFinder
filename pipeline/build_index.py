"""
pipeline/build_index.py

Builds a FAISS index from pre-computed embeddings and ordered skill metadata.
Produces three output artefacts:
  - index.faiss       (FAISS index)
  - metadata.jsonl    (one JSON record per row, aligned with index)
  - version.txt       (YAML-like manifest with hashes and counts)

Usage:
    python pipeline/build_index.py \\
        --embeddings  data/embeddings.npy \\
        --skills      data/ordered_skills.jsonl \\
        --out-index   data/index.faiss \\
        --out-meta    data/metadata.jsonl \\
        --out-version data/version.txt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date
from typing import Optional

import faiss
import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DIM: int = 1024
IVF_THRESHOLD: int = 30_000   # use IVFSQFlat for >= this many vectors
IVF_NLIST: int = 256


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AlignmentError(Exception):
    """Raised when index.ntotal does not match len(metadata)."""


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def l2_normalize(embeddings: np.ndarray) -> np.ndarray:
    """Normalise *embeddings* to unit length in-place using FAISS.

    Parameters
    ----------
    embeddings:
        Array of shape ``(N, DIM)`` with dtype ``float32``.

    Returns
    -------
    np.ndarray
        The same array object (in-place), now L2-normalised.

    Raises
    ------
    ValueError
        If *embeddings* is 1-D or its dtype is not ``float32``.
    """
    if embeddings.ndim != 2:
        raise ValueError(
            f"embeddings must be 2-D (N, DIM), got shape {embeddings.shape}"
        )
    if embeddings.dtype != np.float32:
        raise ValueError(
            f"embeddings dtype must be float32, got {embeddings.dtype}"
        )
    faiss.normalize_L2(embeddings)
    return embeddings


def build_index(embeddings: np.ndarray) -> faiss.Index:
    """Build a FAISS index from L2-normalised *embeddings*.

    Uses ``IndexScalarQuantizer`` (SQ8) for corpora smaller than
    ``IVF_THRESHOLD`` vectors, and ``IndexIVFScalarQuantizer`` (IVF+SQ8)
    for larger corpora.  Both require a ``train()`` call to learn per-dimension
    quantization ranges.  SQ8 reduces index size ~4× vs float32 with ~99%
    recall.

    Parameters
    ----------
    embeddings:
        Array of shape ``(N, DIM)`` with dtype ``float32``.  Vectors are
        assumed to already be L2-normalised (inner-product ≡ cosine similarity).

    Returns
    -------
    faiss.Index

    Raises
    ------
    ValueError
        If ``embeddings.shape[1] != DIM``.
    """
    if embeddings.shape[1] != DIM:
        raise ValueError(
            f"Embedding dimension must be {DIM}, got {embeddings.shape[1]}"
        )

    n = embeddings.shape[0]

    if n < IVF_THRESHOLD:
        index = faiss.IndexScalarQuantizer(
            DIM, faiss.ScalarQuantizer.QT_8bit, faiss.METRIC_INNER_PRODUCT
        )
        index.train(embeddings)
        index.add(embeddings)
    else:
        quantizer = faiss.IndexFlatIP(DIM)
        index = faiss.IndexIVFScalarQuantizer(
            quantizer, DIM, IVF_NLIST,
            faiss.ScalarQuantizer.QT_8bit, faiss.METRIC_INNER_PRODUCT,
        )
        index.train(embeddings)
        index.add(embeddings)

    return index


def verify_alignment(index: faiss.Index, metadata: list[dict]) -> None:
    """Raise ``AlignmentError`` if index row count != metadata record count.

    Parameters
    ----------
    index:
        A trained and populated FAISS index.
    metadata:
        List of skill dictionaries, one per index row.

    Raises
    ------
    AlignmentError
    """
    if index.ntotal != len(metadata):
        raise AlignmentError(
            f"Index has {index.ntotal} vectors but metadata has {len(metadata)} records."
        )


def sha256_file(path: str) -> str:
    """Return the SHA-256 hex digest of the file at *path*.

    Parameters
    ----------
    path:
        Absolute or relative path to the file.

    Returns
    -------
    str
        64-character lowercase hex string.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_version_txt(
    path: str,
    date: str,
    skill_count: int,
    index_sha256: str,
    metadata_sha256: str,
    embed_model: str = "",
) -> None:
    """Write a YAML-like version manifest to *path*.

    Parameters
    ----------
    path:
        Output file path.
    date:
        ISO-8601 date string, e.g. ``"2026-03-10"``.
    skill_count:
        Total number of skills indexed.
    index_sha256:
        Hex digest of the ``.faiss`` file.
    metadata_sha256:
        Hex digest of the ``metadata.jsonl`` file.
    embed_model:
        Ollama model identifier used to build the embeddings (e.g.
        ``"qwen3-embedding:0.6b"``).  Stored so incremental updates can
        detect embedding-space mismatches before appending vectors.
    """
    content = (
        f"date: {date}\n"
        f"skill_count: {skill_count}\n"
        f"embed_model: {embed_model}\n"
        f"index_sha256: {index_sha256}\n"
        f"metadata_sha256: {metadata_sha256}\n"
    )
    with open(path, "w") as fh:
        fh.write(content)


def read_version_txt(path: str) -> dict:
    """Parse a version manifest written by :func:`write_version_txt`.

    Parameters
    ----------
    path:
        Path to ``version.txt``.

    Returns
    -------
    dict
        Keys: ``date`` (str), ``skill_count`` (int), ``index_sha256`` (str),
        ``metadata_sha256`` (str).

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If any required key is missing from the file.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"version.txt not found: {path}")

    required_keys = {"date", "skill_count", "index_sha256", "metadata_sha256"}
    data: dict = {}

    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip()

    missing = required_keys - data.keys()
    if missing:
        raise ValueError(f"version.txt is missing required keys: {missing}")

    data["skill_count"] = int(data["skill_count"])
    return data


# ---------------------------------------------------------------------------
# End-to-end pipeline entry point
# ---------------------------------------------------------------------------

def run_build_index(
    embeddings_path: str,
    ordered_skills_path: str,
    output_index: str,
    output_metadata: str,
    output_version: str,
    date: Optional[str] = None,
    embed_model: str = "",
) -> dict:
    """Full build pipeline: load → normalise → index → write artefacts.

    Parameters
    ----------
    embeddings_path:
        Path to a ``.npy`` file containing a float32 array of shape ``(N, DIM)``.
    ordered_skills_path:
        Path to a JSONL file where line *i* corresponds to vector *i*.
    output_index:
        Destination path for the FAISS index (``.faiss``).
    output_metadata:
        Destination path for the aligned metadata JSONL.
    output_version:
        Destination path for ``version.txt``.
    date:
        ISO-8601 date string for the version manifest.  Defaults to today.

    Returns
    -------
    dict
        ``{"skill_count": int, "index_sha256": str, "metadata_sha256": str}``

    Raises
    ------
    FileNotFoundError
        If *embeddings_path* or *ordered_skills_path* does not exist.
    AlignmentError
        If the number of embeddings differs from the number of skill records.
    """
    # --- validate inputs ----------------------------------------------------
    if not os.path.exists(embeddings_path):
        raise FileNotFoundError(f"Embeddings file not found: {embeddings_path}")
    if not os.path.exists(ordered_skills_path):
        raise FileNotFoundError(f"Ordered skills JSONL not found: {ordered_skills_path}")

    # --- load embeddings ----------------------------------------------------
    embeddings = np.load(embeddings_path)

    # --- load ordered skill records -----------------------------------------
    skills: list[dict] = []
    with open(ordered_skills_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                skills.append(json.loads(line))

    # --- alignment check (pre-build) ----------------------------------------
    if len(embeddings) != len(skills):
        raise AlignmentError(
            f"Embeddings count ({len(embeddings)}) != skill records count ({len(skills)})"
        )

    # --- normalise -----------------------------------------------------------
    embeddings = embeddings.astype(np.float32)
    l2_normalize(embeddings)

    # --- build index ---------------------------------------------------------
    index = build_index(embeddings)

    # --- alignment check (post-build) ----------------------------------------
    verify_alignment(index, skills)

    # --- write index ---------------------------------------------------------
    faiss.write_index(index, output_index)

    # --- write metadata JSONL ------------------------------------------------
    with open(output_metadata, "w") as fh:
        for skill in skills:
            fh.write(json.dumps(skill) + "\n")

    # --- compute hashes -------------------------------------------------------
    index_sha256 = sha256_file(output_index)
    metadata_sha256 = sha256_file(output_metadata)

    # --- write version.txt ---------------------------------------------------
    build_date = date or str(_today())
    write_version_txt(
        output_version,
        date=build_date,
        skill_count=len(skills),
        index_sha256=index_sha256,
        metadata_sha256=metadata_sha256,
        embed_model=embed_model,
    )

    return {
        "skill_count": len(skills),
        "index_sha256": index_sha256,
        "metadata_sha256": metadata_sha256,
    }


def _today() -> "date":
    from datetime import date as _date
    return _date.today()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build FAISS index from embeddings and ordered skill JSONL."
    )
    parser.add_argument("--embeddings", required=True, help="Path to embeddings .npy file")
    parser.add_argument("--skills", required=True, help="Path to ordered skills JSONL")
    parser.add_argument("--out-index", required=True, help="Output path for index.faiss")
    parser.add_argument("--out-meta", required=True, help="Output path for metadata.jsonl")
    parser.add_argument("--out-version", required=True, help="Output path for version.txt")
    parser.add_argument("--date", default=None, help="Build date (ISO-8601); defaults to today")
    parser.add_argument(
        "--embed-model",
        default="",
        metavar="MODEL",
        help="Ollama model used to build embeddings (e.g. qwen3-embedding:0.6b). "
             "Stored in version.txt to guard against incremental mismatches.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run_build_index(
        embeddings_path=args.embeddings,
        ordered_skills_path=args.skills,
        output_index=args.out_index,
        output_metadata=args.out_meta,
        output_version=args.out_version,
        date=args.date,
        embed_model=args.embed_model,
    )
    print(f"Build complete: {result['skill_count']} skills indexed.")
    print(f"  index_sha256:    {result['index_sha256']}")
    print(f"  metadata_sha256: {result['metadata_sha256']}")
