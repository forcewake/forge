"""Retained installation lessons on the sole read-only runtime diagnostics.

Credential windows, protected-variable carriers and ambient substitution are
retired. Historical names pin refusal rather than recreate those mechanisms.
"""

from contextlib import asynccontextmanager
import json

import httpx
from pydantic import ValidationError
import pytest

from forge.runtime import cli, diagnostics
from forge.runtime.assembly import open_runtime
from forge.runtime.diagnostics import Diagnostic, Report, inspect
from forge.runtime.settings import RuntimeSettings, secret
from forge.store.schema import SchemaMismatch
from tests.operator_native_fixture import operator_native as operator_native
from tests.runtime_fixture import configuration


def closed(config, mutate):
    body = config.model_dump(mode="json", by_alias=True)
    mutate(body)
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(json.dumps(body))


def test_result_mapping():
    assert Report((Diagnostic("schema", "pass", "checked"),)).passed
    assert not Report((Diagnostic("schema", "fail", "unavailable"),)).passed
    assert not Report(()).passed


def test_format_report_counts():
    report = Report((Diagnostic("one", "pass", "checked"), Diagnostic("two", "fail", "down")))
    value = report.document()
    assert value["status"] == "failed"
    assert value["checks"] == [
        {"name": "one", "status": "pass", "detail": "checked"},
        {"name": "two", "status": "fail", "detail": "down"},
    ]


async def test_run_checks_aggregates_core_and_project(operator_native):
    env = operator_native
    report = await inspect(env.service)
    assert report.passed
    assert [check.name for check in report.checks] == [
        "schema",
        "native_budget:test-model",
        "repository:project",
    ]
    assert env.peer.calls and all(request.method == "GET" for request in env.peer.calls)
    assert not env.gateway.writes
    assert (await inspect(env.service, "project")).passed
    env.peer.calls.clear()
    assert not (await inspect(env.service, "foreign")).passed
    assert not env.peer.calls and not env.gateway.writes


async def test_main_json_exit_codes(operator_native, monkeypatch, capsys):
    env = operator_native

    @asynccontextmanager
    async def selected(config, **kwargs):
        assert config is env.config
        yield env.service

    monkeypatch.setattr(cli, "load", lambda path: env.config)
    monkeypatch.setattr(cli, "environment", lambda: env.values)
    monkeypatch.setattr(cli, "open_runtime", selected)
    assert await cli.execute(cli.parser().parse_args(["doctor"])) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    env.gateway.unavailable = True
    assert await cli.execute(cli.parser().parse_args(["doctor"])) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert not env.gateway.writes


class TestLegacyCredentialWindowCheck:
    def test_an_explicit_deadline_reports_source_deadline_and_days(self, tmp_path):
        config, _ = configuration(tmp_path)
        closed(config, lambda body: body.update(legacy_credential_deadline="2099-01-01"))

    def test_the_persisted_anchor_file_is_reported_as_the_source(self, tmp_path, monkeypatch):
        anchor = tmp_path / "obsolete-anchor"
        monkeypatch.setenv("FORGE_CREDENTIAL_ANCHOR_FILE", str(anchor))
        config, _ = configuration(tmp_path)
        assert "legacy" not in type(config).model_fields
        assert not anchor.exists()

    def test_a_refused_window_fails_with_the_specific_diagnostic(self, tmp_path):
        config, _ = configuration(tmp_path)
        closed(config, lambda body: body.update(legacy_window={"allow": True}))

    def test_invalid_configuration_fails_as_configuration_invalid(self, tmp_path):
        config, _ = configuration(tmp_path)
        closed(config, lambda body: body.update(credential_delivery="ambient-legacy"))

    async def test_the_grandfathered_count_is_visible_from_the_authority(
        self, operator_native, monkeypatch
    ):
        env = operator_native

        async def refused(engine):
            raise SchemaMismatch("synthetic historical authority")

        monkeypatch.setattr(diagnostics, "check_schema", refused)
        report = await inspect(env.service)
        assert not report.passed and [check.name for check in report.checks] == ["schema"]
        assert not env.peer.calls and not env.gateway.writes


