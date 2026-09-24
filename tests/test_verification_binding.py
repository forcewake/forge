"""R36-14 — bind independent verification to the EXACT candidate and world.

Issue #273: candidate integrity and harness success are necessary but
different from acceptance evidence. These tests pin the binding:

- the verdict NAMES its subject (candidate digest, source OID, tested
  OID recorded separately, generation, plan/revision digest, tested
  environment profile) — ``verification.subject_identity``;
- the freshness gate: a passed record whose subject digest differs from
  the CURRENT candidate's renders ``stale`` and never verified_ready;
- the expected-report inventory frozen with the work contract — a
  missing report, skipped required check or report from an OLDER
  attempt cannot produce verified_ready (the failing secondary .NET
  project cannot disappear behind a passing first report);
- infrastructure-PREREQUISITE failures classify as ``infrastructure``
  with a bounded DISTINCT budget — they never blindly consume the
  code-repair iterations;
- a green harness job alone never satisfies independent verification;
- a changed tested-environment digest invalidates only the affected
  evidence, history retained;
- the restore→collection→verification chain: the verified candidate IS
  the collected generation's diff digest (REAL git, the REAL collector,
  service-level fakes).
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.qualification import (
    ExpectedReport,
    ExpectedReports,
    freeze_report_inventory,
)
from forge.adaptive.verification_binding import (
    DEFAULT_INFRA_RETRIES,
    FAILURE_CLASS_CODE,
    FAILURE_CLASS_INFRASTRUCTURE,
    FRESHNESS_CURRENT,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    BoundEvidence,
    GenerationBinding,
    InfraRetryBudget,
    InfraRetryLedger,
    ObservedReport,
    RequiredReportInventory,
    VerificationSubject,
    candidate_digest_of,
    classify_repair_failure,
    expected_report_coverage_of,
    harness_green_verifies,
    invalidate_for_environment_change,
    subject_freshness,
)
from forge.durable import FlowRun, FlowStatus
from forge.models.base import Base
from forge.runs.consistency import verified_verdict
from forge.runs.verification import (
    PRODUCER_GITHUB_CHECKS,
    STATUS_STALE,
    STATUS_UNKNOWN,
    VerificationResult,
    render_stale,
    subject_digest_of_evidence,
    verdict_freshness,
)
from tests.fixtures.fake_github import FakeGitHub
from tests.test_candidate_collector import (
    CHECKPOINT_ID,
    WORK_ID,
    make_checkout,
    make_generation,
)
from tests.test_github_runs import (
    BASE_HEAD,
    ISSUE,
    ISSUE_DESC,
    ISSUE_TITLE,
    REPO,
    StubPRReviewer,
    drive_to_waiting_ci,
    get_run,
    make_service,
    make_settings,
    make_stack,
    workflow_run,
)

PLAN_DIGEST = "b" * 64
OTHER_PLAN_DIGEST = "c" * 64
ENV_DIGEST = "d" * 64
OTHER_ENV_DIGEST = "e" * 64


def a_subject(**overrides) -> VerificationSubject:
    fields = dict(
        candidate_digest="1" * 64,
        source_oid="a" * 40,
        tested_oid="a" * 40,
        generation=GenerationBinding(
            work_id="run-1", checkpoint_id="f" * 64, checkpoint_binding="exact", source="generation"
        ),
        plan_revision_digest=PLAN_DIGEST,
        environment_profile_digest=ENV_DIGEST,
    )
    fields.update(overrides)
    return VerificationSubject(**fields)


# ---------------------------------------------------------------------------
# 1. The subject: construction, equality, document round-trip
# ---------------------------------------------------------------------------


class TestVerificationSubject:
    def test_subjects_are_frozen_and_equal_only_when_identical(self):
        assert a_subject() == a_subject()
        assert a_subject() != a_subject(source_oid="b" * 40)
        with pytest.raises(FrozenInstanceError):
            a_subject().source_oid = "b" * 40  # type: ignore[misc]

    def test_every_field_moves_the_subject_digest(self):
        base = a_subject()
        changed = a_subject(
            candidate_digest="2" * 64,
        )
        assert base.subject_digest != changed.subject_digest
        for mutation in (
            {"source_oid": "b" * 40},
            {"tested_oid": "b" * 40},
            {"plan_revision_digest": OTHER_PLAN_DIGEST},
            {"environment_profile_digest": OTHER_ENV_DIGEST},
            {"generation": GenerationBinding(work_id="run-2")},
        ):
            assert base.subject_digest != a_subject(**mutation).subject_digest, mutation

    def test_document_round_trip_preserves_the_identity(self):
        subject = a_subject()
        restored = VerificationSubject.from_document(subject.as_document())
        assert restored == subject
        assert restored is not None and restored.subject_digest == subject.subject_digest

    def test_from_document_refuses_foreign_or_empty_fragments(self):
        assert VerificationSubject.from_document(None) is None
        assert VerificationSubject.from_document({}) is None
        assert VerificationSubject.from_document({"schema": "other/1"}) is None
        assert VerificationSubject.from_document({"schema": "forge.verification.subject/1"}) is None

    def test_synthetic_merge_records_source_and_tested_separately(self):
        """Where the provider tests a synthetic merge, the tested sha
        legitimately differs from the review head — recorded SEPARATELY,
        never folded away; the verdict still binds the provider-verified
        sha while the subject remembers which candidate it answers for."""
        subject = a_subject(source_oid="a" * 40, tested_oid="9" * 40)
        document = subject.as_document()
        assert document["source_oid"] == "a" * 40
        assert document["tested_oid"] == "9" * 40
        restored = VerificationSubject.from_document(document)
        assert restored is not None
        assert restored.tested_oid == "9" * 40
        assert restored.source_oid == "a" * 40

    def test_candidate_digest_prefers_the_collector_diff_digest(self):
        recorded = candidate_digest_of(
            source_oid="a" * 40,
            base_oid="0" * 40,
            plan_digest=PLAN_DIGEST,
            recorded_digest="sha256:" + "9" * 64,
        )
        assert recorded == "9" * 64  # the sha256: prefix is normalized away
        assert (
            candidate_digest_of(
                source_oid="a" * 40,
                base_oid="0" * 40,
                plan_digest=PLAN_DIGEST,
                recorded_digest="8" * 64,
            )
            == "8" * 64
        )

    def test_candidate_digest_derives_deterministically_without_a_recorded_one(self):
        once = candidate_digest_of(source_oid="a" * 40, base_oid="0" * 40, plan_digest=PLAN_DIGEST)
        again = candidate_digest_of(source_oid="a" * 40, base_oid="0" * 40, plan_digest=PLAN_DIGEST)
        assert once == again and len(once) == 64
        # any repair / revision / re-collection moves the derived digest
        assert once != candidate_digest_of(
            source_oid="b" * 40, base_oid="0" * 40, plan_digest=PLAN_DIGEST
        )
        assert once != candidate_digest_of(
            source_oid="a" * 40, base_oid="0" * 40, plan_digest=OTHER_PLAN_DIGEST
        )


# ---------------------------------------------------------------------------
# 2. The freshness gate: stale verdicts never verify the current candidate
# ---------------------------------------------------------------------------


class TestFreshnessGate:
    def test_a_verdict_for_the_same_candidate_is_current(self):
        subject = a_subject()
        evidence = {"status": "passed", "subject_identity": subject.as_document()}
        assert subject_freshness(evidence, candidate_digest=subject.candidate_digest).current
        assert verdict_freshness(evidence, subject.candidate_digest) == FRESHNESS_CURRENT

    def test_an_old_candidates_pass_renders_stale_never_verified(self):
        old = a_subject()
        evidence = {
            "status": "passed",
            "tested_oid": old.source_oid,
            "subject_identity": old.as_document(),
        }
        current_digest = candidate_digest_of(
            source_oid="b" * 40, base_oid="0" * 40, plan_digest=PLAN_DIGEST
        )
        assert subject_freshness(evidence, candidate_digest=current_digest).stale
        assert verdict_freshness(evidence, current_digest) == FRESHNESS_STALE
        # the R02 reading agrees: a stale-rendered record never verifies
        rendered = render_stale(evidence, "subject moved")
        assert rendered["status"] == STATUS_STALE
        assert verified_verdict(rendered, old.source_oid) is False
        assert verdict_freshness(rendered, current_digest) == FRESHNESS_STALE

    def test_a_record_without_a_subject_is_honestly_unknown(self):
        evidence = {"status": "passed", "tested_oid": "a" * 40}
        assert verdict_freshness(evidence, "1" * 64) == FRESHNESS_UNKNOWN
        assert verdict_freshness(None, "1" * 64) == FRESHNESS_UNKNOWN
        freshness = subject_freshness(evidence, candidate_digest="1" * 64)
        assert freshness.status == FRESHNESS_UNKNOWN
        assert "freshness_unknown" in freshness.reason

    def test_a_moved_environment_profile_renders_stale_only_when_both_sides_know(self):
        subject = a_subject()
        evidence = {"status": "passed", "subject_identity": subject.as_document()}
        # both sides record one and they differ -> stale
        assert (
            verdict_freshness(
                evidence,
                subject.candidate_digest,
                environment_profile_digest=OTHER_ENV_DIGEST,
            )
            == FRESHNESS_STALE
        )
        # the current side unknown -> the candidate binding decides alone
        assert (
            verdict_freshness(evidence, subject.candidate_digest, environment_profile_digest="")
            == FRESHNESS_CURRENT
        )

    def test_render_stale_retains_the_subject_for_audit(self):
        subject = a_subject()
        evidence = {
            "status": "passed",
            "tested_oid": subject.source_oid,
            "observed_at": "2026-01-01T00:00:00+00:00",
            "producer": PRODUCER_GITHUB_CHECKS,
            "subject_identity": subject.as_document(),
        }
        rendered = render_stale(
            evidence,
            "the candidate changed mid-review",
            now=datetime(2026, 9, 23, tzinfo=timezone.utc),
        )
        assert rendered["producer"] == PRODUCER_GITHUB_CHECKS
        assert rendered["subject_identity"] == subject.as_document()
        assert rendered["summary"].startswith("stale:")
        assert "candidate changed mid-review" in rendered["summary"]
        assert subject_digest_of_evidence(rendered) == subject.candidate_digest

    def test_verification_result_carries_the_subject_identity(self):
        subject = a_subject()
        result = VerificationResult.passed(
            subject.source_oid, PRODUCER_GITHUB_CHECKS, subject_identity=subject.as_document()
        )
        evidence = result.as_evidence()
        assert evidence["subject_identity"] == subject.as_document()
        # pre-R36-14 verdicts stay byte-identical (no subject, no key)
        assert (
            "subject_identity"
            not in VerificationResult.passed(
                subject.source_oid, PRODUCER_GITHUB_CHECKS
            ).as_evidence()
        )


# ---------------------------------------------------------------------------
# 3. The expected-report inventory frozen with the work contract
# ---------------------------------------------------------------------------


def expected_reports(candidate_id: str) -> ExpectedReports:
    return ExpectedReports(
        reports=(
            ExpectedReport(
                test_project="Forge.Api.Tests",
                report_path="forge_Api.Tests_net9.0.trx",
                candidate_id=candidate_id,
                bundle_digest="9" * 64,
            ),
            ExpectedReport(
                test_project="Forge.Domain.Tests",
                report_path="forge_Domain.Tests_net9.0.trx",
                candidate_id=candidate_id,
                bundle_digest="9" * 64,
            ),
        )
    )


def write_trx(found_dir: Path, report_path: str, *, failed: int = 0) -> None:
    """One TRX + its identity sidecar (the qualification fixture shape)."""
    executed = 5
    found_dir.mkdir(parents=True, exist_ok=True)
    (found_dir / report_path).write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<TestRun id="00000000-0000-4000-8000-000000000000" name="synthetic" '
        'xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">\n'
        '  <ResultSummary outcome="Completed">\n'
        f'    <Counters total="{executed}" executed="{executed}" '
        f'passed="{executed - failed}" failed="{failed}" error="0" timeout="0" '
        'aborted="0" inconclusive="0" />\n'
        "  </ResultSummary>\n"
        "</TestRun>\n",
        encoding="utf-8",
    )


def write_sidecar(found_dir: Path, report_path: str, candidate_id: str) -> None:
    (found_dir / (report_path + ".identity.json")).write_text(
        json.dumps({"candidate_id": candidate_id, "bundle_digest": "9" * 64}), encoding="utf-8"
    )


class TestRequiredReportInventory:
    def test_the_inventory_freezes_with_the_work_contract_digest(self):
        document = freeze_report_inventory(expected_reports("cand-1"), contract_digest=PLAN_DIGEST)
        assert document["schema"] == "forge.verification.report-inventory/1"
        assert document["contract_digest"] == PLAN_DIGEST
        assert len(document["reports"]) == 2
        assert len(document["inventory_digest"]) == 64
        # a different work contract freezes a DIFFERENT inventory digest
        other = freeze_report_inventory(
            expected_reports("cand-1"), contract_digest=OTHER_PLAN_DIGEST
        )
        assert other["inventory_digest"] != document["inventory_digest"]

    def test_inventory_document_round_trip(self):
        document = freeze_report_inventory(expected_reports("cand-1"), contract_digest=PLAN_DIGEST)
        inventory = RequiredReportInventory.from_document(document)
        assert inventory is not None
        assert inventory.test_projects == ("Forge.Api.Tests", "Forge.Domain.Tests")
        assert inventory.as_document() == document

    def test_no_frozen_inventory_means_no_gate(self):
        assert RequiredReportInventory.from_document(None) is None
        assert RequiredReportInventory.from_document({}) is None
        assert expected_report_coverage_of({}) is None
        assert expected_report_coverage_of(None) is None

    def test_complete_coverage_verifies(self):
        document = freeze_report_inventory(expected_reports("cand-1"), contract_digest=PLAN_DIGEST)
        inventory = RequiredReportInventory.from_document(document)
        assert inventory is not None
        coverage = inventory.coverage(
            (
                ObservedReport(
                    test_project="Forge.Api.Tests",
                    outcome="passed",
                    subject_digest="cand-1",
                    exit_code=0,
                ),
                ObservedReport(
                    test_project="Forge.Domain.Tests",
                    outcome="passed",
                    subject_digest="cand-1",
                    exit_code=0,
                ),
            )
        )
        assert coverage.verifies
        assert coverage.problems == ()
        assert coverage.as_document()["complete"] is True

    def test_a_missing_report_never_verifies(self):
        document = freeze_report_inventory(expected_reports("cand-1"), contract_digest=PLAN_DIGEST)
        inventory = RequiredReportInventory.from_document(document)
        assert inventory is not None
        coverage = inventory.coverage(
            (
                ObservedReport(
                    test_project="Forge.Api.Tests",
                    outcome="passed",
                    subject_digest="cand-1",
                ),
            )
        )
        assert coverage.verifies is False
        assert coverage.missing == ("Forge.Domain.Tests",)
        assert any("missing_report" in line for line in coverage.problems)

    def test_a_skipped_required_check_never_verifies(self):
        document = freeze_report_inventory(expected_reports("cand-1"), contract_digest=PLAN_DIGEST)
        inventory = RequiredReportInventory.from_document(document)
        assert inventory is not None
        coverage = inventory.coverage(
            (
                ObservedReport(
                    test_project="Forge.Api.Tests",
                    outcome="passed",
                    subject_digest="cand-1",
                ),
                ObservedReport(
                    test_project="Forge.Domain.Tests",
                    outcome="skipped",
                    subject_digest="cand-1",
                ),
            )
        )
        assert coverage.verifies is False
        assert coverage.skipped == ("Forge.Domain.Tests",)

    def test_a_report_from_an_older_attempt_never_verifies(self):
        """The report itself claims ANOTHER candidate's digest — an older
        attempt's green report cannot answer for this one."""
        document = freeze_report_inventory(expected_reports("cand-2"), contract_digest=PLAN_DIGEST)
        inventory = RequiredReportInventory.from_document(document)
        assert inventory is not None
        coverage = inventory.coverage(
            (
                ObservedReport(
                    test_project="Forge.Api.Tests",
                    outcome="passed",
                    subject_digest="cand-2",
                ),
                ObservedReport(
                    test_project="Forge.Domain.Tests",
                    outcome="passed",
                    subject_digest="cand-1",  # the OLDER attempt
                ),
            )
        )
        assert coverage.verifies is False
        assert coverage.older_attempt == ("Forge.Domain.Tests",)
        assert coverage.satisfied == ("Forge.Api.Tests",)

    def test_a_failing_report_never_verifies_even_beside_a_passing_one(self):
        document = freeze_report_inventory(expected_reports("cand-1"), contract_digest=PLAN_DIGEST)
        inventory = RequiredReportInventory.from_document(document)
        assert inventory is not None
        coverage = inventory.coverage(
            (
                ObservedReport(
                    test_project="Forge.Api.Tests",
                    outcome="passed",
                    subject_digest="cand-1",
                ),
                ObservedReport(
                    test_project="Forge.Domain.Tests",
                    outcome="failed",
                    subject_digest="cand-1",
                    exit_code=1,
                ),
            )
        )
        assert coverage.verifies is False
        assert coverage.failed == ("Forge.Domain.Tests",)

    def test_the_failing_secondary_project_cannot_disappear_behind_a_passing_trx(
        self, tmp_path: Path
    ):
        """The FILE-based arm (the .NET TRX world): reusing the
        qualification reconciliation, a deliberately failing secondary
        project surfaces DISTINCTLY beside a passing first report — and a
        MISSING report is never zero failures."""
        document = freeze_report_inventory(expected_reports("cand-1"), contract_digest=PLAN_DIGEST)
        inventory = RequiredReportInventory.from_document(document)
        assert inventory is not None
        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx")
        write_sidecar(found, "forge_Api.Tests_net9.0.trx", "cand-1")
        write_trx(found, "forge_Domain.Tests_net9.0.trx", failed=2)
        write_sidecar(found, "forge_Domain.Tests_net9.0.trx", "cand-1")

        reconciliation = inventory.reconcile_found_dir(found)

        assert reconciliation.is_green is False
        problems = reconciliation.problems
        assert any("Forge.Domain.Tests" in line and "failed" in line for line in problems)
        assert not any("Forge.Api.Tests" in line for line in problems)

        # and the same inventory against an EMPTY directory: the missing
        # Domain report is a problem, the missing Api report too — never
        # zero failures.
        empty = inventory.reconcile_found_dir(tmp_path / "empty")
        assert empty.is_green is False
        assert len([line for line in empty.problems if "missing" in line.lower()]) == 2

    def test_expected_report_coverage_reads_the_run_evidence_shape(self):
        document = freeze_report_inventory(expected_reports("cand-1"), contract_digest=PLAN_DIGEST)
        evidence = {
            "expected_report_inventory": document,
            "observed_reports": [
                {
                    "test_project": "Forge.Api.Tests",
                    "outcome": "passed",
                    "subject_digest": "cand-1",
                    "exit_code": 0,
                    "report_digest": "7" * 64,
                },
                # a malformed row is DROPPED — leaving the expected report
                # missing, the honest direction
                {"test_project": "", "outcome": "passed"},
                "not-a-mapping",
            ],
        }
        coverage = expected_report_coverage_of(evidence)
        assert coverage is not None
        assert coverage.missing == ("Forge.Domain.Tests",)
        assert coverage.verifies is False
        assert coverage.as_document()["expected"] == ["Forge.Api.Tests", "Forge.Domain.Tests"]


