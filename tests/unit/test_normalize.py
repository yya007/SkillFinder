"""
Unit tests for pipeline/normalize.py

Tests cover:
  - canonical_key: URL normalization
  - skill_id: stable SHA256 identifier
  - build_embedding_text: text construction for embedding
  - merge_records: metadata priority merge
  - build_install_cmds: install command generation
  - passes_quality_filter: quality gate logic
  - normalize: end-to-end pipeline function
  - QualityGateError: raised when min_skills threshold not met
"""
import hashlib
import json
import os

import pytest

from pipeline.normalize import (
    CURATED_SOURCES,
    QualityGateError,
    build_embedding_text,
    build_install_cmds,
    canonical_key,
    merge_records,
    normalize,
    passes_quality_filter,
    skill_id,
)


# ---------------------------------------------------------------------------
# canonical_key
# ---------------------------------------------------------------------------

class TestCanonicalKey:
    def test_lowercases_url(self):
        assert canonical_key("https://GitHub.COM/User/Repo") == "https://github.com/user/repo"

    def test_strips_git_suffix(self):
        assert canonical_key("https://github.com/user/repo.git") == "https://github.com/user/repo"

    def test_strips_trailing_slash(self):
        assert canonical_key("https://github.com/user/repo/") == "https://github.com/user/repo"

    def test_strips_both_git_and_slash(self):
        result = canonical_key("https://github.com/user/repo.git/")
        assert result == "https://github.com/user/repo"

    def test_leaves_clean_url_unchanged(self):
        url = "https://github.com/user/repo"
        assert canonical_key(url) == url

    def test_three_variants_same_key(self, raw_records_with_overlap):
        keys = {canonical_key(r["repo_url"]) for r in raw_records_with_overlap}
        assert len(keys) == 1, f"Expected 1 unique key, got {len(keys)}: {keys}"

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError):
            canonical_key("")

    def test_raises_on_none(self):
        with pytest.raises((ValueError, TypeError)):
            canonical_key(None)


# ---------------------------------------------------------------------------
# skill_id
# ---------------------------------------------------------------------------

class TestSkillId:
    def test_returns_64_char_hex(self):
        sid = skill_id("https://github.com/user/repo")
        assert len(sid) == 64
        assert all(c in "0123456789abcdef" for c in sid)

    def test_deterministic(self):
        url = "https://github.com/user/repo"
        assert skill_id(url) == skill_id(url)

    def test_matches_manual_sha256(self):
        url = "https://github.com/user/repo"
        expected = hashlib.sha256(canonical_key(url).encode()).hexdigest()
        assert skill_id(url) == expected

    def test_different_urls_different_ids(self):
        a = skill_id("https://github.com/user/repo-a")
        b = skill_id("https://github.com/user/repo-b")
        assert a != b

    def test_git_suffix_variant_same_id(self):
        assert skill_id("https://github.com/user/repo") == skill_id("https://github.com/user/repo.git")

    def test_uppercase_variant_same_id(self):
        assert skill_id("https://github.com/user/repo") == skill_id("https://GitHub.com/user/repo")


# ---------------------------------------------------------------------------
# build_embedding_text
# ---------------------------------------------------------------------------

class TestBuildEmbeddingText:
    def test_includes_name(self, skill):
        assert skill["name"] in build_embedding_text(skill)

    def test_includes_description(self, skill):
        assert skill["description"] in build_embedding_text(skill)

    def test_includes_categories(self, skill):
        text = build_embedding_text(skill)
        for cat in skill["categories"]:
            assert cat in text

    def test_does_not_include_install_commands(self, skill):
        text = build_embedding_text(skill)
        assert "/plugin install" not in text
        assert "clawhub install" not in text

    def test_does_not_include_star_count(self, skill):
        text = build_embedding_text(skill)
        assert "stars" not in text.lower()

    def test_format_structure(self, skill):
        text = build_embedding_text(skill)
        assert text.startswith(skill["name"] + ".")
        assert "Categories:" in text

    def test_raises_on_missing_name(self, skill):
        bad = {**skill, "name": ""}
        with pytest.raises(ValueError):
            build_embedding_text(bad)

    def test_raises_on_missing_description(self, skill):
        bad = {**skill, "description": ""}
        with pytest.raises(ValueError):
            build_embedding_text(bad)

    def test_under_512_tokens_for_typical_skill(self, skill):
        text = build_embedding_text(skill)
        # Rough token estimate: 4 chars per token
        assert len(text) / 4 < 512


# ---------------------------------------------------------------------------
# merge_records
# ---------------------------------------------------------------------------

