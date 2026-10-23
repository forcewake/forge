"""The worker processes durable command wake-ups and refuses retired tasks."""

import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from forge.worker.app import run_worker
from forge.worker.queue import Task
from forge.worker.tasks import create_run_command_task


async def drive(task, *, error=None):
    shutdown = asyncio.Event()
    queue = AsyncMock()
    queue.claim.return_value = task
    queue.complete.side_effect = lambda _: shutdown.set()
    queue.fail.side_effect = lambda *_: shutdown.set()
    with patch("forge.worker.app._execute_run_command_task", new_callable=AsyncMock) as execute:
        execute.side_effect = error
        await run_worker("worker", None, None, None, queue, shutdown)
    return queue, execute


async def test_worker_processes_task_and_completes():
    task = create_run_command_task({"command": "status", "project_id": 42}, note_id=7)
    queue, execute = await drive(task)
    execute.assert_awaited_once()
    assert execute.await_args.args[3] == task.metadata
    queue.complete.assert_awaited_once_with(task)
    queue.fail.assert_not_awaited()


async def test_worker_handles_failure():
    task = create_run_command_task({"command": "status", "project_id": 42}, note_id=7)
    queue, execute = await drive(task, error=RuntimeError("handler failed"))
    execute.assert_awaited_once()
    queue.complete.assert_not_awaited()
    queue.fail.assert_awaited_once_with(task, "handler failed")


async def test_worker_shutdown_on_signal():
    queue = AsyncMock()
    shutdown = asyncio.Event()
    shutdown.set()
    await run_worker("worker", None, None, None, queue, shutdown)
    queue.claim.assert_not_awaited()


@pytest.mark.parametrize("kind", ["event", "flow_step", "unknown"])
async def test_retired_tasks_fail_without_running_a_handler(kind):
    task = Task(task_id="retired", event_type="old", event_data={}, task_type=kind)
    queue, execute = await drive(task)
    execute.assert_not_awaited()
    queue.complete.assert_not_awaited()
    queue.fail.assert_awaited_once_with(task, "ValidationError: retired_task_type")
