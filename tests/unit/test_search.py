"""
Unit tests for scripts/search.py

Ollama calls are patched throughout. Tests verify:
  - embed_query: applies QUERY_PREFIX, returns L2-normalized float32 vector
  - check_ollama: raises OllamaNotAvailableError when unreachable
  - load_index: loads index + metadata, validates alignment
  - apply_filters: platform, source, safety_only filtering (all combinations)
  - search: end-to-end with tiny real FAISS index
  - format_results: JSON and human-readable output correctness
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import faiss
import numpy as np
import pytest

from scripts.search import (
    AlignmentError,
    OllamaNotAvailableError,
    apply_filters,
    check_ollama,
    embed_query,
    format_results,
    load_index,
    search,
)


# ---------------------------------------------------------------------------
# embed_query
# ---------------------------------------------------------------------------

class TestEmbedQuery:
    def _mock_ollama(self, vec: np.ndarray):
        """Return a mock that always returns a 2D array from Ollama."""
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"embeddings": [vec.tolist()]},
            )
            yield mock_post

    def test_applies_query_prefix(self):
        from scripts.search import QUERY_PREFIX
        rng = np.random.default_rng(0)
        vec = rng.standard_normal(1024).astype(np.float32)
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"embeddings": [vec.tolist()]},
            )
            embed_query("deploy kubernetes")
            call_json = mock_post.call_args[1].get("json") or mock_post.call_args[0][1]
            sent_text = call_json["input"][0]
            assert sent_text.startswith(QUERY_PREFIX) or "Instruct:" in sent_text

    def test_returns_1d_float32_vector(self):
        rng = np.random.default_rng(0)
        vec = rng.standard_normal(1024).astype(np.float32)
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"embeddings": [vec.tolist()]},
            )
            result = embed_query("test query")
        assert result.ndim == 1
        assert result.dtype == np.float32
        assert result.shape[0] == 1024

    def test_returns_l2_normalized_vector(self):
        rng = np.random.default_rng(0)
        vec = rng.standard_normal(1024).astype(np.float32)
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"embeddings": [vec.tolist()]},
            )
            result = embed_query("test query")
        norm = np.linalg.norm(result)
        assert abs(norm - 1.0) < 1e-5

    def test_raises_ollama_not_available_on_connection_error(self):
        import requests
        with patch("requests.post", side_effect=requests.ConnectionError()):
            with pytest.raises(OllamaNotAvailableError):
                embed_query("test query")


# ---------------------------------------------------------------------------
# check_ollama
# ---------------------------------------------------------------------------

class TestCheckOllama:
    def test_does_not_raise_when_available(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            check_ollama()  # should not raise

    def test_raises_with_install_instructions_when_unavailable(self):
        import requests
        with patch("requests.get", side_effect=requests.ConnectionError()):
            with pytest.raises(OllamaNotAvailableError) as exc_info:
                check_ollama()
            assert "ollama" in str(exc_info.value).lower()

    def test_error_message_contains_install_hint(self):
        import requests
        with patch("requests.get", side_effect=requests.ConnectionError()):
            with pytest.raises(OllamaNotAvailableError) as exc_info:
                check_ollama()
            msg = str(exc_info.value).lower()
            assert "install" in msg or "ollama.com" in msg


# ---------------------------------------------------------------------------
# load_index
# ---------------------------------------------------------------------------

class TestLoadIndex:
    def test_loads_index_and_metadata(self, tmp_data_dir):
        index, metadata = load_index(
            str(tmp_data_dir / "index.faiss"),
            str(tmp_data_dir / "metadata.jsonl"),
        )
        assert index.ntotal == len(metadata)

    def test_index_is_faiss_index(self, tmp_data_dir):
        index, _ = load_index(
            str(tmp_data_dir / "index.faiss"),
            str(tmp_data_dir / "metadata.jsonl"),
        )
        assert isinstance(index, faiss.Index)

    def test_metadata_is_list_of_dicts(self, tmp_data_dir):
        _, metadata = load_index(
            str(tmp_data_dir / "index.faiss"),
            str(tmp_data_dir / "metadata.jsonl"),
        )
        assert isinstance(metadata, list)
        assert all(isinstance(m, dict) for m in metadata)

    def test_raises_file_not_found_for_missing_index(self, tmp_data_dir):
        with pytest.raises(FileNotFoundError):
            load_index(
                str(tmp_data_dir / "nonexistent.faiss"),
                str(tmp_data_dir / "metadata.jsonl"),
            )

    def test_raises_file_not_found_for_missing_metadata(self, tmp_data_dir):
        with pytest.raises(FileNotFoundError):
            load_index(
                str(tmp_data_dir / "index.faiss"),
                str(tmp_data_dir / "nonexistent.jsonl"),
            )

    def test_raises_alignment_error_on_count_mismatch(self, tmp_path):
        rng = np.random.default_rng(42)
        vecs = rng.standard_normal((5, 1024)).astype(np.float32)
        faiss.normalize_L2(vecs)
        index = faiss.IndexFlatIP(1024)
        index.add(vecs)
        faiss.write_index(index, str(tmp_path / "index.faiss"))
        # Write only 3 metadata rows, but index has 5
        with open(tmp_path / "metadata.jsonl", "w") as f:
            for i in range(3):
                f.write(json.dumps({"name": f"skill-{i}"}) + "\n")
        with pytest.raises(AlignmentError):
            load_index(str(tmp_path / "index.faiss"), str(tmp_path / "metadata.jsonl"))


# ---------------------------------------------------------------------------
# apply_filters
# ---------------------------------------------------------------------------

class TestApplyFilters:
    def test_no_filters_returns_all(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=[], sources=[], safety_only=False)
        assert len(result) == len(skills_for_search)

    def test_platform_filter_claude_code(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=["claude_code"], sources=[], safety_only=False)
        for skill in result:
            assert "claude_code" in skill["install_cmd"]

    def test_platform_filter_openclaw(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=["openclaw"], sources=[], safety_only=False)
        for skill in result:
            assert "openclaw" in skill["install_cmd"]

    def test_platform_filter_codex(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=["codex"], sources=[], safety_only=False)
        names = [s["name"] for s in result]
        assert "codex-helper" in names
        assert "k8s-deployer" not in names

    def test_multiple_platforms_are_ored(self, skills_for_search):
        result = apply_filters(
            skills_for_search,
            platforms=["claude_code", "openclaw"],
            sources=[],
            safety_only=False,
        )
        for skill in result:
            assert "claude_code" in skill["install_cmd"] or "openclaw" in skill["install_cmd"]

    def test_safety_only_excludes_flagged(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=[], sources=[], safety_only=True)
        for skill in result:
            assert skill["quality"]["safety_flag"] is False

    def test_safety_only_with_no_flagged_returns_all(self):
        skills = [
            {"install_cmd": {"claude_code": "/plugin install a"}, "source": ["skillsmp"], "quality": {"safety_flag": False}},
            {"install_cmd": {"claude_code": "/plugin install b"}, "source": ["skillsmp"], "quality": {"safety_flag": False}},
        ]
        result = apply_filters(skills, platforms=[], sources=[], safety_only=True)
        assert len(result) == 2

    def test_source_filter(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=[], sources=["clawhub"], safety_only=False)
        for skill in result:
            assert "clawhub" in skill["source"]

    def test_multiple_sources_are_ored(self, skills_for_search):
        result = apply_filters(
            skills_for_search,
            platforms=[],
            sources=["skillsmp", "clawhub"],
            safety_only=False,
        )
        for skill in result:
            assert "skillsmp" in skill["source"] or "clawhub" in skill["source"]

    def test_combined_platform_and_safety(self, skills_for_search):
        result = apply_filters(
            skills_for_search,
            platforms=["claude_code"],
            sources=[],
            safety_only=True,
        )
        for skill in result:
            assert "claude_code" in skill["install_cmd"]
            assert skill["quality"]["safety_flag"] is False

    def test_preserves_order(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=[], sources=[], safety_only=False)
        # Order must match input order
        result_names = [s["name"] for s in result]
        input_names = [s["name"] for s in skills_for_search]
        assert result_names == [n for n in input_names if n in result_names]

    def test_empty_input_returns_empty(self):
        assert apply_filters([], platforms=["claude_code"], sources=[], safety_only=True) == []

    def test_impossible_filter_returns_empty(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=["nonexistent_platform"], sources=[], safety_only=False)
        assert result == []


# ---------------------------------------------------------------------------
# search (end-to-end with tiny index)
# ---------------------------------------------------------------------------

class TestSearch:
    def _make_query_vec(self):
        rng = np.random.default_rng(0)
        v = rng.standard_normal(1024).astype(np.float32)
        v /= np.linalg.norm(v)
        return v

    def test_returns_list(self, tmp_data_dir):
        vec = self._make_query_vec()
        index, metadata = load_index(
            str(tmp_data_dir / "index.faiss"),
            str(tmp_data_dir / "metadata.jsonl"),
        )
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("deploy kubernetes", index, metadata, propose_n=2)
        assert isinstance(results, list)

    def test_returns_at_most_propose_n_times_3(self, tmp_data_dir):
        vec = self._make_query_vec()
        index, metadata = load_index(
            str(tmp_data_dir / "index.faiss"),
            str(tmp_data_dir / "metadata.jsonl"),
        )
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("deploy kubernetes", index, metadata, propose_n=1)
        assert len(results) <= 3  # propose_n * 3

    def test_results_have_sim_score(self, tmp_data_dir):
        vec = self._make_query_vec()
        index, metadata = load_index(
            str(tmp_data_dir / "index.faiss"),
            str(tmp_data_dir / "metadata.jsonl"),
        )
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("test", index, metadata)
        for r in results:
            assert "sim_score" in r
            assert 0.0 <= r["sim_score"] <= 1.0

    def test_results_have_name_and_description(self, tmp_data_dir):
        vec = self._make_query_vec()
        index, metadata = load_index(
            str(tmp_data_dir / "index.faiss"),
            str(tmp_data_dir / "metadata.jsonl"),
        )
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("test", index, metadata)
        for r in results:
            assert "name" in r
            assert "description" in r

    def test_platform_filter_applied(self, tmp_data_dir):
        vec = self._make_query_vec()
        index, metadata = load_index(
            str(tmp_data_dir / "index.faiss"),
            str(tmp_data_dir / "metadata.jsonl"),
        )
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("test", index, metadata, platforms=["claude_code"])
        for r in results:
            assert "claude_code" in r.get("install_cmd", {})

    def test_empty_results_when_filters_exclude_all(self, tmp_data_dir):
        vec = self._make_query_vec()
        index, metadata = load_index(
            str(tmp_data_dir / "index.faiss"),
            str(tmp_data_dir / "metadata.jsonl"),
        )
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("test", index, metadata, platforms=["nonexistent_platform"])
        assert results == []


# ---------------------------------------------------------------------------
# format_results
# ---------------------------------------------------------------------------

class TestFormatResults:
    def test_json_output_is_valid_json(self, skills_for_search):
        results = [{"sim_score": 0.9, **s} for s in skills_for_search[:2]]
        output = format_results(results, as_json=True)
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    def test_json_output_contains_required_fields(self, skill):
        results = [{"sim_score": 0.85, **skill}]
        output = format_results(results, as_json=True)
        parsed = json.loads(output)
        item = parsed[0]
        for field in ("sim_score", "name", "description", "repo_url"):
            assert field in item, f"Missing field: {field}"

    def test_human_readable_output_contains_names(self, skills_for_search):
        results = [{"sim_score": 0.9, **skills_for_search[0]}]
        output = format_results(results, as_json=False)
        assert skills_for_search[0]["name"] in output

    def test_empty_results_returns_valid_output(self):
        json_out = format_results([], as_json=True)
        assert json.loads(json_out) == []

    def test_safety_flag_visible_in_json(self, skill_flagged):
        results = [{"sim_score": 0.8, **skill_flagged}]
        output = format_results(results, as_json=True)
        parsed = json.loads(output)
        assert parsed[0]["safety_flag"] is True
