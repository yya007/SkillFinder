"""
Unit tests for scripts/fetch_skill.py

HTTP calls are patched throughout. Tests verify:
  - parse_github_url: extracts owner/repo from all URL formats
  - candidate_urls: correct priority-ordered URL list
  - fetch_skill_md: tries URLs in order, returns first 200
  - parse_frontmatter: YAML extraction, body separation
  - fetch_and_output: file writing and string return
  - FetchError: raised when all URLs fail, contains attempted URLs
"""
from unittest.mock import MagicMock, patch

import pytest

from scripts.fetch_skill import (
    FetchError,
    candidate_urls,
    fetch_and_output,
    fetch_skill_md,
    parse_frontmatter,
    parse_github_url,
)


# ---------------------------------------------------------------------------
# parse_github_url
# ---------------------------------------------------------------------------

class TestParseGithubUrl:
    def test_standard_https_url(self):
        owner, repo = parse_github_url("https://github.com/user/my-skill")
        assert owner == "user"
        assert repo == "my-skill"

    def test_url_with_git_suffix(self):
        owner, repo = parse_github_url("https://github.com/user/my-skill.git")
        assert owner == "user"
        assert repo == "my-skill"

    def test_url_with_tree_path(self):
        owner, repo = parse_github_url("https://github.com/user/my-skill/tree/main/subdir")
        assert owner == "user"
        assert repo == "my-skill"

    def test_url_with_trailing_slash(self):
        owner, repo = parse_github_url("https://github.com/user/my-skill/")
        assert owner == "user"
        assert repo == "my-skill"

    def test_url_with_uppercase(self):
        owner, repo = parse_github_url("https://GitHub.com/User/My-Skill")
        assert owner.lower() == "user"
        assert repo.lower() == "my-skill"

    def test_raises_on_non_github_url(self):
        with pytest.raises(ValueError):
            parse_github_url("https://gitlab.com/user/repo")

    def test_raises_on_url_without_repo(self):
        with pytest.raises(ValueError):
            parse_github_url("https://github.com/user")

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError):
            parse_github_url("")


# ---------------------------------------------------------------------------
# candidate_urls
# ---------------------------------------------------------------------------

class TestCandidateUrls:
    def test_returns_list_of_strings(self):
        urls = candidate_urls("user", "repo")
        assert isinstance(urls, list)
        assert all(isinstance(u, str) for u in urls)

    def test_first_url_is_main_branch_root(self):
        urls = candidate_urls("user", "repo")
        assert urls[0] == "https://raw.githubusercontent.com/user/repo/main/SKILL.md"

    def test_second_url_is_master_branch_root(self):
        urls = candidate_urls("user", "repo")
        assert urls[1] == "https://raw.githubusercontent.com/user/repo/master/SKILL.md"

    def test_returns_at_least_4_urls(self):
        urls = candidate_urls("user", "repo")
        assert len(urls) >= 4

    def test_all_urls_are_raw_githubusercontent(self):
        urls = candidate_urls("user", "repo")
        for url in urls:
            assert "raw.githubusercontent.com" in url

    def test_all_urls_end_with_skill_md(self):
        urls = candidate_urls("user", "repo")
        for url in urls:
            assert url.endswith("SKILL.md")

    def test_owner_and_repo_in_all_urls(self):
        urls = candidate_urls("myowner", "myrepo")
        for url in urls:
            assert "myowner" in url
            assert "myrepo" in url


# ---------------------------------------------------------------------------
# fetch_skill_md
# ---------------------------------------------------------------------------

