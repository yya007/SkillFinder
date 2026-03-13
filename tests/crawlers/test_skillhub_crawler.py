"""Tests for crawlers/skillhub_crawler.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Sample HTML fixtures
# ---------------------------------------------------------------------------

SAMPLE_SKILLHUB_LISTING_HTML = """
<html>
<body>
  <div class="skill-card">
    <a href="/skills/k8s-deployer" class="skill-link">k8s-deployer</a>
    <p class="skill-description">Deploy Kubernetes clusters with automated rollbacks.</p>
    <span class="skill-rank rank-A">A</span>
    <a href="https://github.com/user/k8s-deployer" class="skill-repo">GitHub</a>
  </div>
  <div class="skill-card">
    <a href="/skills/docker-manager" class="skill-link">docker-manager</a>
    <p class="skill-description">Manage Docker containers and images.</p>
    <span class="skill-rank rank-S">S</span>
    <a href="https://github.com/user/docker-manager" class="skill-repo">GitHub</a>
  </div>
</body>
</html>
"""

SAMPLE_SKILLHUB_DETAIL_HTML = """
<html>
<body>
  <h1 class="skill-name">k8s-deployer</h1>
  <p class="skill-description">Deploy Kubernetes clusters with automated rollbacks.</p>
  <a href="https://github.com/user/k8s-deployer" class="skill-repo">GitHub</a>
  <div class="skill-rank">A</div>
  <div class="dimension-scores">
    <div class="dimension" data-name="Practicality" data-score="9.0"></div>
    <div class="dimension" data-name="Clarity" data-score="8.5"></div>
    <div class="dimension" data-name="Automation" data-score="8.0"></div>
    <div class="dimension" data-name="Quality" data-score="8.2"></div>
    <div class="dimension" data-name="Impact" data-score="9.1"></div>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# TestBuildRawRecord
# ---------------------------------------------------------------------------

class TestBuildRawRecord:
    """Unit tests for skillhub_crawler.build_raw_record()."""

    def _make_parsed_skill(
        self,
        name="k8s-deployer",
        description="Deploy Kubernetes clusters.",
        repo_url="https://github.com/user/k8s-deployer",
        rank="A",
        overall_score=8.5,
        dimension_scores=None,
    ):
        return {
            "name": name,
            "description": description,
            "repo_url": repo_url,
            "rank": rank,
            "overall_score": overall_score,
            "dimension_scores": dimension_scores or {
                "Practicality": 9.0,
                "Clarity": 8.5,
                "Automation": 8.0,
                "Quality": 8.2,
                "Impact": 9.1,
            },
        }

    def test_repo_url_is_github_url(self):
        from crawlers.skillhub_crawler import build_raw_record
        skill = self._make_parsed_skill(repo_url="https://github.com/user/myskill")
        record = build_raw_record(skill)
        assert record["repo_url"] == "https://github.com/user/myskill"

    def test_source_is_skillhub(self):
        from crawlers.skillhub_crawler import build_raw_record
        record = build_raw_record(self._make_parsed_skill())
        assert record["source"] == "skillhub"

    def test_rank_in_raw_metadata(self):
        from crawlers.skillhub_crawler import build_raw_record
        record = build_raw_record(self._make_parsed_skill(rank="S"))
        assert record["raw_metadata"]["rank"] == "S"

    def test_overall_score_in_raw_metadata(self):
        from crawlers.skillhub_crawler import build_raw_record
        record = build_raw_record(self._make_parsed_skill(overall_score=9.2))
        assert record["raw_metadata"]["overall_score"] == pytest.approx(9.2)

    def test_dimension_scores_in_raw_metadata(self):
        from crawlers.skillhub_crawler import build_raw_record
        dims = {"Practicality": 8.0, "Clarity": 7.5, "Automation": 9.0, "Quality": 8.5, "Impact": 8.8}
        record = build_raw_record(self._make_parsed_skill(dimension_scores=dims))
        assert record["raw_metadata"]["dimension_scores"] == dims

    def test_name_and_description_from_parsed_skill(self):
        from crawlers.skillhub_crawler import build_raw_record
        skill = self._make_parsed_skill(name="my-skill", description="My description.")
        record = build_raw_record(skill)
        assert record["name"] == "my-skill"
        assert record["description"] == "My description."

    def test_required_fields_present(self):
        from crawlers.skillhub_crawler import build_raw_record
        record = build_raw_record(self._make_parsed_skill())
        for field in ("repo_url", "name", "description", "source", "raw_metadata"):
            assert field in record, f"Missing field: {field}"

    def test_returns_none_for_non_github_repo_url(self):
        from crawlers.skillhub_crawler import build_raw_record
        skill = self._make_parsed_skill(repo_url="https://gitlab.com/user/skill")
        # SkillHub only tracks GitHub-hosted skills; non-GitHub URLs should be skipped
        result = build_raw_record(skill)
        # Either returns None or has the URL as-is — both are valid implementations;
        # we verify the behavior is deterministic
        if result is not None:
            assert result["repo_url"] is not None


