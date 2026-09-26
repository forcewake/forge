"""Q39-15 (#334) — measured operating limits and safe recovery, drilled.

Three layers, each driving the SHIPPED path (never a private helper):

- **The pure folds** (:mod:`forge.adaptive.ops_limits`) — the four
  ``ops.*`` measures stay SEPARATE records with their own windows;
  unknown windows are counted, never zero-filled; the capped-admission
  verdict derives from execution occupancy only and the intake/slots
  separation is REFUSED when violated; the review-budget distinction
  keeps code, checks and budget three different facts.

- **The shipped operator surface** — the real app, the real operator
  router, the real snapshot reader: the detail route's ``ops_limits``
  block carries the six quantities, the customer state, the four
  measures and the review-budget distinction; an out-of-scope principal
  gets the SAME 404 on the detail and the bundle; an applied resume that
  named a DIFFERENT checkpoint never activates the current one (history
  never relabelled as current state).

- **The negative drills** — disposable infrastructure only (SQLite
  databases and tmp-dir stores the drills themselves build; zero
  network, zero lab containers): a slow runner holding the cap, a
  vendor 429 start (ambiguous — occupancy holds until EVIDENCE), a
  control-plane restart (engine disposed and recreated; occupancy
  resolves by its durable correlation), an unknown native start (intent
  without handle parks draining; only a terminal probe frees it),
  concurrent operator commands (one logical command per sequence race;
  exactly the command matching the CTL-04 world dispatches — the stale
  twin expires with the reason), capped admission at the limit (a
  fourth dispatch parks; the verdict flips with HELD occupancy, never
  with the queued intake count), and retention under a paused active
  checkpoint plus an investigation hold (the pinned checkpoint survives
  the retention pass; held receipts are refused, never pruned).

The committed record and the support agreement are pinned at the bottom:
the record loads strict (no findings, every referenced artifact exists)
and the agreement names its pending-human items.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.admission import (
    AdmissionPolicy,
    ExecutionLease,
    NativeStatus,
    clear_native_start_intent,
    definite_start_refusal,
    lease_occupancy,
    reconcile_draining,
    record_native_handle,
    record_native_start_intent,
    release_lease_with_evidence,
    saturation_report,
    try_acquire_lease,
)
from forge.adaptive.checkpoint_repository import FilesystemCheckpointRepository
from forge.adaptive.credential_audit import (
    RedemptionReceipt,
    prune_expired_redemptions,
    record_redemption,
    retention_holds,
)
from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox
from forge.adaptive.models import ControlCommand
from forge.adaptive.ops_limits import (
    COMMAND_APPLICATION_LATENCY,
    CUSTOMER_STATE_OF_OPERATOR_STATE,
    MANUAL_INTERVENTION_MINUTES,
    NATIVE_OCCUPANCY,
    OPS_MEASURE_NAMES,
    RECOVERY_DURATION,
    RECOVERY_ROUNDS,
    TIME_TO_SAFE_ACTION,
    UNRESOLVED_EFFECT_AGE,
    admission_accounting,
    assert_intake_never_slots,
    assert_measures_separate,
    capped_admission_verdict,
    customer_state,
    delivery_round_block,
    manual_intervention_minutes,
    native_occupancy_measure,
    ops_limits_read_model,
    ops_measures,
    recovery_duration,
    recovery_rounds_measure,
    review_budget_distinction,
    time_to_safe_action_measure,
    unresolved_effect_age_measure,
)
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.api_operator import operator_subject_scope_token
from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import FlowRun
from forge.main import create_app
from forge.models.base import Base
from forge.profile_qualification import load_profile_records, validate_record

ROOT = Path(__file__).resolve().parents[1]
SECRET = "operator-secret"  # noqa: S105 — fake value for tests
NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
PROJECT_ID = 42
REPO_A = "owner/alpha"
REPO_B = "owner/beta"
RUN_A = "a" * 32
RUN_B = "b" * 32
SUBJECT_A = CanonicalSubject(provider_family="github", connection="", native_id=REPO_A)
SUBJECT_B = CanonicalSubject(provider_family="github", connection="", native_id=REPO_B)
CHECKPOINT_A = "e" * 64


# ---------------------------------------------------------------------------
# The pure folds — separation, honest unknowns, the intake/slots guard
# ---------------------------------------------------------------------------

PAUSE = {
    "command_id": "cmd-pause",
    "kind": "pause",
    "status": "checkpointed",
    "created_at": "2026-09-25T10:00:00+00:00",
    "applied_at": "2026-09-25T10:00:02+00:00",
    "actor_origin": "server_authenticated_human",
}
PENDING_STEER = {
    "command_id": "cmd-steer",
    "kind": "steer",
    "status": "received",
    "created_at": "2026-09-25T10:05:00+00:00",
    "applied_at": "",
    "actor_origin": "server_authenticated_human",
}
HUMAN_RESUME = {
    "command_id": "cmd-resume",
    "kind": "resume",
    "status": "applied",
    "created_at": "2026-09-25T10:31:00+00:00",
    "applied_at": "2026-09-25T10:31:01+00:00",
    "actor_origin": "operator_token",
    "checkpoint_ref": f"{RUN_A}@{CHECKPOINT_A}",
}
AUTOMATION_RESUME = {
    "command_id": "cmd-resume-auto",
    "kind": "resume",
    "status": "applied",
    "created_at": "2026-09-25T10:40:00+00:00",
    "applied_at": "2026-09-25T10:40:01+00:00",
    "actor_origin": "automation_reconciler",
}
MATCHED_CHECKPOINT = {
    "checkpoint_id": CHECKPOINT_A,
    "digest": CHECKPOINT_A,
    "committed_at": "2026-09-25T10:01:00+00:00",
    "activated_at": "2026-09-25T10:31:05+00:00",
    "activation": "matched",
    "fence": "held",
}


class TestTheFourMeasures:
    def test_each_measure_is_its_own_record_with_its_own_window(self) -> None:
        rows = {
            "commands": [PAUSE, PENDING_STEER, HUMAN_RESUME],
            "checkpoints": [MATCHED_CHECKPOINT],
        }
        document = ops_measures(rows, occupancy=[], limit=3, as_of="2026-09-25T11:00:00+00:00")

        assert sorted(key for key in document if key.startswith("ops.")) == sorted(
            OPS_MEASURE_NAMES
        )
        latency = document[COMMAND_APPLICATION_LATENCY]
        recovery = document[RECOVERY_DURATION]
        intervention = document[MANUAL_INTERVENTION_MINUTES]
        # command latency: only APPLIED commands; the pending steer is
        # counted, never zero-filled into the population
        assert latency["population"] == 2
        assert latency["not_yet_applied"] == 1
        assert {sample["seconds"] for sample in latency["samples"]} == {2.0, 1.0}
        # recovery: pause applied → matched activation (1863s), its own window
        assert recovery["population"] == 1
        assert recovery["samples"][0]["seconds"] == 1863.0
        assert recovery["window"] != latency["window"]
        # manual intervention: 30 minutes WAITING for the human, a third window
        assert intervention["population"] == 1
        assert intervention["samples"][0]["minutes"] == 30.0
        assert intervention["unit"] == "min"
        assert len({latency["window"], recovery["window"], intervention["window"]}) == 3

    def test_the_gauge_never_blends_with_the_durations(self) -> None:
        gauge = native_occupancy_measure(
            [
                {
                    "lease_id": "l1",
                    "occupancy": "dispatched_unknown",
                    "acquired_at": "2026-09-25T10:50:00+00:00",
                    "released_at": "",
                },
                {
                    "lease_id": "l2",
                    "occupancy": "native_running",
                    "acquired_at": "2026-09-25T09:00:00+00:00",
                    "released_at": "",
                },
                {
                    "lease_id": "l3",
                    "occupancy": "observed_terminal",
                    "acquired_at": "2026-09-25T08:00:00+00:00",
                    "released_at": "2026-09-25T09:30:00+00:00",
                },
            ],
            limit=3,
            as_of="2026-09-25T11:00:00+00:00",
        )
        assert gauge["kind"] == "gauge"
        assert gauge["counts"] == {
            "dispatched_unknown": 1,
            "native_running": 1,
            "observed_terminal": 1,
        }
        assert gauge["occupied_vs_limit"] == {"occupied": 2, "limit": 3, "at_limit": False}
        assert gauge["occupancy_unknown"] == 1
        assert gauge["unknown_ages"][0]["age_seconds"] == 600.0
        assert "acquired_at" in gauge["age_basis"]  # the lower-bound honesty

    def test_unknown_windows_are_counted_never_zero_filled(self) -> None:
        # a pause with no matched activation yet: incomplete, aged — never 0
        recovery = recovery_duration([PAUSE], [], as_of="2026-09-25T11:00:00+00:00")
        assert recovery["population"] == 0
        assert recovery["incomplete_cycles"][0]["age_seconds"] == 3598.0
        # a safely-paused state with no human resume yet: an OPEN window
        intervention = manual_intervention_minutes(
            [], [MATCHED_CHECKPOINT], as_of="2026-09-25T11:00:00+00:00"
        )
        assert intervention["population"] == 0
        assert intervention["open_windows"][0]["open_wait_minutes"] == 59.0
        # an unmatched activation is never a recovery sample
        unmatched = dict(MATCHED_CHECKPOINT, activation="unmatched-command")
        recovery = recovery_duration([PAUSE], [unmatched], as_of="2026-09-25T11:00:00+00:00")
        assert recovery["population"] == 0
        assert recovery["unmatched_activations"] == 1

    def test_automation_resumes_are_excluded_from_manual_intervention(self) -> None:
        intervention = manual_intervention_minutes(
            [AUTOMATION_RESUME], [MATCHED_CHECKPOINT], as_of="2026-09-25T11:00:00+00:00"
        )
        assert intervention["population"] == 0
        assert intervention["automation_resumes_excluded"] == 1

    def test_an_unselected_section_renders_unknown_never_empty(self) -> None:
        document = ops_measures({}, occupancy=None, as_of="2026-09-25T11:00:00+00:00")
        for name in OPS_MEASURE_NAMES:
            record = document[name]
            assert record["coverage"] == "unknown", name
            assert record["population"] == 0 and record["samples"] == []

    def test_a_blended_field_is_refused(self) -> None:
        document = ops_measures({}, occupancy=[], as_of="2026-09-25T11:00:00+00:00")
        blended = dict(document, **{"ops.combined_latency_and_occupancy": {}})
        with pytest.raises(ValueError, match="blended"):
            assert_measures_separate(blended)
        inner = dict(document)
        inner[RECOVERY_DURATION] = dict(inner[RECOVERY_DURATION], total=1863.0)
        with pytest.raises(ValueError, match="blended"):
            assert_measures_separate(inner)
        assert_measures_separate(document)  # the honest shape passes


class TestAdmissionAccounting:
    def test_intake_and_slots_are_different_counters_side_by_side(self) -> None:
        accounting = admission_accounting(
            rejected_requests=2, admitted_work=5, held=3, draining=1, completed=4, limit=3
        )
        assert accounting["intake"]["unit"] == "requests"
        assert accounting["execution"]["unit"] == "slots"
        assert accounting["intake"]["admitted_queued"] == 5  # queued intake...
        assert accounting["execution"]["held"] == 3  # ...is not a slot claim

    def test_the_capped_verdict_is_derived_from_occupancy_only(self) -> None:
        assert capped_admission_verdict(held=3, limit=3)["capped"] is True
        assert capped_admission_verdict(held=2, limit=3)["capped"] is False
        # unknown limit / unknown occupancy render unknown, never confident
        assert capped_admission_verdict(held=3, limit=0)["capped"] is None
        assert capped_admission_verdict(held=None, limit=3)["capped"] is None

    def test_the_intake_derived_verdict_is_refused(self) -> None:
        honest = admission_accounting(rejected_requests=0, admitted_work=9, held=2, limit=3)
        assert honest["capped_admission"]["capped"] is False  # 9 queued, 2 held
        forged = dict(honest)
        forged["capped_admission"] = dict(honest["capped_admission"], capped=True)
        with pytest.raises(Exception, match="intake-derived"):
            assert_intake_never_slots(forged)


class TestCustomerState:
    def test_every_projection_state_maps_onto_the_customer_vocabulary(self) -> None:
        # TOTAL over the projection's closed vocabulary (a projection
        # carries exactly one state; wedged/stale/dead ARE states)
        from forge.adaptive.operator_view import OPERATOR_STATES

        assert set(OPERATOR_STATES) == set(CUSTOMER_STATE_OF_OPERATOR_STATE)
        words = {customer_state(state)["state"] for state in OPERATOR_STATES}
        assert words == {"queued", "progressing", "paused", "unverified", "verified", "blocked"}

    def test_the_wedged_and_dead_overlays_reclassify_to_blocked(self) -> None:
        wedged = customer_state("executing", ("wedged",))
        dead = customer_state("cancelled", ("dead",))
        assert wedged["state"] == "blocked" and wedged["overlays"] == ["wedged"]
        assert dead["state"] == "blocked" and dead["overlays"] == ["dead"]

    def test_an_unmapped_state_is_a_modelling_error(self) -> None:
        with pytest.raises(ValueError, match="total"):
            customer_state("warp-speed")


class TestReviewBudgetDistinction:
    def test_a_failed_review_budget_is_not_lost_code_or_failed_checks(self) -> None:
        rows = {
            "run": {
                "id": RUN_A,
                "status": "blocked",
                "blocked_reason": "budget_exhausted at the reviewer leg",
                "candidate_shas": ["c" * 40],
            },
            "commands": [],
            "checkpoints": [MATCHED_CHECKPOINT],
            "verifications": [
                {"verification_id": "v1", "result": "passed", "candidate_sha": "c" * 40, "at": "t"}
            ],
        }
        distinction = review_budget_distinction(rows)
        assert distinction["failed_review_budget"] is True
        assert "NOT the code" in distinction["what_failed"]
        assert distinction["code"]["candidate_sha"] == "c" * 40
        assert distinction["code"]["checkpoint"]["checkpoint_id"] == CHECKPOINT_A
        assert distinction["independent_checks"][0]["result"] == "passed"
        assert "review_only_continuation" in distinction["recovery"]
        assert "top_up" in distinction["recovery"]

    def test_an_implementation_budget_refusal_never_claims_code_was_delivered(self) -> None:
        rows = {
            "run": {
                "id": RUN_A,
                "status": "blocked",
                "blocked_reason": "budget_exhausted during planning",
                "candidate_shas": [],
            },
            "commands": None,
            "checkpoints": [],
            "verifications": None,
        }
        distinction = review_budget_distinction(rows)
        assert distinction["failed_review_budget"] is False
        assert distinction["budget_refusal"] is True
        assert distinction["code"]["candidate_sha"] is None
        assert "no candidate recorded" in distinction["code"]["candidate_state"]


# ---------------------------------------------------------------------------
# The shipped operator surface — the real app, the real router
# ---------------------------------------------------------------------------


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
        "project_id": PROJECT_ID,
        "provider": "github",
        "github_repo_full_name": repo,
        "status": "executing",
        "base_sha": "b" * 40,
        "candidate_shas": [],
        "plan_digest": "p" * 64,
        "evidence": {},
        "created_at": NOW - timedelta(hours=3),
        "updated_at": NOW - timedelta(minutes=10),
    }
    values.update(over)
    return FlowRun(**values)


class RecordingRepository:
    """The injected checkpoint authority — reads only (the entry the
    resume command's checkpoint_ref names)."""

    def __init__(self, entry: dict | None = None) -> None:
        self.entry_document = entry

    async def entry(self, work_id: str) -> dict | None:
        return dict(self.entry_document) if self.entry_document else None

    async def put(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003 — recorder
        return None

    async def read(self, work_id: str):
        return None

    async def lookup_outcome(self, work_id: str):
        return None

    async def authority(self) -> str:
        return "recording"

    async def pin(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003 — recorder
        return False

    async def unpin(self, *args, **kwargs) -> int:  # noqa: ANN002, ANN003 — recorder
        return 0

    async def pins(self, work_id: str | None = None) -> list[dict]:
        return []


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
            "checkpoint_id": CHECKPOINT_A,
            "sequence": 2,
            "files": 2,
            "uploaded_at": (NOW - timedelta(minutes=60)).isoformat(),
        }
    )
    app.state.operator_checkpoint_repository = recording
    return recording


def _scope_headers(subjects: list[CanonicalSubject]) -> dict[str, str]:
    return {"Authorization": f"Bearer {operator_subject_scope_token(SECRET, subjects)}"}


def _subject_query(subjects: list[CanonicalSubject]) -> str:
    return "&".join(f"subject={entry.subject_id()}" for entry in subjects)


async def _seed(app, *rows) -> None:
    async with app.state.session_factory() as session:
        session.add_all(rows)
        await session.commit()


async def _row_count(app, model) -> int:
    async with app.state.session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def _command_row(run_id: str, seq: int, kind: str, **over) -> ControlCommandRow:
    values: dict[str, Any] = {
        "id": f"cmd-{run_id[:6]}-{seq}",
        "work_id": run_id,
        "run_id": run_id,
        "kind": kind,
        "payload": {"run_id": run_id},
        "status": "received",
        "sequence": seq,
        "dedup_key": f"key-{run_id[:6]}-{seq}",
        "actor_ref": "human:op",
        "actor_origin": "server_authenticated_human",
        "created_at": NOW - timedelta(minutes={1: 60, 2: 45, 3: 30}[seq]),
    }
    values.update(over)
    return ControlCommandRow(**values)


async def _seed_ops_shape(app, *, mismatched_resume: bool = False) -> None:
    """The recovery shape: pause booked, checkpoint committed, a resume
    applied — naming THIS checkpoint (or, on *mismatched_resume*, a
    different one: history that must never relabel the current)."""
    other = "f" * 64
    resume_ref = f"{RUN_A}@{other if mismatched_resume else CHECKPOINT_A}"
    await _seed(
        app,
        _run(RUN_A, REPO_A, status="planning", status_reason=""),
        _run(RUN_B, REPO_B, status="blocked", status_reason="fair_use_denied: queue_full"),
        _command_row(
            RUN_A,
            1,
            "pause",
            status="checkpointed",
            applied_at=NOW - timedelta(minutes=58),
        ),
        _command_row(
            RUN_A,
            2,
            "steer",
            payload={"run_id": RUN_A, "text": "use the other approach"},
        ),
        _command_row(
            RUN_A,
            3,
            "resume",
            status="applied",
            actor_origin="operator_token",
            payload={"run_id": RUN_A, "checkpoint_ref": resume_ref},
            applied_at=NOW - timedelta(minutes=29),
        ),
    )


async def test_the_detail_renders_the_ops_limits_read_model(app, client, repository) -> None:
    await _seed_ops_shape(app)
    app.state.operator_admission_policy = AdmissionPolicy(max_active_per_project=2)

    response = await client.get(
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}", headers=_scope_headers([SUBJECT_A])
    )
    assert response.status_code == 200
    ops = response.json()["ops_limits"]

    # the six quantities, each named and coverage-worded
    assert ops["schema"] == "forge.ops.limits/1"
    assert ops["customer_state"]["state"] in {"progressing", "paused"}
    assert ops["current_attempt"]["attempt_id"]
    assert ops["native_occupancy"]["measure"] == NATIVE_OCCUPANCY
    assert ops["exact_checkpoint"]["checkpoint_id"] == CHECKPOINT_A
    assert isinstance(ops["unresolved_effects"], list)
    assert {check["check"] for check in ops["required_checks"]} == {
        "independent_verification",
        "closing_review",
    }
    coverage = ops["accounting_coverage"]
    assert coverage["sources"]["commands"] == "present"
    assert coverage["occupancy_limit_known"] is True

    # the four measures, separate records with their own windows
    measures = ops["measures"]
    assert sorted(measures) == sorted([*OPS_MEASURE_NAMES, "schema", "as_of", "separation"])
    latency = measures[COMMAND_APPLICATION_LATENCY]
    assert latency["population"] == 2 and latency["not_yet_applied"] == 1
    assert {sample["seconds"] for sample in latency["samples"]} == {120.0, 60.0}
    assert measures[MANUAL_INTERVENTION_MINUTES]["population"] == 1
    assert measures[MANUAL_INTERVENTION_MINUTES]["samples"][0]["minutes"] == 30.0
    assert measures[RECOVERY_DURATION]["samples"][0]["seconds"] == 29 * 60

    # history separation is stated, not just true
    assert "never relabelled" in ops["history_separation"]


