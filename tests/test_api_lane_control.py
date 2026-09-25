"""The lane control API (NXT-10 outbound leg) — auth, scoping, ladder.

The surface a CI lane dials OUT to (EXE-04): pending control commands off
the durable ``control_commands`` rows, plus the guarded ack transitions
the lane's drain climbs. Pinned here:

- fail-closed: no ``FORGE_LANE_CONTROL_SECRET`` → BOTH routes 503, never
  unauthenticated-open; no session factory → 503 too;
- the token scheme: ``HMAC-SHA256(secret, work_id)`` — a lane holding
  run A's token can neither poll run B's queue nor ack B's commands
  (403, work-scoping by construction);
- R28-07 attempt-scoped generations: the token gains a generation
  component minted by the dispatch; the server validates against the
  run's CURRENT durable generation (FlowRun.cancellation_generation), a
  SUPERSEDED generation's token answers 403 with an actionable message,
  and the legacy work-id-only HMAC validates only inside the explicit
  NEXT-01 migration window (FORGE_LANE_LEGACY_TOKEN_DEADLINE, or the
  recorded FORGE_LANE_LEGACY_TOKEN_START + 30 days — and since Q35-06
  the DEFAULT window is anchored at a WRITE-ONCE persisted anchor file,
  never at a process's import, so a restart cannot re-open a fresh
  window) — an unavailable generation authority is a
  503 refusal, never a silent legacy acceptance;
- pending means received/authorized ONLY, in durable sequence order, and
  ``after_sequence`` is an honest cursor;
- every ack is the PostgresMailbox's own guarded CAS: rung skips are 409,
  a stale-world dispatch EXPIRES, and ``checkpointed`` climbs the
  observation rungs; R28-10 makes the replay IDEMPOTENT (200, no state
  change) and refuses an ack that declares a superseded generation;
- the lane's journal row is APPENDED to the row's audit journal —
  evidence beside the transition entries, never a status claim.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.adaptive.credential_broker import StagedBroker
from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox
from forge.adaptive.models import ControlCommand
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import ProjectCredentialRegistry
from forge.api_lane_control import (
    LANE_CREDENTIAL_REDEEM_ROUTE,
    LANE_LEGACY_TOKEN_DEADLINE_ENV,
    LANE_LEGACY_TOKEN_START_ENV,
    LEGACY_CREDENTIAL_ANCHOR_FILE_ENV,
    REDEMPTION_TTL_ENV,
    LegacyWindowInvalid,
    _PROCESS_MIGRATION_START,
    _legacy_window_open,
    _superseded_generation,
    lane_control_token,
    legacy_token_deadline,
    legacy_token_start,
    redemption_ttl_seconds,
    resolve_legacy_window,
    verify_lane_token,
)
from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import FlowRun
from forge.main import create_app

SECRET = "lane-secret"  # noqa: S105 — fake value for tests
WORK = "run-lane-1"
OTHER_WORK = "run-lane-2"


def lane_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/lane-control.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        FORGE_LANE_CONTROL_SECRET=SecretStr(SECRET),
    )
    values.update(overrides)
    return Settings(**values)


def _cmd(seq: int, *, work_id: str = WORK, kind: str = "steer", **overrides) -> ControlCommand:
    base = {
        "schema": "forge.proposal.control-command/1",
        "command_id": f"cmd-{work_id}-{seq}",
        "work_id": work_id,
        "sequence": seq,
        "kind": kind,
        "actor_ref": "human:op",
        "actor_origin": "server_authenticated_human",
        "idempotency_key": f"key-{work_id}-{seq}",
        "status": "received",
        "payload": {"run_id": work_id, "text": f"steer number {seq}"},
    }
    base.update(overrides)
    return ControlCommand.model_validate(base)


def auth(work_id: str = WORK) -> dict[str, str]:
    return {"Authorization": f"Bearer {lane_control_token(SECRET, work_id)}"}


@pytest.fixture(autouse=True)
def _isolated_legacy_anchor(tmp_path, monkeypatch):
    """Q35-06: every default-window resolution in this module persists its
    write-once anchor under the test's own tmp dir — never the checkout."""
    monkeypatch.setenv(LEGACY_CREDENTIAL_ANCHOR_FILE_ENV, str(tmp_path / "legacy-anchor"))


@pytest.fixture()
async def app(tmp_path):
    reset_engine()
    application = create_app(settings=lane_settings(tmp_path))
    async with application.router.lifespan_context(application):
        yield application
    reset_engine()


@pytest.fixture()
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture()
def mailbox(app) -> PostgresMailbox:
    return PostgresMailbox(app.state.session_factory)


async def _authorize(mailbox: PostgresMailbox, command: ControlCommand) -> None:
    await mailbox.authorize(command.command_id, {"server_authenticated_human": ("human:op",)})


# -- the token scheme ---------------------------------------------------------


