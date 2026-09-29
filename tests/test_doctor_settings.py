"""forge doctor reports missing settings as FAIL lines, never a traceback."""

from __future__ import annotations

import json

import pytest

from forge import doctor
from forge.config import Settings


@pytest.fixture()
def empty_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env file is read
    for name in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_WEBHOOK_SECRET", "FORGE_GITLAB_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_missing_settings_are_reported_not_raised(empty_env, capsys):
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "GITLAB_URL" in out
    assert "Traceback" not in out


def test_missing_settings_in_json(empty_env, capsys):
    assert doctor.main(["--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["checks"][0]["name"] == "settings"
    assert "GITLAB_URL" in report["checks"][0]["detail"]


async def test_disabled_gitlab_is_a_warning_not_a_network_call(empty_env):
    empty_env.setenv("FORGE_GITLAB_ENABLED", "false")
    results = await doctor.check_gitlab_identity(Settings())
    assert [(r.name, r.status) for r in results] == [("gitlab.token", doctor.WARN)]
