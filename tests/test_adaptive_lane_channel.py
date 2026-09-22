"""The lane-side control channel (NXT-10 outbound leg) — poller + drain.

The CI lane's half of the dial-out: :class:`LaneControlChannel` backs the
SAME sync mailbox surface the steering session's drain already consumes,
so the remote leg is proven by driving the REAL
:class:`~forge.adaptive.lane_control.LaneSteeringSession` over it against
a fake control plane (pytest-httpx) — the steer lands on the vendor
client and the ladder acks round-trip, exactly as over the local mailbox.

Also pinned:

- :func:`lane_channel_from_env` swaps in ONLY on the full env pair (a
  half-configured dial-out stays local) and derives the work id the same
  way the steering attach does;
- ``pending`` delivers each command ONCE and advances the
  ``after_sequence`` cursor the next fetch carries;
- an unreachable/erroring control plane journals an error row and keeps
  the turn's world intact — the buffer is only ever replaced by a proven
  response, and an undeliverable ack RAISES so the mailbox gate refuses
  (never an optimistic booking the durable row cannot support).
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from forge.adaptive.adapters import ClaudeSDKAdapter
from forge.adaptive.control import MailboxSurface
from forge.adaptive.lane_channel import (
    DEFAULT_POLL_INTERVAL_S,
    LANE_CONTROL_GENERATION_ENV,
    LANE_CONTROL_TOKEN_ENV,
    LANE_CONTROL_URL_ENV,
    LaneControlChannel,
    lane_channel_from_env,
)
from forge.adaptive.lane_control import LaneSteeringSession
from forge.adaptive.models import ControlCommand
from forge.lane_driver import steering_service_from_env

BASE = "http://cp.test"
TOKEN = "lane-token-1"
WORK = "run-77"
GET_URL = re.compile(rf"{re.escape(BASE)}/lane/controls(\?.*)?$")
ACK_URL = re.compile(rf"{re.escape(BASE)}/lane/controls/[^/]+/ack$")


def _cmd(seq: int, *, work_id: str = WORK, status: str = "received") -> ControlCommand:
    return ControlCommand.model_validate(
        {
            "schema": "forge.proposal.control-command/1",
            "command_id": f"cmd-{seq}",
            "work_id": work_id,
            "sequence": seq,
            "kind": "steer",
            "actor_ref": "human:op",
            "actor_origin": "server_authenticated_human",
            "idempotency_key": f"key-{seq}",
            "status": status,
            "payload": {"run_id": work_id, "text": f"tighten retry bounds ({seq})"},
        }
    )


def _dump(command: ControlCommand, status: str) -> dict:
    return command.model_copy(update={"status": status}).model_dump(mode="json")


def channel(**overrides) -> LaneControlChannel:
    values = dict(base_url=BASE, token=TOKEN, work_id=WORK, run_id=WORK, ack_timeout=2.0)
    values.update(overrides)
    return LaneControlChannel(**values)


def json_body(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


# -- the env seam ---------------------------------------------------------------


class TestFromEnv:
    def test_no_env_means_no_channel(self):
        assert lane_channel_from_env({}) is None

    def test_the_pair_is_all_or_nothing(self):
        url_only = {LANE_CONTROL_URL_ENV: BASE}
        token_only = {LANE_CONTROL_TOKEN_ENV: TOKEN, "FORGE_RUN_ID": WORK}
        assert lane_channel_from_env(url_only) is None
        assert lane_channel_from_env(token_only) is None
        assert lane_channel_from_env({**url_only, **token_only}) is not None

    def test_nothing_to_scope_to_means_no_channel(self):
        env = {LANE_CONTROL_URL_ENV: BASE, LANE_CONTROL_TOKEN_ENV: TOKEN}
        assert lane_channel_from_env(env) is None

    def test_the_work_id_falls_back_to_the_run_id(self):
        env = {
            LANE_CONTROL_URL_ENV: BASE,
            LANE_CONTROL_TOKEN_ENV: TOKEN,
            "FORGE_RUN_ID": "run-9",
        }
        assert lane_channel_from_env(env).work_id == "run-9"
        assert lane_channel_from_env({**env, "FORGE_WORK_ID": "wp-9"}).work_id == "wp-9"

    def test_a_malformed_poll_cadence_fails_closed_to_the_default(self):
        env = {
            LANE_CONTROL_URL_ENV: BASE,
            LANE_CONTROL_TOKEN_ENV: TOKEN,
            "FORGE_RUN_ID": WORK,
            "FORGE_LANE_CONTROL_POLL_SECONDS": "fast",
        }
        assert lane_channel_from_env(env).poll_interval == DEFAULT_POLL_INTERVAL_S

    def test_the_runner_generation_rides_in_from_the_dispatch_env(self):
        base = {
            LANE_CONTROL_URL_ENV: BASE,
            LANE_CONTROL_TOKEN_ENV: TOKEN,
            "FORGE_RUN_ID": WORK,
        }
        # Unset → nothing declared (the pre-generation lane, the default).
        assert lane_channel_from_env(base).generation is None
        assert lane_channel_from_env({**base, LANE_CONTROL_GENERATION_ENV: "3"}).generation == 3
        # Malformed or negative degrades to "no generation declared" — a
        # typo must not brick the lane's acks against a pre-generation
        # control plane.
        assert (
            lane_channel_from_env({**base, LANE_CONTROL_GENERATION_ENV: "soon"}).generation is None
        )
        assert lane_channel_from_env({**base, LANE_CONTROL_GENERATION_ENV: "-1"}).generation is None

    def test_the_lane_driver_seam_swaps_the_channel_in(self):
        local = steering_service_from_env({"FORGE_STEERING_ENABLED": "1"})
        assert not isinstance(local.mailbox, LaneControlChannel)

        env = {
            "FORGE_STEERING_ENABLED": "1",
            LANE_CONTROL_URL_ENV: BASE,
            LANE_CONTROL_TOKEN_ENV: TOKEN,
            "FORGE_RUN_ID": WORK,
        }
        assert isinstance(steering_service_from_env(env).mailbox, LaneControlChannel)

    def test_the_flag_off_beats_the_pair(self):
        env = {
            LANE_CONTROL_URL_ENV: BASE,
            LANE_CONTROL_TOKEN_ENV: TOKEN,
            "FORGE_RUN_ID": WORK,
        }
        assert steering_service_from_env(env) is None

    def test_the_channel_satisfies_the_reference_drain_protocol(self):
        assert isinstance(channel(), MailboxSurface)


class FakeControlPlane:
    """The in-test control plane: pending GET + guarded ack POSTs.

    Holds one command per id at a mutable status; the GET answers the
    received/authorized view (``after_sequence`` honored), the POST walks
    the same rungs the real endpoint does — including the CTL-04 CAS that
    EXPIRES a stale-world dispatch — enough truth to prove the channel
    against the REAL drain.
    """

    def __init__(self, *commands: ControlCommand) -> None:
        self.commands = {c.command_id: c for c in commands}
        self.statuses = {c.command_id: "received" for c in commands}
        self.ack_bodies: list[dict] = []

    def mount(self, httpx_mock: HTTPXMock) -> None:
        """The whole plane (drain tests): pending GETs + the ack ladder."""
        self.mount_get(httpx_mock)
        self.mount_ack(httpx_mock)

    def mount_get(self, httpx_mock: HTTPXMock) -> None:
        # Reusable: the channel may fetch several times over its lifetime.
        httpx_mock.add_callback(self._get, url=GET_URL, is_reusable=True)

    def mount_ack(self, httpx_mock: HTTPXMock) -> None:
        # Reusable: one applied command walks a full ladder of acks.
        httpx_mock.add_callback(self._ack, url=ACK_URL, is_reusable=True)

    def _get(self, request: httpx.Request) -> httpx.Response:
        after = int(request.url.params.get("after_sequence", "0"))
        work = request.url.params.get("work_id", "")
        commands = [
            _dump(command, self.statuses[command.command_id])
            for command in sorted(self.commands.values(), key=lambda c: c.sequence)
            if command.work_id == work
            and command.sequence > after
            and self.statuses[command.command_id] in ("received", "authorized")
        ]
        return httpx.Response(200, json={"work_id": work, "commands": commands})

    def _ack(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.ack_bodies.append(body)
        command_id = request.url.path.rsplit("/", 2)[-2]
        state = body["state"]
        if state == "authorized":
            self.statuses[command_id] = "authorized"
        elif state == "dispatching":
            command = self.commands[command_id]
            stale = command.expected_execution_epoch not in (None, body["execution_epoch"])
            self.statuses[command_id] = "expired" if stale else "dispatching"
        elif state == "checkpointed":
            self.statuses[command_id] = "checkpointed"
        return httpx.Response(
            200,
            json={
                "command_id": command_id,
                "state": self.statuses[command_id],
                "command": _dump(self.commands[command_id], self.statuses[command_id]),
            },
        )


# -- the fetch leg -----------------------------------------------------------------


class TestFetch:
    def test_pending_delivers_once_and_advances_the_cursor(self, httpx_mock: HTTPXMock):
        FakeControlPlane(_cmd(2), _cmd(1)).mount_get(httpx_mock)
        ch = channel()

        first = ch.pending(WORK)

        assert [c.sequence for c in first] == [1, 2]  # sequence order, not arrival
        # the plane still owes both (they never left pending server-side),
        # but delivery is once per channel lifetime: the next fetch carries
        # the advanced cursor, and the plane answers it with nothing new
        assert ch.pending(WORK) == []
        request = httpx_mock.get_requests()[-1]
        assert request.url.params["after_sequence"] == "2"

    def test_pending_never_hands_over_another_works_commands(self, httpx_mock: HTTPXMock):
        FakeControlPlane(_cmd(1, work_id="run-OTHER")).mount_get(httpx_mock)
        ch = channel()
        assert ch.pending(WORK) == []
        assert ch.channel_journal == []  # scoping is not an error, just not ours

    def test_a_failed_fetch_journals_and_never_raises(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=GET_URL, status_code=500, is_reusable=True)
        ch = channel()

        assert ch.pending(WORK) == []

        assert [row["type"] for row in ch.channel_journal] == ["lane_control_error"]

    def test_an_unreachable_plane_journals_and_never_raises(self, httpx_mock: HTTPXMock):
        httpx_mock.add_exception(httpx.ConnectError("plane down"), url=re.compile(r".*"))
        ch = channel()

        assert ch.pending(WORK) == []
        assert any("fetch failed" in row["error"] for row in ch.channel_journal)

    async def test_the_async_loop_delivers_and_stops_cleanly(self, httpx_mock: HTTPXMock):
        FakeControlPlane(_cmd(1)).mount_get(httpx_mock)
        ch = channel(poll_interval=0.01)
        async with ch:
            for _ in range(100):
                if ch._buffer:  # noqa: SLF001 — the loop's observable effect
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("the fetch loop never delivered the command")
        assert ch._fetch_task is None  # noqa: SLF001 — teardown proven


# -- the ack leg --------------------------------------------------------------------


class TestAcks:
    def _mock_ack(self, httpx_mock: HTTPXMock, status: str) -> None:
        httpx_mock.add_response(
            url=f"{BASE}/lane/controls/cmd-1/ack",
            json={"command_id": "cmd-1", "state": status, "command": _dump(_cmd(1), status)},
        )

    def test_authorize_posts_the_state_and_returns_the_command(self, httpx_mock: HTTPXMock):
        self._mock_ack(httpx_mock, "authorized")
        command = channel().authorize("cmd-1", {})

        assert command.status == "authorized"
        request = httpx_mock.get_requests()[-1]
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        body = json_body(request)
        assert body["state"] == "authorized"
        assert body["journal_row"]["source"] == "lane_channel"

    def test_apply_carries_the_cas_world_and_the_vendor_correlation(self, httpx_mock: HTTPXMock):
        self._mock_ack(httpx_mock, "dispatching")
        ch = channel()
        ch.bind_vendor_session("sess-9")

        command = ch.apply("cmd-1", current_plan_revision=3, current_execution_epoch=2)

        assert command.status == "dispatching"
        body = json_body(httpx_mock.get_requests()[-1])
        assert body["plan_revision"] == 3
        assert body["execution_epoch"] == 2
        assert body["vendor_correlation_id"] == "sess-9"

    def test_checkpoint_acks_the_climb(self, httpx_mock: HTTPXMock):
        self._mock_ack(httpx_mock, "checkpointed")
        assert channel().checkpoint("cmd-1").status == "checkpointed"

    def test_the_acks_declare_the_runner_generation(self, httpx_mock: HTTPXMock):
        """R28-10: a generation-carrying channel DECLARES it on every ack —
        the control plane can then refuse a superseded generation's ack
        before any state moves."""
        self._mock_ack(httpx_mock, "authorized")
        ch = channel(generation=4)

        ch.authorize("cmd-1", {})

        body = json_body(httpx_mock.get_requests()[-1])
        assert body["generation"] == 4
        assert body["journal_row"]["generation"] == 4

    def test_a_generationless_channel_omits_the_field_entirely(self, httpx_mock: HTTPXMock):
        """Pre-generation lanes never send the key — the honest migration
        default, not an explicit null."""
        self._mock_ack(httpx_mock, "authorized")

        channel().authorize("cmd-1", {})

        assert "generation" not in json_body(httpx_mock.get_requests()[-1])

    def test_a_refused_transition_raises_the_mailboxes_own_error(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{BASE}/lane/controls/cmd-1/ack",
            status_code=409,
            json={"detail": "guarded transition refused: ladder"},
        )
        ch = channel()

        with pytest.raises(ValueError, match="ack refused"):
            ch.authorize("cmd-1", {})

        assert any("ack (authorized) refused" in row["error"] for row in ch.channel_journal)

    def test_an_unknown_command_raises_keyerror(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{BASE}/lane/controls/cmd-1/ack", status_code=404, json={"detail": "unknown"}
        )
        with pytest.raises(KeyError):
            channel().authorize("cmd-1", {})

    def test_an_auth_refusal_raises_permissionerror(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{BASE}/lane/controls/cmd-1/ack", status_code=403, json={"detail": "no scope"}
        )
        with pytest.raises(PermissionError):
            channel().checkpoint("cmd-1")

    def test_an_unreachable_ack_raises_and_journals_the_error(self, httpx_mock: HTTPXMock):
        httpx_mock.add_exception(httpx.ConnectError("plane down"), url=re.compile(r".*"))
        ch = channel()

        with pytest.raises(ValueError, match="stays in the mailbox"):
            ch.checkpoint("cmd-1")

        assert any("ack (checkpointed) failed" in row["error"] for row in ch.channel_journal)

    def test_the_lane_never_submits(self):
        with pytest.raises(PermissionError):
            channel().submit(_cmd(1))


# -- the SAME drain path, over the channel --------------------------------------------


class FakeClaudeClient:
    """Matches forge.adaptive.adapters.ClaudeSDKClient (the steer surface)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def start_session(self, task: str) -> str:
        return "sess-1"

    async def send(self, session_id: str, text: str) -> None:
        self.calls.append(("steer", session_id, text))

    async def interrupt(self, session_id: str) -> None:
        self.calls.append(("interrupt", session_id))

    async def query(self, session_id: str) -> list[dict]:
        return []


