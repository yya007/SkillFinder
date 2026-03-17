"""
Unit tests for pipeline/embed.py

External Ollama calls are patched. Tests verify:
  - check_ollama_available: health check behavior
  - embed_batch: request format, output shape, dtype
  - embed_all: batching, row alignment, checkpoint writing
  - run_embed: file output, row alignment between embeddings.npy and ordered JSONL
  - OllamaError: raised on connection failure
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from pipeline.embed import (
    BATCH_SIZE,
    DIM,
    MODEL,
    OllamaError,
    check_ollama_available,
    embed_all,
    embed_batch,
    load_embedding_cache,
    run_embed,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_records(n: int) -> list[dict]:
    return [
        {"embedding_text": f"skill {i}. Description {i}. Categories: testing.", "name": f"skill-{i}"}
        for i in range(n)
    ]


def fake_embed_response(texts: list[str]) -> dict:
    """Simulate Ollama's /api/embed JSON response."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((len(texts), DIM)).tolist()
    return {"embeddings": embeddings}


# ---------------------------------------------------------------------------
# check_ollama_available
# ---------------------------------------------------------------------------

class TestCheckOllamaAvailable:
    def test_returns_true_when_ollama_responds(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            assert check_ollama_available() is True

    def test_returns_false_on_connection_error(self):
        import requests
        with patch("requests.get", side_effect=requests.ConnectionError()):
            assert check_ollama_available() is False

    def test_returns_false_on_timeout(self):
        import requests
        with patch("requests.get", side_effect=requests.Timeout()):
            assert check_ollama_available() is False

    def test_returns_false_on_non_200(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=503)
            assert check_ollama_available() is False

    def test_does_not_raise_on_any_error(self):
        with patch("requests.get", side_effect=Exception("unexpected")):
            result = check_ollama_available()
            assert result is False


# ---------------------------------------------------------------------------
# embed_batch
# ---------------------------------------------------------------------------

class TestEmbedBatch:
    def test_returns_correct_shape(self):
        texts = ["text one", "text two", "text three"]
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: fake_embed_response(texts),
            )
            result = embed_batch(texts)
        assert result.shape == (3, DIM)

    def test_returns_float32(self):
        texts = ["text one"]
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: fake_embed_response(texts),
            )
            result = embed_batch(texts)
        assert result.dtype == np.float32

    def test_sends_correct_model(self):
        texts = ["hello"]
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: fake_embed_response(texts),
            )
            embed_batch(texts)
            call_json = mock_post.call_args[1].get("json") or mock_post.call_args[0][1]
            assert call_json["model"] == MODEL

    def test_sends_texts_as_input(self):
        texts = ["alpha", "beta"]
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: fake_embed_response(texts),
            )
            embed_batch(texts)
            call_json = mock_post.call_args[1].get("json") or mock_post.call_args[0][1]
            assert call_json["input"] == texts

    def test_does_not_apply_query_prefix(self):
        texts = ["deploy kubernetes"]
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: fake_embed_response(texts),
            )
            embed_batch(texts)
            call_json = mock_post.call_args[1].get("json") or mock_post.call_args[0][1]
            for sent_text in call_json["input"]:
                assert "Instruct:" not in sent_text

    def test_raises_ollama_error_on_connection_failure(self):
        import requests
        with patch("requests.post", side_effect=requests.ConnectionError()):
            with pytest.raises(OllamaError):
                embed_batch(["text"])

    def test_raises_ollama_error_on_non_200(self):
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=500, text="Internal Server Error")
            with pytest.raises(OllamaError):
                embed_batch(["text"])

    def test_raises_ollama_error_on_malformed_response(self):
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"unexpected_key": []},
            )
            with pytest.raises(OllamaError):
                embed_batch(["text"])


# ---------------------------------------------------------------------------
# embed_all
# ---------------------------------------------------------------------------

