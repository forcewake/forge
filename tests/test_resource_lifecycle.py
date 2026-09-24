"""R37-18 (issue #299) — the resource-lifecycle leak gate and the
composed regression arms.

The observed basis (upstream CI, Python 3.13): aiosqlite connection
worker threads reached CLOSED event loops in the older two-writer and
retry tests — ``RuntimeError('Event loop is closed')`` raised inside
``_connection_worker_thread``, surfaced by pytest as a
``PytestUnhandledThreadExceptionWarning`` and attributed to WHATEVER
test the garbage collector happened to interrupt (the reviewed run
carried 11 such warnings). Not a production outage, but teardown leaks
smear failures across the report and make them unattributable.

What this module pins:

- **the leak is fixed at the creator** (not silenced at the warning
  site): ``tests/test_checkpoint_retry_authority.py``'s
  ``_sqlite_factory`` handed its engine to callers that dropped it —
  six engines per run whose worker threads outlived their loops. It is
  now a context manager that disposes in a finally block, and
  ``tests/test_saga_durable.py`` drains every ``_process`` engine
  through an autouse disposer so a FAILING test cannot leak either.
- **the warning gate**: each previously-leaking file runs in a
  subprocess with exactly ONE warning escalated to an error —
  ``pytest.PytestUnhandledThreadExceptionWarning``, the specific shape
  of a background thread dying against a closed loop. Nothing else is
  suppressed; deprecations stay warnings and must appear in the tracked
  list below to be accepted.
- **the gate bites**: a canary test that leaks an exception in a
  background thread FAILS under the same filter — a gate that cannot
  fail protects nothing.
- **the composed regression arms** (issue acceptance 4): the three
  inverse-condition tests landed beside their production guards —
  (a) a NEW retry binds its own decision while the SAME event replays
  the frozen one, (b) the CURRENT candidate wins over a historical list
  member, (c) an UNBOUND report is a typed obligation, never
  satisfaction beside an explicit mismatch. Those arms live in their
  owning files; this module cross-references them by node id so
  removing or renaming one refuses THIS gate, and pins the pure
  ``decision_identity`` seam the digest-only defect used to hide
  behind (equal digests never collide identities: the identity never
  sees the digest).
- **the PG gate reports executed IDs** (#267): one cheap presence
  assertion — the report a qualification run writes still carries its
  collected and executed test ids, so dropping that contract refuses
  THIS gate (the behavior detail is owned by tests/test_pg_gate.py).
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from forge.adaptive.continuation import decision_identity

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The files the upstream run flagged (and this fix repaired). Each one
#: must run clean under the escalated warning filter below — REPEATEDLY,
#: because the leak was GC-timing dependent (it reproduced on ~3 of 8
#: runs before the fix, always smeared onto an unrelated test).
PREVIOUSLY_LEAKING_FILES: tuple[str, ...] = (
    "tests/test_saga_durable.py",
    "tests/production_entry/test_two_writer_durable.py",
    "tests/test_checkpoint_retry_authority.py",
    "tests/test_checkpoint_gc.py",
)

#: The GC-pressure order that reproduced the smeared attribution
#: upstream: the three files TOGETHER (the leaked engines' finalizers
#: fired while later files' tests were running).
LEAKING_TRIO: tuple[str, ...] = PREVIOUSLY_LEAKING_FILES[:3]

#: THE escalation — and the only one: a background thread dying with an
#: unhandled exception (an aiosqlite worker reaching a closed loop is
#: exactly this shape) fails the run. ``RuntimeError`` itself is not a
#: Warning subclass, so the gate escalates the warning class the leak
#: actually surfaces as. No global suppression is involved.
LEAK_GATE_FILTER = "error::pytest.PytestUnhandledThreadExceptionWarning"

#: Deprecations stay WARNINGS (never mistaken for failing behavior) but
#: must be consciously tracked here: a deprecation-family message from
#: the gated files that matches NONE of these substrings fails the gate,
#: forcing either a fix or an explicit entry with its compatibility
#: note. The list starts empty — the gated files are deprecation-free.
TRACKED_DEPRECATIONS: tuple[str, ...] = ()

#: The composed regression arms (issue acceptance 4) — each inverse
#: condition landed beside its production guard in the owning file:
#:   (a) new retry vs same event — the production-entry AT-01 seam, its
#:       mutation arm, and the always-running sqlite shape;
#:   (b) current candidate vs historical list member;
#:   (c) unbound report vs explicit mismatch.
REQUIRED_ARMS: dict[str, tuple[str, ...]] = {
    "new retry vs same event": (
        "tests/production_entry/test_continuation_identity.py::"
        "TestAT01TwoEventsTwoAttemptsTwoCheckpoints::"
        "test_e2_binds_attempt_2_and_checkpoint_b_and_e1_replay_changes_nothing",
        "tests/production_entry/test_continuation_identity.py::"
        "TestMutationDigestOnlyMatchingFails::"
        "test_reverting_to_digest_only_matching_restores_the_defect",
        "tests/test_checkpoint_retry_authority.py::"
        "TestServiceRetryAuthority::"
        "test_a_post_decision_upload_never_changes_the_dispatched_reference",
    ),
    "current candidate vs historical list member": (
        "tests/test_adaptive_operator_view.py::TestStateDerivation::"
        "test_an_explicit_active_candidate_pointer_wins_over_the_list_order",
        "tests/test_adaptive_operator_view.py::TestStateDerivation::"
        "test_a_pass_for_an_earlier_candidate_is_history_not_readiness",
    ),
    "unbound report vs explicit mismatch": (
        "tests/test_report_inventory_strict.py::TestStrictInventoryMatching::"
        "test_p04_shape_is_unbound_never_satisfied",
        "tests/test_report_inventory_strict.py::TestStrictInventoryMatching::"
        "test_wrong_path_and_wrong_bundle_are_unmatched_not_assigned",
    ),
}


def _run_pytest(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """A pytest subprocess exactly as CI would run it — never the
    in-process session (the closed-loop leak lives in interpreter
    teardown, which an in-process run cannot observe)."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + "\n" + (result.stderr or "")