class TestTheDrainOverTheChannel:
    async def test_a_steer_flows_the_whole_leg(self, httpx_mock: HTTPXMock):
        plane = FakeControlPlane(_cmd(1))
        plane.mount(httpx_mock)
        client = FakeClaudeClient()
        ch = channel()
        session = LaneSteeringSession(
            service=ch.service(),
            driver=ClaudeSDKAdapter(client),
            driver_kind="claude",
            run_id=WORK,
            work_id=WORK,
        )
        session.bind("sess-1")
        ch.bind_vendor_session("sess-1")

        actions = await session.drain_once()

        # the vendor got the guidance; the journal books it applied
        assert client.calls == [("steer", "sess-1", "tighten retry bounds (1)")]
        (action,) = actions
        assert action.outcome == "applied"
        assert action.delivery == "application_observed"
        assert action.detail["mailbox_status"] == "checkpointed"
        # the control plane saw the whole guarded ladder round-trip
        assert [body["state"] for body in plane.ack_bodies] == [
            "authorized",
            "dispatching",
            "checkpointed",
        ]
        assert plane.statuses["cmd-1"] == "checkpointed"
        # every ack carried the lane's journal row
        assert all(body["journal_row"]["source"] == "lane_channel" for body in plane.ack_bodies)
        # a second drain consumes nothing: delivered once, checkpointed anyway
        assert await session.drain_once() == []

    async def test_an_expired_command_is_refused_not_delivered(self, httpx_mock: HTTPXMock):
        stale = _cmd(1).model_copy(update={"expected_execution_epoch": 9})  # the lane holds 1
        plane = FakeControlPlane(stale)
        plane.mount(httpx_mock)
        ch = channel()
        session = LaneSteeringSession(
            service=ch.service(),
            driver=ClaudeSDKAdapter(FakeClaudeClient()),
            driver_kind="claude",
            run_id=WORK,
            work_id=WORK,
        )
        session.bind("sess-1")

        (action,) = await session.drain_once()

        assert action.outcome == "refused"
        assert "expired" in action.reason
        assert plane.statuses["cmd-1"] == "expired"
        # the plane never saw a checkpointed climb for a dead-world command
        assert [body["state"] for body in plane.ack_bodies] == ["authorized", "dispatching"]


