"""Tests for forge doctor (M4): aggregation, formatting, exit codes."""

from types import SimpleNamespace

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

    async def fake_legacy_window(settings):
        return [CheckResult("credential.legacy_deadline", "pass", "stub")]

    # Q35-06: stubbed here like every other environment-contacting check —
    # the window's own behavior has dedicated tests below.
    monkeypatch.setattr(doctor, "check_legacy_credential_window", fake_legacy_window)

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


# -- the legacy-credential window (Q35-06) ---------------------------------------


class TestLegacyCredentialWindowCheck:
    """The doctor leg of Q35-06: anchor source, deadline, days remaining,
    and the grandfathered drain count — never a credential value."""

    async def test_an_explicit_deadline_reports_source_deadline_and_days(self, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from forge.api_lane_control import LEGACY_CREDENTIAL_DEADLINE_ENV

        deadline = datetime.now(timezone.utc) + timedelta(days=10, minutes=5)
        monkeypatch.setenv(LEGACY_CREDENTIAL_DEADLINE_ENV, deadline.isoformat())

        results = await doctor.check_legacy_credential_window(SimpleNamespace())

        assert len(results) == 1
        check = results[0]
        assert check.name == "credential.legacy_deadline"
        assert check.status == "warn"  # no DB → count not visible, not zero
        assert "source=explicit" in check.detail
        assert deadline.isoformat() in check.detail
        assert "10 day(s) remaining" in check.detail
        assert "not visible" in check.detail  # honest about the unseen count

    async def test_the_persisted_anchor_file_is_reported_as_the_source(self, tmp_path, monkeypatch):
        from forge.api_lane_control import (
            LEGACY_CREDENTIAL_ANCHOR_FILE_ENV,
            resolve_legacy_window,
        )

        anchor = tmp_path / "anchor"
        monkeypatch.setenv(LEGACY_CREDENTIAL_ANCHOR_FILE_ENV, str(anchor))
        first = resolve_legacy_window()  # the write-once creation

        results = await doctor.check_legacy_credential_window(SimpleNamespace())

        assert results[0].name == "credential.legacy_deadline"
        assert "source=persisted-file" in results[0].detail
        assert first.deadline is not None and first.deadline.isoformat() in results[0].detail
        assert "day(s) remaining" in results[0].detail

    async def test_a_refused_window_fails_with_the_specific_diagnostic(self, tmp_path, monkeypatch):
        from forge.api_lane_control import LEGACY_CREDENTIAL_ANCHOR_FILE_ENV

        blocker = tmp_path / "blocker"
        blocker.write_text("a regular file, so the anchor path cannot exist")
        monkeypatch.setenv(LEGACY_CREDENTIAL_ANCHOR_FILE_ENV, str(blocker / "anchor"))

        results = await doctor.check_legacy_credential_window(SimpleNamespace())

        assert results[0].name == "credential.legacy_deadline"
        assert results[0].status == "fail"
        assert "refused rather than re-anchored" in results[0].detail

    async def test_invalid_configuration_fails_as_configuration_invalid(self, monkeypatch):
        from forge.api_lane_control import LANE_LEGACY_TOKEN_DEADLINE_ENV

        monkeypatch.setenv(LANE_LEGACY_TOKEN_DEADLINE_ENV, "soon")

        results = await doctor.check_legacy_credential_window(SimpleNamespace())

        assert results[0].name == "credential.configuration_invalid"
        assert results[0].status == "fail"
        assert LANE_LEGACY_TOKEN_DEADLINE_ENV in results[0].detail
        assert "'soon'" in results[0].detail

    async def test_the_grandfathered_count_is_visible_from_the_authority(
        self, tmp_path, monkeypatch
    ):
        from datetime import datetime, timedelta, timezone

        from forge.api_lane_control import LEGACY_CREDENTIAL_DEADLINE_ENV
        from forge.database import get_engine
        from forge.durable.models import FlowRun
        from forge.models.base import Base

        url = f"sqlite+aiosqlite:///{tmp_path}/authority.db"
        engine = get_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.begin() as conn:
            for index, generation in enumerate((0, 0, 5)):
                await conn.execute(
                    FlowRun.__table__.insert().values(
                        id=f"run-{index}",
                        project_id=1,
                        provider="github",
                        cancellation_generation=generation,
                    )
                )
        await engine.dispose()

        deadline = datetime.now(timezone.utc) + timedelta(days=3, minutes=5)
        monkeypatch.setenv(LEGACY_CREDENTIAL_DEADLINE_ENV, deadline.isoformat())

        results = await doctor.check_legacy_credential_window(SimpleNamespace(DATABASE_URL=url))

        check = results[0]
        assert check.status == "warn"  # two works still drain
        assert "grandfathered generation-less works: 2" in check.detail
