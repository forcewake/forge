"""Tests for forge doctor (M4): aggregation, formatting, exit codes."""

import hashlib
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

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


# -- the credential-delivery preflight (R42-03 / #376) ---------------------------


PROJECT_ID = 68
CREDENTIAL_REF = "env:ANTHROPIC_AUTH_TOKEN"
CARRIER = "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN"
SENTINEL_SECRET = "sk-doctor-sentinel-never-printed-4b7e"


def _delivery_settings(**overrides):
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        FORGE_IMPLEMENTER_BACKEND="ci_harness",
        FORGE_HARNESS_PREFERENCE="claude-code",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeGitLabClient:
    """The project-check surface doctor reads: hooks, variables (with
    protected/masked metadata), runners, protected branches and the
    ``.forge.yml`` blob — values never carried, names/flags only."""

    def __init__(
        self,
        *,
        variables=None,
        protected_branches=None,
        config_text=None,
        protected_branches_error=False,
    ):
        self._variables = variables if variables is not None else []
        self._protected = protected_branches
        self._config_text = config_text
        self._protected_error = protected_branches_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _get(self, path):
        from forge.gitlab.blob_reads import BlobReadResult  # noqa: F401 — shape import

        if path.endswith("/hooks"):
            payload = [{"id": 1}]
        elif path.endswith("/variables"):
            payload = self._variables
        elif path.endswith("/runners"):
            payload = [{"id": 4, "active": True}]
        elif path.endswith("/protected_branches"):
            if self._protected_error:
                raise RuntimeError("protected branches unreadable")
            payload = self._protected if self._protected is not None else []
        else:
            payload = {"path_with_namespace": "grp/proj"}
        return SimpleNamespace(json=lambda: payload)

    async def read_blob(self, project_id, file_path, ref="HEAD"):
        from forge.gitlab.blob_reads import BlobReadResult

        if self._config_text is None:
            return BlobReadResult.not_found()
        return BlobReadResult(
            status="found",
            content=self._config_text,
            content_sha256=hashlib.sha256(self._config_text.encode()).hexdigest(),
            encoding="utf-8",
        )


def _bind_project(tmp_path, monkeypatch, project_id=PROJECT_ID):
    from forge.adaptive.operator_snapshot import CanonicalSubject
    from forge.adaptive.project_credentials import ProjectCredentialRegistry

    path = tmp_path / "bindings.json"
    registry = ProjectCredentialRegistry(path=path)
    registry.bind(
        CanonicalSubject(
            provider_family="gitlab", connection="gitlab.example", native_id=str(project_id)
        ),
        "anthropic-gateway",
        CREDENTIAL_REF,
        bound_by="test",
    )
    monkeypatch.setenv("FORGE_CREDENTIAL_BINDINGS", str(path))
    return path


def _delivery_env(monkeypatch, *, mode="gitlab-protected-variable", **extra):
    from pathlib import Path

    monkeypatch.setenv("FORGE_CREDENTIAL_DELIVERY", mode)
    monkeypatch.setenv(
        "FORGE_CREDENTIAL_TEMPLATE_DIR", str(Path(__file__).resolve().parents[1] / "ci/templates")
    )
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


async def _project_results(monkeypatch, tmp_path, client):
    monkeypatch.setattr(doctor, "GitLabClient", lambda **kwargs: client)
    from forge.orchestrator.project_config import clear_cache

    clear_cache()
    return await doctor.check_project(_delivery_settings(), PROJECT_ID)


