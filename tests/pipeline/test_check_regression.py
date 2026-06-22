"""
Unit tests for pipeline/check_regression.py

Focus on the pure helpers that pick the previous *index* release and parse its
skill count. The data ("index-*") releases must be selected independently of
the npm ("v*") releases, which share the same GitHub Releases list but carry no
"Skills indexed:" field.
"""
from pipeline.check_regression import (
    latest_index_tag,
    parse_skill_count,
)


class TestLatestIndexTag:
    def test_picks_newest_index_tag(self):
        tags = ["index-20260301", "index-20260317", "index-20260210"]
        assert latest_index_tag(tags) == "index-20260317"

    def test_ignores_npm_version_tags(self):
        # npm releases (v0.1.1) are newer but must not be chosen.
        tags = ["v0.1.1", "v0.1.0", "index-20260317"]
        assert latest_index_tag(tags) == "index-20260317"

    def test_returns_none_when_no_index_release(self):
        # The real current state: only npm tags exist.
        assert latest_index_tag(["v0.1.1", "v0.1.0"]) is None

    def test_returns_none_for_empty_list(self):
        assert latest_index_tag([]) is None


class TestParseSkillCount:
    def test_parses_bold_markdown_field(self):
        assert parse_skill_count("**Skills indexed:** 37,962\n") == 37962

    def test_parses_plain_field(self):
        assert parse_skill_count("Skills indexed: 14823") == 14823

    def test_returns_none_when_absent(self):
        # An npm release body has no such field.
        assert parse_skill_count("first npm release\n\nTrimmed package.") is None
