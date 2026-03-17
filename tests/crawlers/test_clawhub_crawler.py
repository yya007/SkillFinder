"""Tests for crawlers/clawhub_crawler.py."""
import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

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
# TestFetchAwesomeReadme
# ---------------------------------------------------------------------------

class TestFetchAwesomeReadme:
    """Unit tests for fetch_awesome_readme()."""

    def test_uses_download_url_when_present(self):
        from crawlers.clawhub_crawler import fetch_awesome_readme

        mock_session = MagicMock()
        raw_content = "# Awesome OpenClaw Skills\n\n## Tools\n"
        raw_resp = MagicMock()
        raw_resp.text = raw_content
        raw_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = raw_resp

        with patch("crawlers.clawhub_crawler.github_get") as mock_get:
            mock_get.return_value = {
                "download_url": "https://raw.githubusercontent.com/VoltAgent/awesome-openclaw-skills/main/README.md",
                "encoding": "base64",
                "content": base64.b64encode(b"should not be decoded").decode(),
            }
            content = fetch_awesome_readme(mock_session, "VoltAgent/awesome-openclaw-skills", "README.md")

        assert content == raw_content
        # session.get called with the download_url
        mock_session.get.assert_called_once()
        call_url = mock_session.get.call_args[0][0]
        assert "raw.githubusercontent.com" in call_url

    def test_falls_back_to_base64_when_no_download_url(self):
        from crawlers.clawhub_crawler import fetch_awesome_readme

        readme_text = "# Awesome OpenClaw Skills\n\n## Tools\n"
        encoded = base64.b64encode(readme_text.encode()).decode()
        mock_session = MagicMock()

        with patch("crawlers.clawhub_crawler.github_get") as mock_get:
            mock_get.return_value = {
                "encoding": "base64",
                "content": encoded,
                # no download_url key
            }
            content = fetch_awesome_readme(mock_session, "VoltAgent/awesome-openclaw-skills", "README.md")

        assert content == readme_text
        # session.get should NOT have been called (no raw download needed)
        mock_session.get.assert_not_called()

    def test_raises_on_unknown_encoding_and_no_download_url(self):
        from crawlers.clawhub_crawler import fetch_awesome_readme

        mock_session = MagicMock()
        with patch("crawlers.clawhub_crawler.github_get") as mock_get:
            mock_get.return_value = {
                "encoding": "gzip",  # unexpected
                "content": "compressed",
            }
            with pytest.raises(RuntimeError, match="Unexpected encoding"):
                fetch_awesome_readme(mock_session, "VoltAgent/awesome-openclaw-skills", "README.md")


# ---------------------------------------------------------------------------
# TestRunClawhub — unit tests with mocked HTTP
# ---------------------------------------------------------------------------

