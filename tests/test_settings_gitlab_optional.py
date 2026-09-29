"""GitLab settings are required only while GitLab is enabled."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from forge.config import Settings
from forge.gateway.validator import validate_webhook_token

GITLAB_ENV = ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_WEBHOOK_SECRET", "FORGE_GITLAB_ENABLED")


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env file is read
    for name in GITLAB_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_gitlab_is_enabled_by_default_and_requires_its_settings(clean_env):
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert "GITLAB_URL, GITLAB_TOKEN, GITLAB_WEBHOOK_SECRET must be set" in str(caught.value)


def test_a_github_only_deployment_needs_no_gitlab_settings(clean_env):
    clean_env.setenv("FORGE_GITLAB_ENABLED", "false")
    settings = Settings()
    assert settings.FORGE_GITLAB_ENABLED is False
    assert settings.GITLAB_URL == ""


def test_complete_gitlab_settings_validate(clean_env):
    settings = Settings(
        GITLAB_URL="https://gitlab.test", GITLAB_TOKEN="t", GITLAB_WEBHOOK_SECRET="w"
    )
    assert settings.GITLAB_WEBHOOK_SECRET.get_secret_value() == "w"


def test_unconfigured_gitlab_ingress_refuses_even_an_empty_token():
    with pytest.raises(HTTPException) as caught:
        validate_webhook_token("", "")
    assert caught.value.status_code == 503


def test_configured_gitlab_ingress_still_checks_the_token():
    assert validate_webhook_token("s3cret", "s3cret") == "s3cret"
    with pytest.raises(HTTPException) as caught:
        validate_webhook_token("s3cret", "wrong")
    assert caught.value.status_code == 401
