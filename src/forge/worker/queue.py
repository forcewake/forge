from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from forge.utils.redis_client import RedisManager
from forge.worker.retry import RetryPolicy

logger = logging.getLogger(__name__)

# Redis key names
PENDING_KEY = "forge:tasks:pending"
PROCESSING_KEY = "forge:tasks:processing"
DEAD_KEY = "forge:tasks:dead"
DEDUP_PREFIX = "forge:dedup:"


@dataclass
class Task:
    """A unit of work to be processed by a worker."""

    task_id: str
    event_type: str  # object_kind value (merge_request, note, pipeline, etc.)
    event_data: dict  # Pydantic model_dump(mode="json") output
    priority: int = 0  # Lower = higher priority (-1 = high, 0 = normal, 1 = low)
    created_at: float = field(default_factory=time.time)
    attempt: int = 0
    max_retries: int = 3
    task_type: str = "event"  # "event" or "flow_step"
    metadata: dict = field(default_factory=dict)  # Extra data (e.g. flow_id, step_index)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> Task:
        return cls(**json.loads(data))

    @property
    def score(self) -> float:
        """Sorted set score: priority bucket first, then FIFO by creation time."""
        return self.priority * 1e12 + self.created_at


class TaskQueue:
    """Redis sorted-set based task queue with processing tracking."""

    def __init__(self, redis: RedisManager, retry_policy: RetryPolicy | None = None) -> None:
        self.redis = redis
        self.retry_policy = retry_policy or RetryPolicy()

    async def submit(self, task: Task) -> None:
        """Add a task to the pending queue."""
        await self.redis.zadd(PENDING_KEY, {task.to_json(): task.score})
        logger.debug("Task submitted: %s (priority=%d)", task.task_id, task.priority)

    async def claim(self, worker_id: str, timeout: float = 5.0) -> Task | None:
        """Block-pop the highest-priority task and move it to the processing set.

        Returns the claimed Task or None if the timeout expires.
        """
        result = await self.redis.bzpopmin(PENDING_KEY, timeout=timeout)
        if result is None:
            return None

        _key, member, _score = result
        task = Task.from_json(member)

        # Track in processing set, scored by claim time for stale detection
        await self.redis.zadd(PROCESSING_KEY, {member: time.time()})
        logger.debug("Task claimed by %s: %s (attempt=%d)", worker_id, task.task_id, task.attempt)
        return task

    async def complete(self, task: Task) -> None:
        """Mark a task as completed — remove from processing set."""
        await self.redis.zrem(PROCESSING_KEY, task.to_json())
        await self.redis.incr_stat("processed")
        logger.debug("Task completed: %s", task.task_id)

    async def fail(self, task: Task, error: str) -> None:
        """Handle task failure: retry or move to dead letter queue."""
        # Remove from processing
        await self.redis.zrem(PROCESSING_KEY, task.to_json())

        if self.retry_policy.should_retry(task.attempt, error):
            # Re-enqueue with incremented attempt
            task.attempt += 1
            await self.redis.zadd(PENDING_KEY, {task.to_json(): task.score})
            delay = self.retry_policy.compute_delay(task.attempt)
            logger.warning(
                "Task %s failed (attempt %d/%d), re-enqueued (delay hint: %.1fs): %s",
                task.task_id,
                task.attempt,
                task.max_retries,
                delay,
                error,
            )
        else:
            # Permanently failed — move to dead letter queue
            dead_entry = json.dumps(
                {
                    "task": json.loads(task.to_json()),
                    "error": error,
                    "failed_at": time.time(),
                }
            )
            await self.redis.lpush(DEAD_KEY, dead_entry)
            await self.redis.incr_stat("failed")
            logger.error(
                "Task %s moved to dead letter queue after %d attempts: %s",
                task.task_id,
                task.attempt,
                error,
            )

    async def requeue_stale(self, max_age_seconds: float = 300) -> int:
        """Requeue tasks stuck in processing (worker crashed).

        Returns the number of tasks requeued.
        """
        cutoff = time.time() - max_age_seconds
        stale_members = await self.redis.zrangebyscore(PROCESSING_KEY, 0, cutoff, withscores=True)
        if not stale_members:
            return 0

        count = 0
        for member, _score in stale_members:
            task = Task.from_json(member)
            await self.redis.zrem(PROCESSING_KEY, member)
            task.attempt += 1
            if task.attempt <= task.max_retries:
                await self.redis.zadd(PENDING_KEY, {task.to_json(): task.score})
                logger.warning("Requeued stale task: %s (attempt %d)", task.task_id, task.attempt)
                count += 1
            else:
                dead_entry = json.dumps(
                    {
                        "task": json.loads(task.to_json()),
                        "error": "worker_crash_max_retries",
                        "failed_at": time.time(),
                    }
                )
                await self.redis.lpush(DEAD_KEY, dead_entry)
                logger.error("Stale task %s exceeded max retries — moved to DLQ", task.task_id)
        return count

    async def depth(self) -> int:
        """Number of tasks in the pending queue."""
        return await self.redis.zcard(PENDING_KEY)

    async def dlq_depth(self) -> int:
        """Number of tasks in the dead letter queue."""
        return await self.redis.llen(DEAD_KEY)

    async def is_duplicate(self, fingerprint: str, ttl: int = 300) -> bool:
        """Check if an event fingerprint was recently seen (5-minute window).

        Returns True if this is a duplicate (key already existed).
        """
        key = f"{DEDUP_PREFIX}{fingerprint}"
        was_set = await self.redis.set_nx(key, "1", ex=ttl)
        return not was_set  # True if key already existed