class TestRunClawhub:
    """Unit tests for clawhub_crawler.run() with HTTP mocked out."""

    def _mock_meta(self):
        """Return a fake fetch_repo_metadata result."""
        return {"stargazers_count": 5, "pushed_at": "2026-01-01", "default_branch": "main"}

    def _patch_run(self, find_paths_return=None):
        """Return a context-manager stack that patches all HTTP calls in run()."""
        if find_paths_return is None:
            find_paths_return = ["SKILL.md"]
        return (
            patch("crawlers.clawhub_crawler.fetch_awesome_readme"),
            patch("crawlers.clawhub_crawler.fetch_repo_metadata"),
            patch("crawlers.clawhub_crawler.find_skill_md_paths"),
            patch("crawlers.clawhub_crawler._fetch_skill_md"),
        )

    def test_respects_limit(self, tmp_path):
        from crawlers.clawhub_crawler import run

        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch, \
             patch("crawlers.clawhub_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.clawhub_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.clawhub_crawler._fetch_skill_md") as mock_skill_md:
            mock_fetch.return_value = SAMPLE_AWESOME_README
            mock_meta.return_value = self._mock_meta()
            mock_paths.return_value = {"SKILL.md": ""}
            mock_skill_md.return_value = None
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
        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch, \
             patch("crawlers.clawhub_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.clawhub_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.clawhub_crawler._fetch_skill_md") as mock_skill_md:
            mock_fetch.return_value = readme_with_non_github
            mock_meta.return_value = self._mock_meta()
            mock_paths.return_value = {"SKILL.md": ""}
            mock_skill_md.return_value = None
            out = str(tmp_path / "out.jsonl")
            count = run(out)

        assert count == 2
        lines = Path(out).read_text().strip().splitlines()
        for line in lines:
            record = json.loads(line)
            assert record["repo_url"].startswith("https://github.com/")

    def test_output_has_required_fields(self, tmp_path):
        from crawlers.clawhub_crawler import run

        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch, \
             patch("crawlers.clawhub_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.clawhub_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.clawhub_crawler._fetch_skill_md") as mock_skill_md:
            mock_fetch.return_value = SAMPLE_AWESOME_README
            mock_meta.return_value = self._mock_meta()
            mock_paths.return_value = {"SKILL.md": ""}
            mock_skill_md.return_value = None
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
            "raw_metadata": {"categories": ["DevOps"]},
        }
        out.write_text(json.dumps(existing) + "\n")

        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch, \
             patch("crawlers.clawhub_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.clawhub_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.clawhub_crawler._fetch_skill_md") as mock_skill_md:
            mock_fetch.return_value = SAMPLE_AWESOME_README
            mock_meta.return_value = self._mock_meta()
            mock_paths.return_value = {"SKILL.md": ""}
            mock_skill_md.return_value = None
            count = run(str(out), resume=True)

        # SAMPLE_AWESOME_README has 3 GitHub entries; 1 was already written
        assert count == 2

    def test_subtree_hint_skips_trees_api(self, tmp_path):
        from crawlers.clawhub_crawler import run

        readme_with_subtree = (
            "## Category\n\n"
            "- [skill-a](https://github.com/openclaw/skills/tree/main/skills/skill-a)"
            " — First subtree skill.\n"
        )
        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch, \
             patch("crawlers.clawhub_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.clawhub_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.clawhub_crawler._fetch_skill_md") as mock_skill_md:
            mock_fetch.return_value = readme_with_subtree
            mock_meta.return_value = self._mock_meta()
            mock_skill_md.return_value = None
            out = str(tmp_path / "out.jsonl")
            count = run(out)

        mock_paths.assert_not_called()  # subtree hint → Trees API never called
        assert count == 1
        record = json.loads(Path(out).read_text().strip())
        assert "skills/skill-a/SKILL.md" in record["raw_metadata"]["skill_md_url"]

    def test_monorepo_dedup_uses_skill_path_not_repo_url(self, tmp_path):
        from crawlers.clawhub_crawler import run

        # Two items with different subtree hints in the same monorepo
        readme_monorepo = (
            "## Category\n\n"
            "- [skill-a](https://github.com/openclaw/skills/tree/main/skills/skill-a)"
            " — First skill.\n"
            "- [skill-b](https://github.com/openclaw/skills/tree/main/skills/skill-b)"
            " — Second skill.\n"
        )
        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch, \
             patch("crawlers.clawhub_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.clawhub_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.clawhub_crawler._fetch_skill_md") as mock_skill_md:
            mock_fetch.return_value = readme_monorepo
            mock_meta.return_value = self._mock_meta()
            mock_skill_md.return_value = None
            out = str(tmp_path / "out.jsonl")
            count = run(out)

        # Both skills written — dedup key is (repo_url, skill_path), not repo_url alone
        assert count == 2
        mock_paths.assert_not_called()  # subtree hints bypass Trees API

        lines = Path(out).read_text().strip().splitlines()
        records = [json.loads(l) for l in lines]
        # Same base repo_url but different skill_md_url
        assert records[0]["repo_url"] == records[1]["repo_url"]
        assert records[0]["raw_metadata"]["skill_md_url"] != records[1]["raw_metadata"]["skill_md_url"]
        # fetch_repo_metadata called only once (cached on second item)
        mock_meta.assert_called_once()

    def test_resume_monorepo_uses_skill_md_url_key(self, tmp_path):
        from crawlers.clawhub_crawler import run

        readme_monorepo = (
            "## Category\n\n"
            "- [skill-a](https://github.com/openclaw/skills/tree/main/skills/skill-a)"
            " — First skill.\n"
            "- [skill-b](https://github.com/openclaw/skills/tree/main/skills/skill-b)"
            " — Second skill.\n"
        )
        # Pre-populate with skill-a already crawled (identified by skill_md_url)
        out = tmp_path / "out.jsonl"
        existing = {
            "repo_url": "https://github.com/openclaw/skills",
            "name": "skill-a",
            "description": "Already crawled.",
            "source": "clawhub",
            "raw_metadata": {
                "skill_md_url": "https://github.com/openclaw/skills/blob/main/skills/skill-a/SKILL.md",
                "categories": [],
                "stars": 5,
                "pushed_at": "2026-01-01",
                "platforms": ["openclaw"],
            },
        }
        out.write_text(json.dumps(existing) + "\n")

        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch, \
             patch("crawlers.clawhub_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.clawhub_crawler._fetch_skill_md") as mock_skill_md:
            mock_fetch.return_value = readme_monorepo
            mock_meta.return_value = self._mock_meta()
            mock_skill_md.return_value = None
            count = run(str(out), resume=True)

        # Only skill-b is new; skill-a's skill_md_url already present in output
        assert count == 1
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 2  # 1 existing + 1 new
        new_record = json.loads(lines[-1])
        assert "skill-b" in new_record["raw_metadata"]["skill_md_url"]


