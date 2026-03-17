"""Tests for pipeline/backfill_metadata.py."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.backfill_metadata import _repo_full_name, backfill_file


# ---------------------------------------------------------------------------
# _repo_full_name
# ---------------------------------------------------------------------------

class TestRepoFullName:
    def test_standard_github_url(self):
        assert _repo_full_name("https://github.com/user/repo") == "user/repo"

    def test_trailing_slash(self):
        assert _repo_full_name("https://github.com/user/repo/") == "user/repo"

    def test_non_github_url(self):
        assert _repo_full_name("https://gitlab.com/user/repo") is None

    def test_github_url_with_subpath(self):
        # Extra path segments are ignored — only owner/repo extracted
        result = _repo_full_name("https://github.com/user/repo/blob/main/SKILL.md")
        assert result == "user/repo"

    def test_github_url_missing_repo(self):
        assert _repo_full_name("https://github.com/user") is None

    def test_empty_string(self):
        assert _repo_full_name("") is None


# ---------------------------------------------------------------------------
# backfill_file
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


class TestBackfillFile:
    def test_skips_when_all_records_have_stars(self, tmp_path):
        p = tmp_path / "skills.jsonl"
        records = [
            {"repo_url": "https://github.com/u/r", "raw_metadata": {"stars": 42}},
        ]
        _write_jsonl(p, records)
        session = MagicMock()
        total, updated = backfill_file(str(p), session)
        assert total == 1
        assert updated == 0
        session.get.assert_not_called()

    def test_fetches_and_patches_missing_stars(self, tmp_path):
        p = tmp_path / "skills.jsonl"
        records = [
            {"repo_url": "https://github.com/u/myrepo", "raw_metadata": {"stars": None}},
        ]
        _write_jsonl(p, records)

        mock_meta = {"stargazers_count": 99, "pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main"}
        with patch("pipeline.backfill_metadata.fetch_repo_metadata", return_value=mock_meta):
            session = MagicMock()
            total, updated = backfill_file(str(p), session)

        assert total == 1
        assert updated == 1
        out = _read_jsonl(p)
        assert out[0]["raw_metadata"]["stars"] == 99
        assert out[0]["raw_metadata"]["pushed_at"] == "2026-01-01T00:00:00Z"

    def test_deduplicates_api_calls_for_same_repo(self, tmp_path):
        p = tmp_path / "skills.jsonl"
        records = [
            {"repo_url": "https://github.com/u/shared", "raw_metadata": {"stars": None}},
            {"repo_url": "https://github.com/u/shared", "raw_metadata": {"stars": None}},
            {"repo_url": "https://github.com/u/shared", "raw_metadata": {"stars": None}},
        ]
        _write_jsonl(p, records)

        mock_meta = {"stargazers_count": 10, "pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main"}
        call_count = 0

        def fake_fetch(session, full_name):
            nonlocal call_count
            call_count += 1
            return mock_meta

        with patch("pipeline.backfill_metadata.fetch_repo_metadata", side_effect=fake_fetch):
            session = MagicMock()
            total, updated = backfill_file(str(p), session)

        assert call_count == 1  # Only one API call for three records
        assert updated == 3

    def test_skips_non_github_repos(self, tmp_path):
        p = tmp_path / "skills.jsonl"
        records = [
            {"repo_url": "https://gitlab.com/u/repo", "raw_metadata": {"stars": None}},
        ]
        _write_jsonl(p, records)

        with patch("pipeline.backfill_metadata.fetch_repo_metadata") as mock_fetch:
            session = MagicMock()
            total, updated = backfill_file(str(p), session)

        mock_fetch.assert_not_called()
        assert updated == 0

    def test_dry_run_does_not_write_file(self, tmp_path):
        p = tmp_path / "skills.jsonl"
        records = [
            {"repo_url": "https://github.com/u/r", "raw_metadata": {"stars": None}},
        ]
        _write_jsonl(p, records)
        original_mtime = p.stat().st_mtime

        mock_meta = {"stargazers_count": 50, "pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main"}
        with patch("pipeline.backfill_metadata.fetch_repo_metadata", return_value=mock_meta):
            session = MagicMock()
            total, updated = backfill_file(str(p), session, dry_run=True)

        assert updated == 1
        # File should not be modified in dry run
        assert p.stat().st_mtime == original_mtime
        # Stars should still be None in the file
        out = _read_jsonl(p)
        assert out[0]["raw_metadata"]["stars"] is None

    def test_handles_failed_api_call(self, tmp_path):
        p = tmp_path / "skills.jsonl"
        records = [
            {"repo_url": "https://github.com/u/r", "raw_metadata": {"stars": None}},
        ]
        _write_jsonl(p, records)

        with patch("pipeline.backfill_metadata.fetch_repo_metadata", return_value=None):
            session = MagicMock()
            total, updated = backfill_file(str(p), session)

        assert updated == 0
        # File still written but record unchanged
        out = _read_jsonl(p)
        assert out[0]["raw_metadata"]["stars"] is None

    def test_writes_atomically_via_tmp_file(self, tmp_path):
        """Verify no .tmp file remains after successful write."""
        p = tmp_path / "skills.jsonl"
        records = [
            {"repo_url": "https://github.com/u/r", "raw_metadata": {"stars": None}},
        ]
        _write_jsonl(p, records)

        mock_meta = {"stargazers_count": 5, "pushed_at": "", "default_branch": "main"}
        with patch("pipeline.backfill_metadata.fetch_repo_metadata", return_value=mock_meta):
            backfill_file(str(p), MagicMock())

        tmp = tmp_path / "skills.jsonl.tmp"
        assert not tmp.exists()
        assert p.exists()

    def test_does_not_overwrite_existing_default_branch(self, tmp_path):
        """If raw_metadata already has default_branch, don't clobber it."""
        p = tmp_path / "skills.jsonl"
        records = [
            {"repo_url": "https://github.com/u/r",
             "raw_metadata": {"stars": None, "default_branch": "develop"}},
        ]
        _write_jsonl(p, records)

        mock_meta = {"stargazers_count": 5, "pushed_at": "", "default_branch": "main"}
        with patch("pipeline.backfill_metadata.fetch_repo_metadata", return_value=mock_meta):
            backfill_file(str(p), MagicMock())

        out = _read_jsonl(p)
        assert out[0]["raw_metadata"]["default_branch"] == "develop"


