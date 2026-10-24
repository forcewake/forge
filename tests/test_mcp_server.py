"""Retained authentication and scope lessons on the sole operator HTTP surface."""

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from forge.runtime.app import create_app
from forge.runtime.assembly import open_runtime
from forge.runtime.settings import RuntimeSettings
from forge.store.records import RunRequest
from forge.store.runs import RunStore
from tests.operator_native_fixture import operator_native as operator_native
from tests.runtime_fixture import configuration
from tests.test_runtime_app import command


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic wrong", "Bearer"])
async def test_missing_or_invalid_authentication_never_opens_the_surface(
    operator_native, authorization
):
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await operator_native.client.get("/api/v1/runs", headers=headers)
    assert response.status_code == 401
    assert not operator_native.peer.calls and not operator_native.gateway.writes


@pytest.mark.parametrize("secret", [None, ""])
async def test_missing_server_secret_is_disabled_not_unauthenticated(tmp_path, secret):
    config, values = configuration(tmp_path)
    from forge.runtime.assembly import database
    from forge.store.schema import initialize

    engine = database(values["SYNTHETIC_DATABASE_URL"])
    await initialize(engine)
    await engine.dispose()
    if secret is None:
        del values["SYNTHETIC_SECRET"]
    else:
        values["SYNTHETIC_SECRET"] = secret
    with pytest.raises(ValueError, match="configured secret"):
        async with open_runtime(config, environ=values):
            pytest.fail("missing signing secret cannot start runtime")
    app = create_app(config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://forge.invalid"
    ) as client:
        assert (await client.get("/api/v1/runs")).status_code == 503


@pytest.mark.parametrize("declared", ["private", "*"])
async def test_a_grant_cannot_widen_its_canonical_subject_scope(operator_native, declared):
    fixture = operator_native
    foreign = await RunStore(fixture.service.factory).admit(
        RunRequest(uuid4().hex, "gitlab", "private", "123", "issue", "7")
    )
    response = await fixture.client.get("/api/v1/runs/" + foreign.id, headers=fixture.headers)
    assert response.status_code == 403
    response = await fixture.client.post(
        "/api/v1/commands", headers=fixture.headers, json=command(repository=declared)
    )
    assert response.status_code == 403 and not fixture.peer.calls and not fixture.gateway.writes


async def test_a_bearer_cannot_omit_its_subject_scope(tmp_path):
    config, _ = configuration(tmp_path)
    value = config.model_dump(mode="json", by_alias=True)
    value["operators"][0]["grants"] = []
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("reference", ["*", "acme/*", "42", "github/foreign/acme/alpha"])
async def test_name_globs_and_numeric_ids_are_not_canonical_scope_grants(
    operator_native, reference
):
    response = await operator_native.client.post(
        "/api/v1/commands", headers=operator_native.headers, json=command(repository=reference)
    )
    assert response.status_code == 403 and not operator_native.peer.calls


@pytest.mark.parametrize("path", ["/api/v1/runs", "/api/v1/runs/" + "a" * 32])
async def test_operator_reads_offer_no_write_method(operator_native, path):
    response = await operator_native.client.post(
        path, headers=operator_native.headers, json={"state": "ready_for_human"}
    )
    assert response.status_code == 405
    assert not operator_native.peer.calls and not operator_native.gateway.writes


async def test_denial_never_echoes_the_bearer(operator_native, caplog):
    token = "secret-bearer-do-not-log"
    response = await operator_native.client.get(
        "/api/v1/runs", headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 401
    assert token not in response.text and token not in caplog.text