# ---------------------------------------------------------------------------
# 4. Repair failure classification: infrastructure prerequisites ride a
#    bounded DISTINCT budget
# ---------------------------------------------------------------------------


class TestRepairFailureClassification:
    def test_infrastructure_prerequisites_classify_as_infrastructure(self):
        for detail in (
            "self-hosted runner unavailable for the checks job",
            "waiting for a runner matching the label",
            "report transport error while uploading TRX",
            "the artifact transport failed mid-upload",
        ):
            assert classify_repair_failure(detail) == FAILURE_CLASS_INFRASTRUCTURE, detail

    def test_plain_check_failures_stay_code(self):
        assert classify_repair_failure("checks failed (tests)") == FAILURE_CLASS_CODE
        assert classify_repair_failure("assertion error in test_widget.py") == FAILURE_CLASS_CODE
        assert classify_repair_failure("") == FAILURE_CLASS_CODE

    def test_the_classifier_reads_the_check_surface_too(self):
        assert (
            classify_repair_failure(
                "checks failed (tests)",
                surface=({"name": "tests", "conclusion": "runner unavailable"},),
            )
            == FAILURE_CLASS_INFRASTRUCTURE
        )

    def test_the_infra_budget_is_bounded_and_distinct(self):
        budget = InfraRetryBudget(max_retries=2)
        ledger = InfraRetryLedger()
        assert budget.allows(ledger)
        first = ledger.record()
        assert budget.allows(first)
        second = first.record()
        assert second.exhausted(budget)  # the distinct budget is spent
        assert not budget.allows(second)
        assert second.record().retries == 3  # the ledger counts, never resets

    def test_the_default_budget_is_bounded(self):
        assert DEFAULT_INFRA_RETRIES >= 1
        assert InfraRetryBudget().max_retries == DEFAULT_INFRA_RETRIES
        with pytest.raises(ValueError):
            InfraRetryBudget(max_retries=-1)

    def test_the_ledger_round_trips_through_evidence(self):
        ledger = InfraRetryLedger.from_document({"infrastructure_retries": 2})
        assert ledger.retries == 2
        assert InfraRetryLedger.from_document(None).retries == 0
        assert InfraRetryLedger.from_document({}).retries == 0
        assert InfraRetryLedger.from_document({"infrastructure_retries": "junk"}).retries == 0
        assert InfraRetryLedger.from_document({"infrastructure_retries": -3}).retries == 0
        # the ledger carries ONLY the infra counter — the code budget stays
        # where it already lives (commit_cycle, spec-frozen)
        assert set(InfraRetryLedger.from_document({"code_iterations": 9}).as_document()) == {
            "infrastructure_retries"
        }


