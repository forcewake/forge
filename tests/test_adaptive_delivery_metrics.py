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

NEXT-23 adds the identity-matched layer: every metric keys to the
ATTEMPT ID, not just the run — ``per_attempt`` receipt rows beside the
totals, and two evidence sources claiming different values for the same
attempt surface as ``conflicting_receipts`` (with the sources named),
degrading the value to unknown — never averaged, never quietly resolved.

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
    AttemptReceipt,
    ConflictingReceipt,
    DeliveryMetrics,
    LatencyBreakdown,
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


def _timed_attempt(
    attempt_id: str,
    *,
    dispatched: datetime,
    started: datetime,
    finished: datetime,
    reviewed: datetime,
    cost_usd: float = 0.5,
    turn_s: float = 60.0,
    tools: int = 5,
) -> dict:
    """One attempt's facts with the FULL causal timestamp chain (R32-20):
    dispatch → start → finish → review, each latency window derivable."""
    return {
        **_attempt(attempt_id, cost_usd=cost_usd, turn_s=turn_s, tools=tools),
        "dispatched_at": _iso(dispatched),
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "reviewed_at": _iso(reviewed),
    }


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
            per_attempt=(
                AttemptReceipt(
                    attempt_id="run:1",
                    sources=("attempts",),
                    spend_usd=0.40,
                    model_time_s=65.0,
                    tool_call_count=12,
                ),
                AttemptReceipt(
                    attempt_id="run:2",
                    sources=("attempts",),
                    spend_usd=1.10,
                    model_time_s=140.0,
                    tool_call_count=30,
                ),
            ),
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
            "per_attempt",
            "conflicting_receipts",
        }
        assert field["attempts_count"] == 1
        assert field["total_spend_usd"] == 0.5
        assert isinstance(field["notes"], list)
        assert field["per_attempt"] == [
            {
                "attempt_id": "run:1",
                "source": "attempts",
                "spend_usd": 0.5,
                "model_time_s": 60.0,
                "tool_call_count": 5,
                "latency_breakdown": {
                    "dispatch_to_start_s": None,
                    "start_to_finish_s": None,
                    "finish_to_review_s": None,
                },
            }
        ]
        assert field["conflicting_receipts"] == []


# ----------------------------------------------------------------------
# NEXT-23 — identity-matched receipts
# ----------------------------------------------------------------------


