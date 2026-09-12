import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.conftest import TEST_WEBHOOK_SECRET

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_webhook_rejects_missing_token(client: AsyncClient):
    response = await client.post("/webhook", json={})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_token(client: AsyncClient):
    response = await client.post(
        "/webhook",
        json={},
        headers={"X-Gitlab-Token": "wrong-secret"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_accepts_valid_token(client: AsyncClient):
    payload = json.loads((FIXTURES / "push.json").read_text())
    response = await client.post(
        "/webhook",
        json=payload,
        headers={
            "X-Gitlab-Token": TEST_WEBHOOK_SECRET,
            "X-Gitlab-Event": "Push Hook",
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["event"] == "push"


@pytest.mark.asyncio
async def test_webhook_accepts_unparseable_payload(client: AsyncClient):
    """Malformed payloads should still return 202, not 400."""
    # MergeRequestEvent requires object_attributes with id, iid, title — missing here
    response = await client.post(
        "/webhook",
        json={"object_kind": "merge_request"},
        headers={
            "X-Gitlab-Token": TEST_WEBHOOK_SECRET,
            "X-Gitlab-Event": "Merge Request Hook",
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["parsed"] is False
