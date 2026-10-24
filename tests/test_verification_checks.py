"""Native check attempts stay independent, current, unambiguous and SHA-bound."""

from datetime import datetime, timedelta, timezone

from forge.port_values import CheckResult
from forge.verification.checks import code_failure, verdict

SHA = "a" * 40
NOW = datetime.now(timezone.utc)


def check(**changes):
    return CheckResult(
        **{
            "name": "unit",
            "tested_sha": SHA,
            "native_id": "check-1",
            "native_run_id": "run-1",
            "workflow_path": ".github/workflows/ci.yml",
            "attempt": 1,
            "role": "verification",
            "status": "passed",
            "observed_at": NOW,
            "evidence": "native:opaque",
            **changes,
        }
    )


def reduce(*checks, **changes):
    return verdict(checks, SHA, ("unit",), now=NOW, max_age_seconds=60, **changes)


def test_newer_pending_attempt_cannot_reuse_an_old_green():
    old = check(observed_at=NOW - timedelta(seconds=1))
    active = check(native_id="check-2", attempt=2, status="pending")
    result = reduce(old, active)
    assert result.status == "pending" and not result.successful
    newer = active.model_copy(update={"status": "passed"})
    assert reduce(old, newer).status == "passed"


def test_ambiguous_check_identity_or_classification_cannot_authorize_repair():
    a = check(status="failed", failure_kind="code")
    b = check(status="failed", failure_kind="unknown")
    result = reduce(a, b)
    assert result.status == "unknown" and not code_failure((a, b), result)
    c = check(native_id="other")
    assert reduce(check(), c).status == "unknown"


def test_missing_lane_stale_foreign_future_and_cancelled_checks_never_pass():
    assert reduce(check(role="lane")).status == "pending"
    assert reduce(missing_is_final=True).status == "not_configured"
    assert reduce(check(tested_sha="f" * 40)).status == "stale"
    assert reduce(check(observed_at=NOW - timedelta(seconds=61))).status == "stale"
    assert reduce(check(observed_at=NOW + timedelta(seconds=1))).status == "stale"
    assert reduce(check(status="cancelled")).status == "unknown"


def test_only_explicit_code_failure_is_repairable_and_native_evidence_is_opaque():
    for failure_kind in ("infrastructure", "configuration", "unknown", "code"):
        native = check(
            status="failed",
            failure_kind=failure_kind,
            evidence='not JSON and not interpreted: {"secret-code": "code"}',
        )
        result = reduce(native)
        assert code_failure((native,), result) == (failure_kind == "code")
