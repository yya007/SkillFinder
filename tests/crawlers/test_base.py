"""Tests for crawlers/base.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# TestFindSkillMdPaths
# ---------------------------------------------------------------------------

class TestFindSkillMdPaths:
    """Unit tests for find_skill_md_paths() and _find_skill_md_via_search()."""

    def test_returns_paths_from_tree(self):
        from crawlers.base import find_skill_md_paths
        mock_session = MagicMock()
        with patch("crawlers.base.github_get") as mock_get:
            mock_get.return_value = {
                "tree": [
                    {"type": "blob", "path": "SKILL.md"},
                    {"type": "blob", "path": "subdir/SKILL.md"},
                    {"type": "blob", "path": "README.md"},
                    {"type": "tree", "path": "subdir"},
                ],
                "truncated": False,
            }
            paths = find_skill_md_paths(mock_session, "user/repo")

        assert set(paths) == {"SKILL.md", "subdir/SKILL.md"}

    def test_empty_when_no_skill_md(self):
        from crawlers.base import find_skill_md_paths
        mock_session = MagicMock()
        with patch("crawlers.base.github_get") as mock_get:
            mock_get.return_value = {
                "tree": [
                    {"type": "blob", "path": "README.md"},
                    {"type": "tree", "path": "subdir"},
                ],
                "truncated": False,
            }
            paths = find_skill_md_paths(mock_session, "user/repo")

        assert paths == []

    def test_falls_back_to_code_search_when_truncated(self):
        from crawlers.base import find_skill_md_paths
        mock_session = MagicMock()
        with patch("crawlers.base.github_get") as mock_get, \
             patch("crawlers.base._find_skill_md_via_search") as mock_search:
            mock_get.return_value = {
                "tree": [{"type": "blob", "path": "early/SKILL.md"}],
                "truncated": True,
            }
            mock_search.return_value = ["skills/a/SKILL.md", "skills/b/SKILL.md"]

            paths = find_skill_md_paths(mock_session, "user/monorepo")

        mock_search.assert_called_once_with(mock_session, "user/monorepo")
        # Tree results are discarded; code search results used instead
        assert set(paths) == {"skills/a/SKILL.md", "skills/b/SKILL.md"}
        assert "early/SKILL.md" not in paths

    def test_returns_empty_on_runtime_error(self):
        from crawlers.base import find_skill_md_paths
        mock_session = MagicMock()
        with patch("crawlers.base.github_get", side_effect=RuntimeError("404")):
            paths = find_skill_md_paths(mock_session, "user/missing")

        assert paths == []

    def test_code_search_paginates_multiple_pages(self):
        """_find_skill_md_via_search correctly paginates across 2 pages."""
        from crawlers.base import _find_skill_md_via_search
        mock_session = MagicMock()
        page1_items = [{"path": f"skills/s{i}/SKILL.md"} for i in range(100)]
        page2_items = [{"path": f"skills/s{i}/SKILL.md"} for i in range(100, 150)]

        call_count = {"n": 0}

        def side_effect(session, url, params=None, timeout=30):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"items": page1_items, "total_count": 150}
            return {"items": page2_items, "total_count": 150}

        with patch("crawlers.base.github_get", side_effect=side_effect), \
             patch("time.sleep"):  # suppress real sleep
            paths = _find_skill_md_via_search(mock_session, "user/big-monorepo")

        assert len(paths) == 150
        assert call_count["n"] == 2  # two pages fetched

    def test_code_search_deduplicates_paths(self):
        """Duplicate paths across pages are only included once."""
        from crawlers.base import _find_skill_md_via_search
        mock_session = MagicMock()
        items_with_dup = [
            {"path": "skills/a/SKILL.md"},
            {"path": "skills/a/SKILL.md"},  # duplicate
            {"path": "skills/b/SKILL.md"},
        ]
        with patch("crawlers.base.github_get") as mock_get, \
             patch("crawlers.base.time"):
            mock_get.return_value = {"items": items_with_dup, "total_count": 3}
            paths = _find_skill_md_via_search(mock_session, "user/repo")

        assert len(paths) == 2
        assert paths.count("skills/a/SKILL.md") == 1


# ---------------------------------------------------------------------------
# TestInferPlatforms
# ---------------------------------------------------------------------------

class TestInferPlatforms:
    """Unit tests for infer_platforms()."""

    def test_clawhub_default_is_openclaw_only(self):
        from crawlers.base import infer_platforms
        platforms = infer_platforms({}, "clawhub")
        assert platforms == ["openclaw"]
        assert "claude_code" not in platforms

    def test_skillsmp_default_is_claude_code(self):
        from crawlers.base import infer_platforms
        platforms = infer_platforms({}, "skillsmp")
        assert platforms == ["claude_code"]

    def test_marketplace_default_is_claude_code(self):
        from crawlers.base import infer_platforms
        platforms = infer_platforms({}, "marketplace")
        assert platforms == ["claude_code"]

    def test_explicit_platforms_frontmatter_overrides_source_default(self):
        from crawlers.base import infer_platforms
        # Even for clawhub, explicit frontmatter wins
        platforms = infer_platforms({"platforms": ["claude_code", "openclaw"]}, "clawhub")
        assert "claude_code" in platforms
        assert "openclaw" in platforms

    def test_codex_target_returns_codex(self):
        from crawlers.base import infer_platforms
        platforms = infer_platforms({"target": "codex"}, "skillsmp")
        assert platforms == ["codex"]