# ---------------------------------------------------------------------------
# TestBackfillFileZeroStars
# ---------------------------------------------------------------------------

class TestBackfillFileZeroStars:
    """Tests for the not-stars condition that now backfills 0-star records."""

    def test_backfills_zero_star_records(self, tmp_path):
        """Record with stars=0 should be backfilled (0 is falsy, so not stars is True)."""
        p = tmp_path / "skills.jsonl"
        records = [
            {"repo_url": "https://github.com/u/zerorepo", "raw_metadata": {"stars": 0}},
        ]
        _write_jsonl(p, records)

        mock_meta = {"stargazers_count": 42, "pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main"}
        with patch("pipeline.backfill_metadata.fetch_repo_metadata", return_value=mock_meta):
            session = MagicMock()
            total, updated = backfill_file(str(p), session)

        assert total == 1
        assert updated == 1
        out = _read_jsonl(p)
        assert out[0]["raw_metadata"]["stars"] == 42

    def test_still_skips_nonzero_stars(self, tmp_path):
        """Record with stars=5 should NOT be backfilled."""
        p = tmp_path / "skills.jsonl"
        records = [
            {"repo_url": "https://github.com/u/r", "raw_metadata": {"stars": 5}},
        ]
        _write_jsonl(p, records)

        with patch("pipeline.backfill_metadata.fetch_repo_metadata") as mock_fetch:
            session = MagicMock()
            total, updated = backfill_file(str(p), session)

        mock_fetch.assert_not_called()
        assert total == 1
        assert updated == 0


# ---------------------------------------------------------------------------
# TestBackfillDescriptions
# ---------------------------------------------------------------------------

import base64

from pipeline.backfill_metadata import backfill_descriptions


