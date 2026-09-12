"""Shared fixtures for the GitLab API contract tests.

These tests pin ``forge.gitlab.client.GitLabClient`` and
``forge.gateway.parser.parse_webhook`` to the DOCUMENTED GitLab REST API v4
and documented webhook payload shapes. All HTTP traffic is mocked with
pytest-httpx; nothing here touches a real GitLab instance.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forge.gitlab.client import GitLabClient

BASE_URL = "https://gitlab.example.com"
BASE = f"{BASE_URL}/api/v4"

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "gitlab"


def load_fixture(name: str) -> Any:
    """Load a JSON fixture from tests/contract/fixtures/gitlab/<name>.json."""
    with (FIXTURES_DIR / f"{name}.json").open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def gitlab_client() -> GitLabClient:
    """Client pointing at a fake GitLab host intercepted by pytest-httpx."""
    return GitLabClient(base_url=BASE_URL, token="forge-test-token", timeout=5.0)


@pytest.fixture()
def fixtures() -> Callable[[str], Any]:
    """Return the fixture loader function."""
    return load_fixture
