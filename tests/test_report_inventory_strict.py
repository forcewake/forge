"""R37-04 (#285) + R37-05 (#286) — exact report identities and complete
tested-subject applicability (AT-05 / AT-06).

AT-05 — required report identity versus an attractive false positive:
REAL report files (TRX + identity sidecars) ingested through the
production ingestion shape (:func:`ingest_report_file`) and judged by
the verdict-time route (``evaluate_waiting_ci_one``'s #273 gate) under
the STRICT inventory profile (``report-inventory/2``):

- a passed report with a BLANK subject, wrong path and exit 1 beside a
  legitimate unrelated report is a typed ``unbound`` obligation, never
  satisfaction (probe P04) — reverting the blank-subject permissiveness
  must fail these tests;
- attempt 10's required failure is not hidden by attempt 9's success
  (NUMERIC ordering — reverting to lexicographic must fail these
  tests); duplicate report keys with contradictory outcomes are a
  ``duplicate_report_conflict``, never resolved by response order;
- wrong path/bundle/producer reports are ``unmatched`` evidence, two
  inventory projects bind to their own candidates per-row, and a passed
  report with a nonzero required-command exit combines to ``failed``
  with the exit recorded;
- valid reports and explicit pre-approved waivers still pass.

AT-06 — evidence replay across different tested worlds: identical diff
bytes on two bases cannot reuse a verdict; a rebuilt image/changed
environment or required contract invalidates dependent evidence with
the moved inputs named; a purely editorial plan revision PRESERVES
applicability (naming the equivalent inputs); strict fields deleted
from a stored record render ``unknown`` — never a looser success; the
service finalization AND the operator rendering answer the same for the
same typed subject, and superseded proof stays archived with its
supersession reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.qualification import ExpectedReport, ExpectedReports
from forge.adaptive.verification_binding import (
    APPLICABILITY_INVALIDATED,
    APPLICABILITY_PRESERVED,
    APPLICABILITY_UNKNOWN,
    MATCH_DUPLICATE_CONFLICT,
    MATCH_FAILED,
    MATCH_UNBOUND,
    ApplicabilityRequest,
    ObservedReport,
    REPORT_INVENTORY_SCHEMA_STRICT,
    RequiredReportInventory,
    VerificationSubject,
    applicability,
    candidate_digest_of,
    expected_report_coverage_of,
    freeze_report_inventory_strict,
    ingest_report_file,
    subject_freshness,
)
from forge.adaptive.operator_view import initial_projection
from forge.durable import FlowRun, FlowStatus
from forge.models.base import Base
from forge.runs.verification import (
    FRESHNESS_CURRENT,
    FRESHNESS_UNKNOWN,
    PRODUCER_GITHUB_CHECKS,
    STATUS_STALE,
    STATUS_UNKNOWN,
    verdict_freshness,
)
from tests.fixtures.fake_github import FakeGitHub
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
CONTRACT_DIGEST = "c" * 64
BUNDLE = "9" * 64
OTHER_BUNDLE = "8" * 64


def expected_reports(
    candidate_id: str,
    *,
    domain_candidate: str | None = None,
) -> ExpectedReports:
    """The frozen obligation rows — per-project candidate ids so the
    per-row binding can be exercised (``domain_candidate`` deliberately
    differs in the two-candidates arm)."""
    return ExpectedReports(
        reports=(
            ExpectedReport(
                test_project="Forge.Api.Tests",
                report_path="forge_Api.Tests_net9.0.trx",
                candidate_id=candidate_id,
                bundle_digest=BUNDLE,
            ),
            ExpectedReport(
                test_project="Forge.Domain.Tests",
                report_path="forge_Domain.Tests_net9.0.trx",
                candidate_id=domain_candidate or candidate_id,
                bundle_digest=BUNDLE,
            ),
        )
    )


def strict_inventory(candidate_id: str, *, domain_candidate: str | None = None) -> dict:
    return freeze_report_inventory_strict(
        expected_reports(candidate_id, domain_candidate=domain_candidate),
        contract_digest=CONTRACT_DIGEST,
        producer=PRODUCER_GITHUB_CHECKS,
    )


def write_trx(found_dir: Path, report_path: str, *, failed: int = 0) -> None:
    """One REAL TRX report file (the qualification fixture shape)."""
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


def write_sidecar(
    found_dir: Path,
    report_path: str,
    candidate_id: str,
    *,
    bundle: str = BUNDLE,
) -> None:
    """The identity sidecar the qualification world writes beside the TRX."""
    found_dir.mkdir(parents=True, exist_ok=True)
    (found_dir / (report_path + ".identity.json")).write_text(
        json.dumps({"candidate_id": candidate_id, "bundle_digest": bundle}), encoding="utf-8"
    )


def ingest(
    found_dir: Path,
    *,
    test_project: str,
    report_path: str,
    candidate_id: str = "cand-1",
    exit_code: int | None = None,
    attempt: str = "",
    attempt_ordinal: int = 0,
    bundle: str = BUNDLE,
    with_sidecar: bool = True,
) -> dict:
    """The PRODUCTION ingestion shape: real files -> an observed-report
    evidence row (never a precomputed coverage object)."""
    if with_sidecar:
        write_sidecar(found_dir, report_path, candidate_id, bundle=bundle)
    return ingest_report_file(
        test_project=test_project,
        report_path=report_path,
        found_dir=found_dir,
        exit_code=exit_code,
        attempt=attempt,
        attempt_ordinal=attempt_ordinal,
        producer=PRODUCER_GITHUB_CHECKS,
    ).as_document()


def coverage_of(inventory_document: dict, rows: list[dict]):
    inventory = RequiredReportInventory.from_document(inventory_document)
    assert inventory is not None and inventory.strict
    return expected_report_coverage_of(
        {"expected_report_inventory": inventory_document, "observed_reports": rows}
    )


def the_subject(**overrides) -> VerificationSubject:
    fields = dict(
        candidate_digest="1" * 64,
        source_oid="a" * 40,
        tested_oid="a" * 40,
        source_base_oid="0" * 40,
        plan_revision_digest=PLAN_DIGEST,
        environment_profile_digest="d" * 64,
    )
    fields.update(overrides)
    return VerificationSubject(**fields)


# ---------------------------------------------------------------------------
# AT-05 — the strict report-inventory contract (real files, production
# ingestion, exact per-row identities)
# ---------------------------------------------------------------------------


class TestStrictInventoryMatching:
    def test_the_strict_document_freezes_round_trips(self):
        document = strict_inventory("cand-1")
        assert document["schema"] == REPORT_INVENTORY_SCHEMA_STRICT
        assert document["producer"] == PRODUCER_GITHUB_CHECKS
        inventory = RequiredReportInventory.from_document(document)
        assert inventory is not None
        assert inventory.strict
        assert inventory.as_document() == document
        # the digest covers the rows — a tampered row no longer matches the
        # digest that was recorded with the inventory
        other = freeze_report_inventory_strict(
            expected_reports("cand-1"),
            contract_digest=CONTRACT_DIGEST,
            producer=PRODUCER_GITHUB_CHECKS,
        )
        tampered_rows = [dict(row) for row in document["reports"]]
        tampered_rows[0]["bundle_digest"] = OTHER_BUNDLE
        assert (
            freeze_report_inventory_strict(
                ExpectedReports(
                    reports=(
                        ExpectedReport(
                            test_project=row["test_project"],
                            report_path=row["report_path"],
                            candidate_id=row["candidate_id"],
                            bundle_digest=row["bundle_digest"],
                        )
                        for row in tampered_rows
                    )
                ),
                contract_digest=CONTRACT_DIGEST,
                producer=PRODUCER_GITHUB_CHECKS,
            )["inventory_digest"]
            != other["inventory_digest"]
        )

    def test_valid_complete_strict_coverage_verifies(self, tmp_path: Path):
        """Valid reports still pass — the strict gate never broadens the
        honest positive path."""
        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx")
        write_trx(found, "forge_Domain.Tests_net9.0.trx")
        rows = [
            ingest(
                found,
                test_project="Forge.Api.Tests",
                report_path="forge_Api.Tests_net9.0.trx",
                attempt="3",
            ),
            ingest(
                found,
                test_project="Forge.Domain.Tests",
                report_path="forge_Domain.Tests_net9.0.trx",
                attempt="4",
            ),
        ]
        coverage = coverage_of(strict_inventory("cand-1"), rows)
        assert coverage is not None
        assert coverage.verifies
        assert coverage.satisfied == ("Forge.Api.Tests", "Forge.Domain.Tests")
        assert coverage.problems == ()

    def test_p04_shape_is_unbound_never_satisfied(self, tmp_path: Path):
        """MUTATION ARM (blank-subject permissiveness): a PASSED report
        with a blank subject, at the wrong path, with exit 1 — beside a
        legitimate unrelated report — leaves the named obligation
        ``unbound``; reverting to the permissive reading (empty subject
        skips the candidate check) must FAIL this test."""
        found = tmp_path / "reports"
        # the attractive false positive: green TRX, NO identity sidecar,
        # misplaced path, and the required command exited 1
        write_trx(found, "misplaced_Api.trx")
        api = ingest(
            found,
            test_project="Forge.Api.Tests",
            report_path="misplaced_Api.trx",
            exit_code=1,
            with_sidecar=False,
        )
        # the legitimate unrelated report satisfies ITS obligation
        write_trx(found, "forge_Domain.Tests_net9.0.trx")
        domain = ingest(
            found, test_project="Forge.Domain.Tests", report_path="forge_Domain.Tests_net9.0.trx"
        )
        coverage = coverage_of(strict_inventory("cand-1"), [api, domain])
        assert coverage is not None
        assert coverage.verifies is False
        assert coverage.unbound == ("Forge.Api.Tests",)
        assert coverage.satisfied == ("Forge.Domain.Tests",)
        outcomes = {oid: outcome for oid, outcome, _exit in coverage.outcomes}
        assert outcomes["Forge.Api.Tests@forge_Api.Tests_net9.0.trx"] == MATCH_UNBOUND
        assert any("unbound_report" in line for line in coverage.problems)
        # response order cannot change the answer
        mirrored = coverage_of(strict_inventory("cand-1"), [domain, api])
        assert mirrored is not None and mirrored.unbound == ("Forge.Api.Tests",)

    def test_attempt_10_failure_not_hidden_by_attempt_9_success(self, tmp_path: Path):
        """MUTATION ARM (numeric ordering): attempt 10's required failure
        supersedes attempt 9's success — the lexicographic reading ("9" >
        "10") hid it; reverting must FAIL this test."""
        found = tmp_path / "reports"
        # attempt 10 (the NEWEST) failed; the artifact path was reused, so
        # the failed bytes are what the world last produced
        write_trx(found, "forge_Api.Tests_net9.0.trx", failed=2)
        newest = ingest(
            found,
            test_project="Forge.Api.Tests",
            report_path="forge_Api.Tests_net9.0.trx",
            attempt="10",
        )
        # attempt 9 passed (the replayed older upload)
        older = dict(newest, attempt="9")
        older["outcome"] = "passed"
        coverage = coverage_of(strict_inventory("cand-1"), [older, newest])
        assert coverage is not None
        assert coverage.verifies is False
        assert coverage.failed == ("Forge.Api.Tests",)
        # and the mirrored response order decides identically
        mirrored = coverage_of(strict_inventory("cand-1"), [newest, older])
        assert mirrored is not None and mirrored.failed == ("Forge.Api.Tests",)

    def test_the_persisted_monotonic_ordinal_orders_non_numeric_attempts(self):
        """Non-integer-shaped attempt ids fall back to the persisted
        monotonic ``attempt_ordinal`` frozen at record time."""
        older = ObservedReport(
            test_project="Forge.Api.Tests",
            outcome="passed",
            subject_digest="cand-1",
            report_path="forge_Api.Tests_net9.0.trx",
            bundle_digest=BUNDLE,
            attempt="run-beta",
            attempt_ordinal=1,
        )
        newest = ObservedReport(
            test_project="Forge.Api.Tests",
            outcome="failed",
            subject_digest="cand-1",
            report_path="forge_Api.Tests_net9.0.trx",
            bundle_digest=BUNDLE,
            attempt="run-alpha",
            attempt_ordinal=2,
        )
        coverage = coverage_of(
            strict_inventory("cand-1"), [newest.as_document(), older.as_document()]
        )
        assert coverage is not None
        assert coverage.failed == ("Forge.Api.Tests",)

    def test_duplicate_ids_with_contradictory_outcomes_conflict(self, tmp_path: Path):
        """The same report key carrying contradictory outcomes is a typed
        conflict — never resolved by response order."""
        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx")
        passed_row = ingest(
            found,
            test_project="Forge.Api.Tests",
            report_path="forge_Api.Tests_net9.0.trx",
            attempt="7",
        )
        # the duplicate id answered again with the opposite outcome
        failed_row = dict(passed_row, outcome="failed", exit_code=1)
        for order in ([passed_row, failed_row], [failed_row, passed_row]):
            coverage = coverage_of(strict_inventory("cand-1"), list(order))
            assert coverage is not None
            assert coverage.duplicate_conflict == ("Forge.Api.Tests",)
            assert coverage.verifies is False
            outcomes = {oid: outcome for oid, outcome, _exit in coverage.outcomes}
            assert (
                outcomes["Forge.Api.Tests@forge_Api.Tests_net9.0.trx"] == MATCH_DUPLICATE_CONFLICT
            )
            assert any("duplicate_report_conflict" in line for line in coverage.problems)

    def test_wrong_path_and_wrong_bundle_are_unmatched_not_assigned(self, tmp_path: Path):
        found = tmp_path / "reports"
        write_trx(found, "elsewhere.trx")
        wrong_path = ingest(
            found, test_project="Forge.Api.Tests", report_path="elsewhere.trx", attempt="2"
        )
        write_trx(found, "forge_Domain.Tests_net9.0.trx")
        wrong_bundle = ingest(
            found,
            test_project="Forge.Domain.Tests",
            report_path="forge_Domain.Tests_net9.0.trx",
            bundle=OTHER_BUNDLE,
        )
        coverage = coverage_of(strict_inventory("cand-1"), [wrong_path, wrong_bundle])
        assert coverage is not None
        assert coverage.verifies is False
        assert coverage.unmatched == ("Forge.Api.Tests", "Forge.Domain.Tests")
        assert coverage.satisfied == ()
        assert any("unmatched_report" in line for line in coverage.problems)

    def test_two_projects_bind_to_their_own_candidates_per_row(self, tmp_path: Path):
        """Each inventory row compares against THAT row's candidate —
        never the first row's (the name-only keying defect)."""
        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx")
        api = ingest(
            found, test_project="Forge.Api.Tests", report_path="forge_Api.Tests_net9.0.trx"
        )
        write_trx(found, "forge_Domain.Tests_net9.0.trx")
        domain = ingest(
            found,
            test_project="Forge.Domain.Tests",
            report_path="forge_Domain.Tests_net9.0.trx",
            candidate_id="cand-domain",
        )
        coverage = coverage_of(
            strict_inventory("cand-1", domain_candidate="cand-domain"), [api, domain]
        )
        assert coverage is not None
        assert coverage.verifies
        assert coverage.satisfied == ("Forge.Api.Tests", "Forge.Domain.Tests")

    def test_a_passed_report_with_a_nonzero_exit_combines_to_failed(self, tmp_path: Path):
        """The combination rule: a passed report whose required command
        exited nonzero does not pass silently — and the exit is recorded."""
        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx")
        row = ingest(
            found,
            test_project="Forge.Api.Tests",
            report_path="forge_Api.Tests_net9.0.trx",
            exit_code=1,
        )
        write_trx(found, "forge_Domain.Tests_net9.0.trx")
        domain = ingest(
            found, test_project="Forge.Domain.Tests", report_path="forge_Domain.Tests_net9.0.trx"
        )
        coverage = coverage_of(strict_inventory("cand-1"), [row, domain])
        assert coverage is not None
        assert coverage.verifies is False
        assert coverage.failed == ("Forge.Api.Tests",)
        assert coverage.exit_codes == (("Forge.Api.Tests@forge_Api.Tests_net9.0.trx", 1),)
        outcomes = {oid: (outcome, exit_code) for oid, outcome, exit_code in coverage.outcomes}
        assert outcomes["Forge.Api.Tests@forge_Api.Tests_net9.0.trx"] == (MATCH_FAILED, 1)

    def test_replayed_old_attempt_and_missing_reports_stay_distinguishable(self, tmp_path: Path):
        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx")
        replayed = ingest(
            found,
            test_project="Forge.Api.Tests",
            report_path="forge_Api.Tests_net9.0.trx",
            candidate_id="older-cand",
        )
        coverage = coverage_of(strict_inventory("cand-1"), [replayed])
        assert coverage is not None
        # the replayed report names ANOTHER candidate: older_attempt
        assert coverage.older_attempt == ("Forge.Api.Tests",)
        # and a project nobody reported: missing (with the skipped arm)
        assert coverage.missing == ("Forge.Domain.Tests",)
        skipped = ObservedReport(
            test_project="Forge.Domain.Tests",
            outcome="skipped",
            subject_digest="cand-1",
            report_path="forge_Domain.Tests_net9.0.trx",
            bundle_digest=BUNDLE,
        ).as_document()
        coverage = coverage_of(strict_inventory("cand-1"), [replayed, skipped])
        assert coverage is not None
        assert coverage.skipped == ("Forge.Domain.Tests",)

    def test_the_legacy_schema1_inventory_keeps_its_approved_weaker_reading(self, tmp_path: Path):
        """The versioned legacy mode: a run approved under
        ``report-inventory/1`` keeps the weaker contract — the SAME
        blank-subject shape that strict refuses still satisfies legacy
        (its reading never broadens, and strict never applies to it)."""
        from forge.adaptive.qualification import freeze_report_inventory

        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx")
        api = ingest(
            found,
            test_project="Forge.Api.Tests",
            report_path="forge_Api.Tests_net9.0.trx",
            with_sidecar=False,
        )
        legacy = freeze_report_inventory(
            expected_reports("cand-1"), contract_digest=CONTRACT_DIGEST
        )
        inventory = RequiredReportInventory.from_document(legacy)
        assert inventory is not None and not inventory.strict
        coverage = expected_report_coverage_of(
            {"expected_report_inventory": legacy, "observed_reports": [api]}
        )
        assert coverage is not None
        assert coverage.satisfied == ("Forge.Api.Tests",)
        assert coverage.unbound == ()
        assert coverage.verifies is False  # the Domain row is still missing