# ---------------------------------------------------------------------------
# 5. Harness green is never independent verification
# ---------------------------------------------------------------------------


class TestHarnessGreenIsNotIndependent:
    def test_the_assertion_holds_for_every_contract_shape(self):
        assert harness_green_verifies(()) is False
        assert harness_green_verifies(("tests",)) is False
        assert harness_green_verifies(("tests", "lint", "sast")) is False

    def test_a_green_harness_check_never_proves_a_required_check(self):
        from forge.runs.verification import evaluate_positive_proof

        proof = evaluate_positive_proof(
            ("tests",),
            {"forge-harness": "success"},  # the lane is green...
        )
        assert proof.verified is False
        assert proof.missing == ("tests",)


# ---------------------------------------------------------------------------
# 6. Applicability invalidation: only the affected evidence, history kept
# ---------------------------------------------------------------------------


class TestEnvironmentInvalidation:
    def test_a_moved_environment_digest_invalidates_only_its_claimants(self):
        records = (
            BoundEvidence("e1", "s1", ENV_DIGEST),
            BoundEvidence("e2", "s2", OTHER_ENV_DIGEST),
            BoundEvidence("e3", "s3", ""),  # never claimed a world
        )
        updated = invalidate_for_environment_change(
            records, previous_digest=ENV_DIGEST, current_digest=OTHER_ENV_DIGEST
        )
        by_id = {record.evidence_id: record for record in updated}
        assert by_id["e1"].invalidated is True
        assert "retained for audit" in by_id["e1"].invalidated_reason
        assert by_id["e2"].invalidated is False
        assert by_id["e3"].invalidated is False

    def test_history_is_retained_never_deleted(self):
        records = (BoundEvidence("e1", "s1", ENV_DIGEST),)
        updated = invalidate_for_environment_change(
            records, previous_digest=ENV_DIGEST, current_digest=OTHER_ENV_DIGEST
        )
        assert len(updated) == len(records)
        assert updated[0].subject_digest == "s1"  # the claim stays inspectable

    def test_an_unchanged_digest_is_a_no_op(self):
        records = (BoundEvidence("e1", "s1", ENV_DIGEST),)
        assert (
            invalidate_for_environment_change(
                records, previous_digest=ENV_DIGEST, current_digest=ENV_DIGEST
            )
            == records
        )

    def test_already_invalidated_rows_keep_their_first_reason(self):
        records = (
            BoundEvidence("e1", "s1", ENV_DIGEST, invalidated=True, invalidated_reason="first"),
        )
        updated = invalidate_for_environment_change(
            records, previous_digest=ENV_DIGEST, current_digest=OTHER_ENV_DIGEST
        )
        assert updated[0].invalidated_reason == "first"