async def test_the_admission_accounting_keeps_intake_and_slots_separate(
    app, client, repository
) -> None:
    await _seed_ops_shape(app)
    app.state.operator_admission_policy = AdmissionPolicy(max_active_per_project=2)
    await _seed(
        app,
        ExecutionLease(
            id=uuid4().hex,
            project_id=PROJECT_ID,
            provider="github",
            run_id=RUN_A,
            slot=1,
            acquired_at=NOW - timedelta(minutes=30),
            native_intent_ref="github:workflow:owner/alpha/ci@lane",
            native_handle="github:workflow:490",
        ),
        ExecutionLease(
            id=uuid4().hex,
            project_id=PROJECT_ID,
            provider="github",
            run_id=None,
            slot=2,
            acquired_at=NOW - timedelta(minutes=25),
            released_at=NOW - timedelta(minutes=5),
            release_reason="reconciled: native job terminal",
        ),
    )

    response = await client.get(
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}", headers=_scope_headers([SUBJECT_A])
    )
    assert response.status_code == 200
    accounting = response.json()["ops_limits"]["accounting_coverage"]["admission"]
    assert accounting["intake"]["rejected_requests"] == 1  # RUN_B's fair-use park
    assert accounting["execution"]["held"] == 1
    assert accounting["execution"]["completed"] == 1
    assert accounting["capped_admission"]["capped"] is False


