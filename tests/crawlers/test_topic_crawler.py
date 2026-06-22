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
            patch("crawlers.topic_crawler.fetch_repo_metadata_batch", return_value={}),
            patch("crawlers.topic_crawler.fetch_repo_metadata_cached"),
            patch("crawlers.topic_crawler.find_skill_md_paths_cached"),
            patch("crawlers.topic_crawler.fetch_skill_md_cached"),
            patch("crawlers.topic_crawler.load_meta_cache", return_value={}),
            patch("crawlers.topic_crawler.save_meta_cache"),
            patch("crawlers.topic_crawler.load_content_cache", return_value={}),
            patch("crawlers.topic_crawler.save_content_cache"),
            patch("crawlers.topic_crawler.load_tree_cache", return_value={}),
            patch("crawlers.topic_crawler.save_tree_cache"),
        )

    def test_writes_records_for_discovered_repos(self, tmp_path):
        from crawlers.topic_crawler import run

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_batch", return_value={}) as mock_batch, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths_cached") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"), \
             patch("crawlers.topic_crawler.load_tree_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_tree_cache"), \
             patch("crawlers.topic_crawler.load_crawl_state", return_value={}), \
             patch("crawlers.topic_crawler.save_crawl_state"):
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
             patch("crawlers.topic_crawler.fetch_repo_metadata_batch", return_value={}), \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths_cached") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"), \
             patch("crawlers.topic_crawler.load_tree_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_tree_cache"), \
             patch("crawlers.topic_crawler.load_crawl_state", return_value={}), \
             patch("crawlers.topic_crawler.save_crawl_state"):
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
             patch("crawlers.topic_crawler.fetch_repo_metadata_batch", return_value={}), \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths_cached") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"), \
             patch("crawlers.topic_crawler.load_tree_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_tree_cache"), \
             patch("crawlers.topic_crawler.load_crawl_state", return_value={}), \
             patch("crawlers.topic_crawler.save_crawl_state"):
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
             patch("crawlers.topic_crawler.fetch_repo_metadata_batch", return_value={}), \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths_cached") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"), \
             patch("crawlers.topic_crawler.load_tree_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_tree_cache"), \
             patch("crawlers.topic_crawler.load_crawl_state", return_value={}), \
             patch("crawlers.topic_crawler.save_crawl_state"):
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
             patch("crawlers.topic_crawler.fetch_repo_metadata_batch", return_value={}), \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths_cached") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"), \
             patch("crawlers.topic_crawler.load_tree_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_tree_cache"), \
             patch("crawlers.topic_crawler.load_crawl_state", return_value={}), \
             patch("crawlers.topic_crawler.save_crawl_state"):
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
             patch("crawlers.topic_crawler.fetch_repo_metadata_batch", return_value={}), \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths_cached") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"), \
             patch("crawlers.topic_crawler.load_tree_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_tree_cache"), \
             patch("crawlers.topic_crawler.load_crawl_state", return_value={}), \
             patch("crawlers.topic_crawler.save_crawl_state"):
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
             patch("crawlers.topic_crawler.fetch_repo_metadata_batch", return_value={}), \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths_cached") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"), \
             patch("crawlers.topic_crawler.load_tree_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_tree_cache"), \
             patch("crawlers.topic_crawler.load_crawl_state", return_value={}), \
             patch("crawlers.topic_crawler.save_crawl_state"):
            mock_disc.return_value = ["user/skill-a", "user/skill-b"]
            mock_meta.return_value = _mock_meta()
            mock_paths.return_value = {"SKILL.md": "sha1"}
            mock_skill_md.return_value = None

            count = run(str(out), resume=True)

        assert count == 1

    def test_name_falls_back_to_repo_name_when_no_frontmatter(self, tmp_path):
        from crawlers.topic_crawler import run

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_batch", return_value={}), \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths_cached") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"), \
             patch("crawlers.topic_crawler.load_tree_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_tree_cache"), \
             patch("crawlers.topic_crawler.load_crawl_state", return_value={}), \
             patch("crawlers.topic_crawler.save_crawl_state"):
            mock_disc.return_value = ["user/my-cool-skill"]
            mock_meta.return_value = _mock_meta()
            mock_paths.return_value = {"SKILL.md": "sha1"}
            mock_skill_md.return_value = None  # no frontmatter

            out = str(tmp_path / "out.jsonl")
            run(out)

        record = json.loads(Path(out).read_text().strip())
        assert record["name"] == "my-cool-skill"

    def test_topic_crawl_uses_caches(self, tmp_path, monkeypatch):
        """Crawl loads meta, content, and tree caches at start and saves all three at end."""
        import crawlers.topic_crawler as tc

        calls = {"saved_meta": 0, "saved_content": 0, "saved_tree": 0}

        monkeypatch.setattr(tc, "fetch_repo_metadata_batch", lambda s, names: {})
        monkeypatch.setattr(tc, "load_meta_cache", lambda p: {})
        monkeypatch.setattr(tc, "load_content_cache", lambda p: {})
        monkeypatch.setattr(tc, "load_tree_cache", lambda p: {})
        monkeypatch.setattr(
            tc, "save_meta_cache",
            lambda c, p: calls.__setitem__("saved_meta", calls["saved_meta"] + 1),
        )
        monkeypatch.setattr(
            tc, "save_content_cache",
            lambda c, p: calls.__setitem__("saved_content", calls["saved_content"] + 1),
        )
        monkeypatch.setattr(
            tc, "save_tree_cache",
            lambda c, p: calls.__setitem__("saved_tree", calls["saved_tree"] + 1),
        )
        monkeypatch.setattr(
            tc, "fetch_repo_metadata_cached",
            lambda s, r, c: {
                "stargazers_count": 50,
                "default_branch": "main",
                "pushed_at": "2026-01-01T00:00:00Z",
                "topics": [],
                "description": "",
            },
        )
        monkeypatch.setattr(
            tc, "find_skill_md_paths_cached",
            lambda s, r, p, c: {"SKILL.md": "sha1"},
        )
        monkeypatch.setattr(tc, "fetch_skill_md_cached", lambda *a, **k: "---\nname: t\n---")
        monkeypatch.setattr(tc, "_discover_topic_repos", lambda s, limit=1000, since=None: ["user/skill-a"])
        monkeypatch.setattr(tc, "load_crawl_state", lambda p: {})
        monkeypatch.setattr(tc, "save_crawl_state", lambda state, p: None)

        out = str(tmp_path / "out.jsonl")
        count = tc.run(out)

        assert count == 1
        assert calls["saved_meta"] == 1
        assert calls["saved_content"] == 1
        assert calls["saved_tree"] == 1


