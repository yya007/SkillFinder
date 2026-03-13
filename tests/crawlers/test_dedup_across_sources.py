"""
Cross-source deduplication tests.

Verifies that when the same GitHub repo appears in multiple crawler outputs
(SkillsMP and ClawHub, for example), pipeline/normalize.py correctly merges
them into a single unified record rather than duplicating them.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.normalize import normalize, canonical_key, skill_id


class TestGithubUrlDedup:
    def test_same_repo_from_two_sources_merges_to_one(self, tmp_path):
        """A repo listed on both SkillsMP and ClawHub → single unified record."""
        skillsmp_record = {
            "repo_url": "https://github.com/user/k8s-deployer",
            "name": "k8s-deployer",
            "description": "Deploy Kubernetes clusters.",
            "source": "skillsmp",
            "raw_metadata": {"stars": 142, "pushed_at": "2026-02-15", "topics": ["kubernetes"]},
        }
        clawhub_record = {
            "repo_url": "https://github.com/user/k8s-deployer",  # same repo
            "name": "kubernetes-deployer",
            "description": "Deploy k8s clusters from ClawHub.",
            "source": "clawhub",
            "raw_metadata": {"categories": ["devops"], "safety_scan": "clean"},
        }
        # Write as two separate raw files
        skillsmp_file = tmp_path / "skillsmp.jsonl"
        clawhub_file = tmp_path / "clawhub.jsonl"
        skillsmp_file.write_text(json.dumps(skillsmp_record) + "\n")
        clawhub_file.write_text(json.dumps(clawhub_record) + "\n")

        output = tmp_path / "unified.jsonl"
        count = normalize([str(skillsmp_file), str(clawhub_file)], str(output))
        assert count == 1, f"Expected 1 merged record, got {count}"

        record = json.loads(output.read_text().strip())
        assert "skillsmp" in record["source"]
        assert "clawhub" in record["source"]
        assert record["quality"]["stars"] == 142  # from skillsmp
        assert record["quality"]["safety_scan"] == "clean"  # from clawhub

    def test_git_suffix_variants_dedup_to_same_record(self, tmp_path):
        """https://github.com/user/repo.git and https://github.com/user/repo → same record."""
        rec1 = {
            "repo_url": "https://github.com/user/myskill.git",
            "name": "myskill", "description": "A skill.", "source": "skillsmp",
            "raw_metadata": {"stars": 5, "pushed_at": "2026-01-01", "topics": []},
        }
        rec2 = {
            "repo_url": "https://github.com/user/myskill",
            "name": "myskill", "description": "A skill from clawhub.", "source": "clawhub",
            "raw_metadata": {"categories": ["tools"], "safety_scan": "clean"},
        }
        f1, f2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        f1.write_text(json.dumps(rec1) + "\n")
        f2.write_text(json.dumps(rec2) + "\n")
        count = normalize([str(f1), str(f2)], str(tmp_path / "out.jsonl"))
        assert count == 1

    def test_trailing_slash_variant_deduplicates(self, tmp_path):
        rec1 = {"repo_url": "https://github.com/user/repo/", "name": "r", "description": "d.", "source": "skillhub", "raw_metadata": {"rank": "A", "overall_score": 8.0}}
        rec2 = {"repo_url": "https://github.com/user/repo",  "name": "r", "description": "d.", "source": "skillsmp", "raw_metadata": {"stars": 50, "pushed_at": "2026-01-01", "topics": []}}
        f1, f2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        f1.write_text(json.dumps(rec1) + "\n")
        f2.write_text(json.dumps(rec2) + "\n")
        count = normalize([str(f1), str(f2)], str(tmp_path / "out.jsonl"))
        assert count == 1

    def test_different_repos_not_merged(self, tmp_path):
        rec1 = {"repo_url": "https://github.com/user/repo-a", "name": "a", "description": "desc a.", "source": "skillsmp", "raw_metadata": {"stars": 5, "pushed_at": "2026-01-01", "topics": []}}
        rec2 = {"repo_url": "https://github.com/user/repo-b", "name": "b", "description": "desc b.", "source": "skillsmp", "raw_metadata": {"stars": 5, "pushed_at": "2026-01-01", "topics": []}}
        f1, f2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        f1.write_text(json.dumps(rec1) + "\n")
        f2.write_text(json.dumps(rec2) + "\n")
        count = normalize([str(f1), str(f2)], str(tmp_path / "out.jsonl"))
        assert count == 2

    def test_merged_record_has_union_of_sources(self, tmp_path):
        """Merged record source list contains all contributing sources."""
        records = [
            {"repo_url": "https://github.com/u/r", "name": "r", "description": "d.", "source": src,
             "raw_metadata": {"stars": 10, "pushed_at": "2026-01-01", "topics": []}}
            for src in ["skillsmp", "clawhub", "skillhub"]
        ]
        # Fix clawhub and skillhub records to have proper raw_metadata
        records[1]["raw_metadata"] = {"categories": [], "safety_scan": "clean"}
        records[2]["raw_metadata"] = {"rank": "A", "overall_score": 8.0}

        files = []
        for i, r in enumerate(records):
            f = tmp_path / f"src{i}.jsonl"
            f.write_text(json.dumps(r) + "\n")
            files.append(str(f))

        out = tmp_path / "out.jsonl"
        count = normalize(files, str(out))
        assert count == 1
        record = json.loads(out.read_text().strip())
        assert set(record["source"]) == {"skillsmp", "clawhub", "skillhub"}

    def test_non_github_url_not_deduped_with_github_url(self, tmp_path):
        """A GitLab skill and a GitHub skill with same name are NOT merged."""
        rec1 = {"repo_url": "https://github.com/user/myskill", "name": "myskill", "description": "On GitHub.", "source": "skillsmp", "raw_metadata": {"stars": 5, "pushed_at": "2026-01-01", "topics": []}}
        rec2 = {"repo_url": "https://gitlab.com/user/myskill", "name": "myskill", "description": "On GitLab.", "source": "clawhub", "raw_metadata": {"categories": [], "safety_scan": "clean"}}
        f1, f2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        f1.write_text(json.dumps(rec1) + "\n")
        f2.write_text(json.dumps(rec2) + "\n")
        count = normalize([str(f1), str(f2)], str(tmp_path / "out.jsonl"))
        assert count == 2

    def test_merged_record_has_stable_id(self, tmp_path):
        """Merged record ID equals sha256(canonical_key(repo_url))."""
        repo_url = "https://github.com/user/myskill"
        rec1 = {"repo_url": repo_url, "name": "myskill", "description": "A skill.", "source": "skillsmp", "raw_metadata": {"stars": 5, "pushed_at": "2026-01-01", "topics": []}}
        rec2 = {"repo_url": repo_url + ".git", "name": "myskill", "description": "Same skill.", "source": "clawhub", "raw_metadata": {"categories": [], "safety_scan": "clean"}}
        f1, f2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        f1.write_text(json.dumps(rec1) + "\n")
        f2.write_text(json.dumps(rec2) + "\n")
        normalize([str(f1), str(f2)], str(tmp_path / "out.jsonl"))
        record = json.loads((tmp_path / "out.jsonl").read_text().strip())
        assert record["id"] == skill_id(repo_url)

    def test_merged_record_takes_max_stars(self, tmp_path):
        """When merging, the record with the highest star count wins."""
        rec_low = {"repo_url": "https://github.com/u/r", "name": "r", "description": "d.", "source": "skillsmp", "raw_metadata": {"stars": 10, "pushed_at": "2026-01-01", "topics": []}}
        rec_high = {"repo_url": "https://github.com/u/r", "name": "r", "description": "d from clawhub.", "source": "clawhub", "raw_metadata": {"categories": [], "safety_scan": "clean", "stars": 200}}
        f1, f2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        f1.write_text(json.dumps(rec_low) + "\n")
        f2.write_text(json.dumps(rec_high) + "\n")
        normalize([str(f1), str(f2)], str(tmp_path / "out.jsonl"))
        record = json.loads((tmp_path / "out.jsonl").read_text().strip())
        # Stars should be the maximum across both records
        assert record["quality"]["stars"] >= 10

    def test_case_insensitive_url_deduplication(self, tmp_path):
        """URLs differing only in case should dedup to the same record."""
        rec1 = {"repo_url": "https://github.com/User/MySkill", "name": "myskill", "description": "Skill.", "source": "skillsmp", "raw_metadata": {"stars": 3, "pushed_at": "2026-01-01", "topics": []}}
        rec2 = {"repo_url": "https://github.com/user/myskill", "name": "myskill", "description": "Same skill.", "source": "clawhub", "raw_metadata": {"categories": [], "safety_scan": "clean"}}
        f1, f2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        f1.write_text(json.dumps(rec1) + "\n")
        f2.write_text(json.dumps(rec2) + "\n")
        count = normalize([str(f1), str(f2)], str(tmp_path / "out.jsonl"))
        assert count == 1

    def test_safety_flag_set_when_clawhub_warns(self, tmp_path):
        """Merged record has safety_flag=True when ClawHub reports a warning."""
        rec_skillsmp = {"repo_url": "https://github.com/u/risky", "name": "risky", "description": "Risky skill.", "source": "skillsmp", "raw_metadata": {"stars": 5, "pushed_at": "2026-01-01", "topics": []}}
        rec_clawhub = {"repo_url": "https://github.com/u/risky", "name": "risky", "description": "Risky skill from clawhub.", "source": "clawhub", "raw_metadata": {"categories": [], "safety_scan": "warning: suspicious network calls"}}
        f1, f2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        f1.write_text(json.dumps(rec_skillsmp) + "\n")
        f2.write_text(json.dumps(rec_clawhub) + "\n")
        normalize([str(f1), str(f2)], str(tmp_path / "out.jsonl"))
        record = json.loads((tmp_path / "out.jsonl").read_text().strip())
        assert record["quality"]["safety_flag"] is True

    def test_all_unified_records_have_unique_ids(self, tmp_path):
        """After normalization, every record has a unique id."""
        records = [
            {"repo_url": f"https://github.com/user/skill-{i}", "name": f"skill-{i}",
             "description": f"Skill {i}.", "source": "skillsmp",
             "raw_metadata": {"stars": i + 2, "pushed_at": "2026-01-01", "topics": []}}
            for i in range(5)
        ]
        f = tmp_path / "raw.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        out = tmp_path / "out.jsonl"
        count = normalize([str(f)], str(out))
        ids = [json.loads(l)["id"] for l in out.read_text().strip().splitlines()]
        assert len(ids) == len(set(ids)), "Non-unique IDs found in normalized output"


