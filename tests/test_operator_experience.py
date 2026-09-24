"""The bounded operator experience (R37-16) — bounded drill-down,
typed diagnostics, bounded export, observable pages.

The read API over the corrected read model, pinned end to end:

- **bounded drill-down**: the detail's section reads run under an
  explicit ``?limit=`` window with the authority totals reported
  (``sections`` carries ``total_count`` / ``returned`` / ``truncated``),
  ``?sections=`` selects which sections are READ AT ALL (unselected
  sections are never queried — their coverage reads ``unknown`` and the
  checkpoint authority is never consulted), and the max caps refuse
  out-of-range windows;
- **typed blocked reasons**: the detail renders ``blocked_reasons`` —
  each typed outcome code with its evidence link and its SAFE next
  action; a revoked authority never suggests retry;
- **pending commands + occupancy**: the actionable control slice (kind,
  age, the CTL-04 world the command must still match) and the native
  occupancy summary (``occupied_vs_limit`` against the mounted
  admission policy, the uncertain leases' ages) render as their own
  sections;
- **bounded export**: the support bundle states its export scope and
  size, refuses (typed ``operator.bundle_too_large``, 413) when the
  serialized document exceeds ``?max_bytes=``, narrows by ``?sections=``
  instead of truncating, and stays redacted;
- **time-to-diagnose observability**: every response carries
  ``operator.query_duration`` and ``operator.page_payload_bytes``;
- **bounded listing**: thousands of historical runs list in pages whose
  per-run authority reads scale with the PAGE, not the history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.adaptive.admission import AdmissionPolicy, ExecutionLease
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import ActionLog, FlowRun, PublicationIntent
from forge.main import create_app
from forge.adaptive.mailbox_db import ControlCommandRow

SECRET = "operator-secret"  # noqa: S105 — fake value for tests
NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
REPO_A = "owner/alpha"
RUN_A = "a" * 32
SUBJECT_A = CanonicalSubject(provider_family="github", connection="", native_id=REPO_A)
REF_A = SUBJECT_A.subject_id()


class RecordingRepository:
    """The injected checkpoint authority — recording every call so the
    bounded-read assertions can see exactly how often the (expensive)
    authority was consulted."""

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


def _run(run_id: str, repo: str = REPO_A, **over) -> FlowRun:
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


def _revival(run_id: str, number: int, status: str = "succeeded") -> ActionLog:
    return ActionLog(
        flow_run_id=run_id,
        action_kind="auto_revive",
        status=status,
        retryability="transient_infrastructure",
        dispatch_state="dispatched",
        remote_result={"generation": number},
        created_at=NOW - timedelta(minutes=120 - number),  # 1 is oldest, N is newest
    )


def _command(
    run_id: str, seq: int, kind: str, status: str, *, created_at=None, **over
) -> ControlCommandRow:
    values: dict = {
        "id": f"cmd-{run_id[:6]}-{seq}",
        "work_id": run_id,
        "run_id": run_id,
        "kind": kind,
        "payload": {"run_id": run_id},
        "status": status,
        "sequence": seq,
        "dedup_key": f"key-{run_id[:6]}-{seq}",
        "actor_ref": "human:op",
        "actor_origin": "server_authenticated_human",
        "created_at": created_at or (NOW - timedelta(minutes=90 - seq)),
        "expected_plan_revision": 7,
        "expected_execution_epoch": 0,
    }
    values.update(over)
    return ControlCommandRow(**values)


def _intent(run_id: str, key: str, status: str, *, minutes_ago: int = 20) -> PublicationIntent:
    return PublicationIntent(
        run_id=run_id,
        provider="github",
        repo=REPO_A,
        operation="commit",
        target_ref="refs/heads/forge/run",
        idempotency_scope=f"cycle-{key}",
        operation_key=key,
        status=status,
        created_at=NOW - timedelta(minutes=minutes_ago),
        updated_at=NOW - timedelta(minutes=min(minutes_ago, 5)),
    )


def _scope_headers(subjects=None) -> dict[str, str]:
    from forge.api_operator import operator_subject_scope_token

    return {
        "Authorization": f"Bearer {operator_subject_scope_token(SECRET, subjects or [SUBJECT_A])}"
    }


def _query(subjects=None, **params) -> str:
    from forge.api_operator import operator_subject_scope_token  # noqa: F401 — header helper

    subjects = subjects or [SUBJECT_A]
    parts = [f"subject={entry.subject_id()}" for entry in subjects]
    parts.extend(f"{name}={value}" for name, value in params.items())
    return "&".join(parts)


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
    async with app.state.session_factory() as session:
        session.add_all(rows)
        await session.commit()


async def _seed_bulk(app, rows) -> None:
    """Chunked bulk seeding (the thousands-of-runs pages)."""
    rows = list(rows)
    for start in range(0, len(rows), 500):
        async with app.state.session_factory() as session:
            session.add_all(rows[start : start + 500])
            await session.commit()


async def _detail(client, **params):
    return await client.get(f"/operator/runs/{RUN_A}?{_query(**params)}", headers=_scope_headers())


async def _bundle(client, **params):
    return await client.get(
        f"/operator/runs/{RUN_A}/support-bundle?{_query(**params)}", headers=_scope_headers()
    )


# ---------------------------------------------------------------------------
# Bounded drill-down — windows, totals, section selection
# ---------------------------------------------------------------------------


async def test_detail_reads_attempts_under_a_window_with_the_authority_total(
    app, client, repository
):
    await _seed(app, _run(RUN_A), *[_revival(RUN_A, number) for number in range(1, 31)])

    response = await _detail(client, limit=5)

    assert response.status_code == 200
    document = response.json()
    section = document["sections"]["attempts"]
    assert section == {
        "total_count": 30,  # the durable revival rows
        "returned": 6,  # the initial execution + the LAST 5
        "truncated": True,
        "limit": 5,
    }
    # the unbounded-by-construction sections report their own shape
    assert document["sections"]["checkpoints"] == {
        "total_count": 1,
        "returned": 1,
        "truncated": False,
        "limit": 5,
    }


async def test_detail_window_keeps_the_newest_history(app, client, repository):
    """The bounded window serves the LAST N rows — the newest attempt is
    what the projection derives from, never an old edge of the journal."""
    from forge.adaptive.operator_snapshot import OperatorSnapshotReader

    await _seed(app, _run(RUN_A), *[_revival(RUN_A, number) for number in range(1, 31)])
    reader = OperatorSnapshotReader(
        app.state.session_factory, checkpoint_repository=repository, clock=lambda: NOW
    )
    snapshot = await reader.snapshot(RUN_A, [SUBJECT_A], section_limit=5)
    assert snapshot is not None
    generations = [row["generation"] for row in snapshot.rows["attempts"]]
    assert generations == ["unknown", 26, 27, 28, 29, 30]  # initial + the LAST five
    assert snapshot.section_totals["attempts"] == 30
    assert snapshot.section_truncated["attempts"] is True


async def test_detail_limit_caps_are_enforced(app, client, repository):
    await _seed(app, _run(RUN_A))
    assert (await _detail(client, limit=0)).status_code == 422
    assert (await _detail(client, limit=101)).status_code == 422
    assert (await _detail(client, limit=100)).status_code == 200
    assert (await _detail(client, limit=1)).status_code == 200


async def test_detail_sections_selection_only_reads_the_named_sections(app, client, repository):
    """``?sections=`` bounds the QUERY itself: the unselected sections
    read ``unknown`` (never an invented empty success) and the checkpoint
    authority is never consulted for them."""
    await _seed(
        app,
        _run(RUN_A),
        _revival(RUN_A, 1),
        _command(RUN_A, 1, "pause", "checkpointed"),
    )

    response = await _detail(client, sections="attempts,commands")

    assert response.status_code == 200
    document = response.json()
    assert document["source_coverage"]["attempts"] == "present"
    assert document["source_coverage"]["commands"] == "present"
    assert document["source_coverage"]["checkpoints"] == "unknown"  # never queried
    assert document["source_coverage"]["verifications"] == "unknown"
    assert document["source_coverage"]["occupancy"] == "unknown"
    assert "checkpoints" not in document["sections"]
    assert "verifications" not in document["sections"]
    assert repository.calls == []  # the expensive authority was never consulted


async def test_detail_sections_validation_refuses_unknown_and_empty(app, client, repository):
    await _seed(app, _run(RUN_A))
    unknown = await _detail(client, sections="attempts,blobs")
    assert unknown.status_code == 400
    assert "unknown section" in unknown.json()["detail"]
    empty = await _detail(client, sections=",,")
    assert empty.status_code == 400
    assert "no sections selected" in empty.json()["detail"]


async def test_commands_render_pending_whole_plus_the_settled_tail(app, client, repository):
    rows = [
        _command(RUN_A, 1, "pause", "applied", applied_at=NOW - timedelta(minutes=80)),
        _command(RUN_A, 2, "steer", "applied", applied_at=NOW - timedelta(minutes=70)),
        _command(RUN_A, 3, "steer", "applied", applied_at=NOW - timedelta(minutes=60)),
        _command(RUN_A, 4, "steer", "applied", applied_at=NOW - timedelta(minutes=50)),
        # the PENDING slice — never windowed away
        _command(RUN_A, 5, "pause", "received"),
        _command(RUN_A, 6, "steer", "authorized"),
        _command(RUN_A, 7, "resume", "dispatching"),
    ]
    await _seed(app, _run(RUN_A), *rows)

    response = await _detail(client, limit=2)

    document = response.json()
    section = document["sections"]["commands"]
    assert section["total_count"] == 7
    assert section["returned"] == 5  # 3 pending + the LAST 2 settled
    assert section["truncated"] is True
    pending = document["pending_commands"]
    assert [entry["sequence"] for entry in pending] == [5, 6, 7]
    assert all(entry["kind"] for entry in pending)
    assert all(entry["age_seconds"] is not None and entry["age_seconds"] >= 0 for entry in pending)
    assert all(entry["expected_plan_revision"] == 7 for entry in pending)  # the CTL-04 world
    assert all(entry["expected_execution_epoch"] == 0 for entry in pending)


async def test_effects_render_unresolved_whole_plus_the_resolved_tail(app, client, repository):
    rows = [
        _intent(RUN_A, f"op-{number}", "committed", minutes_ago=90 - number)
        for number in range(1, 11)  # op-10 is the newest resolved effect
    ]
    rows += [
        _intent(RUN_A, "op-unresolved-1", "dispatched", minutes_ago=60),
        _intent(RUN_A, "op-unresolved-2", "unknown", minutes_ago=59),
    ]
    await _seed(app, _run(RUN_A), *rows)

    response = await _detail(client, limit=4)

    document = response.json()
    section = document["sections"]["publications"]
    assert section["total_count"] == 12
    assert section["returned"] == 6  # 2 unresolved + the LAST 4 resolved
    assert section["truncated"] is True
    # the unresolved slice is never windowed away — it is the effect line
    assert document["unresolved_effects"] == [
        {
            "operation_key": "op-unresolved-1",
            "operation": "commit",
            "target_ref": "refs/heads/forge/run",
            "status": "dispatched",
        },
        {
            "operation_key": "op-unresolved-2",
            "operation": "commit",
            "target_ref": "refs/heads/forge/run",
            "status": "unknown",
        },
    ]


# ---------------------------------------------------------------------------
# The typed blocked reasons on the detail surface
# ---------------------------------------------------------------------------


async def test_a_revoked_authority_renders_a_non_retry_blocked_reason(app, client, repository):
    await _seed(
        app,
        _run(RUN_A, status_reason="pipeline credential revoked by the provider (401)"),
    )

    document = (await _detail(client)).json()

    reasons = document["blocked_reasons"]
    revoked = next(reason for reason in reasons if reason["code"] == "revoked_authority")
    assert revoked["retryable"] is False
    # the headline assertion: a withdrawn authority is NEVER answered with retry
    assert "retry" not in revoked["suggested_action"]
    assert revoked["suggested_action"] == "rotate_or_rebind_credential"
    assert revoked["via"] == "runbook:token-rotation"
    assert revoked["evidence"]["of"] == "run"
    assert revoked["evidence"]["id"] == RUN_A
    assert revoked["evidence"]["ref"].startswith("sha256:")
    assert "revoked" in revoked["explanation"]


async def test_an_uncertain_effect_reason_links_its_exact_row(app, client, repository):
    await _seed(
        app,
        _run(RUN_A, status="failed"),
        _intent(RUN_A, "op-effect-1", "dispatched"),
    )

    document = (await _detail(client)).json()

    uncertain = next(
        reason
        for reason in document["blocked_reasons"]
        if reason["code"] == "uncertain_native_effect"
    )
    assert uncertain["retryable"] is False
    assert uncertain["evidence"] == {
        "of": "publication",
        "id": "op-effect-1",
        "ref": uncertain["evidence"]["ref"],  # the exact row's digest
    }
    assert uncertain["evidence"]["ref"].startswith("sha256:")
    assert uncertain["suggested_action"] == "reconcile"
    assert "unproven" in uncertain["explanation"]


async def test_a_lost_required_checkpoint_is_never_retryable(app, client, repository):
    """safely_paused standing on a checkpoint the authority no longer
    holds: the resume path is gone — restore or retire, never retry."""
    from forge.adaptive.pause_fence import PauseFenceRow

    await _seed(
        app,
        _run(RUN_A, status="proposing"),
        _command(RUN_A, 1, "pause", "checkpointed"),
        PauseFenceRow(
            work_id=RUN_A,
            publication_epoch_bumped=2,
            fenced_at=NOW - timedelta(minutes=20),
        ),
    )
    repository.entry_document = None  # the authority holds NOTHING now

    document = (await _detail(client)).json()

    loss = next(
        reason
        for reason in document["blocked_reasons"]
        if reason["code"] == "required_checkpoint_loss"
    )
    assert loss["retryable"] is False
    assert "retry" not in loss["suggested_action"]
    assert loss["suggested_action"] == "restore_checkpoint_or_retire"
    assert loss["evidence"]["of"] == "checkpoint"
    assert "bytes to restore" in loss["explanation"]


async def test_uncertain_occupancy_renders_a_capacity_wait_reason(app, client, repository):
    await _seed(
        app,
        _run(RUN_A),
        ExecutionLease(
            id="lease-unknown",
            project_id=1,
            provider="github",
            run_id=RUN_A,
            slot=1,
            acquired_at=NOW - timedelta(hours=1),
            native_intent_at=NOW - timedelta(minutes=55),
            native_intent_ref="github:workflow:42",
            # no native_handle — the accepted-but-unobserved start
        ),
        ExecutionLease(
            id="lease-done",
            project_id=1,
            provider="github",
            run_id=RUN_A,
            slot=2,
            acquired_at=NOW - timedelta(hours=2),
            released_at=NOW - timedelta(minutes=3),
            release_reason="native_terminal",
        ),
    )

    document = (await _detail(client)).json()

    waits = [reason for reason in document["blocked_reasons"] if reason["code"] == "capacity_wait"]
    assert len(waits) == 1  # only the UNCERTAIN lease, not the released one
    wait = waits[0]
    assert wait["evidence"]["of"] == "lease"
    assert wait["evidence"]["id"] == "lease-unknown"
    assert "dispatched_unknown" in wait["explanation"]
    assert wait["retryable"] is True  # capacity frees; this is a wait, not a wreck


async def test_a_stale_verification_reason_names_both_candidates(app, client, repository):
    cand_a, cand_b = "a" * 40, "b" * 40
    await _seed(
        app,
        _run(
            RUN_A,
            status="waiting_ci",
            candidate_shas=[cand_a, cand_b],
            evidence={
                "verification": {
                    "status": "passed",
                    "tested_oid": cand_a,  # the green verdict covers the OLD candidate
                    "observed_at": (NOW - timedelta(minutes=6)).isoformat(),
                    "producer": "github-checks",
                }
            },
        ),
    )

    document = (await _detail(client)).json()

    stale = next(
        reason for reason in document["blocked_reasons"] if reason["code"] == "verification_stale"
    )
    assert cand_a[:12] in stale["explanation"]
    assert cand_b[:12] in stale["explanation"]
    assert stale["evidence"]["of"] == "verification"
    assert stale["suggested_action"] == "verify_current_candidate"


async def test_a_healthy_run_carries_no_blocked_reasons(app, client, repository):
    await _seed(app, _run(RUN_A))  # requested, nothing wrong observed

    document = (await _detail(client)).json()
    assert document["blocked_reasons"] == []


async def test_blocked_reason_explanations_are_redacted(app, client, repository):
    await _seed(
        app,
        _run(RUN_A, status_reason="forbidden: Bearer ghp_aaaaaaaaaaaaaaaaaaaa was rejected"),
    )
    document = (await _detail(client)).json()
    assert "ghp_aaaaaaaaaaaaaaaaaaaa" not in str(document["blocked_reasons"])


# ---------------------------------------------------------------------------
# The occupancy summary surface
# ---------------------------------------------------------------------------


async def test_occupancy_summary_reports_occupied_vs_limit_and_unknown_ages(
    app, client, repository
):
    await _seed(
        app,
        _run(RUN_A),
        ExecutionLease(
            id="lease-open",
            project_id=1,
            provider="github",
            run_id=RUN_A,
            slot=1,
            acquired_at=NOW - timedelta(hours=1),
            native_intent_at=NOW - timedelta(minutes=55),
            native_intent_ref="github:workflow:42",
        ),
    )
    app.state.operator_admission_policy = AdmissionPolicy(max_active_per_project=3)

    document = (await _detail(client)).json()

    summary = document["occupancy_summary"]
    assert summary["occupied_vs_limit"] == {"occupied": 1, "limit": 3}
    assert summary["counts"] == {"dispatched_unknown": 1}
    assert summary["unknown_ages"][0]["lease_id"] == "lease-open"
    assert summary["unknown_ages"][0]["occupancy"] == "dispatched_unknown"
    # the age is measured against the render's own clock (computed_at)
    computed = datetime.fromisoformat(document["computed_at"])
    expected = (computed - (NOW - timedelta(hours=1))).total_seconds()
    assert abs(summary["unknown_ages"][0]["age_seconds"] - expected) <= 1.0


async def test_occupancy_limit_reads_null_without_a_mounted_policy(app, client, repository):
    await _seed(app, _run(RUN_A))

    document = (await _detail(client)).json()

    summary = document["occupancy_summary"]
    assert summary["occupied_vs_limit"] == {"occupied": 0, "limit": None}  # honest unknown
    assert summary["unknown_ages"] == []


# ---------------------------------------------------------------------------
# The bounded support-bundle export
# ---------------------------------------------------------------------------


async def test_the_bundle_states_its_export_scope_and_size(app, client, repository):
    await _seed(app, _run(RUN_A), _revival(RUN_A, 1))

    response = await _bundle(client)

    assert response.status_code == 200
    document = response.json()
    export = document["export"]
    assert export["bytes"] == len(response.content)  # the stated size IS the body size
    assert export["bytes"] <= export["max_bytes"]
    assert export["max_bytes"] == 1_048_576  # the default cap
    assert export["hard_max_bytes"] == 8_388_608
    assert export["truncated"] is False
    assert export["sections_selected"] is False
    assert "attempts" in export["scope"]
    assert document["coverage"]["attempts"] == "present"


async def test_an_oversized_bundle_is_refused_with_the_typed_code(app, client, repository):
    await _seed(
        app,
        _run(RUN_A, status="failed"),
        *[_revival(RUN_A, number, status="failed") for number in range(1, 60)],
    )
    full = await _bundle(client)
    assert full.status_code == 200
    full_size = full.json()["export"]["bytes"]

    # the narrowed export first — its size sets the boundary that must fit
    narrowed = await _bundle(client, sections="verifications")
    assert narrowed.status_code == 200
    narrowed_document = narrowed.json()
    narrowed_size = narrowed_document["export"]["bytes"]
    assert narrowed_size < full_size  # the 59 failed attempts are the bulk
    cap = (narrowed_size + full_size) // 2  # above the narrowed, below the full

    # the FULL bundle under that cap: the typed refusal, never truncation
    refused = await _bundle(client, max_bytes=cap)
    assert refused.status_code == 413
    detail = refused.json()["detail"]
    assert detail["code"] == "operator.bundle_too_large"
    assert detail["bytes"] > detail["max_bytes"] == cap
    assert "?sections=" in detail["hint"]

    # narrowing the scope is the bounded way to fit under the same cap
    fits = await _bundle(client, max_bytes=cap, sections="verifications")
    assert fits.status_code == 200
    assert fits.json()["export"]["scope"] == ["verifications"]
    assert fits.json()["export"]["sections_selected"] is True
    assert fits.json()["export"]["bytes"] <= cap
    # the unselected sections were never queried — unknown, never invented
    assert fits.json()["coverage"]["attempts"] == "unknown"
    assert fits.json()["coverage"]["commands"] == "unknown"
    assert fits.json()["coverage"]["verifications"] == "missing"  # queried, empty


async def test_the_bundle_byte_cap_hard_maximum_is_enforced(app, client, repository):
    await _seed(app, _run(RUN_A))
    assert (await _bundle(client, max_bytes=0)).status_code == 422
    assert (await _bundle(client, max_bytes=9_999_999)).status_code == 422
    assert (await _bundle(client, max_bytes=8_388_608)).status_code == 200


async def test_bundle_sections_validation_refuses_unknown_names(app, client, repository):
    await _seed(app, _run(RUN_A))
    response = await _bundle(client, sections="attempts,occupancy")
    assert response.status_code == 400
    assert "unknown section" in response.json()["detail"]


async def test_the_bundle_export_never_leaks_credentials(app, client, repository):
    await _seed(
        app,
        _run(
            RUN_A,
            status_reason="pipeline failed: Bearer ghp_aaaaaaaaaaaaaaaaaaaa rejected",
            status="failed",
        ),
        _revival(RUN_A, 1, status="failed"),
    )
    for params in ({}, {"sections": "attempts"}, {"max_bytes": 1_048_576}):
        response = await _bundle(client, **params)
        assert response.status_code == 200, params
        assert "ghp_aaaaaaaaaaaaaaaaaaaa" not in response.text
        assert "glpat-test" not in response.text  # no model/provider keys either
        assert "operator-secret" not in response.text  # and not the lane secret


# ---------------------------------------------------------------------------
# Time-to-diagnose observability — on every response
# ---------------------------------------------------------------------------


async def test_every_response_carries_the_query_duration_and_payload_bytes(app, client, repository):
    await _seed(app, _run(RUN_A), _revival(RUN_A, 1))

    for response in (
        await client.get(f"/operator/runs?{_query()}", headers=_scope_headers()),
        await _detail(client),
        await _bundle(client),
    ):
        assert response.status_code == 200
        duration = float(response.headers["operator.query_duration"])
        payload = int(response.headers["operator.page_payload_bytes"])
        assert duration >= 0.0
        assert payload == len(response.content)


# ---------------------------------------------------------------------------
# The bounded listing — thousands of runs, page-scaled authority reads
# ---------------------------------------------------------------------------


async def test_thousands_of_runs_list_in_bounded_pages(app, client, repository):
    """R37-16's history-accumulation page: 2 500 historical runs, a 50-run
    page, and the expensive checkpoint authority consulted EXACTLY once
    per page member — never once per run in scope."""
    history = [
        _run(
            f"{index:032x}",
            updated_at=NOW - timedelta(minutes=index % 500, hours=index // 500),
        )
        for index in range(2500)
    ]
    await _seed_bulk(app, history)

    first = await client.get(f"/operator/runs?{_query(limit=50)}", headers=_scope_headers())
    assert first.status_code == 200
    page = first.json()
    assert page["page_size"] == 50
    assert len(page["runs"]) == 50
    assert page["next_cursor"]
    assert repository.calls == ["entry"] * 50  # the page's members, not the scope

    second = await client.get(
        f"/operator/runs?{_query(limit=50, cursor=page['next_cursor'])}",
        headers=_scope_headers(),
    )
    assert second.status_code == 200
    assert len(second.json()["runs"]) == 50
    assert repository.calls == ["entry"] * 100  # still page-scaled

    # the newest run leads the first page (bounded, not unordered)
    assert page["runs"][0]["updated_at"] >= page["runs"][-1]["updated_at"]