# ---------------------------------------------------------------------------
# 7. The service-level gate (fake clients): subject stamping, freshness,
#    inventory refusals, infra classification, harness exclusion.
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


@pytest.fixture()
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
    github.heads[REPO]["main"] = BASE_HEAD
    github.seed_issue(REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
    return github


def current_candidate_digest(run: FlowRun) -> str:
    return candidate_digest_of(
        source_oid=run.candidate_shas[-1],
        base_oid=run.base_sha or "",
        plan_digest=run.plan_digest or "",
    )


class TestServiceSubjectBinding:
    async def test_the_verdict_names_its_subject(self, db, fake):
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])
        reviewer = StubPRReviewer()
        service._stack = make_stack(fake, reviewer=reviewer)

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        verification = run.evidence["verification"]
        subject = verification["subject_identity"]
        assert subject["schema"] == "forge.verification.subject/1"
        assert subject["source_oid"] == candidate
        assert subject["tested_oid"] == candidate  # the provider-verified sha
        assert subject["candidate_digest"] == current_candidate_digest(run)
        assert subject["plan_revision_digest"] == run.plan_digest
        assert subject["subject_digest"]  # the one value applicability binds to

    async def test_an_old_candidates_same_name_green_check_never_verifies(self, db, fake):
        """The stale same-name arm: a check named exactly like the required
        one, GREEN, but bound to the PREVIOUS candidate's sha — the
        provider query is candidate-scoped, so it never even enters the
        observation set: the run keeps waiting, never verified."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        old_candidate = "5" * 40
        assert old_candidate != candidate
        fake.seed_workflow_runs([workflow_run(old_candidate, "tests", "success")])
        reviewer = StubPRReviewer()
        service._stack = make_stack(fake, reviewer=reviewer)

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert reviewer.calls == []  # no review, no ready
        assert not (run.evidence or {}).get("verification", {}).get("subject_identity")

    async def test_out_of_order_provider_events_decide_by_the_newest_run(self, db, fake):
        """B02 at the binding level: API order never decides — the newest
        RUN is authoritative, whichever order the provider answered in."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        reviewer = StubPRReviewer()
        service._stack = make_stack(fake, reviewer=reviewer)

        # the OLDER run succeeded, the NEWEST failed; the provider lists
        # the older FIRST (out of order). The newest must decide.
        fake.seed_workflow_runs(
            [
                workflow_run(candidate, "tests", "success", run_id=11, attempt=1),
                workflow_run(candidate, "tests", "failure", run_id=12, attempt=1),
            ]
        )
        await service.evaluate_waiting_ci_one(run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value  # the builtin lane has no repair dispatch
        assert "tests" in (run.status_reason or "")

        # the mirrored order (newest first in the list, newest green)
        # verifies — ordering, not list position, decided both arms.
        fake2 = FakeGitHub()
        fake2.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
        fake2.heads[REPO]["main"] = BASE_HEAD
        fake2.seed_issue(REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
        service2 = make_service(db, fake2, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id2, candidate2 = await drive_to_waiting_ci(db, service2, fake2)
        fake2.seed_workflow_runs(
            [
                workflow_run(candidate2, "tests", "success", run_id=22, attempt=1),
                workflow_run(candidate2, "tests", "failure", run_id=21, attempt=1),
            ]
        )
        reviewer2 = StubPRReviewer()
        service2._stack = make_stack(fake2, reviewer=reviewer2)
        await service2.evaluate_waiting_ci_one(run_id2)
        run2 = await get_run(db, run_id2)
        assert run2.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_a_revised_plan_makes_the_recorded_pass_stale_on_resume(self, db, fake):
        """The freshness gate on the persisted verdict: the recorded PASS
        names a subject derived under a REVISED plan — the resume renders
        it stale and parks the run instead of shipping verified_ready."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])
        reviewer = StubPRReviewer()
        service._stack = make_stack(fake, reviewer=reviewer)
        await service.evaluate_waiting_ci_one(run_id)
        assert (await get_run(db, run_id)).status == FlowStatus.READY_FOR_HUMAN.value

        # the crash window: the run sits in reviewing with a recorded
        # verdict whose subject was frozen under a DIFFERENT plan digest
        # (a revision landed mid-review).
        stale_subject = VerificationSubject(
            candidate_digest=candidate_digest_of(
                source_oid=candidate, base_oid=BASE_HEAD, plan_digest=OTHER_PLAN_DIGEST
            ),
            source_oid=candidate,
            tested_oid=candidate,
        )
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            assert run is not None
            verification = dict((run.evidence or {}).get("verification") or {})
            verification["subject_identity"] = stale_subject.as_document()
            run.evidence = dict(run.evidence or {}) | {"verification": verification}
            run.status = FlowStatus.REVIEWING.value
            await session.commit()

        await service.resume_verification(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("verification_stale")
        verification = run.evidence["verification"]
        assert verification["status"] == STATUS_STALE
        # the stale render RETAINS the subject it names — audit, not deletion
        assert verification["subject_identity"]["candidate_digest"] == (
            stale_subject.candidate_digest
        )

    async def test_a_current_subject_still_reaches_ready_on_resume(self, db, fake):
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])
        service._stack = make_stack(fake, reviewer=StubPRReviewer())
        await service.evaluate_waiting_ci_one(run_id)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            assert run is not None
            run.status = FlowStatus.REVIEWING.value
            await session.commit()

        await service.resume_verification(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert run.evidence["verification"]["status"] == "passed"

    async def test_a_legacy_record_without_a_subject_keeps_its_meaning(self, db, fake):
        """Backward compatibility: a pre-R36-14 verdict (no subject) is
        freshness-UNKNOWN and its ADR-0008 sha binding decides, exactly
        as before — the resume path neither blocks nor re-verifies."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])
        service._stack = make_stack(fake, reviewer=StubPRReviewer())
        await service.evaluate_waiting_ci_one(run_id)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            assert run is not None
            verification = dict((run.evidence or {}).get("verification") or {})
            verification.pop("subject_identity", None)  # the legacy shape
            run.evidence = dict(run.evidence or {}) | {"verification": verification}
            run.status = FlowStatus.REVIEWING.value
            await session.commit()

        await service.resume_verification(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value


class TestServiceReportInventoryGate:
    async def _drive(self, db, fake, *, observed_reports):
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        run = await get_run(db, run_id)
        digest = current_candidate_digest(run)
        inventory = freeze_report_inventory(
            expected_reports(digest), contract_digest=str(run.spec_digest or PLAN_DIGEST)
        )
        async with db() as session:
            managed = await session.get(FlowRun, run_id)
            assert managed is not None
            patch = {"expected_report_inventory": inventory}
            if observed_reports is not None:
                patch["observed_reports"] = observed_reports
            managed.evidence = dict(managed.evidence or {}) | patch
            await session.commit()
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])
        reviewer = StubPRReviewer()
        service._stack = make_stack(fake, reviewer=reviewer)
        return service, run_id, digest, reviewer

    async def test_a_missing_report_keeps_the_run_waiting(self, db, fake):
        service, run_id, digest, reviewer = await self._drive(db, fake, observed_reports=None)
        await service.evaluate_waiting_ci_one(run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        verification = run.evidence["verification"]
        assert verification["status"] == STATUS_UNKNOWN
        assert "expected report coverage" in verification["summary"]
        coverage = verification["expected_report_coverage"]
        assert coverage["complete"] is False
        assert coverage["missing"] == ["Forge.Api.Tests", "Forge.Domain.Tests"]
        assert reviewer.calls == []

    async def test_an_older_attempt_report_keeps_the_run_waiting(self, db, fake):
        observed = [
            {
                "test_project": "Forge.Api.Tests",
                "outcome": "passed",
                "subject_digest": "older-attempt-digest",
            },
            {
                "test_project": "Forge.Domain.Tests",
                "outcome": "passed",
                "subject_digest": "older-attempt-digest",
            },
        ]
        service, run_id, _digest, reviewer = await self._drive(db, fake, observed_reports=observed)
        await service.evaluate_waiting_ci_one(run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        coverage = run.evidence["verification"]["expected_report_coverage"]
        assert coverage["older_attempt"] == ["Forge.Api.Tests", "Forge.Domain.Tests"]
        assert reviewer.calls == []

    async def test_complete_coverage_reaches_verified_ready(self, db, fake):
        observed = [
            {"test_project": "Forge.Api.Tests", "outcome": "passed", "exit_code": 0},
            {"test_project": "Forge.Domain.Tests", "outcome": "passed", "exit_code": 0},
        ]
        service, run_id, digest, reviewer = await self._drive(db, fake, observed_reports=observed)
        # the observed rows carry the CURRENT candidate's binding (the
        # sidecar identity the ingest path writes)
        async with db() as session:
            managed = await session.get(FlowRun, run_id)
            assert managed is not None
            rows = [
                dict(row, subject_digest=digest)
                for row in (managed.evidence or {}).get("observed_reports", [])
            ]
            managed.evidence = dict(managed.evidence or {}) | {"observed_reports": rows}
            await session.commit()

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        verification = run.evidence["verification"]
        assert verification["status"] == "passed"
        assert verification["expected_report_coverage"]["complete"] is True


class TestServiceInfrastructureClassification:
    async def test_a_checks_transport_error_is_infrastructure_not_a_defect(self, db, fake):
        from forge.integrations.github import GitHubAPIError

        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, _candidate = await drive_to_waiting_ci(db, service, fake)
        before = (await get_run(db, run_id)).commit_cycle

        async def transport_boom(*args, **kwargs):
            raise GitHubAPIError(503, "checks transport error")

        fake.list_workflow_runs_for_sha = transport_boom  # type: ignore[method-assign]

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # bounded by the deadline, not repaired
        repair = (run.evidence or {}).get("repair") or {}
        assert repair.get("failure_class") == FAILURE_CLASS_INFRASTRUCTURE
        assert "transport" in str(repair.get("reason") or "")
        assert run.commit_cycle == before  # the code budget is untouched

    async def test_the_distinct_infra_budget_blocks_when_spent(self, db, fake):
        from forge.integrations.github import GitHubAPIError

        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, _candidate = await drive_to_waiting_ci(db, service, fake)
        cycle_before = (await get_run(db, run_id)).commit_cycle
        # the budget is ALREADY spent (previous passes consumed it) — the
        # next infra-prerequisite failure blocks honestly instead of
        # waiting forever OR spending a code-repair iteration
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            assert run is not None
            run.evidence = dict(run.evidence or {}) | {
                "repair_ledger": {"infrastructure_retries": DEFAULT_INFRA_RETRIES}
            }
            await session.commit()

        async def transport_boom(*args, **kwargs):
            raise GitHubAPIError(503, "checks transport error")

        fake.list_workflow_runs_for_sha = transport_boom  # type: ignore[method-assign]

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("verification_infrastructure")
        assert "retries exhausted" in (run.status_reason or "")
        assert run.commit_cycle == cycle_before  # the code budget is untouched


class TestServiceHarnessExclusion:
    async def test_a_green_harness_lane_alone_never_verifies(self, db, fake):
        """A green harness job ALONE never satisfies independent
        verification: the only observed green check is the harness lane
        itself, the frozen required check never ran — the run keeps
        waiting with an honest unknown verdict (the positive proof over
        the frozen required list is the only verified_ready source)."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "forge-harness", "success")])
        reviewer = StubPRReviewer()
        service._stack = make_stack(fake, reviewer=reviewer)

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # the required check never ran
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == STATUS_UNKNOWN
        assert "tests" in verification["summary"]
        assert reviewer.calls == []


class TestRestoreCollectionVerificationChain:
    async def test_the_verified_candidate_is_the_collected_generation_diff_digest(
        self, db, fake, tmp_path: Path
    ):
        """The chain the issue demands: a REAL restore (generation), a REAL
        collection (subprocess git through the packaged collector), then
        the service-level verification — and the verdict's subject binds
        the EXACT diff digest the collector produced for the active
        generation."""
        from forge.candidate_collector import collect_candidate

        checkout, base_oid = make_checkout(tmp_path)
        generation = make_generation(checkout)
        (generation / "src" / "app.py").write_text("print('restored wip')\n")

        result = collect_candidate(checkout, WORK_ID, base_oid)

        assert result.source == "generation"
        assert result.zero_change is False
        assert result.checkpoint_id == CHECKPOINT_ID

        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        # the publication carried the collector's envelope: the diff
        # digest and the generation binding are on the run's evidence
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            assert run is not None
            run.evidence = dict(run.evidence or {}) | {
                "published_candidate": {
                    **dict((run.evidence or {}).get("published_candidate") or {}),
                    "sha": candidate,
                    "diff_digest": result.diff_digest,
                },
                "candidate_collection": {
                    "work_id": result.resolved_work_id,
                    "checkpoint_id": result.checkpoint_id,
                    "checkpoint_binding": result.checkpoint_binding,
                    "source": result.source,
                },
            }
            await session.commit()

        fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])
        service._stack = make_stack(fake, reviewer=StubPRReviewer())
        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        subject = run.evidence["verification"]["subject_identity"]
        # THE assertion: the verified candidate == the collected
        # generation's diff digest, not a sha spelling or a name
        assert subject["candidate_digest"] == result.diff_digest
        assert subject["generation"]["checkpoint_id"] == CHECKPOINT_ID
        assert subject["generation"]["source"] == "generation"
        assert subject["source_oid"] == candidate
        # and the freshness gate agrees with itself end to end
        assert (
            verdict_freshness(run.evidence["verification"], result.diff_digest) == FRESHNESS_CURRENT
        )
