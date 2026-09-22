"""The lane control API (NXT-10 outbound leg) — auth, scoping, ladder.

The surface a CI lane dials OUT to (EXE-04): pending control commands off
the durable ``control_commands`` rows, plus the guarded ack transitions
the lane's drain climbs. Pinned here:

- fail-closed: no ``FORGE_LANE_CONTROL_SECRET`` → BOTH routes 503, never
  unauthenticated-open; no session factory → 503 too;
- the token scheme: ``HMAC-SHA256(secret, work_id)`` — a lane holding
  run A's token can neither poll run B's queue nor ack B's commands
  (403, work-scoping by construction);
- pending means received/authorized ONLY, in durable sequence order, and
  ``after_sequence`` is an honest cursor;
- every ack is the PostgresMailbox's own guarded CAS: rung skips are 409,
  a stale-world dispatch EXPIRES, and ``checkpointed`` climbs the
  observation rungs (idempotently);
- the lane's journal row is APPENDED to the row's audit journal —
  evidence beside the transition entries, never a status claim.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox
from forge.adaptive.models import ControlCommand
from forge.api_lane_control import lane_control_token, verify_lane_token
from forge.config import Settings
from forge.database import reset_engine
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