# ---------------------------------------------------------------------------
# TestDiscoverPushedFilter — date-filter tests (RED phase)
# ---------------------------------------------------------------------------

class TestDiscoverPushedFilter:
    """Tests for the since= date-filter on _discover_topic_repos."""

    def test_discover_appends_pushed_filter_when_since_set(self):
        """When since is set, every query q should end with pushed:><since>."""
        from crawlers.topic_crawler import _discover_topic_repos

        captured_qs: list[str] = []

        def fake_github_get(session, url, params=None, **kwargs):
            if params:
                captured_qs.append(params.get("q", ""))
            return {"items": []}

        session = MagicMock()
        with patch("crawlers.topic_crawler.github_get", side_effect=fake_github_get):
            _discover_topic_repos(session, since="2026-01-01T00:00:00Z")

        assert len(captured_qs) > 0
        for q in captured_qs:
            assert q.endswith(" pushed:>2026-01-01T00:00:00Z"), (
                f"Expected q to end with pushed filter, got: {q!r}"
            )

    def test_discover_no_filter_when_since_none(self):
        """When since is None, no query q should contain 'pushed:>'."""
        from crawlers.topic_crawler import _discover_topic_repos

        captured_qs: list[str] = []

        def fake_github_get(session, url, params=None, **kwargs):
            if params:
                captured_qs.append(params.get("q", ""))
            return {"items": []}

        session = MagicMock()
        with patch("crawlers.topic_crawler.github_get", side_effect=fake_github_get):
            _discover_topic_repos(session, since=None)

        assert len(captured_qs) > 0
        for q in captured_qs:
            assert "pushed:>" not in q, (
                f"Expected no pushed filter when since=None, got: {q!r}"
            )

    def test_run_uses_and_saves_discovery_state(self, tmp_path):
        """run() in discover mode reads last_discovery_at and saves updated state."""
        from crawlers.topic_crawler import run

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_batch", return_value={}), \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths_cached") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"), \
             patch("crawlers.topic_crawler.load_tree_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_tree_cache"), \
             patch("crawlers.topic_crawler.load_crawl_state",
                   return_value={"last_discovery_at": "2026-01-01T00:00:00Z"}) as mock_load_state, \
             patch("crawlers.topic_crawler.save_crawl_state") as mock_save_state:

            mock_disc.return_value = []
            mock_meta.return_value = _mock_meta()
            mock_paths.return_value = {}
            mock_skill_md.return_value = None

            out = str(tmp_path / "out.jsonl")
            run(out, mode="discover")

        # _discover_topic_repos was called with since= from state
        mock_disc.assert_called_once()
        call_kwargs = mock_disc.call_args
        assert call_kwargs.kwargs.get("since") == "2026-01-01T00:00:00Z" or (
            len(call_kwargs.args) >= 2 and call_kwargs.args[1] == "2026-01-01T00:00:00Z"
        ), f"Expected since='2026-01-01T00:00:00Z', got call: {call_kwargs}"

        # save_crawl_state was called once and the state has last_discovery_at set
        mock_save_state.assert_called_once()
        saved_state = mock_save_state.call_args.args[0]
        assert "last_discovery_at" in saved_state
        assert saved_state["last_discovery_at"]  # non-empty timestamp


