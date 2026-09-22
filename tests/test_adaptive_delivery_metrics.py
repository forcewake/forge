"""R28-22 — all-attempt delivery economics: the reconciliation checked.

The review's two demands, pinned as tests:

- RECONCILIATION: a paused-and-retried task retains BOTH attempts' spend
  and time; a 2-attempt run with known timestamps reconciles to exact
  totals — spend across every attempt (a replayed cumulative receipt
  collapses, it never double-counts), model time from the episode
  breakdowns, tool calls, the CI queue window from the harness
  timestamps, and the human wait from gate approval to the next command
  after it.
- HONESTY: missing data degrades to UNKNOWN — ``None`` with a note
  naming the gap — never to zero. A missing usage receipt leaves spend
  incomplete; a token-only receipt (a driver with no cost API) leaves it
  incomplete; an episode without turn time, a CI observation missing one
  timestamp, a gate with no following command yet (the wait is still
  OPEN) each degrade their metric alone. ``accepted`` comes only from
  the recorded MERGED acceptance — a driver's ``completed`` exit is
  never human acceptance.

The durable-loader tests run against real SQLite sessions: the evidence
``attempts`` list, the acceptance record, the consumed gate approvals
with the next control command after them, and the publication-intent
corroboration of the attempt count when the evidence carries no history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.delivery_metrics import (
    ATTEMPT_EVIDENCE_KEY,
    DeliveryMetrics,
    delivery_metrics_for_run,
    reconcile_delivery,
)
from forge.adaptive.mailbox_db import ControlCommandRow
from forge.durable.models import FlowRun, GateApproval, PublicationIntent
from forge.models.base import Base

SQLITE_URL = "sqlite+aiosqlite:///:memory:"
T0 = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _attempt(
    attempt_id: str,
    *,
    cost_usd: float | None = 0.0,
    turn_s: float | None = 60.0,
    tools: int | None = 5,
    usage_present: bool = True,
) -> dict:
    """One attempt's recorded facts in the evidence contract's shape."""
    attempt: dict = {"attempt_id": attempt_id}
    if usage_present:
        usage: dict = {"input_tokens": 1000, "output_tokens": 200}
        if cost_usd is not None:
            usage["total_cost_usd"] = cost_usd
        attempt["usage"] = usage
    if turn_s is not None:
        attempt["episode"] = {
            "startup_s": 2.0,
            "turn_s": turn_s,
            "interrupt_grace_s": None,
            "teardown_s": 1.0,
        }
    if tools is not None:
        attempt["tool_call_count"] = tools
    return attempt


def _ci(dispatched: datetime, started: datetime) -> dict:
    return {"dispatched_at": _iso(dispatched), "started_at": _iso(started)}


def _gate(approved: datetime, next_command: datetime | None) -> dict:
    return {
        "approved_at": _iso(approved),
        "next_command_at": _iso(next_command) if next_command else None,
    }


# ----------------------------------------------------------------------
# The pure reconciliation
# ----------------------------------------------------------------------


class TestReconciliation:
    def test_a_two_attempt_run_with_known_timestamps_reconciles_exactly(self):
        """The flagship: both attempts' economics, separately sourced,
        summed to exact totals — nothing zero, nothing missing."""
        metrics = reconcile_delivery(
            attempts=[
                _attempt("run:1", cost_usd=0.40, turn_s=65.0, tools=12),
                _attempt("run:2", cost_usd=1.10, turn_s=140.0, tools=30),
            ],
            ci_observations=[
                _ci(T0, T0 + timedelta(seconds=90)),
                _ci(T0 + timedelta(hours=1), T0 + timedelta(hours=1, seconds=45)),
            ],
            gate_waits=[_gate(T0 + timedelta(hours=2), T0 + timedelta(hours=2, minutes=10))],
            accepted=True,
            run_id="run-1",
        )

        assert metrics == DeliveryMetrics(
            run_id="run-1",
            attempts_count=2,
            accepted=True,
            total_spend_usd=pytest.approx(1.5),
            model_time_seconds=pytest.approx(205.0),
            tool_call_count=42,
            ci_queue_seconds=pytest.approx(135.0),
            human_wait_seconds=pytest.approx(600.0),
            notes=(),
        )

    def test_a_replayed_cumulative_receipt_collapses_never_double_counts(self):
        metrics = reconcile_delivery(
            attempts=[
                _attempt("run:1", cost_usd=0.25, turn_s=30.0, tools=4),
                _attempt("run:1", cost_usd=0.60, turn_s=45.0, tools=9),  # the replay
                _attempt("run:2", cost_usd=1.00, turn_s=50.0, tools=10),
            ]
        )

        assert metrics.attempts_count == 2  # one attempt, two arrivals
        assert metrics.total_spend_usd == pytest.approx(1.60)  # the LATEST record wins
        assert metrics.model_time_seconds == pytest.approx(95.0)
        assert metrics.tool_call_count == 19

    def test_a_missing_receipt_leaves_spend_incomplete_never_zero(self):
        metrics = reconcile_delivery(
            attempts=[
                _attempt("run:1", cost_usd=0.40),
                _attempt("run:2", usage_present=False),
            ]
        )

        assert metrics.total_spend_usd is None  # incomplete, NOT 0.40 and NOT 0
        assert "run:2" in metrics.notes[0]
        assert "spend unknown" in metrics.notes[0]
        # The other reconciliations still answer for what IS recorded.
        assert metrics.model_time_seconds == pytest.approx(120.0)
        assert metrics.tool_call_count == 10

    def test_a_token_only_receipt_without_a_cost_figure_is_unknown(self):
        """A driver with no cost API (opencode's shape) reports tokens;
        the spend total must say unknown, not free."""
        metrics = reconcile_delivery(
            attempts=[_attempt("run:1", cost_usd=None)]  # tokens, no total_cost_usd
        )

        assert metrics.total_spend_usd is None
        assert any("no total_cost_usd" in note for note in metrics.notes)

    def test_an_attempt_that_never_drove_contributes_zero_model_time(self):
        """The lane's honest spelling: no episode AND no usage means the
        attempt failed before driving — zero model time is TRUE there,
        while an episode missing its turn time is a gap."""
        metrics = reconcile_delivery(
            attempts=[
                _attempt("run:1", turn_s=65.0),
                {"attempt_id": "run:0"},  # brief_missing: nothing to time
            ]
        )

        assert metrics.model_time_seconds == pytest.approx(65.0)
        assert not any("model time" in note for note in metrics.notes)

        gap = reconcile_delivery(
            attempts=[
                _attempt("run:1", turn_s=65.0),
                {**_attempt("run:2"), "episode": {"startup_s": 1.0}},  # drove, no turn_s
            ]
        )
        assert gap.model_time_seconds is None
        assert any("without turn time" in note for note in gap.notes)

    def test_missing_tool_counters_degrade_to_unknown(self):
        metrics = reconcile_delivery(
            attempts=[_attempt("run:1", tools=12), _attempt("run:2", tools=None)]
        )

        assert metrics.tool_call_count is None
        assert any("tool_call_count" in note for note in metrics.notes)

    def test_an_incomplete_ci_observation_degrades_the_queue_total(self):
        complete = reconcile_delivery(
            attempts=[_attempt("run:1")],
            ci_observations=[_ci(T0, T0 + timedelta(seconds=30))],
        )
        missing_started = reconcile_delivery(
            attempts=[_attempt("run:1")],
            ci_observations=[
                _ci(T0, T0 + timedelta(seconds=30)),
                {"dispatched_at": _iso(T0)},  # the job never reported starting
            ],
        )
        none_at_all = reconcile_delivery(attempts=[_attempt("run:1")])

        assert complete.ci_queue_seconds == pytest.approx(30.0)
        assert missing_started.ci_queue_seconds is None
        assert none_at_all.ci_queue_seconds is None
        assert any("queue time unknown" in note for note in none_at_all.notes)

    def test_an_open_gate_wait_is_unknown_not_zero(self):
        resolved = reconcile_delivery(gate_waits=[_gate(T0, T0 + timedelta(minutes=5))])
        still_open = reconcile_delivery(gate_waits=[_gate(T0, None)])

        assert resolved.human_wait_seconds == pytest.approx(300.0)
        assert still_open.human_wait_seconds is None
        assert any("still open, not zero" in note for note in still_open.notes)

    def test_unparseable_timestamps_degrade_to_unknown_never_raise(self):
        garbage = reconcile_delivery(
            attempts=[_attempt("run:1")],
            ci_observations=[{"dispatched_at": "yesterday-ish", "started_at": _iso(T0)}],
            gate_waits=[{"approved_at": 42, "next_command_at": _iso(T0)}],
        )

        assert garbage.ci_queue_seconds is None
        assert garbage.human_wait_seconds is None

    def test_no_attempts_at_all_is_count_zero_with_everything_unknown(self):
        metrics = reconcile_delivery(run_id="run-x")

        assert metrics.attempts_count == 0
        assert metrics.accepted is False
        assert metrics.total_spend_usd is None
        assert metrics.model_time_seconds is None
        assert metrics.tool_call_count is None
        assert any("no attempt history" in note for note in metrics.notes)

    def test_as_status_field_is_the_additive_status_fragment(self):
        metrics = reconcile_delivery(attempts=[_attempt("run:1", cost_usd=0.5)])

        field = metrics.as_status_field()

        assert set(field) == {
            "attempts_count",
            "accepted",
            "total_spend_usd",
            "model_time_seconds",
            "tool_call_count",
            "ci_queue_seconds",
            "human_wait_seconds",
            "notes",
        }
        assert field["attempts_count"] == 1
        assert field["total_spend_usd"] == 0.5
        assert isinstance(field["notes"], list)


# ----------------------------------------------------------------------
# The durable loader
# ----------------------------------------------------------------------


@pytest.fixture()
async def session_factory():
    engine = create_async_engine(
        SQLITE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        # Importing the modules under test registered every table below
        # (control_commands included) in the shared Base.metadata.
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_run(
    factory,
    *,
    run_id: str = "run-1",
    attempts: list | None = None,
    acceptance_merged: bool = False,
    commit_cycle: int = 1,
    candidate_shas: list[str] | None = None,
) -> None:
    evidence: dict = {}
    if attempts is not None:
        evidence[ATTEMPT_EVIDENCE_KEY] = attempts
    if acceptance_merged:
        evidence["acceptance"] = {
            "state": "merged",
            "merged_at": _iso(T0 + timedelta(hours=3)),
        }
    async with factory() as session:
        session.add(
            FlowRun(
                id=run_id,
                project_id=1,
                status="ready_for_human",
                evidence=evidence,
                commit_cycle=commit_cycle,
                candidate_shas=list(candidate_shas or []),
            )
        )
        await session.commit()


async def _seed_gate(factory, consumed: datetime, *, run_id: str = "run-1") -> None:
    async with factory() as session:
        session.add(
            GateApproval(
                flow_run_id=run_id,
                generation=1,
                plan_digest="1" * 64,
                base_sha="a" * 40,
                policy_digest="2" * 64,
                approver_user_id=17,
                source_event_id="evt-1",
                expires_at=consumed + timedelta(days=1),
                consumed_at=consumed,
            )
        )
        await session.commit()


async def _seed_command(
    factory, created: datetime, *, command_id: str = "cmd-1", run_id: str = "run-1"
) -> None:
    async with factory() as session:
        existing = await session.execute(
            text("SELECT COUNT(*) FROM control_commands WHERE work_id = :work"),
            {"work": run_id},
        )
        sequence = int(existing.scalar() or 0) + 1
        session.add(
            ControlCommandRow(
                id=command_id,
                work_id=run_id,
                run_id=run_id,
                kind="resume",
                status="applied",
                sequence=sequence,
                dedup_key=f"note:{command_id}",
                actor_ref="human:op",
                actor_origin="server_authenticated_human",
                journal=[{"to": "applied"}],
                created_at=created,
            )
        )
        await session.commit()


class TestDurableLoader:
    async def test_a_full_two_attempt_run_reconciles_from_the_durable_state(self, session_factory):
        """Evidence attempts + a consumed gate + the next command: the
        loader reconciles end to end, gate-approved → next command and
        all."""
        approved = T0 + timedelta(hours=2)
        dispatched = T0.replace(hour=9)
        await _seed_run(
            session_factory,
            attempts=[
                {
                    **_attempt("run:1", cost_usd=0.40, turn_s=65.0, tools=12),
                    "ci": _ci(dispatched, dispatched + timedelta(seconds=30)),
                },
                {
                    **_attempt("run:2", cost_usd=1.10, turn_s=140.0, tools=30),
                    "ci": _ci(
                        dispatched + timedelta(hours=1),
                        dispatched + timedelta(hours=1, seconds=15),
                    ),
                },
            ],
            acceptance_merged=True,
            candidate_shas=["b" * 40, "c" * 40],
        )
        await _seed_gate(session_factory, approved)
        await _seed_command(session_factory, approved + timedelta(minutes=7))

        metrics = await delivery_metrics_for_run("run-1", session_factory)

        assert metrics.attempts_count == 2
        assert metrics.accepted is True  # the recorded MERGED acceptance
        assert metrics.total_spend_usd == pytest.approx(1.50)
        assert metrics.model_time_seconds == pytest.approx(205.0)
        assert metrics.tool_call_count == 42
        assert metrics.ci_queue_seconds == pytest.approx(45.0)
        assert metrics.human_wait_seconds == pytest.approx(420.0)
        assert metrics.notes == ()

    async def test_a_missing_run_is_a_typed_empty_answer(self, session_factory):
        metrics = await delivery_metrics_for_run("run-absent", session_factory)

        assert metrics.attempts_count == 0
        assert metrics.accepted is False
        assert metrics.total_spend_usd is None  # unknown, never a free zero
        assert any("no flow run" in note for note in metrics.notes)

    async def test_the_attempt_count_is_corroborated_from_intents_without_history(
        self, session_factory
    ):
        """No per-attempt facts on the evidence: the COUNT still comes
        from the durable corroboration (cycles, candidates, scopes) — and
        the usage metrics honestly stay unknown."""
        async with session_factory() as session:
            for scope in ("cycle-1-op-a", "cycle-2-op-b"):
                session.add(
                    PublicationIntent(
                        run_id="run-1",
                        provider="github",
                        repo="owner/repo",
                        target_ref=f"forge/mr-{scope}",
                        idempotency_scope=scope,
                        operation_key=scope,
                        status="committed",
                        provider_object_id="b" * 40,
                    )
                )
            await session.commit()
        await _seed_run(session_factory, commit_cycle=2, candidate_shas=["b" * 40, "c" * 40])

        metrics = await delivery_metrics_for_run("run-1", session_factory)

        assert metrics.attempts_count == 2
        assert metrics.total_spend_usd is None
        assert metrics.model_time_seconds is None
        assert any("corroborated from durable state" in note for note in metrics.notes)
        assert any("not recorded on the evidence" in note for note in metrics.notes)

    async def test_a_gate_without_a_following_command_stays_open(self, session_factory):
        await _seed_run(session_factory, attempts=[_attempt("run:1")])
        await _seed_gate(session_factory, T0)  # consumed; nothing after it yet

        metrics = await delivery_metrics_for_run("run-1", session_factory)

        assert metrics.human_wait_seconds is None
        assert any("still open" in note for note in metrics.notes)

    async def test_driver_completed_is_never_human_acceptance(self, session_factory):
        """Attempts whose metas claim exit=completed with no recorded
        acceptance: accepted stays False — the bot never merges."""
        await _seed_run(
            session_factory,
            attempts=[{**_attempt("run:1"), "exit": "completed"}],
        )

        metrics = await delivery_metrics_for_run("run-1", session_factory)

        assert metrics.accepted is False

    async def test_without_the_mailbox_table_gate_windows_stay_unknown(self, session_factory):
        """Skip-clean: a deployment without control_commands must get an
        unknown human wait (with its reason), never a crash or a zero."""
        engine_owner = session_factory.kw["bind"]
        async with engine_owner.begin() as conn:
            await conn.execute(text("DROP TABLE control_commands"))
        await _seed_run(session_factory, attempts=[_attempt("run:1")])
        await _seed_gate(session_factory, T0)

        metrics = await delivery_metrics_for_run("run-1", session_factory)

        assert metrics.human_wait_seconds is None
        assert any("still open" in note for note in metrics.notes)

    async def test_only_the_next_command_after_the_gate_counts(self, session_factory):
        """A command recorded BEFORE the approval is not the wait's end."""
        approved = T0 + timedelta(hours=2)
        await _seed_run(session_factory, attempts=[_attempt("run:1")])
        await _seed_gate(session_factory, approved)
        await _seed_command(session_factory, approved - timedelta(hours=1), command_id="cmd-early")
        await _seed_command(session_factory, approved + timedelta(minutes=3), command_id="cmd-next")

        metrics = await delivery_metrics_for_run("run-1", session_factory)

        assert metrics.human_wait_seconds == pytest.approx(180.0)  # 3 minutes