class TestDoctorCredentialDelivery:
    """R42-03: the project checks resolve the delivery plan through the
    SHARED seam, run the per-mode prerequisites over redacted metadata,
    and never require the ambient name when it is not the consumer
    route (the #364 duplicate-ambient workaround's end)."""

    async def test_the_headline_arm_native_carrier_only_green(self, monkeypatch, tmp_path):
        """A native-carrier-only install — masked-not-protected carrier,
        NO ambient ANTHROPIC_AUTH_TOKEN duplicate — passes doctor."""
        _bind_project(tmp_path, monkeypatch)
        _delivery_env(monkeypatch)
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")  # no ambient duplicate at all
        client = _FakeGitLabClient(
            variables=[
                {"key": "ANTHROPIC_BASE_URL", "protected": False, "masked": False},
                {"key": CARRIER, "protected": False, "masked": True},
            ],
            config_text="implement:\n  paths:\n    - 'src/**'\n",
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        by_name = {r.name: r for r in results}

        lane = by_name["project.harness.claude-code"]
        assert lane.status == "pass"
        assert "substitutes the ambient ANTHROPIC_AUTH_TOKEN" in lane.detail
        assert by_name["project.harness_chain"].detail == "[claude-code]"
        assert by_name["credential.delivery_mode"].status == "pass"
        prerequisite = by_name["credential.prerequisite_outcome"]
        assert prerequisite.status == "pass"
        assert (
            "doctor.prerequisite_outcome{delivery_mode=gitlab-protected-variable}=pass"
            in prerequisite.detail
        )
        assert by_name["credential.route_consumer_match"].status == "pass"
        assert by_name["onboarding.review_scope"].status == "pass"
        assert [r for r in results if r.status == "fail"] == []

    async def test_removing_only_the_carrier_fails_precisely(self, monkeypatch, tmp_path):
        """The required native carrier removed, an unrelated ambient key
        retained: the precise failure names the carrier; the unrelated
        key does not satisfy the route; the ambient requirement returns."""
        _bind_project(tmp_path, monkeypatch)
        _delivery_env(monkeypatch)
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        client = _FakeGitLabClient(
            variables=[
                {"key": "ZAI_API_KEY", "protected": False, "masked": True},
            ],
            config_text="implement:\n  paths:\n    - 'src/**'\n",
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        by_name = {r.name: r for r in results}

        prerequisite = by_name["credential.prerequisite_outcome"]
        assert prerequisite.status == "fail"
        assert "native_carrier_absent" in prerequisite.detail
        assert CARRIER in prerequisite.detail
        # no substitution on a failed prerequisite: the ambient lane debt
        # is visible again (warn), never masked by the unrelated key.
        assert by_name["project.harness.claude-code"].status == "warn"
        assert "ANTHROPIC_AUTH_TOKEN" in by_name["project.harness.claude-code"].detail

    async def test_a_protected_only_carrier_on_unprotected_refs_reports_incompatibility(
        self, monkeypatch, tmp_path
    ):
        _bind_project(tmp_path, monkeypatch)
        _delivery_env(monkeypatch)
        client = _FakeGitLabClient(
            variables=[{"key": CARRIER, "protected": True, "masked": True}],
            protected_branches=[],  # factory refs are NOT protected
            config_text="implement:\n  paths:\n    - 'src/**'\n",
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        by_name = {r.name: r for r in results}

        prerequisite = by_name["credential.prerequisite_outcome"]
        assert prerequisite.status == "fail"
        assert "carrier_ref_incompatible" in prerequisite.detail
        assert "MR pipelines" in prerequisite.detail
        assert by_name["project.harness.claude-code"].status == "warn"

    async def test_a_protected_carrier_on_protected_factory_refs_passes(
        self, monkeypatch, tmp_path
    ):
        _bind_project(tmp_path, monkeypatch)
        _delivery_env(monkeypatch)
        client = _FakeGitLabClient(
            variables=[{"key": CARRIER, "protected": True, "masked": True}],
            protected_branches=[{"name": "factory/*"}],
            config_text="implement:\n  paths:\n    - 'src/**'\n",
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        by_name = {r.name: r for r in results}
        assert by_name["credential.prerequisite_outcome"].status == "pass"

    async def test_inaccessible_variable_or_ref_metadata_is_unknown_not_success(
        self, monkeypatch, tmp_path
    ):
        _bind_project(tmp_path, monkeypatch)
        _delivery_env(monkeypatch)
        # The carrier row exists but its flags never came back; the
        # protected-branch listing is unreadable — every compatibility
        # axis is unobservable, so the outcome is UNKNOWN (warn), never
        # a pass, and no ambient substitution happens.
        client = _FakeGitLabClient(
            variables=[{"key": CARRIER}],
            protected_branches_error=True,
            config_text="implement:\n  paths:\n    - 'src/**'\n",
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        by_name = {r.name: r for r in results}
        prerequisite = by_name["credential.prerequisite_outcome"]
        assert prerequisite.status == "warn"
        assert "unknown" in prerequisite.detail
        assert by_name["project.harness.claude-code"].status == "warn"

    async def test_redemption_mode_checks_configuration_not_key_validity(
        self, monkeypatch, tmp_path
    ):
        _bind_project(tmp_path, monkeypatch)
        _delivery_env(monkeypatch, mode="runner-redemption")
        client = _FakeGitLabClient(
            variables=[{"key": "ANTHROPIC_BASE_URL", "protected": False, "masked": False}],
            config_text="implement:\n  paths:\n    - 'src/**'\n",
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        by_name = {r.name: r for r in results}
        prerequisite = by_name["credential.prerequisite_outcome"]
        assert prerequisite.status == "pass"
        assert "redemption_prerequisites_ready" in prerequisite.detail
        assert "/lane/credentials/redeem" in prerequisite.detail
        assert "usable provider key" in prerequisite.detail  # the honesty line
        assert by_name["project.harness.claude-code"].status == "pass"

    async def test_legacy_mode_reports_the_explicit_policy(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FORGE_CREDENTIAL_BINDINGS", raising=False)
        monkeypatch.delenv("FORGE_CREDENTIAL_DELIVERY", raising=False)
        client = _FakeGitLabClient(
            variables=[
                {"key": "ANTHROPIC_AUTH_TOKEN", "protected": False, "masked": True},
                {"key": "ANTHROPIC_BASE_URL", "protected": False, "masked": False},
            ],
            config_text="implement:\n  paths:\n    - 'src/**'\n",
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        by_name = {r.name: r for r in results}
        assert by_name["credential.delivery_mode"].status == "warn"
        assert "ambient-legacy" in by_name["credential.delivery_mode"].detail
        assert "compat" in by_name["credential.delivery_mode"].detail

    async def test_the_report_carries_no_secret_value(self, monkeypatch, tmp_path):
        _bind_project(tmp_path, monkeypatch)
        _delivery_env(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", SENTINEL_SECRET)
        client = _FakeGitLabClient(
            variables=[
                {"key": "ANTHROPIC_BASE_URL", "protected": False, "masked": False},
                {"key": CARRIER, "value": SENTINEL_SECRET, "protected": False, "masked": True},
            ],
            config_text="implement:\n  paths:\n    - 'src/**'\n",
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        rendered = doctor.format_report(results)
        assert SENTINEL_SECRET not in rendered


class TestDoctorReviewScopePreflight:
    async def test_a_correction_enabled_project_missing_scope_gets_the_onboarding_action(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        client = _FakeGitLabClient(
            variables=[{"key": CARRIER, "protected": False, "masked": True}],
            config_text=None,  # no .forge.yml at all
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        by_name = {r.name: r for r in results}
        scope = by_name["onboarding.review_scope"]
        assert scope.status == "fail"
        assert "scope_missing" in scope.detail
        assert "ONBOARDING" in scope.detail
        assert "implement.paths" in scope.detail
        assert "manual_workarounds" in scope.detail

    async def test_a_malformed_scope_is_a_separately_named_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        client = _FakeGitLabClient(
            variables=[{"key": CARRIER, "protected": False, "masked": True}],
            config_text="implement: [unclosed\n",
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        by_name = {r.name: r for r in results}
        scope = by_name["onboarding.review_scope"]
        assert scope.status == "fail"
        assert "scope_unreadable" in scope.detail
        assert "scope_missing" not in scope.detail

    async def test_a_correction_disabled_project_owes_no_scope(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FORGE_REVIEW_FEEDBACK_ENABLED", raising=False)
        client = _FakeGitLabClient(
            variables=[{"key": CARRIER, "protected": False, "masked": True}],
            config_text=None,
        )

        results = await _project_results(monkeypatch, tmp_path, client)
        by_name = {r.name: r for r in results}
        assert by_name["onboarding.review_scope"].status == "pass"
        assert "feature_off" in by_name["onboarding.review_scope"].detail


class TestHarnessLaneSubstitution:
    """The check_harness_lanes leg of the #364 rule: the delivery report
    substitutes exactly the provider's ambient slot — never wider."""

    @staticmethod
    def _report(outcome="pass", code="native_carrier_ready", plan_mode="gitlab-protected-variable"):
        from forge.adaptive.credential_broker import CredentialDeliveryPlan
        from forge.adaptive.credential_preflight import (
            ModePrerequisite,
            CredentialPreflightReport,
        )

        plan = CredentialDeliveryPlan(
            subject="gitlab/gitlab.example/68",
            provider="anthropic-gateway",
            profile="gitlab",
            credential_ref=CREDENTIAL_REF,
            env_var="ANTHROPIC_AUTH_TOKEN",
            binding_revision=1,
            mode=plan_mode,
            transport_ref=CARRIER,
            dispatch_ref="ENV_ANTHROPIC_AUTH_TOKEN",
            redemption=False,
        )
        return CredentialPreflightReport(
            executor="claude-code",
            profile="gitlab",
            ref_class="unprotected",
            provider_route="anthropic-gateway",
            env_var="ANTHROPIC_AUTH_TOKEN",
            delivery_mode=plan_mode,
            consumer="the CI provider's secret facility",
            prerequisite=ModePrerequisite(outcome, code, "detail"),
            plan=plan,
        )

    def test_the_ambient_token_is_not_required_under_a_proven_carrier(self):
        """THE HEADLINE: no ambient ANTHROPIC_AUTH_TOKEN anywhere — the
        chain still compiles through the carrier."""
        results = doctor.check_harness_lanes(
            _delivery_settings(),
            {"ANTHROPIC_BASE_URL"},  # no ANTHROPIC_AUTH_TOKEN, no duplicate
            delivery=self._report(),
        )
        by_name = {r.name: r for r in results}
        assert by_name["project.harness.claude-code"].status == "pass"
        assert by_name["project.harness_chain"].detail == "[claude-code]"

    def test_without_the_report_the_old_ambient_requirement_stands(self):
        results = doctor.check_harness_lanes(_delivery_settings(), {"ANTHROPIC_BASE_URL"})
        by_name = {r.name: r for r in results}
        assert by_name["project.harness.claude-code"].status == "warn"

    def test_an_unknown_or_failed_prerequisite_never_substitutes(self):
        for outcome in ("unknown", "fail"):
            results = doctor.check_harness_lanes(
                _delivery_settings(),
                {"ANTHROPIC_BASE_URL"},
                delivery=self._report(outcome=outcome),
            )
            by_name = {r.name: r for r in results}
            assert by_name["project.harness.claude-code"].status == "warn"

    def test_the_substitution_is_exactly_the_provider_slot(self):
        # ANTHROPIC_BASE_URL (the other required ambient name) is NOT
        # substituted: only the provider credential slot is.
        results = doctor.check_harness_lanes(
            _delivery_settings(),
            {"ANTHROPIC_AUTH_TOKEN"},  # token present, BASE_URL missing
            delivery=self._report(),
        )
        by_name = {r.name: r for r in results}
        assert by_name["project.harness.claude-code"].status == "warn"
        assert "ANTHROPIC_BASE_URL" in by_name["project.harness.claude-code"].detail