_DEPRECATION_LINE = re.compile(r":\s*(DeprecationWarning|PendingDeprecationWarning):\s*(.+)$")


def _untracked_deprecations(stdout: str) -> list[str]:
    """Deprecation-family messages a run's warnings summary carries that
    match NOTHING in ``TRACKED_DEPRECATIONS``. Parses pytest's summary
    lines (``path:line: DeprecationWarning: message``) in order."""
    untracked: list[str] = []
    for line in stdout.splitlines():
        match = _DEPRECATION_LINE.search(line.strip())
        if match is None:
            continue
        message = match.group(2).strip()
        if not any(pattern in message for pattern in TRACKED_DEPRECATIONS):
            if message not in untracked:
                untracked.append(message)
    return untracked


# ---------------------------------------------------------------------------
# 1. The warning gate — repeated runs of the previously-leaking files.
# ---------------------------------------------------------------------------


class TestTeardownLeaksNoLiveWorkerThreads:
    @pytest.mark.parametrize("file", PREVIOUSLY_LEAKING_FILES)
    def test_each_previously_leaking_file_runs_clean_with_the_specific_warning_fatal(
        self, file: str
    ) -> None:
        for _round in range(2):  # REPEATED runs — the leak was timing dependent
            result = _run_pytest(file, "-q", "-W", LEAK_GATE_FILTER)
            output = _combined(result)
            assert result.returncode == 0, output[-4000:]
            assert "Event loop is closed" not in output
            assert "PytestUnhandledThreadExceptionWarning" not in output

    def test_the_gc_pressure_trio_runs_clean_with_the_specific_warning_fatal(self) -> None:
        """The composed order that smeared the leak onto unrelated
        tests upstream: the three files together, one process."""
        result = _run_pytest(*LEAKING_TRIO, "-q", "-W", LEAK_GATE_FILTER)
        output = _combined(result)
        assert result.returncode == 0, output[-4000:]
        assert "Event loop is closed" not in output
        assert "PytestUnhandledThreadExceptionWarning" not in output

    def test_the_gate_bites_a_leaked_thread_exception_fails_the_run(self, tmp_path: Path) -> None:
        """The negative control: under the SAME filter, a test that
        leaks an exception in a background thread (the observable shape
        of an aiosqlite worker reaching a closed loop) must FAIL —
        otherwise the gate above is a no-op."""
        canary = tmp_path / "test_canary_leak.py"
        canary.write_text(
            textwrap.dedent(
                """
                import threading


                def test_leaks_a_thread_exception() -> None:
                    def boom() -> None:
                        raise RuntimeError("Event loop is closed")

                    worker = threading.Thread(target=boom)
                    worker.start()
                    worker.join()
                """
            ),
            encoding="utf-8",
        )
        result = _run_pytest(str(canary), "-q", "-W", LEAK_GATE_FILTER, "--rootdir", str(tmp_path))
        assert result.returncode != 0, _combined(result)

    def test_deprecations_stay_warnings_but_must_be_tracked(self) -> None:
        """Deprecations are NOT part of the leak gate (they stay
        warnings, never mistaken for failing behavior) — but the tracked
        list is exhaustive: a deprecation-family warning from the gated
        files that is not in it fails here, forcing a fix or a conscious
        entry with a compatibility note."""
        result = _run_pytest(*LEAKING_TRIO, "-q", "-W", LEAK_GATE_FILTER)
        assert result.returncode == 0, _combined(result)[-4000:]
        untracked = _untracked_deprecations(result.stdout)
        assert untracked == [], (
            f"untracked deprecations from the gated files: {untracked!r} — "
            "fix them or add an explicit entry (with its compatibility note) "
            "to TRACKED_DEPRECATIONS"
        )

    def test_the_deprecation_tracker_bites(self, tmp_path: Path) -> None:
        """The negative control: a canary deprecation from a gated-style
        run is DETECTED as untracked (and a tracked message passes) —
        otherwise the tracker above is a no-op."""
        canary = tmp_path / "test_canary_deprecation.py"
        canary.write_text(
            textwrap.dedent(
                """
                import warnings


                def test_emits_a_deprecation() -> None:
                    warnings.warn(
                        "the canary legacy alias is deprecated", DeprecationWarning
                    )
                """
            ),
            encoding="utf-8",
        )
        result = _run_pytest(str(canary), "-q", "--rootdir", str(tmp_path))
        assert result.returncode == 0, _combined(result)[-2000:]
        detected = _untracked_deprecations(result.stdout)
        assert detected == ["the canary legacy alias is deprecated"], (
            f"the tracker must see the canary deprecation: {detected!r}"
        )


