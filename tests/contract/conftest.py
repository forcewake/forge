"""Native adapter contract fixtures: no live provider and no retiring client import."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from forge.port_values import Actor
from forge.providers.gitlab.ingress import GitLabIngress
from tests import native_clients_fixture as native_fixture
from tests import test_native_git_publication as git_proof

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "gitlab"
SECRET = b"synthetic-contract-secret-32-bytes!"
HEADERS = {
    "mr_webhook": "Merge Request Hook",
    "note_webhook": "Note Hook",
    "pipeline_webhook": "Pipeline Hook",
    "push_webhook": "Push Hook",
}
gitlab, gitlab_native, native = (
    native_fixture.gitlab,
    native_fixture.gitlab_native,
    git_proof.native,
)


def load_fixture(name):
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


@pytest.fixture
def fixtures():
    return load_fixture


@pytest.fixture
def ingress():
    bot = Actor(
        provider="gitlab",
        connection_id="contract-ce",
        native_id="99",
        login="forge-bot",
        is_bot=True,
    )
    return GitLabIngress("contract-ce", SECRET, bots={"42": bot})


def signed(payload, event):
    return (
        {
            "X-Gitlab-Event": event,
            "X-Gitlab-Event-UUID": str(uuid4()),
            "X-Gitlab-Token": SECRET.decode(),
        },
        json.dumps(payload).encode(),
    )


def complete_note():
    """Explicit complete native target variant of the historical partial note capture."""
    payload = load_fixture("note_webhook")
    payload["object_attributes"]["action"] = "create"
    payload["merge_request"].update(source_project_id=42, target_project_id=42)
    return payload
