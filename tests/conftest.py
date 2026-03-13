"""Shared fixtures for all SkillFinder tests."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
RAW_DIR = FIXTURES_DIR / "raw"
DATA_DIR = Path(__file__).parent.parent / "data"


# ---------------------------------------------------------------------------
# Skill record builders
# ---------------------------------------------------------------------------

def make_skill(
    repo_url="https://github.com/user/test-skill",
    name="test-skill",
    description="A test skill for unit testing purposes.",
    source=None,
    stars=10,
    skillhub_rank=None,
    skillhub_score=None,
    safety_scan="clean",
    safety_flag=False,
    categories=None,
    triggers=None,
    install_cmd=None,
    last_updated="2026-01-01",
) -> dict:
    """Return a fully-populated unified skill record."""
    return {
        "id": "abc123",
        "repo_url": repo_url,
        "name": name,
        "description": description,
        "source": source or ["skillsmp"],
        "categories": categories or ["testing"],
        "triggers": triggers or [],
        "install_cmd": install_cmd or {"claude_code": f"/plugin install {name}"},
        "quality": {
            "stars": stars,
            "skillhub_rank": skillhub_rank,
            "skillhub_score": skillhub_score,
            "safety_scan": safety_scan,
            "safety_flag": safety_flag,
            "last_updated": last_updated,
        },
        "embedding_text": f"{name}. {description} Categories: testing.",
    }


def make_raw_record(
    repo_url="https://github.com/user/test-skill",
    name="test-skill",
    description="A test skill.",
    source="skillsmp",
    stars=10,
    pushed_at="2026-01-01",
    topics=None,
) -> dict:
    """Return a raw crawler record (as written by crawlers to data/raw/)."""
    return {
        "repo_url": repo_url,
        "name": name,
        "description": description,
        "source": source,
        "raw_metadata": {
            "stars": stars,
            "pushed_at": pushed_at,
            "topics": topics or [],
        },
    }


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def skill():
    return make_skill()


@pytest.fixture
def skill_with_triggers():
    return make_skill(
        triggers=["deploy kubernetes", "manage k8s", "blue-green deployment"]
    )


@pytest.fixture
def skill_flagged():
    return make_skill(safety_scan="warning: uses eval()", safety_flag=True)


@pytest.fixture
def skill_low_quality():
    """Skill that should fail quality filter: no stars, not in curated registry."""
    return make_skill(stars=0, source=["skillsmp"], skillhub_rank=None)


@pytest.fixture
def skill_curated():
    """Skill with 0 stars but in a curated registry — should pass quality filter."""
    return make_skill(stars=0, source=["clawhub"])


@pytest.fixture
def skills_for_search():
    """A set of skills covering different platforms for filter testing."""
    return [
        make_skill(
            name="k8s-deployer",
            description="Deploy Kubernetes clusters.",
            source=["skillsmp", "clawhub"],
            install_cmd={"claude_code": "/plugin install k8s-deployer", "openclaw": "clawhub install k8s-deployer"},
            safety_flag=False,
        ),
        make_skill(
            name="codex-helper",
            description="Help with OpenAI Codex tasks.",
            source=["skillsmp"],
            install_cmd={"codex": "cp SKILL.md ~/.codex/skills/"},
            safety_flag=False,
        ),
        make_skill(
            name="flagged-tool",
            description="A tool with a security warning.",
            source=["clawhub"],
            install_cmd={"claude_code": "/plugin install flagged-tool", "openclaw": "clawhub install flagged-tool"},
            safety_flag=True,
        ),
        make_skill(
            name="clawhub-only",
            description="Available only on OpenClaw.",
            source=["clawhub"],
            install_cmd={"openclaw": "clawhub install clawhub-only"},
            safety_flag=False,
        ),
    ]


@pytest.fixture
def raw_records_with_overlap():
    """
    Records for kubernetes-deployer appearing in three sources.
    Used to test dedup and metadata merge.
    """
    return [
        {
            "repo_url": "https://github.com/user/kubernetes-deployer",
            "name": "kubernetes-deployer",
            "description": "Deploy Kubernetes clusters from GitHub search.",
            "source": "skillsmp",
            "raw_metadata": {"stars": 142, "pushed_at": "2026-02-15", "topics": ["kubernetes", "devops"]},
        },
        {
            "repo_url": "https://github.com/user/kubernetes-deployer.git",  # .git suffix variant
            "name": "kubernetes-deployer",
            "description": "Deploy Kubernetes clusters from ClawHub.",
            "source": "clawhub",
            "raw_metadata": {"safety_scan": "clean", "categories": ["devops", "kubernetes"]},
        },
        {
            "repo_url": "https://github.com/user/kubernetes-deployer/",  # trailing slash variant
            "name": "kubernetes-deployer",
            "description": "Deploy and manage Kubernetes clusters with automated rollbacks.",
            "source": "skillhub",
            "raw_metadata": {"rank": "A", "overall_score": 8.4},
        },
    ]


@pytest.fixture
def tmp_raw_dir(tmp_path):
    """A temp directory containing copies of fixture raw JSONL files."""
    for src in RAW_DIR.glob("*.jsonl"):
        (tmp_path / src.name).write_bytes(src.read_bytes())
    return tmp_path


@pytest.fixture
def tmp_data_dir(tmp_path):
    """A temp data/ directory with a tiny FAISS index for search tests."""
    import faiss

    skills = [
        make_skill(name="kubernetes-deployer", description="Deploy Kubernetes clusters.", categories=["devops", "kubernetes"]),
        make_skill(name="docker-manager", description="Manage Docker containers.", categories=["devops", "docker"]),
        make_skill(name="terraform-tool", description="Apply Terraform infrastructure.", categories=["iac", "devops"]),
    ]

    rng = np.random.default_rng(42)
    dim = 1024
    vecs = rng.standard_normal((len(skills), dim)).astype(np.float32)
    faiss.normalize_L2(vecs)

    index = faiss.IndexFlatIP(dim)
    index.add(vecs)

    faiss.write_index(index, str(tmp_path / "index.faiss"))
    with open(tmp_path / "metadata.jsonl", "w") as f:
        for s in skills:
            f.write(json.dumps(s) + "\n")

    version_content = (
        "date: 2026-03-10\n"
        f"skill_count: {len(skills)}\n"
        "index_sha256: abc123\n"
        "metadata_sha256: def456\n"
    )
    (tmp_path / "version.txt").write_text(version_content)

    return tmp_path


@pytest.fixture
def mock_ollama_embed():
    """Returns a mock for embed_batch that returns deterministic random vectors."""
    def _embed(texts, model=None, ollama_url=None):
        rng = np.random.default_rng(abs(hash(str(texts))) % (2**31))
        vecs = rng.standard_normal((len(texts), 1024)).astype(np.float32)
        import faiss
        faiss.normalize_L2(vecs)
        return vecs
    return _embed


@pytest.fixture
def sample_skill_md():
    return (FIXTURES_DIR / "sample_skill.md").read_text()


@pytest.fixture
def test_queries():
    with open(FIXTURES_DIR / "test_queries.json") as f:
        return json.load(f)
