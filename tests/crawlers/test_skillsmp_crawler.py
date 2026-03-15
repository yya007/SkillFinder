"""Tests for crawlers/skillsmp_crawler.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from tests.crawlers.conftest import make_github_repo, SAMPLE_SKILL_MD


# ---------------------------------------------------------------------------
# TestBuildRawRecord
# ---------------------------------------------------------------------------

class TestBuildRawRecord:
    """Unit tests for build_raw_record() in skillsmp_crawler."""

    def _make_record(self, repo=None, skill_md_content=SAMPLE_SKILL_MD):
        from crawlers.skillsmp_crawler import build_raw_record
        if repo is None:
            repo = make_github_repo()
        return build_raw_record(repo, skill_md_content=skill_md_content)

    def test_repo_url_is_github_url(self):
        repo = make_github_repo(full_name="user/test-skill")
        record = self._make_record(repo=repo)
        assert record["repo_url"] == "https://github.com/user/test-skill"

    def test_source_is_skillsmp(self):
        record = self._make_record()
        assert record["source"] == "skillsmp"

    def test_name_from_frontmatter_when_available(self):
        record = self._make_record(skill_md_content=SAMPLE_SKILL_MD)
        assert record["name"] == "test-skill"

    def test_returns_none_when_no_frontmatter_name(self):
        # No SKILL.md (or file without name) → not a real skill → return None
        repo = make_github_repo(full_name="user/my-repo")
        record = self._make_record(repo=repo, skill_md_content=None)
        assert record is None

    def test_returns_none_when_skill_md_has_no_name_field(self):
        no_name_md = "---\ndescription: Something.\n---\n# Docs"
        record = self._make_record(skill_md_content=no_name_md)
        assert record is None

    def test_description_from_frontmatter(self):
        record = self._make_record(skill_md_content=SAMPLE_SKILL_MD)
        assert record["description"] == "A test skill for unit testing."

    def test_description_falls_back_to_repo_description(self):
        # SKILL.md has name but no description → fall back to repo description
        name_only_md = "---\nname: my-skill\n---\n# Docs"
        repo = make_github_repo(description="Repo-level description.")
        record = self._make_record(repo=repo, skill_md_content=name_only_md)
        assert record["description"] == "Repo-level description."

    def test_raw_metadata_contains_stars(self):
        repo = make_github_repo(stars=42)
        record = self._make_record(repo=repo)
        assert record["raw_metadata"]["stars"] == 42

    def test_raw_metadata_contains_pushed_at(self):
        repo = make_github_repo(pushed_at="2026-03-01")
        record = self._make_record(repo=repo)
        assert record["raw_metadata"]["pushed_at"] == "2026-03-01"

    def test_record_has_all_required_fields(self):
        record = self._make_record()
        for field in ("repo_url", "name", "description", "source", "raw_metadata"):
            assert field in record, f"Missing required field: {field}"


# ---------------------------------------------------------------------------
# TestRunSkillsmp — unit tests with mocked HTTP
# ---------------------------------------------------------------------------

class TestRunSkillsmp:
    """Unit tests for skillsmp_crawler.run() with all HTTP calls mocked."""

    def _make_search_response(self, repos, total_count=None):
        """Build a fake GitHub Code Search API response."""
        items = []
        for repo in repos:
            items.append({
                "repository": repo,
                "path": "SKILL.md",
                "html_url": repo["html_url"] + "/blob/main/SKILL.md",
            })
        return {
            "total_count": total_count if total_count is not None else len(repos),
            "incomplete_results": False,
            "items": items,
        }

    def _mock_meta(self):
        """Return a fake fetch_repo_metadata result."""
        return {"stargazers_count": 10, "pushed_at": "2026-01-01", "default_branch": "main"}

    def test_respects_limit(self, tmp_path):
        """With limit=2, at most 2 records are written per shard (8 max, 2 unique)."""
        from crawlers.skillsmp_crawler import run

        repos = [make_github_repo(full_name=f"user/skill-{i}") for i in range(5)]
        search_resp = self._make_search_response(repos)

        with patch("crawlers.skillsmp_crawler.make_session"), \
             patch("crawlers.skillsmp_crawler.github_get") as mock_get, \
             patch("crawlers.skillsmp_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.skillsmp_crawler._fetch_skill_md") as mock_fetch_md:

            mock_fetch_md.return_value = SAMPLE_SKILL_MD
            mock_get.return_value = search_resp
            mock_meta.return_value = self._mock_meta()

            out = str(tmp_path / "out.jsonl")
            count = run(out, token="fake-token", limit=2)

        # limit=2 per shard: shard 1 yields 2 unique repos; shards 2-4 see same
        # repos and skip them (cross-shard dedup) → 2 total records written.
        assert count == 2
        lines = Path(out).read_text().strip().splitlines()
        assert len(lines) == 2

    def test_skips_repos_without_github_url(self, tmp_path):
        """If somehow a non-GitHub result appears in search, it's skipped."""
        from crawlers.skillsmp_crawler import run

        # Forge a repo with a non-GitHub html_url
        bad_repo = make_github_repo(full_name="user/bad")
        bad_repo["html_url"] = "https://notgithub.com/user/bad"

        good_repo = make_github_repo(full_name="user/good")
        search_resp = self._make_search_response([bad_repo, good_repo])

        with patch("crawlers.skillsmp_crawler.make_session"), \
             patch("crawlers.skillsmp_crawler.github_get") as mock_get, \
             patch("crawlers.skillsmp_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.skillsmp_crawler._fetch_skill_md") as mock_fetch_md:

            mock_fetch_md.return_value = SAMPLE_SKILL_MD
            mock_get.return_value = search_resp
            mock_meta.return_value = self._mock_meta()

            out = str(tmp_path / "out.jsonl")
            count = run(out, token="fake-token")

        assert count == 1
        record = json.loads(Path(out).read_text().strip())
        assert "good" in record["repo_url"]

    def test_output_records_have_required_fields(self, tmp_path):
        """All output records contain name, repo_url, description, source, raw_metadata."""
        from crawlers.skillsmp_crawler import run

        repos = [make_github_repo(full_name="user/skill-a", description="A nice skill.")]
        search_resp = self._make_search_response(repos)

        with patch("crawlers.skillsmp_crawler.make_session"), \
             patch("crawlers.skillsmp_crawler.github_get") as mock_get, \
             patch("crawlers.skillsmp_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.skillsmp_crawler._fetch_skill_md") as mock_fetch_md:

            mock_fetch_md.return_value = SAMPLE_SKILL_MD
            mock_get.return_value = search_resp
            mock_meta.return_value = self._mock_meta()

            out = str(tmp_path / "out.jsonl")
            run(out, token="fake-token")

        record = json.loads(Path(out).read_text().strip())
        for field in ("name", "repo_url", "description", "source", "raw_metadata"):
            assert field in record, f"Missing field: {field}"

    def test_no_frontmatter_name_writes_to_filter_cache(self, tmp_path):
        """Repos whose SKILL.md has no 'name' field are added to the filter cache."""
        from crawlers.skillsmp_crawler import run

        repos = [make_github_repo(full_name="user/not-a-skill")]
        search_resp = self._make_search_response(repos)
        filter_cache = tmp_path / "filter_cache.jsonl"

        with patch("crawlers.skillsmp_crawler.make_session"), \
             patch("crawlers.skillsmp_crawler.github_get") as mock_get, \
             patch("crawlers.skillsmp_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.skillsmp_crawler._fetch_skill_md") as mock_fetch_md:
            # SKILL.md exists but has no 'name' → build_raw_record returns None
            mock_fetch_md.return_value = "---\ndescription: No name here.\n---\n# Docs"
            mock_get.return_value = search_resp
            mock_meta.return_value = self._mock_meta()

            out = str(tmp_path / "out.jsonl")
            count = run(out, token="fake-token", filter_cache_path=str(filter_cache))

        assert count == 0
        # Filter cache file must exist and contain the repo
        assert filter_cache.exists()
        entry = json.loads(filter_cache.read_text().strip())
        assert entry["url"] == "https://github.com/user/not-a-skill"
        assert entry["reason"] == "not_a_skill"

    def test_repos_in_filter_cache_are_skipped(self, tmp_path):
        """Repos already in the filter cache are not fetched or written."""
        from crawlers.skillsmp_crawler import run

        repos = [make_github_repo(full_name="user/filtered-skill")]
        search_resp = self._make_search_response(repos)

        # Pre-populate filter cache
        filter_cache = tmp_path / "filter_cache.jsonl"
        filter_cache.write_text(
            json.dumps({
                "url": "https://github.com/user/filtered-skill",
                "reason": "not_a_skill",
                "filtered_at": "2026-01-01T00:00:00Z",
            }) + "\n"
        )

        with patch("crawlers.skillsmp_crawler.make_session"), \
             patch("crawlers.skillsmp_crawler.github_get") as mock_get, \
             patch("crawlers.skillsmp_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.skillsmp_crawler._fetch_skill_md") as mock_fetch_md:
            mock_fetch_md.return_value = SAMPLE_SKILL_MD
            mock_get.return_value = search_resp
            mock_meta.return_value = self._mock_meta()

            out = str(tmp_path / "out.jsonl")
            count = run(out, token="fake-token", filter_cache_path=str(filter_cache))

        assert count == 0
        # Repo metadata and SKILL.md should never have been fetched
        mock_meta.assert_not_called()
        mock_fetch_md.assert_not_called()


