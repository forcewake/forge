from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from forge.flows.loader import FlowLoader
from forge.flows.models import (
    FlowAction,
    FlowDefinition,
    FlowStep,
    FlowTrigger,
)
from forge.flows.runner import FlowRunner
from forge.flows.state import FlowStateManager
from forge.utils.redis_client import RedisManager
from forge.worker.queue import TaskQueue
from forge.worker.retry import RetryPolicy


@pytest.fixture()
def fake_redis():
    manager = RedisManager.__new__(RedisManager)
    server = fakeredis.FakeServer()
    manager._pool = None
    manager._client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    return manager


@pytest.fixture()
def state_mgr(fake_redis):
    return FlowStateManager(fake_redis)


@pytest.fixture()
def queue(fake_redis):
    return TaskQueue(fake_redis, RetryPolicy())


@pytest.fixture()
def mock_registry():
    registry = MagicMock()
    definition = MagicMock()
    definition.name = "code-reviewer"
    definition.type = "code-reviewer"
    definition.model_alias = "default"
    definition.actions = {"summary_note": True}
    definition.settings = {"cooldown": 120}
    registry.get.return_value = definition
    return registry


@pytest.fixture()
def mock_settings():
    settings = MagicMock()
    settings.GITLAB_URL = "https://gitlab.test"
    settings.GITLAB_TOKEN = MagicMock()
    settings.GITLAB_TOKEN.get_secret_value.return_value = "glpat-test"
    settings.FORGE_AGENTS_DIR = "agents/"
    return settings


@pytest.fixture()
def mock_config():
    return MagicMock()


@pytest.fixture()
def mock_session_factory():
    return MagicMock()


@pytest.fixture()
def sample_flow_def():
    return FlowDefinition(
        name="test-flow",
        version="1.0",
        description="Test flow",
        trigger=FlowTrigger(command="@forge /test", target="merge_request"),
        steps=[
            FlowStep(name="review", agent="code-reviewer", output_key="review"),
            FlowAction(
                name="report", action="post_comment", params={"body": "Done: {review.summary}"}
            ),
        ],
        timeout=1800,
    )


@pytest.fixture()
def flow_loader(sample_flow_def):
    loader = MagicMock(spec=FlowLoader)
    loader.get.return_value = sample_flow_def
    loader.get_by_command.return_value = sample_flow_def
    return loader


@pytest.fixture()
def runner(
    state_mgr, flow_loader, mock_registry, queue, mock_settings, mock_config, mock_session_factory
):
    return FlowRunner(
        state_mgr=state_mgr,
        loader=flow_loader,
        registry=mock_registry,
        queue=queue,
        settings=mock_settings,
        forge_config=mock_config,
        session_factory=mock_session_factory,
    )


def _make_event():
    """Create a mock GitLabEvent."""
    event = MagicMock()
    event.model_dump.return_value = {
        "object_kind": "merge_request",
        "project": {"id": 42, "path_with_namespace": "test/project"},
        "object_attributes": {"iid": 1, "action": "open"},
    }
    return event


async def test_start_flow_creates_instance(runner, state_mgr):
    event = _make_event()
    flow_def = runner.loader.get("test-flow")

    flow_id = await runner.start_flow(flow_def, event, project_id=42)

    assert len(flow_id) == 32  # UUID hex
    flow = await state_mgr.get(flow_id)
    assert flow is not None
    assert flow.status == "running"
    assert flow.current_step == 0


async def test_start_flow_enqueues_first_step(runner, queue):
    event = _make_event()
    flow_def = runner.loader.get("test-flow")

    await runner.start_flow(flow_def, event, project_id=42)

    depth = await queue.depth()
    assert depth == 1


@patch("forge.flows.runner.FlowRunner._execute_agent_step")
async def test_execute_step_runs_agent(mock_exec, runner, state_mgr):
    mock_exec.return_value = {"summary": "All good", "severity": "info"}

    event = _make_event()
    flow_def = runner.loader.get("test-flow")
    flow_id = await runner.start_flow(flow_def, event, project_id=42)

    # Execute step 0 (agent step)
    await runner.execute_step(flow_id, 0)

    mock_exec.assert_called_once()
    flow = await state_mgr.get(flow_id)
    assert flow.current_step == 1
    assert "review" in flow.state


@patch("forge.flows.runner.FlowRunner._execute_agent_step")
async def test_execute_step_advances_to_next(mock_exec, runner, state_mgr, queue):
    mock_exec.return_value = {"summary": "ok"}

    event = _make_event()
    flow_def = runner.loader.get("test-flow")
    flow_id = await runner.start_flow(flow_def, event, project_id=42)

    # Claim first task (start_flow enqueued step 0)
    await queue.claim("w", timeout=1.0)

    # Execute step 0
    await runner.execute_step(flow_id, 0)

    # Step 1 should be enqueued
    depth = await queue.depth()
    assert depth == 1