# ---------------------------------------------------------------------------
# TestDiscoverOpenclawRepos
# ---------------------------------------------------------------------------

class TestDiscoverOpenclawRepos:
    """Unit tests for _discover_openclaw_repos()."""

    def test_returns_list_of_full_names(self):
        from crawlers.clawhub_crawler import _discover_openclaw_repos

        page1 = {"items": [{"full_name": "openclaw/skill-a"}, {"full_name": "openclaw/skill-b"}]}

        with patch("crawlers.clawhub_crawler.github_get") as mock_get, \
             patch("crawlers.clawhub_crawler.make_session") as mock_session:
            mock_get.return_value = page1
            result = _discover_openclaw_repos(mock_session.return_value, limit=10)

        assert "openclaw/skill-a" in result
        assert "openclaw/skill-b" in result

    def test_deduplicates_across_queries(self):
        from crawlers.clawhub_crawler import _discover_openclaw_repos

        # Same repo appears in two different query results
        both_return = {"items": [{"full_name": "openclaw/shared-skill"}]}

        with patch("crawlers.clawhub_crawler.github_get") as mock_get, \
             patch("crawlers.clawhub_crawler.make_session") as mock_session:
            mock_get.return_value = both_return
            result = _discover_openclaw_repos(mock_session.return_value, limit=100)

        assert result.count("openclaw/shared-skill") == 1

    def test_respects_limit(self):
        from crawlers.clawhub_crawler import _discover_openclaw_repos

        items_100 = {"items": [{"full_name": f"user/skill-{i}"} for i in range(100)]}
        items_empty = {"items": []}

        with patch("crawlers.clawhub_crawler.github_get") as mock_get, \
             patch("crawlers.clawhub_crawler.make_session") as mock_session:
            mock_get.side_effect = [items_100, items_empty] * 10
            result = _discover_openclaw_repos(mock_session.return_value, limit=5)

        assert len(result) <= 5

    def test_handles_api_error_gracefully(self):
        from crawlers.clawhub_crawler import _discover_openclaw_repos

        with patch("crawlers.clawhub_crawler.github_get") as mock_get, \
             patch("crawlers.clawhub_crawler.make_session") as mock_session:
            mock_get.side_effect = RuntimeError("rate limited")
            result = _discover_openclaw_repos(mock_session.return_value, limit=10)

        assert result == []

    def test_run_includes_discovered_repos_not_in_awesome_list(self, tmp_path):
        """Repos found via org search that are not in the awesome list are crawled."""
        from crawlers.clawhub_crawler import run

        discovered_skill_md = "---\nname: discovered-skill\ndescription: Found via org search.\n---\n"
        meta = {"stargazers_count": 10, "pushed_at": "2026-01-01", "default_branch": "main"}

        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch, \
             patch("crawlers.clawhub_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.clawhub_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.clawhub_crawler._fetch_skill_md") as mock_skill_md, \
             patch("crawlers.clawhub_crawler._discover_openclaw_repos") as mock_discover:
            mock_fetch.return_value = "# Awesome List\n\nNo skills.\n"
            mock_meta.return_value = meta
            mock_paths.return_value = {"SKILL.md": ""}
            mock_skill_md.return_value = discovered_skill_md
            mock_discover.return_value = ["openclaw/discovered-skill"]

            out = str(tmp_path / "out.jsonl")
            # token required: discovery is skipped when token is None
            count = run(out, token="fake-token")

        assert count == 1
        record = json.loads(Path(out).read_text().strip())
        assert record["name"] == "discovered-skill"
        assert record["source"] == "clawhub"

    def test_run_does_not_double_count_awesome_list_repos(self, tmp_path):
        """Repos already crawled via the awesome list are not re-crawled via discovery."""
        from crawlers.clawhub_crawler import run

        meta = {"stargazers_count": 5, "pushed_at": "2026-01-01", "default_branch": "main"}

        with patch("crawlers.clawhub_crawler.fetch_awesome_readme") as mock_fetch, \
             patch("crawlers.clawhub_crawler.fetch_repo_metadata") as mock_meta, \
             patch("crawlers.clawhub_crawler.find_skill_md_paths") as mock_paths, \
             patch("crawlers.clawhub_crawler._fetch_skill_md") as mock_skill_md, \
             patch("crawlers.clawhub_crawler._discover_openclaw_repos") as mock_discover:
            mock_fetch.return_value = SAMPLE_AWESOME_README  # k8s-deployer, docker-manager, test-runner
            mock_meta.return_value = meta
            mock_paths.return_value = {"SKILL.md": ""}
            mock_skill_md.return_value = None
            mock_discover.return_value = ["user/k8s-deployer", "user/docker-manager"]

            out = str(tmp_path / "out.jsonl")
            count = run(out, token="fake-token")

        # Only the 3 unique skills from the awesome list — no duplicates from discovery
        assert count == 3


