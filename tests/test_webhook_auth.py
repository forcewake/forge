"""Native authentication and strict shape checks precede any durable acceptance."""

from tests.native_hook_fixture import post
from tests.operator_native_fixture import operator_native as operator_native
from tests.test_gitlab_engine_ingress import payload, signed


async def test_webhook_rejects_missing_token(operator_native):
    env = operator_native
    headers, _ = signed(payload())
    del headers["X-Gitlab-Token"]
    assert (await post(env, headers=headers)).status_code == 400
    assert not env.peer.calls and not env.gateway.writes


async def test_webhook_rejects_wrong_token(operator_native):
    env = operator_native
    headers, _ = signed(payload())
    headers["X-Gitlab-Token"] = "wrong-secret"
    assert (await post(env, headers=headers)).status_code == 400
    assert not env.peer.calls and not env.gateway.writes


async def test_webhook_accepts_valid_token(operator_native):
    env = operator_native
    response = await post(env)
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "commands": []}
    assert not env.peer.calls and not env.gateway.writes


async def test_webhook_accepts_unparseable_payload(operator_native):
    # Historical tolerant acceptance is retired: malformed native facts refuse.
    env = operator_native
    response = await post(env, {"object_kind": "merge_request"})
    assert response.status_code == 400
    assert not env.peer.calls and not env.gateway.writes
