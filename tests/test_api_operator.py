"""The operator read API (R36-15, R37-02/R37-03) — auth, scoping,
honesty, zero writes.

The authenticated, subject-scoped projection surface mounted in the app:
list, detail and support-bundle over the SAME snapshot reader, fail-closed
like every adaptive route. Pinned here:

- the credential family: the lane-control HMAC under
  ``FORGE_LANE_CONTROL_SECRET`` signing the CANONICAL SUBJECT scope
  (v2) — a token minted for one canonical subject cannot declare
  another's scope (403 cross-scope) and a run outside the verified
  scope is a 404, indistinguishable from unknown, on every path;
- the ladder: no secret → 503, no bearer → 401, no declared scope → 400,
  token/scope mismatch → 403, both grant families declared → 400;
- the shapes: list summaries (subject ids, page bound, cursor), the full
  detail render (coverage, age, occupancy, source-version fence,
  ADVISORY observer actions), and the bundle document with failed
  attempts preserved and explicit coverage;
- redaction: credential-looking values never reach an operator;
- the read-only charter: rendering performs zero writes — the recording
  checkpoint authority sees only ``entry``, the durable row counts do
  not move across GETs, and an operator bearer cannot drive a guarded
  control route;
- stale actions are hints only: replaying one through the REAL guarded
  command route (the lane-control ack's CTL-04 CAS) refuses against the
  current world, and ``RecoveryActions.decide`` names the current state
  and the safe alternative;
- parity: ``status_note_lines`` (the native-comment lines) and the API
  render agree on state and identity for the same snapshot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import json

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select

from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox
from forge.adaptive.models import ControlCommand
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.operator_view import RecoveryActions, status_note_lines
from forge.adaptive.pause_fence import PauseFenceRow
from forge.api_lane_control import lane_control_token
from forge.api_operator import operator_subject_scope_token
from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import ActionLog, FlowRun, PublicationIntent
from forge.main import create_app

SECRET = "operator-secret"  # noqa: S105 — fake value for tests
NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
REPO_A = "owner/alpha"
REPO_B = "owner/beta"
RUN_A = "a" * 32
RUN_B = "b" * 32
SUBJECT_A = CanonicalSubject(provider_family="github", connection="", native_id=REPO_A)
SUBJECT_B = CanonicalSubject(provider_family="github", connection="", native_id=REPO_B)
REF_A = SUBJECT_A.subject_id()
REF_B = SUBJECT_B.subject_id()


class RecordingRepository:
    """The injected checkpoint authority — recording every call so the
    zero-side-effect assertion can see writes never happen."""

    def __init__(self, entry: dict | None = None) -> None:
        self.entry_document = entry
        self.calls: list[str] = []

    async def entry(self, work_id: str) -> dict | None:
        self.calls.append("entry")
        return dict(self.entry_document) if self.entry_document else None

    async def put(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003 — recorder
        self.calls.append("put")

    async def read(self, work_id: str):
        self.calls.append("read")
        return None

    async def lookup_outcome(self, work_id: str):
        self.calls.append("lookup_outcome")

    async def authority(self) -> str:
        return "recording"

    async def pin(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003 — recorder
        self.calls.append("pin")
        return False

    async def unpin(self, *args, **kwargs) -> int:  # noqa: ANN002, ANN003 — recorder
        self.calls.append("unpin")
        return 0

    async def pins(self, work_id: str | None = None) -> list[dict]:
        self.calls.append("pins")
        return []


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/operator.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        FORGE_LANE_CONTROL_SECRET=SecretStr(SECRET),
    )
    values.update(overrides)
    return Settings(**values)


def _run(run_id: str, repo: str, **over) -> FlowRun:
    values: dict = {
        "id": run_id,
        "project_id": 1,
        "provider": "github",
        "github_repo_full_name": repo,
        "status": "validating",
        "base_sha": "b" * 40,
        "candidate_shas": [],
        "plan_digest": "p" * 64,
        "evidence": {},
        "created_at": NOW - timedelta(hours=3),
        "updated_at": NOW - timedelta(minutes=10),
    }
    values.update(over)
    return FlowRun(**values)


def _steer_command(run_id: str, seq: int = 1, **over) -> ControlCommand:
    base = {
        "schema": "forge.proposal.control-command/1",
        "command_id": f"cmd-{run_id[:6]}-{seq}",
        "work_id": run_id,
        "sequence": seq,
        "kind": "steer",
        "actor_ref": "human:op",
        "actor_origin": "server_authenticated_human",
        "idempotency_key": f"key-{run_id[:6]}-{seq}",
        "status": "received",
        "payload": {"run_id": run_id, "text": "use the other approach"},
        "expected_plan_revision": 3,
        "expected_execution_epoch": 0,
    }
    base.update(over)
    return ControlCommand.model_validate(base)


def _scope_headers(subjects: list[CanonicalSubject]) -> dict[str, str]:
    return {"Authorization": f"Bearer {operator_subject_scope_token(SECRET, subjects)}"}


def _subject_query(subjects: list[CanonicalSubject]) -> str:
    return "&".join(f"subject={entry.subject_id()}" for entry in subjects)


@pytest.fixture()
async def app(tmp_path):
    reset_engine()
    application = create_app(settings=_settings(tmp_path))
    async with application.router.lifespan_context(application):
        yield application
    reset_engine()


@pytest.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://forge.test") as http:
        yield http


@pytest.fixture()
def repository(app) -> RecordingRepository:
    recording = RecordingRepository(
        entry={
            "checkpoint_id": "e" * 64,
            "sequence": 2,
            "files": 1,
            "uploaded_at": (NOW - timedelta(minutes=15)).isoformat(),
        }
    )
    app.state.operator_checkpoint_repository = recording
    return recording


async def _seed(app, *rows) -> None:
    session_factory = app.state.session_factory
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()


async def _row_count(app, model) -> int:
    async with app.state.session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# ---------------------------------------------------------------------------
# The fail-closed ladder and the canonical subject scope
# ---------------------------------------------------------------------------


async def test_no_secret_means_503_never_open(tmp_path):
    """An app without the lane secret: the surface is mounted but fails
    closed, exactly like the lane-control routes (503, never open)."""
    bare = create_app(settings=_settings(tmp_path, FORGE_LANE_CONTROL_SECRET=None))
    transport = ASGITransport(app=bare)
    async with AsyncClient(transport=transport, base_url="http://forge.test") as http:
        response = await http.get(
            f"/operator/runs?{_subject_query([SUBJECT_A])}", headers=_scope_headers([SUBJECT_A])
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "operator endpoint disabled"


async def test_missing_bearer_is_401(client):
    response = await client.get(f"/operator/runs?{_subject_query([SUBJECT_A])}")
    assert response.status_code == 401


async def test_no_declared_scope_is_400(client):
    response = await client.get("/operator/runs", headers=_scope_headers([SUBJECT_A]))
    assert response.status_code == 400


async def test_a_malformed_subject_reference_is_400(client):
    headers = _scope_headers([SUBJECT_A])
    response = await client.get("/operator/runs?subject=not-a-subject", headers=headers)
    assert response.status_code == 400
    assert "canonical subject" in response.json()["detail"]


async def test_declaring_both_grant_families_is_400(client):
    response = await client.get(
        f"/operator/runs?{_subject_query([SUBJECT_A])}&repo={REPO_A}",
        headers=_scope_headers([SUBJECT_A]),
    )
    assert response.status_code == 400


async def test_a_token_for_alpha_cannot_declare_betas_scope(client):
    response = await client.get(
        f"/operator/runs?{_subject_query([SUBJECT_B])}", headers=_scope_headers([SUBJECT_A])
    )
    assert response.status_code == 403
    wider = await client.get(
        f"/operator/runs?{_subject_query([SUBJECT_A, SUBJECT_B])}",
        headers=_scope_headers([SUBJECT_A]),
    )
    assert wider.status_code == 403


async def test_a_legacy_name_token_is_not_a_canonical_subject_token(client):
    """A v1 name-only bearer presented against a canonical declaration
    does not verify — the grant must be reissued as canonical subjects."""
    from forge.api_operator import operator_scope_token

    legacy = {"Authorization": f"Bearer {operator_scope_token(SECRET, [REPO_A])}"}
    response = await client.get(f"/operator/runs?{_subject_query([SUBJECT_A])}", headers=legacy)
    assert response.status_code == 403


async def test_list_detail_and_bundle_are_all_subject_scoped(app, client, repository):
    await _seed(app, _run(RUN_A, REPO_A), _run(RUN_B, REPO_B))
    headers = _scope_headers([SUBJECT_A])

    listed = await client.get(f"/operator/runs?{_subject_query([SUBJECT_A])}", headers=headers)
    assert listed.status_code == 200
    assert [run["run_id"] for run in listed.json()["runs"]] == [RUN_A]

    detail = await client.get(
        f"/operator/runs/{RUN_B}?{_subject_query([SUBJECT_A])}", headers=headers
    )
    assert detail.status_code == 404
    bundle = await client.get(
        f"/operator/runs/{RUN_B}/support-bundle?{_subject_query([SUBJECT_A])}", headers=headers
    )
    assert bundle.status_code == 404

    # the same bearer reads its OWN run on every surface
    own_detail = await client.get(
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}", headers=headers
    )
    assert own_detail.status_code == 200
    own_bundle = await client.get(
        f"/operator/runs/{RUN_A}/support-bundle?{_subject_query([SUBJECT_A])}", headers=headers
    )
    assert own_bundle.status_code == 200


# ---------------------------------------------------------------------------
# The endpoint shapes
# ---------------------------------------------------------------------------


async def test_the_list_renders_thin_state_summaries(app, client, repository):
    await _seed(
        app,
        _run(RUN_A, REPO_A, status_reason="waiting on the CI lane"),
        _run(RUN_B, REPO_B),
    )

    response = await client.get(
        f"/operator/runs?{_subject_query([SUBJECT_A, SUBJECT_B])}",
        headers=_scope_headers([SUBJECT_A, SUBJECT_B]),
    )

    assert response.status_code == 200
    document = response.json()
    assert document["scope"] == [REF_A, REF_B]
    assert document["scope_version"] == 2
    assert document["page_size"] >= 1
    by_id = {run["run_id"]: run for run in document["runs"]}
    assert set(by_id) == {RUN_A, RUN_B}
    thin = by_id[RUN_A]
    for key in (
        "run_id",
        "subject",
        "subject_id",
        "state",
        "underlying_state",
        "blocked_reason",
        "waiting_on",
        "summary",
        "updated_at",
        "projection_age_seconds",
        "projection_inconsistent",
        "unresolved_effects",
    ):
        assert key in thin, key
    assert thin["subject"] == REPO_A  # the display spelling
    assert thin["subject_id"] == REF_A  # the canonical one
    assert thin["blocked_reason"] == "waiting on the CI lane"
    assert thin["projection_inconsistent"] is False


async def test_the_detail_renders_the_projection_contract(app, client, repository):
    await _seed(
        app,
        _run(RUN_A, REPO_A, candidate_shas=["c" * 40]),
        ActionLog(
            flow_run_id=RUN_A,
            action_kind="retry_requested",
            status="succeeded",
            retryability="transient_infrastructure",
            dispatch_state="dispatched",
            created_at=NOW - timedelta(minutes=30),
        ),
    )

    response = await client.get(
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}", headers=_scope_headers([SUBJECT_A])
    )

    assert response.status_code == 200
    document = response.json()
    assert document["schema"] == "forge.operator.view/1"
    assert document["run_id"] == RUN_A
    assert document["subject"] == REPO_A
    assert document["subject_id"] == REF_A
    assert document["state"] == "unverified"  # candidate, no verification record
    assert document["identity"]["candidate_shas"] == ["c" * 40]
    assert document["source_coverage"]["run"] == "present"
    assert document["source_coverage"]["attempts"] == "present"
    assert document["source_coverage"]["questions"] == "unknown"
    assert isinstance(document["projection_age_seconds"], float)
    assert document["source_version"]  # the consistency fence is recorded
    assert document["projection_inconsistent"] is False
    # the read-only actor's hints: observer role, probe only, advisory
    actions = document["actions"]
    assert actions and {action["action"] for action in actions} == {"probe"}
    assert all(action["via"] == "read-only:/status" for action in actions)
    assert "hints" in document["actions_advisory"]


async def test_the_bundle_carries_failed_attempts_and_explicit_coverage(app, client, repository):
    await _seed(
        app,
        _run(RUN_A, REPO_A, status="failed", candidate_shas=[]),
        ActionLog(
            flow_run_id=RUN_A,
            action_kind="retry_requested",
            status="failed",
            retryability="transient_infrastructure",
            dispatch_state="dispatched",
            created_at=NOW - timedelta(minutes=45),
        ),
        ActionLog(
            flow_run_id=RUN_A,
            action_kind="auto_revive",
            status="succeeded",
            retryability="operator_override",
            dispatch_state="dispatched",
            created_at=NOW - timedelta(minutes=25),
        ),
    )

    response = await client.get(
        f"/operator/runs/{RUN_A}/support-bundle?{_subject_query([SUBJECT_A])}",
        headers=_scope_headers([SUBJECT_A]),
    )

    assert response.status_code == 200
    bundle = response.json()
    assert bundle["schema"] == "forge.support.bundle/1"
    assert bundle["run_id"] == RUN_A
    assert bundle["digest"].startswith("sha256:")
    outcomes = [attempt["outcome"] for attempt in bundle["attempts"]]
    # the initial execution (failed terminal) + both revival rows — history
    # preserved, order kept, failed attempts included
    assert outcomes == ["failed", "failed", "succeeded"]
    assert bundle["coverage"]["attempts"] == "present"
    assert bundle["coverage"]["checkpoints"] == "present"
    assert bundle["coverage"]["questions"] == "unknown"
    assert bundle["projection"]["run_id"] == RUN_A
    assert bundle["subject_id"] == REF_A
    assert bundle["projection_inconsistent"] is False


async def test_at04_the_http_render_matches_the_reader_projection(app, client, repository):
    """AT-04 (HTTP presentation arm): the current identities the reader
    derived — CP-B unactivated, B the unverified current candidate, A's
    pass historical — are what the API renders, on detail and bundle."""
    cp_a, cp_b = "a" * 64, "b" * 64
    cand_a, cand_b = "a" * 40, "b" * 40
    await _seed(
        app,
        _run(
            RUN_A,
            REPO_A,
            status="waiting_ci",
            candidate_shas=[cand_a, cand_b],
            evidence={
                "verification": {
                    "status": "passed",
                    "tested_oid": cand_a,
                    "observed_at": (NOW - timedelta(minutes=6)).isoformat(),
                    "producer": "github-checks",
                }
            },
        ),
        ActionLog(
            flow_run_id=RUN_A,
            action_kind="retry_requested",
            status="succeeded",
            retryability="transient_infrastructure",
            dispatch_state="dispatched",
            remote_result={"generation": 1},
            created_at=NOW - timedelta(minutes=12),
        ),
        ControlCommandRow(
            id=f"cmd-{RUN_A[:6]}-1",
            work_id=RUN_A,
            run_id=RUN_A,
            kind="pause",
            payload={"run_id": RUN_A},
            status="checkpointed",
            sequence=1,
            dedup_key=f"key-{RUN_A[:6]}-1",
            actor_ref="human:op",
            actor_origin="server_authenticated_human",
            created_at=NOW - timedelta(minutes=20),
        ),
        ControlCommandRow(
            id=f"cmd-{RUN_A[:6]}-2",
            work_id=RUN_A,
            run_id=RUN_A,
            kind="resume",
            payload={"run_id": RUN_A, "checkpoint_ref": f"{RUN_A}@{cp_a}"},
            status="applied",
            sequence=2,
            dedup_key=f"key-{RUN_A[:6]}-2",
            actor_ref="human:op",
            actor_origin="server_authenticated_human",
            created_at=NOW - timedelta(minutes=90),
            applied_at=NOW - timedelta(minutes=89),
        ),
        PauseFenceRow(
            work_id=RUN_A,
            publication_epoch_bumped=2,
            fenced_at=NOW - timedelta(hours=2),
            cleared_at=NOW - timedelta(minutes=89),
        ),
    )
    repository.entry_document = {
        "checkpoint_id": cp_b,  # uploaded AFTER the CP-A resume applied
        "sequence": 5,
        "files": 3,
        "uploaded_at": (NOW - timedelta(minutes=10)).isoformat(),
    }
    headers = _scope_headers([SUBJECT_A])
    query = _subject_query([SUBJECT_A])

    detail = (await client.get(f"/operator/runs/{RUN_A}?{query}", headers=headers)).json()
    assert detail["state"] == "unverified"  # B current, only A passed
    assert detail["identity"]["active_candidate"] == cand_b
    assert [entry["verdict"] for entry in detail["verification_history"]] == ["historical_pass"]

    bundle = (
        await client.get(f"/operator/runs/{RUN_A}/support-bundle?{query}", headers=headers)
    ).json()
    assert bundle["projection"]["state"] == detail["state"]
    assert bundle["projection"]["identity"]["active_candidate"] == cand_b
    assert bundle["checkpoints"][0]["activated_at"] == ""  # no borrowed activation
    assert bundle["verifications"][0]["candidate_sha"] == cand_a


# ---------------------------------------------------------------------------
# Redaction — credential material never reaches an operator
# ---------------------------------------------------------------------------


async def test_credential_looking_values_are_redacted_on_every_surface(app, client, repository):
    await _seed(
        app,
        _run(
            RUN_A,
            REPO_A,
            status_reason="pipeline failed: Bearer ghp_aaaaaaaaaaaaaaaaaaaa rejected",
        ),
    )
    headers = _scope_headers([SUBJECT_A])
    query = _subject_query([SUBJECT_A])

    detail = (await client.get(f"/operator/runs/{RUN_A}?{query}", headers=headers)).json()
    assert "ghp_aaaaaaaaaaaaaaaaaaaa" not in str(detail)
    assert detail["blocked_reason"] == "[redacted]"

    bundle = (
        await client.get(f"/operator/runs/{RUN_A}/support-bundle?{query}", headers=headers)
    ).json()
    assert "ghp_aaaaaaaaaaaaaaaaaaaa" not in str(bundle)


# ---------------------------------------------------------------------------
# The read-only charter — zero writes during rendering
# ---------------------------------------------------------------------------


async def test_rendering_performs_zero_writes(app, client, repository):
    await _seed(
        app,
        _run(RUN_A, REPO_A),
        _run(RUN_B, REPO_B),
        _steer_command_row(RUN_A),
    )  # a command row exists so the read has something honest to show
    headers = _scope_headers([SUBJECT_A])
    before = {model: await _row_count(app, model) for model in (FlowRun, ActionLog)}
    before[ControlCommandRow] = await _row_count(app, ControlCommandRow)

    for path in (
        f"/operator/runs?{_subject_query([SUBJECT_A])}",
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}",
        f"/operator/runs/{RUN_A}/support-bundle?{_subject_query([SUBJECT_A])}",
    ):
        response = await client.get(path, headers=headers)
        assert response.status_code == 200, path

    for model, count in before.items():
        assert await _row_count(app, model) == count, model.__name__
    assert set(repository.calls) == {"entry"}  # reads only through the authority


async def test_an_operator_bearer_cannot_drive_a_guarded_control_route(app, client, repository):
    """The read-only token family carries NO control privileges: the
    lane-control ack route (a real guarded mutation path) refuses the
    operator bearer outright — it is not a lane token."""
    await _seed(app, _run(RUN_A, REPO_A))
    mailbox = PostgresMailbox(app.state.session_factory)
    command, _ = await mailbox.submit(_steer_command(RUN_A))

    response = await client.post(
        f"/lane/controls/{command.command_id}/ack",
        json={"state": "authorized"},
        headers=_scope_headers([SUBJECT_A]),  # the operator bearer, not a lane token
    )
    assert response.status_code == 403
    async with app.state.session_factory() as session:
        row = await session.scalar(
            select(ControlCommandRow).where(ControlCommandRow.id == command.command_id)
        )
    assert row is not None and row.status == "received"  # nothing moved


def _steer_command_row(run_id: str, seq: int = 1) -> ControlCommandRow:
    return ControlCommandRow(
        id=f"cmd-{run_id[:6]}-{seq}",
        work_id=run_id,
        run_id=run_id,
        kind="steer",
        payload={"run_id": run_id, "text": "use the other approach"},
        status="received",
        sequence=seq,
        dedup_key=f"key-{run_id[:6]}-{seq}",
        actor_ref="human:op",
        actor_origin="server_authenticated_human",
        expected_plan_revision=3,
        expected_execution_epoch=0,
        created_at=NOW - timedelta(minutes=20),
    )


def _pause_command_row(run_id: str, seq: int = 1) -> ControlCommandRow:
    """A pause command landed through the booking (``checkpointed``)."""
    return ControlCommandRow(
        id=f"cmd-{run_id[:6]}-{seq}",
        work_id=run_id,
        run_id=run_id,
        kind="pause",
        payload={"run_id": run_id},
        status="checkpointed",
        sequence=seq,
        dedup_key=f"key-{run_id[:6]}-{seq}",
        actor_ref="human:op",
        actor_origin="server_authenticated_human",
        created_at=NOW - timedelta(minutes=25),
        applied_at=NOW - timedelta(minutes=24),
    )


# ---------------------------------------------------------------------------
# Stale actions — hints only; the guarded route revalidates the world
# ---------------------------------------------------------------------------


async def test_a_stale_action_is_refused_by_the_real_guarded_route(app, client, repository):
    """The CTL-04 replay: an operator's steer was planned against the world
    their OLD status comment showed (plan revision 2); the durable command
    was submitted against the CURRENT world (revision 3). Replaying the
    stale dispatch through the REAL lane-control ack route refuses — the
    command expires, written against a world that no longer exists."""
    await _seed(app, _run(RUN_A, REPO_A))
    mailbox = PostgresMailbox(app.state.session_factory)
    command, _ = await mailbox.submit(_steer_command(RUN_A, expected_plan_revision=3))
    lane_headers = {"Authorization": f"Bearer {lane_control_token(SECRET, RUN_A, generation=0)}"}

    # the lane books authorization through the real route
    authorized = await client.post(
        f"/lane/controls/{command.command_id}/ack",
        json={"state": "authorized"},
        headers=lane_headers,
    )
    assert authorized.status_code == 200

    # the STALE replay: dispatching against the old status comment's world
    stale = await client.post(
        f"/lane/controls/{command.command_id}/ack",
        json={"state": "dispatching", "plan_revision": 2, "execution_epoch": 0},
        headers=lane_headers,
    )
    assert stale.status_code == 200  # the ack itself is answered
    assert stale.json()["state"] == "expired"  # but the action is REFUSED
    async with app.state.session_factory() as session:
        row = await session.scalar(
            select(ControlCommandRow).where(ControlCommandRow.id == command.command_id)
        )
    assert row is not None and row.status == "expired"
    assert any("stale expectations" in str(entry) for entry in (row.journal or []))

    # and the operator surface shows the current world, not the stale one
    detail = await client.get(
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}", headers=_scope_headers([SUBJECT_A])
    )
    assert detail.status_code == 200
    assert detail.json()["source_coverage"]["commands"] == "present"


async def test_decide_refuses_a_stale_hint_with_the_current_state_and_safe_alternative(
    app, client, repository
):
    """The advisory decision path: an approver's action planned while the
    run was executing is replayed after the world moved to safely_paused —
    refused, with the CURRENT state and the safe next action named."""
    from forge.adaptive.operator_snapshot import OperatorSnapshotReader
    from forge.adaptive.operator_view import derive_state

    await _seed(
        app,
        _run(RUN_A, REPO_A, status="proposing"),
        ActionLog(
            flow_run_id=RUN_A,
            action_kind="retry_requested",
            status="requested",  # executing
            retryability="transient_infrastructure",
            dispatch_state="dispatched",
            created_at=NOW - timedelta(minutes=5),
        ),
    )
    reader = OperatorSnapshotReader(
        app.state.session_factory, checkpoint_repository=repository, clock=lambda: NOW
    )
    executing = await reader.snapshot(RUN_A, [SUBJECT_A])
    assert executing is not None
    assert derive_state(executing.rows, NOW).state == "executing"
    planned = RecoveryActions.plan(executing.projection(), "human:op", "approver")
    assert {action.action for action in planned} == {"pause", "steer", "cancel", "probe"}
    steer = next(action for action in planned if action.action == "steer")

    # the world moves: the pause lands with a real checkpoint and fence
    async with app.state.session_factory() as session:
        session.add(
            ControlCommandRow(
                id=f"cmd-{RUN_A[:6]}-pause",
                work_id=RUN_A,
                run_id=RUN_A,
                kind="pause",
                payload={"run_id": RUN_A},
                status="checkpointed",
                sequence=2,
                dedup_key=f"key-{RUN_A[:6]}-pause",
                actor_ref="human:op",
                actor_origin="server_authenticated_human",
                created_at=NOW - timedelta(minutes=4),
                applied_at=NOW - timedelta(minutes=3),
            )
        )
        session.add(
            PauseFenceRow(
                work_id=RUN_A,
                publication_epoch_bumped=1,
                fenced_at=NOW - timedelta(minutes=4),
            )
        )
        await session.commit()

    paused = await reader.snapshot(RUN_A, [SUBJECT_A])
    assert paused is not None
    current = paused.projection()
    assert current.state == "safely_paused"

    decision = RecoveryActions.decide(steer, current)
    assert decision.allowed is False
    assert decision.current_state == "safely_paused"
    assert decision.safe_next_action == "resume"
    assert "safely_paused" in decision.reason

    # the API's own hints for the read-only actor follow the SAME world
    detail = await client.get(
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}", headers=_scope_headers([SUBJECT_A])
    )
    assert detail.status_code == 200
    assert detail.json()["state"] == "safely_paused"


# ---------------------------------------------------------------------------
# Parity — the native-comment lines and the API render agree
# ---------------------------------------------------------------------------


async def test_native_comment_lines_and_the_api_render_agree(app, client, repository):
    await _seed(
        app,
        _run(RUN_A, REPO_A, candidate_shas=["c" * 40]),
        ActionLog(
            flow_run_id=RUN_A,
            action_kind="retry_requested",
            status="succeeded",
            retryability="transient_infrastructure",
            dispatch_state="dispatched",
            created_at=NOW - timedelta(minutes=30),
        ),
    )

    response = await client.get(
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}", headers=_scope_headers([SUBJECT_A])
    )
    document = response.json()

    from forge.adaptive.operator_snapshot import OperatorSnapshotReader

    reader = OperatorSnapshotReader(
        app.state.session_factory, checkpoint_repository=repository, clock=lambda: NOW
    )
    snapshot = await reader.snapshot(RUN_A, [SUBJECT_A])
    assert snapshot is not None
    lines = status_note_lines(snapshot.projection())

    # SAME state, SAME identities — the native slice is compact, never different
    assert lines[0] == f"Run {RUN_A} is {document['state']}."
    identity_line = next(line for line in lines if line.startswith("Identity:"))
    assert document["identity"]["source_sha"][:12] in identity_line
    assert document["identity"]["plan_digest"][:12] in identity_line
    assert document["identity"]["candidate_shas"][0][:12] in identity_line
    if document["blocked_reason"]:
        assert any(line.startswith("Blocked:") for line in lines)


# ---------------------------------------------------------------------------
# The R38-15 recovery surface — detail section, stale-action display,
# bounded export diagnostics
# ---------------------------------------------------------------------------


async def test_the_detail_renders_the_recovery_section(app, client, repository):
    """The detail route carries the recovery surface: the delivery outcome
    (an empty resumed diff is a FAILED/no-effect delivery), the
    five-milestone ladder (each independent) and the advisory hint naming
    the guarded route — beside the existing projection render."""
    checkpoint = "e" * 64
    await _seed(
        app,
        _run(
            RUN_A,
            REPO_A,
            status="proposing",
            evidence={
                "harness": {
                    "driver_exit": "completed",
                    "collector_exit": 0,
                    "candidate_state": "zero_change",
                }
            },
        ),
        _pause_command_row(RUN_A),
        ControlCommandRow(
            id=f"cmd-{RUN_A[:6]}-2",
            work_id=RUN_A,
            run_id=RUN_A,
            kind="resume",
            payload={"run_id": RUN_A, "checkpoint_ref": f"{RUN_A}@{checkpoint}"},
            status="applied",
            sequence=2,
            dedup_key=f"key-{RUN_A[:6]}-2",
            actor_ref="human:op",
            actor_origin="server_authenticated_human",
            created_at=NOW - timedelta(minutes=20),
            applied_at=NOW - timedelta(minutes=19),
        ),
        PauseFenceRow(
            work_id=RUN_A,
            publication_epoch_bumped=2,
            fenced_at=NOW - timedelta(minutes=25),
            cleared_at=NOW - timedelta(minutes=19),
        ),
    )
    repository.entry_document = {
        "checkpoint_id": checkpoint,
        "sequence": 3,
        "files": 2,
        "uploaded_at": (NOW - timedelta(minutes=24)).isoformat(),
    }
    headers = _scope_headers([SUBJECT_A])

    response = await client.get(
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}", headers=headers
    )

    assert response.status_code == 200
    document = response.json()
    assert document["state"] == "resumed"  # the activation receipt holds
    recovery = document["recovery"]
    assert recovery["schema"] == "forge.operator.recovery/1"
    assert recovery["delivery"]["outcome"] == "empty_diff_no_effect"
    assert recovery["delivery"]["failed"] is True
    assert "FAILED/no-effect delivery" in recovery["delivery"]["headline"]
    statuses = {name: entry["status"] for name, entry in recovery["ladder"].items()}
    assert statuses == {
        "pause_requested": "present",
        "checkpoint_committed": "present",
        "runner_stopped": "absent",  # occupancy queried, no lease row — no terminal observation
        "resume_authorized": "present",
        "exact_resume_applied": "present",
    }
    assert recovery["consistency"] == "observed"
    hint = recovery["hint"]
    assert "not a successful resume" in hint["advisory"]
    assert {command["via"] for command in hint["commands"]} >= {
        "command_router:/steer",
        "command_router:/resume",
    }
    # a consistent render: the action hints carry no stale mark
    assert document["actions_stale"] is False
    assert all("stale" not in entry for entry in document["actions"])


async def test_the_bundle_export_carries_bounded_allowlisted_diagnostics(app, client, repository):
    """The export's diagnostics slice rides the bundle: entry-bounded,
    field-allowlisted, and raw operational backup names excluded (the
    #304 receipts referenced at most) — the whole document still under
    the export's byte accounting."""
    await _seed(
        app,
        _run(
            RUN_A,
            REPO_A,
            status="failed",
            status_reason=(
                "restore failed: pre-r3708-alignment-1.dump unreadable "
                "(credential revoked, token expired)"
            ),
        ),
        *[
            PublicationIntent(
                run_id=RUN_A,
                provider="github",
                repo=REPO_A,
                operation="commit",
                target_ref="refs/heads/forge/run",
                idempotency_scope=f"cycle-{index}",
                operation_key=f"op-{RUN_A[:6]}-{index}",
                status="unknown",
                created_at=NOW - timedelta(minutes=40 - index),
                updated_at=NOW - timedelta(minutes=5),
            )
            for index in range(15)  # more uncertain effects than the diagnostics cap
        ],
    )
    headers = _scope_headers([SUBJECT_A])

    response = await client.get(
        f"/operator/runs/{RUN_A}/support-bundle?{_subject_query([SUBJECT_A])}", headers=headers
    )

    assert response.status_code == 200
    bundle = response.json()
    diagnostics = bundle["diagnostics"]
    assert diagnostics["schema"] == "forge.operator.diagnostics/1"
    # bounded: the 15 uncertain effects render as at most the cap blocked reasons
    assert len(diagnostics["sections"]["blocked_reasons"]) == 10
    assert diagnostics["export"]["truncated"]["blocked_reasons"] is True
    # allowlisted: every section key is a declared field
    from forge.adaptive.operator_view import DIAGNOSTIC_SECTION_FIELDS

    for entry in diagnostics["sections"]["blocked_reasons"]:
        assert set(entry) <= DIAGNOSTIC_SECTION_FIELDS["blocked_reasons"]
    # backup-free: the raw dump name never reaches the diagnostics slice,
    # and its exclusion is counted (the bundle's evidence sections are the
    # R38-03 surface, not the diagnostics slice)
    rendered = json.dumps(diagnostics)
    assert "alignment-1.dump" not in rendered
    assert diagnostics["export"]["raw_backups_excluded"] >= 1
    # the byte accounting includes the diagnostics slice
    assert bundle["export"]["bytes"] == len(response.content)