# ---------------------------------------------------------------------------
# TestClawhubCrawlerNetwork — real network calls
# ---------------------------------------------------------------------------

@pytest.mark.network
class TestClawhubCrawlerNetwork:
    """Network integration tests — skipped by default."""

    def test_fetches_real_awesome_list(self, github_session):
        from crawlers.clawhub_crawler import fetch_awesome_readme, parse_awesome_readme, AWESOME_LIST_REPOS
        repo, path = AWESOME_LIST_REPOS[0]
        content = fetch_awesome_readme(github_session, repo, path)
        skills = parse_awesome_readme(content)
        assert len(skills) > 10, f"Expected > 10 entries, got {len(skills)}"

    def test_all_records_have_github_url(self, github_session):
        from crawlers.clawhub_crawler import fetch_awesome_readme, parse_awesome_readme, build_raw_record, AWESOME_LIST_REPOS
        repo, path = AWESOME_LIST_REPOS[0]
        content = fetch_awesome_readme(github_session, repo, path)
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


# ---------------------------------------------------------------------------
# TestParseFrontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    """Unit tests for clawhub_crawler._parse_frontmatter."""

    def test_plain_frontmatter(self):
        from crawlers.clawhub_crawler import _parse_frontmatter
        result = _parse_frontmatter("---\nname: foo\n---\n")
        assert result == {"name": "foo"}

    def test_html_comment_before_frontmatter(self):
        from crawlers.clawhub_crawler import _parse_frontmatter
        result = _parse_frontmatter("<!-- copyright -->\n---\nname: bar\n---\n")
        assert result == {"name": "bar"}

    def test_multiple_html_comments(self):
        from crawlers.clawhub_crawler import _parse_frontmatter
        content = "<!-- first comment -->\n<!-- second comment -->\n---\nname: baz\n---\n"
        result = _parse_frontmatter(content)
        assert result == {"name": "baz"}

    def test_empty_string(self):
        from crawlers.clawhub_crawler import _parse_frontmatter
        assert _parse_frontmatter("") == {}

    def test_no_frontmatter(self):
        from crawlers.clawhub_crawler import _parse_frontmatter
        assert _parse_frontmatter("# Just a heading\n\nSome content.") == {}

    def test_html_comment_only(self):
        from crawlers.clawhub_crawler import _parse_frontmatter
        assert _parse_frontmatter("<!-- comment -->") == {}


# ---------------------------------------------------------------------------
# TestFetchSkillMdClawhub
# ---------------------------------------------------------------------------

class TestFetchSkillMdClawhub:
    """Unit tests for clawhub_crawler._fetch_skill_md with a mocked session."""

    def _make_response(self, status_code=200, json_data=None):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data or {}
        return mock_resp

    def test_symlink_resolved(self):
        import base64
        from crawlers.clawhub_crawler import _fetch_skill_md

        encoded = base64.b64encode(b"content").decode() + "\n"
        session = MagicMock()
        symlink_resp = self._make_response(200, {"type": "symlink", "target": "other/SKILL.md"})
        file_resp = self._make_response(200, {"type": "file", "content": encoded})
        session.get.side_effect = [symlink_resp, file_resp]

        result = _fetch_skill_md(session, "owner/repo", "SKILL.md", "main", _depth=0)
        assert result == "content"

    def test_symlink_depth_limit(self):
        from crawlers.clawhub_crawler import _fetch_skill_md

        session = MagicMock()
        session.get.return_value = self._make_response(200, {"type": "symlink", "target": "other/SKILL.md"})

        result = _fetch_skill_md(session, "owner/repo", "SKILL.md", "main", _depth=1)
        assert result is None

    def test_non_200_returns_none(self):
        from crawlers.clawhub_crawler import _fetch_skill_md

        session = MagicMock()
        session.get.return_value = self._make_response(404, {})

        result = _fetch_skill_md(session, "owner/repo", "SKILL.md", "main", _depth=0)
        assert result is None

    def test_normal_file_returned(self):
        import base64
        from crawlers.clawhub_crawler import _fetch_skill_md

        encoded = base64.b64encode(b"hello world").decode() + "\n"
        session = MagicMock()
        session.get.return_value = self._make_response(200, {"type": "file", "content": encoded})

        result = _fetch_skill_md(session, "owner/repo", "SKILL.md", "main", _depth=0)
        assert result == "hello world"
