"""
Unit tests for scripts/search.py

Ollama calls are patched throughout. Tests verify:
  - embed_query: applies QUERY_PREFIX, returns L2-normalized float32 vector
  - ensure_ollama: raises OllamaNotAvailableError when unreachable
  - load_index: loads index + metadata, validates alignment
  - apply_filters: platform, source filtering (all combinations)
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
    ensure_ollama,
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
            assert sent_text.startswith(QUERY_PREFIX), (
                f"Expected text to start with QUERY_PREFIX.\n"
                f"  QUERY_PREFIX: {QUERY_PREFIX!r}\n"
                f"  Sent text:    {sent_text!r}"
            )

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
# ensure_ollama
# ---------------------------------------------------------------------------

class TestEnsureOllama:
    def test_returns_none_when_already_running(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            result = ensure_ollama()  # should not raise
            assert result is None  # already running, did not start a new process

    def test_raises_with_install_instructions_when_ollama_missing(self):
        with patch("requests.get", side_effect=Exception("connection refused")):
            with patch("subprocess.Popen", side_effect=FileNotFoundError()):
                with pytest.raises(OllamaNotAvailableError) as exc_info:
                    ensure_ollama()
                assert "ollama" in str(exc_info.value).lower()

    def test_error_message_contains_install_hint(self):
        with patch("requests.get", side_effect=Exception("connection refused")):
            with patch("subprocess.Popen", side_effect=FileNotFoundError()):
                with pytest.raises(OllamaNotAvailableError) as exc_info:
                    ensure_ollama()
                msg = str(exc_info.value).lower()
                assert "install" in msg or "ollama.com" in msg

    def test_ensure_ollama_returns_popen_when_started(self):
        """Ollama not running → starts it, returns the Popen object."""
        import subprocess as _subprocess
        mock_proc = MagicMock(spec=_subprocess.Popen)
        # First call to requests.get (is_ollama_running check) raises → not running
        # Second call (inside the wait loop) succeeds → Ollama ready
        call_count = {"n": 0}

        def mock_get(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("connection refused")
            return MagicMock(status_code=200)

        with patch("requests.get", side_effect=mock_get):
            with patch("subprocess.Popen", return_value=mock_proc):
                result = ensure_ollama()

        assert result is mock_proc


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
        result = apply_filters(skills_for_search, platforms=[], sources=[])
        assert len(result) == len(skills_for_search)

    def test_platform_filter_claude_code(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=["claude_code"], sources=[])
        for skill in result:
            assert "claude_code" in skill["install_cmd"]

    def test_platform_filter_openclaw(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=["openclaw"], sources=[])
        for skill in result:
            assert "openclaw" in skill["install_cmd"]

    def test_platform_filter_codex(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=["codex"], sources=[])
        names = [s["name"] for s in result]
        assert "codex-helper" in names
        assert "k8s-deployer" not in names

    def test_multiple_platforms_are_ored(self, skills_for_search):
        result = apply_filters(
            skills_for_search,
            platforms=["claude_code", "openclaw"],
            sources=[],
        )
        for skill in result:
            assert "claude_code" in skill["install_cmd"] or "openclaw" in skill["install_cmd"]

    def test_source_filter(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=[], sources=["clawhub"])
        for skill in result:
            assert "clawhub" in skill["source"]

    def test_multiple_sources_are_ored(self, skills_for_search):
        result = apply_filters(
            skills_for_search,
            platforms=[],
            sources=["skillsmp", "clawhub"],
        )
        for skill in result:
            assert "skillsmp" in skill["source"] or "clawhub" in skill["source"]

    def test_preserves_order(self, skills_for_search):
        # Filter to a strict subset so ordering is non-trivial to verify
        result = apply_filters(
            skills_for_search, platforms=["claude_code"], sources=[],         )
        result_names = [s["name"] for s in result]
        # Build expected: input_names filtered to only those in results (original order)
        result_set = set(result_names)
        expected_order = [s["name"] for s in skills_for_search if s["name"] in result_set]
        assert result_names == expected_order

    def test_empty_input_returns_empty(self):
        assert apply_filters([], platforms=["claude_code"], sources=[]) == []

    def test_impossible_filter_returns_empty(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=["nonexistent_platform"], sources=[])
        assert result == []

    def test_platform_filter_uses_platforms_field_not_install_cmd(self):
        """Skills with platforms=["claude_code"] but install_cmd={} must still match."""
        skillhub_skill = {
            "name": "skillhub-tool",
            "description": "A SkillHub metadata-only skill.",
            "source": ["skillhub"],
            "platforms": ["claude_code"],
            "install_cmd": {},  # SkillHub records always have empty install_cmd
            "quality": {"stars": 50},
        }
        result = apply_filters([skillhub_skill], platforms=["claude_code"], sources=[])
        assert len(result) == 1, "Skills with platforms field but no install_cmd should match"

    def test_min_stars_zero_returns_all(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=[], sources=[], min_stars=0)
        assert len(result) == len(skills_for_search)

    def test_min_stars_at_threshold_returns_all(self, skills_for_search):
        # All fixture skills have stars=10 by default
        result = apply_filters(skills_for_search, platforms=[], sources=[], min_stars=10)
        assert len(result) == len(skills_for_search)

    def test_min_stars_above_threshold_excludes_all(self, skills_for_search):
        result = apply_filters(skills_for_search, platforms=[], sources=[], min_stars=11)
        assert result == []

    def test_min_stars_partial_filter(self):
        high_star = {
            "name": "popular",
            "platforms": [],
            "source": [],
            "quality": {"stars": 100},
        }
        low_star = {
            "name": "obscure",
            "platforms": [],
            "source": [],
            "quality": {"stars": 5},
        }
        result = apply_filters([high_star, low_star], platforms=[], sources=[], min_stars=50)
        assert len(result) == 1
        assert result[0]["name"] == "popular"

    def test_min_stars_combined_with_platform(self, skills_for_search):
        # claude_code skills: k8s-deployer (stars=10), flagged-tool (stars=10)
        result = apply_filters(
            skills_for_search, platforms=["claude_code"], sources=[], min_stars=10
        )
        assert all("claude_code" in s.get("platforms", []) for s in result)
        assert all(s.get("quality", {}).get("stars", 0) >= 10 for s in result)

    def test_safety_only_filter_excludes_unscanned(self):
        safe = {"name": "safe", "platforms": [], "source": [], "quality": {}, "safety_scan": True}
        unsafe = {"name": "unsafe", "platforms": [], "source": [], "quality": {}}
        result = apply_filters([safe, unsafe], platforms=[], sources=[], safety_only=True)
        assert len(result) == 1
        assert result[0]["name"] == "safe"

    def test_safety_only_passes_scanned_skills(self):
        safe = {"name": "safe", "platforms": [], "source": [], "quality": {}, "safety_scan": True}
        result = apply_filters([safe], platforms=[], sources=[], safety_only=True)
        assert len(result) == 1

    def test_safety_only_false_returns_all(self):
        safe = {"name": "safe", "platforms": [], "source": [], "quality": {}, "safety_scan": True}
        unsafe = {"name": "unsafe", "platforms": [], "source": [], "quality": {}}
        result = apply_filters([safe, unsafe], platforms=[], sources=[], safety_only=False)
        assert len(result) == 2


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

    @pytest.mark.parametrize("propose_n", [1, 2, 3])
    def test_result_count_does_not_exceed_propose_n_times_3(self, tmp_data_dir, propose_n):
        vec = self._make_query_vec()
        index, metadata = load_index(
            str(tmp_data_dir / "index.faiss"),
            str(tmp_data_dir / "metadata.jsonl"),
        )
        with patch("scripts.search.embed_query", return_value=vec):
            results = search("test", index, metadata, propose_n=propose_n)
        assert len(results) <= propose_n * 3

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
        assert "safety_notice" in parsed
        assert isinstance(parsed["results"], list)
        assert len(parsed["results"]) == 2

    def test_json_output_contains_required_fields(self, skill):
        results = [{"sim_score": 0.85, **skill}]
        output = format_results(results, as_json=True)
        parsed = json.loads(output)
        item = parsed["results"][0]
        for field in ("sim_score", "name", "description", "repo_url"):
            assert field in item, f"Missing field: {field}"

    def test_json_output_contains_safety_notice(self, skill):
        results = [{"sim_score": 0.85, **skill}]
        output = format_results(results, as_json=True)
        parsed = json.loads(output)
        assert "safety_notice" in parsed
        assert "third-party" in parsed["safety_notice"].lower()

    def test_human_readable_output_contains_safety_notice(self, skill):
        results = [{"sim_score": 0.85, **skill}]
        output = format_results(results, as_json=False)
        assert "third-party" in output.lower()

    def test_human_readable_output_contains_names(self, skills_for_search):
        results = [{"sim_score": 0.9, **skills_for_search[0]}]
        output = format_results(results, as_json=False)
        assert skills_for_search[0]["name"] in output

    def test_human_readable_output_no_score(self, skills_for_search):
        results = [{"sim_score": 0.9, **skills_for_search[0]}]
        output = format_results(results, as_json=False)
        assert "(score:" not in output

    def test_human_readable_shows_stars(self):
        skill = {
            "sim_score": 0.9,
            "name": "popular-skill",
            "description": "Does something useful.",
            "repo_url": "https://github.com/user/popular-skill",
            "platforms": ["claude_code"],
            "install_cmd": {"claude_code": "/plugin install popular-skill"},
            "quality": {"stars": 1234},
        }
        output = format_results([skill], as_json=False)
        assert "⭐" in output
        assert "1,234" in output

    def test_empty_results_returns_valid_output(self):
        json_out = format_results([], as_json=True)
        parsed = json.loads(json_out)
        assert parsed["results"] == []
        assert "safety_notice" in parsed
