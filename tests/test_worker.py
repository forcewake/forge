import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from forge.gitlab.events import MergeRequestEvent, MRObjectAttributes, ProjectInfo, UserInfo
from forge.worker.app import run_worker
from forge.worker.tasks import create_task


def _mr_event() -> MergeRequestEvent:
    return MergeRequestEvent(
        object_kind="merge_request",
        user=UserInfo(id=1, name="Test", username="testuser"),
        project=ProjectInfo(
            id=42,
            name="test",
            path_with_namespace="group/test",
            web_url="https://gitlab.test/group/test",
        ),
        object_attributes=MRObjectAttributes(
            id=100,
            iid=10,
            title="Test MR",
            action="open",
        ),
    )


async def test_worker_processes_task_and_completes():
    """Worker should deserialize event, call orchestrator, and complete the task."""
    event = _mr_event()
    task = create_task(event)
    shutdown_event = asyncio.Event()

    mock_queue = AsyncMock()

    async def claim_side_effect(*args, **kwargs):
        if not mock_queue.claim._claimed:
            mock_queue.claim._claimed = True
            return task
        # After processing, give the shutdown a moment then yield
        await asyncio.sleep(0.05)
        return None

    mock_queue.claim = AsyncMock(side_effect=claim_side_effect)
    mock_queue.claim._claimed = False
    mock_queue.complete = AsyncMock(side_effect=lambda t: shutdown_event.set())
    mock_queue.fail = AsyncMock()

    with patch("forge.worker.app.Orchestrator") as MockOrchestrator:
        mock_orch_instance = MockOrchestrator.return_value
        mock_orch_instance.handle_event = AsyncMock()

        await run_worker(
            "test-worker",
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            mock_queue,
            shutdown_event,
        )

    mock_orch_instance.handle_event.assert_called_once()
    mock_queue.complete.assert_called_once_with(task)
    mock_queue.fail.assert_not_called()


async def test_worker_handles_failure():
    """Worker should call queue.fail when orchestrator raises."""
    event = _mr_event()
    task = create_task(event)
    shutdown_event = asyncio.Event()

    mock_queue = AsyncMock()

    async def claim_side_effect(*args, **kwargs):
        if not mock_queue.claim._claimed:
            mock_queue.claim._claimed = True
            return task
        await asyncio.sleep(0.05)
        return None

    mock_queue.claim = AsyncMock(side_effect=claim_side_effect)
    mock_queue.claim._claimed = False
    mock_queue.complete = AsyncMock()
    mock_queue.fail = AsyncMock(side_effect=lambda t, e: shutdown_event.set())

    with patch("forge.worker.app.Orchestrator") as MockOrchestrator:
        mock_orch_instance = MockOrchestrator.return_value
        mock_orch_instance.handle_event = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        await run_worker(
            "test-worker",
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
            mock_queue,
            shutdown_event,
        )

    mock_queue.fail.assert_called_once()
    mock_queue.complete.assert_not_called()
    fail_args = mock_queue.fail.call_args
    assert "LLM timeout" in fail_args[0][1]


async def test_worker_shutdown_on_signal():
    """Worker should exit cleanly when shutdown event is set."""
    mock_queue = AsyncMock()

    async def claim_yields(*args, **kwargs):
        await asyncio.sleep(0.05)
        return None

    mock_queue.claim = AsyncMock(side_effect=claim_yields)

    shutdown_event = asyncio.Event()
    shutdown_event.set()  # Immediate shutdown

    await run_worker(
        "test-worker",
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        mock_queue,
        shutdown_event,
    )
    # If we reach here, the worker exited cleanly
