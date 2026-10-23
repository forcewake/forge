"""Portable authentication lessons on the current operator HTTP owner.

The MCP transport, master scope, token glob configuration and provider
passthrough tools are retired. These checks exercise actual HTTP handlers.
"""

from tests.fixtures.operator_http import (
    operator_app as operator_app,
    operator_client as operator_client,
)


from httpx import ASGITransport, AsyncClient
import pytest
from forge.config import Settings
from forge.main import create_app
from tests.fixtures.operator_http import (
    SUBJECT_A,
    SUBJECT_B,
    scope,
)


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic wrong", "Bearer"])
async def test_missing_or_invalid_authentication_never_opens_the_surface(
    operator_client, authorization
):
    params, headers = scope()
    if authorization is None:
        headers = {}
    else:
        headers["Authorization"] = authorization
    response = await operator_client.get("/operator/runs", params=params, headers=headers)
    assert response.status_code in {401, 403}


@pytest.mark.parametrize("secret", [None, ""])
async def test_missing_server_secret_is_disabled_not_unauthenticated(secret):
    app = create_app(
        Settings(_env_file=None, FORGE_GITLAB_ENABLED=False, FORGE_LANE_CONTROL_SECRET=secret)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://forge.test"
    ) as client:
        params, headers = scope()
        response = await client.get("/operator/runs", params=params, headers=headers)
    assert response.status_code == 503


@pytest.mark.parametrize("declared", [(SUBJECT_B,), (SUBJECT_A, SUBJECT_B)])
async def test_a_grant_cannot_widen_its_canonical_subject_scope(operator_client, declared):
    params, headers = scope(declared, signed=(SUBJECT_A,))
    response = await operator_client.get("/operator/runs", params=params, headers=headers)
    assert response.status_code == 403


async def test_a_bearer_cannot_omit_its_subject_scope(operator_client):
    _, headers = scope()
    response = await operator_client.get("/operator/runs", headers=headers)
    assert response.status_code == 400


@pytest.mark.parametrize("reference", ["*", "acme/*", "42", "github/foreign/acme/alpha"])
async def test_name_globs_and_numeric_ids_are_not_canonical_scope_grants(
    operator_client, reference
):
    _, headers = scope()
    response = await operator_client.get(
        "/operator/runs", params={"subject": reference}, headers=headers
    )
    assert response.status_code in {400, 403}


@pytest.mark.parametrize("path", ["/operator/runs", "/operator/runs/" + "a" * 32])
async def test_operator_reads_offer_no_write_method(operator_client, path):
    params, headers = scope()
    response = await operator_client.post(
        path, params=params, headers=headers, json={"status": "ready_for_human"}
    )
    assert response.status_code == 405


async def test_denial_never_echoes_the_bearer(operator_client, caplog):
    token = "secret-bearer-do-not-log"
    response = await operator_client.get(
        "/operator/runs", params=scope()[0], headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 403
    assert token not in response.text and token not in caplog.text
