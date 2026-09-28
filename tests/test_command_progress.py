"""R42-07 (#380) — one coherent command-to-delivery progress view.

The live process spans accepted commands, provider observations, rounds,
checkpoints, verification and human resolution; a single run status
cannot explain the stages. Pinned here over hand-built rows and real
sqlite (the reader + the API):

- the COMMAND-OUTCOME axis: ``accepted`` / ``pending`` / ``applied`` /
  ``refused`` / ``exhausted`` derived from the #374 journals — an axis
  SEPARATE from execution and human-review state. The R42-01 transient
  (a retryable provider observation journaled ``pending``) renders
  VISIBLY pending with its retry trail — never apparently complete —
  and ``exhausted`` names the repair-query seam;
- the NEXT-STEP distinction set: an unresolved REQUIRED discussion, a
  failed INDEPENDENT check, an EXHAUSTED review budget, a PROVIDER
  outage and a SPENT observation budget are five different conditions
  with five different safe actions — each its own row with its own
  reason and its sentence naming what it is NOT (never one blurred
  "blocked"); the budget case names the LIMITING axis and the amendment
  route;
- ONE coherent current subject per snapshot: after a superseding round
  the document names the open round's child run and labels this
  delivery's outcomes SUPERSEDED history;
- STALE labelling: a delayed/inconsistent projection is labelled STALE
  with its version, never blended;
- the COMMAND-OUTCOME fence of the action CAS: an action planned while
  a provider observation was RETRYING (``pending``) is refused once the
  outcome settles or a different command becomes the newest unresolved
  one — a stale action can never mutate a different current attempt;
- the bounded SUPPORT slice: scoped ids, the relevant TYPED errors, the
  settings NAMES and evidence refs — no raw code, no reviewer prompts,
  no credential values by default, the redactions COUNTED;
- the replay-stable MR status comment: the same facts on CLI, API and
  the native comment, one identity per world.

The pure folds live in
:mod:`forge.adaptive.operator_view` (:func:`command_outcome_rows`,
:func:`next_step_rows`, :func:`command_progress`,
:func:`export_command_support`); the observability fold in
:mod:`forge.adaptive.ops_limits`
(:func:`command_progress_limits_fold`).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.operator_snapshot import CanonicalSubject, OperatorSnapshotReader
from forge.adaptive.operator_view import (
    COMMAND_OUTCOMES,
    COMMAND_OUTCOME_PROOF,
    COMMAND_PROGRESS_SCHEMA,
    COMMAND_RECOVERY_SETTINGS,
    COMMAND_SUPPORT_FIELDS,
    COMMAND_SUPPORT_SCHEMA,
    FEEDBACK_OUTCOME_EVENT,
    NEXT_STEP_CODES,
    PROVIDER_OBSERVATION_RETRY_EVENT,
    STALE_ACTION_REFUSAL,
    OperatorAction,
    RecoveryActions,
    command_outcome_rows,
    command_progress,
    export_command_support,
    initial_projection,
    next_step_rows,
    render_status_comment,
    status_note_lines,
)
from forge.adaptive.ops_limits import (
    COMMAND_ACCEPTED_TO_APPLIED,
    MANUAL_RESCUE_MINUTES,
    PROJECTION_LAG,
    command_progress_limits_fold,
)

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
RUN_ID = "f" * 32
CHILD = "c" * 32
NOTE = "9501"
CANDIDATE = "c" * 40

#: The note body a reviewer typed — NEVER renderable on the operator
#: surface (the value-freedom sentinel every export arm greps for).
NOTE_TEXT = "/fix rename forge-demo/x.md handler and add the flaky test"


def _run(**over) -> dict:
    row: dict = {
        "id": RUN_ID,
        "status": "executing",
        "base_sha": "b" * 40,
        "candidate_shas": [CANDIDATE],
        "plan_digest": "p" * 64,
        "evidence": {},
        "blocked_reason": "",
        "cancel_requested": False,
        "created_at": "2026-09-26T09:00:00+00:00",
        "updated_at": "2026-09-26T11:50:00+00:00",
    }
    row.update(over)
    return row


def _journal(note_id: str, outcome: str, *, at: str, reason: str = "") -> dict:
    return {
        "event_id": f"ob-{note_id}-{outcome}-{at}",
        "event_type": FEEDBACK_OUTCOME_EVENT,
        "note_id": note_id,
        "outcome": outcome,
        "reason": reason,
        "observed": None,
        "status_code": None,
        "detail": "",
        "at": at,
    }


def _retry(
    note_id: str,
    *,
    at: str,
    reason: str = "head_unavailable",
    status_code: int = 503,
    detail: str = "GitLab transiently unavailable",
) -> dict:
    return {
        "event_id": f"ob-{note_id}-retry-{at}",
        "event_type": PROVIDER_OBSERVATION_RETRY_EVENT,
        "note_id": note_id,
        "outcome": "",
        "reason": reason,
        "observed": False,
        "status_code": status_code,
        "detail": detail,
        "at": at,
    }


def _attempt(**over) -> dict:
    row: dict = {
        "attempt_id": "att-1",
        "status": "executing",
        "started_at": "2026-09-26T11:40:00+00:00",  # inside WEDGED_AFTER of NOW
        "updated_at": "2026-09-26T11:50:00+00:00",
        "generation": 1,
    }
    row.update(over)
    return row


def _rows(**sections: Any) -> dict:
    rows: dict = {"run": _run()}
    rows.update(sections)
    return rows


def _request(note_id: str, status: str, classification: str = "material_change") -> dict:
    return {
        "note_id": note_id,
        "status": status,
        "classification": classification,
        "decision_id": "dec-1",
        "head_sha": "b" * 40,
        "created_at": "2026-09-26T10:00:00+00:00",
    }


def _codes(steps: list[dict]) -> list[str]:
    return [str(step.get("code") or "") for step in steps]


# ---------------------------------------------------------------------------
# The command-outcome axis — accepted ≠ applied, pending is visible
# ---------------------------------------------------------------------------


class TestCommandOutcomeAxis:
    def test_the_journal_spellings_are_the_service_canon(self):
        """The projection's event names ARE the #374 service's journaled
        spellings — one canon, never a second vocabulary."""
        from forge.runs.service import FEEDBACK_OUTCOME_EVENT as SERVICE_OUTCOME
        from forge.runs.service import PROVIDER_OBSERVATION_RETRY_EVENT as SERVICE_RETRY

        assert FEEDBACK_OUTCOME_EVENT == SERVICE_OUTCOME == "feedback.outcome"
        assert PROVIDER_OBSERVATION_RETRY_EVENT == SERVICE_RETRY

    def test_the_r42_01_transient_renders_visibly_pending(self):
        """The review's acceptance, structural: a journaled ``pending``
        (the retryable observation) renders ``pending`` with its retry
        trail, ``complete: False``, and the honest note saying the
        command is NOT complete — never an apparently-complete view."""
        rows = _rows(
            feedback_events=[
                _retry(NOTE, at="2026-09-26T11:00:00+00:00"),
                _journal(
                    NOTE, "pending", at="2026-09-26T11:00:05+00:00", reason="head_unavailable"
                ),
            ]
        )
        (entry,) = command_outcome_rows(rows, coverage={"feedback_events": "present"})
        assert entry["note_id"] == NOTE
        assert entry["outcome"] == "pending"
        assert entry["complete"] is False
        assert entry["retries"] == {
            "count": 1,
            "reasons": ["head_unavailable"],
            "last_at": "2026-09-26T11:00:00+00:00",
            "observed": False,
        }
        assert "NOT complete" in entry["honest_note"]
        assert "1 deferral(s)" in entry["honest_note"]

    def test_accepted_is_not_applied(self):
        """The durable command identity with NO outcome journal is
        ACCEPTED — accepted ≠ applied is a different row, not a nuance."""
        rows = _rows(
            run=_run(evidence={"review_feedback_requests": {NOTE: _request(NOTE, "recorded")}})
        )
        (entry,) = command_outcome_rows(rows, coverage={"feedback_events": "missing"})
        assert entry["outcome"] == "accepted"
        assert entry["complete"] is False
        assert entry["source"] == "request"
        assert "accepted ≠ applied" in COMMAND_OUTCOME_PROOF["accepted"]

    def test_the_journal_word_completed_is_the_axis_word_applied(self):
        rows = _rows(
            feedback_events=[
                _retry(NOTE, at="2026-09-26T11:00:00+00:00"),
                _journal(NOTE, "pending", at="2026-09-26T11:00:05+00:00"),
                _journal(NOTE, "completed", at="2026-09-26T11:05:00+00:00", reason="staged"),
            ]
        )
        (entry,) = command_outcome_rows(rows, coverage={"feedback_events": "present"})
        assert entry["outcome"] == "applied"
        assert entry["complete"] is True
        assert entry["proven_by"]["of"] == "feedback outcome journal"

    def test_refused_and_exhausted_come_from_the_journal(self):
        for word in ("refused", "exhausted"):
            rows = _rows(feedback_events=[_journal(NOTE, word, at="2026-09-26T11:00:00+00:00")])
            (entry,) = command_outcome_rows(rows, coverage={"feedback_events": "present"})
            assert entry["outcome"] == word
            assert entry["complete"] is False

    def test_a_retry_trail_alone_is_durable_retrying_state(self):
        """A deferral journal whose outcome row was not read still says
        the command is RETRYING — the retry trail is durable state."""
        rows = _rows(feedback_events=[_retry(NOTE, at="2026-09-26T11:00:00+00:00")])
        (entry,) = command_outcome_rows(rows, coverage={"feedback_events": "present"})
        assert entry["outcome"] == "pending"
        assert entry["complete"] is False

    def test_the_request_lifecycle_fallback(self):
        """Where no journal row exists the request document decides:
        ``staged`` is accepted, a typed refusal is refused, a settled
        lifecycle is applied — and the journal OUTRANKS the fallback."""
        base = _rows(
            run=_run(
                evidence={
                    "review_feedback_requests": {
                        "1": _request("1", "staged"),
                        "2": _request("2", "refused_unauthorized"),
                        "3": _request("3", "round_admitted"),
                    }
                }
            )
        )
        outcomes = {
            entry["note_id"]: entry["outcome"]
            for entry in command_outcome_rows(base, coverage={"feedback_events": "missing"})
        }
        assert outcomes == {"1": "accepted", "2": "refused", "3": "applied"}
        outranked = _rows(
            run=base["run"],
            feedback_events=[_journal("2", "completed", at="2026-09-26T11:00:00+00:00")],
        )
        outcomes = {
            entry["note_id"]: entry["outcome"]
            for entry in command_outcome_rows(outranked, coverage={"feedback_events": "present"})
        }
        assert outcomes["2"] == "applied"

    def test_an_unobserved_authority_renders_one_unknown_entry(self):
        """No section and no coverage word (or an explicit unknown) → ONE
        honest ``unknown`` entry, never an empty success — including for
        a coverage map assembled before the section existed."""
        for coverage in (None, {}, {"feedback_events": "unknown"}, {"run": "present"}):
            entries = command_outcome_rows(_rows(), coverage=coverage)  # type: ignore[arg-type]
            assert len(entries) == 1
            assert entries[0]["outcome"] == "unknown"
            assert entries[0]["proven_by"] is None

    def test_a_queried_empty_section_renders_no_commands(self):
        entries = command_outcome_rows(
            _rows(feedback_events=[]), coverage={"feedback_events": "missing"}
        )
        assert entries == ()

    def test_the_outcome_vocabulary_is_closed(self):
        assert COMMAND_OUTCOMES == (
            "accepted",
            "pending",
            "applied",
            "refused",
            "exhausted",
            "unknown",
        )


# ---------------------------------------------------------------------------
# The next-step distinction set — five conditions, five safe actions
# ---------------------------------------------------------------------------


class TestNextStepDistinctionSet:
    def test_an_unresolved_required_discussion_is_its_own_row(self):
        rows = _rows(
            run=_run(evidence={"review_feedback_requests": {NOTE: _request(NOTE, "recorded")}})
        )
        (step,) = next_step_rows(rows, coverage={"feedback_events": "missing"})
        assert step["code"] == "unresolved_required_discussion"
        assert NOTE in step["reason"]
        assert "NOT failed CI" in step["distinct_from"]
        assert step["retryable"] is False
        assert step["safe_action"]["via"] == "human:discussion-resolution"
        assert step["evidence"]["of"] == "feedback request"

    def test_a_clarification_is_not_a_required_discussion(self):
        rows = _rows(
            run=_run(
                evidence={
                    "review_feedback_requests": {
                        NOTE: _request(NOTE, "recorded", classification="clarification")
                    }
                }
            )
        )
        assert next_step_rows(rows, coverage={"feedback_events": "missing"}) == []

    def test_a_resolved_discussion_never_renders_unresolved(self):
        """A settled lifecycle and a typed refusal are RESOLUTIONS — the
        unresolved-discussion row renders for none of them."""
        settled = _rows(
            run=_run(
                evidence={"review_feedback_requests": {NOTE: _request(NOTE, "round_admitted")}}
            )
        )
        assert "unresolved_required_discussion" not in _codes(
            next_step_rows(settled, coverage={"feedback_events": "missing"})
        )
        refused = _rows(
            run=_run(evidence={"review_feedback_requests": {NOTE: _request(NOTE, "stale_head")}}),
            feedback_events=[_journal(NOTE, "refused", at="2026-09-26T11:00:00+00:00")],
        )
        # a typed refusal is resolved — no next-step row on this axis at all
        assert next_step_rows(refused, coverage={"feedback_events": "present"}) == []

    def test_failed_checks_bind_to_the_current_candidate(self):
        rows = _rows(
            verifications=[
                {
                    "verification_id": "ver-1",
                    "result": "failed",
                    "candidate_sha": CANDIDATE,
                    "at": "2026-09-26T11:45:00+00:00",
                }
            ]
        )
        (step,) = next_step_rows(rows)
        assert step["code"] == "failed_independent_checks"
        assert step["binding"] == "current"
        assert "NOT an exhausted review budget" in step["distinct_from"]
        assert step["retryable"] is True

    def test_a_failed_check_on_a_superseded_candidate_is_history(self):
        """The #367 rule on the checks axis: a failure naming an older
        candidate is HISTORICAL — listed, never mistaken for the current
        attempt's checks."""
        rows = _rows(
            verifications=[
                {
                    "verification_id": "ver-old",
                    "result": "failed",
                    "candidate_sha": "d" * 40,
                    "at": "2026-09-26T10:45:00+00:00",
                }
            ],
        )
        (step,) = next_step_rows(rows)
        assert step["binding"] == "historical"
        assert "HISTORICAL" in step["reason"]

    def test_the_budget_case_names_the_limiting_axis_and_amendment_route(self):
        rows = _rows(
            run=_run(
                blocked_reason="budget_exhausted",
                evidence={
                    "review_budget_block": {
                        "budget": {"closing_review_fits": False},
                        "amendments": [
                            {
                                "axis": "calls",
                                "amount": 10,
                                "status": "refused",
                                "refusal_reason": "tokens: cross-axis conversion refused",
                            }
                        ],
                        "short_reason": "closing review does not fit",
                    }
                },
            )
        )
        (step,) = [row for row in next_step_rows(rows) if row["code"] == "exhausted_review_budget"]
        assert "tokens axis" in step["reason"]
        assert "amend" in step["safe_action"]["action"]
        assert step["safe_action"]["shape"]["fields"].startswith("axis=")
        assert "ONE axis per amendment" in step["safe_action"]["shape"]["rule"]
        assert "NOT failed CI" in step["distinct_from"]

    def test_a_provider_outage_is_wait_not_manual_retry(self):
        rows = _rows(
            feedback_events=[
                _retry(NOTE, at="2026-09-26T11:00:00+00:00"),
                _journal(NOTE, "pending", at="2026-09-26T11:00:05+00:00"),
            ]
        )
        (step,) = next_step_rows(rows, coverage={"feedback_events": "present"})
        assert step["code"] == "provider_outage"
        assert "RETRYING" in step["reason"]
        assert "NOT exhausted" in step["distinct_from"]
        assert step["retryable"] is True
        assert "wait" in step["safe_action"]["action"]

    def test_a_spent_observation_budget_is_repair_not_replay(self):
        rows = _rows(
            feedback_events=[
                _journal(
                    NOTE, "exhausted", at="2026-09-26T11:00:00+00:00", reason="head_unavailable"
                )
            ]
        )
        (step,) = next_step_rows(rows, coverage={"feedback_events": "present"})
        assert step["code"] == "observation_exhausted"
        assert "feedback_steps_without_outcome" in step["reason"]
        assert "never an automatic replay" in step["safe_action"]["action"]
        assert step["retryable"] is False
        assert "NOT a provider outage" in step["distinct_from"]

    def test_all_five_conditions_render_simultaneously_with_their_own_codes(self):
        """The distinction set is total: one world carrying all five
        conditions renders five rows — never one blurred 'blocked'."""
        rows = _rows(
            run=_run(
                evidence={
                    "review_feedback_requests": {"9000": _request("9000", "recorded")},
                    "review_budget_block": {
                        "budget": {"closing_review_fits": False},
                        "amendments": [],
                        "short_reason": "closing review does not fit",
                    },
                }
            ),
            feedback_events=[
                _retry("9100", at="2026-09-26T11:00:00+00:00"),
                _journal("9100", "pending", at="2026-09-26T11:00:05+00:00"),
                _journal("9200", "exhausted", at="2026-09-26T10:00:00+00:00"),
            ],
            verifications=[
                {
                    "verification_id": "ver-1",
                    "result": "failed",
                    "candidate_sha": CANDIDATE,
                    "at": "2026-09-26T11:45:00+00:00",
                }
            ],
        )
        steps = next_step_rows(rows, coverage={"feedback_events": "present"})
        assert sorted(_codes(steps)) == sorted(NEXT_STEP_CODES)
        for step in steps:
            assert step["distinct_from"], step["code"]
            assert step["safe_action"]["action"], step["code"]