# ---------------------------------------------------------------------------
# AT-06 — the applicability contract (complete tested subject)
# ---------------------------------------------------------------------------


class TestApplicability:
    def test_identical_diff_bytes_on_two_bases_cannot_reuse_a_verdict(self):
        """THE P06 fix: the recorded candidate digest is a DIFF hash that
        two bases share — the full-subject comparison (base included)
        invalidates the reuse the legacy two-string gate granted."""
        recorded = the_subject()
        current = the_subject(source_base_oid="2" * 40)
        evidence = {"status": "passed", "subject_identity": recorded.as_document()}
        # the LEGACY reading shipped exactly this (the probe)
        assert verdict_freshness(evidence, current.candidate_digest) == FRESHNESS_CURRENT
        # the typed freshness and the applicability contract refuse it
        assert subject_freshness(evidence, current_subject=current).stale
        decision = applicability(
            ApplicabilityRequest(current=current, recorded=evidence["subject_identity"])
        )
        assert decision.invalidated
        assert decision.invalidated_inputs == ("source_base_oid",)
        assert decision.as_document()["invalidated_inputs"] == ["source_base_oid"]

    def test_a_rebuilt_environment_image_invalidates_dependent_evidence(self):
        recorded = the_subject()
        current = the_subject(environment_profile_digest="e" * 64)
        decision = applicability(
            ApplicabilityRequest(current=current, recorded=recorded.as_document())
        )
        assert decision.invalidated
        assert decision.invalidated_inputs == ("environment_profile_digest",)

    def test_a_changed_required_contract_invalidates_with_the_input_named(self):
        recorded = the_subject()
        current = the_subject()
        decision = applicability(
            ApplicabilityRequest(
                current=current,
                recorded=recorded.as_document(),
                recorded_contract_digest="k" * 32,
                required_contract_digest="m" * 32,
            )
        )
        assert decision.invalidated
        assert decision.invalidated_inputs == ("required_contract_digest",)
        # and an unchanged contract is named among the equivalents
        same = applicability(
            ApplicabilityRequest(
                current=current,
                recorded=recorded.as_document(),
                recorded_contract_digest="k" * 32,
                required_contract_digest="k" * 32,
            )
        )
        assert same.preserved
        assert "required_contract_digest" in same.equivalent_inputs

    def test_a_purely_editorial_plan_revision_preserves_applicability(self):
        """An editorial plan revision does not rerun unrelated tests: the
        decision names the inputs that stayed equivalent."""
        recorded = the_subject()
        current = the_subject(plan_revision_digest="c" * 64)
        decision = applicability(
            ApplicabilityRequest(current=current, recorded=recorded.as_document())
        )
        assert decision.preserved
        assert decision.invalidated_inputs == ()
        assert set(decision.equivalent_inputs) == {
            "candidate_digest",
            "source_oid",
            "source_base_oid",
            "tested_oid",
            "environment_profile_digest",
        }
        assert "editorial" in decision.reason
        document = decision.as_document()
        assert document["applicability"] == APPLICABILITY_PRESERVED
        assert document["equivalent_inputs"]

    def test_a_synthetic_merge_correlates_both_base_and_head(self):
        recorded = the_subject(tested_oid="9" * 40)  # the provider tested a merge
        preserved = applicability(
            ApplicabilityRequest(
                current=the_subject(tested_oid="9" * 40), recorded=recorded.as_document()
            )
        )
        assert preserved.preserved
        assert "synthetic merge" in preserved.reason
        # the head correlates but the TARGET moved: no reuse
        moved_base = applicability(
            ApplicabilityRequest(
                current=the_subject(tested_oid="9" * 40, source_base_oid="2" * 40),
                recorded=recorded.as_document(),
            )
        )
        assert moved_base.invalidated
        assert moved_base.invalidated_inputs == ("source_base_oid",)

    def test_missing_strict_fields_render_unknown_never_a_looser_success(self):
        """A stored record with its strict fields deleted (or never
        written) cannot downgrade to the two-string success path."""
        legacy_recorded = the_subject(source_base_oid="")  # the pre-R37-05 shape
        decision = applicability(
            ApplicabilityRequest(current=the_subject(), recorded=legacy_recorded.as_document())
        )
        assert decision.status == APPLICABILITY_UNKNOWN
        assert not decision.preserved and not decision.invalidated
        evidence = {"status": "passed", "subject_identity": legacy_recorded.as_document()}
        assert (
            subject_freshness(evidence, current_subject=the_subject()).status == FRESHNESS_UNKNOWN
        )
        # no recorded subject at all
        nobody = applicability(ApplicabilityRequest(current=the_subject(), recorded=None))
        assert nobody.status == APPLICABILITY_UNKNOWN

    def test_a_digest_content_mismatch_is_refused_not_normalized(self):
        tampered = the_subject().as_document()
        tampered["candidate_digest"] = "7" * 64  # the content moved, the digest did not
        assert VerificationSubject.from_document(tampered) is None
        decision = applicability(ApplicabilityRequest(current=the_subject(), recorded=tampered))
        assert decision.status == APPLICABILITY_UNKNOWN
        assert (
            subject_freshness({"subject_identity": tampered}, current_subject=the_subject()).status
            == FRESHNESS_UNKNOWN
        )

    def test_typed_freshness_compares_the_full_subject(self):
        recorded = the_subject()
        evidence = {"status": "passed", "subject_identity": recorded.as_document()}
        assert subject_freshness(evidence, current_subject=the_subject()).current
        # a moved tested revision (the provider tested something else)
        assert subject_freshness(evidence, current_subject=the_subject(tested_oid="9" * 40)).stale
        # a moved plan revision: a DIFFERENT subject identity
        assert subject_freshness(
            evidence, current_subject=the_subject(plan_revision_digest="c" * 64)
        ).stale
        # a missing revision digest on the current side: unknown, not current
        assert (
            subject_freshness(evidence, current_subject=the_subject(plan_revision_digest="")).status
            == FRESHNESS_UNKNOWN
        )