class TestIdentityMatchedReceipts:
    """Every metric keys to the attempt ID: per-attempt rows beside the
    totals, and cross-source disagreement is a REPORTED conflict — never
    an average, never a silent pick."""

    def test_two_attempts_with_distinct_receipts_key_to_their_attempt_ids(self):
        metrics = reconcile_delivery(
            attempts=[
                _attempt("lane-a-1", cost_usd=0.40, turn_s=65.0, tools=12),
                _attempt("lane-a-2", cost_usd=1.10, turn_s=140.0, tools=30),
            ],
            run_id="run-1",
        )

        assert [row.attempt_id for row in metrics.per_attempt] == ["lane-a-1", "lane-a-2"]
        assert [row.spend_usd for row in metrics.per_attempt] == [0.40, 1.10]
        assert [row.model_time_s for row in metrics.per_attempt] == [65.0, 140.0]
        assert [row.tool_call_count for row in metrics.per_attempt] == [12, 30]
        # The totals are exactly the fold of the rows.
        assert metrics.total_spend_usd == pytest.approx(
            sum(row.spend_usd for row in metrics.per_attempt)
        )
        assert metrics.conflicting_receipts == ()

    def test_anonymous_attempts_get_index_labels_not_blank_ids(self):
        metrics = reconcile_delivery(attempts=[{"tool_call_count": 1}, {"tool_call_count": 2}])

        assert [row.attempt_id for row in metrics.per_attempt] == ["#1", "#2"]
        assert metrics.tool_call_count == 3

    def test_a_same_source_replay_still_collapses_latest_wins(self):
        """The R28-22 rule survives identity-matching: a source's own
        cumulative receipt replay replaces its earlier claim — ONE
        attempt, no conflict, no double count."""
        metrics = reconcile_delivery(
            attempts=[
                {**_attempt("run:1", cost_usd=0.25), "source": "lane_meta"},
                {**_attempt("run:1", cost_usd=0.60), "source": "lane_meta"},
                _attempt("run:2", cost_usd=1.00),
            ]
        )

        assert metrics.attempts_count == 2
        assert metrics.total_spend_usd == pytest.approx(1.60)  # the latest claim wins
        assert metrics.conflicting_receipts == ()
        assert [row.spend_usd for row in metrics.per_attempt] == [0.60, 1.00]

    def test_agreeing_sources_collapse_without_a_conflict(self):
        metrics = reconcile_delivery(
            attempts=[
                {**_attempt("run:1", cost_usd=0.40), "source": "lane_meta"},
                {**_attempt("run:1", cost_usd=0.40), "source": "provider_api"},
            ]
        )

        assert metrics.attempts_count == 1
        assert metrics.total_spend_usd == pytest.approx(0.40)  # agreed, not added
        assert metrics.conflicting_receipts == ()
        assert metrics.per_attempt[0].sources == ("lane_meta", "provider_api")

    def test_a_conflicting_receipt_surfaces_as_a_conflict_never_an_average(self):
        """The NEXT-23 demand: two sources claiming different spend for
        ONE attempt is a conflict row naming both sources — the value is
        unknown, and 0.475 (the average) or 0.55 (latest-wins across
        sources) appears nowhere."""
        metrics = reconcile_delivery(
            attempts=[
                {**_attempt("run:1", cost_usd=0.40), "source": "lane_meta"},
                {**_attempt("run:1", cost_usd=0.55), "source": "provider_api"},
                _attempt("run:2", cost_usd=1.00),
            ]
        )

        assert metrics.attempts_count == 2
        assert metrics.conflicting_receipts == (
            ConflictingReceipt(
                attempt_id="run:1",
                source_a="lane_meta",
                source_b="provider_api",
                fields=("spend_usd",),
            ),
        )
        # The conflicted attempt's spend is unknown, so the TOTAL is
        # unknown — never the average, never one side's claim. The
        # agreeing attempt's receipt stays visible in its own row.
        assert metrics.per_attempt[0].spend_usd is None
        assert metrics.per_attempt[1].spend_usd == 1.00
        assert metrics.total_spend_usd is None
        # The OTHER metrics of the conflicted attempt still answer.
        assert metrics.per_attempt[0].model_time_s == 60.0

    def test_conflicting_turn_and_tool_receipts_conflict_on_their_own_fields(self):
        metrics = reconcile_delivery(
            attempts=[
                {**_attempt("run:1", turn_s=30.0, tools=4), "source": "lane_meta"},
                {**_attempt("run:1", turn_s=45.0, tools=4), "source": "provider_api"},
            ]
        )

        conflicts = {c.fields for c in metrics.conflicting_receipts}
        assert conflicts == {("model_time_s",)}
        assert metrics.per_attempt[0].model_time_s is None
        assert metrics.model_time_seconds is None
        # The AGREEING tool counter survives the turn conflict untouched.
        assert metrics.per_attempt[0].tool_call_count == 4
        assert metrics.tool_call_count == 4

    def test_a_replay_then_a_corroborating_source_is_not_a_conflict(self):
        """The replay (0.25 → 0.60 within lane_meta) is superseded BEFORE
        cross-source comparison: provider_api corroborating the LATEST
        0.60 agrees, and the stale 0.25 fabricates no conflict."""
        metrics = reconcile_delivery(
            attempts=[
                {**_attempt("run:1", cost_usd=0.25), "source": "lane_meta"},
                {**_attempt("run:1", cost_usd=0.60), "source": "lane_meta"},
                {**_attempt("run:1", cost_usd=0.60), "source": "provider_api"},
            ]
        )

        assert metrics.attempts_count == 1
        assert metrics.conflicting_receipts == ()
        assert metrics.total_spend_usd == pytest.approx(0.60)

    def test_the_conflict_names_the_attempt_and_both_sources_in_the_notes(self):
        metrics = reconcile_delivery(
            attempts=[
                {**_attempt("run:1", cost_usd=0.40), "source": "lane_meta"},
                {**_attempt("run:1", cost_usd=0.55), "source": "provider_api"},
            ]
        )

        assert any(
            "run:1" in note and "conflicting receipts" in note and "never averaged" in note
            for note in metrics.notes
        )


# ----------------------------------------------------------------------
# R32-20 — identity-matched latency records
# ----------------------------------------------------------------------