# ---------------------------------------------------------------------------
# TestTopicCrawlerBatchMetaIntegration
# ---------------------------------------------------------------------------

class TestTopicCrawlerBatchMetaIntegration:
    """Verify that batch GraphQL metadata is used (and REST is skipped) when available."""

    def test_batch_result_used_and_rest_not_called(self, tmp_path):
        """When fetch_repo_metadata_batch returns metadata for a repo, the per-repo
        fetch_repo_metadata_cached should NOT be called for that repo."""
        from crawlers.topic_crawler import run

        batch_meta = {
            "user/skill-a": {
                "stargazers_count": 99,
                "pushed_at": "2026-05-01T00:00:00Z",
                "description": "from graphql",
                "default_branch": "main",
                "topics": ["graphql"],
            }
        }

        with patch("crawlers.topic_crawler._discover_topic_repos") as mock_disc, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_batch",
                   return_value=batch_meta) as mock_batch, \
             patch("crawlers.topic_crawler.fetch_repo_metadata_cached") as mock_rest_meta, \
             patch("crawlers.topic_crawler.find_skill_md_paths_cached") as mock_paths, \
             patch("crawlers.topic_crawler.fetch_skill_md_cached") as mock_skill_md, \
             patch("crawlers.topic_crawler.load_meta_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_meta_cache"), \
             patch("crawlers.topic_crawler.load_content_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_content_cache"), \
             patch("crawlers.topic_crawler.load_tree_cache", return_value={}), \
             patch("crawlers.topic_crawler.save_tree_cache"), \
             patch("crawlers.topic_crawler.load_crawl_state", return_value={}), \
             patch("crawlers.topic_crawler.save_crawl_state"):

            mock_disc.return_value = ["user/skill-a"]
            mock_paths.return_value = {"SKILL.md": "sha1"}
            mock_skill_md.return_value = None

            out = str(tmp_path / "out.jsonl")
            count = run(out)

        assert count == 1
        # The batch provided metadata — per-repo REST fallback must NOT have been called
        mock_rest_meta.assert_not_called()

        # Confirm the record used the batch-sourced star count
        import json as _json
        record = _json.loads(Path(out).read_text().strip())
        assert record["raw_metadata"]["stars"] == 99