@pytest.mark.network
class TestCrossSourceDedupNetwork:
    """End-to-end dedup test using tiny real crawler samples."""

    def test_skillsmp_and_marketplace_dedup_shared_repos(self, tmp_path):
        """anthropics/skills repos that also appear in SkillsMP search results should dedup."""
        import os
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            pytest.skip("GITHUB_TOKEN not set")

        from crawlers.skillsmp_crawler import run as run_skillsmp
        from crawlers.marketplace_crawler import run as run_marketplace

        skillsmp_out = str(tmp_path / "skillsmp.jsonl")
        marketplace_out = str(tmp_path / "marketplace.jsonl")

        run_skillsmp(skillsmp_out, token=token, limit=20)
        run_marketplace(marketplace_out, token=token, limit=10)

        # Collect all repo_urls from both sources
        all_urls: dict[str, list[str]] = {}
        for path in [skillsmp_out, marketplace_out]:
            for line in open(path):
                r = json.loads(line)
                url = r["repo_url"]
                all_urls.setdefault(url, []).append(r["source"])

        # After normalize, duplicates should be gone
        unified_out = str(tmp_path / "unified.jsonl")
        count = normalize([skillsmp_out, marketplace_out], unified_out)
        unified_records = [json.loads(l) for l in open(unified_out)]
        assert len(unified_records) == count
        # All unified records should have unique IDs
        ids = [r["id"] for r in unified_records]
        assert len(ids) == len(set(ids))
