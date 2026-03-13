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
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from pipeline.embed import (
    BATCH_SIZE,
    DIM,
    MODEL,
    OLLAMA_URL,
    OllamaError,
    check_ollama_available,
    embed_all,
    embed_batch,
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
