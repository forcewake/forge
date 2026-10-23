"""Portable webhook lessons retained on the supported signed ingress."""

import hashlib
import hmac
import json
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from forge.gateway.github_webhook import normalize_issue_comment, verify_github_signature
from forge.main import create_app
from forge.config import Settings

PAYLOAD = Path(__file__).parent / "fixtures/github_payloads/issue_comment_created.json"


def test_supported_comment_identity_is_stable_under_redelivery():
    payload = json.loads(PAYLOAD.read_text())
    first = normalize_issue_comment(payload)
    second = normalize_issue_comment(json.loads(json.dumps(payload)))
    assert first == second
    assert first["provider"] == "github"
    assert first["note_id"] == payload["comment"]["id"]
    assert first["command"] == "start_run"


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_authentication_binds_the_exact_raw_delivery(mutation):
    body = PAYLOAD.read_bytes()
    secret = "test-webhook-key"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(secret, body, signature)
    assert not verify_github_signature(
        secret,
        body + b" " if mutation == "tampered" else body,
        None if mutation == "missing" else signature,
    )


async def test_retired_ingress_has_no_route():
    app = create_app(settings=Settings(_env_file=None, FORGE_GITLAB_ENABLED=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhook/azure_devops", json={})
    assert response.status_code == 404