class TestLaneToken:
    def test_the_token_is_the_hmac_of_the_work_id_under_the_secret(self):
        token = lane_control_token(SECRET, WORK)

        assert len(token) == 64  # sha256 hex
        assert verify_lane_token(SECRET, token, WORK) is True
        # work-scoped by construction: another work's expected bytes differ
        assert verify_lane_token(SECRET, token, OTHER_WORK) is False
        assert verify_lane_token("other-secret", token, WORK) is False
        assert verify_lane_token(SECRET, "", WORK) is False

    def test_a_generation_scoped_token_is_not_the_legacy_token(self):
        """R28-07: the attempt-scoped derivation binds the generation into
        the HMAC material — the two spellings never collide, and each
        verifies only against its own expectation."""
        legacy = lane_control_token(SECRET, WORK)
        scoped = lane_control_token(SECRET, WORK, generation=3)
        other_generation = lane_control_token(SECRET, WORK, generation=4)

        assert scoped != legacy
        assert scoped != other_generation
        assert verify_lane_token(SECRET, scoped, WORK, generation=3) is True
        # A scoped token is neither the legacy expectation nor another
        # generation's — moving the run's generation retires it at once.
        assert verify_lane_token(SECRET, scoped, WORK) is False
        assert verify_lane_token(SECRET, scoped, WORK, generation=4) is False
        # The legacy default is byte-identical to the pre-R28-07 scheme.
        assert lane_control_token(SECRET, WORK, generation=None) == legacy

    def test_the_superseded_generation_scan_names_the_stale_generation(self):
        stale = lane_control_token(SECRET, WORK, generation=1)

        assert _superseded_generation(SECRET, stale, WORK, 2) == 1
        # The CURRENT generation's token is not stale...
        current = lane_control_token(SECRET, WORK, generation=2)
        assert _superseded_generation(SECRET, current, WORK, 2) is None
        # ...neither is a foreign work's, a legacy token, or garbage —
        # those are work-scoping failures, not generational ones.
        assert _superseded_generation(SECRET, lane_control_token(SECRET, WORK), WORK, 2) is None
        assert (
            _superseded_generation(SECRET, lane_control_token(SECRET, OTHER_WORK), WORK, 2) is None
        )
        assert _superseded_generation(SECRET, "nope", WORK, 2) is None
        # Without a durable generation nothing can be judged stale.
        assert _superseded_generation(SECRET, stale, WORK, None) is None


async def _put_run(app, work_id: str, *, generation: int) -> None:
    """The durable run row with its CURRENT generation (R28-07's authority)."""
    async with app.state.session_factory() as session:
        session.add(
            FlowRun(id=work_id, project_id=1, provider="github", cancellation_generation=generation)
        )
        await session.commit()


def _gen_headers(work_id: str, generation: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {lane_control_token(SECRET, work_id, generation=generation)}"}


# -- attempt-scoped generations (R28-07) ----------------------------------------


class TestGenerationScoping:
    async def test_the_current_generations_token_reads_and_acks(self, app, client, mailbox):
        await _put_run(app, WORK, generation=2)
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)

        pending = await client.get(
            "/lane/controls", params={"work_id": WORK}, headers=_gen_headers(WORK, 2)
        )
        assert pending.status_code == 200

        ack = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "dispatching", "plan_revision": 1, "execution_epoch": 1},
            headers=_gen_headers(WORK, 2),
        )
        assert ack.status_code == 200
        assert ack.json()["command"]["status"] == "dispatching"

    async def test_a_superseded_generations_token_is_refused_with_the_reason(
        self, app, client, mailbox
    ):
        """The reviewer's retired-lane race: the OLD attempt's lane process
        is still alive, dialing in with the token its dispatch minted."""
        await _put_run(app, WORK, generation=2)
        await mailbox.submit(_cmd(1))

        response = await client.get(
            "/lane/controls", params={"work_id": WORK}, headers=_gen_headers(WORK, 1)
        )

        assert response.status_code == 403
        detail = response.json()["detail"]
        assert "superseded runner generation" in detail
        assert "(1;" in detail and "generation 2" in detail

    async def test_a_superseded_generations_token_cannot_ack_either(self, app, client, mailbox):
        await _put_run(app, WORK, generation=2)
        command = _cmd(1)
        await mailbox.submit(command)

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "authorized"},
            headers=_gen_headers(WORK, 1),
        )

        assert response.status_code == 403
        assert "superseded runner generation" in response.json()["detail"]
        stored = await mailbox.get(command.command_id)
        assert stored is not None and stored.status == "received"  # nothing moved

    async def test_the_legacy_token_keeps_validating_during_migration(self, app, client, mailbox):
        """A token that never carried a generation is not stale — the
        honest default while dispatches still mint work-scoped tokens."""
        await _put_run(app, WORK, generation=2)
        await mailbox.submit(_cmd(1))

        pending = await client.get("/lane/controls", params={"work_id": WORK}, headers=auth())
        assert pending.status_code == 200

        ack = await client.post(
            f"/lane/controls/cmd-{WORK}-1/ack",
            json={"state": "authorized"},
            headers=auth(),
        )
        assert ack.status_code == 200

    async def test_a_generation_ahead_of_the_run_is_a_scoping_refusal(self, app, client, mailbox):
        """A token for a generation the run never reached matches nothing —
        plain 403, never mislabeled as superseded."""
        await _put_run(app, WORK, generation=2)
        await mailbox.submit(_cmd(1))

        response = await client.get(
            "/lane/controls", params={"work_id": WORK}, headers=_gen_headers(WORK, 99)
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "lane token does not scope this work"


# -- replay-safe acknowledgements (R28-10) ---------------------------------------


class TestReplaySafeAcks:
    async def _climb_the_full_ladder(self, client: AsyncClient, mailbox, command) -> None:
        """authorized → dispatching → vendor_accepted → applied → checkpointed,
        every rung through the API's own ack surface."""
        rungs = (
            ("authorized", {}),
            ("dispatching", {"plan_revision": 1, "execution_epoch": 1}),
            ("vendor_accepted", {}),
            ("applied", {}),
            ("checkpointed", {}),
        )
        for state, extra in rungs:
            response = await client.post(
                f"/lane/controls/{command.command_id}/ack",
                json={"state": state, **extra},
                headers=auth(),
            )
            assert response.status_code == 200, (state, response.text)
            assert response.json()["command"]["status"] == state

    async def test_replaying_the_final_checkpointed_ack_is_idempotent(self, app, client, mailbox):
        """R28-10's composed trace: full ack ladder → the response is lost →
        the lane replays the final ack. 200, the same state, NO new ladder
        journal entries — a lost response never re-drives the vendor effect."""
        command = _cmd(1)
        await mailbox.submit(command)
        await self._climb_the_full_ladder(client, mailbox, command)
        before = await _row(app, command.command_id)
        rungs_before = [entry.get("to") for entry in before.journal]

        replay = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "checkpointed"},
            headers=auth(),
        )

        assert replay.status_code == 200  # idempotent success, not 409
        assert replay.json()["command"]["status"] == "checkpointed"
        stored = await mailbox.get(command.command_id)
        assert stored is not None and stored.status == "checkpointed"
        after = await _row(app, command.command_id)
        rungs_after = [entry.get("to") for entry in after.journal]
        # No state change: the ladder's own journal is unchanged (the
        # replayed evidence append may ride, transitions may not).
        assert rungs_after[: len(rungs_before)] == rungs_before
        assert rungs_after.count("checkpointed") == 1

    async def test_an_ack_declaring_the_current_generation_transitions(self, app, client, mailbox):
        await _put_run(app, WORK, generation=2)
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={
                "state": "dispatching",
                "plan_revision": 1,
                "execution_epoch": 1,
                "generation": 2,
            },
            headers=auth(),
        )

        assert response.status_code == 200
        assert response.json()["command"]["status"] == "dispatching"

    async def test_an_ack_from_a_superseded_generation_is_refused_state_unchanged(
        self, app, client, mailbox
    ):
        """The retired lane can still READ (its queue view answers 200) but
        its ack cannot move the current attempt's state."""
        await _put_run(app, WORK, generation=2)
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)

        read = await client.get("/lane/controls", params={"work_id": WORK}, headers=auth())
        assert read.status_code == 200  # the legacy holder can still read

        refused = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={
                "state": "dispatching",
                "plan_revision": 1,
                "execution_epoch": 1,
                "generation": 1,  # superseded by the run's current 2
            },
            headers=auth(),
        )

        assert refused.status_code == 403
        detail = refused.json()["detail"]
        assert "superseded runner generation" in detail and "generation 2" in detail
        stored = await mailbox.get(command.command_id)
        assert stored is not None and stored.status == "authorized"  # nothing moved

    async def test_a_generationless_ack_still_transitions_during_migration(
        self, app, client, mailbox
    ):
        """Pre-generation lanes omit the field; a known current generation
        does not retroactively brick them."""
        await _put_run(app, WORK, generation=2)
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "dispatching", "plan_revision": 1, "execution_epoch": 1},
            headers=auth(),
        )

        assert response.status_code == 200
        assert response.json()["command"]["status"] == "dispatching"

    async def test_a_negative_generation_is_a_shape_refusal(self, client, mailbox):
        command = _cmd(1)
        await mailbox.submit(command)

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "authorized", "generation": -1},
            headers=auth(),
        )

        assert response.status_code == 422