# ---------------------------------------------------------------------------
# The one document — current subject, stale labelling, value freedom
# ---------------------------------------------------------------------------


class TestCommandProgressDocument:
    def test_the_document_schema_and_summary(self):
        rows = _rows(
            feedback_events=[
                _journal(NOTE, "pending", at="2026-09-26T11:00:00+00:00"),
                _journal("9400", "completed", at="2026-09-26T10:00:00+00:00"),
            ]
        )
        document = command_progress(rows, coverage={"feedback_events": "present"}, now=NOW)
        assert document["schema"] == COMMAND_PROGRESS_SCHEMA
        assert document["summary"] == {
            "accepted": 0,
            "pending": 1,
            "applied": 1,
            "refused": 0,
            "exhausted": 0,
            "unknown": 0,
            "complete": False,
        }

    def test_one_coherent_current_subject(self):
        document = command_progress(_rows(), now=NOW)
        assert document["current_subject"]["run_id"] == RUN_ID
        assert document["current_subject"]["candidate"] == CANDIDATE
        assert document["current_subject"]["note"] == "the lineage's current execution"

    def test_after_a_superseding_round_the_document_names_the_child(self):
        """After a superseding round ONE snapshot refers to ONE coherent
        subject: the open round's child run, with this delivery's
        outcomes labelled SUPERSEDED history."""
        rows = _rows(
            rounds=[
                {
                    "round_id": "rr-2",
                    "round_number": 2,
                    "status": "dispatched",
                    "status_reason": "",
                    "decision_id": "dec-2",
                    "note_id": "9600",
                    "mr_iid": 4,
                    "base_head_sha": "b" * 40,
                    "parent_run_id": RUN_ID,
                    "child_run_id": CHILD,
                    "root_run_id": RUN_ID,
                    "requested_by": "reviewer:op",
                    "created_at": "2026-09-26T11:00:00+00:00",
                    "updated_at": "2026-09-26T11:10:00+00:00",
                }
            ],
            feedback_events=[_journal(NOTE, "applied", at="2026-09-26T10:00:00+00:00")],
        )
        document = command_progress(rows, coverage={"feedback_events": "present"}, now=NOW)
        assert document["current_subject"]["run_id"] == CHILD
        assert "SUPERSEDED delivery" in document["current_subject"]["note"]
        assert CHILD in document["current_subject"]["note"]

    def test_a_moved_fence_labels_stale_with_the_version_never_blended(self):
        rows = _rows(feedback_events=[_journal(NOTE, "pending", at="2026-09-26T11:00:00+00:00")])
        fresh = command_progress(rows, coverage={"feedback_events": "present"}, now=NOW)
        stale = command_progress(
            rows,
            coverage={"feedback_events": "present"},
            now=NOW,
            projection_inconsistent=True,
        )
        assert fresh["stale"] is False
        assert stale["stale"] is True
        assert "v1" in stale["stale_basis"]
        # never blended: the facts are identical, only the label moved
        assert stale["commands"] == fresh["commands"]
        assert stale["next_steps"] == fresh["next_steps"]

    def test_the_document_is_value_free(self):
        """The reviewer's note TEXT never rides any command-axis
        document — identities and typed reasons only."""
        rows = _rows(
            run=_run(
                evidence={
                    "review_feedback_requests": {
                        NOTE: {**_request(NOTE, "recorded"), "text": NOTE_TEXT}
                    }
                }
            ),
            feedback_events=[
                _retry(NOTE, at="2026-09-26T11:00:00+00:00", detail=NOTE_TEXT),
                _journal(NOTE, "pending", at="2026-09-26T11:00:05+00:00"),
            ],
        )
        document = command_progress(rows, coverage={"feedback_events": "present"}, now=NOW)
        assert NOTE_TEXT not in json.dumps(document)