# -- the lane_driver integration: the attach over the channel --------------------


class SteerableLaneClient:
    """The lane's own client contract AND the steering adapter's protocol —
    ONE object, the SAME-client rule (mirrors tests/test_lane_driver.py):
    the turn's ResultMessage appears only once the steer landed, so the
    concurrent drain over the REMOTE channel is proven deterministic."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.steered = asyncio.Event()

    async def start_session(self, task: str) -> str:
        self.calls.append(("start_session", task))
        return "sess-1"

    async def send(self, session_id: str, text: str) -> None:
        self.calls.append(("steer", session_id, text))
        self.steered.set()

    async def interrupt(self, session_id: str) -> None:
        self.calls.append(("interrupt", session_id))

    async def query(self, session_id: str) -> list[dict]:
        if self.steered.is_set():
            return [{"is_error": False, "num_turns": 2, "terminal_reason": "completed"}]
        return []

    async def close(self, session_id: str) -> None:
        self.calls.append(("close", session_id))


class TestLaneDriverIntegration:
    async def test_drive_lane_drains_the_remote_mailbox_mid_turn(
        self, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
    ):
        from forge.lane_driver import drive_lane

        # the attach derives the lane identity from the dispatch env
        monkeypatch.setenv("FORGE_RUN_ID", WORK)
        monkeypatch.setenv("FORGE_WORK_ID", WORK)
        FakeControlPlane(_cmd(1)).mount(httpx_mock)
        client = SteerableLaneClient()
        control = channel(poll_interval=0.01).service()

        outcome = await drive_lane(
            client, task="do the thing", budget_s=5.0, poll_s=0.01, control=control
        )

        # the turn could only complete THROUGH the remotely drained steer
        assert outcome.exit_status == "completed"
        assert ("steer", "sess-1", "tighten retry bounds (1)") in client.calls
        (entry, *rest) = outcome.steering_journal or []
        assert entry["kind"] == "steer"
        assert entry["outcome"] == "applied"
        assert entry["detail"]["mailbox_status"] == "checkpointed"
        assert rest == []  # no channel error rows on a healthy plane

    async def test_channel_error_rows_ride_the_meta_journal(self, httpx_mock: HTTPXMock):
        from forge.lane_driver import _steering_journal

        httpx_mock.add_response(url=GET_URL, status_code=500, is_reusable=True)
        ch = channel()
        session = LaneSteeringSession(
            service=ch.service(),
            driver=ClaudeSDKAdapter(FakeClaudeClient()),
            driver_kind="claude",
            run_id=WORK,
            work_id=WORK,
        )
        session.bind("sess-1")
        ch.pending(WORK)  # the failing fetch journals its error row

        rows = _steering_journal(session)

        assert rows and rows[-1]["type"] == "lane_control_error"
        assert "fetch failed" in rows[-1]["error"]
