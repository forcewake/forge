from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis

if TYPE_CHECKING:
    from forge.config import Settings

logger = logging.getLogger(__name__)


class RedisManager:
    """Manages a Redis connection pool and exposes queue/stats primitives."""

    def __init__(self, url: str) -> None:
        self._pool = aioredis.ConnectionPool.from_url(
            url, decode_responses=True, max_connections=20
        )
        self._client = aioredis.Redis(connection_pool=self._pool)

    @classmethod
    async def from_settings(cls, settings: Settings) -> RedisManager | None:
        """Create a RedisManager if REDIS_URL is configured, else return None."""
        if not settings.REDIS_URL:
            return None
        manager = cls(settings.REDIS_URL)
        # Verify connectivity
        try:
            await manager.ping()
            return manager
        except Exception:
            logger.error("Failed to connect to Redis at %s", settings.REDIS_URL)
            await manager.close()
            return None

    async def ping(self) -> bool:
        return await self._client.ping()

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        """Add members with scores to a sorted set."""
        return await self._client.zadd(key, mapping)  # type: ignore[return-value]

    async def bzpopmin(self, key: str, timeout: float = 5.0) -> tuple[str, str, float] | None:
        """Blocking pop of the member with the lowest score.

        Returns (key, member, score) or None on timeout.
        """
        result = await self._client.bzpopmin(key, timeout=timeout)  # type: ignore[arg-type]
        return result  # type: ignore[return-value]

    async def zrem(self, key: str, *members: str) -> int:
        return await self._client.zrem(key, *members)  # type: ignore[return-value]

    async def zcard(self, key: str) -> int:
        return await self._client.zcard(key)  # type: ignore[return-value]

    async def zrangebyscore(
        self, key: str, min_score: float, max_score: float, *, withscores: bool = False
    ) -> list[Any]:
        return await self._client.zrangebyscore(
            key, min=min_score, max=max_score, withscores=withscores
        )

    async def lpush(self, key: str, *values: str) -> int:
        return await self._client.lpush(key, *values)  # type: ignore[return-value]

    async def llen(self, key: str) -> int:
        return await self._client.llen(key)  # type: ignore[return-value]

    async def set_nx(self, key: str, value: str, ex: int) -> bool:
        """SET key value NX EX ttl. Returns True if set (not a duplicate)."""
        result = await self._client.set(key, value, nx=True, ex=ex)
        return result is not None

    async def set_ex(self, key: str, value: str, ex: int) -> None:
        """SET key value EX ttl (unconditional)."""
        await self._client.set(key, value, ex=ex)

    async def get(self, key: str) -> str | None:
        """GET a key's value."""
        return await self._client.get(key)  # type: ignore[return-value]

    async def delete(self, key: str) -> int:
        """DEL a key. Returns number of keys removed."""
        return await self._client.delete(key)  # type: ignore[return-value]

    async def scan_keys(self, pattern: str) -> list[str]:
        """Scan for keys matching a pattern. Use sparingly."""
        keys: list[str] = []
        async for key in self._client.scan_iter(match=pattern, count=100):
            keys.append(key)
        return keys

    async def incr_stat(self, name: str, ttl: int = 7200) -> int:
        """Increment an hourly-bucketed counter. TTL covers current + next hour."""
        bucket = f"forge:stats:{name}:{_hour_bucket()}"
        pipe = self._client.pipeline()
        pipe.incr(bucket)
        pipe.expire(bucket, ttl)
        results = await pipe.execute()
        return results[0]

    async def get_stat(self, name: str, hours: int = 1) -> int:
        """Sum counter values for the last N hour buckets."""
        now_bucket = _hour_bucket()
        total = 0
        for i in range(hours + 1):
            bucket = f"forge:stats:{name}:{now_bucket - i}"
            val = await self._client.get(bucket)
            if val is not None:
                total += int(val)
        return total

    async def close(self) -> None:
        await self._client.aclose()
        await self._pool.aclose()


def _hour_bucket() -> int:
    """Current hour as an integer bucket (hours since epoch)."""
    return int(time.time()) // 3600