# ---------------------------------------------------------------------------
# The service route (fake clients, REAL gate): the strict evaluator wired
# into the verdict-time consumption point
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


async def drive_strict(
    db,
    fake,
    *,
    rows_for_digest,
    settings=None,
):
    """Drive a run to waiting_ci, freeze the STRICT inventory (bound to the
    run's CURRENT candidate digest) on its evidence, land the ingested
    observed-report rows, and hand back the armed service. *rows_for_digest*
    builds the rows once the digest is known — the sidecars must claim
    exactly the candidate the inventory froze."""
    service = make_service(
        db, fake, settings=settings or make_settings(FORGE_REQUIRED_JOBS="tests")
    )
    run_id, candidate = await drive_to_waiting_ci(db, service, fake)
    run = await get_run(db, run_id)
    digest = candidate_digest_of(
        source_oid=candidate,
        base_oid=run.base_sha or "",
        plan_digest=run.plan_digest or "",
    )
    rows = rows_for_digest(digest)
    async with db() as session:
        managed = await session.get(FlowRun, run_id)
        assert managed is not None
        managed.evidence = dict(managed.evidence or {}) | {
            "expected_report_inventory": strict_inventory(digest),
            "observed_reports": rows,
        }
        await session.commit()
    fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])
    reviewer = StubPRReviewer()
    service._stack = make_stack(fake, reviewer=reviewer)
    return service, run_id, candidate, digest, reviewer