async def test_an_out_of_scope_principal_gets_404_on_every_surface(app, client, repository) -> None:
    """The negative drill: a private evidence bundle requested by an
    out-of-scope principal — the detail AND the bundle answer the SAME
    404, indistinguishable from unknown, and the out-of-scope run never
    appears in the authorized list."""
    await _seed_ops_shape(app)

    listed = await client.get(
        f"/operator/runs?{_subject_query([SUBJECT_A])}", headers=_scope_headers([SUBJECT_A])
    )
    assert listed.status_code == 200
    assert [run["run_id"] for run in listed.json()["runs"]] == [RUN_A]

    for suffix in ("", "/support-bundle"):
        response = await client.get(
            f"/operator/runs/{RUN_B}{suffix}?{_subject_query([SUBJECT_A])}",
            headers=_scope_headers([SUBJECT_A]),
        )
        assert response.status_code == 404, suffix
    # a token minted for B cannot DECLARE A's scope either
    mismatched = await client.get(
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}",
        headers=_scope_headers([SUBJECT_B]),
    )
    assert mismatched.status_code == 403


async def test_history_is_never_relabelled_as_current_state(app, client, repository) -> None:
    """An applied resume that named a DIFFERENT checkpoint never
    activates the current one: the exact checkpoint's activation reads
    ``unmatched-command``, the recovery measure COUNTS it as unmatched
    (never a sample), and the current checkpoint stays unactivated."""
    await _seed_ops_shape(app, mismatched_resume=True)

    response = await client.get(
        f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}", headers=_scope_headers([SUBJECT_A])
    )
    assert response.status_code == 200
    ops = response.json()["ops_limits"]
    assert ops["exact_checkpoint"]["activation"] == "unmatched-command"
    assert ops["exact_checkpoint"]["activated_at"] is None
    measures = ops["measures"]
    assert measures[RECOVERY_DURATION]["population"] == 0
    assert measures[RECOVERY_DURATION]["unmatched_activations"] == 1


