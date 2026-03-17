"""
Unit tests for pipeline/build_index.py

Tests cover:
  - l2_normalize: unit-length vectors, in-place operation
  - build_index: IndexFlatIP for small sets, IVFFlat for large sets
  - verify_alignment: count matching, AlignmentError on mismatch
  - sha256_file: correct hash computation
  - write_version_txt / read_version_txt: round-trip
  - run_build_index: full pipeline, output files, return dict
  - AlignmentError: raised on count mismatch
"""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import faiss
import numpy as np
import pytest

from pipeline.build_index import (
    DIM,
    AlignmentError,
    build_index,
    l2_normalize,
    read_version_txt,
    run_build_index,
    sha256_file,
    verify_alignment,
    write_version_txt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def random_embeddings(n: int, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal((n, DIM)).astype(np.float32)


def write_jsonl(path: Path, records: list[dict]):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# l2_normalize
# ---------------------------------------------------------------------------

class TestL2Normalize:
    def test_vectors_have_unit_norm_after_normalization(self):
        vecs = random_embeddings(10)
        result = l2_normalize(vecs)
        norms = np.linalg.norm(result, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-6)

    def test_returns_same_array(self):
        vecs = random_embeddings(5)
        original_id = id(vecs)
        result = l2_normalize(vecs)
        assert id(result) == original_id

    def test_modifies_in_place(self):
        vecs = random_embeddings(5)
        l2_normalize(vecs)
        norms = np.linalg.norm(vecs, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-6)

    def test_raises_on_1d_input(self):
        with pytest.raises(ValueError):
            l2_normalize(np.zeros(DIM, dtype=np.float32))

    def test_raises_on_wrong_dtype(self):
        vecs = random_embeddings(5).astype(np.float64)
        with pytest.raises(ValueError):
            l2_normalize(vecs)


# ---------------------------------------------------------------------------
# build_index
# ---------------------------------------------------------------------------

class TestBuildIndex:
    def test_small_corpus_uses_flat_index(self):
        vecs = random_embeddings(100)
        l2_normalize(vecs)
        index = build_index(vecs)
        assert isinstance(index, faiss.IndexFlatIP)

    def test_index_ntotal_equals_input_count(self):
        n = 50
        vecs = random_embeddings(n)
        l2_normalize(vecs)
        index = build_index(vecs)
        assert index.ntotal == n

    def test_index_dimension_is_correct(self):
        vecs = random_embeddings(10)
        l2_normalize(vecs)
        index = build_index(vecs)
        assert index.d == DIM

    def test_search_returns_results(self):
        vecs = random_embeddings(20)
        l2_normalize(vecs)
        index = build_index(vecs)
        query = vecs[:1].copy()
        scores, ids = index.search(query, 5)
        assert scores.shape == (1, 5)
        assert ids[0][0] == 0  # top result should be itself

    def test_top_result_is_self(self):
        vecs = random_embeddings(20)
        l2_normalize(vecs)
        index = build_index(vecs)
        for i in range(len(vecs)):
            query = vecs[i:i+1].copy()
            _, ids = index.search(query, 1)
            assert ids[0][0] == i

    def test_raises_on_wrong_shape(self):
        with pytest.raises(ValueError):
            build_index(np.zeros((10, DIM + 1), dtype=np.float32))

    def test_ivf_threshold_boundary(self):
        """Below threshold → IndexFlatIP; at threshold → IndexIVFFlat."""
        import pipeline.build_index as bi_module
        with patch.object(bi_module, "IVF_THRESHOLD", 5):
            # 4 vectors: below threshold → FlatIP
            vecs_small = random_embeddings(4)
            l2_normalize(vecs_small)
            assert isinstance(build_index(vecs_small), faiss.IndexFlatIP)

            # 5 vectors: at threshold → IVFFlat (nlist ≤ n so training succeeds)
            with patch.object(bi_module, "IVF_NLIST", 2):
                vecs_large = random_embeddings(5)
                l2_normalize(vecs_large)
                assert isinstance(build_index(vecs_large), faiss.IndexIVFFlat)


# ---------------------------------------------------------------------------
# verify_alignment
# ---------------------------------------------------------------------------

class TestVerifyAlignment:
    def test_passes_when_counts_match(self):
        vecs = random_embeddings(5)
        l2_normalize(vecs)
        index = build_index(vecs)
        metadata = [{"name": f"skill-{i}"} for i in range(5)]
        verify_alignment(index, metadata)  # should not raise

    def test_raises_alignment_error_on_mismatch(self):
        vecs = random_embeddings(5)
        l2_normalize(vecs)
        index = build_index(vecs)
        metadata = [{"name": "only-one"}]
        with pytest.raises(AlignmentError):
            verify_alignment(index, metadata)


# ---------------------------------------------------------------------------
# sha256_file
# ---------------------------------------------------------------------------

class TestSha256File:
    def test_returns_64_char_hex(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"hello world")
        result = sha256_file(str(f))
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_matches_manual_sha256(self, tmp_path):
        content = b"test content for hashing"
        f = tmp_path / "test.bin"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert sha256_file(str(f)) == expected

    def test_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sha256_file(str(tmp_path / "nonexistent.bin"))

    def test_different_content_different_hash(self, tmp_path):
        f1, f2 = tmp_path / "a.bin", tmp_path / "b.bin"
        f1.write_bytes(b"content a")
        f2.write_bytes(b"content b")
        assert sha256_file(str(f1)) != sha256_file(str(f2))


# ---------------------------------------------------------------------------
# write_version_txt / read_version_txt
# ---------------------------------------------------------------------------

class TestVersionTxt:
    def test_write_and_read_round_trip(self, tmp_path):
        path = str(tmp_path / "version.txt")
        write_version_txt(
            path,
            date="2026-03-10",
            skill_count=14823,
            index_sha256="abc123def456",
            metadata_sha256="fed654cba321",
        )
        data = read_version_txt(path)
        assert data["date"] == "2026-03-10"
        assert data["skill_count"] == 14823
        assert data["index_sha256"] == "abc123def456"
        assert data["metadata_sha256"] == "fed654cba321"

    def test_skill_count_is_int(self, tmp_path):
        path = str(tmp_path / "version.txt")
        write_version_txt(path, date="2026-01-01", skill_count=500, index_sha256="a", metadata_sha256="b")
        data = read_version_txt(path)
        assert isinstance(data["skill_count"], int)

    def test_read_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_version_txt(str(tmp_path / "missing.txt"))

    def test_read_raises_value_error_on_missing_key(self, tmp_path):
        path = tmp_path / "version.txt"
        path.write_text("date: 2026-01-01\n")  # missing other keys
        with pytest.raises(ValueError):
            read_version_txt(str(path))


# ---------------------------------------------------------------------------
# run_build_index (end-to-end)
# ---------------------------------------------------------------------------

class TestRunBuildIndex:
    def _setup(self, tmp_path: Path, n: int = 10):
        vecs = random_embeddings(n)
        # Note: run_build_index does the L2 normalization internally
        np.save(tmp_path / "embeddings.npy", vecs)
        records = [{"name": f"skill-{i}", "description": f"desc {i}"} for i in range(n)]
        write_jsonl(tmp_path / "ordered.jsonl", records)
        return tmp_path

    def test_creates_index_faiss(self, tmp_path):
        self._setup(tmp_path)
        run_build_index(
            str(tmp_path / "embeddings.npy"),
            str(tmp_path / "ordered.jsonl"),
            str(tmp_path / "index.faiss"),
            str(tmp_path / "metadata.jsonl"),
            str(tmp_path / "version.txt"),
        )
        assert (tmp_path / "index.faiss").exists()

    def test_creates_metadata_jsonl(self, tmp_path):
        self._setup(tmp_path)
        run_build_index(
            str(tmp_path / "embeddings.npy"),
            str(tmp_path / "ordered.jsonl"),
            str(tmp_path / "index.faiss"),
            str(tmp_path / "metadata.jsonl"),
            str(tmp_path / "version.txt"),
        )
        assert (tmp_path / "metadata.jsonl").exists()

    def test_creates_version_txt(self, tmp_path):
        self._setup(tmp_path)
        run_build_index(
            str(tmp_path / "embeddings.npy"),
            str(tmp_path / "ordered.jsonl"),
            str(tmp_path / "index.faiss"),
            str(tmp_path / "metadata.jsonl"),
            str(tmp_path / "version.txt"),
        )
        assert (tmp_path / "version.txt").exists()

    def test_row_alignment_index_vs_metadata(self, tmp_path):
        n = 8
        self._setup(tmp_path, n)
        run_build_index(
            str(tmp_path / "embeddings.npy"),
            str(tmp_path / "ordered.jsonl"),
            str(tmp_path / "index.faiss"),
            str(tmp_path / "metadata.jsonl"),
            str(tmp_path / "version.txt"),
        )
        index = faiss.read_index(str(tmp_path / "index.faiss"))
        meta_count = sum(1 for _ in open(tmp_path / "metadata.jsonl"))
        assert index.ntotal == meta_count == n

    def test_returns_dict_with_expected_keys(self, tmp_path):
        self._setup(tmp_path)
        result = run_build_index(
            str(tmp_path / "embeddings.npy"),
            str(tmp_path / "ordered.jsonl"),
            str(tmp_path / "index.faiss"),
            str(tmp_path / "metadata.jsonl"),
            str(tmp_path / "version.txt"),
        )
        assert "skill_count" in result
        assert "index_sha256" in result
        assert "metadata_sha256" in result

    def test_raises_alignment_error_on_count_mismatch(self, tmp_path):
        n = 10
        vecs = random_embeddings(n)
        np.save(tmp_path / "embeddings.npy", vecs)
        # Write fewer records than embeddings
        write_jsonl(tmp_path / "ordered.jsonl", [{"name": "only-one"}])
        with pytest.raises(AlignmentError):
            run_build_index(
                str(tmp_path / "embeddings.npy"),
                str(tmp_path / "ordered.jsonl"),
                str(tmp_path / "index.faiss"),
                str(tmp_path / "metadata.jsonl"),
                str(tmp_path / "version.txt"),
            )

    def test_raises_file_not_found_for_missing_embeddings(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run_build_index(
                str(tmp_path / "missing.npy"),
                str(tmp_path / "ordered.jsonl"),
                str(tmp_path / "index.faiss"),
                str(tmp_path / "metadata.jsonl"),
                str(tmp_path / "version.txt"),
            )

    def test_version_txt_skill_count_matches_actual(self, tmp_path):
        n = 6
        self._setup(tmp_path, n)
        run_build_index(
            str(tmp_path / "embeddings.npy"),
            str(tmp_path / "ordered.jsonl"),
            str(tmp_path / "index.faiss"),
            str(tmp_path / "metadata.jsonl"),
            str(tmp_path / "version.txt"),
        )
        data = read_version_txt(str(tmp_path / "version.txt"))
        assert data["skill_count"] == n