# -- fail closed ---------------------------------------------------------------


class TestFailClosed:
    @pytest.fixture()
    async def disabled_client(self, tmp_path):
        reset_engine()
        application = create_app(settings=lane_settings(tmp_path, FORGE_LANE_CONTROL_SECRET=None))
        async with application.router.lifespan_context(application):
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
        reset_engine()

    async def test_get_without_the_secret_returns_503(self, disabled_client: AsyncClient):
        response = await disabled_client.get("/lane/controls", params={"work_id": WORK})
        assert response.status_code == 503
        assert response.json() == {"detail": "lane control endpoint disabled"}

    async def test_ack_without_the_secret_returns_503(self, disabled_client: AsyncClient):
        response = await disabled_client.post(
            "/lane/controls/cmd-x/ack", json={"state": "authorized"}, headers=auth()
        )
        assert response.status_code == 503

    async def test_get_without_a_database_returns_503(self, app, client: AsyncClient):
        app.state.session_factory = None
        response = await client.get("/lane/controls", params={"work_id": WORK}, headers=auth())
        assert response.status_code == 503


# -- auth and work-scoping -------------------------------------------------------


class TestAuth:
    async def test_missing_bearer_returns_401(self, client: AsyncClient, mailbox):
        await mailbox.submit(_cmd(1))
        response = await client.get("/lane/controls", params={"work_id": WORK})
        assert response.status_code == 401

    async def test_non_bearer_scheme_returns_401(self, client: AsyncClient, mailbox):
        await mailbox.submit(_cmd(1))
        response = await client.get(
            "/lane/controls",
            params={"work_id": WORK},
            headers={"Authorization": f"Basic {lane_control_token(SECRET, WORK)}"},
        )
        assert response.status_code == 401

    async def test_another_works_token_returns_403(self, client: AsyncClient, mailbox):
        await mailbox.submit(_cmd(1))
        response = await client.get(
            "/lane/controls", params={"work_id": WORK}, headers=auth(OTHER_WORK)
        )
        assert response.status_code == 403

    async def test_garbage_token_returns_403(self, client: AsyncClient, mailbox):
        await mailbox.submit(_cmd(1))
        response = await client.get(
            "/lane/controls", params={"work_id": WORK}, headers={"Authorization": "Bearer nope"}
        )
        assert response.status_code == 403

    async def test_an_ack_cannot_cross_works(self, client: AsyncClient, mailbox):
        await mailbox.submit(_cmd(1, work_id=OTHER_WORK))
        response = await client.post(
            f"/lane/controls/cmd-{OTHER_WORK}-1/ack",
            json={"state": "authorized"},
            headers=auth(WORK),
        )
        assert response.status_code == 403


# -- the pending view --------------------------------------------------------------