async def test_rendering_the_ops_block_performs_zero_writes(app, client, repository) -> None:
    await _seed_ops_shape(app)
    before = {model: await _row_count(app, model) for model in (FlowRun, ControlCommandRow)}
    for _ in range(2):  # a repeated GET moves nothing
        response = await client.get(
            f"/operator/runs/{RUN_A}?{_subject_query([SUBJECT_A])}",
            headers=_scope_headers([SUBJECT_A]),
        )
        assert response.status_code == 200
    after = {model: await _row_count(app, model) for model in (FlowRun, ControlCommandRow)}
    assert before == after


# ---------------------------------------------------------------------------
# The negative drills — disposable infrastructure, zero network
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


POLICY = AdmissionPolicy(max_active_per_project=3, max_queued_runs=10)


class VendorThrottled(Exception):
    """The vendor answered 429 on a start call (ambiguous acceptance)."""


class SlowNativeLane:
    """A fake native lane with INJECTABLE faults — realistic runner
    latency and vendor throttling (the ops_drills pattern, minimal for
    these drills). Occupancy resolves ONLY through probes."""

    def __init__(self, *, start_latency: float = 0.01) -> None:
        self.start_latency = start_latency
        self.starts = 0
        self.statuses: dict[str, NativeStatus] = {}

    async def start(self, key: str) -> str:
        await asyncio.sleep(self.start_latency)  # the slow runner's real start cost
        self.starts += 1
        self.statuses[key] = NativeStatus.RUNNING
        return f"{key}#job-{self.starts}"

    async def finish(self, key: str) -> None:
        self.statuses[key] = NativeStatus.TERMINAL

    async def probe(self, key: str) -> NativeStatus:
        return self.statuses.get(key, NativeStatus.UNKNOWN)


