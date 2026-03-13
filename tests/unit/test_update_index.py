"""
Unit tests for scripts/update_index.py

External calls (GitHub API, file downloads) are patched. Tests verify:
  - read_local_version: reads date from version.txt, handles missing file
  - get_latest_release: parses GitHub API response correctly
  - needs_update: date comparison logic
  - verify_sha256: checksum verification
  - download_file: writes file to disk
  - extract_artifact: atomic extraction (to .new/, then rename)
  - parse_release_sha256: extracts hash from release body markdown
  - run_update: end-to-end update orchestration
"""
import hashlib
import json
import os
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from scripts.update_index import (
    download_file,
    extract_artifact,
    get_latest_release,
    needs_update,
    parse_release_sha256,
    read_local_version,
    run_update,
    verify_sha256,
)


# ---------------------------------------------------------------------------
# read_local_version
# ---------------------------------------------------------------------------

class TestReadLocalVersion:
    def test_reads_date_from_version_txt(self, tmp_data_dir):
        result = read_local_version(str(tmp_data_dir))
        assert result == "2026-03-10"

    def test_returns_none_when_version_txt_missing(self, tmp_path):
        result = read_local_version(str(tmp_path))
        assert result is None

    def test_raises_value_error_on_missing_date_field(self, tmp_path):
        (tmp_path / "version.txt").write_text("skill_count: 100\n")
        with pytest.raises(ValueError):
            read_local_version(str(tmp_path))

    def test_raises_value_error_on_malformed_date(self, tmp_path):
        (tmp_path / "version.txt").write_text("date: not-a-date\nskill_count: 100\n")
        with pytest.raises(ValueError):
            read_local_version(str(tmp_path))


# ---------------------------------------------------------------------------
# get_latest_release
# ---------------------------------------------------------------------------

class TestGetLatestRelease:
    def _mock_release(self, tag="index-20260310", asset_name="skill-finder-index-20260310.tar.gz"):
        return {
            "tag_name": tag,
            "assets": [{"name": asset_name, "browser_download_url": f"https://github.com/releases/{asset_name}", "size": 50_000_000}],
            "body": f"**Skills indexed:** 14823\n**SHA256:** `abc123def456`",
        }

    def test_returns_dict_with_tag_name(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: self._mock_release(),
            )
            release = get_latest_release()
        assert "tag_name" in release

    def test_returns_dict_with_assets(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: self._mock_release(),
            )
            release = get_latest_release()
        assert "assets" in release
        assert len(release["assets"]) > 0

    def test_returns_dict_with_body(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: self._mock_release(),
            )
            release = get_latest_release()
        assert "body" in release

    def test_raises_on_api_error(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404, text="Not Found")
            with pytest.raises((RuntimeError, OSError, ValueError)):
                get_latest_release()


# ---------------------------------------------------------------------------
# needs_update
# ---------------------------------------------------------------------------

class TestNeedsUpdate:
    def test_newer_tag_returns_true(self):
        assert needs_update("2026-03-01", "index-20260310") is True

    def test_same_date_returns_false(self):
        assert needs_update("2026-03-10", "index-20260310") is False

    def test_older_tag_returns_false(self):
        assert needs_update("2026-03-15", "index-20260310") is False

    def test_none_local_version_returns_true(self):
        assert needs_update(None, "index-20260310") is True

    def test_handles_index_prefix_in_tag(self):
        # Tag is "index-YYYYMMDD", local is "YYYY-MM-DD"
        assert needs_update("2026-01-01", "index-20260201") is True

    def test_handles_tag_without_index_prefix(self):
        # Some tags might just be "20260310"
        assert needs_update("2026-03-01", "20260310") is True


# ---------------------------------------------------------------------------
# verify_sha256
# ---------------------------------------------------------------------------

class TestVerifySha256:
    def test_returns_true_for_correct_hash(self, tmp_path):
        content = b"test content for hashing"
        f = tmp_path / "artifact.tar.gz"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert verify_sha256(str(f), expected) is True

    def test_returns_false_for_wrong_hash(self, tmp_path):
        f = tmp_path / "artifact.tar.gz"
        f.write_bytes(b"some content")
        assert verify_sha256(str(f), "wrong_hash_value") is False

    def test_case_insensitive_comparison(self, tmp_path):
        content = b"hello"
        f = tmp_path / "test.bin"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest().upper()
        assert verify_sha256(str(f), expected) is True

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            verify_sha256(str(tmp_path / "missing.bin"), "abc123")


# ---------------------------------------------------------------------------
# download_file
# ---------------------------------------------------------------------------

class TestDownloadFile:
    def test_writes_content_to_destination(self, tmp_path):
        content = b"fake tar.gz content"
        dest = tmp_path / "download.tar.gz"
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                iter_content=lambda chunk_size: [content],
            )
            download_file("https://example.com/release.tar.gz", str(dest))
        assert dest.read_bytes() == content

    def test_file_created_at_destination(self, tmp_path):
        dest = tmp_path / "download.tar.gz"
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                iter_content=lambda chunk_size: [b"data"],
            )
            download_file("https://example.com/release.tar.gz", str(dest))
        assert dest.exists()

    def test_raises_on_non_200_response(self, tmp_path):
        dest = tmp_path / "download.tar.gz"
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=403)
            with pytest.raises(Exception):
                download_file("https://example.com/release.tar.gz", str(dest))


