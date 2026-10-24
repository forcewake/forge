"""Removed features have no runtime module, configuration or executable command."""

from importlib.util import find_spec
import json

import pytest
from pydantic import ValidationError

from forge.ingress_values import Command
from forge.runtime.app import create_app
from forge.runtime.settings import RuntimeSettings
from tests.runtime_fixture import configuration


def absent(module):
    try:
        return find_spec(module) is None
    except ModuleNotFoundError:
        return True


@pytest.mark.parametrize("name", ["forge.findings", "forge.mcp_server", "forge.reactive.ci_debug"])
def test_deferred_feature_module_is_absent(name):
    assert absent(name)


def test_mcp_is_not_mounted_and_its_settings_are_gone(tmp_path):
    config, _ = configuration(tmp_path)
    assert all(getattr(route, "path", "") != "/mcp" for route in create_app(config).routes)
    for name in ("FORGE_MCP_KEY", "FORGE_SECURITY_ENABLED"):
        with pytest.raises(ValidationError):
            RuntimeSettings.model_validate_json(
                json.dumps({**config.model_dump(mode="json", by_alias=True), name: "stale"})
            )


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("review", [False, True])
def test_security_is_a_typed_unavailable_command(provider, review):
    from tests.test_github_engine_ingress import (
        payload as gh_payload,
        signed as gh_signed,
    )
    from tests.test_gitlab_engine_ingress import (
        payload as gl_payload,
        signed as gl_signed,
    )
    from forge.providers.github.ingress import GitHubIngress
    from forge.providers.gitlab.ingress import GitLabIngress
    from tests.test_github_rest import BOT
    from tests import gitlab_engine_fixture as native
    from tests.test_github_engine_ingress import SECRET

    if provider == "github":
        parser = GitHubIngress("conn-1", SECRET, repositories={"123": 900}, bot=BOT)
        body = gh_payload()
        body["comment"]["body"] = "/security"
        if review:
            body["issue"]["pull_request"] = {
                "url": "https://api.github.com/repos/new/repository/pulls/7"
            }
        event = parser.parse(*gh_signed(body))
    else:
        parser = GitLabIngress("ce-1", SECRET, bots={"123": native.BOT})
        body = gl_payload()
        body["object_attributes"]["note"] = "/security"
        if review:
            body["object_attributes"]["noteable_type"] = "MergeRequest"
            body["merge_request"] = {
                "id": 70,
                "iid": 7,
                "source_project_id": 123,
                "target_project_id": 123,
            }
        event = parser.parse(*gl_signed(body))
    assert event is not None and not event.commands
    with pytest.raises(ValidationError):
        Command(kind="security")