async def _seed_run(db, run_id: str, status: str = "validating", **over) -> str:
    values: dict[str, Any] = {
        "id": run_id,
        "project_id": PROJECT_ID,
        "provider": "gitlab",
        "status": status,
        "status_reason": "",
        "evidence": {},
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=30),
    }
    values.update(over)
    async with db() as session:
        session.add(FlowRun(**values))
        await session.commit()
    return run_id


class TestCappedAdmissionUnderSlowRunnerAndThrottling:
    async def test_the_cap_holds_and_the_verdict_follows_occupancy_not_intake(self, db) -> None:
        """Slow runners hold all three slots; the fourth dispatch parks;
        releasing ONE slot from evidence flips the verdict while the
        QUEUED INTAKE GREW — the drill's one pinned sentence."""
        lane = SlowNativeLane(start_latency=0.01)
        run_ids = [await _seed_run(db, f"run-{slot}") for slot in range(4)]
        for run_id in run_ids[:3]:
            lease = await try_acquire_lease(POLICY, PROJECT_ID, db, run_id=run_id, provider="lab")
            assert lease is not None
            await record_native_start_intent(db, run_id, f"gitlab:pipeline:{run_id}@lane")
            handle = await lane.start(f"gitlab:pipeline:{run_id}@lane")
            await record_native_handle(lease.lease_id, handle, db)

        # the cap: the fourth dispatch parks (no lease — honest None)
        assert (
            await try_acquire_lease(POLICY, PROJECT_ID, db, run_id=run_ids[3], provider="lab")
            is None
        )
        report = await saturation_report(POLICY, PROJECT_ID, db, provider="lab")
        assert report["execution.occupied_vs_limit"] == {
            "occupied": 3,
            "limit": 3,
            "available": 0,
            "at_limit": True,
        }
        accounting = admission_accounting(
            rejected_requests=0, admitted_work=1, held=3, draining=0, completed=0, limit=3
        )
        assert accounting["capped_admission"]["capped"] is True

        # one slot frees from EVIDENCE (its native job observed terminal)
        await lane.finish(f"gitlab:pipeline:{run_ids[0]}@lane")
        outcome = await release_lease_with_evidence(
            db, run_ids[0], reason="terminal", native_terminal=True
        )
        assert outcome.released == 1
        # the intake count GREW (9 queued now) — the verdict follows
        # occupancy and flips; the intake count never claimed a slot
        accounting = admission_accounting(
            rejected_requests=0, admitted_work=9, held=2, draining=0, completed=1, limit=3
        )
        assert accounting["capped_admission"]["capped"] is False
        # and the parked dispatch now takes the freed slot
        assert await try_acquire_lease(POLICY, PROJECT_ID, db, run_id=run_ids[3], provider="lab")

    async def test_a_throttled_start_keeps_the_slot_until_evidence(self, db) -> None:
        run_id = await _seed_run(db, "run-429")
        lease = await try_acquire_lease(POLICY, PROJECT_ID, db, run_id=run_id, provider="lab")
        assert lease is not None
        await record_native_start_intent(db, run_id, "gitlab:pipeline:run-429@lane")

        # the vendor answers 429: NOT a definite refusal — the start may
        # have been accepted before the throttle, so occupancy stays
        # dispatched_unknown (the slot is held), never freed on the 429
        assert definite_start_refusal(429) is False
        assert definite_start_refusal(404) is True  # the definite contrast

        async with db() as session:
            row = await session.get(ExecutionLease, lease.lease_id)
        assert row is not None and lease_occupancy(row).value == "dispatched_unknown"

        # only a terminal probe observation frees it
        released = await release_lease_with_evidence(db, run_id, reason="terminal")
        assert released.released == 0 and released.drained == 1  # parked, not freed
        assert await reconcile_draining(db, native_status=lambda key: NativeStatus.TERMINAL) == 1

        # the pre-call abort spelling: a DEFINITE refusal clears the
        # intent so a never-started job never parks a slot (AT-06)
        run2 = await _seed_run(db, "run-404")
        assert await try_acquire_lease(POLICY, PROJECT_ID, db, run_id=run2, provider="lab")
        await record_native_start_intent(db, run2, "gitlab:pipeline:run-404@lane")
        assert await clear_native_start_intent(db, run2, "gitlab:pipeline:run-404@lane") == 1
        outcome = await release_lease_with_evidence(db, run2, reason="definite 404 refusal")
        assert outcome.released == 1  # proven never-dispatched — freed now


class TestControlPlaneRestart:
    async def test_occupancy_survives_the_restart_and_resolves_by_correlation(
        self, tmp_path
    ) -> None:
        """The restart drill: the engine is disposed and recreated over
        the SAME durable store (the process died; the database did not).
        Occupancy identities survive, an unknown native start parks
        draining, and the reconciler resolves it by its durable
        native_intent_ref through the fresh engine."""
        url = f"sqlite+aiosqlite:///{tmp_path}/restart.db"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        run_id = await _seed_run(factory, "run-restart")
        assert await try_acquire_lease(POLICY, PROJECT_ID, factory, run_id=run_id, provider="lab")
        # the start response was LOST: intent persisted, no handle
        await record_native_start_intent(factory, run_id, "gitlab:pipeline:run-restart@lane")
        await engine.dispose()  # the control plane dies here

        # the fresh process: same store, fresh engine, nothing in memory
        revived = create_async_engine(url)
        fresh = async_sessionmaker(revived, expire_on_commit=False)
        report = await saturation_report(POLICY, PROJECT_ID, fresh, provider="lab")
        assert report["native_start.unknown_count"] == 1
        assert report["execution.occupied_vs_limit"]["occupied"] == 1
        # the run's local terminal transition WITHOUT native evidence
        # parks the lease draining — never frees on the local verdict
        parked = await release_lease_with_evidence(fresh, run_id, reason="run terminal")
        assert parked.drained == 1 and parked.released == 0

        # the reconciler resolves by the durable correlation prefix
        async def probe(key: str) -> NativeStatus:
            assert key.startswith("gitlab:")
            return NativeStatus.TERMINAL

        assert await reconcile_draining(fresh, native_status=probe) == 1
        report = await saturation_report(POLICY, PROJECT_ID, fresh, provider="lab")
        assert report["execution.occupied_vs_limit"]["occupied"] == 0
        await revived.dispose()


