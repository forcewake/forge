"""Actual identity-bound report reconciliation survives qualification retirement."""

from __future__ import annotations
import json
import shutil
from dataclasses import replace
from pathlib import Path
import pytest
from forge.adaptive.qualification import (
    ExpectedReport,
    ExpectedReports,
    ReportVerdict,
    reconcile_reports,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "qualification"


CANDIDATE_ID = "cand-e2e-226"


CURRENT_BUNDLE = "9a6523b1570416565c0dd37ea4901dabadd91c888aa4461a51cf76a34a59e3f3"


STALE_BUNDLE = "f377c36ae0ace28509cbe1660ab2598920af9e764e9bb96ade7e9abdf0fbfbd4"


_API_TRX = "forge_Api.Tests_net9.0.trx"


_DOMAIN_TRX = "forge_Domain.Tests_net9.0.trx"


_INFRA_TRX = "forge_Infra.Tests_net9.0.trx"


def _expected_reports() -> ExpectedReports:
    return ExpectedReports(
        reports=(
            ExpectedReport(
                test_project="Forge.Api.Tests",
                report_path=_API_TRX,
                candidate_id=CANDIDATE_ID,
                bundle_digest=CURRENT_BUNDLE,
            ),
            ExpectedReport(
                test_project="Forge.Domain.Tests",
                report_path=_DOMAIN_TRX,
                candidate_id=CANDIDATE_ID,
                bundle_digest=CURRENT_BUNDLE,
            ),
            ExpectedReport(
                test_project="Forge.Infra.Tests",
                report_path=_INFRA_TRX,
                candidate_id=CANDIDATE_ID,
                bundle_digest=CURRENT_BUNDLE,
            ),
        )
    )


def _write_report(
    found_dir: Path,
    report_path: str,
    *,
    total: int,
    executed: int,
    passed: int,
    failed: int,
    candidate_id: str = CANDIDATE_ID,
    bundle_digest: str = CURRENT_BUNDLE,
) -> None:
    """Write one TRX + its identity sidecar into *found_dir*."""
    (found_dir / report_path).write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<TestRun id="00000000-0000-4000-8000-000000000000" '
        'name="synthetic" '
        'xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">\n'
        '  <ResultSummary outcome="Completed">\n'
        f'    <Counters total="{total}" executed="{executed}" passed="{passed}" '
        f'failed="{failed}" error="0" timeout="0" aborted="0" inconclusive="0" '
        'passedRunLevel="0" failedRunLevel="0" />\n'
        "  </ResultSummary>\n"
        "</TestRun>\n",
        encoding="utf-8",
    )
    (found_dir / (report_path + ".identity.json")).write_text(
        json.dumps({"candidate_id": candidate_id, "bundle_digest": bundle_digest}),
        encoding="utf-8",
    )


