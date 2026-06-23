"""Unit tests for pipeline/render_release_notes.py."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.render_release_notes import main, render

_VERSION_TXT = (
    "date: 2026-06-22\n"
    "skill_count: 42068\n"
    "embed_model: qwen3-embedding:0.6b\n"
    "index_sha256: abc\n"
)

_LOG = [
    {"date": "2026-03-17", "skill_count": 37962, "sources": {}},
    {"date": "2026-06-22", "skill_count": 42068,
     "sources": {"marketplace": 28253, "topic": 15397}},
]


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    vf = tmp_path / "version.txt"
    vf.write_text(_VERSION_TXT)
    log = tmp_path / "release_log.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in _LOG) + "\n")
    return vf, log


class TestRender:
    def test_includes_version_count_and_delta(self, tmp_path):
        vf, log = _setup(tmp_path)
        body = render("0.1.3", vf, log, None)
        assert "v0.1.3" in body
        assert "42,068" in body
        assert "+4,106" in body  # 42068 - 37962

    def test_includes_sources_and_install(self, tmp_path):
        vf, log = _setup(tmp_path)
        body = render("0.1.3", vf, log, None)
        assert "marketplace 28,253" in body
        # current Claude Code install (the /plugin flow, not old cp commands)
        assert "/plugin install skill-finder@skillfinder" in body
        assert "cp -r" not in body
        assert "ollama pull qwen3-embedding:0.6b" in body

    def test_custom_highlights_used(self, tmp_path):
        vf, log = _setup(tmp_path)
        body = render("0.1.3", vf, log, "Fixed the topic crawler; added GraphQL batch.")
        assert "Fixed the topic crawler" in body

    def test_first_release_has_no_delta_number(self, tmp_path):
        vf = tmp_path / "version.txt"
        vf.write_text("date: 2026-03-14\nskill_count: 4599\nembed_model:\n")
        log = tmp_path / "release_log.jsonl"
        log.write_text(json.dumps({"date": "2026-03-14", "skill_count": 4599, "sources": {}}) + "\n")
        body = render("0.1.0", vf, log, None)
        assert "first release" in body


class TestMain:
    def test_writes_to_stdout(self, tmp_path, capsys):
        vf, log = _setup(tmp_path)
        rc = main(["--npm-version", "0.1.3", "--version-file", str(vf), "--log", str(log)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "v0.1.3" in out and "42,068" in out

    def test_missing_version_file_errors(self, tmp_path):
        rc = main(["--npm-version", "0.1.3", "--version-file", str(tmp_path / "nope.txt"),
                   "--log", str(tmp_path / "log.jsonl")])
        assert rc == 1
