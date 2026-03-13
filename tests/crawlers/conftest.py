"""Shared fixtures for crawler tests."""
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def github_session():
    """A real requests.Session with no token (for network tests)."""
    from crawlers.base import make_session
    return make_session()


@pytest.fixture
def mock_session():
    """A mock session for unit tests."""
    return MagicMock()


_REPO_ID_COUNTER = 1


def make_github_repo(
    full_name="user/test-skill",
    stars=10,
    pushed_at="2026-01-01",
    topics=None,
    description="A test skill.",
    default_branch="main",
    repo_id=None,
):
    """Build a fake GitHub repo API response dict."""
    global _REPO_ID_COUNTER
    if repo_id is None:
        repo_id = _REPO_ID_COUNTER
        _REPO_ID_COUNTER += 1
    owner, repo = full_name.split("/")
    return {
        "id": repo_id,
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "name": repo,
        "description": description,
        "stargazers_count": stars,
        "pushed_at": pushed_at,
        "topics": topics or [],
        "default_branch": default_branch,
    }


SAMPLE_SKILL_MD = """---
name: test-skill
description: A test skill for unit testing.
triggers:
  - run tests
  - pytest
---
# Test Skill
This skill runs your test suite.
"""

SAMPLE_AWESOME_README = """# Awesome OpenClaw Skills

## DevOps

- [k8s-deployer](https://github.com/user/k8s-deployer) — Deploy Kubernetes clusters
- [docker-manager](https://github.com/user/docker-manager) — Manage Docker containers

## Testing

- [test-runner](https://github.com/user/test-runner) — Run test suites automatically
"""
