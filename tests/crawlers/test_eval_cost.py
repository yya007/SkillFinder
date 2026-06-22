from unittest.mock import patch
from crawlers.eval_cost import measure


def test_measure_aggregates_per_skill_cost():
    # 2 repos, 1 SKILL.md each → 2 skills.
    with patch("crawlers.eval_cost.fetch_repo_metadata", return_value={"default_branch": "main"}), \
         patch("crawlers.eval_cost.find_skill_md_paths", return_value={"SKILL.md": "sha1"}), \
         patch("crawlers.eval_cost.fetch_skill_md", return_value="---\nname: x\n---\n"), \
         patch("crawlers.eval_cost.reset_api_counters"), \
         patch("crawlers.eval_cost.get_api_counters",
               return_value={"rest": 6, "search": 0, "raw_free": 2,
                             "conditional_304": 0, "graphql": 0}):
        result = measure(session=object(), repos=["a/b", "c/d"])
    assert result["skills"] == 2
    assert result["metered"] == 6          # rest + search
    assert result["free"] == 2             # raw_free + conditional_304
    assert result["per_skill"] == 3.0      # 6 metered / 2 skills