class TestDoctorCredentialDelivery:
    async def test_the_headline_arm_native_carrier_only_green(self, operator_native, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        env = operator_native
        assert (await inspect(env.service)).passed
        assert not env.gateway.keys and not env.gateway.writes

    async def test_removing_only_the_carrier_fails_precisely(self, operator_native):
        env = operator_native
        values = {**env.values, "ANTHROPIC_AUTH_TOKEN": "unrelated-synthetic-token"}
        del values[env.config.gateway.admin_key_env]
        with pytest.raises(ValueError, match=env.config.gateway.admin_key_env):
            async with open_runtime(
                env.config,
                environ=values,
                native_transport=httpx.MockTransport(env.peer),
                gateway_transport=httpx.MockTransport(env.gateway),
            ):
                pytest.fail("ambient token must not replace the selected credential")
        assert not env.peer.calls and not env.gateway.writes

    def test_a_protected_only_carrier_on_unprotected_refs_reports_incompatibility(self, tmp_path):
        config, _ = configuration(tmp_path)
        closed(
            config,
            lambda body: body["connections"][0]["repositories"][0].update(
                credential_carrier={"protected": True}
            ),
        )

    def test_a_protected_carrier_on_protected_factory_refs_passes(self, tmp_path):
        config, _ = configuration(tmp_path)
        closed(
            config,
            lambda body: body["connections"][0]["repositories"][0].update(
                protected_factory_credentials=True
            ),
        )

    async def test_inaccessible_variable_or_ref_metadata_is_unknown_not_success(
        self, operator_native, monkeypatch
    ):
        env = operator_native
        native = env.service.connections.registry.named("project")

        async def unavailable(*args):
            raise RuntimeError("synthetic-response-secret-must-not-escape")

        monkeypatch.setattr(type(native.repository), "head", unavailable)
        report = await inspect(env.service)
        assert not report.passed
        assert report.checks[-1].status == "fail"
        assert "synthetic-response-secret" not in json.dumps(report.document())
        assert not env.gateway.writes

    async def test_redemption_mode_checks_configuration_not_key_validity(self, operator_native):
        env = operator_native
        report = await inspect(env.service)
        assert report.passed
        assert not env.gateway.keys and not env.gateway.writes
        assert all(check.name != "usable_provider_key" for check in report.checks)

    def test_legacy_mode_reports_the_explicit_policy(self, tmp_path):
        config, _ = configuration(tmp_path)
        closed(config, lambda body: body["gateway"].update(mode="ambient-legacy"))

    async def test_the_report_carries_no_secret_value(self, operator_native):
        env = operator_native
        rendered = json.dumps((await inspect(env.service)).document())
        assert all(value not in rendered for value in env.values.values())
        assert not env.gateway.writes


class TestDoctorReviewScopePreflight:
    def test_a_correction_enabled_project_missing_scope_gets_the_onboarding_action(self, tmp_path):
        config, _ = configuration(tmp_path)
        closed(
            config,
            lambda body: body["connections"][0]["repositories"][0]["policy"].pop("write_policy"),
        )

    def test_a_malformed_scope_is_a_separately_named_failure(self, tmp_path):
        config, _ = configuration(tmp_path)
        closed(
            config,
            lambda body: body["connections"][0]["repositories"][0]["policy"].update(
                write_policy="src/**"
            ),
        )

    def test_a_correction_disabled_project_owes_no_scope(self, tmp_path):
        config, _ = configuration(tmp_path)
        closed(
            config,
            lambda body: body["connections"][0]["repositories"][0].update(
                review_feedback_enabled=False
            ),
        )


class TestHarnessLaneSubstitution:
    def test_the_ambient_token_is_not_required_under_a_proven_carrier(self, tmp_path):
        config, values = configuration(tmp_path)
        assert "ANTHROPIC_AUTH_TOKEN" not in values
        assert secret(config.gateway.admin_key_env, values).get_secret_value()

    def test_without_the_report_the_old_ambient_requirement_stands(self, tmp_path):
        config, _ = configuration(tmp_path)
        with pytest.raises(ValueError):
            secret(config.gateway.admin_key_env, {"ANTHROPIC_AUTH_TOKEN": "synthetic-old-key"})

    def test_an_unknown_or_failed_prerequisite_never_substitutes(self, tmp_path):
        config, values = configuration(tmp_path)
        for missing in ("", "\x00", "x" * 65537):
            with pytest.raises(ValueError):
                secret(
                    config.gateway.admin_key_env, {**values, config.gateway.admin_key_env: missing}
                )

    def test_the_substitution_is_exactly_the_provider_slot(self, tmp_path):
        config, values = configuration(tmp_path)
        del values[config.secret_key_env]
        assert secret(config.gateway.admin_key_env, values).get_secret_value()
        with pytest.raises(ValueError):
            secret(config.secret_key_env, values, minimum=32)


async def test_read_only_diagnostics_have_one_total_deadline(operator_native, monkeypatch):
    import asyncio

    async def stalled(model):
        await asyncio.Event().wait()

    env = operator_native
    monkeypatch.setattr(diagnostics, "DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(env.service.budget.admission, "readiness", stalled)
    report = await inspect(env.service)
    assert not report.passed and [check.name for check in report.checks] == ["deadline"]
    assert not env.peer.calls and not env.gateway.writes