# ---------------------------------------------------------------------------
# The command-outcome fence of the action CAS
# ---------------------------------------------------------------------------


class TestCommandOutcomeActionFence:
    def _pending_rows(self) -> dict:
        return _rows(
            attempts=[_attempt()],
            feedback_events=[
                _retry(NOTE, at="2026-09-26T11:00:00+00:00"),
                _journal(NOTE, "pending", at="2026-09-26T11:00:05+00:00"),
            ],
        )

    def _plan_steer(self, rows: dict) -> OperatorAction:
        projection = initial_projection(rows, NOW)
        assert projection.state == "executing"
        assert projection.feedback_note_ref == NOTE
        assert projection.feedback_outcome == "pending"
        planned = RecoveryActions.plan(projection, "human:op", "approver")
        action = next(item for item in planned if item.action == "steer")
        assert action.expected_command_ref == NOTE
        assert action.expected_command_outcome == "pending"
        return action

    def test_the_ticket_carries_the_command_subject(self):
        action = self._plan_steer(self._pending_rows())
        fact = action.audit_fact()
        assert fact["expected_command_ref"] == NOTE
        assert fact["expected_command_outcome"] == "pending"

    def test_an_action_planned_while_pending_is_refused_once_applied(self):
        """The R42-01 acceptance on the action axis: an action planned
        while the observation was RETRYING is refused once it settles —
        the operator re-decides against the outcome as it NOW stands."""
        action = self._plan_steer(self._pending_rows())
        settled = _rows(
            attempts=[_attempt()],
            feedback_events=[
                _retry(NOTE, at="2026-09-26T11:00:00+00:00"),
                _journal(NOTE, "pending", at="2026-09-26T11:00:05+00:00"),
                _journal(NOTE, "completed", at="2026-09-26T11:05:00+00:00"),
            ],
        )
        current = initial_projection(settled, NOW)
        assert current.feedback_note_ref == ""  # settled — no fence subject
        decision = RecoveryActions.decide(action, current)
        assert decision.allowed is False
        assert decision.reason.startswith(STALE_ACTION_REFUSAL)
        assert NOTE in decision.reason
        assert "settled" in decision.reason
        assert decision.current_state == current.state
        assert decision.safe_next_action  # a readable safe alternative

    def test_a_stale_action_cannot_mutate_a_different_command(self):
        action = self._plan_steer(self._pending_rows())
        moved = _rows(
            attempts=[_attempt()],
            feedback_events=[
                _journal(NOTE, "completed", at="2026-09-26T11:05:00+00:00"),
                _retry("9600", at="2026-09-26T11:06:00+00:00"),
                _journal("9600", "pending", at="2026-09-26T11:06:05+00:00"),
            ],
        )
        current = initial_projection(moved, NOW)
        assert current.feedback_note_ref == "9600"  # a DIFFERENT current subject
        decision = RecoveryActions.decide(action, current)
        assert decision.allowed is False
        assert decision.reason.startswith(STALE_ACTION_REFUSAL)
        assert "9600" in decision.reason

    def test_a_settled_world_decides_exactly_as_before(self):
        """No unresolved feedback command → no command fence: the #367
        ticket alone decides (a settled world never gains a new rung)."""
        rows = _rows(attempts=[_attempt()])  # no feedback events at all
        projection = initial_projection(rows, NOW)
        assert projection.feedback_note_ref == ""
        planned = RecoveryActions.plan(projection, "human:op", "approver")
        steer = next(item for item in planned if item.action == "steer")
        assert steer.expected_command_ref == ""
        decision = RecoveryActions.decide(steer, projection)
        assert decision.allowed is True

    def test_the_status_note_lines_render_the_pending_command(self):
        projection = initial_projection(self._pending_rows(), NOW)
        lines = status_note_lines(projection)
        command = [line for line in lines if line.startswith("Command:")]
        assert command == [f"Command: feedback {NOTE} is pending — RETRYING, not complete."]


