"""Retained lane-control lessons on signed v1 sessions and normalized SQL receipts.

Original test names preserve the lesson inventory. HMAC tokens, migration windows,
client-authored mailbox ladders/journals, resume DTOs and generic provider-secret
redemption are retired. Tests bearing those names assert their closed replacement.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from forge_protocol import ControlAck, SessionRequest
import httpx
import jwt
from pydantic import ValidationError
import pytest
from sqlalchemy import delete

from forge.api.lane_auth_values import LaneAuthError
from forge.api.lane_settings import LaneAuthSettings
from forge.api.lane_v1 import router
from forge.store.models_commands import EngineCommand
from forge.store.models_lane import EngineLaneSession
from tests.lane_control_surface_fixture import control_case as control_case
from tests.native_lane_fixture import (
    PUBLIC,
    auth as auth,
    checkpoint,
    oidc,
    settings as settings,
    signing_key as signing_key,
    upload,
)


def session_claims(case):
    return jwt.decode(case.token, options={"verify_signature": False})


def signed(case, **changes):
    return jwt.encode(
        {**session_claims(case), **changes},
        case.lane.auth._secret,
        algorithm="HS256",
        headers={"typ": "forge-lane-session+jwt"},
    )


async def poll(case, token=None, after=0):
    headers = {"Authorization": "Bearer " + (case.token if token is None else token)}
    return await case.client.get("/lane/v1/controls", params={"after": after}, headers=headers)


async def raw_ack(case, command, **changes):
    body = {**case.ack(command).model_dump(mode="json"), **changes}
    return await case.client.post("/lane/v1/controls/" + command.command_id + "/ack", json=body)


async def saved_checkpoint(case, sequence=1):
    blob = await upload(case.client)
    value = checkpoint(case.pending, blob, sequence=sequence)
    result = await case.client.put(
        "/lane/v1/checkpoints/" + value.checkpoint_id, json=value.model_dump(mode="json")
    )
    assert result.status_code == 201
    return value


async def exchange_request(case, **changes):
    return await case.client.post(
        "/lane/v1/session",
        json=SessionRequest(
            **case.identity.model_dump(),
            provider=case.pending.dispatch.provider,
            oidc_token=oidc(case.key, case.pending, changes=changes),
        ).model_dump(mode="json"),
    )


async def disabled_request(method, path, **kwargs):
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        return await client.request(method, path, **kwargs)


class TestLaneToken:
    async def test_the_token_is_the_hmac_of_the_work_id_under_the_secret(self, control_case):
        case = control_case
        claims = session_claims(case)
        assert jwt.get_unverified_header(case.token)["typ"] == "forge-lane-session+jwt"
        assert claims["sub"] == f"{case.identity.forge_run_id}:2:7"
        old = hmac.new(b"synthetic-secret", case.identity.forge_run_id.encode(), hashlib.sha256)
        assert (await poll(case, old.hexdigest())).status_code == 401

    async def test_a_generation_scoped_token_is_not_the_legacy_token(self, control_case):
        case = control_case
        assert (await poll(case, signed(case, generation=8))).status_code == 401
        assert (await poll(case)).status_code == 200

    async def test_the_superseded_generation_scan_names_the_stale_generation(self, control_case):
        case = control_case
        await case.mutate_run(generation=8)
        response = await poll(case)
        assert response.status_code == 401 and "no longer current" in response.text
        assert case.token not in response.text


class TestGenerationScoping:
    async def test_the_current_generations_token_reads_and_acks(self, control_case):
        case = control_case
        command = await case.queue()
        assert (await poll(case)).json() == [command.model_dump(mode="json")]
        assert (await case.acknowledge(command)).status_code == 200

    async def test_a_superseded_generations_token_is_refused_with_the_reason(self, control_case):
        await control_case.mutate_run(attempt=3)
        assert (await poll(control_case)).status_code == 401

    async def test_a_superseded_generations_token_cannot_ack_either(self, control_case):
        case = control_case
        command = await case.queue()
        await case.mutate_run(generation=8)
        assert (await case.acknowledge(command)).status_code == 401
        assert (await case.rows())[0][1:3] == ("pending", None)

    async def test_the_legacy_token_keeps_validating_during_migration(
        self, control_case, monkeypatch
    ):
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", "2099-01-01T00:00:00Z")
        assert (await poll(control_case, "a" * 64)).status_code == 401

    async def test_a_generation_ahead_of_the_run_is_a_scoping_refusal(self, control_case):
        case = control_case
        token = signed(case, generation=8, sub=f"{case.identity.forge_run_id}:2:8")
        assert (await poll(case, token)).status_code == 401


class TestReplaySafeAcks:
    async def test_replaying_the_final_checkpointed_ack_is_idempotent(self, control_case):
        case = control_case
        command = await case.queue("pause")
        saved = await saved_checkpoint(case)
        first = await case.acknowledge(command, checkpoint_id=saved.checkpoint_id)
        before = await case.rows()
        replay = await case.acknowledge(command, checkpoint_id=saved.checkpoint_id)
        assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
        assert await case.rows() == before

    async def test_an_ack_declaring_the_current_generation_transitions(self, control_case):
        case = control_case
        command = await case.queue()
        assert (await raw_ack(case, command, generation=7)).status_code == 200
        assert (await case.rows())[0][1] == "acknowledged"

    async def test_an_ack_from_a_superseded_generation_is_refused_state_unchanged(
        self, control_case
    ):
        case = control_case
        command = await case.queue()
        before = await case.rows()
        assert (await raw_ack(case, command, generation=6)).status_code == 409
        assert await case.rows() == before

    async def test_a_generationless_ack_still_transitions_during_migration(self, control_case):
        case = control_case
        command = await case.queue()
        body = case.ack(command).model_dump(mode="json")
        body.pop("generation")
        response = await case.client.post(
            "/lane/v1/controls/" + command.command_id + "/ack", json=body
        )
        assert response.status_code == 422 and (await case.rows())[0][1] == "pending"

    async def test_a_negative_generation_is_a_shape_refusal(self, control_case):
        command = await control_case.queue()
        assert (await raw_ack(control_case, command, generation=-1)).status_code == 422


class TestFailClosed:
    async def test_get_without_the_secret_returns_503(self):
        assert (await disabled_request("GET", "/lane/v1/controls")).status_code == 503

    async def test_ack_without_the_secret_returns_503(self):
        assert (
            await disabled_request("POST", "/lane/v1/controls/x/ack", json={})
        ).status_code == 503

    async def test_get_without_a_database_returns_503(self, settings):
        with pytest.raises(ValidationError):
            LaneAuthSettings(**{**settings.model_dump(), "secret_key": ""})
        assert (await disabled_request("GET", "/lane/v1/brief")).status_code == 503


class TestAuth:
    async def test_missing_bearer_returns_401(self, control_case):
        case = control_case
        case.client.headers.pop("Authorization")
        assert (await case.client.get("/lane/v1/controls")).status_code == 401

    async def test_non_bearer_scheme_returns_401(self, control_case):
        response = await control_case.client.get(
            "/lane/v1/controls", headers={"Authorization": "Basic x"}
        )
        assert response.status_code == 401

    async def test_another_works_token_returns_403(self, control_case):
        case = control_case
        foreign = "b" * 32
        token = signed(case, forge_run_id=foreign, sub=f"{foreign}:2:7")
        assert (await poll(case, token)).status_code == 401

    async def test_garbage_token_returns_403(self, control_case):
        assert (await poll(control_case, "not-a-jwt")).status_code == 401

    async def test_an_ack_cannot_cross_works(self, control_case):
        case = control_case
        command = await case.queue()
        assert (await raw_ack(case, command, forge_run_id="b" * 32)).status_code == 409
        assert (await case.rows())[0][1] == "pending"


class TestPendingView:
    async def test_pending_is_received_and_authorized_only_in_sequence_order(self, control_case):
        case = control_case
        commands = [await case.queue() for _ in range(3)]
        assert (await case.acknowledge(commands[1], status="refused")).status_code == 200
        assert [r["sequence"] for r in (await poll(case)).json()] == [1, 3]
        assert len(await case.rows()) == 3

    async def test_after_sequence_is_an_honest_cursor(self, control_case):
        case = control_case
        for _ in range(3):
            await case.queue()
        assert [r["sequence"] for r in (await poll(case, after=1)).json()] == [2, 3]
        assert (await poll(case, after=3)).json() == []

    async def test_work_id_is_required(self, control_case):
        # Identity is exclusively the signed session; a query cannot select another run.
        case = control_case
        own = await case.queue()
        response = await case.client.get("/lane/v1/controls?work_id=" + "b" * 32)
        assert response.status_code == 200 and response.json() == [own.model_dump(mode="json")]

    async def test_negative_after_sequence_is_refused(self, control_case):
        assert (await poll(control_case, after=-1)).status_code == 422


class TestAckTransitions:
    async def test_authorized_books_the_control_planes_acceptance(self, control_case):
        case = control_case
        command = await case.queue()
        async with case.factory() as session:
            row = await session.get(EngineCommand, command.command_id)
            assert (
                row.state == "resolved"
                and json.loads(row.result_json)["reason"] == "control_queued"
            )
        assert (await case.rows())[0][1] == "pending"

    async def test_a_journal_row_is_optional(self, control_case):
        command = await control_case.queue()
        assert (await control_case.acknowledge(command)).status_code == 200
        assert json.loads((await control_case.rows())[0][2])["status"] == "applied"

    async def test_dispatching_requires_the_lanes_world(self, control_case):
        command = await control_case.queue()
        assert (await raw_ack(control_case, command, status="dispatching")).status_code == 422

    async def test_dispatching_records_the_intent(self, control_case):
        case = control_case
        command = await case.queue()
        payload = json.loads((await case.rows())[0][3])
        assert payload == command.model_dump(mode="json") and payload["generation"] == 7
        assert (await case.rows())[0][2] is None

    async def test_a_stale_world_dispatch_expires(self, control_case):
        case = control_case
        command = await case.queue()
        await case.mutate_run(cancel_requested=True, cancellation_generation=4)
        assert (await case.acknowledge(command)).status_code == 401
        assert (await case.rows())[0][1] == "pending"

    async def test_checkpointed_climbs_the_observation_rungs(self, control_case):
        case = control_case
        command = await case.queue("pause")
        assert (await raw_ack(case, command, checkpoint_id="0" * 64)).status_code == 409
        saved = await saved_checkpoint(case)
        assert (
            await case.acknowledge(command, checkpoint_id=saved.checkpoint_id)
        ).status_code == 200

    async def test_a_redelivered_checkpointed_ack_spends_nothing(self, control_case):
        case = control_case
        command = await case.queue()
        assert (await case.acknowledge(command)).status_code == 200
        first = await case.rows()
        for _ in range(3):
            assert (await case.acknowledge(command)).status_code == 200
        assert await case.rows() == first and (await poll(case)).json() == []

    async def test_a_skipped_rung_is_refused_with_409(self, control_case):
        command = await control_case.queue()
        assert (await raw_ack(control_case, command, sequence=2)).status_code == 409

    async def test_reauthorizing_is_refused_with_409(self, control_case):
        case = control_case
        command = await case.queue()
        assert (await case.acknowledge(command)).status_code == 200
        assert (await case.acknowledge(command, status="refused")).status_code == 409

    async def test_unknown_command_returns_404(self, control_case):
        body = ControlAck(
            **control_case.identity.model_dump(), command_id="unknown", sequence=1, status="applied"
        )
        response = await control_case.client.post(
            "/lane/v1/controls/unknown/ack", json=body.model_dump(mode="json")
        )
        assert response.status_code == 409

    async def test_unknown_state_returns_422(self, control_case):
        command = await control_case.queue()
        assert (await raw_ack(control_case, command, status="made-up")).status_code == 422

    async def test_a_malformed_body_is_refused(self, control_case):
        response = await control_case.client.post(
            "/lane/v1/controls/x/ack", json={"status": "applied"}
        )
        assert response.status_code == 422 and (await control_case.rows()) == []


class TestLegacyTokenDeadline:
    async def test_the_default_deadline_is_anchored_at_a_write_once_file(
        self, control_case, tmp_path, monkeypatch
    ):
        anchor = tmp_path / "removed-anchor"
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_ANCHOR_FILE", str(anchor))
        assert (await poll(control_case)).status_code == 200 and not anchor.exists()

    async def test_the_default_deadline_does_not_slide_between_resolutions(self, control_case):
        claims = session_claims(control_case)
        for _ in range(2):
            assert (await poll(control_case)).status_code == 200
        assert session_claims(control_case) == claims

    async def test_an_explicit_iso_deadline_is_honored(self, settings):
        with pytest.raises(ValidationError):
            LaneAuthSettings(
                **{
                    **settings.model_dump(),
                    "FORGE_LANE_LEGACY_TOKEN_DEADLINE": "2099-01-01T00:00:00Z",
                }
            )

    async def test_a_recorded_migration_start_fixes_the_deadline_thirty_days_out(
        self, control_case
    ):
        claims = session_claims(control_case)
        assert 0 < claims["exp"] - claims["iat"] == control_case.lane.auth.ttl <= 900

    async def test_a_start_recorded_31_days_ago_closes_the_window_for_this_process(
        self, control_case, monkeypatch
    ):
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_START", "2020-01-01T00:00:00Z")
        assert (await poll(control_case, "0" * 64)).status_code == 401

    async def test_the_boundary_itself_is_closed(self, control_case):
        case = control_case
        stamp = int(datetime.now(timezone.utc).timestamp())
        token = signed(case, iat=stamp - 60, nbf=stamp - 60, exp=stamp)
        assert (await poll(case, token)).status_code == 401

    async def test_the_window_is_closed_after_31_days_even_across_a_restart(self, control_case):
        case = control_case
        await case.lane.auth.authenticate_bearer("Bearer " + case.token)
        assert (await poll(case, signed(case, exp=1, iat=0, nbf=0))).status_code == 401

    async def test_a_start_recorded_inside_the_window_still_accepts_the_legacy_token(
        self, control_case, monkeypatch
    ):
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_START", datetime.now(timezone.utc).isoformat())
        assert (await poll(control_case, "1" * 64)).status_code == 401

    async def test_a_legacy_token_past_the_deadline_is_refused(self, control_case):
        case = control_case
        token = signed(case, exp=session_claims(case)["iat"] + 901)
        assert (await poll(case, token)).status_code == 401

    async def test_a_generation_token_survives_past_the_deadline(self, control_case, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", "1970-01-01T00:00:00Z")
        assert (await poll(control_case)).status_code == 200

    async def test_a_generation_authority_outage_refuses_rather_than_degrading(
        self, control_case, monkeypatch
    ):
        async def unavailable(identity):
            raise LaneAuthError("current authority unavailable")

        monkeypatch.setattr(control_case.lane.auth.store, "load", unavailable)
        assert (await poll(control_case)).status_code == 401


class TestResumeSpecRead:
    async def test_the_latest_resume_command_row_is_served_whatever_its_rung(self, control_case):
        # Current brief carries the exact frozen specification; no command-row resume DTO.
        response = await control_case.client.get("/lane/v1/brief")
        assert (
            response.status_code == 200
            and response.json()["spec_json"] == control_case.pending.brief.spec_json
        )

    async def test_the_latest_of_several_resumes_wins(self, control_case):
        case = control_case
        first = await saved_checkpoint(case)
        second = await saved_checkpoint(case, sequence=2)
        assert first.checkpoint_id != second.checkpoint_id
        latest = await case.client.get("/lane/v1/checkpoints/latest")
        assert latest.status_code == 200 and latest.json()["sequence"] == 2

    async def test_no_resume_ever_answers_command_none(self, control_case):
        assert (await control_case.client.get("/lane/v1/checkpoints/latest")).status_code == 404
        assert (await control_case.client.get("/lane/resume-spec")).status_code == 404

    async def test_the_read_cannot_cross_works(self, control_case):
        case = control_case
        await saved_checkpoint(case)
        foreign = "b" * 32
        token = signed(case, forge_run_id=foreign, sub=f"{foreign}:2:7")
        response = await case.client.get(
            "/lane/v1/checkpoints/latest", headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 401


class TestCredentialRedemption:
    async def test_the_current_attempt_redeems_the_bound_credential(self, control_case):
        case = control_case
        calls = []

        async def provider(pending):
            calls.append(pending.identity)
            return case.grant()

        case.lane.model_key_provider = provider
        first = await case.client.post("/lane/v1/model-key")
        replay = await case.client.post("/lane/v1/model-key")
        assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
        assert calls == [case.identity]
        assert first.json()["key"] not in json.dumps(await case.state())

    async def test_a_superseded_generations_token_cannot_redeem(self, control_case):
        await control_case.mutate_run(generation=8)
        assert (await control_case.client.post("/lane/v1/model-key")).status_code == 401
        assert "model_key_requested" not in await control_case.state()

    async def test_another_works_token_cannot_redeem(self, control_case):
        case = control_case
        foreign = "b" * 32
        token = signed(case, forge_run_id=foreign, sub=f"{foreign}:2:7")
        response = await case.client.post(
            "/lane/v1/model-key", headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 401 and "model_key_requested" not in await case.state()

    async def test_a_revoked_binding_redeems_nothing(self, control_case):
        await control_case.mutate_run(cancel_requested=True)
        assert (await control_case.client.post("/lane/v1/model-key")).status_code == 401

    async def test_a_changed_reference_redeems_nothing(self, control_case):
        assert (await exchange_request(control_case, ref="refs/heads/foreign")).status_code == 401

    async def test_an_unbound_subject_redeems_nothing(self, control_case):
        case = control_case
        async with case.factory.begin() as session:
            await session.execute(delete(EngineLaneSession))
        assert (await case.client.post("/lane/v1/model-key")).status_code == 401

    async def test_a_broker_refusal_is_a_403_never_a_value(self, control_case):
        case = control_case
        calls = []

        async def provider(pending):
            calls.append(pending.identity)
            raise ValueError("synthetic provider-secret sentinel")

        case.lane.model_key_provider = provider
        response = await case.client.post("/lane/v1/model-key")
        assert response.status_code == 503 and "sentinel" not in response.text
        assert (await case.client.post("/lane/v1/model-key")).status_code == 409
        assert calls == [case.identity]

    async def test_a_broker_staging_the_wrong_slot_is_refused(self, control_case):
        case = control_case

        async def provider(pending):
            return case.grant(model="foreign-model")

        case.lane.model_key_provider = provider
        assert (await case.client.post("/lane/v1/model-key")).status_code == 503
        assert (await case.client.post("/lane/v1/model-key")).status_code == 409

    async def test_a_missing_bearer_is_401_and_no_secret_is_503(self, control_case):
        control_case.client.headers.pop("Authorization")
        assert (await control_case.client.post("/lane/v1/model-key")).status_code == 401
        assert (await disabled_request("POST", "/lane/v1/model-key")).status_code == 503

    async def test_an_unknown_work_is_404_and_an_unsubjected_work_403(self, control_case):
        case = control_case
        token = signed(case, forge_run_id="f" * 32, sub=f"{'f' * 32}:2:7")
        response = await case.client.post(
            "/lane/v1/model-key", headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 401
        assert (await case.client.post("/lane/credentials/redeem", json={})).status_code == 404

    async def test_a_malformed_ttl_refuses_rather_than_re_defaulting(self, control_case):
        case = control_case

        async def provider(pending):
            return case.grant(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))

        case.lane.model_key_provider = provider
        assert (await case.client.post("/lane/v1/model-key")).status_code == 503
        assert (await case.state())["model_key_requested"] is True

    async def test_an_audit_persistence_failure_prevents_the_credential_response(
        self, control_case
    ):
        case = control_case

        async def provider(pending):
            # Cancellation between native mint and delivery must withhold the credential.
            await case.mutate_run(cancel_requested=True, cancellation_generation=4)
            return case.grant()

        case.lane.model_key_provider = provider
        response = await case.client.post("/lane/v1/model-key")
        assert response.status_code == 401 and "synthetic-scoped-model-key" not in response.text
        assert (await case.state())["model_key_requested"] is True