class TestMergeRecords:
    def test_raises_on_empty_list(self):
        with pytest.raises(ValueError):
            merge_records([])

    def test_single_record_returns_self(self):
        record = {
            "repo_url": "https://github.com/user/skill",
            "name": "skill",
            "description": "A skill.",
            "source": "skillsmp",
            "raw_metadata": {"stars": 5},
        }
        merged = merge_records([record])
        assert merged["name"] == "skill"
        assert "skillsmp" in merged["source"]

    def test_description_from_skillhub_beats_skillsmp(self, raw_records_with_overlap):
        # skillhub record has the most detailed description
        merged = merge_records(raw_records_with_overlap)
        # Should contain content from the skillhub record (longest/most authoritative)
        assert merged["description"]

    def test_stars_taken_from_skillsmp(self, raw_records_with_overlap):
        merged = merge_records(raw_records_with_overlap)
        assert merged["quality"]["stars"] == 142

    def test_skillhub_rank_and_score_preserved(self, raw_records_with_overlap):
        merged = merge_records(raw_records_with_overlap)
        assert merged["quality"]["skillhub_rank"] == "A"
        assert merged["quality"]["skillhub_score"] == pytest.approx(8.4)

    def test_categories_are_union(self, raw_records_with_overlap):
        merged = merge_records(raw_records_with_overlap)
        cats = set(merged["categories"])
        assert "kubernetes" in cats
        assert "devops" in cats

    def test_source_list_contains_all_sources(self, raw_records_with_overlap):
        merged = merge_records(raw_records_with_overlap)
        sources = set(merged["source"])
        assert "skillsmp" in sources
        assert "clawhub" in sources
        assert "skillhub" in sources

    def test_repo_url_is_canonical(self, raw_records_with_overlap):
        merged = merge_records(raw_records_with_overlap)
        url = merged["repo_url"]
        assert not url.endswith(".git")
        assert not url.endswith("/")
        assert url == url.lower()


# ---------------------------------------------------------------------------
# build_install_cmds
# ---------------------------------------------------------------------------

class TestBuildInstallCmds:
    def test_skillsmp_source_generates_claude_code_cmd(self):
        merged = {"name": "my-skill", "source": ["skillsmp"], "repo_url": "https://github.com/user/my-skill"}
        cmds = build_install_cmds(merged)
        assert "claude_code" in cmds
        assert "my-skill" in cmds["claude_code"]

    def test_clawhub_source_generates_openclaw_cmd(self):
        merged = {"name": "my-skill", "source": ["clawhub"], "repo_url": "https://github.com/user/my-skill"}
        cmds = build_install_cmds(merged)
        assert "openclaw" in cmds
        assert "clawhub install" in cmds["openclaw"]

    def test_marketplace_source_generates_claude_code_cmd(self):
        merged = {"name": "my-skill", "source": ["marketplace"], "repo_url": "https://github.com/user/my-skill"}
        cmds = build_install_cmds(merged)
        assert "claude_code" in cmds

    def test_multiple_sources_generate_multiple_cmds(self):
        merged = {"name": "my-skill", "source": ["skillsmp", "clawhub"], "repo_url": "https://github.com/user/my-skill"}
        cmds = build_install_cmds(merged)
        assert "claude_code" in cmds
        assert "openclaw" in cmds


# ---------------------------------------------------------------------------
# passes_quality_filter
# ---------------------------------------------------------------------------

