"""
Unit tests for crawlers/skillsmp_deep_crawler.py

Covers:
  - load_state: returns empty dict when file missing, loads JSON otherwise
  - save_state / load_state round-trip
  - date-shard exhaustion logic (< 950 → exhausted, >= 950 → not exhausted)
  - resume: pre-loaded exhausted shards are skipped (no API calls)
  - target_per_cell: reaching the target stops remaining date shards
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from crawlers.skillsmp_deep_crawler import (
    DATE_SHARDS,
    _OVERFLOW_THRESHOLD,
    cell_key,
    get_cell_state,
    load_state,
    save_state,
)


# ---------------------------------------------------------------------------
# load_state
# ---------------------------------------------------------------------------

class TestLoadState:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        result = load_state(str(tmp_path / "no_such_state.json"))
        assert result == {}

    def test_loads_existing_state(self, tmp_path):
        data = {"cell1": {"collected": 5, "exhausted_date_shards": ["pushed:<2018-01-01"]}}
        p = tmp_path / "state.json"
        p.write_text(json.dumps(data))
        result = load_state(str(p))
        assert result == data

    def test_invalid_json_returns_empty_dict(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("{ not valid json }")
        result = load_state(str(p))
        assert result == {}


# ---------------------------------------------------------------------------
# save_state + load_state round-trip
# ---------------------------------------------------------------------------

class TestSaveAndReloadState:
    def test_round_trip_preserves_values(self, tmp_path):
        state = {
            "size:1..500|stars:0": {
                "collected": 42,
                "exhausted_date_shards": ["pushed:<2018-01-01", "pushed:2018-01-01..2019-01-01"],
            }
        }
        state_path = str(tmp_path / "state.json")
        save_state(state, state_path)
        reloaded = load_state(state_path)
        assert reloaded == state

    def test_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "deep" / "dir" / "state.json"
        save_state({"x": 1}, str(nested))
        assert nested.exists()

    def test_atomic_write_uses_tmp_file(self, tmp_path):
        """save_state should NOT leave a .tmp file around after success."""
        state_path = str(tmp_path / "state.json")
        save_state({"a": 1}, state_path)
        tmp_file = Path(state_path).with_suffix(".tmp")
        assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# Date-shard exhaustion logic
# ---------------------------------------------------------------------------

class TestExhaustionLogic:
    """
    The run() loop marks a date shard exhausted when the sub-query returns
    fewer than _OVERFLOW_THRESHOLD items.  We test this via the state dict
    that run() updates after each cell.
    """

    def _make_run_args(self, tmp_path, exhausted_date_shards=None):
        """Minimal keyword args for run() that process a single cell."""
        output = str(tmp_path / "out.jsonl")
        state_path = str(tmp_path / "state.json")

        # Pre-seed state so we only process one cell and can inspect state afterward
        from crawlers.skillsmp_deep_crawler import SIZE_SHARDS, STAR_SHARDS
        size_s, star_s = SIZE_SHARDS[0], STAR_SHARDS[0]
        key = cell_key(size_s, star_s)

        initial_state = {key: {"collected": 0, "exhausted_date_shards": exhausted_date_shards or []}}
        save_state(initial_state, state_path)

        return dict(
            output_path=output,
            state_path=state_path,
            target_per_cell=5,
            token=None,
            limit_per_cell=5,
            resume=True,
            only_cell=(size_s, star_s),
        ), key

    def test_cell_marked_exhausted_when_under_threshold(self, tmp_path):
        """When a date shard returns < 950 items, it's added to exhausted_date_shards."""
        run_args, key = self._make_run_args(tmp_path)
        first_date_shard = DATE_SHARDS[0]

        # Make the first date shard return 0 items (< 950 → exhausted)
        # and make all subsequent date shards return 0 items too so run() finishes quickly
        with patch("crawlers.skillsmp_deep_crawler._search_date_shard", return_value=iter([])):
            from crawlers.skillsmp_deep_crawler import run
            run(**run_args)

        state = load_state(run_args["state_path"])
        exhausted = state.get(key, {}).get("exhausted_date_shards", [])
        assert first_date_shard in exhausted, (
            f"Expected {first_date_shard!r} to be exhausted, got: {exhausted}"
        )

    def test_cell_not_marked_exhausted_at_overflow(self, tmp_path):
        """When a date shard yields >= 950 items, it is NOT marked exhausted."""
        # Note: limit_per_cell must be None (or >= _OVERFLOW_THRESHOLD) so that
        # _search_date_shard can yield all overflow items to run()'s counter.
        run_args, key = self._make_run_args(tmp_path)
        run_args["limit_per_cell"] = None  # no cap — let all items through
        first_date_shard = DATE_SHARDS[0]

        # Simulate exactly _OVERFLOW_THRESHOLD items for the first shard, none for others
        overflow_items = [
            {"repository": {"id": i, "full_name": f"user/repo{i}",
                            "html_url": f"https://github.com/user/repo{i}",
                            "name": f"repo{i}", "owner": {"login": "user"}}}
            for i in range(_OVERFLOW_THRESHOLD)
        ]

        call_count = {"n": 0}

        def fake_search(session, query_extra, per_page=100, limit=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                yield from iter(overflow_items)
            else:
                yield from iter([])

        # build_raw_record returns None → records don't count toward target,
        # so run() processes all _OVERFLOW_THRESHOLD items before moving on
        with patch("crawlers.skillsmp_deep_crawler._search_date_shard", side_effect=fake_search):
            with patch("crawlers.skillsmp_deep_crawler.fetch_repo_metadata", return_value={"default_branch": "main"}):
                with patch("crawlers.skillsmp_deep_crawler._fetch_skill_md", return_value=None):
                    with patch("crawlers.skillsmp_deep_crawler.build_raw_record", return_value=None):
                        from crawlers.skillsmp_deep_crawler import run
                        run(**run_args)

        state = load_state(run_args["state_path"])
        exhausted = state.get(key, {}).get("exhausted_date_shards", [])
        assert first_date_shard not in exhausted, (
            f"Shard yielded {_OVERFLOW_THRESHOLD} items but was wrongly marked exhausted"
        )

    def test_resume_skips_exhausted_date_shards(self, tmp_path):
        """Pre-loaded exhausted shards produce no _search_date_shard calls for those shards."""
        # Pre-exhaust all but the last date shard
        pre_exhausted = DATE_SHARDS[:-1]
        run_args, key = self._make_run_args(tmp_path, exhausted_date_shards=list(pre_exhausted))

        call_args_list = []

        def fake_search(session, query_extra, per_page=100, limit=None):
            call_args_list.append(query_extra)
            return iter([])

        with patch("crawlers.skillsmp_deep_crawler._search_date_shard", side_effect=fake_search):
            from crawlers.skillsmp_deep_crawler import run
            run(**run_args)

        # Only the last (non-exhausted) date shard should have been queried
        for exhausted_shard in pre_exhausted:
            for call_extra in call_args_list:
                assert exhausted_shard not in call_extra, (
                    f"Exhausted shard {exhausted_shard!r} was queried: {call_extra!r}"
                )

    def test_target_reached_stops_early(self, tmp_path):
        """Once target_count new records are collected, remaining date shards are skipped."""
        run_args, key = self._make_run_args(tmp_path)
        run_args["target_per_cell"] = 1  # Stop after 1 record

        # Each date shard call returns 1 valid record (a skill)
        records_emitted = []

        def fake_search(session, query_extra, per_page=100, limit=None):
            shard_idx = len(records_emitted)
            yield {
                "repository": {
                    "id": shard_idx,
                    "full_name": f"user/repo{shard_idx}",
                    "html_url": f"https://github.com/user/repo{shard_idx}",
                    "name": f"repo{shard_idx}",
                    "owner": {"login": "user"},
                }
            }

        good_record = {
            "name": "test-skill",
            "repo_url": "https://github.com/user/repo0",
            "description": "A test skill.",
            "source": "skillsmp",
            "raw_metadata": {"stars": 5},
        }

        search_call_count = {"n": 0}

        def counting_search(session, query_extra, per_page=100, limit=None):
            search_call_count["n"] += 1
            yield {
                "repository": {
                    "id": search_call_count["n"],
                    "full_name": f"user/repo{search_call_count['n']}",
                    "html_url": f"https://github.com/user/repo{search_call_count['n']}",
                    "name": f"repo{search_call_count['n']}",
                    "owner": {"login": "user"},
                }
            }

        with patch("crawlers.skillsmp_deep_crawler._search_date_shard", side_effect=counting_search):
            with patch("crawlers.skillsmp_deep_crawler.fetch_repo_metadata", return_value={"default_branch": "main"}):
                with patch("crawlers.skillsmp_deep_crawler._fetch_skill_md", return_value="---\nname: test\n---"):
                    with patch("crawlers.skillsmp_deep_crawler.build_raw_record", return_value=good_record):
                        from crawlers.skillsmp_deep_crawler import run
                        run(**run_args)

        # With target=1, only 1 date shard should have been queried
        assert search_call_count["n"] <= len(DATE_SHARDS), "More shards queried than exist"
        # The key assertion: we didn't query all 10 shards just to get 1 record
        assert search_call_count["n"] < len(DATE_SHARDS), (
            f"Expected early stop: only 1 record needed but queried all {len(DATE_SHARDS)} shards"
        )