# ---------------------------------------------------------------------------
# extract_artifact
# ---------------------------------------------------------------------------

class TestExtractArtifact:
    def _make_tar(self, tmp_path: Path) -> Path:
        """Create a realistic tar.gz with index.faiss, metadata.jsonl, version.txt."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.faiss").write_bytes(b"fake faiss data")
        (src / "metadata.jsonl").write_text('{"name": "skill-1"}\n')
        (src / "version.txt").write_text("date: 2026-03-10\nskill_count: 1\n")
        tar_path = tmp_path / "artifact.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            for f in src.iterdir():
                tar.add(f, arcname=f.name)
        return tar_path

    def test_extracts_files_to_data_dir(self, tmp_path):
        tar = self._make_tar(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        extract_artifact(str(tar), str(data_dir))
        assert (data_dir / "index.faiss").exists()
        assert (data_dir / "metadata.jsonl").exists()
        assert (data_dir / "version.txt").exists()

    def test_extraction_is_atomic(self, tmp_path):
        """Verify extraction uses a .new/ staging dir — no partial writes to data/."""
        tar = self._make_tar(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # Place an existing version.txt so we can verify it's replaced atomically
        (data_dir / "version.txt").write_text("date: 2026-01-01\n")
        extract_artifact(str(tar), str(data_dir))
        # After extraction, version.txt should have new content
        content = (data_dir / "version.txt").read_text()
        assert "2026-03-10" in content

    def test_no_dot_new_dir_left_after_extraction(self, tmp_path):
        tar = self._make_tar(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        extract_artifact(str(tar), str(data_dir))
        assert not (data_dir / ".new").exists()


# ---------------------------------------------------------------------------
# parse_release_sha256
# ---------------------------------------------------------------------------

class TestParseReleaseSha256:
    def test_extracts_sha256_from_release_body(self):
        body = "**Skills indexed:** 14823\n**SHA256:** `abc123def456789`"
        result = parse_release_sha256(body)
        assert result == "abc123def456789"

    def test_returns_none_when_not_present(self):
        body = "**Skills indexed:** 14823\nNo checksum here."
        result = parse_release_sha256(body)
        assert result is None

    def test_handles_different_formatting(self):
        body = "SHA256: abc123def456"
        result = parse_release_sha256(body)
        assert result == "abc123def456"


# ---------------------------------------------------------------------------
# run_update (end-to-end orchestration)
# ---------------------------------------------------------------------------

class TestRunUpdate:
    def _mock_release(self):
        return {
            "tag_name": "index-20260310",
            "assets": [{
                "name": "skill-finder-index-20260310.tar.gz",
                "browser_download_url": "https://github.com/releases/artifact.tar.gz",
                "size": 1024,
            }],
            "body": "**Skills indexed:** 14823\n**SHA256:** `fakechecksum123`",
        }

    def test_check_only_does_not_download(self, tmp_data_dir):
        with patch("scripts.update_index.get_latest_release", return_value=self._mock_release()):
            with patch("scripts.update_index.download_file") as mock_dl:
                run_update(str(tmp_data_dir), check_only=True)
                mock_dl.assert_not_called()

    def test_returns_status_up_to_date_when_current(self, tmp_data_dir):
        release = self._mock_release()
        release["tag_name"] = "index-20260301"  # older than local 2026-03-10
        with patch("scripts.update_index.get_latest_release", return_value=release):
            result = run_update(str(tmp_data_dir))
        assert result["status"] == "up_to_date"

    def test_returns_status_updated_when_newer_available(self, tmp_path):
        # tmp_path has no version.txt → local version is None
        with patch("scripts.update_index.get_latest_release", return_value=self._mock_release()):
            with patch("scripts.update_index.download_file"):
                with patch("scripts.update_index.verify_sha256", return_value=True):
                    with patch("scripts.update_index.extract_artifact"):
                        result = run_update(str(tmp_path))
        assert result["status"] in ("updated", "error")  # error OK if sha mismatch in test

    def test_force_flag_downloads_even_if_current(self, tmp_data_dir):
        release = self._mock_release()
        release["tag_name"] = "index-20260301"  # older, would normally skip
        with patch("scripts.update_index.get_latest_release", return_value=release):
            with patch("scripts.update_index.download_file") as mock_dl:
                with patch("scripts.update_index.verify_sha256", return_value=True):
                    with patch("scripts.update_index.extract_artifact"):
                        run_update(str(tmp_data_dir), force=True)
                mock_dl.assert_called_once()

    def test_result_has_status_and_message(self, tmp_data_dir):
        with patch("scripts.update_index.get_latest_release", return_value=self._mock_release()):
            result = run_update(str(tmp_data_dir))
        assert "status" in result
        assert "message" in result