# ---------------------------------------------------------------------------
# The bounded support slice
# ---------------------------------------------------------------------------


class TestCommandSupportSlice:
    def _rows(self) -> dict:
        return _rows(
            feedback_events=[
                _retry(NOTE, at="2026-09-26T11:00:00+00:00", detail="GitLab 503 boom " + "x" * 300),
                _journal(NOTE, "pending", at="2026-09-26T11:00:05+00:00"),
            ]
        )

    def test_the_slice_schema_and_sections(self):
        rows = self._rows()
        document = export_command_support(
            rows, coverage={"feedback_events": "present"}, retry_details=rows["feedback_events"]
        )
        assert document["schema"] == COMMAND_SUPPORT_SCHEMA
        assert sorted(document["sections"]) == [
            "commands",
            "next_steps",
            "relevant_errors",
            "settings_names",
        ]
        assert document["export"]["fields"] == "allowlisted"

    def test_only_the_declared_fields_serialize(self):
        rows = self._rows()
        document = export_command_support(
            rows, coverage={"feedback_events": "present"}, retry_details=rows["feedback_events"]
        )
        for row in document["sections"]["commands"]:
            assert set(row) <= set(COMMAND_SUPPORT_FIELDS["commands"])
        for row in document["sections"]["next_steps"]:
            assert set(row) <= set(COMMAND_SUPPORT_FIELDS["next_steps"])

    def test_relevant_errors_carry_the_typed_reason_bounded(self):
        rows = self._rows()
        document = export_command_support(
            rows, coverage={"feedback_events": "present"}, retry_details=rows["feedback_events"]
        )
        (error,) = document["sections"]["relevant_errors"]
        assert error["note_id"] == NOTE
        assert error["reason"] == "head_unavailable"
        assert error["status_code"] == 503
        assert error["observed"] is False
        assert len(error["detail"]) == 200  # the bounded excerpt

    def test_only_retry_rows_become_relevant_errors(self):
        rows = self._rows()
        document = export_command_support(
            rows,
            coverage={"feedback_events": "present"},
            retry_details=[*rows["feedback_events"], _journal(NOTE, "pending", at="x")],
        )
        assert all(
            error["reason"] and error["observed"] is False
            for error in document["sections"]["relevant_errors"]
        )

    def test_redactions_are_counted(self):
        """A credential-shaped excerpt the guard catches is replaced and
        COUNTED — ``support.bundle_redactions`` never a silent scrub."""
        rows = _rows(
            feedback_events=[
                _retry(
                    NOTE,
                    at="2026-09-26T11:00:00+00:00",
                    detail="provider said glpat-9428abcdef0123456789",
                ),
            ]
        )
        document = export_command_support(
            rows, coverage={"feedback_events": "present"}, retry_details=rows["feedback_events"]
        )
        assert document["export"]["redactions"] >= 1
        assert document["export"]["redactions_measure"] == "support.bundle_redactions"
        assert "glpat-9428" not in json.dumps(document)
        assert "[redacted]" in json.dumps(document)

    def test_settings_names_never_values(self):
        rows = self._rows()
        document = export_command_support(
            rows, coverage={"feedback_events": "present"}, retry_details=rows["feedback_events"]
        )
        names = [entry["name"] for entry in document["sections"]["settings_names"]]
        assert names == [entry["name"] for entry in COMMAND_RECOVERY_SETTINGS]
        assert set(names) == {"FORGE_MAX_REVIEW_ROUNDS", "FORGE_APPROVERS", "STEP_MAX_ATTEMPTS"}
        for entry in document["sections"]["settings_names"]:
            assert set(entry) == {"name", "governs"}

    def test_entries_are_bounded(self):
        many = _rows(
            feedback_events=[
                _journal(f"9{i:03d}", "pending", at=f"2026-09-26T1{i % 10}:00:00+00:00")
                for i in range(20)
            ]
        )
        document = export_command_support(
            many,
            coverage={"feedback_events": "present"},
            retry_details=many["feedback_events"],
            max_entries=5,
        )
        assert len(document["sections"]["commands"]) == 5
        assert document["export"]["max_entries_per_section"] == 5
        assert document["export"]["truncated"]["commands"] is True

    def test_the_slice_is_value_free(self):
        """The reviewer's note TEXT never rides: the request document's
        body is dropped by the allowlist (the bounded provider-error
        excerpt IS the slice's declared error surface — a provider error
        string, never the reviewer's prompt)."""
        rows = _rows(
            run=_run(
                evidence={
                    "review_feedback_requests": {
                        NOTE: {**_request(NOTE, "recorded"), "text": NOTE_TEXT}
                    }
                }
            ),
            feedback_events=[_retry(NOTE, at="2026-09-26T11:00:00+00:00")],
        )
        document = export_command_support(
            rows, coverage={"feedback_events": "present"}, retry_details=rows["feedback_events"]
        )
        assert NOTE_TEXT not in json.dumps(document)


