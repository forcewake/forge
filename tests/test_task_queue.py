import time

import fakeredis
import pytest

from forge.utils.redis_client import RedisManager
from forge.worker.queue import DEAD_KEY, PENDING_KEY, PROCESSING_KEY, Task, TaskQueue
from forge.worker.retry import RetryPolicy


@pytest.fixture()
def fake_redis():
    """Create a RedisManager backed by fakeredis."""
    manager = RedisManager.__new__(RedisManager)
    server = fakeredis.FakeServer()
    manager._pool = None  # not used with fakeredis
    manager._client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    return manager


@pytest.fixture()
def queue(fake_redis):
    return TaskQueue(fake_redis, RetryPolicy(max_retries=3))


def _make_task(priority: int = 0, task_id: str = "test-task", **kwargs) -> Task:
    return Task(
        task_id=task_id,
        event_type="merge_request",
        event_data={"object_kind": "merge_request", "project": {"id": 1}},
        priority=priority,
        **kwargs,
    )


async def test_submit_and_claim_round_trip(queue):
    task = _make_task(task_id="rt-1")
    await queue.submit(task)

    claimed = await queue.claim("worker-1", timeout=1.0)
    assert claimed is not None
    assert claimed.task_id == "rt-1"
    assert claimed.event_type == "merge_request"
    assert claimed.event_data["object_kind"] == "merge_request"


async def test_claim_timeout_returns_none(queue):
    result = await queue.claim("worker-1", timeout=0.1)
    assert result is None


async def test_complete_removes_from_processing(queue, fake_redis):
    task = _make_task(task_id="comp-1")
    await queue.submit(task)
    claimed = await queue.claim("worker-1", timeout=1.0)
    assert claimed is not None

    # Verify task is in processing
    count = await fake_redis.zcard(PROCESSING_KEY)
    assert count == 1

    await queue.complete(claimed)

    # Processing set should be empty
    count = await fake_redis.zcard(PROCESSING_KEY)
    assert count == 0


async def test_priority_ordering(queue):
    """High-priority (-1) task should be claimed before normal (0) and low (1)."""
    low = _make_task(priority=1, task_id="low")
    normal = _make_task(priority=0, task_id="normal")
    high = _make_task(priority=-1, task_id="high")

    # Submit in reverse order
    await queue.submit(low)
    await queue.submit(normal)
    await queue.submit(high)

    first = await queue.claim("w", timeout=1.0)
    second = await queue.claim("w", timeout=1.0)
    third = await queue.claim("w", timeout=1.0)

    assert first is not None and first.task_id == "high"
    assert second is not None and second.task_id == "normal"
    assert third is not None and third.task_id == "low"


async def test_fifo_within_same_priority(queue):
    """Tasks with the same priority should be claimed in FIFO order."""
    t1 = Task(
        task_id="first",
        event_type="note",
        event_data={},
        priority=0,
        created_at=1000.0,
    )
    t2 = Task(
        task_id="second",
        event_type="note",
        event_data={},
        priority=0,
        created_at=1001.0,
    )

    await queue.submit(t1)
    await queue.submit(t2)

    first = await queue.claim("w", timeout=1.0)
    second = await queue.claim("w", timeout=1.0)

    assert first is not None and first.task_id == "first"
    assert second is not None and second.task_id == "second"


async def test_fail_requeues_within_max_retries(queue, fake_redis):
    task = _make_task(task_id="retry-1")
    task.attempt = 0
    await queue.submit(task)

    claimed = await queue.claim("w", timeout=1.0)
    assert claimed is not None

    await queue.fail(claimed, "ConnectionError: timeout")

    # Should be back in pending with attempt=1
    pending_count = await fake_redis.zcard(PENDING_KEY)
    assert pending_count == 1

    reclaimed = await queue.claim("w", timeout=1.0)
    assert reclaimed is not None
    assert reclaimed.attempt == 1


async def test_fail_moves_to_dlq_after_max_retries(queue, fake_redis):
    task = _make_task(task_id="dlq-1")
    task.attempt = 3  # At max
    task.max_retries = 3
    await queue.submit(task)

    claimed = await queue.claim("w", timeout=1.0)
    assert claimed is not None

    await queue.fail(claimed, "RuntimeError: permanent failure")

    # Should not be in pending
    pending_count = await fake_redis.zcard(PENDING_KEY)
    assert pending_count == 0

    # Should be in DLQ
    dlq_count = await fake_redis.llen(DEAD_KEY)
    assert dlq_count == 1


async def test_fail_non_retryable_goes_to_dlq(queue, fake_redis):
    task = _make_task(task_id="nonretry-1")
    task.attempt = 0  # First attempt
    await queue.submit(task)

    claimed = await queue.claim("w", timeout=1.0)
    assert claimed is not None

    await queue.fail(claimed, "ValidationError: bad payload")

    # Non-retryable error should go straight to DLQ
    pending_count = await fake_redis.zcard(PENDING_KEY)
    assert pending_count == 0
    dlq_count = await fake_redis.llen(DEAD_KEY)
    assert dlq_count == 1


async def test_requeue_stale_tasks(queue, fake_redis):
    task = _make_task(task_id="stale-1")

    # Directly place in processing with an old timestamp
    old_time = time.time() - 600  # 10 minutes ago
    await fake_redis.zadd(PROCESSING_KEY, {task.to_json(): old_time})

    count = await queue.requeue_stale(max_age_seconds=300)
    assert count == 1

    # Should be back in pending
    pending_count = await fake_redis.zcard(PENDING_KEY)
    assert pending_count == 1

    # Processing should be empty
    processing_count = await fake_redis.zcard(PROCESSING_KEY)
    assert processing_count == 0


async def test_depth_reporting(queue):
    assert await queue.depth() == 0
    assert await queue.dlq_depth() == 0

    await queue.submit(_make_task(task_id="d1"))
    await queue.submit(_make_task(task_id="d2"))

    assert await queue.depth() == 2
    assert await queue.dlq_depth() == 0


async def test_is_duplicate(queue):
    assert await queue.is_duplicate("fp-abc") is False
    assert await queue.is_duplicate("fp-abc") is True  # Second time = duplicate


async def test_different_fingerprints_not_duplicate(queue):
    assert await queue.is_duplicate("fp-one") is False
    assert await queue.is_duplicate("fp-two") is False  # Different fingerprint