# ---------------------------------------------------------------------------
# TestSkillsmpCrawlerNetwork — real network calls
# ---------------------------------------------------------------------------

@pytest.mark.network
class TestSkillsmpCrawlerNetwork:
    """Network integration tests — skipped by default; require GITHUB_TOKEN."""

    def test_fetches_real_skills_with_limit(self, tmp_path):
        import os
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            pytest.skip("GITHUB_TOKEN not set")
        from crawlers.skillsmp_crawler import run
        out = str(tmp_path / "out.jsonl")
        count = run(out, token=token, limit=3)
        assert count == 3

    def test_all_records_have_github_repo_url(self, tmp_path):
        import os
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            pytest.skip("GITHUB_TOKEN not set")
        from crawlers.skillsmp_crawler import run
        out = str(tmp_path / "out.jsonl")
        run(out, token=token, limit=5)
        for line in Path(out).read_text().strip().splitlines():
            record = json.loads(line)
            assert record["repo_url"].startswith("https://github.com/"), (
                f"Non-GitHub URL: {record['repo_url']}"
            )

    def test_all_records_have_source_skillsmp(self, tmp_path):
        import os
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            pytest.skip("GITHUB_TOKEN not set")
        from crawlers.skillsmp_crawler import run
        out = str(tmp_path / "out.jsonl")
        run(out, token=token, limit=5)
        for line in Path(out).read_text().strip().splitlines():
            record = json.loads(line)
            assert record["source"] == "skillsmp"

    def test_no_duplicate_repo_urls(self, tmp_path):
        import os
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            pytest.skip("GITHUB_TOKEN not set")
        from crawlers.skillsmp_crawler import run
        out = str(tmp_path / "out.jsonl")
        run(out, token=token, limit=10)
        urls = [
            json.loads(line)["repo_url"]
            for line in Path(out).read_text().strip().splitlines()
        ]
        assert len(urls) == len(set(urls)), "Duplicate repo_urls found in output"