class TestPassesQualityFilter:
    def test_passes_with_stars_gte_10(self):
        skill = {"description": "desc", "source": ["skillsmp"], "quality": {"stars": 10, "skillhub_rank": None}}
        assert passes_quality_filter(skill) is True

    def test_passes_with_many_stars(self):
        skill = {"description": "desc", "source": ["skillsmp"], "quality": {"stars": 100, "skillhub_rank": None}}
        assert passes_quality_filter(skill) is True

    def test_passes_if_in_curated_source_with_zero_stars(self):
        for curated in CURATED_SOURCES:
            skill = {"description": "desc", "source": [curated], "quality": {"stars": 0, "skillhub_rank": None}}
            assert passes_quality_filter(skill) is True, f"Failed for curated source: {curated}"

    def test_passes_with_skillhub_rank_s(self):
        skill = {"description": "desc", "source": ["skillsmp"], "quality": {"stars": 0, "skillhub_rank": "S"}}
        assert passes_quality_filter(skill) is True

    def test_passes_with_skillhub_rank_a(self):
        skill = {"description": "desc", "source": ["skillsmp"], "quality": {"stars": 0, "skillhub_rank": "A"}}
        assert passes_quality_filter(skill) is True

    def test_fails_rank_b_with_zero_stars_not_curated(self):
        skill = {"description": "desc", "source": ["skillsmp"], "quality": {"stars": 0, "skillhub_rank": "B"}}
        assert passes_quality_filter(skill) is False

    def test_fails_no_description(self):
        skill = {"description": "", "source": ["skillsmp"], "quality": {"stars": 100, "skillhub_rank": "S"}}
        assert passes_quality_filter(skill) is False

    def test_fails_missing_description_key(self):
        skill = {"source": ["skillsmp"], "quality": {"stars": 100, "skillhub_rank": "S"}}
        assert passes_quality_filter(skill) is False

    def test_fails_zero_stars_noncurated_no_rank(self, skill_low_quality):
        assert passes_quality_filter(skill_low_quality) is False

    def test_passes_curated_zero_stars(self, skill_curated):
        assert passes_quality_filter(skill_curated) is True

    def test_star_count_1_fails(self):
        skill = {"description": "desc", "source": ["skillsmp"], "quality": {"stars": 1, "skillhub_rank": None}}
        assert passes_quality_filter(skill) is False

    def test_star_count_9_fails(self):
        skill = {"description": "desc", "source": ["skillsmp"], "quality": {"stars": 9, "skillhub_rank": None}}
        assert passes_quality_filter(skill) is False

    def test_star_count_10_passes(self):
        skill = {"description": "desc", "source": ["skillsmp"], "quality": {"stars": 10, "skillhub_rank": None}}
        assert passes_quality_filter(skill) is True


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# normalize (end-to-end)
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_deduplicates_overlapping_records(self, tmp_raw_dir, tmp_path):
        output = str(tmp_path / "out.jsonl")
        count = normalize([str(p) for p in tmp_raw_dir.glob("*.jsonl")], output)
        # kubernetes-deployer appears in skillsmp + clawhub + skillhub → 1 merged record
        records = [json.loads(l) for l in open(output)]
        k8s = [r for r in records if r["name"] == "kubernetes-deployer"]
        assert len(k8s) == 1

    def test_output_file_created(self, tmp_raw_dir, tmp_path):
        output = str(tmp_path / "out.jsonl")
        normalize([str(p) for p in tmp_raw_dir.glob("*.jsonl")], output)
        assert os.path.exists(output)

    def test_all_records_have_id(self, tmp_raw_dir, tmp_path):
        output = str(tmp_path / "out.jsonl")
        normalize([str(p) for p in tmp_raw_dir.glob("*.jsonl")], output)
        for line in open(output):
            record = json.loads(line)
            assert "id" in record
            assert len(record["id"]) == 64

    def test_all_records_have_embedding_text(self, tmp_raw_dir, tmp_path):
        output = str(tmp_path / "out.jsonl")
        normalize([str(p) for p in tmp_raw_dir.glob("*.jsonl")], output)
        for line in open(output):
            record = json.loads(line)
            assert "embedding_text" in record
            assert record["embedding_text"]

    def test_drops_no_description_records(self, tmp_raw_dir, tmp_path):
        output = str(tmp_path / "out.jsonl")
        normalize([str(p) for p in tmp_raw_dir.glob("*.jsonl")], output)
        records = [json.loads(l) for l in open(output)]
        bad = [r for r in records if r["name"] == "no-description-skill"]
        assert len(bad) == 0

    def test_returns_count_of_written_records(self, tmp_raw_dir, tmp_path):
        output = str(tmp_path / "out.jsonl")
        count = normalize([str(p) for p in tmp_raw_dir.glob("*.jsonl")], output)
        actual = sum(1 for _ in open(output))
        assert count == actual

    def test_quality_gate_raises_when_below_threshold(self, tmp_raw_dir, tmp_path):
        output = str(tmp_path / "out.jsonl")
        with pytest.raises(QualityGateError):
            normalize([str(p) for p in tmp_raw_dir.glob("*.jsonl")], output, min_skills=99999)

    def test_raises_file_not_found_for_missing_input(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            normalize([str(tmp_path / "nonexistent.jsonl")], str(tmp_path / "out.jsonl"))

    def test_records_have_canonical_repo_urls(self, tmp_raw_dir, tmp_path):
        output = str(tmp_path / "out.jsonl")
        normalize([str(p) for p in tmp_raw_dir.glob("*.jsonl")], output)
        for line in open(output):
            record = json.loads(line)
            url = record["repo_url"]
            assert not url.endswith(".git")
            assert not url.endswith("/")
            assert url == url.lower()

    def test_is_official_not_in_output_metadata(self, tmp_raw_dir, tmp_path):
        """is_official is a pipeline-internal flag and must be stripped from output records."""
        output = str(tmp_path / "out.jsonl")
        normalize([str(p) for p in tmp_raw_dir.glob("*.jsonl")], output)
        for line in open(output):
            record = json.loads(line)
            assert "is_official" not in record, (
                f"is_official should be stripped from output but found in: {record.get('name')}"
            )
