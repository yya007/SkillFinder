"""Tests for crawlers/base.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from crawlers.base import extract_github_url, write_jsonl, load_existing_urls


# ---------------------------------------------------------------------------
# TestExtractGithubUrl
# ---------------------------------------------------------------------------

class TestExtractGithubUrl:
    def test_returns_none_for_non_github_url(self):
        assert extract_github_url("https://gitlab.com/user/repo") is None

    def test_strips_git_suffix(self):
        result = extract_github_url("https://github.com/user/repo.git")
        assert result == "https://github.com/user/repo"

    def test_strips_trailing_slash(self):
        result = extract_github_url("https://github.com/user/repo/")
        assert result == "https://github.com/user/repo"

    def test_strips_tree_path_segment(self):
        result = extract_github_url("https://github.com/user/repo/tree/main/subdir")
        assert result == "https://github.com/user/repo"

    def test_strips_blob_path_segment(self):
        result = extract_github_url("https://github.com/user/repo/blob/main/README.md")
        assert result == "https://github.com/user/repo"

    def test_lowercases_result(self):
        result = extract_github_url("https://GitHub.COM/User/MyRepo")
        assert result == "https://github.com/user/myrepo"

    def test_returns_none_for_empty_string(self):
        assert extract_github_url("") is None

    def test_handles_github_com_with_raw_subdomain(self):
        # raw.githubusercontent.com is NOT a repo URL
        assert extract_github_url("https://raw.githubusercontent.com/user/repo/main/SKILL.md") is None

    def test_preserves_owner_and_repo(self):
        result = extract_github_url("https://github.com/anthropics/skills")
        assert result == "https://github.com/anthropics/skills"

    def test_returns_none_for_github_root(self):
        # No owner/repo path segments
        assert extract_github_url("https://github.com/") is None

    def test_returns_none_for_github_owner_only(self):
        # Only one path segment (owner), no repo
        assert extract_github_url("https://github.com/user") is None

    def test_handles_git_suffix_on_repo_with_tree(self):
        # .git suffix combined with tree path
        result = extract_github_url("https://github.com/user/repo.git/tree/main")
        assert result == "https://github.com/user/repo"


# ---------------------------------------------------------------------------
# TestWriteJsonl
# ---------------------------------------------------------------------------

class TestWriteJsonl:
    def test_writes_records_to_file(self, tmp_path):
        records = [{"a": 1}, {"b": 2}]
        out = str(tmp_path / "out.jsonl")
        write_jsonl(records, out)
        lines = Path(out).read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"a": 1}
        assert json.loads(lines[1]) == {"b": 2}

    def test_returns_count(self, tmp_path):
        records = [{"x": i} for i in range(5)]
        out = str(tmp_path / "out.jsonl")
        count = write_jsonl(records, out)
        assert count == 5

    def test_append_mode_adds_to_existing(self, tmp_path):
        out = str(tmp_path / "out.jsonl")
        write_jsonl([{"first": True}], out)
        write_jsonl([{"second": True}], out, append=True)
        lines = Path(out).read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"first": True}
        assert json.loads(lines[1]) == {"second": True}

    def test_creates_file_if_not_exists(self, tmp_path):
        out = str(tmp_path / "nested" / "dir" / "out.jsonl")
        write_jsonl([{"key": "value"}], out)
        assert Path(out).exists()

    def test_overwrite_mode_replaces_existing(self, tmp_path):
        out = str(tmp_path / "out.jsonl")
        write_jsonl([{"old": True}], out)
        write_jsonl([{"new": True}], out, append=False)
        lines = Path(out).read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"new": True}

    def test_empty_records_writes_empty_file(self, tmp_path):
        out = str(tmp_path / "out.jsonl")
        count = write_jsonl([], out)
        assert count == 0
        assert Path(out).read_text() == ""


# ---------------------------------------------------------------------------
# TestLoadExistingUrls
# ---------------------------------------------------------------------------

class TestLoadExistingUrls:
    def test_loads_repo_urls_from_file(self, tmp_path):
        out = tmp_path / "out.jsonl"
        records = [
            {"repo_url": "https://github.com/user/repo-a", "name": "a"},
            {"repo_url": "https://github.com/user/repo-b", "name": "b"},
        ]
        out.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        urls = load_existing_urls(str(out))
        assert urls == {"https://github.com/user/repo-a", "https://github.com/user/repo-b"}

    def test_returns_empty_set_for_missing_file(self, tmp_path):
        urls = load_existing_urls(str(tmp_path / "nonexistent.jsonl"))
        assert urls == set()

    def test_handles_malformed_lines_gracefully(self, tmp_path):
        out = tmp_path / "out.jsonl"
        out.write_text(
            '{"repo_url": "https://github.com/user/good"}\n'
            'not valid json\n'
            '{"no_repo_url": true}\n'
        )
        urls = load_existing_urls(str(out))
        assert urls == {"https://github.com/user/good"}

    def test_handles_empty_lines_gracefully(self, tmp_path):
        out = tmp_path / "out.jsonl"
        out.write_text(
            '{"repo_url": "https://github.com/user/repo"}\n'
            '\n'
            '\n'
        )
        urls = load_existing_urls(str(out))
        assert len(urls) == 1

    def test_handles_empty_file(self, tmp_path):
        out = tmp_path / "out.jsonl"
        out.write_text("")
        urls = load_existing_urls(str(out))
        assert urls == set()
