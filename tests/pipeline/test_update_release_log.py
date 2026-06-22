"""Unit tests for pipeline/update_release_log.py."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.update_release_log import (
    main,
    parse_version_txt,
    render_markdown,
    upsert,
)

_VERSION_TXT = (
    "date: 2026-06-22\n"
    "skill_count: 42068\n"
    "embed_model: qwen3-embedding:0.6b\n"
    "index_sha256: abc123\n"
    "metadata_sha256: def456\n"
)


def _write_metadata(path: Path, sources_per_record: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for srcs in sources_per_record:
            fh.write(json.dumps({"source": srcs, "quality": {"stars": 10}}) + "\n")


class TestParseVersionTxt:
    def test_parses_keys(self, tmp_path):
        p = tmp_path / "version.txt"
        p.write_text(_VERSION_TXT)
        v = parse_version_txt(p)
        assert v["date"] == "2026-06-22"
        assert v["skill_count"] == "42068"
        assert v["embed_model"] == "qwen3-embedding:0.6b"


class TestUpsert:
    def test_replaces_same_date_and_sorts(self):
        records = [
            {"date": "2026-03-17", "skill_count": 37962},
            {"date": "2026-06-22", "skill_count": 40000},
        ]
        out = upsert(records, {"date": "2026-06-22", "skill_count": 42068})
        # same date replaced, not duplicated; sorted ascending
        assert [r["date"] for r in out] == ["2026-03-17", "2026-06-22"]
        assert out[-1]["skill_count"] == 42068

    def test_appends_new_date(self):
        out = upsert([{"date": "2026-03-17", "skill_count": 1}], {"date": "2026-06-22", "skill_count": 2})
        assert len(out) == 2


class TestRenderMarkdown:
    def test_table_newest_first_with_delta(self):
        records = [
            {"date": "2026-03-17", "skill_count": 100, "sources": {}, "embed_model": None, "npm_version": None},
            {"date": "2026-06-22", "skill_count": 130, "sources": {"topic": 130}, "embed_model": "m", "npm_version": "0.1.2"},
        ]
        md = render_markdown(records)
        lines = md.splitlines()
        # header table present
        assert any(line.startswith("| Date ") for line in lines)
        # newest row appears before the older row
        i_new = next(i for i, ln in enumerate(lines) if "2026-06-22" in ln)
        i_old = next(i for i, ln in enumerate(lines) if "2026-03-17" in ln)
        assert i_new < i_old
        # delta of the newest vs previous is +30
        assert "+30" in lines[i_new]


class TestMainEndToEnd:
    def _run(self, tmp_path, sources_per_record):
        version = tmp_path / "version.txt"
        version.write_text(_VERSION_TXT)
        metadata = tmp_path / "metadata.jsonl"
        _write_metadata(metadata, sources_per_record)
        log = tmp_path / "release_log.jsonl"
        md = tmp_path / "release-log.md"
        rc = main([
            "--version", str(version), "--metadata", str(metadata),
            "--log", str(log), "--md", str(md),
        ])
        return rc, log, md

    def test_creates_log_with_source_breakdown(self, tmp_path):
        rc, log, md = self._run(tmp_path, [["topic"], ["topic"], ["clawhub"]])
        assert rc == 0
        records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert len(records) == 1
        entry = records[0]
        assert entry["date"] == "2026-06-22"
        assert entry["skill_count"] == 42068
        assert entry["sources"] == {"topic": 2, "clawhub": 1}
        assert entry["embed_model"] == "qwen3-embedding:0.6b"
        assert "42,068" in md.read_text()

    def test_idempotent_same_date(self, tmp_path):
        version = tmp_path / "version.txt"
        version.write_text(_VERSION_TXT)
        metadata = tmp_path / "metadata.jsonl"
        _write_metadata(metadata, [["topic"]])
        log = tmp_path / "release_log.jsonl"
        md = tmp_path / "release-log.md"
        argv = ["--version", str(version), "--metadata", str(metadata),
                "--log", str(log), "--md", str(md)]
        main(argv)
        main(argv)  # re-run for the same date
        records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert len(records) == 1  # not duplicated

    def test_records_npm_version(self, tmp_path):
        version = tmp_path / "version.txt"
        version.write_text(_VERSION_TXT)
        metadata = tmp_path / "metadata.jsonl"
        _write_metadata(metadata, [["topic"]])
        log = tmp_path / "release_log.jsonl"
        md = tmp_path / "release-log.md"
        main(["--version", str(version), "--metadata", str(metadata),
              "--log", str(log), "--md", str(md), "--npm-version", "0.1.2"])
        entry = json.loads(log.read_text().splitlines()[0])
        assert entry["npm_version"] == "0.1.2"

    def test_missing_version_file_returns_error(self, tmp_path):
        rc = main(["--version", str(tmp_path / "nope.txt"),
                   "--log", str(tmp_path / "log.jsonl"), "--md", str(tmp_path / "md.md")])
        assert rc == 1
