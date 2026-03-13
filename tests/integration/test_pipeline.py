"""
Integration tests for the full offline pipeline:
  normalize → embed → build_index

Ollama is mocked with a deterministic fake. All file I/O uses real temp dirs.
Tests verify end-to-end row alignment and output correctness.
"""
import json
from pathlib import Path

import faiss
import numpy as np
import pytest

from pipeline.build_index import (
    AlignmentError,
    read_version_txt,
    run_build_index,
)
from pipeline.embed import run_embed
from pipeline.normalize import normalize


FIXTURES_RAW = Path(__file__).parent.parent / "fixtures" / "raw"


# ---------------------------------------------------------------------------
# Full pipeline: normalize → embed → build_index
# ---------------------------------------------------------------------------

class TestFullPipeline:
    def test_pipeline_produces_searchable_index(self, tmp_path, mock_ollama_embed):
        # Step 1: normalize
        raw_files = [str(p) for p in FIXTURES_RAW.glob("*.jsonl")]
        unified = str(tmp_path / "unified_skills.jsonl")
        count = normalize(raw_files, unified)
        assert count > 0

        # Step 2: embed (mocked Ollama)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pipeline.embed.embed_batch", mock_ollama_embed)
            run_embed(
                unified,
                str(tmp_path / "embeddings.npy"),
                str(tmp_path / "ordered.jsonl"),
            )

        # Step 3: build index
        result = run_build_index(
            str(tmp_path / "embeddings.npy"),
            str(tmp_path / "ordered.jsonl"),
            str(tmp_path / "index.faiss"),
            str(tmp_path / "metadata.jsonl"),
            str(tmp_path / "version.txt"),
        )

        assert result["skill_count"] == count
        index = faiss.read_index(str(tmp_path / "index.faiss"))
        meta_count = sum(1 for _ in open(tmp_path / "metadata.jsonl"))
        assert index.ntotal == meta_count == count

    def test_row_alignment_survives_full_pipeline(self, tmp_path, mock_ollama_embed):
        raw_files = [str(p) for p in FIXTURES_RAW.glob("*.jsonl")]
        unified = str(tmp_path / "unified_skills.jsonl")
        normalize(raw_files, unified)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pipeline.embed.embed_batch", mock_ollama_embed)
            run_embed(unified, str(tmp_path / "embeddings.npy"), str(tmp_path / "ordered.jsonl"))

        run_build_index(
            str(tmp_path / "embeddings.npy"),
            str(tmp_path / "ordered.jsonl"),
            str(tmp_path / "index.faiss"),
            str(tmp_path / "metadata.jsonl"),
            str(tmp_path / "version.txt"),
        )

        # Load ordered.jsonl names
        ordered_names = [json.loads(l)["name"] for l in open(tmp_path / "ordered.jsonl")]
        meta_names = [json.loads(l)["name"] for l in open(tmp_path / "metadata.jsonl")]
        assert ordered_names == meta_names

    def test_version_txt_skill_count_matches_index(self, tmp_path, mock_ollama_embed):
        raw_files = [str(p) for p in FIXTURES_RAW.glob("*.jsonl")]
        unified = str(tmp_path / "unified_skills.jsonl")
        normalize(raw_files, unified)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pipeline.embed.embed_batch", mock_ollama_embed)
            run_embed(unified, str(tmp_path / "embeddings.npy"), str(tmp_path / "ordered.jsonl"))

        run_build_index(
            str(tmp_path / "embeddings.npy"),
            str(tmp_path / "ordered.jsonl"),
            str(tmp_path / "index.faiss"),
            str(tmp_path / "metadata.jsonl"),
            str(tmp_path / "version.txt"),
        )

        version = read_version_txt(str(tmp_path / "version.txt"))
        index = faiss.read_index(str(tmp_path / "index.faiss"))
        assert version["skill_count"] == index.ntotal

    def test_no_duplicate_ids_in_output(self, tmp_path, mock_ollama_embed):
        raw_files = [str(p) for p in FIXTURES_RAW.glob("*.jsonl")]
        unified = str(tmp_path / "unified_skills.jsonl")
        normalize(raw_files, unified)

        records = [json.loads(l) for l in open(unified)]
        ids = [r["id"] for r in records]
        assert len(ids) == len(set(ids)), "Duplicate IDs found in normalized output"

    def test_pipeline_quality_gate_enforced(self, tmp_path, mock_ollama_embed):
        """normalize raises QualityGateError when output is below min_skills."""
        from pipeline.normalize import QualityGateError
        raw_files = [str(p) for p in FIXTURES_RAW.glob("*.jsonl")]
        unified = str(tmp_path / "unified_skills.jsonl")
        with pytest.raises(QualityGateError):
            normalize(raw_files, unified, min_skills=99999)

    def test_metadata_jsonl_valid_json_per_line(self, tmp_path, mock_ollama_embed):
        raw_files = [str(p) for p in FIXTURES_RAW.glob("*.jsonl")]
        unified = str(tmp_path / "unified_skills.jsonl")
        normalize(raw_files, unified)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("pipeline.embed.embed_batch", mock_ollama_embed)
            run_embed(unified, str(tmp_path / "embeddings.npy"), str(tmp_path / "ordered.jsonl"))

        run_build_index(
            str(tmp_path / "embeddings.npy"),
            str(tmp_path / "ordered.jsonl"),
            str(tmp_path / "index.faiss"),
            str(tmp_path / "metadata.jsonl"),
            str(tmp_path / "version.txt"),
        )

        for i, line in enumerate(open(tmp_path / "metadata.jsonl")):
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                pytest.fail(f"Invalid JSON on line {i}: {e}")
