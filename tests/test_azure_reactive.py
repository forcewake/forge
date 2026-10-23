"""Portable self-trigger lessons retained on the live reactive engine."""

from unittest.mock import AsyncMock
import pytest
from forge.config import ForgeConfig, Settings
from forge.reactive.github_review import GitHubReactiveReviewEngine
from forge.reactive.ci_debug import is_forge_harness_run


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


@pytest.mark.parametrize(
    ("branch", "workflow", "expected"),
    [
        ("forge/42/candidate", "forge-harness.yml", True),
        ("feature", "forge-harness.yml", False),
        ("forge/42/candidate", "human-ci.yml", False),
    ],
)
def test_only_the_owned_lane_is_excluded_from_reactive_debug(branch, workflow, expected):
    assert is_forge_harness_run(branch, workflow, "forge-harness.yml") is expected
