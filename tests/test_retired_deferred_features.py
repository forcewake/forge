"""Deferred features are absent and cannot be re-enabled by stale settings."""

import importlib.util
import pytest
from forge.config import Settings
from forge.main import create_app


@pytest.mark.parametrize("name", ["forge.findings", "forge.mcp_server", "forge.reactive.ci_debug"])
def test_deferred_feature_module_is_absent(name):
    assert importlib.util.find_spec(name) is None


def test_mcp_is_not_mounted_and_its_settings_are_gone():
    settings = Settings(_env_file=None, FORGE_GITLAB_ENABLED=False, FORGE_MCP_KEY="stale-key")
    app = create_app(settings)
    assert all(getattr(route, "path", "") != "/mcp" for route in app.routes)
    assert not any(
        name.startswith("FORGE_MCP_") or name.startswith("FORGE_SECURITY_")
        for name in Settings.model_fields
    )


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("review", [False, True])
def test_security_is_a_typed_unavailable_command(provider, review):
    import json
    from pathlib import Path
    from forge.gateway.github_webhook import normalize_issue_comment
    from forge.gateway.router import _match_run_command
    from forge.gateway.parser import parse_webhook

    fixtures = Path(__file__).parent / "fixtures"
    if provider == "github":
        name = "issue_comment_pr.json" if review else "issue_comment_created.json"
        payload = json.loads((fixtures / "github_payloads" / name).read_text())
        payload["comment"]["body"] = "@forge /security"
        command = normalize_issue_comment(payload)
    else:
        name = "note_mr.json" if review else "note_issue.json"
        payload = json.loads((fixtures / name).read_text())
        payload["object_attributes"]["note"] = "@forge /security"
        command = _match_run_command(
            parse_webhook("Note Hook", payload),
            Settings(_env_file=None, FORGE_GITLAB_ENABLED=False),
        )
    assert command["command"] == "feature_unavailable"
    assert command["feature"] == "/security"
