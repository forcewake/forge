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
  NEXT-01 migration window (FORGE_LANE_LEGACY_TOKEN_DEADLINE, default
  30 days from now) — an unavailable generation authority is a 503
  refusal, never a silent legacy acceptance;
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

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox
from forge.adaptive.models import ControlCommand
from forge.api_lane_control import (
    LANE_LEGACY_TOKEN_DEADLINE_ENV,
    _superseded_generation,
    lane_control_token,
    legacy_token_deadline,
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


# -- the legacy migration window (NEXT-01) -------------------------------------


class TestLegacyTokenDeadline:
    def test_the_deadline_defaults_to_thirty_days_from_now(self):
        deadline = legacy_token_deadline({})
        remaining = deadline - datetime.now(timezone.utc)

        assert timedelta(days=29) <= remaining <= timedelta(days=30)

    def test_an_explicit_iso_deadline_is_honored(self):
        fixed = "2020-01-01T00:00:00+00:00"
        assert legacy_token_deadline(
            {LANE_LEGACY_TOKEN_DEADLINE_ENV: fixed}
        ) == datetime.fromisoformat(fixed)
        naive = legacy_token_deadline({LANE_LEGACY_TOKEN_DEADLINE_ENV: "2020-01-01"})
        assert naive.tzinfo is not None  # naive reads as UTC
        # malformed degrades to the default window, never to "no deadline"
        assert legacy_token_deadline({LANE_LEGACY_TOKEN_DEADLINE_ENV: "soon"}) > (
            datetime.now(timezone.utc)
        )

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
