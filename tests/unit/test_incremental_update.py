"""
Unit tests for pipeline/incremental_update.py

Covers:
  - load_existing_ids: reads IDs from metadata.jsonl
  - find_new_skills: filters skills not already in the index
  - run_incremental_update: orchestration including model-mismatch guard,
    IVFFlat guard, no-op when nothing new, alignment invariant after append
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import faiss
import numpy as np
import pytest

from pipeline.build_index import IVF_THRESHOLD
from pipeline.embed import MODEL
from pipeline.incremental_update import (
    IncrementalError,
    find_new_skills,
    load_existing_ids,
    run_incremental_update,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_metadata(tmp_path: Path, skills: list[dict]) -> Path:
    """Write a metadata.jsonl file and return its path."""
    p = tmp_path / "metadata.jsonl"
    with p.open("w") as f:
        for s in skills:
            f.write(json.dumps(s) + "\n")
    return p


def _make_unified_skills(tmp_path: Path, skills: list[dict], filename="new_skills.jsonl") -> Path:
    """Write a unified_skills.jsonl file and return its path."""
    p = tmp_path / filename
    with p.open("w") as f:
        for s in skills:
            f.write(json.dumps(s) + "\n")
    return p


def _make_flat_index(tmp_path: Path, n_vecs: int = 3, dim: int = 16) -> Path:
    """Build a small IndexScalarQuantizer (SQ8) and write it to disk.

    Mirrors the production index type used for corpora below IVF_THRESHOLD.
    """
    rng = np.random.default_rng(42)
    vecs = rng.standard_normal((n_vecs, dim)).astype(np.float32)
    faiss.normalize_L2(vecs)
    idx = faiss.IndexScalarQuantizer(dim, faiss.ScalarQuantizer.QT_8bit, faiss.METRIC_INNER_PRODUCT)
    idx.train(vecs)
    idx.add(vecs)
    p = tmp_path / "index.faiss"
    faiss.write_index(idx, str(p))
    return p


def _make_version_txt(tmp_path: Path, embed_model: str = MODEL) -> Path:
    """Write a minimal version.txt with the given embed_model."""
    p = tmp_path / "version.txt"
    p.write_text(
        "date: 2026-03-10\n"
        "skill_count: 3\n"
        "index_sha256: abc\n"
        "metadata_sha256: def\n"
        f"embed_model: {embed_model}\n"
    )
    return p


def _make_skill(skill_id: str, name: str = None) -> dict:
    return {
        "id": skill_id,
        "name": name or skill_id,
        "description": f"Description for {skill_id}.",
        "embedding_text": f"{name or skill_id}. Description for {skill_id}. Categories: testing.",
        "source": ["skillsmp"],
        "categories": ["testing"],
        "platforms": ["claude_code"],
        "repo_url": f"https://github.com/user/{skill_id}",
        "install_cmd": {"claude_code": f"/plugin install {skill_id}"},
        "quality": {"stars": 10, "skillhub_rank": None, "skillhub_score": None, "last_updated": "2026-01-01"},
    }


# ---------------------------------------------------------------------------
# load_existing_ids
# ---------------------------------------------------------------------------

class TestLoadExistingIds:
    def test_returns_set_of_ids(self, tmp_path):
        skills = [_make_skill("aaa"), _make_skill("bbb")]
        meta = _make_metadata(tmp_path, skills)
        ids = load_existing_ids(str(meta))
        assert ids == {"aaa", "bbb"}

    def test_empty_file_returns_empty_set(self, tmp_path):
        meta = tmp_path / "metadata.jsonl"
        meta.write_text("")
        ids = load_existing_ids(str(meta))
        assert ids == set()

    def test_skips_blank_lines(self, tmp_path):
        meta = tmp_path / "metadata.jsonl"
        meta.write_text(json.dumps({"id": "aaa"}) + "\n\n" + json.dumps({"id": "bbb"}) + "\n")
        ids = load_existing_ids(str(meta))
        assert ids == {"aaa", "bbb"}

    def test_skips_records_without_id(self, tmp_path):
        meta = tmp_path / "metadata.jsonl"
        meta.write_text(json.dumps({"name": "no-id"}) + "\n" + json.dumps({"id": "ccc"}) + "\n")
        ids = load_existing_ids(str(meta))
        assert ids == {"ccc"}


# ---------------------------------------------------------------------------
# find_new_skills
# ---------------------------------------------------------------------------

class TestFindNewSkills:
    def test_filters_known_ids(self, tmp_path):
        skills = [_make_skill("aaa"), _make_skill("bbb"), _make_skill("ccc")]
        new_skills_path = _make_unified_skills(tmp_path, skills)
        result = find_new_skills(str(new_skills_path), existing_ids={"aaa", "bbb"})
        assert len(result) == 1
        assert result[0]["id"] == "ccc"

    def test_returns_all_when_no_existing(self, tmp_path):
        skills = [_make_skill("aaa"), _make_skill("bbb")]
        new_skills_path = _make_unified_skills(tmp_path, skills)
        result = find_new_skills(str(new_skills_path), existing_ids=set())
        assert len(result) == 2

    def test_returns_empty_when_all_known(self, tmp_path):
        skills = [_make_skill("aaa"), _make_skill("bbb")]
        new_skills_path = _make_unified_skills(tmp_path, skills)
        result = find_new_skills(str(new_skills_path), existing_ids={"aaa", "bbb"})
        assert result == []

    def test_skips_blank_lines(self, tmp_path):
        p = tmp_path / "new_skills.jsonl"
        p.write_text(json.dumps(_make_skill("aaa")) + "\n\n" + json.dumps(_make_skill("bbb")) + "\n")
        result = find_new_skills(str(p), existing_ids=set())
        assert len(result) == 2


# ---------------------------------------------------------------------------
# run_incremental_update
# ---------------------------------------------------------------------------

class TestRunIncrementalUpdate:
    """Tests for run_incremental_update orchestration."""

    def _setup(self, tmp_path, existing_ids, new_skills, embed_model=MODEL):
        """Build a minimal on-disk environment for run_incremental_update."""
        meta = _make_metadata(tmp_path, [_make_skill(i) for i in existing_ids])
        new_skills_path = _make_unified_skills(tmp_path, new_skills)
        index_path = _make_flat_index(tmp_path, n_vecs=max(len(existing_ids), 1))
        version_path = _make_version_txt(tmp_path, embed_model=embed_model)
        return str(new_skills_path), str(index_path), str(meta), str(version_path)

    def test_embed_model_mismatch_raises(self, tmp_path):
        """Version.txt records a different model → IncrementalError."""
        new_skills_path, index_path, meta, version_path = self._setup(
            tmp_path,
            existing_ids=["aaa"],
            new_skills=[_make_skill("bbb")],
            embed_model="some-other-model:1.0",
        )
        with pytest.raises(IncrementalError, match="model"):
            run_incremental_update(new_skills_path, index_path, meta, version_path)

    def test_matching_embed_model_proceeds(self, tmp_path):
        """Matching model in version.txt → no IncrementalError from model check."""
        new_skill = _make_skill("bbb")
        new_skills_path, index_path, meta, version_path = self._setup(
            tmp_path,
            existing_ids=["aaa"],
            new_skills=[new_skill],
            embed_model=MODEL,
        )
        fake_vec = np.random.default_rng(0).standard_normal((1, 16)).astype(np.float32)
        with patch("pipeline.incremental_update.embed_all", return_value=fake_vec):
            with patch("pipeline.incremental_update.sha256_file", return_value="fakehash"):
                result = run_incremental_update(new_skills_path, index_path, meta, version_path)
        assert result["added"] == 1

    def test_ivfflat_guard_raises(self, tmp_path):
        """Index with ntotal >= IVF_THRESHOLD → IncrementalError with rebuild hint."""
        existing_ids = ["aaa"]
        new_skills_path, index_path, meta, version_path = self._setup(
            tmp_path, existing_ids=existing_ids, new_skills=[_make_skill("bbb")]
        )
        # Inject a mock FAISS index whose ntotal is >= IVF_THRESHOLD
        mock_index = MagicMock()
        mock_index.ntotal = IVF_THRESHOLD
        with patch("faiss.read_index", return_value=mock_index):
            with pytest.raises(IncrementalError, match="rebuild"):
                run_incremental_update(new_skills_path, index_path, meta, version_path)

    def test_no_new_skills_is_noop(self, tmp_path):
        """All IDs already indexed → returns added=0, skipped='no_new_skills'."""
        new_skills_path, index_path, meta, version_path = self._setup(
            tmp_path, existing_ids=["aaa", "bbb"], new_skills=[_make_skill("aaa")]
        )
        result = run_incremental_update(new_skills_path, index_path, meta, version_path)
        assert result["added"] == 0
        assert result["skipped"] == "no_new_skills"

    def test_append_preserves_alignment(self, tmp_path):
        """After appending N new skills, index.ntotal == number of metadata rows."""
        new_skills = [_make_skill("bbb"), _make_skill("ccc")]
        new_skills_path, index_path, meta, version_path = self._setup(
            tmp_path, existing_ids=["aaa"], new_skills=new_skills
        )
        # Return 2 fake vectors for the 2 new skills (dim=16 matches _make_flat_index)
        fake_vecs = np.random.default_rng(1).standard_normal((2, 16)).astype(np.float32)
        with patch("pipeline.incremental_update.embed_all", return_value=fake_vecs):
            with patch("pipeline.incremental_update.sha256_file", return_value="fakehash"):
                run_incremental_update(new_skills_path, index_path, meta, version_path)

        # Reload index and metadata to verify alignment
        updated_index = faiss.read_index(index_path)
        metadata_rows = [
            json.loads(line) for line in Path(meta).read_text().splitlines() if line.strip()
        ]
        assert updated_index.ntotal == len(metadata_rows), (
            f"Alignment broken: index.ntotal={updated_index.ntotal}, "
            f"metadata rows={len(metadata_rows)}"
        )
