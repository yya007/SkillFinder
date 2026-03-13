"""Tests for crawlers/clawhub_crawler.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.crawlers.conftest import SAMPLE_AWESOME_README


# ---------------------------------------------------------------------------
# TestParseAwesomeReadme
# ---------------------------------------------------------------------------

class TestParseAwesomeReadme:
    """Unit tests for parse_awesome_readme()."""

    def test_extracts_skill_names(self):
        from crawlers.clawhub_crawler import parse_awesome_readme
        skills = parse_awesome_readme(SAMPLE_AWESOME_README)
        names = [s["name"] for s in skills]
        assert "k8s-deployer" in names
        assert "docker-manager" in names
        assert "test-runner" in names

    def test_extracts_github_urls(self):
        from crawlers.clawhub_crawler import parse_awesome_readme
        skills = parse_awesome_readme(SAMPLE_AWESOME_README)
        urls = [s["url"] for s in skills]
        assert "https://github.com/user/k8s-deployer" in urls
        assert "https://github.com/user/docker-manager" in urls

    def test_extracts_descriptions(self):
        from crawlers.clawhub_crawler import parse_awesome_readme
        skills = parse_awesome_readme(SAMPLE_AWESOME_README)
        by_name = {s["name"]: s for s in skills}
        assert by_name["k8s-deployer"]["description"] == "Deploy Kubernetes clusters"
        assert by_name["docker-manager"]["description"] == "Manage Docker containers"

    def test_assigns_category_from_heading(self):
        from crawlers.clawhub_crawler import parse_awesome_readme
        skills = parse_awesome_readme(SAMPLE_AWESOME_README)
        by_name = {s["name"]: s for s in skills}
        assert by_name["k8s-deployer"]["category"] == "DevOps"
        assert by_name["docker-manager"]["category"] == "DevOps"
        assert by_name["test-runner"]["category"] == "Testing"

    def test_skips_non_skill_lines(self):
        from crawlers.clawhub_crawler import parse_awesome_readme
        content = """# Awesome List

## Tools

Not a skill line.
Some other text.

