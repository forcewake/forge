"""Portable self-trigger lessons retained on the live reactive engine."""

from unittest.mock import AsyncMock
import pytest
from forge.config import ForgeConfig, Settings
from forge.reactive.github_review import GitHubReactiveReviewEngine
from forge.gateway.github_webhook import normalize_pull_request_event


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"sender_type": "Bot"}, "bot_sender"),
        ({"pr_author_type": "Bot"}, "bot_authored_pr"),
        ({"head_branch": "forge/42/candidate"}, "forge_branch"),
    ],
)
async def test_self_trigger_guard_precedes_every_provider_and_model_call(metadata, reason):
    client, reviewer = AsyncMock(), AsyncMock()
    engine = GitHubReactiveReviewEngine(
        Settings(_env_file=None, FORGE_GITLAB_ENABLED=False),
        ForgeConfig(),
        None,
        client=client,
        reviewer=reviewer,
        repo_full_name="acme/widget",
    )
    result = await engine.run(
        {"pr_number": 7, "head_sha": "a" * 40, "head_branch": "feature", **metadata}
    )
    assert result == {"status": "skipped", "reason": reason}
    assert client.mock_calls == []
    assert reviewer.mock_calls == []


@pytest.mark.parametrize("branch", ["forge/42/candidate", "feature/human-change"])
def test_owned_output_cannot_reenter_the_current_reactive_review(branch):
    import json
    from pathlib import Path

    payload = json.loads(
        (
            Path(__file__).parent / "fixtures/github_payloads/pull_request_synchronize.json"
        ).read_text()
    )
    payload["pull_request"]["head"]["ref"] = branch
    payload["sender"]["type"] = "User"
    payload["pull_request"]["user"]["type"] = "User"
    result = normalize_pull_request_event(payload)
    assert (result is None) == branch.startswith("forge/")