# ---------------------------------------------------------------------------
# The MR status comment — one identity per world, pending is visible
# ---------------------------------------------------------------------------


class TestStatusCommentParity:
    def _pending_rows(self) -> dict:
        return _rows(
            feedback_events=[
                _retry(NOTE, at="2026-09-26T11:00:00+00:00"),
                _journal(NOTE, "pending", at="2026-09-26T11:00:05+00:00"),
            ]
        )

    def test_the_transient_is_pending_on_all_three_surfaces(self):
        """CLI (status_note_lines), API (command_progress) and the native
        comment render the SAME subject and outcome word — the R42-01
        transient never reads complete on any surface."""
        rows = self._pending_rows()
        document = command_progress(rows, coverage={"feedback_events": "present"}, now=NOW)
        projection = initial_projection(rows, NOW)
        comment = render_status_comment(rows)

        assert document["commands"][-1]["outcome"] == "pending"
        assert projection.feedback_outcome == "pending"
        assert projection.feedback_note_ref == NOTE
        assert f"feedback `{NOTE}` is pending — RETRYING, not complete" in comment
        assert any(
            line.startswith(f"Command: feedback {NOTE} is pending")
            for line in status_note_lines(projection)
        )

    def test_the_comment_speaks_for_the_unresolved_command_first(self):
        """A settled command never masks a retrying one: the comment
        names the SAME subject the projection's actions fence."""
        rows = _rows(
            feedback_events=[
                _journal(NOTE, "pending", at="2026-09-26T11:00:00+00:00"),
                _journal("9900", "completed", at="2026-09-26T11:30:00+00:00"),
            ]
        )
        comment = render_status_comment(rows)
        assert f"feedback `{NOTE}` is pending — RETRYING, not complete" in comment
        projection = initial_projection(rows, NOW)
        assert projection.feedback_note_ref == NOTE

    def test_replayed_delivery_collapses_to_one_comment_identity(self):
        rows = self._pending_rows()
        first = render_status_comment(rows)
        second = render_status_comment(rows)
        assert first == second  # byte-identical replay — ONE current status
        moved = _rows(
            feedback_events=[
                _retry(NOTE, at="2026-09-26T11:00:00+00:00"),
                _journal(NOTE, "pending", at="2026-09-26T11:00:05+00:00"),
                _journal(NOTE, "completed", at="2026-09-26T11:05:00+00:00"),
            ]
        )
        settled = render_status_comment(moved)
        assert settled != first  # a moved outcome is a different world


