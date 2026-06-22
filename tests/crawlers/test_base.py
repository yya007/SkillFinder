"""Tests for crawlers/base.py."""
import base64
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


from crawlers.base import (
    decode_b64_utf8,
    extract_github_url,
    fetch_commit_sha,
    fetch_repo_metadata_batch,
    fetch_skill_md,
    load_crawl_state,
    load_existing_records,
    load_existing_urls,
    make_tombstone,
    parse_frontmatter,
    save_crawl_state,
    write_jsonl,
    fetch_skill_md_cached,
    find_skill_md_paths_cached,
    load_tree_cache,
    save_tree_cache,
)


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
# TestLoadExistingRecords
# ---------------------------------------------------------------------------

class TestLoadExistingRecords:
    """Tests for load_existing_records()."""

    def _write_records(self, tmp_path, records):
        p = tmp_path / "records.jsonl"
        with p.open("w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        return str(p)

    def test_returns_empty_dict_for_missing_file(self, tmp_path):
        result = load_existing_records(str(tmp_path / "nonexistent.jsonl"))
        assert result == {}

    def test_returns_empty_dict_for_empty_file(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert load_existing_records(str(p)) == {}

    def test_keys_by_skill_md_url_when_present(self, tmp_path):
        records = [
            {
                "repo_url": "https://github.com/user/repo",
                "name": "skill",
                "raw_metadata": {"skill_md_url": "https://github.com/user/repo/blob/main/SKILL.md", "stars": 5, "pushed_at": "2026-01-01"},
            }
        ]
        path = self._write_records(tmp_path, records)
        result = load_existing_records(path)
        assert "https://github.com/user/repo/blob/main/SKILL.md" in result
        assert result["https://github.com/user/repo/blob/main/SKILL.md"]["name"] == "skill"

    def test_keys_by_repo_url_when_skill_md_url_absent(self, tmp_path):
        records = [
            {
                "repo_url": "https://github.com/user/repo",
                "name": "skill",
                "raw_metadata": {"skill_md_url": "", "stars": 5, "pushed_at": "2026-01-01"},
            }
        ]
        path = self._write_records(tmp_path, records)
        result = load_existing_records(path)
        assert "https://github.com/user/repo" in result

    def test_keys_by_repo_url_when_no_raw_metadata(self, tmp_path):
        records = [
            {"repo_url": "https://github.com/user/repo2", "name": "skill2"}
        ]
        path = self._write_records(tmp_path, records)
        result = load_existing_records(path)
        assert "https://github.com/user/repo2" in result

    def test_handles_mixed_keys(self, tmp_path):
        records = [
            {
                "repo_url": "https://github.com/user/a",
                "raw_metadata": {"skill_md_url": "https://github.com/user/a/blob/main/SKILL.md"},
            },
            {
                "repo_url": "https://github.com/user/b",
                "raw_metadata": {"skill_md_url": ""},
            },
        ]
        path = self._write_records(tmp_path, records)
        result = load_existing_records(path)
        assert "https://github.com/user/a/blob/main/SKILL.md" in result
        assert "https://github.com/user/b" in result
        assert len(result) == 2


    def test_skips_malformed_lines(self, tmp_path):
        p = tmp_path / "bad.jsonl"
        p.write_text('{"repo_url": "https://github.com/user/good"}\nnot-json\n')
        result = load_existing_records(str(p))
        assert len(result) == 1
        assert "https://github.com/user/good" in result

    def test_returns_full_record(self, tmp_path):
        records = [
            {
                "repo_url": "https://github.com/user/repo",
                "name": "my-skill",
                "description": "does stuff",
                "source": "skillsmp",
                "raw_metadata": {"skill_md_url": "", "stars": 42, "pushed_at": "2026-02-01"},
            }
        ]
        path = self._write_records(tmp_path, records)
        result = load_existing_records(path)
        rec = result["https://github.com/user/repo"]
        assert rec["name"] == "my-skill"
        assert rec["raw_metadata"]["stars"] == 42



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

        assert paths == {}

    def test_falls_back_to_code_search_when_truncated(self):
        from crawlers.base import find_skill_md_paths
        mock_session = MagicMock()
        with patch("crawlers.base.github_get") as mock_get, \
             patch("crawlers.base._find_skill_md_via_search") as mock_search:
            mock_get.return_value = {
                "tree": [{"type": "blob", "path": "early/SKILL.md"}],
                "truncated": True,
            }
            mock_search.return_value = {"skills/a/SKILL.md": "", "skills/b/SKILL.md": ""}

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

        assert paths == {}

    def test_code_search_paginates_multiple_pages(self):
        """_find_skill_md_via_search correctly paginates across 2 pages."""
        from crawlers.base import _find_skill_md_via_search
        mock_session = MagicMock()
        page1_items = [{"path": f"skills/s{i}/SKILL.md"} for i in range(100)]
        page2_items = [{"path": f"skills/s{i}/SKILL.md"} for i in range(100, 150)]

        call_count = {"n": 0}

        def side_effect(session, url, params=None, timeout=30, etag=None):
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
             patch("time.sleep"):
            mock_get.return_value = {"items": items_with_dup, "total_count": 3}
            paths = _find_skill_md_via_search(mock_session, "user/repo")

        assert len(paths) == 2
        assert "skills/a/SKILL.md" in paths
        assert "skills/b/SKILL.md" in paths


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


# ---------------------------------------------------------------------------
# TestLoadCrawlState
# ---------------------------------------------------------------------------

class TestLoadCrawlState:
    def test_returns_empty_state_when_no_file(self, tmp_path):
        state = load_crawl_state("testcrawler", state_dir=str(tmp_path))
        assert state == {"source": "testcrawler", "repos": {}, "awesome_lists": {}}

    def test_loads_existing_state(self, tmp_path):
        state_file = tmp_path / "testcrawler.json"
        state_file.write_text(
            json.dumps({"source": "testcrawler", "repos": {"user/repo": {"last_sha": "abc"}}, "awesome_lists": {}}),
            encoding="utf-8",
        )
        state = load_crawl_state("testcrawler", state_dir=str(tmp_path))
        assert state["repos"]["user/repo"]["last_sha"] == "abc"

    def test_returns_empty_state_on_corrupt_file(self, tmp_path):
        state_file = tmp_path / "testcrawler.json"
        state_file.write_text("not valid json", encoding="utf-8")
        state = load_crawl_state("testcrawler", state_dir=str(tmp_path))
        assert state == {"source": "testcrawler", "repos": {}, "awesome_lists": {}}


# ---------------------------------------------------------------------------
# TestSaveCrawlState
# ---------------------------------------------------------------------------

class TestSaveCrawlState:
    def test_saves_state_to_file(self, tmp_path):
        state = {"source": "clawhub", "repos": {"user/r": {"last_sha": "def"}}, "awesome_lists": {}}
        save_crawl_state(state, "clawhub", state_dir=str(tmp_path))
        saved = json.loads((tmp_path / "clawhub.json").read_text(encoding="utf-8"))
        assert saved["repos"]["user/r"]["last_sha"] == "def"

    def test_creates_directory_if_missing(self, tmp_path):
        nested = tmp_path / "a" / "b" / "state"
        state = {"source": "x", "repos": {}, "awesome_lists": {}}
        save_crawl_state(state, "x", state_dir=str(nested))
        assert (nested / "x.json").exists()

    def test_overwrites_existing_state(self, tmp_path):
        state_v1 = {"source": "s", "repos": {"r": {"sha": "old"}}, "awesome_lists": {}}
        save_crawl_state(state_v1, "s", state_dir=str(tmp_path))
        state_v2 = {"source": "s", "repos": {"r": {"sha": "new"}}, "awesome_lists": {}}
        save_crawl_state(state_v2, "s", state_dir=str(tmp_path))
        saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
        assert saved["repos"]["r"]["sha"] == "new"


# ---------------------------------------------------------------------------
# TestMakeTombstone
# ---------------------------------------------------------------------------

class TestMakeTombstone:
    def test_tombstone_has_required_fields(self):
        t = make_tombstone(
            "https://github.com/user/repo",
            "https://github.com/user/repo/blob/main/SKILL.md",
            "skillsmp",
        )
        assert t["tombstone"] is True
        assert t["repo_url"] == "https://github.com/user/repo"
        assert t["skill_md_url"] == "https://github.com/user/repo/blob/main/SKILL.md"
        assert t["source"] == "skillsmp"
        assert "deleted_at" in t

    def test_tombstone_deleted_at_is_iso_string(self):
        t = make_tombstone("https://github.com/u/r", "...", "clawhub")
        deleted_at = t["deleted_at"]
        assert "T" in deleted_at  # ISO 8601 format


# ---------------------------------------------------------------------------
# TestFetchCommitSha
# ---------------------------------------------------------------------------

class TestFetchCommitSha:
    def test_returns_sha_on_success(self):
        mock_session = MagicMock()
        with patch("crawlers.base.github_get") as mock_get:
            mock_get.return_value = {"sha": "abc123def456"}
            sha = fetch_commit_sha(mock_session, "user/repo")
        assert sha == "abc123def456"

    def test_returns_none_on_runtime_error(self):
        mock_session = MagicMock()
        with patch("crawlers.base.github_get", side_effect=RuntimeError("not found")):
            sha = fetch_commit_sha(mock_session, "user/missing")
        assert sha is None

    def test_returns_none_when_github_get_returns_none(self):
        mock_session = MagicMock()
        with patch("crawlers.base.github_get", return_value=None):
            sha = fetch_commit_sha(mock_session, "user/repo")
        assert sha is None


# ---------------------------------------------------------------------------
# TestDecodeB64Utf8
# ---------------------------------------------------------------------------

class TestDecodeB64Utf8:
    def test_decodes_simple_ascii(self):
        encoded = base64.b64encode(b"hello world").decode()
        assert decode_b64_utf8(encoded) == "hello world"

    def test_strips_newlines_before_decoding(self):
        # GitHub API wraps base64 at 60-char column boundaries
        raw = b"Hello, GitHub Contents API!"
        encoded_with_newlines = base64.b64encode(raw).decode()
        # Insert a newline mid-string as GitHub does
        chunked = "\n".join(encoded_with_newlines[i:i+10] for i in range(0, len(encoded_with_newlines), 10))
        assert decode_b64_utf8(chunked) == raw.decode()

    def test_replaces_invalid_utf8_bytes(self):
        # \xff is not valid UTF-8 — should become U+FFFD replacement character
        encoded = base64.b64encode(b"\xff\xfe").decode()
        result = decode_b64_utf8(encoded)
        assert "\ufffd" in result

    def test_decodes_multi_line_yaml(self):
        content = "---\nname: test-skill\ndescription: A skill.\n---\n# Body\n"
        encoded = base64.b64encode(content.encode()).decode()
        assert decode_b64_utf8(encoded) == content


# ---------------------------------------------------------------------------
# TestParseFrontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_extracts_basic_frontmatter(self):
        content = "---\nname: my-skill\ndescription: Does stuff.\n---\n# Body\n"
        fm = parse_frontmatter(content)
        assert fm["name"] == "my-skill"
        assert fm["description"] == "Does stuff."

    def test_returns_empty_dict_for_empty_string(self):
        assert parse_frontmatter("") == {}

    def test_returns_empty_dict_when_no_frontmatter(self):
        assert parse_frontmatter("# Just a heading\n\nSome content.\n") == {}

    def test_returns_empty_dict_for_malformed_yaml(self):
        content = "---\n: invalid: yaml: [\n---\n"
        assert parse_frontmatter(content) == {}

    def test_returns_empty_dict_when_yaml_not_a_dict(self):
        # YAML that parses to a list instead of a dict
        content = "---\n- item1\n- item2\n---\n"
        assert parse_frontmatter(content) == {}

    def test_strips_leading_html_comment(self):
        content = "<!-- Copyright 2025 Acme Corp. -->\n---\nname: skill-with-copyright\n---\n"
        fm = parse_frontmatter(content)
        assert fm["name"] == "skill-with-copyright"

    def test_strips_multi_line_html_comment(self):
        content = "<!--\nCopyright (c) 2025\nAll rights reserved.\n-->\n---\nname: test\n---\n"
        fm = parse_frontmatter(content)
        assert fm["name"] == "test"

    def test_handles_content_without_closing_dashes(self):
        content = "---\nname: incomplete\n"
        assert parse_frontmatter(content) == {}

    def test_handles_multiple_frontmatter_fields(self):
        content = "---\nname: skill\ntriggers:\n  - deploy\n  - ship\nplatforms:\n  - claude_code\n---\n"
        fm = parse_frontmatter(content)
        assert fm["name"] == "skill"
        assert fm["triggers"] == ["deploy", "ship"]
        assert fm["platforms"] == ["claude_code"]


# ---------------------------------------------------------------------------
# TestFetchSkillMd
# ---------------------------------------------------------------------------

class TestFetchSkillMd:
    """Unit tests for fetch_skill_md() in crawlers/base.py."""

    def _make_contents_response(self, content: str) -> dict:
        """Build a fake GitHub Contents API response for a regular file."""
        encoded = base64.b64encode(content.encode()).decode()
        return {"type": "file", "content": encoded, "encoding": "base64"}

    def test_returns_decoded_content_on_success(self):
        mock_session = MagicMock()
        skill_text = "---\nname: test\n---\n"
        with patch("crawlers.base.github_get") as mock_get:
            mock_get.return_value = self._make_contents_response(skill_text)
            result = fetch_skill_md(mock_session, "user/repo")
        assert result == skill_text

    def test_returns_none_on_runtime_error(self):
        mock_session = MagicMock()
        with patch("crawlers.base.github_get", side_effect=RuntimeError("404")):
            result = fetch_skill_md(mock_session, "user/missing")
        assert result is None

    def test_tries_default_branch_first(self):
        mock_session = MagicMock()
        calls = []

        def side_effect(session, url, params=None, **kwargs):
            calls.append(params.get("ref") if params else None)
            return self._make_contents_response("---\nname: x\n---\n")

        with patch("crawlers.base.github_get", side_effect=side_effect):
            fetch_skill_md(mock_session, "user/repo", default_branch="develop")

        assert calls[0] == "develop"

    def test_falls_back_to_main_when_default_branch_fails(self):
        mock_session = MagicMock()
        call_refs = []

        def side_effect(session, url, params=None, **kwargs):
            ref = params.get("ref") if params else None
            call_refs.append(ref)
            if ref == "develop":
                raise RuntimeError("404")
            return self._make_contents_response("---\nname: x\n---\n")

        with patch("crawlers.base.github_get", side_effect=side_effect):
            result = fetch_skill_md(mock_session, "user/repo", default_branch="develop")

        assert result is not None
        assert "develop" in call_refs
        assert "main" in call_refs

    def test_falls_back_to_master_when_main_fails(self):
        mock_session = MagicMock()
        call_refs = []

        def side_effect(session, url, params=None, **kwargs):
            ref = params.get("ref") if params else None
            call_refs.append(ref)
            if ref in ("main",):
                raise RuntimeError("404")
            return self._make_contents_response("---\nname: x\n---\n")

        with patch("crawlers.base.github_get", side_effect=side_effect):
            result = fetch_skill_md(mock_session, "user/repo", default_branch="main")

        assert result is not None
        assert "master" in call_refs

    def test_resolves_symlink_one_level_deep(self):
        mock_session = MagicMock()
        symlink_response = {"type": "symlink", "target": "../../shared/SKILL.md"}
        resolved_content = "---\nname: shared-skill\n---\n"

        call_count = {"n": 0}

        def side_effect(session, url, params=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return symlink_response
            return self._make_contents_response(resolved_content)

        with patch("crawlers.base.github_get", side_effect=side_effect):
            result = fetch_skill_md(mock_session, "user/repo", path="subdir/SKILL.md")

        assert result == resolved_content
        assert call_count["n"] == 2

    def test_symlink_depth_guard_prevents_infinite_recursion(self):
        """A symlink at _depth=1 is NOT followed — no infinite loop."""
        mock_session = MagicMock()
        symlink_response = {"type": "symlink", "target": "other/SKILL.md"}

        with patch("crawlers.base.github_get", return_value=symlink_response):
            result = fetch_skill_md(mock_session, "user/repo", _depth=1)

        # At depth=1, symlink is not followed → no content → None
        assert result is None

    def test_returns_none_when_content_is_empty(self):
        mock_session = MagicMock()
        with patch("crawlers.base.github_get", return_value={"type": "file", "content": ""}):
            result = fetch_skill_md(mock_session, "user/repo")
        assert result is None

    def test_does_not_duplicate_main_in_branch_list(self):
        """When default_branch is 'main', branches_to_try should not contain 'main' twice."""
        mock_session = MagicMock()
        call_refs = []

        def side_effect(session, url, params=None, **kwargs):
            ref = params.get("ref") if params else None
            call_refs.append(ref)
            raise RuntimeError("always fail")

        with patch("crawlers.base.github_get", side_effect=side_effect):
            fetch_skill_md(mock_session, "user/repo", default_branch="main")

        # main should appear only once, master also once
        assert call_refs.count("main") == 1
        assert call_refs.count("master") == 1


# ---------------------------------------------------------------------------
# TestFetchSkillMdRaw
# ---------------------------------------------------------------------------

class TestFetchSkillMdRaw:
    def test_uses_raw_and_skips_api_on_success(self):
        from crawlers.base import fetch_skill_md
        raw_body = "---\nname: deploy\ndescription: x\n---\nbody"

        class Resp:
            status_code = 200
            text = raw_body

        mock_session = MagicMock()
        mock_session.get.return_value = Resp()
        with patch("crawlers.base.github_get") as mock_api:
            content = fetch_skill_md(mock_session, "user/repo", "SKILL.md", "main")

        assert content == raw_body
        mock_api.assert_not_called()              # Contents API never touched

    def test_falls_back_to_api_when_raw_404(self):
        from crawlers.base import fetch_skill_md

        class Resp404:
            status_code = 404
            text = "404: Not Found"

        mock_session = MagicMock()
        mock_session.get.return_value = Resp404()
        with patch("crawlers.base.github_get") as mock_api:
            mock_api.return_value = {"content": "LS0tCm5hbWU6IHkKLS0t"}  # base64 "---\nname: y\n---"
            content = fetch_skill_md(mock_session, "user/repo", "SKILL.md", "main")

        assert content is not None and "name: y" in content
        mock_api.assert_called()                  # fell back to API

    def test_falls_back_to_api_when_raw_looks_like_symlink(self):
        from crawlers.base import fetch_skill_md

        class RespSymlink:
            status_code = 200
            text = "../shared/SKILL.md"          # short, no frontmatter → suspicious

        mock_session = MagicMock()
        mock_session.get.return_value = RespSymlink()
        with patch("crawlers.base.github_get") as mock_api:
            mock_api.return_value = {"content": "LS0tCm5hbWU6IHoKLS0t"}  # "---\nname: z\n---"
            content = fetch_skill_md(mock_session, "user/repo", "SKILL.md", "main")

        assert content is not None and "name: z" in content
        mock_api.assert_called()


# ---------------------------------------------------------------------------
# TestGithubGetRateLimit
# ---------------------------------------------------------------------------

def _mk_response(status_code, headers=None, text="", json_body=None):
    """Build a MagicMock standing in for a requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    resp.url = "https://api.github.com/test"
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


class TestGithubGetRateLimit:
    """github_get() must never hang on a persistent rate limit (the CI bug)."""

    def test_bounds_retries_on_persistent_429(self):
        """A session that always returns 429 must raise, not loop forever."""
        from crawlers.base import github_get, _MAX_RATELIMIT_RETRIES

        # 429 with a tiny Retry-After and a near-now reset — the conditions
        # GitHub's secondary (abuse) rate limit produces.
        always_429 = _mk_response(
            429, headers={"Retry-After": "0", "X-RateLimit-Remaining": "100"}
        )
        mock_session = MagicMock()
        mock_session.get.return_value = always_429

        with patch("crawlers.base.time.sleep"):  # don't actually sleep
            with pytest.raises(RuntimeError):
                github_get(mock_session, "https://api.github.com/test")

        # Bounded: at most the initial GET + _MAX_RATELIMIT_RETRIES retries.
        assert mock_session.get.call_count <= _MAX_RATELIMIT_RETRIES + 1

    def test_recovers_when_429_clears(self):
        """If the limit clears after a few 429s, github_get returns the body."""
        from crawlers.base import github_get

        responses = [
            _mk_response(429, headers={"Retry-After": "0"}),
            _mk_response(429, headers={"Retry-After": "0"}),
            _mk_response(200, headers={"X-RateLimit-Remaining": "100"},
                         json_body={"ok": True}),
        ]
        mock_session = MagicMock()
        mock_session.get.side_effect = responses

        with patch("crawlers.base.time.sleep"):
            result = github_get(mock_session, "https://api.github.com/test")

        assert result == {"ok": True}

    def test_retry_after_takes_precedence_over_reset(self):
        """Secondary limits are signalled by Retry-After, not X-RateLimit-Reset."""
        from crawlers.base import _rate_limit_wait_seconds

        # Reset is far in the future, but Retry-After says 3s — honor Retry-After.
        resp = _mk_response(
            429,
            headers={
                "Retry-After": "3",
                "X-RateLimit-Reset": str(int(time.time()) + 3600),
            },
        )
        wait = _rate_limit_wait_seconds(resp)
        assert 3 <= wait <= 6  # ~3s (+ small safety margin), NOT ~3600s

    def test_wait_is_capped(self):
        """An absurd Retry-After must be capped, not honored literally."""
        from crawlers.base import _rate_limit_wait_seconds, _MAX_WAIT_SECONDS

        resp = _mk_response(429, headers={"Retry-After": "99999"})
        assert _rate_limit_wait_seconds(resp) <= _MAX_WAIT_SECONDS

    def test_falls_back_to_reset_without_retry_after(self):
        """Without Retry-After, use X-RateLimit-Reset (primary quota)."""
        from crawlers.base import _rate_limit_wait_seconds

        resp = _mk_response(
            403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(time.time()) + 10),
            },
        )
        wait = _rate_limit_wait_seconds(resp)
        assert 10 <= wait <= 20


class TestApiCounters:
    def test_categorizes_requests_by_kind(self):
        from crawlers.base import (
            reset_api_counters, get_api_counters, record_request,
        )
        reset_api_counters()
        record_request("https://api.github.com/repos/a/b", 200)
        record_request("https://api.github.com/search/code?q=x", 200)
        record_request("https://raw.githubusercontent.com/a/b/main/SKILL.md", 200)
        record_request("https://api.github.com/repos/a/b", 304)
        counters = get_api_counters()
        assert counters["rest"] == 1
        assert counters["search"] == 1
        assert counters["raw_free"] == 1
        assert counters["conditional_304"] == 1

    def test_reset_zeroes_counters(self):
        from crawlers.base import reset_api_counters, get_api_counters, record_request
        record_request("https://api.github.com/repos/a/b", 200)
        reset_api_counters()
        assert get_api_counters()["rest"] == 0

    def test_fetch_repo_metadata_with_etag_counts_304(self):
        from crawlers.base import (
            reset_api_counters, get_api_counters, fetch_repo_metadata_with_etag,
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 304
        fake_resp.headers = {"ETag": 'W/"x"'}
        session = MagicMock()
        session.get.return_value = fake_resp
        reset_api_counters()
        fetch_repo_metadata_with_etag(session, "a/b", 'W/"x"')
        assert get_api_counters()["conditional_304"] == 1

    def test_fetch_repo_metadata_with_etag_counts_200(self):
        from crawlers.base import (
            reset_api_counters, get_api_counters, fetch_repo_metadata_with_etag,
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.headers = {"ETag": 'W/"y"'}
        fake_resp.json.return_value = {
            "stargazers_count": 5, "pushed_at": "2026-01-01T00:00:00Z",
            "topics": [], "description": "", "default_branch": "main",
        }
        session = MagicMock()
        session.get.return_value = fake_resp
        reset_api_counters()
        fetch_repo_metadata_with_etag(session, "a/b", None)
        assert get_api_counters()["rest"] == 1

    def test_fetch_repo_metadata_with_etag_retries_on_429(self):
        """fetch_repo_metadata_with_etag must honor rate limits: on 429 it waits
        and retries, ultimately returning the 200 metadata (regression: the old
        single-attempt path returned {} on 429 without retrying)."""
        from crawlers.base import fetch_repo_metadata_with_etag

        resp429 = MagicMock()
        resp429.status_code = 429
        resp429.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}

        resp200 = MagicMock()
        resp200.status_code = 200
        resp200.headers = {"ETag": 'W/"v"'}
        resp200.json.return_value = {
            "stargazers_count": 5, "pushed_at": "", "topics": [],
            "description": "", "default_branch": "main",
        }

        mock_session = MagicMock()
        mock_session.get.side_effect = [resp429, resp200]

        with patch("crawlers.base._wait_for_reset"):  # no-op — no real sleep
            meta, new_etag = fetch_repo_metadata_with_etag(mock_session, "a/b")

        assert meta["stargazers_count"] == 5, (
            "Expected successful 200 metadata; got {} — rate-limit retry not implemented"
        )
        assert new_etag == 'W/"v"'
        assert mock_session.get.call_count == 2, (
            "Expected 2 GET calls (1 retry after 429); got "
            f"{mock_session.get.call_count}"
        )


# ---------------------------------------------------------------------------
# TestMetaCacheIO
# ---------------------------------------------------------------------------

class TestMetaCacheIO:
    def test_roundtrip(self, tmp_path):
        from crawlers.base import load_meta_cache, save_meta_cache
        p = str(tmp_path / "repo_meta_cache.json")
        cache = {"a/b": {"etag": "W/\"x\"", "pushed_at": "2026-01-01T00:00:00Z",
                          "stargazers_count": 10, "topics": [], "description": "",
                          "default_branch": "main"}}
        save_meta_cache(cache, p)
        assert load_meta_cache(p) == cache

    def test_missing_file_returns_empty_dict(self, tmp_path):
        from crawlers.base import load_meta_cache
        assert load_meta_cache(str(tmp_path / "nope.json")) == {}


# ---------------------------------------------------------------------------
# TestFetchRepoMetadataCached
# ---------------------------------------------------------------------------

class TestFetchRepoMetadataCached:
    def test_returns_cached_on_304(self):
        from crawlers.base import fetch_repo_metadata_cached
        cache = {"a/b": {"etag": "W/\"v1\"", "stargazers_count": 42,
                         "pushed_at": "2026-01-01T00:00:00Z", "topics": [],
                         "description": "", "default_branch": "main"}}
        with patch("crawlers.base.fetch_repo_metadata_with_etag",
                   return_value=(None, "W/\"v1\"")) as mock_fetch:
            meta = fetch_repo_metadata_cached(MagicMock(), "a/b", cache)
        mock_fetch.assert_called_once()
        assert mock_fetch.call_args.args[1] == "a/b"      # called with the repo
        assert mock_fetch.call_args.args[2] == "W/\"v1\""  # ...and its cached etag
        assert meta["stargazers_count"] == 42             # served from cache, no re-download

    def test_updates_cache_on_200(self):
        from crawlers.base import fetch_repo_metadata_cached
        cache = {}
        fresh = {"stargazers_count": 7, "pushed_at": "2026-02-02T00:00:00Z",
                 "topics": ["ai"], "description": "d", "default_branch": "main"}
        with patch("crawlers.base.fetch_repo_metadata_with_etag",
                   return_value=(fresh, "W/\"v2\"")):
            meta = fetch_repo_metadata_cached(MagicMock(), "c/d", cache)
        assert meta["stargazers_count"] == 7
        assert cache["c/d"]["etag"] == "W/\"v2\""
        assert cache["c/d"]["stargazers_count"] == 7


# ---------------------------------------------------------------------------
# TestFetchSkillMdCached
# ---------------------------------------------------------------------------

class TestFetchSkillMdCached:
    def test_cache_hit_skips_fetch(self):
        cache = {"sha123": "---\nname: cached\n---"}
        with patch("crawlers.base.fetch_skill_md") as mock_fetch:
            content = fetch_skill_md_cached(
                MagicMock(), "u/r", "SKILL.md", "sha123", "main", cache)
        assert content == "---\nname: cached\n---"
        mock_fetch.assert_not_called()

    def test_cache_miss_fetches_and_stores(self):
        cache = {}
        with patch("crawlers.base.fetch_skill_md", return_value="---\nname: new\n---"):
            content = fetch_skill_md_cached(
                MagicMock(), "u/r", "SKILL.md", "sha999", "main", cache)
        assert content == "---\nname: new\n---"
        assert cache["sha999"] == "---\nname: new\n---"

    def test_empty_sha_always_fetches(self):
        # Code-search fallback yields "" SHAs — never cache-key on empty.
        cache = {"": "WRONG"}
        with patch("crawlers.base.fetch_skill_md", return_value="real") as mock_fetch:
            content = fetch_skill_md_cached(MagicMock(), "u/r", "SKILL.md", "", "main", cache)
        assert content == "real"
        mock_fetch.assert_called_once()


# ---------------------------------------------------------------------------
# TestTreeCache (load_tree_cache / save_tree_cache aliases)
# ---------------------------------------------------------------------------

class TestTreeCacheAliases:
    def test_load_tree_cache_returns_empty_for_missing_file(self, tmp_path):
        result = load_tree_cache(str(tmp_path / "nonexistent.json"))
        assert result == {}

    def test_save_and_load_round_trip(self, tmp_path):
        path = str(tmp_path / "tree_cache.json")
        cache = {"u/r": {"pushed_at": "2026-01-01T00:00:00Z", "paths": {"SKILL.md": "sha1"}}}
        save_tree_cache(cache, path)
        result = load_tree_cache(path)
        assert result == cache

    def test_load_tree_cache_returns_empty_on_corrupt_file(self, tmp_path):
        p = tmp_path / "corrupt.json"
        p.write_text("not valid json")
        result = load_tree_cache(str(p))
        assert result == {}


# ---------------------------------------------------------------------------
# TestFindSkillMdPathsCached
# ---------------------------------------------------------------------------

class TestFindSkillMdPathsCached:
    def test_cache_hit_skips_tree_call(self):
        """Pre-seeded cache with matching pushed_at and default_branch → no API call made."""
        tree_cache = {
            "u/r": {"pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main", "paths": {"SKILL.md": "sha1"}}
        }
        with patch("crawlers.base.find_skill_md_paths") as mock_tree:
            result = find_skill_md_paths_cached(
                MagicMock(), "u/r", "2026-01-01T00:00:00Z", "main", tree_cache
            )
        assert result == {"SKILL.md": "sha1"}
        mock_tree.assert_not_called()

    def test_cache_miss_calls_and_stores(self):
        """Empty cache → calls find_skill_md_paths and stores result."""
        tree_cache = {}
        with patch("crawlers.base.find_skill_md_paths", return_value={"SKILL.md": "sha9"}) as mock_tree:
            result = find_skill_md_paths_cached(
                MagicMock(), "u/r", "2026-02-02T00:00:00Z", "main", tree_cache
            )
        assert result == {"SKILL.md": "sha9"}
        mock_tree.assert_called_once()
        assert tree_cache["u/r"] == {
            "pushed_at": "2026-02-02T00:00:00Z",
            "default_branch": "main",
            "paths": {"SKILL.md": "sha9"},
        }

    def test_empty_result_not_cached(self):
        """An empty result (no SKILL.md OR a transient Trees failure, both {}) must
        NOT be cached — caching it would pin a repo with skills to empty forever."""
        tree_cache = {}
        with patch("crawlers.base.find_skill_md_paths", return_value={}) as mock_tree:
            result = find_skill_md_paths_cached(
                MagicMock(), "u/r", "2026-02-02T00:00:00Z", "main", tree_cache
            )
        assert result == {}
        mock_tree.assert_called_once()
        assert tree_cache == {}  # not cached

    def test_changed_pushed_at_refetches(self):
        """Stale pushed_at → refetches and updates cache entry."""
        tree_cache = {
            "u/r": {"pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main", "paths": {"SKILL.md": "old_sha"}}
        }
        new_paths = {"SKILL.md": "new_sha", "sub/SKILL.md": "abc"}
        with patch("crawlers.base.find_skill_md_paths", return_value=new_paths) as mock_tree:
            result = find_skill_md_paths_cached(
                MagicMock(), "u/r", "2026-03-15T12:00:00Z", "main", tree_cache
            )
        mock_tree.assert_called_once()
        assert result == new_paths
        assert tree_cache["u/r"]["pushed_at"] == "2026-03-15T12:00:00Z"
        assert tree_cache["u/r"]["paths"] == new_paths

    def test_empty_pushed_at_always_calls_and_does_not_cache(self):
        """Falsy pushed_at → always calls API and never caches the result."""
        tree_cache = {}
        with patch("crawlers.base.find_skill_md_paths", return_value={"SKILL.md": "x"}) as mock_tree:
            result = find_skill_md_paths_cached(
                MagicMock(), "u/r", "", "main", tree_cache
            )
        assert result == {"SKILL.md": "x"}
        mock_tree.assert_called_once()
        assert tree_cache == {}  # nothing cached

    def test_changed_default_branch_refetches(self):
        """Same pushed_at but different default_branch → cache miss; refetches and updates."""
        tree_cache = {
            "u/r": {"pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main", "paths": {"SKILL.md": "old"}}
        }
        with patch("crawlers.base.find_skill_md_paths", return_value={"SKILL.md": "new"}) as mock_tree:
            result = find_skill_md_paths_cached(
                MagicMock(), "u/r", "2026-01-01T00:00:00Z", "develop", tree_cache
            )
        mock_tree.assert_called_once()
        assert result == {"SKILL.md": "new"}
        assert tree_cache["u/r"]["default_branch"] == "develop"
        assert tree_cache["u/r"]["paths"] == {"SKILL.md": "new"}


# ---------------------------------------------------------------------------
# TestFetchRepoMetadataBatch
# ---------------------------------------------------------------------------

def _make_graphql_node(
    stargazer_count=5,
    pushed_at="2026-01-01T00:00:00Z",
    description="A skill.",
    default_branch="main",
    topics=("python", "ai"),
):
    """Build a fake GraphQL repository node dict."""
    return {
        "stargazerCount": stargazer_count,
        "pushedAt": pushed_at,
        "description": description,
        "defaultBranchRef": {"name": default_branch},
        "repositoryTopics": {
            "nodes": [{"topic": {"name": t}} for t in topics],
        },
    }


def _make_graphql_response(aliases: dict):
    """Wrap alias→node mapping in a fake GraphQL response envelope."""
    return {"data": aliases}


class TestFetchRepoMetadataBatch:
    """Unit tests for fetch_repo_metadata_batch() — no network."""

    def _mock_session(self, json_body, status_code=200):
        """Return a MagicMock session whose .post() returns a fake response."""
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_body
        session = MagicMock()
        session.post.return_value = resp
        return session

    def test_parses_batch_response(self):
        """Two repos resolved → both are in the result with correct flattened fields."""
        payload = _make_graphql_response({
            "r0": _make_graphql_node(
                stargazer_count=10,
                pushed_at="2026-03-01T00:00:00Z",
                description="skill a",
                default_branch="main",
                topics=["python"],
            ),
            "r1": _make_graphql_node(
                stargazer_count=20,
                pushed_at="2026-04-01T00:00:00Z",
                description="skill b",
                default_branch="develop",
                topics=["go", "cli"],
            ),
        })
        session = self._mock_session(payload)
        result = fetch_repo_metadata_batch(session, ["o/a", "o/b"])

        assert set(result.keys()) == {"o/a", "o/b"}

        a = result["o/a"]
        assert a["stargazers_count"] == 10
        assert a["pushed_at"] == "2026-03-01T00:00:00Z"
        assert a["description"] == "skill a"
        assert a["default_branch"] == "main"
        assert a["topics"] == ["python"]

        b = result["o/b"]
        assert b["stargazers_count"] == 20
        assert b["default_branch"] == "develop"
        assert b["topics"] == ["go", "cli"]

    def test_null_repo_is_omitted(self):
        """An alias whose value is null is absent from the result."""
        payload = _make_graphql_response({
            "r0": _make_graphql_node(stargazer_count=5),
            "r1": None,
        })
        session = self._mock_session(payload)
        result = fetch_repo_metadata_batch(session, ["o/a", "o/b"])

        assert "o/a" in result
        assert "o/b" not in result

    def test_partial_errors_still_returns_resolved(self):
        """data with one resolved repo AND a top-level errors list → resolved repo returned."""
        payload = {
            "data": {
                "r0": _make_graphql_node(stargazer_count=7),
                "r1": None,
            },
            "errors": [{"message": "Could not resolve to a Repository with the name 'o/missing'."}],
        }
        session = self._mock_session(payload)
        result = fetch_repo_metadata_batch(session, ["o/a", "o/missing"])

        assert "o/a" in result
        assert result["o/a"]["stargazers_count"] == 7
        assert "o/missing" not in result

    def test_non_200_returns_empty(self):
        """HTTP 500 → {} with no exception raised."""
        session = self._mock_session({}, status_code=500)
        result = fetch_repo_metadata_batch(session, ["o/a", "o/b"])

        assert result == {}
        # No exception propagated
        session.post.assert_called_once()

    def test_chunks_over_100(self):
        """150 repos → two POST calls (100 + 50)."""
        # Both chunks return empty data (all null / absent) — we just count POSTs.
        payload = {"data": {}}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = payload
        session = MagicMock()
        session.post.return_value = resp

        names = [f"owner/repo{i}" for i in range(150)]
        fetch_repo_metadata_batch(session, names)

        assert session.post.call_count == 2

    def test_counts_graphql_request(self):
        """A successful batch POST increments the 'graphql' API counter."""
        from crawlers.base import reset_api_counters, get_api_counters

        payload = _make_graphql_response({"r0": _make_graphql_node()})
        session = self._mock_session(payload)

        reset_api_counters()
        fetch_repo_metadata_batch(session, ["o/a"])

        assert get_api_counters()["graphql"] >= 1