class TestReportReconciliation:
    def _found_dir(self, tmp_path: Path) -> Path:
        found = tmp_path / "reports"
        found.mkdir()
        for name in (_API_TRX, _DOMAIN_TRX):
            shutil.copy(FIXTURES / name, found / name)
            shutil.copy(FIXTURES / f"{name}.identity.json", found / f"{name}.identity.json")
        # Forge.Infra.Tests is deliberately absent: its report is MISSING.
        return found

    def test_the_three_project_scenario_surfaces_each_problem_distinctly(self, tmp_path):
        reconciliation = reconcile_reports(_expected_reports(), self._found_dir(tmp_path))
        by_project = {v.test_project: v for v in reconciliation.verdicts}
        assert by_project["Forge.Api.Tests"].verdict == "failed"
        assert by_project["Forge.Api.Tests"].failed == 2
        assert by_project["Forge.Infra.Tests"].verdict == "missing_report"
        assert by_project["Forge.Domain.Tests"].verdict == "stale_report"
        # the aggregate is NOT green, and every problem is listed — the
        # passing 12/12 TRX inside the stale Domain report cancels nothing
        assert not reconciliation.is_green
        problems = reconciliation.problems
        assert any("Forge.Api.Tests" in p and "failed" in p for p in problems)
        assert any("Forge.Infra.Tests" in p and "missing_report" in p for p in problems)
        assert any("Forge.Domain.Tests" in p and "stale_report" in p for p in problems)

    def test_a_missing_report_is_never_zero_failures(self, tmp_path):
        reconciliation = reconcile_reports(_expected_reports(), self._found_dir(tmp_path))
        infra = next(v for v in reconciliation.verdicts if v.test_project == "Forge.Infra.Tests")
        assert infra.verdict == "missing_report"
        assert infra.failed is None  # unknown, NOT zero
        assert infra.total is None and infra.executed is None and infra.passed is None
        assert "never zero failures" in infra.detail

    def test_a_failing_project_cannot_disappear_behind_a_passing_trx(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(found, _DOMAIN_TRX, total=12, executed=12, passed=12, failed=0)
        _write_report(found, _API_TRX, total=12, executed=12, passed=10, failed=2)
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Domain.Tests",
                    report_path=_DOMAIN_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
                ExpectedReport(
                    test_project="Forge.Api.Tests",
                    report_path=_API_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        reconciliation = reconcile_reports(expected, found)
        assert not reconciliation.is_green
        assert reconciliation.problems == (
            "Forge.Api.Tests: failed — 2 failing test(s) of 12 executed",
        )

    def test_a_stale_leftover_names_both_identities(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(
            found,
            _DOMAIN_TRX,
            total=12,
            executed=12,
            passed=12,
            failed=0,
            candidate_id="cand-e2e-000",  # the PREVIOUS run's identity
            bundle_digest=STALE_BUNDLE,
        )
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Domain.Tests",
                    report_path=_DOMAIN_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        verdict = reconcile_reports(expected, found).verdicts[0]
        assert verdict.verdict == "stale_report"
        assert verdict.failed is None
        assert "cand-e2e-000" in verdict.detail and CANDIDATE_ID in verdict.detail

    def test_a_report_without_an_identity_sidecar_is_a_leftover(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(found, _API_TRX, total=4, executed=4, passed=4, failed=0)
        (found / f"{_API_TRX}.identity.json").unlink()
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Api.Tests",
                    report_path=_API_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        verdict = reconcile_reports(expected, found).verdicts[0]
        assert verdict.verdict == "stale_report"
        assert "identity" in verdict.detail

    def test_an_unparseable_report_is_unknown_never_green(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        (found / _API_TRX).write_text("<not-a-trx>", encoding="utf-8")
        (found / f"{_API_TRX}.identity.json").write_text(
            json.dumps({"candidate_id": CANDIDATE_ID, "bundle_digest": CURRENT_BUNDLE}),
            encoding="utf-8",
        )
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Api.Tests",
                    report_path=_API_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        verdict = reconcile_reports(expected, found).verdicts[0]
        assert verdict.verdict == "unparseable_report"
        assert verdict.failed is None and verdict.total is None

    def test_a_report_that_executed_nothing_proves_nothing(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(found, _API_TRX, total=4, executed=0, passed=0, failed=0)
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Api.Tests",
                    report_path=_API_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        verdict = reconcile_reports(expected, found).verdicts[0]
        assert verdict.verdict == "failed"
        assert "zero tests" in verdict.detail

    def test_an_all_green_reconciliation_is_green_with_counts(self, tmp_path):
        found = tmp_path / "reports"
        found.mkdir()
        _write_report(found, _API_TRX, total=12, executed=12, passed=12, failed=0)
        expected = ExpectedReports(
            reports=(
                ExpectedReport(
                    test_project="Forge.Api.Tests",
                    report_path=_API_TRX,
                    candidate_id=CANDIDATE_ID,
                    bundle_digest=CURRENT_BUNDLE,
                ),
            )
        )
        reconciliation = reconcile_reports(expected, found)
        assert reconciliation.is_green
        assert reconciliation.problems == ()
        assert reconciliation.verdicts[0].verdict == "passed"
        assert reconciliation.verdicts[0].passed == 12

    def test_expected_reports_refuse_overlaps_and_emptiness(self):
        one = ExpectedReport(
            test_project="A", report_path="a.trx", candidate_id="c", bundle_digest="d"
        )
        with pytest.raises(ValueError, match="at least one"):
            ExpectedReports(reports=())
        with pytest.raises(ValueError, match="per test project"):
            ExpectedReports(reports=(one, replace(one, report_path="b.trx")))
        with pytest.raises(ValueError, match="per report path"):
            ExpectedReports(reports=(one, replace(one, test_project="B")))

    def test_an_expected_report_names_every_binding(self):
        with pytest.raises(ValueError, match="candidate_id"):
            ExpectedReport(
                test_project="A", report_path="a.trx", candidate_id="", bundle_digest="d"
            )

    def test_a_verdict_outside_the_vocabulary_is_refused(self):
        with pytest.raises(ValueError, match="vocabulary"):
            ReportVerdict(test_project="A", report_path="a.trx", verdict="greenish", detail="")

    def test_of_path_finds_the_expected_row(self):
        assert _expected_reports().of_path(_API_TRX) is not None
        assert _expected_reports().of_path("nope.trx") is None
