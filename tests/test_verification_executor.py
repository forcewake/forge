"""Required report evidence on the shipped verification-binding owner.

The standalone installed-wheel executor and its synthetic environment are
retired. These tests ingest report bytes and apply the real frozen gate;
they do not claim that a separate verifier process ran.
"""

import hashlib
import json
import pytest
from forge.adaptive.qualification import ExpectedReport, ExpectedReports
from forge.adaptive.verification_binding import (
    ObservedReport,
    RequiredReportInventory,
    freeze_report_inventory_strict,
    ingest_report_file,
)

SUBJECT = "1" * 64
BUNDLE = "2" * 64
PRODUCER = "ci-native"


def inventory():
    reports = ExpectedReports(
        tuple(ExpectedReport(name, name + ".trx", SUBJECT, BUNDLE) for name in ("first", "second"))
    )
    document = freeze_report_inventory_strict(reports, contract_digest="3" * 64, producer=PRODUCER)
    return RequiredReportInventory.from_document(document)


def observed(project, **overrides):
    values = dict(
        test_project=project,
        outcome="passed",
        subject_digest=SUBJECT,
        report_path=project + ".trx",
        bundle_digest=BUNDLE,
        producer=PRODUCER,
        attempt="10",
        exit_code=0,
        report_digest="4" * 64,
    )
    values.update(overrides)
    return ObservedReport(**values)


def test_every_frozen_report_is_required_even_when_the_first_is_green():
    gate = inventory()
    partial = gate.coverage([observed("first")])
    assert not partial.verifies
    assert partial.missing
    complete = gate.coverage([observed("first"), observed("second")])
    assert complete.verifies


@pytest.mark.parametrize(
    "mutation",
    [
        {"outcome": "failed"},
        {"outcome": "skipped"},
        {"outcome": "missing"},
        {"subject_digest": "old"},
        {"subject_digest": ""},
        {"bundle_digest": "wrong"},
        {"report_path": "another.trx"},
        {"producer": "untrusted"},
        {"exit_code": 1},
    ],
)
def test_a_non_satisfying_second_report_never_hides_behind_the_first(mutation):
    result = inventory().coverage([observed("first"), observed("second", **mutation)])
    assert not result.verifies
    assert result.problems


def test_numeric_attempt_order_never_prefers_an_older_green_report():
    result = inventory().coverage(
        [
            observed("first"),
            observed("second", attempt="9"),
            observed("second", attempt="10", outcome="failed"),
        ]
    )
    assert not result.verifies
    assert result.failed


def test_conflicting_reports_with_the_same_identity_never_pick_a_favorable_winner():
    result = inventory().coverage(
        [observed("first"), observed("second"), observed("second", outcome="failed")]
    )
    assert not result.verifies
    assert result.duplicate_conflict


@pytest.mark.parametrize(("executed", "failed"), [(0, 0), (5, 1)])
def test_zero_test_and_failed_reports_fail_when_real_bytes_are_ingested(tmp_path, executed, failed):
    path = tmp_path / "second.trx"
    path.write_text(
        f'<TestRun><ResultSummary><Counters total="{executed}" executed="{executed}" passed="{executed - failed}" failed="{failed}" error="0" timeout="0" aborted="0" /></ResultSummary></TestRun>'
    )
    (tmp_path / "second.trx.identity.json").write_text(
        json.dumps({"candidate_id": SUBJECT, "bundle_digest": BUNDLE})
    )
    report = ingest_report_file(
        test_project="second",
        report_path=path.name,
        found_dir=tmp_path,
        exit_code=0,
        attempt="10",
        producer=PRODUCER,
    )
    assert report.outcome == "failed"
    assert report.report_digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not inventory().coverage([observed("first"), report]).verifies


@pytest.mark.parametrize("content", [None, "not XML", "<TestRun />"])
def test_missing_and_unparseable_reports_never_become_a_green_observation(tmp_path, content):
    if content is not None:
        (tmp_path / "second.trx").write_text(content)
    report = ingest_report_file(test_project="second", report_path="second.trx", found_dir=tmp_path)
    assert report.outcome in {"failed", "missing"}
    assert not inventory().coverage([observed("first"), report]).verifies


def test_an_unchanged_frozen_inventory_is_read_without_reexecuting_anything():
    frozen = inventory()
    restored = RequiredReportInventory.from_document(frozen.as_document())
    reports = [observed("first"), observed("second")]
    assert restored == frozen
    assert restored.coverage(reports) == frozen.coverage(reports)
