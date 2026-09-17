"""Contract tests for terminal-failure classification and the revival schedule.

Tier 1 auto-revive (and the operator ``/retry`` on top of it) rests on a
shared, provider-neutral table: a terminal ``failed`` reason is either
*transient* (the environment tripped — re-dispatch the same branch) or
*fatal* (a human decides — park ``blocked``). Parity across the GitLab,
GitHub and Azure DevOps services is the contract, so the table lives in one
module and every service classifies through it.
"""

from datetime import datetime, timedelta, timezone

from forge.runs.failures import (
    FATAL,
    TRANSIENT,
    classify_terminal_failure,
    revival_count,
    revival_due,
    revival_due_at,
    revival_evidence,
    revival_pending,
    revival_repair_context,
    revival_state,
    strip_revival_schedule,
)


def test_classification_table() -> None:
    """The table: transient causes against fatal ones, unclassified → fatal."""
    transient = [
        # dispatch legs — including the live empty-error shape
        "harness_start_failed: ",
        "harness_start_failed: GitLab API error 502: Bad Gateway",
        "harness_start_failed: runner startup failed on the shard",
        # provider 5xx / rate limits
        "commit_failed: GitLab API error 503: Service Unavailable",
        "mr_failed: GitLab API error 429: slow down",
        "planning_failed: LLM error: connection refused",
        # timeouts and networks
        "proposal_failed: ReadTimeout",
        "commit_failed: network reset by peer",
        # dead runner
        "harness_infrastructure: runner_system_failure",
    ]
    fatal = [
        # config errors — a retry cannot fix them (the 422 family)
        "harness_start_failed: GitLab API error 422: Unexpected inputs: commit_sha",
        "harness_start_failed: GitHub API error 404: Not Found: workflow",
        "backend_config: missing workflow FORGE_GITHUB_HARNESS_WORKFLOW",
        # real quality signals and exhausted budgets
        "harness_code: exit code 1",
        "commit_cycles_exhausted: 3 of 3 commit cycles used",
        # unknown outcomes are never blind-retried (ADR-0005)
        "commit_unknown_outcome",
        "mr_unknown_outcome",
    ]
    for reason in transient:
        assert classify_terminal_failure(reason) == TRANSIENT, reason
    for reason in fatal:
        assert classify_terminal_failure(reason) == FATAL, reason
    # The default is fatal: an unclassified failure needs a human.
    assert classify_terminal_failure("something entirely new") == FATAL
    assert classify_terminal_failure("") == FATAL


def test_explicit_fatal_cause_outranks_a_transient_fragment() -> None:
    """A 4xx config error inside a dispatch leg stays fatal — never retried."""
    reason = "harness_start_failed: GitLab API error 502 wrapping 422: validation error"
    assert classify_terminal_failure(reason) == FATAL


def test_revival_backoff_is_bounded_and_doubling() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = revival_due_at(now, 1, 60)
    second = revival_due_at(now, 2, 60)
    assert first == now + timedelta(seconds=60)
    assert second == now + timedelta(seconds=120)
    # …and never grows past the ceiling.
    tenth = revival_due_at(now, 10, 600)
    assert tenth == now + timedelta(seconds=900)


def test_revival_state_roundtrip_and_garbage() -> None:
    due = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    evidence = revival_evidence(due, 2, "commit_failed: 503")
    assert evidence["revive_count"] == 2
    at, count = revival_state(evidence)
    assert at == due
    assert count == 2
    # Naive timestamps (SQLite) read back aware.
    naive = revival_evidence(due.replace(tzinfo=None), 1, "x")
    at, count = revival_state(naive)
    assert at is not None and at.tzinfo is not None
    assert count == 1
    # No schedule, or a corrupt one — reads as (None, 0), never raises.
    assert revival_state(None) == (None, 0)
    assert revival_state({}) == (None, 0)
    assert revival_state({"revive_at": "not-a-date", "revive_count": "x"}) == (None, 0)
    assert revival_count(None) == 0


def test_revival_pending_and_strip() -> None:
    now = datetime.now(timezone.utc)
    scheduled = revival_evidence(now, 1, "reason")
    assert revival_pending(scheduled) is True
    # The count survives the strip (the per-run bound); the schedule does not.
    stripped = strip_revival_schedule(scheduled)
    assert revival_pending(stripped) is False
    assert revival_count(stripped) == 1
    assert revival_pending(None) is False


def test_revival_due_skips_until_the_deadline() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    evidence = revival_evidence(now + timedelta(seconds=60), 1, "reason")
    assert revival_due(evidence, now) is False
    assert revival_due(evidence, now + timedelta(seconds=60)) is True
    # Nothing scheduled — never due.
    assert revival_due({}, now) is False


def test_revival_repair_context_carries_reason_and_verification() -> None:
    context = revival_repair_context(
        "commit_failed: 503",
        {"pipeline": {"id": 9, "status": "failed", "url": "https://ci/9"}},
    )
    assert "commit_failed: 503" in context
    assert "pipeline 9" in context and "failed" in context
    review = revival_repair_context(
        "harness_code: exit 1",
        {"review": {"verdict": "concerns", "summary": "weak tests"}},
    )
    assert "weak tests" in review and "concerns" in review
    # A run with neither reason nor evidence still yields a usable context.
    assert "died" in revival_repair_context(None, None)
