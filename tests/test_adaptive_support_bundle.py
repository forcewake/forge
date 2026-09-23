"""The exportable support bundle (R32-23) — one run's whole honest story.

``SupportBundle.build`` derives the evidence pack from the same durable
row shapes the operator view reads: attempts history with FAILED attempts
preserved (recovery never resets them), commands and deliveries,
checkpoints as ids + digests (never blobs), verification records, and the
projection state the bundle was built under. Coverage stays EXPLICIT
(``present | missing | unknown`` — never filled, never assumed), outcomes
forge cannot prove export as ``unknown``, every section is redacted, and
the digest covers content, never the clock.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from forge.adaptive.support_bundle import BUNDLE_SCHEMA, SupportBundle

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
RUN_ID = "f" * 32


def _run(**over) -> dict:
    row: dict = {
        "id": RUN_ID,
        "status": "planning",
        "base_sha": "b" * 40,
        "candidate_shas": ["c" * 40],
        "plan_digest": "p" * 64,
        "evidence": {},
        "blocked_reason": "",
        "cancel_requested": False,
        "created_at": "2026-09-23T09:00:00+00:00",
        "updated_at": "2026-09-23T09:00:00+00:00",
    }
    row.update(over)
    return row


def _attempt(status: str, *, attempt_id: str = "att-1", **over) -> dict:
    row: dict = {
        "attempt_id": attempt_id,
        "status": status,
        "started_at": "2026-09-23T11:00:00+00:00",
        "updated_at": "2026-09-23T11:50:00+00:00",
        "generation": 1,
    }
    row.update(over)
    return row


def _cmd(seq: int, kind: str, status: str, **over) -> dict:
    row: dict = {
        "command_id": f"cmd-{seq}",
        "work_id": "wp-1",
        "sequence": seq,
        "kind": kind,
        "status": status,
        "actor_ref": "human:op",
        "actor_origin": "server_authenticated_human",
        "created_at": "2026-09-23T11:00:00+00:00",
    }
    row.update(over)
    return row


def _full_rows() -> dict:
    return {
        "run": _run(),
        "attempts": [
            _attempt("failed", attempt_id="att-1"),
            _attempt("executing", attempt_id="att-2", generation=2),
        ],
        "commands": [_cmd(1, "pause", "checkpointed")],
        "deliveries": [{"command_id": "cmd-1", "recipient": "lane-1", "status": "acknowledged"}],
        "checkpoints": [
            {
                "checkpoint_id": "ck-1",
                "digest": "d" * 64,
                "committed_at": "2026-09-23T11:30:00+00:00",
                "fence": "held",
            }
        ],
        "verifications": [
            {"verification_id": "ver-1", "result": "passed", "candidate_sha": "c" * 40}
        ],
        "publications": [
            {"operation_key": "op-key-1", "status": "committed", "operation": "commit"}
        ],
        "approvals": [{"approved_by": "op@corp", "generation": 1}],
        "questions": [{"question_id": "q-1", "resolved": True}],
    }


class TestBundleCompleteness:
    def test_a_fully_populated_run_builds_a_complete_bundle(self):
        bundle = SupportBundle.build(RUN_ID, _full_rows(), now=NOW)

        assert bundle.schema == BUNDLE_SCHEMA == "forge.support.bundle/1"
        assert bundle.run_id == RUN_ID
        assert bundle.digest.startswith("sha256:")
        assert bundle.generated_at == NOW.isoformat()
        assert set(bundle.coverage.values()) == {"present"}
        assert [a["attempt_id"] for a in bundle.attempts] == ["att-1", "att-2"]
        assert bundle.commands[0]["command_id"] == "cmd-1"
        assert bundle.deliveries[0]["recipient"] == "lane-1"
        assert bundle.checkpoints[0]["checkpoint_id"] == "ck-1"
        assert bundle.verifications[0]["result"] == "passed"
        assert bundle.publications[0]["operation_key"] == "op-key-1"
        assert bundle.approvals[0]["approved_by"] == "op@corp"
        assert bundle.projection["run_id"] == RUN_ID
        assert bundle.projection["state"] == "safely_paused"

    def test_the_document_carries_every_section_and_stamps(self):
        bundle = SupportBundle.build(RUN_ID, _full_rows(), now=NOW)

        document = bundle.as_document()

        assert document["schema"] == "forge.support.bundle/1"
        assert document["digest"] == bundle.digest
        for section in (
            "attempts",
            "commands",
            "deliveries",
            "checkpoints",
            "verifications",
            "publications",
            "approvals",
            "questions",
        ):
            assert document[section], section
        assert json.loads(bundle.to_json())["schema"] == "forge.support.bundle/1"


class TestExplicitCoverage:
    def test_an_absent_section_is_unknown_never_assumed(self):
        rows = _full_rows()
        del rows["verifications"]
        del rows["deliveries"]

        bundle = SupportBundle.build(RUN_ID, rows, now=NOW)

        assert bundle.coverage["verifications"] == "unknown"
        assert bundle.coverage["deliveries"] == "unknown"
        assert bundle.verifications == ()
        assert bundle.deliveries == ()

    def test_an_observed_empty_section_is_missing_not_filled(self):
        rows = _full_rows()
        rows["publications"] = []
        rows["questions"] = []

        bundle = SupportBundle.build(RUN_ID, rows, now=NOW)

        assert bundle.coverage["publications"] == "missing"
        assert bundle.coverage["questions"] == "missing"
        assert bundle.publications == ()

    def test_missing_data_never_becomes_a_guess_elsewhere(self):
        """A run with no verification rows: the projection says
        ``unverified`` (a candidate without passed verification), the
        coverage says where the absence came from — nothing invents a
        verification."""
        rows = _full_rows()
        rows["verifications"] = []

        bundle = SupportBundle.build(RUN_ID, rows, now=NOW)

        assert bundle.coverage["verifications"] == "missing"
        assert bundle.projection["state"] == "safely_paused"  # pause outranks candidate states

    def test_unprovable_outcomes_stay_unknown(self):
        rows = _full_rows()
        rows["attempts"] = [{"attempt_id": "att-x", "generation": 4}]
        rows["commands"] = [{"command_id": "cmd-x", "kind": "steer"}]
        rows["verifications"] = [{"verification_id": "ver-x"}]

        bundle = SupportBundle.build(RUN_ID, rows, now=NOW)

        assert bundle.attempts[0]["outcome"] == "unknown"
        assert bundle.commands[0]["status"] == "unknown"
        assert bundle.verifications[0]["result"] == "unknown"


class TestHistoryPreservation:
    def test_failed_attempts_survive_recovery(self):
        """Recovery never resets history: a failed attempt followed by a
        fresh executing one exports BOTH, outcomes verbatim."""
        bundle = SupportBundle.build(RUN_ID, _full_rows(), now=NOW)

        outcomes = {a["attempt_id"]: a["outcome"] for a in bundle.attempts}

        assert outcomes == {"att-1": "failed", "att-2": "executing"}

    def test_a_late_recovery_does_not_rewrite_the_earlier_failure(self):
        rows = _full_rows()
        rows["attempts"] = [_attempt("failed", attempt_id="att-1")]
        failed = SupportBundle.build(RUN_ID, rows, now=NOW)

        rows["attempts"].append(_attempt("executing", attempt_id="att-2", generation=2))
        recovered = SupportBundle.build(RUN_ID, rows, now=NOW)

        assert failed.attempts[0]["outcome"] == "failed"
        assert recovered.attempts[0]["outcome"] == "failed"  # untouched by the recovery


class TestRedaction:
    def test_secret_values_never_reach_the_bundle(self):
        rows = _full_rows()
        rows["commands"][0]["actor_ref"] = "sk-abcdefghijklmn"
        rows["checkpoints"][0]["digest"] = "ghp_abcdefghijklmn"
        rows["approvals"][0]["approved_by"] = "glpat-abcdefghijkl"

        bundle = SupportBundle.build(RUN_ID, rows, now=NOW)
        rendered = bundle.to_json()

        assert "sk-abcdefghijklmn" not in rendered
        assert "ghp_abcdefghijklmn" not in rendered
        assert "glpat-abcdefghijkl" not in rendered
        assert bundle.commands[0]["actor_ref"] == "[redacted]"
        assert bundle.checkpoints[0]["digest"] == "[redacted]"
        assert bundle.approvals[0]["approved_by"] == "[redacted]"

    def test_checkpoint_entries_carry_ids_and_digests_never_blobs(self):
        rows = _full_rows()
        rows["checkpoints"][0]["blobs"] = {"src/main.py": "def secret_pipeline(): ..."}
        rows["checkpoints"][0]["manifest"] = {"files": ["src/main.py"]}

        bundle = SupportBundle.build(RUN_ID, rows, now=NOW)

        assert set(bundle.checkpoints[0]) == {
            "checkpoint_id",
            "digest",
            "committed_at",
            "activated_at",
            "fence",
            "sequence",
        }
        assert "secret_pipeline" not in bundle.to_json()


class TestDigestDeterminism:
    def test_the_same_rows_digest_identically_whatever_the_clock(self):
        rows = _full_rows()

        first = SupportBundle.build(RUN_ID, rows, now=NOW)
        second = SupportBundle.build(
            RUN_ID, rows, now=datetime(2026, 9, 23, 18, 30, 0, tzinfo=timezone.utc)
        )

        assert first.digest == second.digest
        assert first.generated_at != second.generated_at  # the clock moved, the content did not

    def test_different_rows_digest_differently(self):
        first = SupportBundle.build(RUN_ID, _full_rows(), now=NOW)
        rows = _full_rows()
        rows["attempts"].append(_attempt("cancelled", attempt_id="att-3"))
        second = SupportBundle.build(RUN_ID, rows, now=NOW)

        assert first.digest != second.digest


class TestGuardrails:
    def test_a_bundle_never_mixes_runs(self):
        rows = _full_rows()
        rows["run"] = _run(id="e" * 32)

        with pytest.raises(ValueError, match="never mixes runs"):
            SupportBundle.build(RUN_ID, rows, now=NOW)

    def test_a_bundle_without_a_run_row_is_nothing(self):
        with pytest.raises(ValueError, match="no run row"):
            SupportBundle.build(RUN_ID, {"attempts": [_attempt("executing")]}, now=NOW)