# ---------------------------------------------------------------------------
# The observability fold — three separate records, never blended
# ---------------------------------------------------------------------------


class TestCommandProgressLimitsFold:
    def _rows(self) -> dict:
        return _rows(
            feedback_events=[
                _retry("9400", at="2026-09-26T10:00:00+00:00"),
                _journal("9400", "completed", at="2026-09-26T10:10:00+00:00", reason="staged"),
                _journal("9500", "exhausted", at="2026-09-26T10:30:00+00:00"),
                _journal(NOTE, "pending", at="2026-09-26T11:00:05+00:00"),
            ]
        )

    def _fold(self) -> dict:
        return command_progress_limits_fold(
            self._rows(),
            projection={"state": "executing"},
            as_of="2026-09-26T12:00:00+00:00",
            coverage={"feedback_events": "present"},
        )

    def test_accepted_to_applied_seconds_over_the_journal_windows(self):
        record = self._fold()[COMMAND_ACCEPTED_TO_APPLIED]
        assert record["measure"] == "command.accepted_to_applied_seconds"
        assert record["population"] == 1
        (sample,) = record["samples"]
        assert sample["note_id"] == "9400"
        assert sample["from"] == "2026-09-26T10:00:00+00:00"
        assert sample["seconds"] == 600.0
        assert record["not_yet_applied"] == 2  # exhausted + pending, never zero-filled

    def test_manual_rescue_minutes_counts_only_settled_seams(self):
        record = self._fold()[MANUAL_RESCUE_MINUTES]
        assert record["measure"] == "operator.manual_rescue_minutes"
        (window,) = record["open_windows"]
        assert window["note_id"] == "9500"
        assert window["outcome"] == "exhausted"
        assert window["open_wait_minutes"] == 90.0
        assert "lower bound" in record["definition"]

    def test_projection_lag_is_its_own_gauge(self):
        lag = self._fold()[PROJECTION_LAG]
        assert lag["measure"] == "status.projection_lag"
        assert lag["kind"] == "gauge"
        (sample,) = lag["samples"]
        assert sample["from"] == "2026-09-26T11:00:05+00:00"  # the newest fact
        assert sample["seconds"] == 3595.0
        assert lag["projection_inconsistent"] is False

    def test_an_unobserved_authority_renders_unknown_records(self):
        fold = command_progress_limits_fold(
            _rows(), projection={"state": "executing"}, as_of="2026-09-26T12:00:00+00:00"
        )
        for name in (COMMAND_ACCEPTED_TO_APPLIED, MANUAL_RESCUE_MINUTES, PROJECTION_LAG):
            assert fold[name]["coverage"] == "unknown", name
            assert fold[name]["samples"] == [], name

    def test_the_fold_rides_the_read_model(self):
        from forge.adaptive.ops_limits import ops_limits_read_model

        document = ops_limits_read_model(
            self._rows(),
            occupancy=[],
            coverage={"feedback_events": "present", "attempts": "present"},
            projection={"state": "executing", "unresolved_effects": ()},
            as_of="2026-09-26T12:00:00+00:00",
        )
        block = document["command_progress"]
        assert block[COMMAND_ACCEPTED_TO_APPLIED]["population"] == 1
        assert block[PROJECTION_LAG]["kind"] == "gauge"
        # the four ops.* measures stay four — the command records never join
        assert set(document["measures"]) - {"schema", "as_of", "separation"} == {
            "ops.command_application_latency",
            "ops.native_occupancy",
            "ops.recovery_duration",
            "ops.manual_intervention_minutes",
        }


