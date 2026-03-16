"""Tests for pipeline/update_docs.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.update_docs import (
    compute_stats,
    render_distribution_table,
    render_skill_count,
    render_source_table,
    update_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_metadata(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_record(stars: int = 10, sources: list[str] | None = None) -> dict:
    return {
        "quality": {"stars": stars},
        "source": sources or ["skillsmp"],
    }


# ---------------------------------------------------------------------------
# compute_stats
# ---------------------------------------------------------------------------

class TestComputeStats:
    def test_counts_total(self, tmp_path):
        p = tmp_path / "metadata.jsonl"
        _write_metadata(p, [_make_record(10), _make_record(50), _make_record(200)])
        stats = compute_stats(p)
        assert stats["total"] == 3

    def test_star_buckets(self, tmp_path):
        p = tmp_path / "metadata.jsonl"
        _write_metadata(p, [
            _make_record(0),
            _make_record(5),
            _make_record(10),
            _make_record(100),
            _make_record(5000),
        ])
        stats = compute_stats(p)
        assert stats["buckets"]["0"] == 1
        assert stats["buckets"]["1–9"] == 1
        assert stats["buckets"]["10–49"] == 1
        assert stats["buckets"]["100–499"] == 1
        assert stats["buckets"]["5k+"] == 1

    def test_source_counts(self, tmp_path):
        p = tmp_path / "metadata.jsonl"
        _write_metadata(p, [
            _make_record(sources=["skillsmp"]),
            _make_record(sources=["marketplace"]),
            _make_record(sources=["skillsmp", "marketplace"]),
        ])
        stats = compute_stats(p)
        assert stats["sources"]["skillsmp"] == 2
        assert stats["sources"]["marketplace"] == 2

    def test_skips_blank_lines(self, tmp_path):
        p = tmp_path / "metadata.jsonl"
        p.write_text('{"quality":{"stars":10},"source":["skillsmp"]}\n\n\n')
        stats = compute_stats(p)
        assert stats["total"] == 1

    def test_missing_quality_field_treated_as_zero_stars(self, tmp_path):
        p = tmp_path / "metadata.jsonl"
        p.write_text('{"source":["skillsmp"]}\n')
        stats = compute_stats(p)
        assert stats["buckets"]["0"] == 1


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

class TestRenderSkillCount:
    def test_rounds_to_nearest_500(self):
        assert render_skill_count({"total": 4599}) == "4,500+"
        assert render_skill_count({"total": 14306}) == "14,000+"
        assert render_skill_count({"total": 500}) == "500+"

    def test_exactly_on_boundary(self):
        assert render_skill_count({"total": 5000}) == "5,000+"


class TestRenderDistributionTable:
    def test_contains_header_row(self):
        stats = {"total": 10, "buckets": {"0": 0, "1–9": 0, "10–49": 10, "50–99": 0,
                                           "100–499": 0, "500–999": 0, "1k–5k": 0, "5k+": 0}}
        table = render_distribution_table(stats)
        assert "| Stars |" in table
        assert "| Skills |" in table

    def test_skips_zero_count_buckets(self):
        stats = {"total": 10, "buckets": {"0": 0, "1–9": 0, "10–49": 10, "50–99": 0,
                                           "100–499": 0, "500–999": 0, "1k–5k": 0, "5k+": 0}}
        table = render_distribution_table(stats)
        assert "1–9" not in table

    def test_includes_total_row(self):
        stats = {"total": 42, "buckets": {"0": 0, "1–9": 0, "10–49": 42, "50–99": 0,
                                           "100–499": 0, "500–999": 0, "1k–5k": 0, "5k+": 0}}
        table = render_distribution_table(stats)
        assert "**Total**" in table
        assert "42" in table

    def test_bar_chart_uses_unicode_blocks(self):
        stats = {"total": 10, "buckets": {"0": 0, "1–9": 0, "10–49": 10, "50–99": 0,
                                           "100–499": 0, "500–999": 0, "1k–5k": 0, "5k+": 0}}
        table = render_distribution_table(stats)
        assert "█" in table


class TestRenderSourceTable:
    def test_includes_all_known_crawlers(self):
        stats = {"total": 100, "sources": {"skillsmp": 30, "clawhub": 20, "marketplace": 50}}
        table = render_source_table(stats)
        assert "skillsmp_crawler.py" in table
        assert "clawhub_crawler.py" in table
        assert "marketplace_crawler.py" in table
        assert "skillhub_crawler.py" in table
        assert "topic_crawler.py" in table

    def test_total_row(self):
        stats = {"total": 100, "sources": {}}
        table = render_source_table(stats)
        assert "**Total (after dedup)**" in table
        assert "100" in table

    def test_zero_for_missing_source(self):
        stats = {"total": 50, "sources": {"skillsmp": 50}}
        table = render_source_table(stats)
        # clawhub not in sources; should still appear with 0
        lines = [l for l in table.splitlines() if "clawhub_crawler" in l]
        assert lines
        assert "| 0 |" in lines[0]


# ---------------------------------------------------------------------------
# update_file
# ---------------------------------------------------------------------------

class TestUpdateFile:
    def _sentinel(self, name: str, content: str = "old") -> str:
        return f"<!-- stats:{name}:start -->\n{content}\n<!-- stats:{name}:end -->"

    def _inline_sentinel(self, name: str, content: str = "old") -> str:
        return f"<!-- stats:{name}:start -->{content}<!-- stats:{name}:end -->"

    def test_replaces_block_sentinel(self, tmp_path):
        p = tmp_path / "README.md"
        p.write_text(self._sentinel("skill-count", "old-value"))
        stats = {"total": 5000, "buckets": {k: 0 for k in ["0","1–9","10–49","50–99","100–499","500–999","1k–5k","5k+"]},
                 "sources": {}}
        stats["buckets"]["5k+"] = 5000
        count = update_file(p, stats)
        assert count == 1
        text = p.read_text()
        assert "5,000+" in text
        assert "old-value" not in text

    def test_replaces_inline_sentinel(self, tmp_path):
        p = tmp_path / "SKILL.md"
        p.write_text(self._inline_sentinel("skill-count", "old"))
        stats = {"total": 14306, "buckets": {k: 0 for k in ["0","1–9","10–49","50–99","100–499","500–999","1k–5k","5k+"]},
                 "sources": {}}
        update_file(p, stats)
        text = p.read_text()
        # Inline: no newline between tags
        assert "<!-- stats:skill-count:start -->14,000+<!-- stats:skill-count:end -->" in text

    def test_returns_zero_when_no_change(self, tmp_path):
        """If file has no sentinels, update count is 0."""
        p = tmp_path / "file.md"
        p.write_text("# No sentinels here\n")
        stats = {"total": 100, "buckets": {k: 0 for k in ["0","1–9","10–49","50–99","100–499","500–999","1k–5k","5k+"]},
                 "sources": {}}
        count = update_file(p, stats)
        assert count == 0

    def test_dry_run_does_not_write(self, tmp_path):
        p = tmp_path / "README.md"
        p.write_text(self._sentinel("skill-count", "old"))
        stats = {"total": 5000, "buckets": {k: 0 for k in ["0","1–9","10–49","50–99","100–499","500–999","1k–5k","5k+"]},
                 "sources": {}}
        stats["buckets"]["5k+"] = 5000
        update_file(p, stats, dry_run=True)
        text = p.read_text()
        assert "old" in text  # unchanged

    def test_multiple_sentinels_in_one_file(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_text(
            self._inline_sentinel("skill-count", "0") + "\n" +
            self._sentinel("coverage-table", "old-table")
        )
        stats = {"total": 1000, "buckets": {k: 0 for k in ["0","1–9","10–49","50–99","100–499","500–999","1k–5k","5k+"]},
                 "sources": {}}
        count = update_file(p, stats)
        assert count == 2

    def test_unknown_sentinel_name_is_skipped(self, tmp_path, caplog):
        import logging
        p = tmp_path / "file.md"
        p.write_text("<!-- stats:unknown-block:start -->x<!-- stats:unknown-block:end -->")
        stats = {"total": 100, "buckets": {}, "sources": {}}
        with caplog.at_level(logging.WARNING):
            count = update_file(p, stats)
        assert count == 0
        assert "unknown" in caplog.text

    def test_atomic_write_no_tmp_left(self, tmp_path):
        p = tmp_path / "README.md"
        p.write_text(self._sentinel("skill-count", "old"))
        stats = {"total": 500, "buckets": {k: 0 for k in ["0","1–9","10–49","50–99","100–499","500–999","1k–5k","5k+"]},
                 "sources": {}}
        stats["buckets"]["500–999"] = 500
        update_file(p, stats)
        assert not (tmp_path / "README.md.tmp").exists()
        assert p.exists()
