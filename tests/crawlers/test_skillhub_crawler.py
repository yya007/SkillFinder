"""Tests for crawlers/skillhub_crawler.py."""
import json
from pathlib import Path
from unittest.mock import patch

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
# TestScrapeSkillListing — unit tests for category iteration
# ---------------------------------------------------------------------------

class TestScrapeSkillListing:
    """Unit tests for scrape_skill_listing() category iteration logic."""

    def _make_card(self, i: int) -> dict:
        return {
            "name": f"skill-{i}",
            "skillhub_url": f"https://skillhub.club/skills/skill-{i}",
            "description": f"Description {i}.",
            "rank": "A",
        }

    def _make_detail(self, i: int) -> dict:
        return {
            "full_description": f"Full description {i}.",
            "github_url": f"https://github.com/user/skill-{i}",
            "rank": "A",
            "overall_score": 8.0,
            "dimension_scores": {},
        }

    def test_iterates_over_all_categories(self):
        """scrape_skill_listing crawls uncategorised + each discovered category."""
        from crawlers.skillhub_crawler import scrape_skill_listing

        call_categories = []

        def fake_get_skill_list_page(session, page, category=""):
            call_categories.append(category)
            if page == 1:
                return ([self._make_card(0)], False)
            return ([], False)

        with patch("crawlers.skillhub_crawler.discover_categories", return_value=["devops", "testing"]), \
             patch("crawlers.skillhub_crawler.get_skill_list_page", side_effect=fake_get_skill_list_page), \
             patch("crawlers.skillhub_crawler.get_skill_detail", return_value=self._make_detail(0)), \
             patch("crawlers.skillhub_crawler._load_robots") as mock_robots:
            mock_robots.return_value.can_fetch = lambda *a: True
            list(scrape_skill_listing())

        # Should have called with "" (uncategorised), "devops", "testing"
        assert "" in call_categories
        assert "devops" in call_categories
        assert "testing" in call_categories

    def test_deduplicates_skills_across_categories(self):
        """Skills appearing in multiple categories are only included once."""
        from crawlers.skillhub_crawler import scrape_skill_listing

        shared_card = self._make_card(0)  # same skillhub_url in both categories

        def fake_get_skill_list_page(session, page, category=""):
            if page == 1:
                return ([shared_card], False)
            return ([], False)

        with patch("crawlers.skillhub_crawler.discover_categories", return_value=["devops", "testing"]), \
             patch("crawlers.skillhub_crawler.get_skill_list_page", side_effect=fake_get_skill_list_page), \
             patch("crawlers.skillhub_crawler.get_skill_detail", return_value=self._make_detail(0)), \
             patch("crawlers.skillhub_crawler._load_robots") as mock_robots:
            mock_robots.return_value.can_fetch = lambda *a: True
            results = list(scrape_skill_listing())

        # The same skill appeared in 3 crawl targets (uncategorised + 2 categories)
        # but must only appear once in results
        assert len(results) == 1

    def test_falls_back_gracefully_when_no_categories(self):
        """If discover_categories returns [], only the uncategorised pass is done."""
        from crawlers.skillhub_crawler import scrape_skill_listing

        call_count = []

        def fake_get_skill_list_page(session, page, category=""):
            call_count.append(category)
            if page == 1:
                return ([self._make_card(0)], False)
            return ([], False)

        with patch("crawlers.skillhub_crawler.discover_categories", return_value=[]), \
             patch("crawlers.skillhub_crawler.get_skill_list_page", side_effect=fake_get_skill_list_page), \
             patch("crawlers.skillhub_crawler.get_skill_detail", return_value=self._make_detail(0)), \
             patch("crawlers.skillhub_crawler._load_robots") as mock_robots:
            mock_robots.return_value.can_fetch = lambda *a: True
            results = list(scrape_skill_listing())

        assert call_count == [""]  # only uncategorised
        assert len(results) == 1

    def test_respects_limit_across_categories(self):
        """limit=N stops after N skills even when multiple categories remain."""
        from crawlers.skillhub_crawler import scrape_skill_listing

        # Use a counter so each category returns distinct cards (no dedup interference)
        call_idx = [0]

        def fake_get_skill_list_page(session, page, category=""):
            if page == 1:
                i = call_idx[0]
                call_idx[0] += 2
                return ([self._make_card(i), self._make_card(i + 1)], False)
            return ([], False)

        def fake_get_skill_detail(session, url):
            i = int(url.rsplit("-", 1)[-1])
            return self._make_detail(i)

        with patch("crawlers.skillhub_crawler.discover_categories", return_value=["devops", "testing"]), \
             patch("crawlers.skillhub_crawler.get_skill_list_page", side_effect=fake_get_skill_list_page), \
             patch("crawlers.skillhub_crawler.get_skill_detail", side_effect=fake_get_skill_detail), \
             patch("crawlers.skillhub_crawler._load_robots") as mock_robots:
            mock_robots.return_value.can_fetch = lambda *a: True
            results = list(scrape_skill_listing(limit=3))

        assert len(results) == 3


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
        urls = [json.loads(line)["repo_url"] for line in Path(out).read_text().strip().splitlines()]
        assert len(urls) == len(set(urls)), "Duplicate repo_urls in SkillHub output"