# ---------------------------------------------------------------------------
# The reader section — the #374 journals through the live snapshot reader
# ---------------------------------------------------------------------------

SUBJECT = CanonicalSubject(provider_family="github", connection="", native_id="owner/alpha")


class TestReaderFeedbackEventsSection:
    @pytest.fixture()
    async def session_factory(self):
        from forge.models.base import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
        await engine.dispose()

    async def _seed_run(self, session_factory, *rows) -> None:
        from forge.durable.models import FlowRun

        async with session_factory() as session:
            session.add(
                FlowRun(
                    id=RUN_ID,
                    project_id=1,
                    provider="github",
                    github_repo_full_name="owner/alpha",
                    status="validating",
                    base_sha="b" * 40,
                    candidate_shas=[],
                    plan_digest="p" * 64,
                    evidence={},
                    created_at=NOW - timedelta(hours=3),
                    updated_at=NOW - timedelta(minutes=10),
                )
            )
            session.add_all(rows)
            await session.commit()

    async def test_the_journals_read_as_their_own_section(self, session_factory):
        from forge.durable.models import EventInbox, Outbox

        await self._seed_run(
            session_factory,
            EventInbox(
                source_event_id="ab" * 32,
                project_id=1,
                event_type="run_command",
                payload={"command": "fix", "project_id": 1, "note_id": NOTE},
                received_at=NOW - timedelta(hours=1),
            ),
            Outbox(
                flow_run_id=RUN_ID,
                event_type=PROVIDER_OBSERVATION_RETRY_EVENT,
                payload={
                    "run_id": RUN_ID,
                    "note_id": NOTE,
                    "reason": "head_unavailable",
                    "status_code": 503,
                    "observed": False,
                    "detail": NOTE_TEXT,
                },
                created_at=NOW - timedelta(minutes=55),
            ),
            Outbox(
                flow_run_id=RUN_ID,
                event_type=FEEDBACK_OUTCOME_EVENT,
                payload={
                    "run_id": RUN_ID,
                    "note_id": NOTE,
                    "outcome": "pending",
                    "reason": "head_unavailable",
                },
                created_at=NOW - timedelta(minutes=54),
            ),
        )
        reader = OperatorSnapshotReader(session_factory, clock=lambda: NOW)
        snapshot = await reader.snapshot(
            RUN_ID, [SUBJECT], sections=("run", "inbox", "feedback_events")
        )
        assert snapshot is not None
        events = snapshot.rows["feedback_events"]
        assert [row["event_type"] for row in events] == [
            PROVIDER_OBSERVATION_RETRY_EVENT,
            FEEDBACK_OUTCOME_EVENT,
        ]
        assert snapshot.source_coverage["feedback_events"] == "present"
        assert snapshot.section_totals["feedback_events"] == 2
        # the journaled error excerpt rides for the SUPPORT export; the
        # projection renders only the typed reason (value freedom below)
        assert events[0]["detail"] == NOTE_TEXT
        document = command_progress(snapshot.rows, coverage=snapshot.source_coverage, now=NOW)
        assert document["commands"][0]["outcome"] == "pending"
        assert document["commands"][0]["retries"]["count"] == 1
        assert document["commands"][0]["first_observed_at"]  # the inbox receipt
        assert NOTE_TEXT not in json.dumps(document)

    async def test_an_unselected_section_is_never_queried(self, session_factory):
        await self._seed_run(session_factory)
        reader = OperatorSnapshotReader(session_factory, clock=lambda: NOW)
        snapshot = await reader.snapshot(RUN_ID, [SUBJECT], sections=("run",))
        assert snapshot is not None
        assert "feedback_events" not in snapshot.rows
        assert snapshot.source_coverage["feedback_events"] == "unknown"

    async def test_a_limited_window_reports_the_authority_total(self, session_factory):
        from forge.durable.models import Outbox

        await self._seed_run(
            session_factory,
            *(
                Outbox(
                    flow_run_id=RUN_ID,
                    event_type=FEEDBACK_OUTCOME_EVENT,
                    payload={
                        "run_id": RUN_ID,
                        "note_id": f"9{i:03d}",
                        "outcome": "completed",
                        "reason": "staged",
                    },
                    created_at=NOW - timedelta(minutes=60 - i),
                )
                for i in range(5)
            ),
        )
        reader = OperatorSnapshotReader(session_factory, clock=lambda: NOW)
        snapshot = await reader.snapshot(
            RUN_ID, [SUBJECT], sections=("run", "feedback_events"), section_limit=2
        )
        assert snapshot is not None
        assert len(snapshot.rows["feedback_events"]) == 2
        assert snapshot.section_totals["feedback_events"] == 5
        assert snapshot.section_truncated["feedback_events"] is True


# ---------------------------------------------------------------------------
# The live surface — the API detail and bundle arms
# ---------------------------------------------------------------------------


