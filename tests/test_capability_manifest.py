"""Commands are proved through current ingress, without a capability registry."""

import json
from pathlib import Path

import pytest

from forge.config import Settings
from forge.gateway.github_webhook import _GITHUB_RUN_COMMANDS, normalize_issue_comment
from forge.gateway.router import _RUN_COMMANDS, _match_run_command
from forge.gateway.parser import parse_webhook

FIXTURES = Path(__file__).parent / "fixtures"


def normalized(provider, command):
    if provider == "github":
        payload = json.loads((FIXTURES / "github_payloads/issue_comment_created.json").read_text())
        payload["comment"]["body"] = "@forge " + command
        return normalize_issue_comment(payload)
    payload = json.loads((FIXTURES / "note_issue.json").read_text())
    payload["object_attributes"]["note"] = "@forge " + command
    return _match_run_command(
        parse_webhook("Note Hook", payload), Settings(_env_file=None, FORGE_GITLAB_ENABLED=False)
    )


def test_classic_commands_match_the_live_ingress_sets():
    assert _RUN_COMMANDS == _GITHUB_RUN_COMMANDS
    for verb in _RUN_COMMANDS:
        gh, gl = normalized("github", verb), normalized("gitlab", verb)
        assert gh is not None and gl is not None
        assert gh["command"] == gl["command"]


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_deferred_commands_are_explicitly_unavailable(provider):
    result = normalized(provider, "/security")
    assert result["command"] == "feature_unavailable"
    assert result["feature"] == "/security"


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_unrecognized_commands_do_not_gain_a_capability_claim(provider):
    assert normalized(provider, "/invented") is None


def test_operator_and_review_feedback_rollouts_default_off():
    from forge.adaptive.command_router import adaptive_command_set
    from forge.gateway.feedback import review_feedback_command_set

    assert adaptive_command_set({}) == frozenset()
    assert review_feedback_command_set({}) == frozenset()