# ---------------------------------------------------------------------------
# 2. The composed regression arms — present by node id, plus the pure
#    identity seam the digest-only defect used to hide behind.
# ---------------------------------------------------------------------------


class TestComposedRegressionArms:
    def _collected_ids(self, *files: str) -> set[str]:
        result = _run_pytest("--collect-only", "-q", *files)
        assert result.returncode == 0, _combined(result)[-4000:]
        return {
            line.strip()
            for line in result.stdout.splitlines()
            if "::" in line and not line.strip().startswith(("=", "-", " "))
        }

    @pytest.mark.parametrize(
        ("label", "node_ids"), [(label, ids) for label, ids in REQUIRED_ARMS.items()]
    )
    def test_every_required_inverse_condition_arm_is_collected(self, label: str, node_ids) -> None:
        """Each inverse-condition arm landed beside its production guard
        in the file that owns the seam (referenced, not duplicated). If
        one is removed or renamed, THIS gate refuses — the composed
        regression protection cannot be silently deleted."""
        files = sorted({node.split("::")[0] for node in node_ids})
        collected = self._collected_ids(*files)
        missing = [node for node in node_ids if node not in collected]
        assert missing == [], (
            f"the {label!r} regression arm(s) are missing from the suite: "
            f"{missing!r} — restore them or update REQUIRED_ARMS deliberately"
        )

    def test_decision_identity_separates_events_and_attempts_never_digests(self) -> None:
        """The pure seam behind arm (a): the identity is (subject, source
        attempt, native event). The SAME event replays the SAME id (a
        restart reconstructs the decision by id); a NEW event or a NEW
        source attempt mints a DIFFERENT id — and because the identity
        never sees the checkpoint digest, two events over byte-identical
        checkpoints can never collide (the pre-R37-01 digest-only trap).
        The production-entry proofs over this seam are PG-gated; this
        pin keeps the invariant visible in every default run."""
        subject = "run-r37-18"

        same_event = decision_identity(subject, 2, "delivery-e2")
        assert decision_identity(subject, 2, "delivery-e2") == same_event

        # A NEW event over an equal digest: different identity, always.
        assert decision_identity(subject, 2, "delivery-e3") != same_event
        # The same delivery id arriving from a LATER source attempt: its
        # own decision — never the earlier attempt's.
        assert decision_identity(subject, 3, "delivery-e2") != same_event
        # And the non-event-driven recovery scan's identity is its own.
        assert decision_identity(subject, 2, None) != same_event


# ---------------------------------------------------------------------------
# 3. The PG qualification gate reports executed test ids (#267) — one
#    cheap PRESENCE assertion. The behavior (manifest parsing, skip
#    classification, refusal hierarchy, report shape) is owned by
#    tests/test_pg_gate.py; this only pins that the report a
#    qualification run writes still CARRIES the executed IDs, so the
#    R37-18 composed gate notices if that contract is dropped.
# ---------------------------------------------------------------------------


class TestQualificationManifest:
    def test_the_pg_gate_report_still_carries_executed_test_ids(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "pg_gate_presence_check", REPO_ROOT / "scripts" / "pg_gate.py"
        )
        assert spec is not None and spec.loader is not None
        pg_gate = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pg_gate  # dataclasses resolves string annotations
        spec.loader.exec_module(pg_gate)

        manifest = pg_gate.parse_collected_manifest("tests/test_x.py::TestA::test_one\n")
        records = [pg_gate.TestRecord(test_id=manifest[0], outcome="passed", duration=0.1)]
        run = pg_gate.ProfileRun(
            profile=pg_gate.GateProfile(
                name="core", selection=("tests/test_x.py",), database_suffix="ck"
            ),
            database_url="postgresql://u:***@host/db",
            manifest=manifest,
            records=records,
        )
        report = pg_gate.build_report(
            mode="pytest-pg",
            admin_url_masked="postgresql://admin:***@host/postgres",
            head="head",
            runs=[run],
            started_iso="2026-09-24T00:00:00+00:00",
            total_duration=1.0,
            refusal=None,
            databases={},
        )
        profile_report = report["profiles"][0]
        assert profile_report["collected_test_ids"] == manifest
        assert profile_report["executed_test_ids"] == manifest
        assert "required_test_ids" in report["qualification"]