class TestPendingView:
    async def test_pending_is_received_and_authorized_only_in_sequence_order(
        self, client: AsyncClient, mailbox
    ):
        gone, late, fresh = _cmd(1), _cmd(2), _cmd(3)
        for command in (gone, late, fresh):
            await mailbox.submit(command)
        await _authorize(mailbox, gone)
        await mailbox.dispatch(
            gone.command_id, current_plan_revision=1, current_execution_epoch=1
        )  # beyond pending — its fate is read from the row, never re-delivered
        await _authorize(mailbox, late)  # authorized, still pending
        await mailbox.submit(_cmd(1, work_id=OTHER_WORK))  # another work, never ours

        response = await client.get("/lane/controls", params={"work_id": WORK}, headers=auth())

        assert response.status_code == 200
        body = response.json()
        assert [c["command_id"] for c in body["commands"]] == [
            f"cmd-{WORK}-2",
            f"cmd-{WORK}-3",
        ]
        assert body["work_id"] == WORK
        assert body["after_sequence"] == 0

    async def test_after_sequence_is_an_honest_cursor(self, client: AsyncClient, mailbox):
        await mailbox.submit(_cmd(1))
        await mailbox.submit(_cmd(2))
        await mailbox.submit(_cmd(3))

        response = await client.get(
            "/lane/controls",
            params={"work_id": WORK, "after_sequence": 2},
            headers=auth(),
        )

        assert [c["sequence"] for c in response.json()["commands"]] == [3]

    async def test_work_id_is_required(self, client: AsyncClient):
        response = await client.get("/lane/controls", headers=auth())
        assert response.status_code == 422

    async def test_negative_after_sequence_is_refused(self, client: AsyncClient):
        response = await client.get(
            "/lane/controls", params={"work_id": WORK, "after_sequence": -1}, headers=auth()
        )
        assert response.status_code == 422


# -- the ack surface ------------------------------------------------------------------


class TestAckTransitions:
    async def test_authorized_books_the_control_planes_acceptance(
        self, app, client: AsyncClient, mailbox
    ):
        command = _cmd(1)
        await mailbox.submit(command)

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "authorized", "journal_row": {"source": "lane_channel"}},
            headers=auth(),
        )

        assert response.status_code == 200
        assert response.json()["command"]["status"] == "authorized"
        assert response.json()["journal_appended"] is True
        stored = await mailbox.get(command.command_id)
        assert stored is not None and stored.status == "authorized"
        # the lane's evidence row rides AFTER the transition's own entry
        row = await _row(app, command.command_id)
        assert row.journal[-1]["lane_ack"] == "authorized"
        assert row.journal[-1]["row"] == {"source": "lane_channel"}

    async def test_a_journal_row_is_optional(self, client: AsyncClient, mailbox):
        command = _cmd(1)
        await mailbox.submit(command)
        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "authorized"},
            headers=auth(),
        )
        assert response.json()["journal_appended"] is False

    async def test_dispatching_requires_the_lanes_world(self, client: AsyncClient, mailbox):
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "dispatching"},
            headers=auth(),
        )
        assert response.status_code == 422

    async def test_dispatching_records_the_intent(self, app, client: AsyncClient, mailbox):
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={
                "state": "dispatching",
                "plan_revision": 4,
                "execution_epoch": 2,
                "vendor_correlation_id": "sess-1",
            },
            headers=auth(),
        )

        assert response.status_code == 200
        assert response.json()["command"]["status"] == "dispatching"
        row = await _row(app, command.command_id)
        assert row.epoch == 2

    async def test_a_stale_world_dispatch_expires(self, client: AsyncClient, mailbox):
        command = _cmd(1, expected_execution_epoch=7)
        await mailbox.submit(command)
        await _authorize(mailbox, command)

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "dispatching", "plan_revision": 1, "execution_epoch": 1},
            headers=auth(),
        )

        assert response.status_code == 200
        assert response.json()["command"]["status"] == "expired"

    async def test_checkpointed_climbs_the_observation_rungs(
        self, app, client: AsyncClient, mailbox
    ):
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)
        await mailbox.dispatch(
            command.command_id, current_plan_revision=1, current_execution_epoch=1
        )

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "checkpointed"},
            headers=auth(),
        )

        assert response.status_code == 200
        assert response.json()["command"]["status"] == "checkpointed"
        row = await _row(app, command.command_id)
        rungs = [entry.get("to") for entry in row.journal]
        assert rungs[-3:] == ["vendor_accepted", "applied", "checkpointed"]

    async def test_a_redelivered_checkpointed_ack_spends_nothing(
        self, client: AsyncClient, mailbox
    ):
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)
        await mailbox.dispatch(
            command.command_id, current_plan_revision=1, current_execution_epoch=1
        )
        first = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "checkpointed"},
            headers=auth(),
        )
        assert first.status_code == 200

        again = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "checkpointed"},
            headers=auth(),
        )

        assert again.status_code == 200
        assert again.json()["command"]["status"] == "checkpointed"

    async def test_a_skipped_rung_is_refused_with_409(self, client: AsyncClient, mailbox):
        command = _cmd(1)
        await mailbox.submit(command)  # still received

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "checkpointed"},
            headers=auth(),
        )

        assert response.status_code == 409
        stored = await mailbox.get(command.command_id)
        assert stored is not None and stored.status == "received"

    async def test_reauthorizing_is_refused_with_409(self, client: AsyncClient, mailbox):
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)

        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "authorized"},
            headers=auth(),
        )
        assert response.status_code == 409

    async def test_unknown_command_returns_404(self, client: AsyncClient):
        response = await client.post(
            "/lane/controls/cmd-nope/ack", json={"state": "authorized"}, headers=auth()
        )
        assert response.status_code == 404

    async def test_unknown_state_returns_422(self, client: AsyncClient, mailbox):
        command = _cmd(1)
        await mailbox.submit(command)
        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "received"},
            headers=auth(),
        )
        assert response.status_code == 422

    async def test_a_malformed_body_is_refused(self, client: AsyncClient, mailbox):
        command = _cmd(1)
        await mailbox.submit(command)
        response = await client.post(
            f"/lane/controls/{command.command_id}/ack",
            json={"state": "authorized", "surprise": True},
            headers=auth(),
        )
        assert response.status_code == 422


