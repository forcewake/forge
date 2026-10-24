"""Installed CLI failures carry no tracebacks, ambient config or secret values."""

import json

from forge.runtime import cli
from forge.runtime.settings import RuntimeSettings
from pydantic import ValidationError
import pytest

from tests.runtime_fixture import configuration


def test_missing_settings_are_reported_not_raised(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["doctor"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "failed"
    assert not captured.err and "Traceback" not in captured.out


def test_missing_settings_in_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GITLAB_TOKEN=synthetic-secret-must-not-be-read\n")
    assert cli.main(["doctor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"][0]["name"] == "installation"
    assert "synthetic-secret" not in json.dumps(report)


def test_disabled_gitlab_is_a_warning_not_a_network_call(tmp_path):
    # Disabled-provider flags are retired; unknown flags refuse during parsing.
    config, _ = configuration(tmp_path)
    body = config.model_dump(mode="json", by_alias=True)
    body["connections"][0]["enabled"] = False
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(json.dumps(body))


def test_project_checks_refuse_when_gitlab_is_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FORGE_GITLAB_ENABLED", "false")
    assert cli.main(["onboard", "unconfigured"]) == 1
    output = capsys.readouterr().err
    assert "operation refused" in output and "Traceback" not in output
