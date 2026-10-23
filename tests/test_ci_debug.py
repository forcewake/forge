"""CI failures no longer schedule the deferred diagnostic lane.

The candidate's existing bounded repair loop and evidence policy remain.
"""

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
from forge.policy.evidence import EvidencePolicy

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("conclusion", ["failure", "success", "cancelled"])
async def test_github_workflow_jobs_are_recorded_without_debug_work(
    operator_app, operator_client, conclusion
):
    payload = json.loads((FIXTURES / "github_payloads/workflow_job_failed.json").read_text())
    payload["workflow_job"]["conclusion"] = conclusion
    body = json.dumps(payload).encode()
    signature = hmac.new(b"github-hook-secret", body, hashlib.sha256).hexdigest()
    operator_app.state.task_queue = AsyncMock()
    response = await operator_client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-Hub-Signature-256": "sha256=" + signature,
            "X-GitHub-Event": "workflow_job",
            "X-GitHub-Delivery": "fixture-job",
        },
    )
    assert response.status_code in {200, 202}
    assert not response.json().get("run_command")
    assert operator_app.state.task_queue.mock_calls == []
    async with operator_app.state.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(StepRun)) == 0
        assert await session.scalar(select(func.count()).select_from(EventInbox)) == 1


async def test_failed_gitlab_pipeline_never_falls_into_the_legacy_debugger(
    operator_app, operator_client
):
    payload = json.loads((FIXTURES / "pipeline_failed.json").read_text())
    operator_app.state.task_queue = AsyncMock()
    response = await operator_client.post(
        "/webhook",
        json=payload,
        headers={
            "X-Gitlab-Token": "gitlab-hook-secret",
            "X-Gitlab-Event": "Pipeline Hook",
        },
    )
    assert response.status_code == 202
    assert response.json()["refusal_reason"] == "feature_not_available"
    assert response.json()["feature"] == "ci_debug"
    assert operator_app.state.task_queue.mock_calls == []


@pytest.mark.parametrize(
    "secret", ["glpat-fake-fixture-value", "ghs_fake-fixture-value", "sk-fake-fixture-value"]
)
def test_log_evidence_remains_redacted_and_bounded_on_the_actual_policy(secret):
    result, changed = EvidencePolicy(max_chars=80).apply_policy(
        "error " + secret + "\n" + "x" * 1000
    )
    assert changed and secret not in result
    assert "[REDACTED:rule]" in result and len(result) == 80