- [real-skill](https://github.com/user/real-skill) — Does something useful.
"""
        skills = parse_awesome_readme(content)
        assert len(skills) == 1
        assert skills[0]["name"] == "real-skill"

    def test_handles_em_dash_separator(self):
        from crawlers.clawhub_crawler import parse_awesome_readme
        # em-dash (—)
        content = "## Tools\n\n- [skill-a](https://github.com/u/a) — With em-dash\n"
        skills = parse_awesome_readme(content)
        assert len(skills) == 1
        assert skills[0]["description"] == "With em-dash"

    def test_handles_hyphen_separator(self):
        from crawlers.clawhub_crawler import parse_awesome_readme
        # regular hyphen (-)
        content = "## Tools\n\n- [skill-b](https://github.com/u/b) - With hyphen\n"
        skills = parse_awesome_readme(content)
        assert len(skills) == 1
        assert skills[0]["description"] == "With hyphen"

    def test_handles_multiple_categories(self):
        from crawlers.clawhub_crawler import parse_awesome_readme
        content = """## CategoryA

- [skill-1](https://github.com/u/s1) — First skill.

## CategoryB

- [skill-2](https://github.com/u/s2) — Second skill.
- [skill-3](https://github.com/u/s3) — Third skill.
"""
        skills = parse_awesome_readme(content)
        assert len(skills) == 3
        by_name = {s["name"]: s for s in skills}
        assert by_name["skill-1"]["category"] == "CategoryA"
        assert by_name["skill-2"]["category"] == "CategoryB"
        assert by_name["skill-3"]["category"] == "CategoryB"

    def test_empty_readme_returns_empty_list(self):
        from crawlers.clawhub_crawler import parse_awesome_readme
        assert parse_awesome_readme("") == []

    def test_readme_with_no_skills_returns_empty_list(self):
        from crawlers.clawhub_crawler import parse_awesome_readme
        content = "# Awesome Skills\n\nNo skills here yet.\n"
        assert parse_awesome_readme(content) == []


# ---------------------------------------------------------------------------
# TestBuildRawRecord
# ---------------------------------------------------------------------------

class TestBuildRawRecord:
    """Unit tests for clawhub_crawler.build_raw_record()."""

    def _make_item(self, name="my-skill", url="https://github.com/user/my-skill",
                   description="A skill.", category="DevOps"):
        return {"name": name, "url": url, "description": description, "category": category}

    def test_repo_url_is_normalized_github_url(self):
        from crawlers.clawhub_crawler import build_raw_record
        item = self._make_item(url="https://github.com/User/MySkill.git")
        record = build_raw_record(item)
        assert record is not None
        assert record["repo_url"] == "https://github.com/user/myskill"

    def test_returns_none_for_non_github_url(self):
        from crawlers.clawhub_crawler import build_raw_record
        item = self._make_item(url="https://gitlab.com/user/skill")
        assert build_raw_record(item) is None

    def test_source_is_clawhub(self):
        from crawlers.clawhub_crawler import build_raw_record
        item = self._make_item()
        record = build_raw_record(item)
        assert record["source"] == "clawhub"

    def test_category_in_raw_metadata(self):
        from crawlers.clawhub_crawler import build_raw_record
        item = self._make_item(category="Security")
        record = build_raw_record(item)
        assert "Security" in record["raw_metadata"]["categories"]

    def test_safety_scan_default_unknown(self):
        from crawlers.clawhub_crawler import build_raw_record
        item = self._make_item()
        record = build_raw_record(item)
        assert record["raw_metadata"]["safety_scan"] == "unknown"

    def test_safety_scan_custom_value(self):
        from crawlers.clawhub_crawler import build_raw_record
        item = self._make_item()
        record = build_raw_record(item, safety_scan="clean")
        assert record["raw_metadata"]["safety_scan"] == "clean"

    def test_description_from_item(self):
        from crawlers.clawhub_crawler import build_raw_record
        item = self._make_item(description="Automates deployments.")
        record = build_raw_record(item)
        assert record["description"] == "Automates deployments."

    def test_name_from_item(self):
        from crawlers.clawhub_crawler import build_raw_record
        item = self._make_item(name="my-custom-skill")
        record = build_raw_record(item)
        assert record["name"] == "my-custom-skill"

    def test_empty_category_yields_empty_categories_list(self):
        from crawlers.clawhub_crawler import build_raw_record
        item = self._make_item(category="")
        record = build_raw_record(item)
        assert record["raw_metadata"]["categories"] == []


# ---------------------------------------------------------------------------
# TestRunClawhub — unit tests with mocked HTTP
# ---------------------------------------------------------------------------

class TestRunClawhub:
    """Unit tests for clawhub_crawler.run() with HTTP mocked out."""

    def test_respects_limit(self, tmp_path):
        from crawlers.clawhub_crawler import run

        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch:
            mock_fetch.return_value = SAMPLE_AWESOME_README
            out = str(tmp_path / "out.jsonl")
            count = run(out, limit=2)

        assert count == 2
        lines = Path(out).read_text().strip().splitlines()
        assert len(lines) == 2

    def test_skips_non_github_entries(self, tmp_path):
        from crawlers.clawhub_crawler import run

        readme_with_non_github = """## Tools

- [github-skill](https://github.com/user/github-skill) — Good skill.
- [gitlab-skill](https://gitlab.com/user/gitlab-skill) — Bad source.
- [another-github](https://github.com/user/another-github) — Also good.
"""
        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch:
            mock_fetch.return_value = readme_with_non_github
            out = str(tmp_path / "out.jsonl")
            count = run(out)

        assert count == 2
        lines = Path(out).read_text().strip().splitlines()
        for line in lines:
            record = json.loads(line)
            assert record["repo_url"].startswith("https://github.com/")

    def test_output_has_required_fields(self, tmp_path):
        from crawlers.clawhub_crawler import run

        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch:
            mock_fetch.return_value = SAMPLE_AWESOME_README
            out = str(tmp_path / "out.jsonl")
            run(out, limit=1)

        record = json.loads(Path(out).read_text().strip())
        for field in ("repo_url", "name", "description", "source", "raw_metadata"):
            assert field in record, f"Missing field: {field}"

    def test_resume_skips_existing_urls(self, tmp_path):
        from crawlers.clawhub_crawler import run

        # Pre-populate output with one of the skills
        out = tmp_path / "out.jsonl"
        existing = {
            "repo_url": "https://github.com/user/k8s-deployer",
            "name": "k8s-deployer",
            "description": "Already crawled.",
            "source": "clawhub",
            "raw_metadata": {"categories": ["DevOps"], "safety_scan": "unknown"},
        }
        out.write_text(json.dumps(existing) + "\n")

        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch:
            mock_fetch.return_value = SAMPLE_AWESOME_README
            count = run(str(out), resume=True)

        # SAMPLE_AWESOME_README has 3 GitHub entries; 1 was already written
        assert count == 2


# ---------------------------------------------------------------------------
# TestClawhubCrawlerNetwork — real network calls
# ---------------------------------------------------------------------------

@pytest.mark.network
class TestClawhubCrawlerNetwork:
    """Network integration tests — skipped by default."""

    def test_fetches_real_awesome_list(self, github_session):
        from crawlers.clawhub_crawler import fetch_awesome_readme, parse_awesome_readme
        content = fetch_awesome_readme(github_session)
        skills = parse_awesome_readme(content)
        assert len(skills) > 10, f"Expected > 10 entries, got {len(skills)}"

    def test_all_records_have_github_url(self, github_session):
        from crawlers.clawhub_crawler import fetch_awesome_readme, parse_awesome_readme, build_raw_record
        content = fetch_awesome_readme(github_session)
        items = parse_awesome_readme(content)
        records = [build_raw_record(item) for item in items if build_raw_record(item) is not None]
        for record in records:
            assert record["repo_url"].startswith("https://github.com/")

    def test_run_with_limit(self, tmp_path):
        from crawlers.clawhub_crawler import run
        out = str(tmp_path / "out.jsonl")
        count = run(out, limit=5)
        assert count <= 5
        lines = Path(out).read_text().strip().splitlines()
        assert len(lines) == count
