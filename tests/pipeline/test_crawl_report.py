"""Tests for pipeline/crawl_report.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.crawl_report import compute_stats, load_raw, print_report, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# TestComputeStats
# ---------------------------------------------------------------------------

class TestComputeStats:
    def test_empty_list(self):
        stats = compute_stats([])
        assert stats["total"] == 0
        assert stats["no_desc"] == 0
        assert stats["zero_stars"] == 0
        assert stats["no_desc_pct"] == 0.0
        assert stats["zero_stars_pct"] == 0.0
        assert stats["median_stars"] == 0
        assert stats["p95_stars"] == 0

    def test_all_with_descriptions_and_stars(self):
        records = [
            {"description": "A skill", "raw_metadata": {"stars": 10}},
            {"description": "B skill", "raw_metadata": {"stars": 20}},
            {"description": "C skill", "raw_metadata": {"stars": 30}},
        ]
        stats = compute_stats(records)
        assert stats["total"] == 3
        assert stats["no_desc"] == 0
        assert stats["zero_stars"] == 0

    def test_counts_missing_description(self):
        records = [
            {"description": "Has one", "raw_metadata": {"stars": 5}},
            {"description": "", "raw_metadata": {"stars": 5}},
            {"description": "Has three", "raw_metadata": {"stars": 5}},
        ]
        stats = compute_stats(records)
        assert stats["no_desc"] == 1
        assert abs(stats["no_desc_pct"] - 100 / 3) < 0.1

    def test_counts_zero_stars(self):
        records = [
            {"description": "A", "raw_metadata": {"stars": 0}},
            {"description": "B", "raw_metadata": {"stars": 5}},
            {"description": "C", "raw_metadata": {"stars": 0}},
        ]
        stats = compute_stats(records)
        assert stats["zero_stars"] == 2

    def test_median_stars(self):
        records = [
            {"description": "A", "raw_metadata": {"stars": 10}},
            {"description": "B", "raw_metadata": {"stars": 20}},
            {"description": "C", "raw_metadata": {"stars": 30}},
        ]
        stats = compute_stats(records)
        assert stats["median_stars"] == 20

    def test_p95_stars(self):
        # 20 records with stars 1..20; p95 should be near the high end
        records = [
            {"description": f"skill {i}", "raw_metadata": {"stars": i}}
            for i in range(1, 21)
        ]
        stats = compute_stats(records)
        # p95 of 20 items: idx = min(int(20 * 95 / 100), 19) = min(19, 19) = 19
        # sorted values[19] = 20
        assert stats["p95_stars"] == 20


# ---------------------------------------------------------------------------
# TestLoadRaw
# ---------------------------------------------------------------------------

class TestLoadRaw:
    def test_loads_records_from_multiple_files(self, tmp_path):
        clawhub_file = tmp_path / "clawhub.jsonl"
        marketplace_file = tmp_path / "marketplace.jsonl"

        _write_jsonl(clawhub_file, [
            {"name": "skill1", "source": "clawhub"},
            {"name": "skill2", "source": "clawhub"},
        ])
        _write_jsonl(marketplace_file, [
            {"name": "skill3", "source": "marketplace"},
        ])

        result = load_raw(str(tmp_path))

        assert "clawhub" in result
        assert "marketplace" in result
        assert len(result["clawhub"]) == 2
        assert len(result["marketplace"]) == 1

    def test_falls_back_to_filename_stem_when_no_source_field(self, tmp_path):
        clawhub_file = tmp_path / "clawhub.jsonl"
        # Records with no 'source' field — should fall back to "clawhub"
        _write_jsonl(clawhub_file, [
            {"name": "skill1"},
            {"name": "skill2"},
        ])

        result = load_raw(str(tmp_path))
        assert "clawhub" in result
        assert len(result["clawhub"]) == 2

    def test_skips_tombstone_records(self, tmp_path):
        f = tmp_path / "skillsmp.jsonl"
        _write_jsonl(f, [
            {"name": "alive", "source": "skillsmp"},
            {"name": "dead", "source": "skillsmp", "tombstone": True},
        ])

        result = load_raw(str(tmp_path))
        assert len(result["skillsmp"]) == 1
        assert result["skillsmp"][0]["name"] == "alive"

    def test_skips_empty_data_dir(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist"
        result = load_raw(str(nonexistent))
        assert result == {} or len(result) == 0


# ---------------------------------------------------------------------------
# TestPrintReport
# ---------------------------------------------------------------------------

class TestPrintReport:
    def _make_source_records(self, n: int, no_desc: int, stars: int = 10) -> list[dict]:
        """Helper: create n records where the first no_desc have empty descriptions."""
        records = []
        for i in range(n):
            desc = "" if i < no_desc else f"Description {i}"
            records.append({"description": desc, "raw_metadata": {"stars": stars}})
        return records

    def test_exits_zero_always(self, tmp_path):
        """main() always returns 0, even with data present."""
        f = tmp_path / "source.jsonl"
        _write_jsonl(f, [{"name": "s", "description": "desc", "raw_metadata": {"stars": 5}}])
        result = main(["--data-dir", str(tmp_path)])
        assert result == 0

    def test_warn_lines_on_high_no_desc(self):
        """Source with >5% no-desc records -> [WARN] in returned warn_lines."""
        # 10 records, 2 without description = 20% > 5% threshold
        records = self._make_source_records(10, no_desc=2)
        records_by_source = {"testsrc": records}
        warn_lines = print_report(records_by_source)
        assert any("[WARN]" in line and "testsrc" in line for line in warn_lines)

    def test_no_warn_below_threshold(self):
        """~3% no-desc -> no warn lines for description."""
        # 100 records, 3 without description = 3% < 5% threshold
        records = self._make_source_records(100, no_desc=3)
        records_by_source = {"testsrc": records}
        warn_lines = print_report(records_by_source)
        desc_warns = [l for l in warn_lines if "missing description" in l and "testsrc" in l]
        assert len(desc_warns) == 0
