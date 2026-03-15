"""
Integration tests for search.py against a real (tiny) FAISS index.

The index is built fresh from fixture data with a patched embed_batch.
Ollama is also mocked at query time for determinism. Tests verify that
the search → filter → format pipeline works end-to-end correctly.

Note: built_index is function-scoped (not module-scoped) because
mock_ollama_embed is a function-scoped fixture and module-scoped fixtures
cannot depend on function-scoped ones.
"""
import json
from pathlib import Path
from unittest.mock import patch

import faiss
import numpy as np
import pytest

from scripts.search import apply_filters, format_results, load_index, search


def _build_test_index(tmp_path: Path, mock_embed_fn) -> Path:
    """Build a tiny FAISS index from fixture data. Returns the data directory."""
    from pipeline.normalize import normalize
    from pipeline.embed import run_embed
    from pipeline.build_index import run_build_index

    raw_dir = Path(__file__).parent.parent / "fixtures" / "raw"
    raw_files = [str(p) for p in raw_dir.glob("*.jsonl")]

    unified = str(tmp_path / "unified.jsonl")
    normalize(raw_files, unified)

    with patch("pipeline.embed.embed_batch", side_effect=mock_embed_fn):
        run_embed(unified, str(tmp_path / "embeddings.npy"), str(tmp_path / "ordered.jsonl"))

    run_build_index(
        str(tmp_path / "embeddings.npy"),
        str(tmp_path / "ordered.jsonl"),
        str(tmp_path / "index.faiss"),
        str(tmp_path / "metadata.jsonl"),
        str(tmp_path / "version.txt"),
    )
    return tmp_path


@pytest.fixture
def built_index(tmp_path, mock_ollama_embed):
    """Build a real tiny FAISS index from fixture data."""
    return _build_test_index(tmp_path, mock_ollama_embed)


class TestSearchIntegration:
    def _random_query_vec(self, seed=0):
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(1024).astype(np.float32)
        v /= np.linalg.norm(v)
        return v

    def test_search_returns_non_empty_results(self, built_index):
        index, metadata = load_index(
            str(built_index / "index.faiss"),
            str(built_index / "metadata.jsonl"),
        )
        vec = self._random_query_vec()
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("deploy kubernetes", index, metadata, propose_n=5)
        assert len(results) > 0

    @pytest.mark.parametrize("propose_n", [1, 3, 5])
    def test_results_do_not_exceed_propose_n_times_3(self, built_index, propose_n):
        index, metadata = load_index(
            str(built_index / "index.faiss"),
            str(built_index / "metadata.jsonl"),
        )
        vec = self._random_query_vec()
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("test query", index, metadata, propose_n=propose_n)
        assert len(results) <= propose_n * 3

    def test_all_results_have_required_fields(self, built_index):
        index, metadata = load_index(
            str(built_index / "index.faiss"),
            str(built_index / "metadata.jsonl"),
        )
        vec = self._random_query_vec()
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("test query", index, metadata)
        required = {"sim_score", "name", "description", "repo_url", "install_cmd", "quality"}
        for r in results:
            missing = required - set(r.keys())
            assert not missing, f"Result missing fields: {missing}"

    def test_sim_scores_between_0_and_1(self, built_index):
        index, metadata = load_index(
            str(built_index / "index.faiss"),
            str(built_index / "metadata.jsonl"),
        )
        vec = self._random_query_vec()
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("test query", index, metadata)
        for r in results:
            assert 0.0 <= r["sim_score"] <= 1.0, f"sim_score out of range: {r['sim_score']}"

    def test_platform_filter_only_returns_matching_skills(self, built_index):
        index, metadata = load_index(
            str(built_index / "index.faiss"),
            str(built_index / "metadata.jsonl"),
        )
        vec = self._random_query_vec()
        with patch("scripts.search.embed_query", return_value=vec):
            claude_results = search("test", index, metadata, propose_n=10, platforms=["claude_code"])
        for r in claude_results:
            assert "claude_code" in r["install_cmd"]

    def test_json_output_is_parseable(self, built_index):
        index, metadata = load_index(
            str(built_index / "index.faiss"),
            str(built_index / "metadata.jsonl"),
        )
        vec = self._random_query_vec()
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("test query", index, metadata)
        parsed = json.loads(format_results(results, as_json=True))
        assert isinstance(parsed, dict)
        assert isinstance(parsed.get("results"), list)

    def test_search_with_no_index_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_index(
                str(tmp_path / "nonexistent.faiss"),
                str(tmp_path / "nonexistent.jsonl"),
            )
