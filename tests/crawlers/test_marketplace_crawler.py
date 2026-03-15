"""Tests for crawlers/marketplace_crawler.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.crawlers.conftest import SAMPLE_SKILL_MD


# ---------------------------------------------------------------------------
# Sample fixtures
# ---------------------------------------------------------------------------

SAMPLE_MARKETPLACE_JSON = json.dumps([
    {
        "name": "k8s-deployer",
        "description": "Deploy Kubernetes clusters.",
        "path": "skills/k8s-deployer",
    },
    {
        "name": "docker-manager",
        "description": "Manage Docker containers.",
        "path": "skills/docker-manager",
    },
])

SAMPLE_SKILL_MD_WITH_REPO = """---
name: official-skill
description: An official Anthropic skill.
repo_url: https://github.com/anthropics/skills
---
# Official Skill
"""

SAMPLE_SKILL_MD_WITHOUT_REPO = """---
name: community-skill
description: A community skill.
---
# Community Skill
"""


# ---------------------------------------------------------------------------
# TestBuildRawRecord
# ---------------------------------------------------------------------------

class TestBuildRawRecord:
    """Unit tests for marketplace_crawler.build_raw_record()."""

    def _make_entry(
        self,
        name="my-skill",
        description="A skill.",
        path="skills/my-skill",
        parent_repo_url="https://github.com/owner/registry-repo",
        skill_md_content=None,
        official=False,
    ):
        return {
            "name": name,
            "description": description,
            "path": path,
            "parent_repo_url": parent_repo_url,
            "skill_md_content": skill_md_content,
            "official": official,
        }

    def test_uses_frontmatter_repo_url_if_present(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry(skill_md_content=SAMPLE_SKILL_MD_WITH_REPO)
        record = build_raw_record(entry)
        assert record is not None
        # The SKILL.md frontmatter specifies repo_url
        assert record["repo_url"] == "https://github.com/anthropics/skills"

    def test_falls_back_to_parent_repo_url(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry(
            skill_md_content=SAMPLE_SKILL_MD_WITHOUT_REPO,
            parent_repo_url="https://github.com/daymade/claude-code-skills",
        )
        record = build_raw_record(entry)
        assert record is not None
        assert record["repo_url"] == "https://github.com/daymade/claude-code-skills"

    def test_falls_back_to_parent_repo_url_when_no_skill_md(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry(
            skill_md_content=None,
            parent_repo_url="https://github.com/mhattingpete/claude-skills-marketplace",
        )
        record = build_raw_record(entry)
        assert record is not None
        assert record["repo_url"] == "https://github.com/mhattingpete/claude-skills-marketplace"

    def test_source_is_marketplace(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry()
        record = build_raw_record(entry)
        assert record["source"] == "marketplace"

    def test_name_from_frontmatter(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry(skill_md_content=SAMPLE_SKILL_MD_WITH_REPO)
        record = build_raw_record(entry)
        assert record["name"] == "official-skill"

    def test_name_falls_back_to_entry_name(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry(name="fallback-name", skill_md_content=None)
        record = build_raw_record(entry)
        assert record["name"] == "fallback-name"

    def test_official_flag_for_anthropics_repo(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry(
            parent_repo_url="https://github.com/anthropics/skills",
            official=True,
        )
        record = build_raw_record(entry)
        assert record["raw_metadata"]["official"] is True

    def test_non_anthropic_repo_not_official(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry(
            parent_repo_url="https://github.com/daymade/claude-code-skills",
            official=False,
        )
        record = build_raw_record(entry)
        assert record["raw_metadata"]["official"] is False

    def test_required_fields_present(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry()
        record = build_raw_record(entry)
        assert record is not None
        for field in ("repo_url", "name", "description", "source", "raw_metadata"):
            assert field in record, f"Missing field: {field}"

    def test_description_from_frontmatter(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry(skill_md_content=SAMPLE_SKILL_MD_WITH_REPO)
        record = build_raw_record(entry)
        assert record["description"] == "An official Anthropic skill."

    def test_description_falls_back_to_entry_description(self):
        from crawlers.marketplace_crawler import build_raw_record
        entry = self._make_entry(description="Entry description.", skill_md_content=None)
        record = build_raw_record(entry)
        assert record["description"] == "Entry description."


# ---------------------------------------------------------------------------
# TestRunMarketplace — unit tests with mocked HTTP
# ---------------------------------------------------------------------------

class TestRunMarketplace:
    """Unit tests for marketplace_crawler.run() with HTTP mocked out."""

    def _make_marketplace_entries(self, n=3, parent_repo="anthropics/skills", official=True):
        return [
            {
                "name": f"skill-{i}",
                "description": f"Description {i}.",
                "path": f"skills/skill-{i}",
                "parent_repo_url": f"https://github.com/{parent_repo}",
                "skill_md_content": None,
                "official": official,
            }
            for i in range(n)
        ]

    def test_respects_limit(self, tmp_path):
        from crawlers.marketplace_crawler import run

        with patch("crawlers.marketplace_crawler.list_skill_dirs") as mock_list:
            mock_list.return_value = self._make_marketplace_entries(n=10)
            out = str(tmp_path / "out.jsonl")
            count = run(out, limit=5)

        assert count == 5

    def test_output_has_required_fields(self, tmp_path):
        from crawlers.marketplace_crawler import run

        with patch("crawlers.marketplace_crawler.list_skill_dirs") as mock_list:
            mock_list.return_value = self._make_marketplace_entries(n=1)
            out = str(tmp_path / "out.jsonl")
            run(out)

        record = json.loads(Path(out).read_text().strip())
        for field in ("repo_url", "name", "description", "source", "raw_metadata"):
            assert field in record

    def test_source_is_marketplace_for_all_records(self, tmp_path):
        from crawlers.marketplace_crawler import run

        with patch("crawlers.marketplace_crawler.list_skill_dirs") as mock_list:
            mock_list.return_value = self._make_marketplace_entries(n=3)
            out = str(tmp_path / "out.jsonl")
            run(out)

        for line in Path(out).read_text().strip().splitlines():
            record = json.loads(line)
            assert record["source"] == "marketplace"

    def test_list_skill_dirs_delegates_to_find_skill_md_paths(self):
        """list_skill_dirs() uses the shared find_skill_md_paths helper (not its own tree walk)."""
        from crawlers.marketplace_crawler import list_skill_dirs

        mock_session = MagicMock()
        with patch("crawlers.marketplace_crawler.find_skill_md_paths") as mock_find, \
             patch("crawlers.marketplace_crawler.fetch_skill_content") as mock_content:
            mock_find.return_value = ["foo/SKILL.md"]
            mock_content.return_value = None

            entries = list_skill_dirs(mock_session, "owner/repo")

        mock_find.assert_called_once_with(mock_session, "owner/repo")
        assert len(entries) == 1
        assert entries[0]["path"] == "foo/SKILL.md"
        assert entries[0]["parent_repo_url"] == "https://github.com/owner/repo"


# ---------------------------------------------------------------------------
# TestMarketplaceCrawlerNetwork — real network calls
# ---------------------------------------------------------------------------

@pytest.mark.network
class TestMarketplaceCrawlerNetwork:
    """Network integration tests for Marketplace crawler — skipped by default."""

    def test_finds_skills_in_anthropics_skills_repo(self):
        import os
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            pytest.skip("GITHUB_TOKEN not set")
        from crawlers.marketplace_crawler import list_skill_dirs
        from crawlers.base import make_session
        session = make_session(token=token)
        entries = list_skill_dirs(session, "anthropics/skills")
        assert len(entries) > 0, "Expected at least one skill in anthropics/skills"

    def test_all_skills_have_github_repo_url(self, tmp_path):
        import os
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            pytest.skip("GITHUB_TOKEN not set")
        from crawlers.marketplace_crawler import run
        out = str(tmp_path / "out.jsonl")
        run(out, token=token, limit=10)
        for line in Path(out).read_text().strip().splitlines():
            record = json.loads(line)
            assert record["repo_url"].startswith("https://github.com/"), (
                f"Non-GitHub URL: {record['repo_url']}"
            )

    def test_run_with_limit(self, tmp_path):
        import os
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            pytest.skip("GITHUB_TOKEN not set")
        from crawlers.marketplace_crawler import run
        out = str(tmp_path / "out.jsonl")
        count = run(out, token=token, limit=5)
        assert count <= 5
        lines = Path(out).read_text().strip().splitlines()
        assert len(lines) == count

    def test_anthropics_skills_marked_official(self, tmp_path):
        import os
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            pytest.skip("GITHUB_TOKEN not set")
        from crawlers.marketplace_crawler import run
        out = str(tmp_path / "out.jsonl")
        run(out, token=token, limit=5)
        for line in Path(out).read_text().strip().splitlines():
            record = json.loads(line)
            if "anthropics" in record["repo_url"]:
                assert record["raw_metadata"]["official"] is True, (
                    f"anthropics skill not marked official: {record['repo_url']}"
                )
