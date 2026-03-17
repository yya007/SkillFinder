"""
Search quality tests — Recall@30 on the labeled test suite.

SKIP CONDITIONS (both must be true to run):
  - data/index.faiss exists (pre-built index downloaded)
  - Ollama is reachable with qwen3-embedding:0.6b loaded

Run with: pytest tests/quality/ -v -m quality
Skip in CI by default: pytest -m "not quality"
"""
import json
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent.parent.parent / "data"
FIXTURES = Path(__file__).parent.parent / "fixtures"
INDEX_PATH = DATA_DIR / "index.faiss"
METADATA_PATH = DATA_DIR / "metadata.jsonl"

RECALL_TARGET = 0.90  # Recall@30 >= 90%


def _ollama_available() -> bool:
    try:
        import requests
        r = requests.get("http://localhost:11434/", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


requires_index = pytest.mark.skipif(
    not INDEX_PATH.exists(),
    reason="data/index.faiss not found — run: python scripts/update_index.py",
)

requires_ollama = pytest.mark.skipif(
    not _ollama_available(),
    reason="Ollama not reachable — run: ollama serve && ollama pull qwen3-embedding:0.6b",
)

pytestmark = [pytest.mark.quality, requires_index, requires_ollama]


@pytest.fixture(scope="module")
def loaded_index():
    from scripts.search import load_index
    return load_index(str(INDEX_PATH), str(METADATA_PATH))


@pytest.fixture(scope="module")
def queries():
    with open(FIXTURES / "test_queries.json") as f:
        return json.load(f)


def _search_for_skill(query: str, index, metadata) -> list[str]:
    """Run a search and return list of result skill names."""
    from scripts.search import search
    results = search(query, index, metadata, propose_n=10)
    return [r["name"] for r in results]


class TestSearchQuality:
    def test_recall_at_30_meets_target(self, loaded_index, queries):
        """Expected skill must appear somewhere in the 30-candidate pool for ≥90% of queries."""
        index, metadata = loaded_index
        hits = 0
        misses = []

        for item in queries:
            result_names = _search_for_skill(item["query"], index, metadata)
            if item["expected_skill_name"] in result_names:
                hits += 1
            else:
                misses.append({
                    "query": item["query"],
                    "expected": item["expected_skill_name"],
                    "got": result_names[:5],
                })

        recall = hits / len(queries)
        miss_report = "\n".join(
            f"  MISS: '{m['query']}' → expected '{m['expected']}', got {m['got']}"
            for m in misses
        )
        assert recall >= RECALL_TARGET, (
            f"Recall@30 = {recall:.2%} (target {RECALL_TARGET:.0%})\n"
            f"{len(misses)} misses:\n{miss_report}"
        )

    def test_all_queries_return_at_least_one_result(self, loaded_index, queries):
        """No query should return an empty result set."""
        index, metadata = loaded_index
        empty_queries = []
        for item in queries:
            results = _search_for_skill(item["query"], index, metadata)
            if not results:
                empty_queries.append(item["query"])
        assert not empty_queries, f"Queries with zero results: {empty_queries}"

    def test_results_have_valid_sim_scores(self, loaded_index, queries):
        """All sim_score values must be in [0, 1]."""
        from scripts.search import search
        index, metadata = loaded_index
        for item in queries[:5]:  # sample 5 queries
            results = search(item["query"], index, metadata, propose_n=10)
            for r in results:
                assert 0.0 <= r["sim_score"] <= 1.0

    def test_platform_filter_does_not_crash(self, loaded_index, queries):
        """Platform filter must not throw even if it eliminates all results."""
        from scripts.search import search
        index, metadata = loaded_index
        for item in queries[:3]:
            results = search(
                item["query"], index, metadata,
                propose_n=10,
                platforms=["claude_code"],
            )
            assert isinstance(results, list)