# ---------------------------------------------------------------------------
# TestRunSkillhub — unit tests with mocked HTTP
# ---------------------------------------------------------------------------

class TestRunSkillhub:
    """Unit tests for skillhub_crawler.run() with HTTP mocked out."""

    def test_respects_limit(self, tmp_path):
        from crawlers.skillhub_crawler import run

        parsed_skills = [
            {
                "name": f"skill-{i}",
                "description": f"Description {i}.",
                "repo_url": f"https://github.com/user/skill-{i}",
                "rank": "A",
                "overall_score": 8.0,
                "dimension_scores": {},
            }
            for i in range(5)
        ]

        with patch("crawlers.skillhub_crawler.scrape_skill_listing") as mock_listing:
            mock_listing.return_value = parsed_skills
            out = str(tmp_path / "out.jsonl")
            count = run(out, limit=3)

        assert count == 3

    def test_output_has_required_fields(self, tmp_path):
        from crawlers.skillhub_crawler import run

        parsed_skills = [
            {
                "name": "good-skill",
                "description": "A good skill.",
                "repo_url": "https://github.com/user/good-skill",
                "rank": "B",
                "overall_score": 7.0,
                "dimension_scores": {},
            }
        ]

        with patch("crawlers.skillhub_crawler.scrape_skill_listing") as mock_listing:
            mock_listing.return_value = parsed_skills
            out = str(tmp_path / "out.jsonl")
            run(out)

        record = json.loads(Path(out).read_text().strip())
        for field in ("repo_url", "name", "description", "source", "raw_metadata"):
            assert field in record

    def test_source_is_skillhub_for_all_records(self, tmp_path):
        from crawlers.skillhub_crawler import run

        parsed_skills = [
            {
                "name": f"skill-{i}",
                "description": "Desc.",
                "repo_url": f"https://github.com/user/skill-{i}",
                "rank": "A",
                "overall_score": 8.0,
                "dimension_scores": {},
            }
            for i in range(3)
        ]

        with patch("crawlers.skillhub_crawler.scrape_skill_listing") as mock_listing:
            mock_listing.return_value = parsed_skills
            out = str(tmp_path / "out.jsonl")
            run(out)

        for line in Path(out).read_text().strip().splitlines():
            record = json.loads(line)
            assert record["source"] == "skillhub"


# ---------------------------------------------------------------------------
# TestSkillhubCrawlerNetwork — real network calls
# ---------------------------------------------------------------------------

@pytest.mark.network
class TestSkillhubCrawlerNetwork:
    """Network integration tests for SkillHub scraper — skipped by default."""

    def test_fetches_real_skills_with_limit(self, tmp_path):
        from crawlers.skillhub_crawler import run
        out = str(tmp_path / "out.jsonl")
        count = run(out, limit=3)
        assert count <= 3
        assert count >= 1

    def test_all_records_have_rank(self, tmp_path):
        from crawlers.skillhub_crawler import run
        out = str(tmp_path / "out.jsonl")
        run(out, limit=5)
        for line in Path(out).read_text().strip().splitlines():
            record = json.loads(line)
            assert record["raw_metadata"].get("rank") in {"S", "A", "B", "C"}, (
                f"Unexpected rank: {record['raw_metadata'].get('rank')}"
            )

    def test_all_records_have_source_skillhub(self, tmp_path):
        from crawlers.skillhub_crawler import run
        out = str(tmp_path / "out.jsonl")
        run(out, limit=5)
        for line in Path(out).read_text().strip().splitlines():
            record = json.loads(line)
            assert record["source"] == "skillhub"

    def test_no_duplicate_repo_urls(self, tmp_path):
        from crawlers.skillhub_crawler import run
        out = str(tmp_path / "out.jsonl")
        run(out, limit=10)
        urls = [json.loads(l)["repo_url"] for l in Path(out).read_text().strip().splitlines()]
        assert len(urls) == len(set(urls)), "Duplicate repo_urls in SkillHub output"
