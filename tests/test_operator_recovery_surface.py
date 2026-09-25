"""The operator recovery surface (R38-15, #316) — delivery outcome, the
five-milestone ladder, advisory recovery hints, bounded diagnostics.

The live single-writer run's exact pain: an empty resumed diff displayed
as a "successful resume" shape, and operators could not tell a requested
pause from a verified checkpoint, a stopped runner, an authorized resume
or an APPLIED exact resume. Pinned here, on REAL persisted rows through
the real reader (two same-name repositories on different connections —
the #283 canonical-subject fixtures — with historical revival attempts,
never hand-assembled ideal snapshots):

- the DELIVERY OUTCOME as a first-class field, derived from the #302
  finalization markers (``candidate_state`` / driver exit) and the run
  row's typed blocked reason — an empty resumed diff is a FAILED/no-effect
  delivery, never a successful resume;
- the FIVE-MILESTONE ladder, each present/absent/unknown INDEPENDENTLY;
- the recovery-hint matrix (every state × outcome → the correct advisory
  + the guarded route named; the #297 non-retryable codes never retry);
- stale action hints (rendered from a snapshot whose #284 fence moved)
  carry ``stale: true`` and the current safe alternative;
- the export diagnostics slice — entry-bounded, field-allowlisted, raw
  operational backup names excluded (the #304 receipts referenced at
  most);
- concurrent updates during render → explicit uncertainty, never a
  confident mixed ladder; a checkpoint authority outage with the DB
  healthy → unknown, never "no checkpoint".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.admission import ExecutionLease
from forge.adaptive.checkpoint_repository import CheckpointRepositoryUnavailable
from forge.adaptive.mailbox_db import ControlCommandRow
from forge.adaptive.operator_snapshot import (
    CanonicalSubject,
    OperatorSnapshotReader,
)
from forge.adaptive.operator_view import (
    ACTION_VIA,
    DELIVERY_OUTCOMES,
    DIAGNOSTIC_MAX_ENTRIES,
    DIAGNOSTIC_SECTION_FIELDS,
    OPERATOR_STATES,
    RECOVERY_MILESTONES,
    _allowlisted,
    action_hint_block,
    export_diagnostics,
    initial_projection,
    recovery_document,
    recovery_hint,
)
from forge.adaptive.pause_fence import PauseFenceRow
from forge.durable.models import ActionLog, FlowRun
from forge.models.base import Base

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)

#: The #283 collision world: ONE display name, TWO connections — each a
#: genuinely distinct canonical subject no grant of the other can read.
NAME = "owner/alpha"
GITHUB_COM = CanonicalSubject(
    provider_family="github", connection="github.com", native_id=NAME, display=NAME
)
GITHUB_ENTERPRISE = CanonicalSubject(
    provider_family="github", connection="ghe.corp", native_id=NAME, display=NAME
)
RUN_COM = "1" * 32
RUN_GHE = "2" * 32
CHECKPOINT = "e" * 64
OTHER_CHECKPOINT = "f" * 64


class RecordingRepository:
    """The injected checkpoint authority — recording calls, optionally
    unavailable (the outage arm: an outage is not an absence)."""

    def __init__(self, entry: dict | None = None, *, unavailable: bool = False) -> None:
        self.entry_document = entry
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []

    async def entry(self, work_id: str) -> dict | None:
        self.calls.append(("entry", work_id))
        if self.unavailable:
            raise CheckpointRepositoryUnavailable("authority down")
        return dict(self.entry_document) if self.entry_document else None

    async def put(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003 — recorder
        self.calls.append(("put", args[0] if args else "?"))

    async def read(self, work_id: str):
        self.calls.append(("read", work_id))
        return None

    async def lookup_outcome(self, work_id: str):
        self.calls.append(("lookup_outcome", work_id))

    async def authority(self) -> str:
        return "recording"

    async def pin(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003 — recorder
        self.calls.append(("pin", args[0] if args else "?"))
        return False

    async def unpin(self, *args, **kwargs) -> int:  # noqa: ANN002, ANN003 — recorder
        self.calls.append(("unpin", args[0] if args else "?"))
        return 0

    async def pins(self, work_id: str | None = None) -> list[dict]:
        self.calls.append(("pins", work_id or ""))
        return []


@pytest.fixture()
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _run(run_id: str, connection: str, **over) -> FlowRun:
    """A REAL FlowRun row on one connection of the colliding display name."""
    host = connection.split("://", 1)[-1].split("/", 1)[0].lower()
    values: dict = {
        "id": run_id,
        "project_id": {"github.com": 101, "ghe.corp": 202}[host],
        "provider": "github",
        "github_repo_full_name": NAME,
        "status": "proposing",
        "base_sha": "b" * 40,
        "candidate_shas": [],
        "plan_digest": "p" * 64,
        "evidence": {"connection": connection},
        "created_at": NOW - timedelta(hours=3),
        "updated_at": NOW - timedelta(minutes=10),
    }
    values.update(over)
    return FlowRun(**values)


def _revival(run_id: str, status: str, *, attempt: int = 1, **over) -> ActionLog:
    """One durable REVIVAL record — the historical attempts the ladder and
    the delivery outcome sit beside."""
    values: dict = {
        "flow_run_id": run_id,
        "action_kind": "retry_requested",
        "status": status,
        "retryability": "transient_infrastructure",
        "dispatch_state": "dispatched",
        "remote_result": {"generation": attempt - 1},
        "created_at": NOW - timedelta(minutes=60 - attempt),
    }
    values.update(over)
    return ActionLog(**values)


def _command(
    run_id: str,
    seq: int,
    kind: str,
    status: str,
    *,
    applied_at: datetime | None = None,
    checkpoint_ref: str | None = None,
) -> ControlCommandRow:
    payload: dict = {"run_id": run_id}
    if checkpoint_ref is not None:
        payload["checkpoint_ref"] = checkpoint_ref
    return ControlCommandRow(
        id=f"cmd-{run_id[:6]}-{seq}",
        work_id=run_id,
        run_id=run_id,
        kind=kind,
        payload=payload,
        status=status,
        sequence=seq,
        dedup_key=f"dedup-{run_id[:6]}-{seq}",
        actor_ref="human:op",
        actor_origin="server_authenticated_human",
        created_at=NOW - timedelta(minutes=40 - seq),
        applied_at=applied_at,
    )


def _fence(run_id: str, *, cleared: bool = False) -> PauseFenceRow:
    return PauseFenceRow(
        work_id=run_id,
        publication_epoch_bumped=2,
        fenced_at=NOW - timedelta(minutes=35),
        cleared_at=NOW - timedelta(minutes=20) if cleared else None,
    )


def _lease(run_id: str, slot: int, *, released: bool = False) -> ExecutionLease:
    return ExecutionLease(
        id=f"lease-{run_id[:6]}-{slot}",
        project_id=1,
        provider="github",
        run_id=run_id,
        slot=slot,
        acquired_at=NOW - timedelta(hours=2),
        native_intent_at=NOW - timedelta(minutes=110),
        native_handle=f"github:workflow:{slot}",
        **(
            {
                "draining_at": NOW - timedelta(minutes=15),
                "released_at": NOW - timedelta(minutes=10),
                "release_reason": "native_terminal",
            }
            if released
            else {}
        ),
    )


def _checkpoint_entry(**over) -> dict:
    entry: dict = {
        "checkpoint_id": CHECKPOINT,
        "sequence": 3,
        "files": 2,
        "uploaded_at": (NOW - timedelta(minutes=30)).isoformat(),
    }
    entry.update(over)
    return entry


async def _seed(session_factory, *rows) -> None:
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()


def _reader(session_factory, repository=None) -> OperatorSnapshotReader:
    return OperatorSnapshotReader(
        session_factory, checkpoint_repository=repository, clock=lambda: NOW
    )


async def _recovery(session_factory, repository, run_id, subject, sections=None):
    """Read one run through the REAL reader and render its recovery surface."""
    snapshot = await _reader(session_factory, repository).snapshot(
        run_id, [subject], sections=sections
    )
    assert snapshot is not None
    projection = snapshot.projection()
    return (
        snapshot,
        recovery_document(
            snapshot.rows,
            state=projection.state,
            coverage=snapshot.source_coverage,
            occupancy=snapshot.occupancy,
            projection_inconsistent=snapshot.projection_inconsistent,
        ),
    )


# ---------------------------------------------------------------------------
# The headline: an EMPTY resumed diff is a FAILED/no-effect delivery
# ---------------------------------------------------------------------------


async def test_an_empty_resumed_diff_is_a_failed_delivery_never_a_successful_resume(
    session_factory,
):
    """The issue's headline arm, on real rows: the resume command applied,
    the checkpoint activated (the #284 receipt matches) — the state ladder
    says ``resumed`` — but the recorded lane outcome says the collected
    candidate was ZERO-CHANGE. The recovery surface displays a FAILED
    no-effect delivery with both escapes named, never a successful resume."""
    await _seed(
        session_factory,
        _run(
            RUN_COM,
            "https://github.com",
            evidence={
                "connection": "https://github.com",
                "harness": {
                    "driver_exit": "completed",
                    "collector_exit": 0,
                    "candidate_state": "zero_change",
                },
            },
        ),
        _revival(RUN_COM, "succeeded", attempt=1),
        _revival(RUN_COM, "succeeded", attempt=2),  # the resumed attempt
        _command(RUN_COM, 1, "pause", "checkpointed"),
        _command(
            RUN_COM,
            2,
            "resume",
            "applied",
            applied_at=NOW - timedelta(minutes=20),
            checkpoint_ref=f"{RUN_COM}@{CHECKPOINT}",
        ),
        _fence(RUN_COM, cleared=True),
    )
    repository = RecordingRepository(entry=_checkpoint_entry())

    snapshot, recovery = await _recovery(session_factory, repository, RUN_COM, GITHUB_COM)

    # the activation receipt holds — the STATE ladder is honestly `resumed`
    assert snapshot.projection().state == "resumed"
    assert snapshot.rows["run"]["lane_outcome"] == {
        "driver_exit": "completed",
        "collector_exit": 0,
        "candidate_state": "zero_change",
    }
    # but the DELIVERY is a failure — never displayed as a successful resume
    delivery = recovery["delivery"]
    assert delivery["outcome"] == "empty_diff_no_effect"
    assert delivery["failed"] is True
    assert delivery["reason"] == "harness candidate_state=zero_change"
    assert "FAILED/no-effect delivery" in delivery["headline"]
    assert "never a successful resume" in delivery["headline"]
    # the advisory names BOTH escapes with their exact command shapes
    hint = recovery["hint"]
    assert "not a successful resume" in hint["advisory"]
    shapes = {command["command"]: command["via"] for command in hint["commands"]}
    assert shapes["/steer <run-id> <corrected guidance>"] == "command_router:/steer"
    assert shapes["/resume <run-id>"] == "command_router:/resume"


async def test_the_typed_no_effect_blockage_derives_the_same_failed_delivery(session_factory):
    """The fallback arm: no lane-outcome marker journaled, but the run row
    carries the harness backend's typed ``repair_no_effect`` reason beside
    OLDER delivered candidates — the typed reason wins, the old candidates
    never repaint the current attempt as delivered."""
    await _seed(
        session_factory,
        _run(
            RUN_COM,
            "https://github.com",
            status="failed",
            candidate_shas=["c" * 40],  # an EARLIER cycle's candidate
            status_reason="repair_no_effect: the resumed diff was empty",
        ),
    )

    _, recovery = await _recovery(session_factory, RecordingRepository(), RUN_COM, GITHUB_COM)

    assert recovery["delivery"]["outcome"] == "empty_diff_no_effect"
    assert recovery["delivery"]["failed"] is True
    assert "repair_no_effect" in recovery["delivery"]["reason"]


async def test_a_resumed_run_with_a_real_candidate_is_delivered_not_failed(session_factory):
    """The positive arm: the same resume shape with a RECORDED candidate —
    the delivery reads ``delivered``, the failure display never fires."""
    await _seed(
        session_factory,
        _run(
            RUN_COM,
            "https://github.com",
            candidate_shas=["c" * 40],
            evidence={
                "connection": "https://github.com",
                "harness": {
                    "driver_exit": "completed",
                    "collector_exit": 0,
                    "candidate_state": "candidate",
                },
            },
        ),
        _command(RUN_COM, 1, "pause", "checkpointed"),
        _command(
            RUN_COM,
            2,
            "resume",
            "applied",
            applied_at=NOW - timedelta(minutes=20),
            checkpoint_ref=f"{RUN_COM}@{CHECKPOINT}",
        ),
        _fence(RUN_COM, cleared=True),
    )
    repository = RecordingRepository(entry=_checkpoint_entry())

    snapshot, recovery = await _recovery(session_factory, repository, RUN_COM, GITHUB_COM)

    assert snapshot.projection().state == "resumed"
    assert recovery["delivery"]["outcome"] == "delivered"
    assert recovery["delivery"]["failed"] is False


# ---------------------------------------------------------------------------
# The five-milestone ladder — each present/absent/unknown INDEPENDENTLY
# ---------------------------------------------------------------------------


def _statuses(recovery: dict) -> dict[str, str]:
    return {name: entry["status"] for name, entry in recovery["ladder"].items()}


async def test_every_milestone_is_unknown_when_no_section_was_queried(session_factory):
    """``sections=run`` only: commands/checkpoints/occupancy were never
    queried — every milestone reads UNKNOWN, never a guessed absence."""
    await _seed(
        session_factory,
        _run(RUN_COM, "https://github.com"),
        _command(RUN_COM, 1, "pause", "checkpointed"),
        _fence(RUN_COM),
    )

    snapshot = await _reader(
        session_factory, RecordingRepository(entry=_checkpoint_entry())
    ).snapshot(RUN_COM, [GITHUB_COM], sections=("run",))
    assert snapshot is not None
    recovery = recovery_document(
        snapshot.rows, state="safely_paused", coverage=snapshot.source_coverage
    )

    assert set(recovery["ladder"]) == set(RECOVERY_MILESTONES)
    assert set(_statuses(recovery).values()) == {"unknown"}


async def test_the_milestones_are_independent_on_real_rows(session_factory):
    """The full ladder on one coherent world: pause requested + checkpoint
    committed + runner stopped + resume authorized + exact resume applied —
    each carrying its OWN evidence row, and each independently derivable
    as absent in the neighbouring worlds below."""
    await _seed(
        session_factory,
        _run(RUN_COM, "https://github.com"),
        _command(RUN_COM, 1, "pause", "checkpointed"),
        _command(
            RUN_COM,
            2,
            "resume",
            "applied",
            applied_at=NOW - timedelta(minutes=20),
            checkpoint_ref=f"{RUN_COM}@{CHECKPOINT}",
        ),
        _fence(RUN_COM, cleared=True),
        _lease(RUN_COM, 1, released=True),  # the native job's terminal observation
    )
    repository = RecordingRepository(entry=_checkpoint_entry())

    snapshot, recovery = await _recovery(session_factory, repository, RUN_COM, GITHUB_COM)

    assert _statuses(recovery) == {
        "pause_requested": "present",
        "checkpoint_committed": "present",
        "runner_stopped": "present",
        "resume_authorized": "present",
        "exact_resume_applied": "present",
    }
    ladder = recovery["ladder"]
    assert ladder["pause_requested"]["evidence"]["of"] == "pause command"
    assert ladder["checkpoint_committed"]["evidence"]["id"] == CHECKPOINT
    assert ladder["runner_stopped"]["evidence"]["of"] == "lease"
    assert ladder["resume_authorized"]["evidence"]["of"] == "resume command"
    assert ladder["exact_resume_applied"]["at"]  # the activation receipt's moment
    assert snapshot is not None


async def test_an_open_lease_means_the_runner_has_not_stopped(session_factory):
    await _seed(
        session_factory,
        _run(RUN_COM, "https://github.com"),
        _lease(RUN_COM, 1),  # still occupying — no terminal observation
    )

    _, recovery = await _recovery(session_factory, RecordingRepository(), RUN_COM, GITHUB_COM)

    assert recovery["ladder"]["runner_stopped"]["status"] == "absent"


async def test_a_refused_resume_was_never_authorized(session_factory):
    await _seed(
        session_factory,
        _run(RUN_COM, "https://github.com"),
        _command(RUN_COM, 1, "pause", "checkpointed"),
        _command(RUN_COM, 2, "resume", "rejected"),
        _fence(RUN_COM),
    )
    repository = RecordingRepository(entry=_checkpoint_entry())

    _, recovery = await _recovery(session_factory, repository, RUN_COM, GITHUB_COM)

    assert recovery["ladder"]["resume_authorized"]["status"] == "absent"
    assert recovery["ladder"]["exact_resume_applied"]["status"] == "absent"
    # the pause and the checkpoint stand on their own — independence
    assert recovery["ladder"]["pause_requested"]["status"] == "present"
    assert recovery["ladder"]["checkpoint_committed"]["status"] == "present"


async def test_an_unmatched_resume_activates_nothing(session_factory):
    """The #284 matching displayed: an applied resume naming a DIFFERENT
    checkpoint authorizes the resume but activates NOTHING on this one."""
    await _seed(
        session_factory,
        _run(RUN_COM, "https://github.com"),
        _command(RUN_COM, 1, "pause", "checkpointed"),
        _command(
            RUN_COM,
            2,
            "resume",
            "applied",
            applied_at=NOW - timedelta(minutes=20),
            checkpoint_ref=f"{RUN_COM}@{OTHER_CHECKPOINT}",  # NOT the active entry
        ),
        _fence(RUN_COM),
    )
    repository = RecordingRepository(entry=_checkpoint_entry())

    _, recovery = await _recovery(session_factory, repository, RUN_COM, GITHUB_COM)

    assert recovery["ladder"]["resume_authorized"]["status"] == "present"
    assert recovery["ladder"]["exact_resume_applied"]["status"] == "absent"


async def test_a_commands_authority_observed_empty_makes_the_requests_absent(session_factory):
    """Commands queried and EMPTY (coverage missing): the requests are
    ABSENT — distinguishable from the unqueried unknown above."""
    await _seed(session_factory, _run(RUN_COM, "https://github.com"))

    _, recovery = await _recovery(session_factory, RecordingRepository(), RUN_COM, GITHUB_COM)

    assert recovery["ladder"]["pause_requested"]["status"] == "absent"
    assert recovery["ladder"]["resume_authorized"]["status"] == "absent"
    assert recovery["ladder"]["checkpoint_committed"]["status"] == "absent"


async def test_an_unavailable_checkpoint_authority_is_unknown_not_no_checkpoint(session_factory):
    """The negative arm: the DB is healthy (the run reads fine) but the
    checkpoint authority is DOWN — both checkpoint milestones read
    UNKNOWN, never an absence a resume decision could stand on."""
    await _seed(
        session_factory,
        _run(RUN_COM, "https://github.com"),
        _command(RUN_COM, 1, "pause", "checkpointed"),
        _fence(RUN_COM),
    )
    repository = RecordingRepository(unavailable=True)

    snapshot, recovery = await _recovery(session_factory, repository, RUN_COM, GITHUB_COM)

    assert snapshot is not None
    assert snapshot.source_coverage["checkpoints"] == "unknown"
    assert recovery["ladder"]["checkpoint_committed"]["status"] == "unknown"
    assert recovery["ladder"]["exact_resume_applied"]["status"] == "unknown"
    assert recovery["ladder"]["pause_requested"]["status"] == "present"  # the DB row stands


# ---------------------------------------------------------------------------
# The recovery-hint matrix — every state × outcome, the guarded route named
# ---------------------------------------------------------------------------

#: The only healthy combinations with NOTHING to recover.
_NO_HINT_EXPECTED = {("verified_ready", "delivered"), ("accepted", "delivered")}

_KNOWN_ROUTES = frozenset(ACTION_VIA.values()) | {"runbook:token-rotation"}


def test_the_hint_matrix_covers_every_state_and_outcome():
    """Every state × outcome answers a well-formed advisory whose commands
    each name an EXISTING guarded route — the hint is never a second
    authorization surface, and no combination goes unanswered."""
    for state in OPERATOR_STATES:
        for outcome in DELIVERY_OUTCOMES:
            hint = recovery_hint(state, outcome)
            if (state, outcome) in _NO_HINT_EXPECTED:
                assert hint is None, (state, outcome)
                continue
            assert hint is not None, (state, outcome)
            assert hint.advisory
            assert hint.commands
            for command in hint.commands:
                assert command["command"], (state, outcome)
                assert command["via"] in _KNOWN_ROUTES, (state, outcome, command)


def test_the_empty_diff_hint_names_both_escapes_with_exact_shapes():
    hint = recovery_hint("resumed", "empty_diff_no_effect")

    assert "not a successful resume" in hint.advisory
    shapes = {command["command"] for command in hint.commands}
    assert shapes == {
        "/steer <run-id> <corrected guidance>",
        "/resume <run-id>",
        "GET /operator/runs/<run-id>",
    }


def test_the_collection_failure_hint_carries_the_typed_error_and_the_rerun_path():
    hint = recovery_hint("rejected", "collection_failed", blocked_reason="harness_artifact_missing")

    assert "harness_artifact_missing" in hint.advisory
    routes = {command["via"] for command in hint.commands}
    assert "operator-commands:/retry" in routes


def test_a_stale_authority_hint_rotates_or_reconciles_and_never_retries():
    """The #297 non-retryable condition: a revoked authority outranks the
    delivery outcome — rotate/reconcile, retryable=False, no retry verb."""
    for outcome in DELIVERY_OUTCOMES:
        hint = recovery_hint("safely_paused", outcome, blocked_reason="credential revoked (403)")

        assert hint is not None, outcome
        assert hint.retryable is False
        commands = {command["via"] for command in hint.commands}
        assert "runbook:token-rotation" in commands
        assert "operator-commands:/retry" not in commands


def test_unknown_states_and_outcomes_fail_visibly():
    with pytest.raises(ValueError, match="unknown operator state"):
        recovery_hint("exploded", "delivered")
    with pytest.raises(ValueError, match="unknown delivery outcome"):
        recovery_hint("executing", "vaporized")


# ---------------------------------------------------------------------------
# Stale action hints — the old-snapshot revalidation display
# ---------------------------------------------------------------------------


def _delivering_projection():
    rows = {
        "run": {
            "id": RUN_COM,
            "status": "proposing",
            "candidate_shas": [],
            "plan_digest": "p" * 64,
            "evidence": {},
            "blocked_reason": "",
            "cancel_requested": False,
            "created_at": "2026-09-24T09:00:00+00:00",
            "updated_at": "2026-09-24T09:30:00+00:00",
        }
    }
    return initial_projection(rows, NOW)


def test_hints_from_a_consistent_snapshot_carry_no_stale_mark():
    block = action_hint_block(_delivering_projection())

    assert block["actions_stale"] is False
    assert block["actions"]
    for entry in block["actions"]:
        assert "stale" not in entry
        assert "safe_alternative" not in entry
    assert "actions_stale_reason" not in block


def test_hints_from_a_moved_fence_render_stale_with_the_safe_alternative():
    """The #284 fence moved while the snapshot was read: every hint carries
    ``stale: true`` plus the current state's safe alternative (probe), and
    the block says WHY — the display half of the refusal the guarded route
    already enforces."""
    block = action_hint_block(_delivering_projection(), snapshot_inconsistent=True)

    assert block["actions_stale"] is True
    for entry in block["actions"]:
        assert entry["stale"] is True
        assert entry["safe_alternative"] == "probe"
    assert "refresh" in block["actions_stale_reason"]
    assert "refuse" in block["actions_stale_reason"]


# ---------------------------------------------------------------------------
# The export diagnostics slice — bounded, allowlisted, backup-free
# ---------------------------------------------------------------------------


def _diagnostics_rows(blocked_reason: str = "", publications: int = 0) -> dict:
    return {
        "run": {
            "id": RUN_COM,
            "status": "failed" if blocked_reason else "proposing",
            "base_sha": "b" * 40,
            "candidate_shas": [],
            "plan_digest": "p" * 64,
            "evidence": {},
            "blocked_reason": blocked_reason,
            "cancel_requested": False,
            "created_at": "2026-09-24T09:00:00+00:00",
            "updated_at": "2026-09-24T09:30:00+00:00",
        },
        "publications": [
            {
                "operation_key": f"op-{index}",
                "status": "unknown",
                "operation": "commit",
                "target_ref": "refs/heads/forge/run",
                "at": "2026-09-24T09:20:00+00:00",
            }
            for index in range(publications)
        ],
    }


def test_diagnostics_entries_are_bounded_per_section():
    document = export_diagnostics(_diagnostics_rows(publications=DIAGNOSTIC_MAX_ENTRIES + 5))

    reasons = document["sections"]["blocked_reasons"]
    assert len(reasons) == DIAGNOSTIC_MAX_ENTRIES
    assert document["export"]["truncated"]["blocked_reasons"] is True
    assert len(document["sections"]["recovery_ladder"]) == len(RECOVERY_MILESTONES)
    assert document["export"]["truncated"]["recovery_ladder"] is False


def test_diagnostics_sections_serialize_allowlisted_fields_only():
    document = export_diagnostics(_diagnostics_rows(blocked_reason="credential revoked"))

    sections = document["sections"]
    assert set(sections["delivery"]) <= DIAGNOSTIC_SECTION_FIELDS["delivery"]
    for entry in sections["recovery_ladder"]:
        assert set(entry) <= DIAGNOSTIC_SECTION_FIELDS["recovery_ladder"]
    for entry in sections["recovery_hint"]:
        assert set(entry) <= DIAGNOSTIC_SECTION_FIELDS["recovery_hint"]
    for entry in sections["blocked_reasons"]:
        assert set(entry) <= DIAGNOSTIC_SECTION_FIELDS["blocked_reasons"]


def test_unlisted_fields_are_dropped_entirely():
    over_full = {
        "code": "capacity_wait",
        "explanation": "held by lease",
        "secret_payload": "glpat-should-never-serialize",
        "raw_log": "PostgreSQL custom-format dump bytes",
    }

    cleaned = _allowlisted(over_full, DIAGNOSTIC_SECTION_FIELDS["blocked_reasons"])

    assert set(cleaned) == {"code", "explanation"}


def test_raw_operational_backup_names_are_excluded_receipts_referenced_at_most():
    """The #304 world: a blocked reason naming raw dump files and a
    backups/ path — the diagnostics export carries NEITHER name, counts
    what it excluded, and a sanitized RECEIPT reference survives whole."""
    rows = _diagnostics_rows(
        blocked_reason=(
            "restore failed: pre-r3708-alignment-1.dump unreadable after copying "
            "backups/pre-r3708-alignment-2.dump.gz (revoked token)"
        )
    )

    document = export_diagnostics(rows)
    rendered = json.dumps(document)

    assert "alignment-1.dump" not in rendered
    assert "alignment-2.dump.gz" not in rendered
    assert "backups/" not in rendered
    assert document["export"]["raw_backups_excluded"] >= 3
    assert "[backup-excluded]" in rendered

    receipt_rows = _diagnostics_rows(
        blocked_reason=(
            "restore verified per pre-r3708-alignment-1.dump.receipt.json (credential revoked)"
        )
    )
    receipt_document = export_diagnostics(receipt_rows)
    assert "alignment-1.dump.receipt.json" in json.dumps(receipt_document)
    assert receipt_document["export"]["raw_backups_excluded"] == 0


# ---------------------------------------------------------------------------
# The #284 fence during render — explicit uncertainty, never a mixed ladder
# ---------------------------------------------------------------------------


class DriftingFenceReader(OperatorSnapshotReader):
    """Bounded fault injection (the same seam as the reader's own fence
    tests): the source version drifts between the start and end fences —
    a concurrent attempt/checkpoint commit under READ COMMITTED."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fence_reads = 0

    async def _source_version(self, session: Any, run_id: str) -> str:
        self._fence_reads += 1
        base = await super()._source_version(session, run_id)
        return f"{base}#r{self._fence_reads}"


async def test_concurrent_updates_during_render_surface_explicit_uncertainty(session_factory):
    await _seed(
        session_factory,
        _run(RUN_COM, "https://github.com"),
        _command(RUN_COM, 1, "pause", "checkpointed"),
        _revival(RUN_COM, "succeeded"),
        _fence(RUN_COM),
    )
    repository = RecordingRepository(entry=_checkpoint_entry())

    snapshot = await DriftingFenceReader(
        session_factory, checkpoint_repository=repository, clock=lambda: NOW
    ).snapshot(RUN_COM, [GITHUB_COM])
    assert snapshot is not None
    assert snapshot.projection_inconsistent is True

    recovery = recovery_document(
        snapshot.rows,
        state=snapshot.projection().state,
        coverage=snapshot.source_coverage,
        occupancy=snapshot.occupancy,
        projection_inconsistent=snapshot.projection_inconsistent,
    )

    assert recovery["consistency"] == "inconsistent"
    assert "re-read before acting" in recovery["uncertainty"]
    # the ladder still names its milestones — but the document says the
    # whole render may describe a moved world, never a confident state
    assert set(recovery["ladder"]) == set(RECOVERY_MILESTONES)


# ---------------------------------------------------------------------------
# Multi-connection canonical subjects through the REAL reader
# ---------------------------------------------------------------------------


async def test_each_connection_recovery_reads_only_its_own_rows(session_factory):
    """The #283 collision world with recovery data on both sides: the same
    display name on github.com (an empty-diff resume) and on ghe.corp (a
    delivered candidate with historical attempts). Each canonical grant
    reads exactly its own recovery surface; the other run is a 404-shaped
    None, and neither ladder borrows the other's rows."""
    await _seed(
        session_factory,
        # connection A: the failed empty-diff resume
        _run(
            RUN_COM,
            "https://github.com",
            evidence={
                "connection": "https://github.com",
                "harness": {
                    "driver_exit": "completed",
                    "collector_exit": 0,
                    "candidate_state": "zero_change",
                },
            },
        ),
        _revival(RUN_COM, "succeeded", attempt=1),
        _command(RUN_COM, 1, "pause", "checkpointed"),
        _command(
            RUN_COM,
            2,
            "resume",
            "applied",
            applied_at=NOW - timedelta(minutes=20),
            checkpoint_ref=f"{RUN_COM}@{CHECKPOINT}",
        ),
        _fence(RUN_COM, cleared=True),
        # connection B: a delivered candidate, no pause/resume history
        _run(
            RUN_GHE,
            "https://ghe.corp",
            status="waiting_ci",
            candidate_shas=["d" * 40],
        ),
        _revival(RUN_GHE, "succeeded", attempt=1),
    )
    repository = RecordingRepository(
        entry=_checkpoint_entry()
    )  # the authority answers BOTH work ids the same way
    reader = _reader(session_factory, repository)

    com = await reader.snapshot(RUN_COM, [GITHUB_COM])
    ghe = await reader.snapshot(RUN_GHE, [GITHUB_ENTERPRISE])
    assert com is not None and ghe is not None

    com_recovery = recovery_document(
        com.rows,
        state=com.projection().state,
        coverage=com.source_coverage,
        occupancy=com.occupancy,
    )
    ghe_recovery = recovery_document(
        ghe.rows,
        state=ghe.projection().state,
        coverage=ghe.source_coverage,
        occupancy=ghe.occupancy,
    )

    # connection A: the failed no-effect delivery with the full ladder
    assert com_recovery["delivery"]["outcome"] == "empty_diff_no_effect"
    assert com_recovery["delivery"]["failed"] is True
    assert _statuses(com_recovery)["exact_resume_applied"] == "present"
    # connection B: delivered, no pause/resume history — its OWN rows only
    assert ghe_recovery["delivery"]["outcome"] == "delivered"
    assert _statuses(ghe_recovery)["pause_requested"] == "absent"
    assert _statuses(ghe_recovery)["exact_resume_applied"] == "absent"
    assert ghe_recovery["hint"] is not None  # unverified candidate → verify

    # the same-name cross-connection read never happens: out-of-scope ≡ unknown
    assert await reader.snapshot(RUN_COM, [GITHUB_ENTERPRISE]) is None
    assert await reader.snapshot(RUN_GHE, [GITHUB_COM]) is None


async def test_the_lane_outcome_slice_maps_only_recorded_markers(session_factory):
    """The reader's additive mapping: the run row carries ``lane_outcome``
    exactly where the #302 markers were journaled — nothing recorded,
    nothing mapped (never an invented outcome)."""
    await _seed(
        session_factory,
        _run(
            RUN_COM,
            "https://github.com",
            evidence={"connection": "https://github.com", "driver_exit": "failed"},
        ),
        _run(RUN_GHE, "https://ghe.corp"),  # no markers at all
    )
    reader = _reader(session_factory)

    com = await reader.snapshot(RUN_COM, [GITHUB_COM])
    ghe = await reader.snapshot(RUN_GHE, [GITHUB_ENTERPRISE])
    assert com is not None and ghe is not None

    # the top-level spelling maps too (the fallback the harness journal uses)
    assert com.rows["run"]["lane_outcome"] == {"driver_exit": "failed"}
    assert com.rows["run"]["lane_outcome"]["driver_exit"] == "failed"
    assert recovery_document(com.rows)["delivery"]["outcome"] == "driver_failed"
    # no recorded marker → no slice, and the honest not-collected-yet outcome
    assert "lane_outcome" not in ghe.rows["run"]
    assert recovery_document(ghe.rows)["delivery"]["outcome"] == "not_collected_yet"