class TestConcurrentOperatorCommands:
    async def test_one_command_applies_per_world_and_the_stale_twin_expires(self, db) -> None:
        """Two operators race: both commands enter the durable mailbox;
        the CTL-04 CAS means exactly the command matching the CURRENT
        world dispatches — the stale twin EXPIRES with the reason, and a
        redelivered duplicate is refused atomically (created=False)."""
        run_id = await _seed_run(db, "run-race")
        mailbox = PostgresMailbox(db)

        def command(command_id: str, sequence: int, revision: int | None) -> ControlCommand:
            return ControlCommand.model_validate(
                {
                    "schema": "forge.proposal.control-command/1",
                    "command_id": command_id,
                    "work_id": run_id,
                    "sequence": sequence,
                    "kind": "steer",
                    "actor_ref": f"human:{command_id}",
                    "actor_origin": "server_authenticated_human",
                    "idempotency_key": f"key-{command_id}",
                    "payload": {"run_id": run_id, "text": "approach"},
                    "status": "received",
                    "expected_plan_revision": revision,
                    "expected_execution_epoch": 0,
                }
            )

        first, created = await mailbox.submit(command("cmd-a", 1, 2))
        assert created is True
        # the redelivery: a fresh submission of the SAME logical command
        # (new sequence slot, SAME dedup key) loses the insert race and
        # READS the winner — created=False, nothing changed
        twin, created_again = await mailbox.submit(command("cmd-a", 2, 2))
        assert created_again is False and twin.command_id == first.command_id

        second, _ = await mailbox.submit(command("cmd-b", 3, 3))
        scopes = {
            "server_authenticated_human": ("human:cmd-a", "human:cmd-b"),
            "operator_token": (),
            "automation_reconciler": (),
        }
        await mailbox.authorize(first.command_id, scopes)
        await mailbox.authorize(second.command_id, scopes)

        # the world moved to plan revision 3 (operator B's plan landed):
        # B's command matches, A's was written against revision 2
        taken = await mailbox.dispatch(
            second.command_id, current_plan_revision=3, current_execution_epoch=0
        )
        assert taken.status == "dispatching"
        expired = await mailbox.dispatch(
            first.command_id, current_plan_revision=3, current_execution_epoch=0
        )
        assert expired.status == "expired"

        async with db() as session:
            row = await session.scalar(
                select(ControlCommandRow).where(ControlCommandRow.id == first.command_id)
            )
        assert row is not None and row.status == "expired"
        assert any("stale expectations" in str(entry) for entry in (row.journal or []))

    async def test_a_sequence_race_keeps_one_logical_command(self, tmp_path) -> None:
        """Two operators submit CONCURRENTLY on separate connections: the
        per-work sequence uniqueness keeps exactly ONE logical command;
        the loser is refused loudly (pre-read or insert race — both are
        the honest refusal), never silently merged."""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        run_id = await _seed_run(factory, "run-seq")
        mailbox = PostgresMailbox(factory)

        def command(command_id: str, sequence: int) -> ControlCommand:
            return ControlCommand.model_validate(
                {
                    "schema": "forge.proposal.control-command/1",
                    "command_id": command_id,
                    "work_id": run_id,
                    "sequence": sequence,
                    "kind": "pause",
                    "actor_ref": f"human:{command_id}",
                    "actor_origin": "server_authenticated_human",
                    "idempotency_key": f"key-{command_id}",
                    "payload": {"run_id": run_id},
                    "status": "received",
                }
            )

        # both racers read max(sequence)=0 and submit sequence 1
        results = await asyncio.gather(
            mailbox.submit(command("cmd-r1", 1)),
            mailbox.submit(command("cmd-r2", 1)),
            return_exceptions=True,
        )
        landed = [entry for entry in results if not isinstance(entry, BaseException)]
        refused = [entry for entry in results if isinstance(entry, BaseException)]
        assert len(landed) == 1 and len(refused) == 1  # one winner, one loud refusal
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ControlCommandRow).where(ControlCommandRow.work_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1  # exactly one logical command exists
        await engine.dispose()


class TestRetentionUnderPausedCheckpointAndInvestigationHold:
    async def test_the_pinned_paused_checkpoint_survives_the_retention_pass(self, tmp_path) -> None:
        """A PAUSED run's active checkpoint is PINNED (the persisted
        continuation decision): the retention pass removes the superseded
        row but the pinned checkpoint AND its blobs survive."""
        repository = FilesystemCheckpointRepository(tmp_path / "cas")
        work_id = "pausedwork0000000000000000000000f"

        def payload(sequence: int) -> tuple[bytes, dict[str, bytes], str]:
            data = f"v{sequence}\n".encode()
            manifest = json.dumps(
                {
                    "schema": "forge.wip.manifest/2",
                    "work_id": work_id,
                    "sequence": sequence,
                    "source_oids": {"attempt_base": "e" * 40},
                    "files": {
                        "src/app.py": {
                            "digest": hashlib.sha256(data).hexdigest(),
                            "mode": 0o644,
                            "role": "new",
                        }
                    },
                    "deletions": [],
                }
            ).encode()
            return (
                manifest,
                {hashlib.sha256(data).hexdigest(): data},
                hashlib.sha256(manifest).hexdigest(),
            )

        pinned_id = ""
        for sequence in (1, 2, 3):
            manifest, blobs, checkpoint_id = payload(sequence)
            await repository.put(work_id, checkpoint_id, manifest, blobs)
            if sequence == 2:  # the PAUSED run's active checkpoint
                pinned_id = checkpoint_id
                assert await repository.pin(work_id, checkpoint_id, "pause fence: safely paused")

        removed = await repository.apply_retention(work_id, keep_last=1)
        assert removed == 1  # the superseded row went; the pinned one stayed
        pins = await repository.pins(work_id)
        assert any(row.get("checkpoint_id") == pinned_id for row in pins)
        served = await repository.read(work_id)
        assert served is not None  # the active checkpoint's bytes still resolve
        active_manifest, _blobs = served
        assert json.loads(active_manifest)["sequence"] == 3  # the ACTIVE survives too

    async def test_held_receipts_are_refused_never_pruned(self, db) -> None:
        """The #322 retention holds: a non-terminal (paused) run holds
        its receipts; an investigation hold holds them REGARDLESS of
        status; only an unheld terminal work's expired receipts prune."""
        paused = await _seed_run(db, "workpaused")  # non-terminal: the paused run
        investigated = await _seed_run(
            db,
            "workprobe",
            status="failed",
            evidence={"credential_investigation_open": {"by": "op", "at": "now"}},
        )
        free = await _seed_run(db, "workfree", status="cancelled")
        expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
        for work_id in (paused, investigated, free):
            await record_redemption(
                db,
                RedemptionReceipt(
                    receipt_id=f"rcpt-{work_id}",
                    work_id=work_id,
                    route="gitlab-protected-variable",
                    credential_ref="lane-token",
                    expires_at=expiry,
                ),
            )

        assert (await retention_holds(db, paused)).reasons == ("active_attempt",)
        assert "investigation" in (await retention_holds(db, investigated)).reasons
        assert (await retention_holds(db, free)).reasons == ()

        outcome = await prune_expired_redemptions(db)
        assert outcome["deleted"] == 1  # only the unheld terminal work
        assert set(outcome["held_works"]) == {paused, investigated}


# ---------------------------------------------------------------------------
# The committed record and the support agreement
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_ops_limits_record_loads_strict_with_live_references() -> None:
    records = {record.record_id: record for record in load_profile_records(ROOT)}
    record = records["gitlab-ce-v1-Q39-15-ops-limits-2026-09-25"]
    assert record.legacy is False  # strict: any finding would have refused the load
    assert (
        validate_record(_load_json(ROOT / "qualification/records/ops-limits-2026-09-25.json")) == []
    )
    for ref in record.evidence_refs:
        assert (ROOT / ref).exists(), f"dangling evidence ref {ref}"
    # the record sorts before the composition-v2 record and changes
    # NOTHING about which record speaks for the profile (the per-release
    # machine record at the tree version stays the latest, the v2 record
    # stays `supported`)
    from forge import __version__

    gitlab_ids = [r.record_id for r in load_profile_records(ROOT) if r.profile == "gitlab-ce-v1"]
    assert gitlab_ids[-1] == f"gitlab-ce-v1@{__version__}"
    assert gitlab_ids.index("gitlab-ce-v1-Q39-15-ops-limits-2026-09-25") < gitlab_ids.index(
        "gitlab-ce-v1-composition-v2-2026-09-25"
    )


def test_the_support_agreement_names_its_pending_human_items() -> None:
    text = (ROOT / "docs/operations/support-agreement.md").read_text(encoding="utf-8")
    assert "pending-human" in text
    assert "second operator" in text.lower()
    assert "HISTORY-CLEANUP-PLAN" in text
    assert "excluded" in text.lower()  # the excluded failure domains
    for name in OPS_MEASURE_NAMES:  # the four measures are named, each separately
        assert name in text


# ---------------------------------------------------------------------------
# R40-13 (#349) — the delivery-round read-model arm: the operator.*
# observability records and the lineage customer state
# ---------------------------------------------------------------------------

R40_ROUND = {
    "round_id": "rr-1",
    "parent_run_id": RUN_A,
    "child_run_id": RUN_B,
    "root_run_id": RUN_A,
    "round_number": 2,
    "note_id": "918",
    "base_head_sha": "c" * 40,
    "decision_id": "corr-1",
    "status": "dispatched",
    "status_reason": "",
    "created_at": "2026-09-25T11:00:00+00:00",
    "updated_at": "2026-09-25T11:05:00+00:00",
}
R40_READY_RUN = {
    "id": RUN_A,
    "status": "ready_for_human",
    "base_sha": "b" * 40,
    "candidate_shas": ["c" * 40],
    "plan_digest": "p" * 64,
    "evidence": {"verification": {"status": "passed", "tested_oid": "c" * 40}},
    "blocked_reason": "",
    "cancel_requested": False,
    "created_at": "2026-09-25T09:00:00+00:00",
    "updated_at": "2026-09-25T10:00:00+00:00",
}


class TestOperatorObservabilityRecords:
    def test_recovery_rounds_counts_the_lineage_rounds(self) -> None:
        record = recovery_rounds_measure({"rounds": [R40_ROUND], "run": R40_READY_RUN})

        assert record["measure"] == RECOVERY_ROUNDS
        assert record["recorded"] == 1
        assert record["open"] == 1
        assert record["newest"] == "round:2:corr-1"
        assert record["kind"] == "gauge"

    def test_recovery_rounds_reads_the_child_fragment_without_the_table(self) -> None:
        child_run = {
            **R40_READY_RUN,
            "id": RUN_B,
            "status": "proposing",
            "candidate_shas": [],
            "evidence": {
                "review_round": {
                    "parent_run_id": RUN_A,
                    "root_run_id": RUN_A,
                    "round_number": 2,
                    "decision_id": "corr-1",
                }
            },
        }
        record = recovery_rounds_measure({"run": child_run})  # no rounds section

        assert record["coverage"] == "present"
        assert record["recorded"] == 1
        assert record["newest"] == "round:2:corr-1"

    def test_recovery_rounds_renders_unknown_never_zero(self) -> None:
        record = recovery_rounds_measure({"run": R40_READY_RUN})

        assert record["coverage"] == "unknown"
        assert record["population"] == 0
        assert "never a confident zero" in record["reason"]

    def test_unresolved_effect_ages_are_per_row_lower_bounds(self) -> None:
        record = unresolved_effect_age_measure(
            {
                "publications": [
                    {
                        "operation_key": "op-1",
                        "status": "dispatched",
                        "at": "2026-09-25T10:00:00+00:00",
                    },
                    {
                        "operation_key": "op-2",
                        "status": "committed",
                        "at": "2026-09-25T09:00:00+00:00",
                    },
                    {
                        "operation_key": "op-3",
                        "status": "unknown",
                        "at": "2026-09-25T10:30:00+00:00",
                    },
                ]
            },
            as_of="2026-09-25T11:00:00+00:00",
        )

        assert record["measure"] == UNRESOLVED_EFFECT_AGE
        assert record["unresolved"] == 2  # the committed intent is not unresolved
        ages = {sample["operation_key"]: sample["age_seconds"] for sample in record["samples"]}
        assert ages["op-1"] == 3600.0
        assert ages["op-3"] == 1800.0

    def test_unresolved_effect_age_renders_unknown_when_not_queried(self) -> None:
        record = unresolved_effect_age_measure({}, as_of="2026-09-25T11:00:00+00:00")

        assert record["coverage"] == "unknown"

    def test_time_to_safe_action_is_a_stated_lower_bound(self) -> None:
        from forge.adaptive.operator_view import initial_projection

        projection = initial_projection(
            {
                "run": R40_READY_RUN,
                "verifications": [
                    {
                        "verification_id": "v1",
                        "result": "passed",
                        "candidate_sha": "c" * 40,
                        "at": "2026-09-25T10:45:00+00:00",
                    }
                ],
            },
            NOW,
        )
        record = time_to_safe_action_measure(projection, as_of="2026-09-25T12:00:00+00:00")

        assert record["measure"] == TIME_TO_SAFE_ACTION
        assert record["stood_seconds"] is not None
        assert record["stood_seconds"] >= 0
        assert "LOWER BOUND" in record["definition"]
        assert "never invented" in record["definition"]


class TestDeliveryRoundBlock:
    def _projection(self, rows: dict) -> Any:
        from forge.adaptive.operator_view import initial_projection

        return initial_projection(rows, NOW)

    def test_an_open_round_makes_the_lineage_progressing(self) -> None:
        rows = {"run": R40_READY_RUN, "rounds": [R40_ROUND]}
        block = delivery_round_block(
            rows, projection=self._projection(rows), as_of="2026-09-25T12:00:00+00:00"
        )

        assert block["round_ref"] == "round:2:corr-1"
        assert block["superseded_by"] == "round:2:corr-1"
        lineage = block["lineage_customer_state"]
        assert lineage["state"] == "progressing"
        assert "round's child" in lineage["basis"]
        assert block["stale_action_refusal"] == "operator.stale_action_refusal"
        assert block["acceptance"]["state"] == "none"  # green CI is not acceptance

    def test_the_read_model_document_carries_the_delivery_round_arm(self) -> None:
        from forge.adaptive.operator_view import initial_projection

        rows = {
            "run": R40_READY_RUN,
            "rounds": [R40_ROUND],
            "verifications": [
                {
                    "verification_id": "v1",
                    "result": "passed",
                    "candidate_sha": "c" * 40,
                    "at": "2026-09-25T10:45:00+00:00",
                }
            ],
        }
        projection = initial_projection(rows, NOW)
        document = ops_limits_read_model(
            rows,
            occupancy=[],
            coverage={"run": "present", "rounds": "present"},
            projection=projection,
            as_of="2026-09-25T12:00:00+00:00",
        )

        assert document["delivery_round"]["round_ref"] == "round:2:corr-1"
        assert document[RECOVERY_ROUNDS]["recorded"] == 1
        assert UNRESOLVED_EFFECT_AGE in document
        assert TIME_TO_SAFE_ACTION in document
        # the four ops.* measures stay separate — the new records are
        # their own keys, never blended into the measures document
        assert set(document["measures"]) - {"schema", "as_of", "separation"} == set(
            OPS_MEASURE_NAMES
        )


class TestQueueAgeMeasure:
    """R40-15 (#351): ``operator.queue_age`` — the sustained-queue-age
    alert's observable, over the QUEUED population only (the #334
    separate population; a queue age is never a slots claim)."""

    def test_queued_ages_are_per_run_with_the_oldest_named(self) -> None:
        from forge.adaptive.ops_limits import QUEUE_AGE, queue_age_measure

        rows = {
            "runs": [
                {
                    "run_id": "r-queued-old",
                    "status": "waiting_harness",
                    "created_at": "2026-09-26T10:00:00+00:00",
                },
                {
                    "run_id": "r-queued-new",
                    "status": "waiting_ci",
                    "created_at": "2026-09-26T11:50:00+00:00",
                },
                # NOT the queued population — never sampled
                {
                    "run_id": "r-executing",
                    "status": "executing",
                    "created_at": "2026-09-26T09:00:00+00:00",
                },
                {
                    "run_id": "r-terminal",
                    "status": "ready_for_human",
                    "created_at": "2026-09-26T09:30:00+00:00",
                },
            ]
        }
        record = queue_age_measure(rows, as_of="2026-09-26T12:00:00+00:00")
        assert record["measure"] == QUEUE_AGE
        assert record["population"] == 2
        assert record["queued"] == 2
        assert record["oldest_seconds"] == 7200.0
        assert {sample["run_id"] for sample in record["samples"]} == {
            "r-queued-old",
            "r-queued-new",
        }
        # the separation sentence travels with the record
        assert "SEPARATE" in record["definition"]

    def test_an_unreadable_clock_is_counted_never_synthesized(self) -> None:
        from forge.adaptive.ops_limits import queue_age_measure

        rows = {"runs": [{"run_id": "r-x", "status": "planning", "created_at": ""}]}
        record = queue_age_measure(rows, as_of="2026-09-26T12:00:00+00:00")
        assert record["queued"] == 1
        assert record["oldest_seconds"] is None
        assert record["ages_unknown"] == 1

    def test_an_unqueried_runs_authority_renders_unknown_never_empty(self) -> None:
        from forge.adaptive.ops_limits import QUEUE_AGE, queue_age_measure

        record = queue_age_measure({}, as_of="2026-09-26T12:00:00+00:00")
        assert record["measure"] == QUEUE_AGE
        assert record["coverage"] == "unknown"
        assert record["population"] == 0
        assert "never a confident empty queue" in record["reason"]

    def test_the_read_model_carries_the_queue_age_record(self) -> None:
        from forge.adaptive.operator_view import initial_projection
        from forge.adaptive.ops_limits import (
            QUEUE_AGE,
            ops_limits_read_model,
        )

        rows = {
            "run": R40_READY_RUN,
            "runs": [
                {
                    "run_id": "r-q",
                    "status": "waiting_harness",
                    "created_at": "2026-09-26T10:00:00+00:00",
                }
            ],
        }
        projection = initial_projection(
            {
                "run": R40_READY_RUN,
                "verifications": [
                    {
                        "verification_id": "v1",
                        "result": "passed",
                        "candidate_sha": "c" * 40,
                        "at": "2026-09-25T10:45:00+00:00",
                    }
                ],
            },
            NOW,
        )
        document = ops_limits_read_model(
            rows,
            occupancy=[],
            coverage={"run": "present", "runs": "present"},
            projection=projection,
            as_of="2026-09-26T12:00:00+00:00",
        )
        assert document[QUEUE_AGE]["queued"] == 1
        assert document[QUEUE_AGE]["coverage"] == "present"
