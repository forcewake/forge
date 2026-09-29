"""scripts/assert_no_skips.py: the Invariant Suite must execute, not skip."""

from __future__ import annotations

from tests._scripts import load_script

no_skips = load_script("assert_no_skips")


def _report(tmp_path, cases: str) -> str:
    path = tmp_path / "report.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>')
    return str(path)


def test_all_cases_executed_passes(tmp_path):
    report = _report(
        tmp_path, '<testcase classname="t" name="a"/><testcase classname="t" name="b"/>'
    )
    assert no_skips.main([report]) == 0


def test_a_skipped_case_fails(tmp_path, capsys):
    report = _report(
        tmp_path, '<testcase classname="t" name="a"><skipped message="no pg"/></testcase>'
    )
    assert no_skips.main([report]) == 1
    assert "skipped t::a" in capsys.readouterr().out


def test_an_empty_report_fails(tmp_path):
    assert no_skips.main([_report(tmp_path, "")]) == 1


def test_usage_error_without_a_report():
    assert no_skips.main([]) == 2