class TestStrictServiceGate:
    async def test_p04_through_the_verifier_route_keeps_the_run_waiting(self, db, fake, tmp_path):
        """AT-05 through the production route: required checks are GREEN,
        but the strict inventory refuses the subject-less misplaced
        exit-1 report — the run records the typed unbound token and waits."""
        found = tmp_path / "reports"
        write_trx(found, "misplaced_Api.trx")
        write_trx(found, "forge_Domain.Tests_net9.0.trx")

        def rows_for_digest(digest: str) -> list[dict]:
            api = ingest(
                found,
                test_project="Forge.Api.Tests",
                report_path="misplaced_Api.trx",
                exit_code=1,
                with_sidecar=False,
            )
            domain = ingest(
                found,
                test_project="Forge.Domain.Tests",
                report_path="forge_Domain.Tests_net9.0.trx",
                candidate_id=digest,
            )
            return [api, domain]

        service, run_id, _candidate, digest, reviewer = await drive_strict(
            db, fake, rows_for_digest=rows_for_digest
        )
        assert digest
        await service.evaluate_waiting_ci_one(run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        verification = run.evidence["verification"]
        assert verification["status"] == STATUS_UNKNOWN
        assert verification["unbound_report"] == ["Forge.Api.Tests"]
        coverage = verification["expected_report_coverage"]
        assert coverage["complete"] is False
        assert coverage["unbound"] == ["Forge.Api.Tests"]
        assert coverage["satisfied"] == ["Forge.Domain.Tests"]
        outcomes = {row["obligation_id"]: row for row in coverage["outcomes"]}
        assert (
            outcomes["Forge.Api.Tests@forge_Api.Tests_net9.0.trx"]["report_match_outcome"]
            == MATCH_UNBOUND
        )
        # the RAW report digests are archived beside the decision
        archived = {row["obligation_id"]: row["digests"] for row in coverage["report_digests"]}
        assert archived["Forge.Domain.Tests@forge_Domain.Tests_net9.0.trx"]
        assert all(
            len(digest) == 64
            for digest in archived["Forge.Domain.Tests@forge_Domain.Tests_net9.0.trx"]
        )
        assert reviewer.calls == []  # no review, no ready

    async def test_numeric_attempt_ordering_through_the_route(self, db, fake, tmp_path):
        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx", failed=2)
        write_trx(found, "forge_Domain.Tests_net9.0.trx")

        def rows_for_digest(digest: str) -> list[dict]:
            newest = ingest(
                found,
                test_project="Forge.Api.Tests",
                report_path="forge_Api.Tests_net9.0.trx",
                candidate_id=digest,
                attempt="10",
            )
            older = dict(newest, attempt="9", outcome="passed")
            domain = ingest(
                found,
                test_project="Forge.Domain.Tests",
                report_path="forge_Domain.Tests_net9.0.trx",
                candidate_id=digest,
            )
            return [older, newest, domain]

        service, run_id, _candidate, _digest, reviewer = await drive_strict(
            db, fake, rows_for_digest=rows_for_digest
        )
        await service.evaluate_waiting_ci_one(run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        coverage = run.evidence["verification"]["expected_report_coverage"]
        assert coverage["failed"] == ["Forge.Api.Tests"]
        assert coverage["satisfied"] == ["Forge.Domain.Tests"]
        assert reviewer.calls == []

    async def test_conflicting_duplicates_through_the_route(self, db, fake, tmp_path):
        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx")
        write_trx(found, "forge_Domain.Tests_net9.0.trx")

        def rows_for_digest(digest: str) -> list[dict]:
            passed_row = ingest(
                found,
                test_project="Forge.Api.Tests",
                report_path="forge_Api.Tests_net9.0.trx",
                candidate_id=digest,
                attempt="7",
            )
            failed_row = dict(passed_row, outcome="failed", exit_code=1)
            domain = ingest(
                found,
                test_project="Forge.Domain.Tests",
                report_path="forge_Domain.Tests_net9.0.trx",
                candidate_id=digest,
            )
            return [passed_row, failed_row, domain]

        service, run_id, _candidate, _digest, _reviewer = await drive_strict(
            db, fake, rows_for_digest=rows_for_digest
        )
        await service.evaluate_waiting_ci_one(run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        verification = run.evidence["verification"]
        assert verification["duplicate_report_conflict"] == ["Forge.Api.Tests"]
        outcomes = {
            row["obligation_id"]: row
            for row in verification["expected_report_coverage"]["outcomes"]
        }
        assert (
            outcomes["Forge.Api.Tests@forge_Api.Tests_net9.0.trx"]["report_match_outcome"]
            == MATCH_DUPLICATE_CONFLICT
        )

    async def test_valid_reports_reach_verified_ready(self, db, fake, tmp_path):
        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx")
        write_trx(found, "forge_Domain.Tests_net9.0.trx")

        def rows_for_digest(digest: str) -> list[dict]:
            return [
                ingest(
                    found,
                    test_project="Forge.Api.Tests",
                    report_path="forge_Api.Tests_net9.0.trx",
                    candidate_id=digest,
                    attempt="3",
                ),
                ingest(
                    found,
                    test_project="Forge.Domain.Tests",
                    report_path="forge_Domain.Tests_net9.0.trx",
                    candidate_id=digest,
                    attempt="4",
                ),
            ]

        service, run_id, _candidate, _digest, reviewer = await drive_strict(
            db, fake, rows_for_digest=rows_for_digest
        )
        await service.evaluate_waiting_ci_one(run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        verification = run.evidence["verification"]
        assert verification["status"] == "passed"
        assert verification["expected_report_coverage"]["complete"] is True
        assert reviewer.calls  # the review ran

    async def test_an_explicit_pre_approved_waiver_still_passes_with_valid_reports(
        self, db, fake, tmp_path
    ):
        """Waivers are preserved WITHOUT broadening: the deployment's
        pre-approved skipped-conclusion policy still verifies the check
        surface, while the report obligations still demand their exact
        identities."""
        found = tmp_path / "reports"
        write_trx(found, "forge_Api.Tests_net9.0.trx")
        write_trx(found, "forge_Domain.Tests_net9.0.trx")

        def rows_for_digest(digest: str) -> list[dict]:
            return [
                ingest(
                    found,
                    test_project="Forge.Api.Tests",
                    report_path="forge_Api.Tests_net9.0.trx",
                    candidate_id=digest,
                ),
                ingest(
                    found,
                    test_project="Forge.Domain.Tests",
                    report_path="forge_Domain.Tests_net9.0.trx",
                    candidate_id=digest,
                ),
            ]

        service, run_id, candidate, digest, _reviewer = await drive_strict(
            db,
            fake,
            rows_for_digest=rows_for_digest,
            settings=make_settings(
                FORGE_REQUIRED_JOBS="tests",
                FORGE_VERIFICATION_WAIVE_CONCLUSIONS="skipped",
            ),
        )
        assert digest
        # the required check concluded SKIPPED — explicitly waived
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "skipped")])
        await service.evaluate_waiting_ci_one(run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert run.evidence["verification"]["expected_report_coverage"]["complete"] is True


# ---------------------------------------------------------------------------
# AT-06 through the service finalization + the operator rendering
# ---------------------------------------------------------------------------


def operator_rows(run_id: str, candidate: str, verification: dict) -> dict:
    """The source-row shape the operator snapshot reader derives from the
    SAME evidence fragment the service finalized (its ``_verification_row``)."""
    return {
        "run": {
            "id": run_id,
            "candidate_shas": [candidate],
            "evidence": {},
            "status": FlowStatus.REVIEWING.value,
        },
        "verifications": [
            {
                "verification_id": "run-evidence:verification",
                "result": str(verification.get("status") or ""),
                "candidate_sha": str(verification.get("tested_oid") or ""),
                "producer": str(verification.get("producer") or ""),
                "at": str(verification.get("observed_at") or ""),
            }
        ],
    }


class TestApplicabilityServiceRoute:
    async def _drive_verified_with_recorded_diff(
        self, db, fake, diff_digest: str
    ) -> tuple[str, str, dict]:
        """A run whose publication recorded the collector's DIFF digest —
        the exact shape where the candidate digest does NOT fold the base
        in (probe P06)."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            assert run is not None
            published = dict((run.evidence or {}).get("published_candidate") or {})
            published["diff_digest"] = diff_digest
            run.evidence = dict(run.evidence or {}) | {"published_candidate": published}
            await session.commit()
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])
        service._stack = make_stack(fake, reviewer=StubPRReviewer())
        await service.evaluate_waiting_ci_one(run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        return run_id, candidate, dict(run.evidence["verification"])

    async def _park_for_resume(self, db, run_id: str, **mutations) -> None:
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            assert run is not None
            for name, value in mutations.items():
                setattr(run, name, value)
            run.status = FlowStatus.REVIEWING.value
            await session.commit()

    async def test_identical_diff_bytes_on_a_moved_base_block_on_resume(self, db, fake):
        """AT-06: the same diff bytes re-cut from a DIFFERENT base (the
        target moved during review latency) — the recorded PASS cannot be
        reused: the resume blocks, the superseded proof stays archived
        with the applicability decision and its supersession reason."""
        diff_digest = "5" * 64
        run_id, candidate, verification = await self._drive_verified_with_recorded_diff(
            db, fake, diff_digest
        )
        assert verification["subject_identity"]["source_base_oid"] == BASE_HEAD
        # the legacy two-string gate would keep this verdict current
        assert verdict_freshness(verification, diff_digest) == FRESHNESS_CURRENT
        moved_base = "3" * 40
        await self._park_for_resume(db, run_id, base_sha=moved_base)

        await self._resume(db, fake, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("verification_stale")
        stale = run.evidence["verification"]
        assert stale["status"] == STATUS_STALE
        # the applicability fragment names the moved input
        assert stale["applicability"]["applicability"] == APPLICABILITY_INVALIDATED
        assert stale["applicability"]["invalidated_inputs"] == ["source_base_oid"]
        # archived, never deleted: the original identity + the reason
        assert stale["subject_identity"]["source_base_oid"] == BASE_HEAD
        assert stale["subject_identity"]["candidate_digest"] == diff_digest
        assert stale["superseded_reason"]

    async def test_a_purely_editorial_plan_revision_does_not_rerun_on_resume(self, db, fake):
        """AT-06: a purely editorial plan revision preserves applicability
        (the equivalent inputs are named) — the resume replays the review
        and reaches ready without fresh verification."""
        diff_digest = "6" * 64
        run_id, candidate, verification = await self._drive_verified_with_recorded_diff(
            db, fake, diff_digest
        )
        revised_plan = "e" * 64
        assert revised_plan != (await get_run(db, run_id)).plan_digest
        await self._park_for_resume(db, run_id, plan_digest=revised_plan)

        await self._resume(db, fake, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        verification = run.evidence["verification"]
        assert verification["status"] == "passed"
        # the shared decision says PRESERVED for exactly this shape
        current = VerificationSubject(
            candidate_digest=diff_digest,
            source_oid=candidate,
            tested_oid=candidate,
            plan_revision_digest=revised_plan,
            source_base_oid=BASE_HEAD,
        )
        decision = applicability(
            ApplicabilityRequest(current=current, recorded=verification["subject_identity"])
        )
        assert decision.preserved
        assert "source_base_oid" in decision.equivalent_inputs
        assert decision.invalidated_inputs == ()

    async def test_the_operator_rendering_agrees_with_the_service_decision(self, db, fake):
        """The operator view renders the SAME fragment the service
        finalized: a superseded (stale) verdict is NOT verified_ready
        there either — one readiness answer for the same typed subject,
        through the shared applicability decision."""
        diff_digest = "7" * 64
        run_id, candidate, verification = await self._drive_verified_with_recorded_diff(
            db, fake, diff_digest
        )
        # the current, applicable verdict renders verified_ready
        projection = initial_projection(operator_rows(run_id, candidate, verification))
        assert projection.state == "verified_ready"
        # the same typed subject after the base moved: the service withdraws
        await self._park_for_resume(db, run_id, base_sha="3" * 40)
        await self._resume(db, fake, run_id)
        stale = dict((await get_run(db, run_id)).evidence["verification"])
        assert stale["status"] == STATUS_STALE
        # ... and the operator rendering over the same fragment agrees
        projection = initial_projection(operator_rows(run_id, candidate, stale))
        assert projection.state != "verified_ready"

    async def _resume(self, db, fake, run_id: str) -> None:
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        service._stack = make_stack(fake, reviewer=StubPRReviewer())
        await service.resume_verification(run_id)
