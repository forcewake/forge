"""Each native connection is explicit; no default provider or disabled-provider flag."""

import json

from pydantic import ValidationError
import pytest

from forge.providers.gitlab.ingress import GitLabIngress
from forge.runtime.settings import RuntimeSettings, load
from tests.github_webhook_fixture import configured
from tests import gitlab_engine_fixture as native
from tests.native_hook_fixture import post
from tests.operator_native_fixture import operator_native as operator_native
from tests.runtime_fixture import configuration
from tests.test_gitlab_engine_ingress import SECRET, payload, signed


def test_gitlab_is_enabled_by_default_and_requires_its_settings(tmp_path):
    # Provider defaults are retired. An install must explicitly select connections.
    config, _ = configuration(tmp_path)
    document = config.model_dump(mode="json", by_alias=True)
    del document["connections"]
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(json.dumps(document))


def test_a_github_only_deployment_needs_no_gitlab_settings(tmp_path, monkeypatch):
    for name in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    config = configured(tmp_path)
    assert {connection.provider for connection in config.connections} == {"github"}
    assert not any(
        repository.read_token_env or repository.write_token_env
        for connection in config.connections
        for repository in connection.repositories
    )


def test_complete_gitlab_settings_validate(tmp_path):
    config, values = configuration(tmp_path)
    connection, repository = config.repositories()["project"]
    assert connection.provider == "gitlab" and repository.native_id == "123"
    assert values[connection.webhook_secret_env].encode() == SECRET


def test_unconfigured_gitlab_ingress_refuses_even_an_empty_token():
    with pytest.raises(ValueError):
        GitLabIngress("ce-1", b"", bots={"123": native.BOT})


def test_configured_gitlab_ingress_still_checks_the_token():
    ingress = GitLabIngress("ce-1", SECRET, bots={"123": native.BOT})
    headers, body = signed(payload())
    assert ingress.parse(headers, body).target.number == 7
    with pytest.raises(ValueError):
        ingress.parse({**headers, "X-Gitlab-Token": "wrong"}, body)


def test_settings_errors_never_echo_secret_values(tmp_path):
    path = tmp_path / "invalid.toml"
    path.write_text('GITLAB_TOKEN="synthetic-secret-must-not-be-printed"\n')
    with pytest.raises(ValueError) as caught:
        load(path)
    assert "synthetic-secret" not in str(caught.value)


async def test_disabled_gitlab_ingress_refuses_even_a_matching_token(operator_native):
    env = operator_native
    response = await post(env, connection="unconfigured")
    assert response.status_code == 404
    assert (await env.client.post("/webhook", json={})).status_code == 404
    assert not env.peer.calls and not env.gateway.writes
