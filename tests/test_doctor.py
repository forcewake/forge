"""Tests for forge doctor (M4): aggregation, formatting, exit codes."""

import pytest

import forge.doctor as doctor
from forge.doctor import CheckResult, _result, format_report, run_checks


def test_result_mapping():
    assert _result("a", True, "ok", "bad").status == "pass"
    assert _result("a", False, "ok", "bad").status == "fail"
    assert _result("a", None, "meh", "bad").status == "warn"


def test_format_report_counts():
    results = [
        CheckResult("one", "pass", "fine"),
        CheckResult("two-long-name", "fail", "broken"),
        CheckResult("three", "warn", "meh"),
    ]
    report = format_report(results)
    assert "forge doctor — 3 checks" in report
    assert "FAIL two-long-name  broken" in report
    assert "1 failed, 1 warned, 1 passed" in report


@pytest.mark.asyncio
async def test_run_checks_aggregates_core_and_project(monkeypatch):
    async def fake_gitlab(settings):
        return CheckResult("gitlab.token", "pass", "@me")

    async def fake_project(settings, project_id):
        return [CheckResult("project.exists", "pass", str(project_id))]

    async def fake_redis(settings):
        return CheckResult("redis", "fail", "down")

    async def skip(settings):
        return CheckResult("skipped", "pass", "")

    monkeypatch.setattr(doctor, "check_gitlab", fake_gitlab)
    monkeypatch.setattr(doctor, "check_redis", fake_redis)
    monkeypatch.setattr(doctor, "check_bot_token", skip)
    monkeypatch.setattr(doctor, "check_database", skip)
    monkeypatch.setattr(doctor, "check_litellm", skip)
    monkeypatch.setattr(doctor, "check_project", fake_project)

    settings = object()
    names = [r.name for r in await run_checks(settings)]
    assert names[:2] == ["python.version", "gitlab.token"]
    assert "project.exists" not in names

    results = await run_checks(settings, project_id=68)
    by_name = {r.name: r for r in results}
    assert by_name["project.exists"].detail == "68"
    assert by_name["redis"].status == "fail"


def test_main_json_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "Settings", lambda: object())

    async def all_pass(settings, project_id):
        return [CheckResult("one", "pass", "ok")]

    monkeypatch.setattr(doctor, "run_checks", all_pass)
    assert doctor.main(["--json"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out

    async def with_fail(settings, project_id):
        return [CheckResult("one", "fail", "nope")]

    monkeypatch.setattr(doctor, "run_checks", with_fail)
    assert doctor.main([]) == 1
    assert "1 failed" in capsys.readouterr().out
