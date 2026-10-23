"""Modern commands retain durable acceptance; legacy mentions execute nothing."""

import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from forge.durable.models import StepRun
from tests.fixtures.operator_http import (
    operator_app as operator_app,
    operator_client as operator_client,
)

FIXTURE = Path(__file__).parent / "fixtures/note_issue.json"


@pytest.mark.parametrize(
    "text",
    [
        "@forge /review",
        "@forge /debug",
        "@forge /help",
        "@forge hello",
        "/full-review",
        "ordinary comment",
    ],
)
async def test_legacy_mentions_never_queue_an_execution(operator_app, operator_client, text):
    payload = json.loads(FIXTURE.read_text())
    payload["object_attributes"]["note"] = text
    operator_app.state.task_queue = AsyncMock()
    response = await operator_client.post(
        "/webhook",
        json=payload,
        headers={"X-Gitlab-Token": "gitlab-hook-secret", "X-Gitlab-Event": "Note Hook"},
    )
    assert response.status_code == 202 and response.json()["status"] == "ignored"
    assert operator_app.state.task_queue.mock_calls == []
    async with operator_app.state.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(StepRun)) == 0


@pytest.mark.parametrize("text", ["/status", "@forge /status"])
async def test_supported_operator_commands_still_commit_before_queue_wakeup(
    operator_app, operator_client, text
):
    payload = json.loads(FIXTURE.read_text())
    payload["object_attributes"]["note"] = text
    queue = AsyncMock()
    queue.is_duplicate.return_value = False
    operator_app.state.task_queue = queue
    response = await operator_client.post(
        "/webhook",
        json=payload,
        headers={"X-Gitlab-Token": "gitlab-hook-secret", "X-Gitlab-Event": "Note Hook"},
    )
    assert response.status_code == 202 and response.json()["run_command"]
    async with operator_app.state.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(StepRun)) == 1
    queue.submit.assert_awaited_once()