class TestFetchSkillMd:
    SAMPLE_CONTENT = "---\nname: test\ndescription: A test skill.\n---\n# Test"

    def test_returns_content_and_resolved_url_on_success(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, text=self.SAMPLE_CONTENT)
            content, resolved_url = fetch_skill_md("https://github.com/user/repo")
        assert content == self.SAMPLE_CONTENT
        assert "raw.githubusercontent.com" in resolved_url

    def test_tries_first_url_first(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, text=self.SAMPLE_CONTENT)
            fetch_skill_md("https://github.com/user/repo")
            first_call_url = mock_get.call_args_list[0][0][0]
            assert "main/SKILL.md" in first_call_url

    def test_falls_back_to_master_if_main_404(self):
        responses = [
            MagicMock(status_code=404),
            MagicMock(status_code=200, text=self.SAMPLE_CONTENT),
        ]
        with patch("requests.get", side_effect=responses):
            content, resolved_url = fetch_skill_md("https://github.com/user/repo")
        assert content == self.SAMPLE_CONTENT
        assert "master" in resolved_url

    def test_raises_fetch_error_when_all_fail(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404)
            with pytest.raises(FetchError) as exc_info:
                fetch_skill_md("https://github.com/user/repo")
            assert len(exc_info.value.attempted_urls) > 0

    def test_fetch_error_contains_all_attempted_urls(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404)
            with pytest.raises(FetchError) as exc_info:
                fetch_skill_md("https://github.com/user/repo")
            # Should have tried at least 4 URLs
            assert len(exc_info.value.attempted_urls) >= 4

    def test_raises_fetch_error_on_network_error(self):
        import requests
        with patch("requests.get", side_effect=requests.ConnectionError()):
            with pytest.raises(FetchError):
                fetch_skill_md("https://github.com/user/repo")


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_extracts_name_and_description(self, sample_skill_md):
        frontmatter, body = parse_frontmatter(sample_skill_md)
        assert frontmatter["name"] == "kubernetes-deployer"
        assert "Deploy" in frontmatter["description"]

    def test_body_does_not_contain_frontmatter_delimiters(self, sample_skill_md):
        frontmatter, body = parse_frontmatter(sample_skill_md)
        assert not body.startswith("---")

    def test_body_contains_markdown_content(self, sample_skill_md):
        frontmatter, body = parse_frontmatter(sample_skill_md)
        assert "# kubernetes-deployer" in body

    def test_triggers_extracted_as_list(self, sample_skill_md):
        frontmatter, _ = parse_frontmatter(sample_skill_md)
        assert isinstance(frontmatter.get("triggers", []), list)

    def test_no_frontmatter_returns_empty_dict_and_full_content(self):
        content = "# Just a heading\n\nNo frontmatter here."
        frontmatter, body = parse_frontmatter(content)
        assert frontmatter == {}
        assert "Just a heading" in body

    def test_raises_on_malformed_yaml(self):
        bad_content = "---\nname: [unclosed bracket\n---\n# Body"
        with pytest.raises(ValueError):
            parse_frontmatter(bad_content)

    def test_empty_frontmatter_returns_empty_dict(self):
        content = "---\n---\n# Body"
        frontmatter, body = parse_frontmatter(content)
        assert frontmatter == {}


# ---------------------------------------------------------------------------
# fetch_and_output
# ---------------------------------------------------------------------------

class TestFetchAndOutput:
    SAMPLE_CONTENT = "---\nname: test\ndescription: A skill.\n---\n# Test"

    def test_returns_content_as_string_when_no_output_path(self):
        with patch("scripts.fetch_skill.fetch_skill_md",
                   return_value=(self.SAMPLE_CONTENT, "https://raw.example.com/SKILL.md")):
            result = fetch_and_output("https://github.com/user/repo")
        assert result == self.SAMPLE_CONTENT

    def test_writes_to_file_when_output_path_given(self, tmp_path):
        out = tmp_path / "skill.md"
        with patch("scripts.fetch_skill.fetch_skill_md",
                   return_value=(self.SAMPLE_CONTENT, "https://raw.example.com/SKILL.md")):
            fetch_and_output("https://github.com/user/repo", output_path=str(out))
        assert out.read_text() == self.SAMPLE_CONTENT

    def test_returns_output_path_when_writing_to_file(self, tmp_path):
        out = tmp_path / "skill.md"
        with patch("scripts.fetch_skill.fetch_skill_md",
                   return_value=(self.SAMPLE_CONTENT, "https://raw.example.com/SKILL.md")):
            result = fetch_and_output("https://github.com/user/repo", output_path=str(out))
        assert result == str(out)

    def test_propagates_fetch_error(self):
        with patch("scripts.fetch_skill.fetch_skill_md",
                   side_effect=FetchError("all failed", ["url1", "url2"])):
            with pytest.raises(FetchError):
                fetch_and_output("https://github.com/user/repo")