class TestCommandProgressApiSurface:
    @pytest.fixture()
    async def app(self, tmp_path):
        from pydantic import SecretStr

        from forge.config import Settings
        from forge.database import reset_engine
        from forge.main import create_app

        reset_engine()
        self.secret = "operator-secret"
        settings = Settings(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
            DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/command-progress.db",
            LITELLM_URL="http://litellm:4000",
            REDIS_URL=None,
            FORGE_CAPTURE_DIR=None,
            FORGE_BOT_TOKEN=None,
            FORGE_BOT_USERNAME="forge-bot",
            FORGE_LANE_CONTROL_SECRET=SecretStr(self.secret),
        )
        application = create_app(settings=settings)
        async with application.router.lifespan_context(application):
            yield application
        reset_engine()

    @pytest.fixture()
    async def client(self, app):
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://forge.test") as http:
            yield http

    async def _seed_transient(self, app) -> None:
        from forge.durable.models import FlowRun, Outbox

        async with app.state.session_factory() as session:
            session.add(
                FlowRun(
                    id=RUN_ID,
                    project_id=1,
                    provider="github",
                    github_repo_full_name="owner/alpha",
                    status="validating",
                    base_sha="b" * 40,
                    candidate_shas=[CANDIDATE],
                    plan_digest="p" * 64,
                    evidence={
                        "review_feedback_requests": {
                            NOTE: {**_request(NOTE, "recorded"), "text": NOTE_TEXT}
                        }
                    },
                    created_at=NOW - timedelta(hours=3),
                    updated_at=NOW - timedelta(minutes=10),
                )
            )
            session.add(
                Outbox(
                    flow_run_id=RUN_ID,
                    event_type=PROVIDER_OBSERVATION_RETRY_EVENT,
                    payload={
                        "run_id": RUN_ID,
                        "note_id": NOTE,
                        "reason": "head_unavailable",
                        "status_code": 503,
                        "observed": False,
                        "detail": "GitLab 503: branch read faulted",
                    },
                    created_at=NOW - timedelta(minutes=55),
                )
            )
            session.add(
                Outbox(
                    flow_run_id=RUN_ID,
                    event_type=FEEDBACK_OUTCOME_EVENT,
                    payload={
                        "run_id": RUN_ID,
                        "note_id": NOTE,
                        "outcome": "pending",
                        "reason": "head_unavailable",
                    },
                    created_at=NOW - timedelta(minutes=54),
                )
            )
            await session.commit()

    def _auth(self) -> tuple[dict[str, str], str]:
        from forge.api_operator import operator_subject_scope_token

        token = operator_subject_scope_token(self.secret, [SUBJECT])
        return {"Authorization": f"Bearer {token}"}, f"subject={SUBJECT.subject_id()}"

    async def test_the_detail_renders_the_command_progress_block(self, app, client):
        await self._seed_transient(app)
        headers, query = self._auth()
        response = await client.get(f"/operator/runs/{RUN_ID}?{query}", headers=headers)
        assert response.status_code == 200
        document = response.json()
        progress = document["command_progress"]
        assert progress["schema"] == COMMAND_PROGRESS_SCHEMA
        assert progress["commands"][0]["outcome"] == "pending"
        assert progress["summary"]["complete"] is False
        # the journal outranks the recorded request: pending, not accepted
        assert progress["next_steps"][0]["code"] == "unresolved_required_discussion"
        assert progress["next_steps"][-1]["code"] == "provider_outage"
        # the observability fold rides the ops_limits block
        block = document["ops_limits"]["command_progress"]
        assert block[PROJECTION_LAG]["measure"] == "status.projection_lag"
        assert block[COMMAND_ACCEPTED_TO_APPLIED]["not_yet_applied"] >= 1
        assert NOTE_TEXT not in json.dumps(progress)  # value freedom end to end

    async def test_the_bundle_carries_the_command_support_slice(self, app, client):
        await self._seed_transient(app)
        headers, query = self._auth()
        response = await client.get(
            f"/operator/runs/{RUN_ID}/support-bundle?{query}", headers=headers
        )
        assert response.status_code == 200
        document = response.json()
        support = document["command_support"]
        assert support["schema"] == COMMAND_SUPPORT_SCHEMA
        assert support["sections"]["commands"][0]["outcome"] == "pending"
        (error,) = support["sections"]["relevant_errors"]
        assert error["reason"] == "head_unavailable"
        assert error["status_code"] == 503
        assert error["detail"].startswith("GitLab 503")  # the bounded excerpt rides
        names = [entry["name"] for entry in support["sections"]["settings_names"]]
        assert "FORGE_APPROVERS" in names
        assert support["export"]["redactions_measure"] == "support.bundle_redactions"
        # the reviewer's task TEXT never enters the bundle — a scoped
        # reader cannot infer it from the export (allowlists by construction)
        assert NOTE_TEXT not in json.dumps(document)

    async def test_a_narrowed_bundle_still_carries_the_command_axis(self, app, client):
        """``?sections=`` narrows the BUNDLE's export scope, never the
        command slice's sources — the axis never silently renders
        unknown because the caller narrowed the export."""
        await self._seed_transient(app)
        headers, query = self._auth()
        response = await client.get(
            f"/operator/runs/{RUN_ID}/support-bundle?{query}&sections=attempts", headers=headers
        )
        assert response.status_code == 200
        document = response.json()
        assert document["export"]["scope"] == ["attempts"]
        assert document["command_support"]["sections"]["commands"][0]["outcome"] == "pending"

    async def test_a_scoped_reader_cannot_read_another_repositorys_run(self, app, client):
        from forge.durable.models import FlowRun

        await self._seed_transient(app)
        async with app.state.session_factory() as session:
            session.add(
                FlowRun(
                    id="b" * 32,
                    project_id=2,
                    provider="github",
                    github_repo_full_name="owner/beta",
                    status="validating",
                    base_sha="b" * 40,
                    candidate_shas=[],
                    plan_digest="p" * 64,
                    evidence={},
                    created_at=NOW - timedelta(hours=3),
                    updated_at=NOW - timedelta(minutes=10),
                )
            )
            await session.commit()
        headers, query = self._auth()
        detail = await client.get(f"/operator/runs/{'b' * 32}?{query}", headers=headers)
        bundle = await client.get(
            f"/operator/runs/{'b' * 32}/support-bundle?{query}", headers=headers
        )
        assert detail.status_code == 404  # indistinguishable from unknown
        assert bundle.status_code == 404