class TestIdentityMatchedLatency:
    """Every latency window keys to the attempt IDENTITY: distinct
    attempts carry distinct breakdowns, a missing attempt degrades to
    sticky-unknown, and a cross-source disagreement about WHEN an attempt
    ran conflicts exactly like one about what it cost."""

    def test_two_attempts_with_distinct_latencies_key_to_their_ids(self):
        """The flagship: the fast attempt (short queue, long turn) and
        the slow one (long queue, short turn) keep their OWN windows —
        neither inherits the other's, and the totals remain exactly the
        fold of the per-attempt rows."""
        fast = _timed_attempt(
            "lane-a-1",
            dispatched=T0,
            started=T0 + timedelta(seconds=10),
            finished=T0 + timedelta(seconds=70),
            reviewed=T0 + timedelta(seconds=130),
            cost_usd=0.40,
            turn_s=65.0,
            tools=12,
        )
        slow = _timed_attempt(
            "lane-a-2",
            dispatched=T0 + timedelta(hours=1),
            started=T0 + timedelta(hours=1, minutes=5),
            finished=T0 + timedelta(hours=1, minutes=7),
            reviewed=T0 + timedelta(hours=1, minutes=12),
            cost_usd=1.10,
            turn_s=140.0,
            tools=30,
        )

        metrics = reconcile_delivery(attempts=[fast, slow], run_id="run-1")

        assert [row.attempt_id for row in metrics.per_attempt] == ["lane-a-1", "lane-a-2"]
        assert [row.latency_breakdown for row in metrics.per_attempt] == [
            LatencyBreakdown(
                dispatch_to_start_s=10.0, start_to_finish_s=60.0, finish_to_review_s=60.0
            ),
            LatencyBreakdown(
                dispatch_to_start_s=300.0, start_to_finish_s=120.0, finish_to_review_s=300.0
            ),
        ]
        # The totals are exactly the fold of the per-attempt rows.
        assert metrics.total_spend_usd == pytest.approx(
            sum(row.spend_usd for row in metrics.per_attempt)
        )
        assert metrics.model_time_seconds == pytest.approx(
            sum(row.model_time_s for row in metrics.per_attempt)
        )
        assert metrics.tool_call_count == sum(row.tool_call_count for row in metrics.per_attempt)

    def test_the_ci_fragment_timestamps_derive_the_dispatch_window(self):
        """The same timestamps may ride the attempt's ``ci`` sub-dict (the
        shape the loader's CI observations read) — the breakdown reads
        either spelling."""
        attempt = {
            **_attempt("run:1"),
            "ci": _ci(T0, T0 + timedelta(seconds=45)),
        }

        metrics = reconcile_delivery(attempts=[attempt])

        assert metrics.per_attempt[0].latency_breakdown.dispatch_to_start_s == pytest.approx(45.0)

    def test_a_missing_attempt_degrades_to_unknown_never_zero(self):
        """The sticky-unknown rule: the durable state knows a THIRD
        attempt existed (a failed sibling that left no receipt); its
        placeholder row carries None everywhere — and the folds it joins
        (spend, model time, tools) degrade to unknown rather than
        excluding it or zeroing it. The honest never-drove zero is NOT
        available to an attempt whose driving is unknown."""
        recorded = [
            _timed_attempt(
                "lane-a-1",
                dispatched=T0,
                started=T0 + timedelta(seconds=10),
                finished=T0 + timedelta(seconds=70),
                reviewed=T0 + timedelta(seconds=130),
            ),
            _timed_attempt(
                "lane-a-2",
                dispatched=T0 + timedelta(hours=1),
                started=T0 + timedelta(hours=1, minutes=5),
                finished=T0 + timedelta(hours=1, minutes=7),
                reviewed=T0 + timedelta(hours=1, minutes=12),
            ),
        ]

        metrics = reconcile_delivery(attempts=recorded, expected_attempt_count=3)

        assert metrics.attempts_count == 3
        missing = metrics.per_attempt[2]
        assert missing.attempt_id == "missing:1"
        assert missing.spend_usd is None  # never free
        assert missing.model_time_s is None  # never the never-drove zero
        assert missing.tool_call_count is None
        assert missing.latency_breakdown == LatencyBreakdown()
        assert metrics.total_spend_usd is None
        assert metrics.model_time_seconds is None
        assert metrics.tool_call_count is None
        assert any(
            "no receipt in the evidence" in note and "never zero" in note for note in metrics.notes
        )
        # The RECORDED attempts' rows stay fully known beside the gap.
        assert metrics.per_attempt[0].latency_breakdown.dispatch_to_start_s == 10.0
        assert metrics.per_attempt[1].latency_breakdown.dispatch_to_start_s == 300.0

    def test_a_partial_timestamp_chain_names_its_missing_windows(self):
        """An attempt that recorded dispatch and start but neither finish
        nor review: the first window answers, the later ones are NAMED
        gaps (unknown, never zero), and the note says which stamp is
        absent."""
        attempt = {
            **_attempt("run:1"),
            "dispatched_at": _iso(T0),
            "started_at": _iso(T0 + timedelta(seconds=20)),
        }

        metrics = reconcile_delivery(attempts=[attempt])

        breakdown = metrics.per_attempt[0].latency_breakdown
        assert breakdown.dispatch_to_start_s == pytest.approx(20.0)
        assert breakdown.start_to_finish_s is None
        assert breakdown.finish_to_review_s is None
        assert any("started_at but not finished_at" in note for note in metrics.notes)

    def test_conflicting_latency_windows_conflict_never_average(self):
        """Two sources claiming different start times for ONE attempt is
        a conflict row naming both sources — the window is unknown, not
        averaged and not latest-won across sources."""
        lane_meta = _timed_attempt(
            "run:1",
            dispatched=T0,
            started=T0 + timedelta(seconds=30),
            finished=T0 + timedelta(seconds=90),
            reviewed=T0 + timedelta(seconds=150),
        )
        provider_api = _timed_attempt(
            "run:1",
            dispatched=T0,
            started=T0 + timedelta(seconds=60),  # the provider clock disagrees
            finished=T0 + timedelta(seconds=90),
            reviewed=T0 + timedelta(seconds=150),
        )

        metrics = reconcile_delivery(
            attempts=[
                {**lane_meta, "source": "lane_meta"},
                {**provider_api, "source": "provider_api"},
            ]
        )

        # The start stamp feeds TWO windows — both disagree, both conflict.
        conflicts = {c.fields for c in metrics.conflicting_receipts}
        assert conflicts == {("dispatch_to_start_s",), ("start_to_finish_s",)}
        breakdown = metrics.per_attempt[0].latency_breakdown
        assert breakdown.dispatch_to_start_s is None
        assert breakdown.start_to_finish_s is None
        # The AGREEING window (finish → review) survives the conflicts.
        assert breakdown.finish_to_review_s == 60.0
        assert any("conflicting receipts for dispatch_to_start_s" in note for note in metrics.notes)
        assert any("never averaged" in note for note in metrics.notes)

    def test_a_replayed_receipt_keeps_its_latest_windows_without_a_conflict(self):
        """The replay rule extends to latency: a source's own cumulative
        receipt (a later record with a corrected started_at) replaces its
        earlier window — ONE attempt, no conflict, no double count."""
        metrics = reconcile_delivery(
            attempts=[
                {
                    **_timed_attempt(
                        "run:1",
                        dispatched=T0,
                        started=T0 + timedelta(seconds=30),
                        finished=T0 + timedelta(seconds=90),
                        reviewed=T0 + timedelta(seconds=150),
                    ),
                    "source": "lane_meta",
                },
                {
                    **_timed_attempt(
                        "run:1",
                        dispatched=T0,
                        started=T0 + timedelta(seconds=45),
                        finished=T0 + timedelta(seconds=90),
                        reviewed=T0 + timedelta(seconds=150),
                    ),
                    "source": "lane_meta",
                },
            ]
        )

        assert metrics.attempts_count == 1
        assert metrics.conflicting_receipts == ()
        assert metrics.per_attempt[0].latency_breakdown.dispatch_to_start_s == 45.0


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
        # The CI fragments carry dispatched_at + started_at (no finish or
        # review stamps yet): each attempt's dispatch_to_start window IS
        # derivable, and the un-derivable ones are NAMED gaps (R32-20) —
        # one note per attempt, never silence.
        assert metrics.notes == (
            "attempt run:1 records started_at but not finished_at"
            " — the start_to_finish_s window is unknown",
            "attempt run:2 records started_at but not finished_at"
            " — the start_to_finish_s window is unknown",
        )
        windows = [row.latency_breakdown for row in metrics.per_attempt]
        assert windows == [
            LatencyBreakdown(dispatch_to_start_s=30.0),
            LatencyBreakdown(dispatch_to_start_s=15.0),
        ]

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

    async def test_a_failed_sibling_attempt_without_a_receipt_degrades_the_totals(
        self, session_factory
    ):
        """R32-20's loader leg: two recorded attempts but THREE candidate
        shas (a failed sibling that left no receipt) — the missing attempt
        joins as a placeholder row with every metric None, and the totals
        it joins degrade to unknown instead of silently dropping it."""
        await _seed_run(
            session_factory,
            attempts=[
                _timed_attempt(
                    "run:1",
                    dispatched=T0,
                    started=T0 + timedelta(seconds=10),
                    finished=T0 + timedelta(seconds=70),
                    reviewed=T0 + timedelta(seconds=130),
                ),
                _timed_attempt(
                    "run:2",
                    dispatched=T0 + timedelta(hours=1),
                    started=T0 + timedelta(hours=1, minutes=5),
                    finished=T0 + timedelta(hours=1, minutes=7),
                    reviewed=T0 + timedelta(hours=1, minutes=12),
                ),
            ],
            candidate_shas=["b" * 40, "c" * 40, "d" * 40],  # three attempts existed
        )

        metrics = await delivery_metrics_for_run("run-1", session_factory)

        assert metrics.attempts_count == 3
        assert [row.attempt_id for row in metrics.per_attempt] == [
            "run:1",
            "run:2",
            "missing:1",
        ]
        assert metrics.total_spend_usd is None  # the sibling is not free
        assert metrics.model_time_seconds is None
        assert any("no receipt in the evidence" in note for note in metrics.notes)