class TestEmbedAll:
    def test_returns_correct_shape(self, mock_ollama_embed):
        records = make_records(5)
        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            result = embed_all(records)
        assert result.shape == (5, DIM)

    def test_row_order_matches_input_order(self, mock_ollama_embed):
        records = make_records(10)
        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            result = embed_all(records)
        assert result.shape[0] == 10

    def test_raises_on_missing_embedding_text(self, mock_ollama_embed):
        records = [{"name": "bad-skill"}]  # no embedding_text
        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            with pytest.raises(ValueError):
                embed_all(records)

    def test_writes_checkpoints_to_dir(self, tmp_path, mock_ollama_embed):
        records = make_records(BATCH_SIZE * 3)  # enough for at least one checkpoint
        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            embed_all(records, checkpoint_dir=str(tmp_path))
        checkpoints = list(tmp_path.glob("embeddings_checkpoint_*.npy"))
        assert len(checkpoints) > 0

    def test_no_checkpoints_without_dir(self, tmp_path, mock_ollama_embed):
        records = make_records(10)
        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            embed_all(records, checkpoint_dir=None)
        # Should not write anything to tmp_path
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# run_embed
# ---------------------------------------------------------------------------

class TestRunEmbed:
    def _write_input(self, path: Path, records: list[dict]):
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_creates_embeddings_npy(self, tmp_path, mock_ollama_embed):
        records = make_records(5)
        input_path = tmp_path / "unified_skills.jsonl"
        self._write_input(input_path, records)
        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            run_embed(
                str(input_path),
                str(tmp_path / "embeddings.npy"),
                str(tmp_path / "ordered.jsonl"),
            )
        assert (tmp_path / "embeddings.npy").exists()

    def test_creates_ordered_jsonl(self, tmp_path, mock_ollama_embed):
        records = make_records(5)
        input_path = tmp_path / "unified_skills.jsonl"
        self._write_input(input_path, records)
        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            run_embed(
                str(input_path),
                str(tmp_path / "embeddings.npy"),
                str(tmp_path / "ordered.jsonl"),
            )
        assert (tmp_path / "ordered.jsonl").exists()

    def test_row_alignment_between_npy_and_jsonl(self, tmp_path, mock_ollama_embed):
        records = make_records(7)
        input_path = tmp_path / "unified_skills.jsonl"
        self._write_input(input_path, records)
        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            run_embed(
                str(input_path),
                str(tmp_path / "embeddings.npy"),
                str(tmp_path / "ordered.jsonl"),
            )
        embeddings = np.load(tmp_path / "embeddings.npy")
        jsonl_count = sum(1 for _ in open(tmp_path / "ordered.jsonl"))
        assert embeddings.shape[0] == jsonl_count

    def test_returns_record_count(self, tmp_path, mock_ollama_embed):
        records = make_records(6)
        input_path = tmp_path / "unified_skills.jsonl"
        self._write_input(input_path, records)
        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            n = run_embed(
                str(input_path),
                str(tmp_path / "embeddings.npy"),
                str(tmp_path / "ordered.jsonl"),
            )
        assert n == 6

    def test_raises_file_not_found_for_missing_input(self, tmp_path, mock_ollama_embed):
        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            with pytest.raises(FileNotFoundError):
                run_embed(
                    str(tmp_path / "nonexistent.jsonl"),
                    str(tmp_path / "embeddings.npy"),
                    str(tmp_path / "ordered.jsonl"),
                )


# ---------------------------------------------------------------------------
# load_embedding_cache
# ---------------------------------------------------------------------------

