"""Q35-06 — the legacy credential window closes independently of restarts.

The defect being closed: the per-request sliding deadline was fixed
(R32-03) and explicit START/DEADLINE configuration was already
restart-stable, but the DEFAULT anchor was captured at module import —
a new process without persisted state re-opened a fresh 30-day window.
The resolution ladder in :func:`forge.api_lane_control.
resolve_legacy_window` now persists a WRITE-ONCE anchor file, refuses
malformed explicit configuration with a typed failure, and fails
closed (legacy acceptance refused) when nothing restart-stable can be
configured or persisted. Pinned here:

- explicit valid deadline → two FRESH resolutions (this process and a
  subprocess with the recorded state alone) agree;
- no config + a writable location → the FIRST resolution creates the
  anchor file, every later resolution (in-process or across a
  "restart") reads the SAME anchor — the window never extends, and an
  existing file is THE anchor forever (corrupt content refuses rather
  than rewriting);
- restart after expiry cannot revive a legacy token, while
  generation-scoped tokens are unaffected;
- malformed / empty / out-of-sanity-bounds configuration is a typed
  :class:`LegacyWindowInvalid` carrying the variable name and value —
  separately, including through the wire (a specific refusal, never a
  500 and never a silent re-anchor);
- an unwritable anchor with no explicit configuration → legacy
  acceptance REFUSED fail-closed with a specific diagnostic;
  generation tokens keep working;
- ``forge doctor`` reports the anchor source, the deadline and the
  days remaining (its dedicated tests live in test_doctor.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.adaptive.mailbox_db import PostgresMailbox
from forge.adaptive.models import ControlCommand
from forge.api_lane_control import (
    LANE_LEGACY_TOKEN_DEADLINE_ENV,
    LEGACY_CREDENTIAL_ANCHOR_FILE_ENV,
    LEGACY_CREDENTIAL_DEADLINE_ENV,
    LegacyWindow,
    LegacyWindowInvalid,
    lane_control_token,
    resolve_legacy_window,
)
from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import FlowRun
from forge.main import create_app

SECRET = "lane-secret"  # noqa: S105 — fake value for tests
WORK = "run-q35-06"

_NOW = datetime.now(timezone.utc)


def _anchor_env(path: Path) -> dict[str, str]:
    return {LEGACY_CREDENTIAL_ANCHOR_FILE_ENV: str(path)}


def _fresh_resolution(env_extra: dict[str, str], cwd: Path) -> dict[str, str | None]:
    """A FRESH process resolves the window from the recorded state alone.

    The subprocess inherits nothing forge-related from this process's
    environment — only PATH/HOME plus the recorded state under test —
    and runs with *cwd* as its working directory, so a stray
    default-path anchor would land somewhere visible instead of the
    checkout.
    """
    script = (
        "import json\n"
        "from forge.api_lane_control import resolve_legacy_window\n"
        "window = resolve_legacy_window()\n"
        "print(json.dumps({\n"
        "    'source': window.source,\n"
        "    'anchor': window.anchor.isoformat() if window.anchor else None,\n"
        "    'deadline': window.deadline.isoformat() if window.deadline else None,\n"
        "}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(cwd),
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), **env_extra},
    )
    return json.loads(completed.stdout.strip())


def _write_anchor(path: Path, instant: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(instant.isoformat(), encoding="utf-8")


# -- the shared app surface ------------------------------------------------------


def _lane_settings(tmp_path: Path) -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/legacy-deadline.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        FORGE_LANE_CONTROL_SECRET=SecretStr(SECRET),
    )


async def _put_run(app, work_id: str, *, generation: int) -> None:
    async with app.state.session_factory() as session:
        session.add(
            FlowRun(id=work_id, project_id=1, provider="github", cancellation_generation=generation)
        )
        await session.commit()


def _legacy_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {lane_control_token(SECRET, WORK)}"}


def _generation_headers(generation: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {lane_control_token(SECRET, WORK, generation=generation)}"}


async def _client_for(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _cmd(seq: int) -> ControlCommand:
    return ControlCommand.model_validate(
        {
            "schema": "forge.proposal.control-command/1",
            "command_id": f"cmd-{WORK}-{seq}",
            "work_id": WORK,
            "sequence": seq,
            "kind": "steer",
            "actor_ref": "human:op",
            "actor_origin": "server_authenticated_human",
            "idempotency_key": f"key-{WORK}-{seq}",
            "status": "received",
            "payload": {"run_id": WORK, "text": f"steer number {seq}"},
        }
    )


# -- 1. explicit deadline: restart-stable across fresh resolutions ----------------


class TestExplicitDeadlineRestartStability:
    def test_two_fresh_resolutions_agree_on_an_explicit_deadline(self, tmp_path):
        deadline = _NOW + timedelta(days=5)
        env = {LEGACY_CREDENTIAL_DEADLINE_ENV: deadline.isoformat()}

        here = resolve_legacy_window(env)
        fresh = _fresh_resolution(env, tmp_path)

        assert here.source == fresh["source"] == "explicit"
        assert here.deadline == datetime.fromisoformat(str(fresh["deadline"]))
        # The explicit branch never touches the persisted anchor...
        assert here.anchor_file is None
        # ...and the fresh process wrote nothing to its working directory.
        assert list(tmp_path.iterdir()) == []

    def test_processes_on_opposite_sides_of_the_deadline_agree(self, tmp_path):
        """The acceptance shape: a deadline already in the past is VALID
        configuration — both a process started before it and one started
        after it derive the same closed window (a fixed instant, reached)."""
        env = {LEGACY_CREDENTIAL_DEADLINE_ENV: "2020-01-01T00:00:00+00:00"}

        here = resolve_legacy_window(env)
        fresh = _fresh_resolution(env, tmp_path)

        assert here.deadline == datetime.fromisoformat(str(fresh["deadline"]))
        assert here.open_at(datetime.now(timezone.utc)) is False
        assert datetime.fromisoformat(str(fresh["deadline"])) < datetime.now(timezone.utc)

    def test_the_legacy_spelling_is_still_an_honored_explicit_deadline(self):
        window = resolve_legacy_window({LANE_LEGACY_TOKEN_DEADLINE_ENV: "2030-01-01"})

        assert window.source == "explicit"
        assert window.deadline == datetime(2030, 1, 1, tzinfo=timezone.utc)

    def test_the_canonical_spelling_wins_when_both_are_set(self):
        both = {
            LEGACY_CREDENTIAL_DEADLINE_ENV: "2031-01-01",
            LANE_LEGACY_TOKEN_DEADLINE_ENV: "2030-01-01",
        }

        window = resolve_legacy_window(both)

        assert window.deadline == datetime(2031, 1, 1, tzinfo=timezone.utc)


# -- 2. the persisted write-once anchor -------------------------------------------


class TestPersistedAnchorWriteOnce:
    def test_the_first_resolution_creates_the_file_and_the_second_reads_it(self, tmp_path):
        anchor = tmp_path / "state" / "lane-legacy-credential-anchor"

        first = resolve_legacy_window(_anchor_env(anchor))
        second = resolve_legacy_window(_anchor_env(anchor))

        assert anchor.is_file()
        assert first.source == second.source == "persisted-file"
        assert first.anchor == second.anchor  # SAME anchor, never a fresh one
        assert first.deadline == second.deadline == first.anchor + timedelta(days=30)
        assert anchor.read_text(encoding="utf-8").strip() == first.anchor.isoformat()

    def test_the_window_never_extends_across_resolutions(self, tmp_path):
        """The Q35-06 core against the old behavior: resolving again must not
        move the deadline 30 days into the future — the file, not the
        resolving process, anchors the window."""
        anchor = tmp_path / "anchor"
        first = resolve_legacy_window(_anchor_env(anchor))

        second = resolve_legacy_window(_anchor_env(anchor))
        third = resolve_legacy_window(_anchor_env(anchor))

        assert second.deadline == first.deadline
        assert third.deadline == first.deadline

    def test_a_fresh_process_agrees_when_the_anchor_file_persists(self, tmp_path):
        anchor = tmp_path / "anchor"
        here = resolve_legacy_window(_anchor_env(anchor))  # the write-once creation

        restarted = _fresh_resolution(_anchor_env(anchor), tmp_path)

        assert restarted["source"] == "persisted-file"
        assert restarted["anchor"] == here.anchor.isoformat()
        assert restarted["deadline"] == here.deadline.isoformat()

    def test_an_existing_file_is_the_anchor_forever(self, tmp_path):
        """Pre-existing deployment state (here: a start recorded 10 days ago)
        is THE anchor — the resolution never rewrites it with a fresh now."""
        recorded = _NOW - timedelta(days=10)
        anchor = tmp_path / "anchor"
        _write_anchor(anchor, recorded)

        window = resolve_legacy_window(_anchor_env(anchor))

        assert window.anchor == recorded
        assert window.deadline == recorded + timedelta(days=30)
        assert window.deadline < _NOW + timedelta(days=21)  # not now + 30
        assert anchor.read_text(encoding="utf-8").strip() == recorded.isoformat()

    def test_the_default_anchor_lives_under_the_checkpoint_store_dir(self, tmp_path, monkeypatch):
        from forge.api_checkpoint_channel import CHECKPOINT_STORE_DIR_ENV

        monkeypatch.setenv(CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "checkpoints"))

        window = resolve_legacy_window()

        expected = tmp_path / "checkpoints" / "lane-legacy-credential-anchor"
        assert window.source == "persisted-file"
        assert window.anchor_file == expected
        assert expected.is_file()

    def test_a_corrupt_anchor_file_refuses_rather_than_rewriting(self, tmp_path):
        """Machine state nobody can parse is a fail-closed refusal — writing
        a fresh anchor over corrupt state is precisely the restart hole
        this issue closes."""
        anchor = tmp_path / "anchor"
        anchor.write_text("not an instant", encoding="utf-8")

        window = resolve_legacy_window(_anchor_env(anchor))

        assert window.refused
        assert "malformed" in window.diagnostic
        assert str(anchor) in window.diagnostic
        assert anchor.read_text(encoding="utf-8") == "not an instant"  # never rewritten


# -- 3. restart after expiry cannot revive a legacy token -------------------------


class TestRestartAfterExpiry:
    @pytest.fixture()
    async def app(self, tmp_path, monkeypatch):
        """The control plane against deployment state whose migration window
        EXPIRED 1 day ago (anchor persisted 31 days ago, before the app)."""
        anchor = tmp_path / "lane-legacy-credential-anchor"
        _write_anchor(anchor, datetime.now(timezone.utc) - timedelta(days=31))
        monkeypatch.setenv(LEGACY_CREDENTIAL_ANCHOR_FILE_ENV, str(anchor))
        reset_engine()
        application = create_app(settings=_lane_settings(tmp_path))
        async with application.router.lifespan_context(application):
            await _put_run(application, WORK, generation=2)
            yield application
        reset_engine()

    async def test_the_legacy_token_is_refused_and_a_restart_cannot_revive_it(
        self, app, tmp_path, monkeypatch
    ):
        async with await _client_for(app) as client:
            first = await client.get(
                "/lane/controls", params={"work_id": WORK}, headers=_legacy_headers()
            )
        assert first.status_code == 403
        assert "migration deadline" in first.json()["detail"]

        # The "restarted" process: a FRESH resolution over the SAME
        # persisted state derives the SAME past deadline — no new window.
        restarted = _fresh_resolution(
            {LEGACY_CREDENTIAL_ANCHOR_FILE_ENV: os.environ[LEGACY_CREDENTIAL_ANCHOR_FILE_ENV]},
            tmp_path,
        )
        assert restarted["source"] == "persisted-file"
        assert datetime.fromisoformat(str(restarted["deadline"])) < datetime.now(timezone.utc)

        # And a SECOND control-plane instance (a real re-composition reading
        # the same anchor) refuses exactly the same way.
        restart_state = tmp_path / "restart"
        (restart_state / "db").mkdir(parents=True)
        (restart_state / "anchor").write_text(
            (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(), encoding="utf-8"
        )
        monkeypatch.setenv(LEGACY_CREDENTIAL_ANCHOR_FILE_ENV, str(restart_state / "anchor"))
        reset_engine()
        second_app = create_app(settings=_lane_settings(restart_state / "db"))
        async with second_app.router.lifespan_context(second_app):
            await _put_run(second_app, WORK, generation=2)
            async with await _client_for(second_app) as client:
                again = await client.get(
                    "/lane/controls", params={"work_id": WORK}, headers=_legacy_headers()
                )
        assert again.status_code == 403
        assert "migration deadline" in again.json()["detail"]

    async def test_generation_scoped_tokens_survive_the_expiry(self, app):
        async with await _client_for(app) as client:
            pending = await client.get(
                "/lane/controls", params={"work_id": WORK}, headers=_generation_headers(2)
            )
        assert pending.status_code == 200
        assert pending.json()["commands"] == []


# -- 4. malformed explicit configuration: typed, specific, separate -----------------


class TestInvalidConfiguration:
    def test_a_malformed_deadline_is_a_typed_failure(self):
        with pytest.raises(LegacyWindowInvalid, match=LEGACY_CREDENTIAL_DEADLINE_ENV) as raised:
            resolve_legacy_window({LEGACY_CREDENTIAL_DEADLINE_ENV: "soon"})
        assert "'soon'" in str(raised.value)
        assert "not an ISO date/datetime" in str(raised.value)

    def test_an_empty_deadline_is_a_typed_failure(self):
        with pytest.raises(LegacyWindowInvalid, match="empty"):
            resolve_legacy_window({LEGACY_CREDENTIAL_DEADLINE_ENV: ""})

    def test_a_whitespace_deadline_is_a_typed_failure(self):
        with pytest.raises(LegacyWindowInvalid, match="empty"):
            resolve_legacy_window({LEGACY_CREDENTIAL_DEADLINE_ENV: "   "})

    def test_a_deadline_before_the_sanity_floor_is_a_typed_failure(self):
        with pytest.raises(LegacyWindowInvalid, match="sanity bounds"):
            resolve_legacy_window({LEGACY_CREDENTIAL_DEADLINE_ENV: "1900-01-01"})

    def test_a_deadline_after_the_sanity_ceiling_is_a_typed_failure(self):
        with pytest.raises(LegacyWindowInvalid, match="sanity bounds"):
            resolve_legacy_window({LEGACY_CREDENTIAL_DEADLINE_ENV: "2500-01-01"})

    def test_a_malformed_legacy_spelling_is_a_typed_failure(self):
        with pytest.raises(LegacyWindowInvalid, match=LANE_LEGACY_TOKEN_DEADLINE_ENV):
            resolve_legacy_window({LANE_LEGACY_TOKEN_DEADLINE_ENV: "next quarter"})

    def test_a_malformed_recorded_start_is_a_typed_failure(self):
        from forge.api_lane_control import LANE_LEGACY_TOKEN_START_ENV

        with pytest.raises(LegacyWindowInvalid, match=LANE_LEGACY_TOKEN_START_ENV):
            resolve_legacy_window({LANE_LEGACY_TOKEN_START_ENV: "a while ago"})

    def test_missing_configuration_is_not_a_failure_it_persists_an_anchor(self, tmp_path):
        """UNSET is deliberately not invalid — it falls through to the
        write-once anchor (the deployment pins its own start); only SET
        values that cannot be honored raise."""
        window = resolve_legacy_window(_anchor_env(tmp_path / "anchor"))

        assert not window.refused
        assert window.source == "persisted-file"

    async def test_invalid_configuration_refuses_on_the_wire_not_a_500(self, tmp_path, monkeypatch):
        """A typo'd env must fail CLOSED and SPECIFICALLY: the legacy token
        gets the diagnostic as a refusal, the generation token is
        unaffected, and nothing ever re-anchors onto a fresh window."""
        monkeypatch.setenv(LEGACY_CREDENTIAL_DEADLINE_ENV, "soon")
        reset_engine()
        application = create_app(settings=_lane_settings(tmp_path))
        async with application.router.lifespan_context(application):
            await _put_run(application, WORK, generation=2)
            async with await _client_for(application) as client:
                legacy = await client.get(
                    "/lane/controls", params={"work_id": WORK}, headers=_legacy_headers()
                )
                scoped = await client.get(
                    "/lane/controls", params={"work_id": WORK}, headers=_generation_headers(2)
                )
        reset_engine()

        assert legacy.status_code == 403  # refused with the diagnostic...
        assert LEGACY_CREDENTIAL_DEADLINE_ENV in legacy.json()["detail"]
        assert "'soon'" in legacy.json()["detail"]
        assert scoped.status_code == 200  # ...while generation auth is unaffected


# -- 5. unwritable anchor + no explicit configuration: fail closed -----------------


class TestUnwritableAnchorFailsClosed:
    @pytest.fixture()
    async def app(self, tmp_path, monkeypatch):
        """No explicit configuration and an anchor path that cannot exist:
        its parent IS a regular file."""
        blocker = tmp_path / "blocker"
        blocker.write_text("a regular file, not a directory", encoding="utf-8")
        monkeypatch.setenv(LEGACY_CREDENTIAL_ANCHOR_FILE_ENV, str(blocker / "anchor"))
        reset_engine()
        application = create_app(settings=_lane_settings(tmp_path))
        async with application.router.lifespan_context(application):
            await _put_run(application, WORK, generation=2)
            yield application
        reset_engine()

    def test_resolution_is_refused_with_the_specific_diagnostic(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("a regular file, not a directory", encoding="utf-8")

        window = resolve_legacy_window(_anchor_env(blocker / "anchor"))

        assert isinstance(window, LegacyWindow)
        assert window.refused
        assert window.source == "refused"
        assert str(blocker / "anchor") in window.diagnostic
        assert "refused rather than re-anchored" in window.diagnostic
        assert LEGACY_CREDENTIAL_DEADLINE_ENV in window.diagnostic  # the fix is named

    async def test_legacy_tokens_are_refused_generation_tokens_work(self, app):
        async with await _client_for(app) as client:
            legacy = await client.get(
                "/lane/controls", params={"work_id": WORK}, headers=_legacy_headers()
            )
            scoped = await client.get(
                "/lane/controls", params={"work_id": WORK}, headers=_generation_headers(2)
            )

        assert legacy.status_code == 403
        assert "refused rather than re-anchored" in legacy.json()["detail"]
        assert scoped.status_code == 200

    async def test_the_same_refusal_reaches_the_ack_surface(self, app):
        mailbox = PostgresMailbox(app.state.session_factory)
        await mailbox.submit(_cmd(1))
        async with await _client_for(app) as client:
            ack = await client.post(
                f"/lane/controls/cmd-{WORK}-1/ack",
                json={"state": "authorized"},
                headers=_legacy_headers(),
            )
        assert ack.status_code == 403
        assert "refused rather than re-anchored" in ack.json()["detail"]