@patch("forge.flows.runner.FlowRunner._execute_action_step")
@patch("forge.flows.runner.FlowRunner._execute_agent_step")
async def test_flow_completes_after_last_step(mock_agent, mock_action, runner, state_mgr, queue):
    mock_agent.return_value = {"summary": "ok"}
    mock_action.return_value = {"posted": True}

    event = _make_event()
    flow_def = runner.loader.get("test-flow")
    flow_id = await runner.start_flow(flow_def, event, project_id=42)

    # Drain start task
    await queue.claim("w", timeout=1.0)

    # Execute both steps
    await runner.execute_step(flow_id, 0)
    await queue.claim("w", timeout=1.0)
    await runner.execute_step(flow_id, 1)

    flow = await state_mgr.get(flow_id)
    assert flow.status == "completed"


async def test_condition_false_skips_step(runner, state_mgr, queue):
    """When a step's condition evaluates to false, it should be skipped."""
    # Create a flow with a conditional step
    flow_def = FlowDefinition(
        name="cond-flow",
        version="1.0",
        description="Test",
        trigger=FlowTrigger(command="@forge /cond", target="merge_request"),
        steps=[
            FlowStep(
                name="conditional",
                agent="code-reviewer",
                condition="review.severity != 'critical'",
                output_key="result",
            ),
        ],
        timeout=1800,
    )
    runner.loader.get.return_value = flow_def

    event = _make_event()
    flow_id = await runner.start_flow(flow_def, event, project_id=42)

    # Set state so condition is false
    flow = await state_mgr.get(flow_id)
    flow.state["review"] = {"severity": "critical"}
    await state_mgr.create(flow)  # overwrite

    await queue.claim("w", timeout=1.0)
    await runner.execute_step(flow_id, 0)

    # Flow should complete (single step skipped)
    flow = await state_mgr.get(flow_id)
    assert flow.status == "completed"


@patch("forge.flows.runner.FlowRunner._execute_agent_step")
async def test_on_failure_abort(mock_exec, runner, state_mgr, queue):
    mock_exec.side_effect = RuntimeError("Agent crashed")

    flow_def = FlowDefinition(
        name="abort-flow",
        version="1.0",
        description="Test",
        trigger=FlowTrigger(command="@forge /abort", target="merge_request"),
        steps=[
            FlowStep(name="failing", agent="code-reviewer", on_failure="abort"),
        ],
        timeout=1800,
    )
    runner.loader.get.return_value = flow_def

    event = _make_event()
    flow_id = await runner.start_flow(flow_def, event, project_id=42)
    await queue.claim("w", timeout=1.0)

    await runner.execute_step(flow_id, 0)

    flow = await state_mgr.get(flow_id)
    assert flow.status == "failed"
    assert "Agent crashed" in flow.error


@patch("forge.flows.runner.FlowRunner._execute_agent_step")
async def test_on_failure_skip(mock_exec, runner, state_mgr, queue):
    mock_exec.side_effect = RuntimeError("Agent crashed")

    flow_def = FlowDefinition(
        name="skip-flow",
        version="1.0",
        description="Test",
        trigger=FlowTrigger(command="@forge /skip", target="merge_request"),
        steps=[
            FlowStep(name="failing", agent="code-reviewer", on_failure="skip"),
            FlowStep(name="next", agent="chat", output_key="result"),
        ],
        timeout=1800,
    )
    runner.loader.get.return_value = flow_def

    event = _make_event()
    flow_id = await runner.start_flow(flow_def, event, project_id=42)
    await queue.claim("w", timeout=1.0)

    await runner.execute_step(flow_id, 0)

    # Flow should still be running, step 1 enqueued
    flow = await state_mgr.get(flow_id)
    assert flow.status == "running"
    depth = await queue.depth()
    assert depth == 1


async def test_abort_flow(runner, state_mgr):
    event = _make_event()
    flow_def = runner.loader.get("test-flow")
    flow_id = await runner.start_flow(flow_def, event, project_id=42)

    await runner.abort_flow(flow_id, "User cancelled")

    flow = await state_mgr.get(flow_id)
    assert flow.status == "aborted"
    assert flow.error == "User cancelled"


async def test_execute_step_nonrunning_flow_skipped(runner, state_mgr, queue):
    event = _make_event()
    flow_def = runner.loader.get("test-flow")
    flow_id = await runner.start_flow(flow_def, event, project_id=42)

    await state_mgr.set_status(flow_id, "completed")
    await queue.claim("w", timeout=1.0)

    # Should be a no-op
    await runner.execute_step(flow_id, 0)

    flow = await state_mgr.get(flow_id)
    assert flow.status == "completed"


async def test_execute_step_missing_flow(runner):
    """Executing a step for a nonexistent flow should not raise."""
    await runner.execute_step("nonexistent", 0)
