"""Deferred security requests refuse before scheduling, provider I/O or spend."""

from tests.fixtures.operator_http import (
    operator_app as operator_app,
    operator_client as operator_client,
)


import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from forge.durable.models import StepRun, EventInbox
from forge.runs.service import execute_run_command
from forge.runs.github_service import execute_github_run_command
from forge.runs.admission import check_admission
from forge.config import ForgeConfig, Settings

FIXTURES = Path(__file__).parent / "fixtures"


def security_payload(provider, review, mention=True):
    text = ("@forge " if mention else "") + "/security"
    if provider == "github":
        file = "issue_comment_pr.json" if review else "issue_comment_created.json"
        payload = json.loads((FIXTURES / "github_payloads" / file).read_text())
        payload["comment"]["body"] = text
    else:
        file = "note_mr.json" if review else "note_issue.json"
        payload = json.loads((FIXTURES / file).read_text())
        payload["object_attributes"]["note"] = text
    return payload


def signed_headers(provider, body):
    if provider == "github":
        signature = hmac.new(b"github-hook-secret", body, hashlib.sha256).hexdigest()
        return {
            "X-Hub-Signature-256": "sha256=" + signature,
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": "fixture-security",
        }
    return {"X-Gitlab-Token": "gitlab-hook-secret", "X-Gitlab-Event": "Note Hook"}


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("mention", [False, True])
async def test_security_is_refused_without_durable_acceptance(
    operator_app, operator_client, provider, review, mention
):
    operator_app.state.task_queue = AsyncMock()
    body = json.dumps(security_payload(provider, review, mention)).encode()
    path = "/webhook/github" if provider == "github" else "/webhook"
    response = await operator_client.post(
        path, content=body, headers=signed_headers(provider, body)
    )
    assert response.status_code in {200, 202}
    assert response.json() == {
        "status": "refused",
        "refusal_reason": "feature_not_available",
        "feature": "/security",
        "message": "/security is not available in this version.",
    }
    assert operator_app.state.task_queue.mock_calls == []
    async with operator_app.state.session_factory() as session:
        for model in (EventInbox, StepRun):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_unavailable_feature_does_not_bypass_webhook_authentication(
    operator_client, provider
):
    body = json.dumps(security_payload(provider, False)).encode()
    path = "/webhook/github" if provider == "github" else "/webhook"
    response = await operator_client.post(
        path,
        content=body,
        headers={"X-GitHub-Event": "issue_comment", "X-Gitlab-Event": "Note Hook"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("command", ["security_triage", "debug_ci", "debug_pipeline"])
async def test_pre_upgrade_deferred_commands_are_inert_before_any_dependency_read(
    provider, command
):
    await execute_run_command(None, None, None, {"provider": provider, "command": command})
    await execute_github_run_command(None, None, None, {"provider": "github", "command": command})


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_a_model_or_unlisted_actor_still_cannot_authorize_a_run(provider):
    settings = Settings(
        _env_file=None,
        FORGE_GITLAB_ENABLED=False,
        FORGE_APPROVERS="owner",
        FORGE_GITHUB_APPROVERS="github-owner",
    )
    assert not check_admission(settings, ForgeConfig(), 42, "model-suggestion", provider).allowed
    assert not check_admission(
        settings, ForgeConfig(), 42, settings.FORGE_BOT_USERNAME, provider
    ).allowed
    owner = "github-owner" if provider == "github" else "owner"
    assert check_admission(settings, ForgeConfig(), 42, owner, provider).allowed