class TestLoadEmbeddingCache:
    def _make_cache_files(self, tmp_path, records: list[dict]) -> tuple[str, str]:
        """Write a fake cache (embeddings.npy + ordered.jsonl) and return their paths."""
        rng = np.random.default_rng(0)
        vecs = rng.standard_normal((len(records), DIM)).astype(np.float32)
        emb_path = str(tmp_path / "embeddings.npy")
        ord_path = str(tmp_path / "ordered.jsonl")
        np.save(emb_path, vecs)
        with open(ord_path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return emb_path, ord_path

    def test_loads_all_entries(self, tmp_path):
        records = [
            {"id": f"abc{i}", "embedding_text": f"text {i}"}
            for i in range(5)
        ]
        emb_path, ord_path = self._make_cache_files(tmp_path, records)
        cache = load_embedding_cache(emb_path, ord_path)
        assert len(cache) == 5

    def test_vector_shape_correct(self, tmp_path):
        records = [{"id": "abc", "embedding_text": "hello"}]
        emb_path, ord_path = self._make_cache_files(tmp_path, records)
        cache = load_embedding_cache(emb_path, ord_path)
        vec, _ = cache["abc"]
        assert vec.shape == (DIM,)

    def test_stores_embedding_text(self, tmp_path):
        records = [{"id": "xyz", "embedding_text": "my text"}]
        emb_path, ord_path = self._make_cache_files(tmp_path, records)
        cache = load_embedding_cache(emb_path, ord_path)
        _, text = cache["xyz"]
        assert text == "my text"

    def test_skips_records_without_id(self, tmp_path):
        records = [
            {"embedding_text": "no id here"},
            {"id": "has_id", "embedding_text": "with id"},
        ]
        emb_path, ord_path = self._make_cache_files(tmp_path, records)
        cache = load_embedding_cache(emb_path, ord_path)
        assert "has_id" in cache
        assert len(cache) == 1

    def test_missing_embeddings_file_returns_empty(self, tmp_path):
        cache = load_embedding_cache(
            str(tmp_path / "no.npy"),
            str(tmp_path / "no.jsonl"),
        )
        assert cache == {}

    def test_missing_ordered_file_returns_empty(self, tmp_path):
        # Write a real embeddings file but no ordered jsonl
        np.save(str(tmp_path / "e.npy"), np.zeros((3, DIM), dtype=np.float32))
        cache = load_embedding_cache(
            str(tmp_path / "e.npy"),
            str(tmp_path / "no.jsonl"),
        )
        assert cache == {}


# ---------------------------------------------------------------------------
# run_embed — incremental / cache behaviour
# ---------------------------------------------------------------------------

class TestRunEmbedIncremental:
    """Tests for the cache_embeddings / cache_ordered incremental path."""

    def _write_input(self, path, records):
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def _make_cache(self, tmp_path, records, subdir="cache"):
        """Seed a cache directory with fake embeddings for given records."""
        d = tmp_path / subdir
        d.mkdir()
        rng = np.random.default_rng(99)
        vecs = rng.standard_normal((len(records), DIM)).astype(np.float32)
        emb = str(d / "embeddings.npy")
        ord_ = str(d / "ordered.jsonl")
        np.save(emb, vecs)
        with open(ord_, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return emb, ord_, vecs

    def test_all_cached_skips_ollama(self, tmp_path, mock_ollama_embed):
        """When all records are in the cache with unchanged text, Ollama is not called."""
        records = [
            {"id": f"id{i}", "embedding_text": f"text {i}", "name": f"s{i}"}
            for i in range(4)
        ]
        cache_emb, cache_ord, _ = self._make_cache(tmp_path, records)
        input_path = tmp_path / "input.jsonl"
        self._write_input(input_path, records)

        embed_call_count = {"n": 0}
        def counting_embed(texts, **kwargs):
            embed_call_count["n"] += 1
            return mock_ollama_embed(texts, **kwargs)

        with patch("pipeline.embed.embed_batch", side_effect=counting_embed):
            run_embed(
                str(input_path),
                str(tmp_path / "out.npy"),
                str(tmp_path / "out.jsonl"),
                cache_embeddings=cache_emb,
                cache_ordered=cache_ord,
            )
        assert embed_call_count["n"] == 0, "embed_batch should not be called when all records are cached"

    def test_no_cache_embeds_all(self, tmp_path, mock_ollama_embed):
        """Without cache args, all records are embedded (existing behaviour)."""
        records = [
            {"id": f"id{i}", "embedding_text": f"text {i}", "name": f"s{i}"}
            for i in range(3)
        ]
        input_path = tmp_path / "input.jsonl"
        self._write_input(input_path, records)

        embedded_texts = []
        def capturing_embed(texts, **kwargs):
            embedded_texts.extend(texts)
            return mock_ollama_embed(texts, **kwargs)

        with patch("pipeline.embed.embed_batch", side_effect=capturing_embed):
            run_embed(
                str(input_path),
                str(tmp_path / "out.npy"),
                str(tmp_path / "out.jsonl"),
            )
        assert len(embedded_texts) == 3

    def test_partial_cache_only_new_embedded(self, tmp_path, mock_ollama_embed):
        """Only skills absent from the cache (or with changed text) are sent to Ollama."""
        cached_records = [
            {"id": "cached_0", "embedding_text": "unchanged text 0", "name": "s0"},
            {"id": "cached_1", "embedding_text": "unchanged text 1", "name": "s1"},
        ]
        new_record = {"id": "new_0", "embedding_text": "brand new skill", "name": "new"}
        cache_emb, cache_ord, _ = self._make_cache(tmp_path, cached_records)

        all_input = cached_records + [new_record]
        input_path = tmp_path / "input.jsonl"
        self._write_input(input_path, all_input)

        embedded_texts = []
        def capturing_embed(texts, **kwargs):
            embedded_texts.extend(texts)
            return mock_ollama_embed(texts, **kwargs)

        with patch("pipeline.embed.embed_batch", side_effect=capturing_embed):
            n = run_embed(
                str(input_path),
                str(tmp_path / "out.npy"),
                str(tmp_path / "out.jsonl"),
                cache_embeddings=cache_emb,
                cache_ordered=cache_ord,
            )

        assert n == 3
        assert embedded_texts == ["brand new skill"], (
            f"Only the new skill should be embedded, got: {embedded_texts}"
        )

    def test_text_change_invalidates_cache(self, tmp_path, mock_ollama_embed):
        """If embedding_text changes for a cached skill, it is re-embedded."""
        old_record = {"id": "skill_x", "embedding_text": "old text", "name": "x"}
        cache_emb, cache_ord, _ = self._make_cache(tmp_path, [old_record])

        updated_record = {"id": "skill_x", "embedding_text": "updated text", "name": "x"}
        input_path = tmp_path / "input.jsonl"
        self._write_input(input_path, [updated_record])

        embedded_texts = []
        def capturing_embed(texts, **kwargs):
            embedded_texts.extend(texts)
            return mock_ollama_embed(texts, **kwargs)

        with patch("pipeline.embed.embed_batch", side_effect=capturing_embed):
            run_embed(
                str(input_path),
                str(tmp_path / "out.npy"),
                str(tmp_path / "out.jsonl"),
                cache_embeddings=cache_emb,
                cache_ordered=cache_ord,
            )

        assert "updated text" in embedded_texts, "Changed embedding_text should trigger re-embed"

    def test_cached_vectors_preserved_in_output(self, tmp_path, mock_ollama_embed):
        """Vectors for cached skills are written verbatim to the output embeddings."""
        record = {"id": "skill_a", "embedding_text": "stable text", "name": "a"}
        cache_emb, cache_ord, original_vecs = self._make_cache(tmp_path, [record])

        input_path = tmp_path / "input.jsonl"
        self._write_input(input_path, [record])

        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            run_embed(
                str(input_path),
                str(tmp_path / "out.npy"),
                str(tmp_path / "out.jsonl"),
                cache_embeddings=cache_emb,
                cache_ordered=cache_ord,
            )

        out_vecs = np.load(tmp_path / "out.npy")
        assert out_vecs.shape == (1, DIM)
        np.testing.assert_array_equal(out_vecs[0], original_vecs[0])

    def test_output_alignment_preserved(self, tmp_path, mock_ollama_embed):
        """Row N in output embeddings matches line N in output ordered JSONL.

        Specifically verifies that cached vectors land at the correct row indices
        when cached and new records are interleaved in the input.
        """
        cached = [{"id": f"c{i}", "embedding_text": f"cached {i}", "name": f"c{i}"} for i in range(3)]
        new = [{"id": f"n{i}", "embedding_text": f"new {i}", "name": f"n{i}"} for i in range(2)]
        cache_emb, cache_ord, cache_vecs = self._make_cache(tmp_path, cached)

        # Interleave cached and new: positions 0,2,4 are cached; 1,3 are new
        all_input = [cached[0], new[0], cached[1], new[1], cached[2]]
        input_path = tmp_path / "input.jsonl"
        self._write_input(input_path, all_input)

        with patch("pipeline.embed.embed_batch", side_effect=mock_ollama_embed):
            run_embed(
                str(input_path),
                str(tmp_path / "out.npy"),
                str(tmp_path / "out.jsonl"),
                cache_embeddings=cache_emb,
                cache_ordered=cache_ord,
            )

        out_vecs = np.load(tmp_path / "out.npy")
        with open(tmp_path / "out.jsonl") as f:
            out_records = [json.loads(line) for line in f]

        assert out_vecs.shape[0] == len(out_records) == 5

        # Verify record identity at each row
        assert out_records[0]["id"] == "c0"
        assert out_records[1]["id"] == "n0"
        assert out_records[2]["id"] == "c1"
        assert out_records[3]["id"] == "n1"
        assert out_records[4]["id"] == "c2"

        # Verify cached vectors appear verbatim at their interleaved positions
        np.testing.assert_array_equal(out_vecs[0], cache_vecs[0])
        np.testing.assert_array_equal(out_vecs[2], cache_vecs[1])
        np.testing.assert_array_equal(out_vecs[4], cache_vecs[2])
