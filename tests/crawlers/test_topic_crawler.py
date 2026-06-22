"""Tests for crawlers/topic_crawler.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch


from tests.crawlers.conftest import SAMPLE_SKILL_MD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_meta(stars=10, pushed_at="2026-01-01", default_branch="main"):
    return {
        "stargazers_count": stars,
        "pushed_at": pushed_at,
        "default_branch": default_branch,
    }


# ---------------------------------------------------------------------------
# TestDiscoverTopicRepos
# ---------------------------------------------------------------------------

class TestDiscoverTopicRepos:
    """Unit tests for _discover_topic_repos()."""

    def test_returns_full_names(self):
        from crawlers.topic_crawler import _discover_topic_repos

        page = {"items": [{"full_name": "user/skill-a"}, {"full_name": "user/skill-b"}]}
        empty = {"items": []}

        session = MagicMock()
        with patch("crawlers.topic_crawler.github_get") as mock_get:
            mock_get.side_effect = [page, empty] * 20
            result = _discover_topic_repos(session, limit=100)

        assert "user/skill-a" in result
        assert "user/skill-b" in result

    def test_deduplicates_across_queries(self):
        from crawlers.topic_crawler import _discover_topic_repos

        both = {"items": [{"full_name": "user/shared-skill"}]}
        empty = {"items": []}

        session = MagicMock()
        with patch("crawlers.topic_crawler.github_get") as mock_get:
            mock_get.side_effect = [both, empty] * 20
            result = _discover_topic_repos(session, limit=100)

        assert result.count("user/shared-skill") == 1

    def test_respects_limit(self):
        from crawlers.topic_crawler import _discover_topic_repos

        big_page = {"items": [{"full_name": f"user/skill-{i}"} for i in range(100)]}
        empty = {"items": []}

        session = MagicMock()
        with patch("crawlers.topic_crawler.github_get") as mock_get:
            mock_get.side_effect = [big_page, empty] * 20
            result = _discover_topic_repos(session, limit=5)

        assert len(result) <= 5

    def test_handles_api_error_gracefully(self):
        from crawlers.topic_crawler import _discover_topic_repos

        session = MagicMock()
        with patch("crawlers.topic_crawler.github_get") as mock_get:
            mock_get.side_effect = RuntimeError("rate limited")
            result = _discover_topic_repos(session, limit=10)

        assert result == []


# ---------------------------------------------------------------------------
# TestTopicCrawlerRun
# ---------------------------------------------------------------------------

class TestTopicCrawlerRun:
    """Unit tests for topic_crawler.run() with HTTP mocked out."""

    def _patch_run(self):
        return (
            patch("crawlers.topic_crawler._discover_topic_repos"),
            patch("crawlers.topic_crawler.fetch_repo_metadata_cached"),
            patch("crawlers.topic_crawler.find_skill_md_paths"),
            patch("crawlers.topic_crawler.fetch_skill_md_cached"),
            patch("crawlers.topic_crawler.load_meta_cache", return_value={}),
            patch("crawlers.topic_crawler.save_meta_cache"),
            patch("crawlers.topic_crawler.load_content_cache", return_value={}),
            patch("crawlers.topic_crawler.save_content_cache"),
        )

    def test_writes_records_for_discovered_repos(self, tmp_path):
        from crawlers.topic_crawler import run

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"):
            mock_disc.return_value = ["user/skill-a", "user/skill-b"]
            mock_meta.return_value = _mock_meta()
            mock_paths.return_value = {"SKILL.md": "sha1"}
            mock_skill_md.return_value = SAMPLE_SKILL_MD

            out = str(tmp_path / "out.jsonl")
            count = run(out)

        assert count == 2

    def test_output_has_required_fields(self, tmp_path):
        from crawlers.topic_crawler import run

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"):
            mock_disc.return_value = ["user/skill-a"]
            mock_meta.return_value = _mock_meta()
            mock_paths.return_value = {"SKILL.md": "sha1"}
            mock_skill_md.return_value = SAMPLE_SKILL_MD

            out = str(tmp_path / "out.jsonl")
            run(out)

        record = json.loads(Path(out).read_text().strip())
        for field in ("repo_url", "name", "description", "source", "raw_metadata"):
            assert field in record, f"Missing field: {field}"

    def test_source_tag_is_topic(self, tmp_path):
        from crawlers.topic_crawler import run

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"):
            mock_disc.return_value = ["user/skill-a"]
            mock_meta.return_value = _mock_meta()
            mock_paths.return_value = {"SKILL.md": "sha1"}
            mock_skill_md.return_value = None

            out = str(tmp_path / "out.jsonl")
            run(out)

        record = json.loads(Path(out).read_text().strip())
        assert record["source"] == "topic"

    def test_respects_limit(self, tmp_path):
        from crawlers.topic_crawler import run

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"):
            mock_disc.return_value = [f"user/skill-{i}" for i in range(10)]
            mock_meta.return_value = _mock_meta()
            mock_paths.return_value = {"SKILL.md": "sha1"}
            mock_skill_md.return_value = None

            out = str(tmp_path / "out.jsonl")
            count = run(out, limit=3)

        assert count == 3

    def test_skips_repos_with_no_skill_md(self, tmp_path):
        from crawlers.topic_crawler import run

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"):
            mock_disc.return_value = ["user/no-skill-md", "user/has-skill-md"]
            mock_meta.return_value = _mock_meta()
            mock_paths.side_effect = [{}, {"SKILL.md": "sha1"}]
            mock_skill_md.return_value = None

            out = str(tmp_path / "out.jsonl")
            count = run(out)

        assert count == 1

    def test_skips_already_covered_repos(self, tmp_path):
        from crawlers.topic_crawler import run

        # Create a fake existing raw JSONL in a data dir
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        existing = raw_dir / "skillsmp.jsonl"
        existing.write_text(
            json.dumps({"repo_url": "https://github.com/user/already-covered"}) + "\n"
        )

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"):
            mock_disc.return_value = ["user/already-covered", "user/new-skill"]
            mock_meta.return_value = _mock_meta()
            mock_paths.return_value = {"SKILL.md": "sha1"}
            mock_skill_md.return_value = None

            out = str(tmp_path / "out.jsonl")
            count = run(out, existing_raw_dirs=[str(raw_dir)])

        assert count == 1
        record = json.loads(Path(out).read_text().strip())
        assert "user/new-skill" in record["repo_url"]

    def test_resume_skips_existing_keys(self, tmp_path):
        from crawlers.topic_crawler import run

        out = tmp_path / "out.jsonl"
        out.write_text(
            json.dumps({
                "repo_url": "https://github.com/user/skill-a",
                "name": "test-skill",
                "source": "topic",
                "raw_metadata": {"skill_md_url": "https://github.com/user/skill-a/blob/main/SKILL.md"},
            }) + "\n"
        )

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"):
            mock_disc.return_value = ["user/skill-a", "user/skill-b"]
            mock_meta.return_value = _mock_meta()
            mock_paths.return_value = {"SKILL.md": "sha1"}
            mock_skill_md.return_value = None

            count = run(str(out), resume=True)

        assert count == 1

    def test_name_falls_back_to_repo_name_when_no_frontmatter(self, tmp_path):
        from crawlers.topic_crawler import run

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"):
            mock_disc.return_value = ["user/my-cool-skill"]
            mock_meta.return_value = _mock_meta()
            mock_paths.return_value = {"SKILL.md": "sha1"}
            mock_skill_md.return_value = None  # no frontmatter

            out = str(tmp_path / "out.jsonl")
            run(out)

        record = json.loads(Path(out).read_text().strip())
        assert record["name"] == "my-cool-skill"

    def test_topic_crawl_uses_caches(self, tmp_path, monkeypatch):
        """Crawl loads both caches at start and saves both caches at end."""
        import crawlers.topic_crawler as tc

        calls = {"saved_meta": 0, "saved_content": 0}

        monkeypatch.setattr(tc, "load_meta_cache", lambda p: {})
        monkeypatch.setattr(tc, "load_content_cache", lambda p: {})
        monkeypatch.setattr(
            tc, "save_meta_cache",
            lambda c, p: calls.__setitem__("saved_meta", calls["saved_meta"] + 1),
        )
        monkeypatch.setattr(
            tc, "save_content_cache",
            lambda c, p: calls.__setitem__("saved_content", calls["saved_content"] + 1),
        )
        monkeypatch.setattr(
            tc, "fetch_repo_metadata_cached",
            lambda s, r, c: {
                "stargazers_count": 50,
                "default_branch": "main",
                "pushed_at": "",
                "topics": [],
                "description": "",
            },
        )
        monkeypatch.setattr(tc, "find_skill_md_paths", lambda s, r: {"SKILL.md": "sha1"})
        monkeypatch.setattr(tc, "fetch_skill_md_cached", lambda *a, **k: "---\nname: t\n---")
        monkeypatch.setattr(tc, "_discover_topic_repos", lambda s, limit=1000: ["user/skill-a"])

        out = str(tmp_path / "out.jsonl")
        count = tc.run(out)

        assert count == 1
        assert calls["saved_meta"] == 1
        assert calls["saved_content"] == 1