class TestBackfillDescriptions:
    """Tests for backfill_descriptions function."""

    def _write_jsonl(self, path: Path, records: list[dict]) -> None:
        with path.open("w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict]:
        with path.open() as f:
            return [json.loads(line) for line in f if line.strip()]

    def _make_record(self, description="", stars=10, skill_md_url="https://github.com/user/repo/blob/main/SKILL.md"):
        return {
            "repo_url": "https://github.com/user/repo",
            "description": description,
            "name": "foo",
            "raw_metadata": {
                "skill_md_url": skill_md_url,
                "stars": stars,
            },
        }

    def _b64(self, content: str) -> str:
        return base64.b64encode(content.encode()).decode()

    def test_skips_when_all_have_descriptions(self, tmp_path):
        """All records already have description -> returns (total, 0), no API call."""
        p = tmp_path / "skills.jsonl"
        records = [
            self._make_record(description="A fine skill."),
            self._make_record(description="Another skill."),
        ]
        self._write_jsonl(p, records)

        with patch("crawlers.base.github_get") as mock_gh:
            session = MagicMock()
            total, updated = backfill_descriptions(str(p), session)

        assert total == 2
        assert updated == 0
        mock_gh.assert_not_called()

    def test_updates_description_from_skill_md(self, tmp_path):
        """Record with no description and skill_md_url present -> fetches SKILL.md and updates."""
        p = tmp_path / "skills.jsonl"
        skill_md_content = "---\nname: foo\ndescription: Great skill.\n---\n"
        encoded = self._b64(skill_md_content)
        records = [self._make_record(description="")]
        self._write_jsonl(p, records)

        fake_response = {"type": "file", "content": encoded}
        with patch("crawlers.base.github_get", return_value=fake_response):
            session = MagicMock()
            total, updated = backfill_descriptions(str(p), session)

        assert total == 1
        assert updated == 1
        out = self._read_jsonl(p)
        assert out[0]["description"] == "Great skill."

    def test_falls_back_to_github_description(self, tmp_path):
        """SKILL.md has no description in frontmatter -> fallback to GitHub repo description."""
        p = tmp_path / "skills.jsonl"
        # SKILL.md with frontmatter but no description field
        skill_md_content = "---\nname: foo\n---\n"
        encoded = self._b64(skill_md_content)
        records = [self._make_record(description="")]
        self._write_jsonl(p, records)

        fake_response = {"type": "file", "content": encoded}
        # The fallback uses `from crawlers.base import fetch_repo_metadata` inside the function,
        # so we must patch the name on crawlers.base, not on pipeline.backfill_metadata.
        with patch("crawlers.base.github_get", return_value=fake_response), \
             patch("crawlers.base.fetch_repo_metadata", return_value={"description": "Fallback desc"}):
            session = MagicMock()
            total, updated = backfill_descriptions(str(p), session)

        assert total == 1
        assert updated == 1
        out = self._read_jsonl(p)
        assert out[0]["description"] == "Fallback desc"

    def test_dry_run_does_not_write(self, tmp_path):
        """dry_run=True: description would be updated but file stays unchanged."""
        p = tmp_path / "skills.jsonl"
        skill_md_content = "---\nname: foo\ndescription: Great skill.\n---\n"
        encoded = self._b64(skill_md_content)
        records = [self._make_record(description="")]
        self._write_jsonl(p, records)
        original_mtime = p.stat().st_mtime

        fake_response = {"type": "file", "content": encoded}
        with patch("crawlers.base.github_get", return_value=fake_response):
            session = MagicMock()
            total, updated = backfill_descriptions(str(p), session, dry_run=True)

        assert updated == 1
        # File should not be modified
        assert p.stat().st_mtime == original_mtime
        out = self._read_jsonl(p)
        assert out[0]["description"] == ""

    def test_skips_record_without_skill_md_url(self, tmp_path):
        """Record with no skill_md_url and no description -> nothing updated, no API called."""
        p = tmp_path / "skills.jsonl"
        records = [
            {
                "repo_url": "https://github.com/user/repo",
                "description": "",
                "name": "foo",
                "raw_metadata": {"stars": 5},  # No skill_md_url
            }
        ]
        self._write_jsonl(p, records)

        with patch("crawlers.base.github_get") as mock_gh:
            session = MagicMock()
            total, updated = backfill_descriptions(str(p), session)

        assert total == 1
        assert updated == 0
        mock_gh.assert_not_called()