async def _row(app, command_id: str) -> ControlCommandRow:
    """The raw durable row (journal/epoch audit reads)."""
    from sqlalchemy import select

    async with app.state.session_factory() as session:
        row = await session.scalar(
            select(ControlCommandRow).where(ControlCommandRow.id == command_id)
        )
    assert row is not None
    return row


# -- the legacy migration window (NEXT-01, R32-03) ------------------------------


class TestLegacyTokenDeadline:
    """R32-03/Q35-06: the deadline is a FIXED instant, never "now + 30 days"
    recomputed per check (the reviewer's proof: a sliding deadline is
    accepted on day 3650). Anchors, in order: the explicit deadline env,
    the RECORDED migration start env + 30 days (restart-stable), and —
    since Q35-06 — the WRITE-ONCE persisted anchor file + 30 days; with
    no anchor persistable the window is REFUSED, never re-anchored at a
    fresh process's import."""

    def test_the_default_deadline_is_anchored_at_a_write_once_file(self, tmp_path):
        anchor = tmp_path / "anchor"
        env = {LEGACY_CREDENTIAL_ANCHOR_FILE_ENV: str(anchor)}

        window = resolve_legacy_window(env)

        assert anchor.is_file()  # created on the first resolution...
        assert window.source == "persisted-file"
        assert window.anchor == _PROCESS_MIGRATION_START  # ...with the seed
        assert window.deadline == _PROCESS_MIGRATION_START + timedelta(days=30)
        assert legacy_token_deadline(env) == window.deadline

    def test_the_default_deadline_does_not_slide_between_resolutions(self, tmp_path):
        """The reviewer's P01 core: two resolutions after one another must
        see the SAME deadline — the persisted file anchors it, not the
        clock and not whichever process happens to resolve."""
        env = {LEGACY_CREDENTIAL_ANCHOR_FILE_ENV: str(tmp_path / "anchor")}
        first = legacy_token_deadline(env)
        second = legacy_token_deadline(env)

        assert first == second
        # ...and it is reachable: a controlled clock AT the anchored
        # deadline closes the window (equality is past the window).
        assert _legacy_window_open(env) is (datetime.now(timezone.utc) < first)

    def test_an_explicit_iso_deadline_is_honored(self):
        fixed = "2020-01-01T00:00:00+00:00"
        assert legacy_token_deadline(
            {LANE_LEGACY_TOKEN_DEADLINE_ENV: fixed}
        ) == datetime.fromisoformat(fixed)
        naive = legacy_token_deadline({LANE_LEGACY_TOKEN_DEADLINE_ENV: "2020-01-01"})
        assert naive.tzinfo is not None  # naive reads as UTC
        # Q35-06: malformed (or set-but-empty) explicit configuration is a
        # TYPED failure — never a silent re-anchor onto a default window.
        with pytest.raises(LegacyWindowInvalid, match=LANE_LEGACY_TOKEN_DEADLINE_ENV):
            legacy_token_deadline({LANE_LEGACY_TOKEN_DEADLINE_ENV: "soon"})
        with pytest.raises(LegacyWindowInvalid, match="empty"):
            legacy_token_deadline({LANE_LEGACY_TOKEN_DEADLINE_ENV: "   "})

    def test_a_recorded_migration_start_fixes_the_deadline_thirty_days_out(self):
        """The persisted shape: FORGE_LANE_LEGACY_TOKEN_START records when
        the window OPENED; the deadline is start + 30 days regardless of
        when the check runs (a pure function of the recorded value)."""
        start = datetime(2030, 5, 1, 12, 0, tzinfo=timezone.utc)
        env = {LANE_LEGACY_TOKEN_START_ENV: start.isoformat()}

        assert legacy_token_deadline(env) == start + timedelta(days=30)
        assert legacy_token_start(env) == start
        # A naive start reads as UTC, same as the deadline spelling.
        naive = legacy_token_start({LANE_LEGACY_TOKEN_START_ENV: "2030-05-01"})
        assert naive == datetime(2030, 5, 1, tzinfo=timezone.utc)
        # Q35-06: a malformed recorded start is a typed failure too.
        with pytest.raises(LegacyWindowInvalid, match=LANE_LEGACY_TOKEN_START_ENV):
            legacy_token_start({LANE_LEGACY_TOKEN_START_ENV: "soon"})
        # The explicit deadline still wins over a recorded start.
        both = {
            LANE_LEGACY_TOKEN_START_ENV: "2030-05-01",
            LANE_LEGACY_TOKEN_DEADLINE_ENV: "2029-01-01",
        }
        assert legacy_token_deadline(both) == datetime(2029, 1, 1, tzinfo=timezone.utc)

    def test_a_start_recorded_31_days_ago_closes_the_window_for_this_process(self):
        """With a controlled start (the clock advanced past it): start + 31
        days > deadline → the window is closed, NOW, in this process."""
        start = datetime.now(timezone.utc) - timedelta(days=31)
        env = {LANE_LEGACY_TOKEN_START_ENV: start.isoformat()}

        assert legacy_token_deadline(env) < datetime.now(timezone.utc)
        assert _legacy_window_open(env) is False

    def test_the_boundary_itself_is_closed(self):
        """At exactly the deadline the legacy window is past it (the
        comparison is strict): the boundary refuses, not the microsecond
        after."""
        boundary = datetime.now(timezone.utc) - timedelta(days=30)
        start = {LANE_LEGACY_TOKEN_START_ENV: (boundary - timedelta(days=30)).isoformat()}

        class _Clock(datetime):  # a controlled clock AT start + 30 days
            @classmethod
            def now(cls, tz=None):
                return boundary

        import forge.api_lane_control as api

        real_datetime = api.datetime
        api.datetime = _Clock
        try:
            assert legacy_token_deadline(start) == boundary
            assert _legacy_window_open(start) is False
        finally:
            api.datetime = real_datetime

    async def test_the_window_is_closed_after_31_days_even_across_a_restart(
        self, app, client, mailbox, monkeypatch
    ):
        """R32-03's acceptance: a start RECORDED 31 days ago refuses the
        legacy token through the real surface, and a fresh process derives
        the SAME (past) deadline — the decision survives restarts because
        it lives in the recorded env value, never in process memory."""
        import subprocess
        import sys

        await _put_run(app, WORK, generation=2)
        await mailbox.submit(_cmd(1))
        start = datetime.now(timezone.utc) - timedelta(days=31)
        recorded = start.isoformat()
        monkeypatch.setenv(LANE_LEGACY_TOKEN_START_ENV, recorded)

        pending = await client.get("/lane/controls", params={"work_id": WORK}, headers=auth())
        assert pending.status_code == 403
        assert "migration deadline" in pending.json()["detail"]

        # The "restarted" process: the deadline is re-derived from the
        # recorded start alone (no process state exists to extend it).
        fresh = subprocess.run(
            [
                sys.executable,
                "-c",
                "from forge.api_lane_control import legacy_token_deadline;"
                "print(legacy_token_deadline().isoformat())",
            ],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, LANE_LEGACY_TOKEN_START_ENV: recorded},
        )
        restarted = datetime.fromisoformat(fresh.stdout.strip())
        assert restarted == start + timedelta(days=30)  # exactly start + window
        assert restarted < datetime.now(timezone.utc)  # and it stays in the past

    async def test_a_start_recorded_inside_the_window_still_accepts_the_legacy_token(
        self, app, client, mailbox, monkeypatch
    ):
        """The bounded window is a WINDOW: a start recorded 5 days ago
        keeps validating the legacy derivation (old attempts finish)."""
        await _put_run(app, WORK, generation=2)
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)
        monkeypatch.setenv(
            LANE_LEGACY_TOKEN_START_ENV,
            (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
        )

        pending = await client.get("/lane/controls", params={"work_id": WORK}, headers=auth())

        assert pending.status_code == 200

    async def test_a_legacy_token_past_the_deadline_is_refused(
        self, app, client, mailbox, monkeypatch
    ):
        await _put_run(app, WORK, generation=2)
        await mailbox.submit(_cmd(1))
        monkeypatch.setenv(LANE_LEGACY_TOKEN_DEADLINE_ENV, "2020-01-01T00:00:00+00:00")

        pending = await client.get("/lane/controls", params={"work_id": WORK}, headers=auth())
        ack = await client.post(
            f"/lane/controls/cmd-{WORK}-1/ack", json={"state": "authorized"}, headers=auth()
        )

        assert pending.status_code == 403
        assert "migration deadline" in pending.json()["detail"]
        assert ack.status_code == 403

    async def test_a_generation_token_survives_past_the_deadline(
        self, app, client, mailbox, monkeypatch
    ):
        """The deadline retires the LEGACY derivation only: the
        dispatch-issued attempt credential is the standing contract."""
        await _put_run(app, WORK, generation=2)
        command = _cmd(1)
        await mailbox.submit(command)
        await _authorize(mailbox, command)
        monkeypatch.setenv(LANE_LEGACY_TOKEN_DEADLINE_ENV, "2020-01-01T00:00:00+00:00")

        pending = await client.get(
            "/lane/controls", params={"work_id": WORK}, headers=_gen_headers(WORK, 2)
        )

        assert pending.status_code == 200

    async def test_a_generation_authority_outage_refuses_rather_than_degrading(
        self, app, client, mailbox, monkeypatch
    ):
        """NEXT-01's reviewer case: a FAILED generation lookup is an
        unavailable authority (503), never "the work must be legacy"."""
        import forge.api_lane_control as api_lane_control

        await _put_run(app, WORK, generation=2)
        await mailbox.submit(_cmd(1))

        async def _broken(session_factory, work_id):
            raise api_lane_control.LaneAuthorityUnavailable("storage down")

        monkeypatch.setattr(api_lane_control, "durable_run_generation", _broken)
        response = await client.get("/lane/controls", params={"work_id": WORK}, headers=auth())

        assert response.status_code == 503
        assert "authority is unavailable" in response.json()["detail"]


# -- the durable resume-spec read (NEXT-03) --------------------------------------


class TestResumeSpecRead:
    async def test_the_latest_resume_command_row_is_served_whatever_its_rung(
        self, app, client, mailbox
    ):
        """The resume decision outlives the pending set: a resume command
        ACKed all the way past ``authorized`` still answers with its
        payload (the lane restores by the ROW, not the queue)."""
        command = _cmd(
            4,
            kind="resume",
            payload={
                "checkpoint_ref": f"{WORK}@{'a' * 64}",
                "checkpoint_sequence": 7,
                "source_oid": "0" * 40,
            },
        )
        await mailbox.submit(command)
        await _authorize(mailbox, command)
        await mailbox.dispatch(
            command.command_id, current_plan_revision=1, current_execution_epoch=1
        )

        response = await client.get(
            "/lane/controls/resume-spec", params={"work_id": WORK}, headers=auth()
        )

        assert response.status_code == 200
        body = response.json()
        assert body["command"]["command_id"] == command.command_id
        assert body["command"]["status"] == "dispatching"  # gone from pending
        assert body["command"]["payload"]["checkpoint_ref"] == f"{WORK}@{'a' * 64}"
        assert body["command"]["payload"]["checkpoint_sequence"] == 7

    async def test_the_latest_of_several_resumes_wins(self, app, client, mailbox):
        older = _cmd(1, kind="resume", payload={"checkpoint_ref": f"{WORK}@{'1' * 64}"})
        newer = _cmd(2, kind="resume", payload={"checkpoint_ref": f"{WORK}@{'2' * 64}"})
        await mailbox.submit(older)
        await mailbox.submit(newer)

        response = await client.get(
            "/lane/controls/resume-spec", params={"work_id": WORK}, headers=auth()
        )

        assert response.json()["command"]["command_id"] == newer.command_id

    async def test_no_resume_ever_answers_command_none(self, app, client, mailbox):
        await mailbox.submit(_cmd(1))  # a steer, not a resume

        response = await client.get(
            "/lane/controls/resume-spec", params={"work_id": WORK}, headers=auth()
        )

        assert response.status_code == 200
        assert response.json()["command"] is None

    async def test_the_read_cannot_cross_works(self, app, client, mailbox):
        await mailbox.submit(_cmd(1, kind="resume"))

        response = await client.get(
            "/lane/controls/resume-spec", params={"work_id": WORK}, headers=auth(OTHER_WORK)
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "lane token does not scope this work"


# -- the runner-time credential redemption (R38-02 / #303) ----------------------


#: The sentinel the BROKER stages — what a successful redemption returns
#: (and what the audit row must NEVER contain).
REDEEM_SENTINEL = "sk-redeem-sentinel-0123456789"  # noqa: S105 — a test fixture value
REDEEM_REF = "env:ANTHROPIC_AUTH_TOKEN"
#: The run's canonical subject (gitlab family, no recorded connection,
#: the numeric project id — the subject_of_run derivation).
REDEEM_PROJECT_ID = 90210
REDEEM_SUBJECT_ID = "gitlab/-/90210"


async def _put_gitlab_run(app, work_id: str, *, generation: int) -> None:
    async with app.state.session_factory() as session:
        session.add(
            FlowRun(
                id=work_id,
                project_id=REDEEM_PROJECT_ID,
                provider="gitlab",
                cancellation_generation=generation,
            )
        )
        await session.commit()


def _bound_credential_lab(app) -> None:
    """A registry with THIS subject's live binding + a broker staging the
    sentinel, both mounted on the app (the control plane's own wiring)."""
    from forge.adaptive.credential_broker import StagedBroker
    from forge.adaptive.operator_snapshot import CanonicalSubject
    from forge.adaptive.project_credentials import ProjectCredentialRegistry

    registry = ProjectCredentialRegistry()
    registry.bind(
        CanonicalSubject(
            provider_family="gitlab", connection="-", native_id=str(REDEEM_PROJECT_ID)
        ),
        "anthropic-gateway",
        REDEEM_REF,
        bound_by="ops@a",
    )
    broker = StagedBroker()
    broker.stage(REDEEM_REF, REDEEM_SENTINEL, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
    app.state.credential_registry = registry
    app.state.credential_broker = broker


async def _grant_dispatch(
    app,
    work_id: str,
    *,
    generation: int,
    ref: str = REDEEM_REF,
    provider: str = "anthropic-gateway",
    deadline_seconds: float = 3600.0,
    created_days_ago: float = 0.0,
) -> str:
    """Persist the attempt's operation grant EXACTLY the dispatch seam
    does (Q39-01) — through :func:`persist_operation_grant`, so the test
    evidence shape is the production one. Returns the grant id."""
    from datetime import timedelta

    from forge.adaptive.credential_broker import CredentialOperationGrant
    from forge.api_lane_control import persist_operation_grant

    now = datetime.now(timezone.utc) - timedelta(days=created_days_ago)
    grant = CredentialOperationGrant(
        grant_id=uuid.uuid4().hex,
        work_id=work_id,
        subject=REDEEM_SUBJECT_ID,
        provider=provider,
        credential_ref=ref,
        binding_revision=1,
        attempt_generation=generation,
        delivery_mode="runner-redemption",
        redemption_deadline=now + timedelta(seconds=deadline_seconds),
        created_at=now,
    )
    effective = await persist_operation_grant(app.state.session_factory, grant=grant)
    return effective.grant_id


def _redeem_params(work_id: str = WORK, ref: str = REDEEM_REF) -> dict[str, str]:
    return {"work_id": work_id, "credential_ref": ref, "provider": "anthropic-gateway"}


async def _redemptions(app, work_id: str) -> list[dict]:
    """The work's audit records from the APPEND-ONLY ledger (Q39-03 —
    the table is the audit authority; the embedded evidence list is its
    bounded projection)."""
    from forge.adaptive.credential_audit import recent_redemptions

    return await recent_redemptions(app.state.session_factory, work_id, limit=100)


class TestCredentialRedemption:
    """Delivery profile (b): the lane exchanges its EXISTING attempt-scoped
    token for the bound model credential — authorized against the
    attempt's OPERATION GRANT (Q39-01), TTL-bounded, audited before the
    value leaves — and every wrong-axis attempt retrieves NOTHING."""

    async def test_the_current_attempt_redeems_the_bound_credential(self, app, client):
        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)
        grant_id = await _grant_dispatch(app, WORK, generation=2)

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers(WORK, 2)
        )

        assert response.status_code == 200
        body = response.json()
        assert body["value"] == REDEEM_SENTINEL
        assert body["env_var"] == "ANTHROPIC_AUTH_TOKEN"
        assert body["credential_ref"] == REDEEM_REF
        assert body["binding_revision"] == 1
        assert body["redemption_id"]
        assert body["grant_id"] == grant_id  # the join every receipt keys on
        assert body["attempt_generation"] == 2
        assert body["work_id"] == WORK
        # TTL-bounded to the attempt: a concrete, near-future expiry.
        expires = datetime.fromisoformat(body["expires_at"])
        assert expires > datetime.now(timezone.utc)
        assert expires <= datetime.now(timezone.utc) + timedelta(hours=2)
        # The audit row persisted BEFORE the value left — refs only.
        rows = await _redemptions(app, WORK)
        assert len(rows) == 1
        assert rows[0]["redemption_id"] == body["redemption_id"]
        assert rows[0]["grant_id"] == grant_id
        assert rows[0]["attempt_generation"] == 2
        assert REDEEM_SENTINEL not in json.dumps(rows)

    async def test_a_superseded_generations_token_cannot_redeem(self, app, client):
        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)
        await _grant_dispatch(app, WORK, generation=2)

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers(WORK, 1)
        )

        assert response.status_code == 403
        assert "superseded runner generation" in response.json()["detail"]
        assert await _redemptions(app, WORK) == []  # zero successful retrievals

    async def test_another_works_token_cannot_redeem(self, app, client):
        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)
        await _grant_dispatch(app, WORK, generation=2)

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE,
            params=_redeem_params(),
            headers=_gen_headers(OTHER_WORK, 2),
        )

        assert response.status_code == 403
        assert await _redemptions(app, WORK) == []

    async def test_a_revoked_binding_redeems_nothing(self, app, client):
        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)
        await _grant_dispatch(app, WORK, generation=2)
        app.state.credential_registry.revoke(
            CanonicalSubject(
                provider_family="gitlab", connection="-", native_id=str(REDEEM_PROJECT_ID)
            ),
            "anthropic-gateway",
            revoked_by="ops@a",
        )

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers(WORK, 2)
        )

        assert response.status_code == 403
        assert "revoked" in response.json()["detail"]
        assert await _redemptions(app, WORK) == []

    async def test_a_changed_reference_redeems_nothing(self, app, client):
        """Q39-01: an attempt that changes the REQUESTED reference cannot
        reach even its own route's sibling ref — the grant names the EXACT
        ref, and the refusal happens with ZERO broker calls."""
        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)
        await _grant_dispatch(app, WORK, generation=2)
        broker = app.state.credential_broker

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE,
            params=_redeem_params(ref="vault:kv/other#1"),
            headers=_gen_headers(WORK, 2),
        )

        assert response.status_code == 403
        assert "grant_ref_mismatch" in response.json()["detail"]
        assert broker.resolve_calls == []  # refused before ANY broker I/O
        assert await _redemptions(app, WORK) == []

    async def test_an_unbound_subject_redeems_nothing(self, app, client):
        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)
        await _grant_dispatch(app, WORK, generation=2)
        app.state.credential_registry = ProjectCredentialRegistry()  # no binding

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers(WORK, 2)
        )

        assert response.status_code == 403
        assert "no_binding" in response.json()["detail"]
        assert await _redemptions(app, WORK) == []

    async def test_a_broker_refusal_is_a_403_never_a_value(self, app, client):
        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)
        await _grant_dispatch(app, WORK, generation=2)
        app.state.credential_broker = StagedBroker()  # stages nothing

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers(WORK, 2)
        )

        assert response.status_code == 403
        assert "unresolved_ref" in response.json()["detail"]
        assert await _redemptions(app, WORK) == []

    async def test_a_broker_staging_the_wrong_slot_is_refused(self, app, client):
        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)
        await _grant_dispatch(app, WORK, generation=2)
        broker = StagedBroker()
        broker.stage(REDEEM_REF, REDEEM_SENTINEL, env_var="ZAI_API_KEY")
        app.state.credential_broker = broker

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers(WORK, 2)
        )

        assert response.status_code == 403
        assert "staged_slot_mismatch" in response.json()["detail"]

    async def test_a_missing_bearer_is_401_and_no_secret_is_503(self, app, client):
        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)

        missing = await client.get(LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params())
        assert missing.status_code == 401

        app.state.settings.FORGE_LANE_CONTROL_SECRET = None
        disabled = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers(WORK, 2)
        )
        assert disabled.status_code == 503
        assert "disabled" in disabled.json()["detail"]

    async def test_an_unknown_work_is_404_and_an_unsubjected_work_403(self, app, client):
        _bound_credential_lab(app)
        unknown = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers(WORK, 2)
        )
        assert unknown.status_code in (403, 404)  # no run row — fail closed

        # A run whose row names no subject (unknown provider family).
        async with app.state.session_factory() as session:
            session.add(FlowRun(id=OTHER_WORK, project_id=1, provider="drill"))
            await session.commit()
        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE,
            params=_redeem_params(work_id=OTHER_WORK),
            headers=_gen_headers(OTHER_WORK, 0),
        )
        assert response.status_code == 403
        assert "no credential binding subject" in response.json()["detail"]

    async def test_a_malformed_ttl_refuses_rather_than_re_defaulting(
        self, app, client, monkeypatch
    ):
        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)
        monkeypatch.setenv(REDEMPTION_TTL_ENV, "soon")

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers(WORK, 2)
        )

        assert response.status_code == 503
        assert REDEMPTION_TTL_ENV in response.json()["detail"]
        with pytest.raises(LegacyWindowInvalid, match=REDEMPTION_TTL_ENV):
            redemption_ttl_seconds({"FORGE_CREDENTIAL_REDEEM_TTL_SECONDS": "soon"})
        # The default and valid shapes.
        assert redemption_ttl_seconds({}) == 3600.0
        assert redemption_ttl_seconds({REDEMPTION_TTL_ENV: "600"}) == 600.0

    async def test_an_audit_persistence_failure_prevents_the_credential_response(
        self, app, client, monkeypatch
    ):
        """Q39-03: every redemption is centrally auditable or it does not
        happen — a failed ledger/projection write refuses the response
        (503), the value NEVER leaves, and no partial audit row leaks."""
        from forge.adaptive import credential_audit

        await _put_gitlab_run(app, WORK, generation=2)
        _bound_credential_lab(app)
        await _grant_dispatch(app, WORK, generation=2)

        async def failing_record(session_factory, redemption):
            raise RuntimeError("the audit ledger is unreachable")

        monkeypatch.setattr(credential_audit, "record_redemption", failing_record)
        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers(WORK, 2)
        )
        assert response.status_code == 503
        assert "redemption audit could not be persisted" in response.json()["detail"]
        assert await _redemptions(app, WORK) == []  # zero successful retrievals
