from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol


class RateLimitBackend(Protocol):
    async def incr_with_expiry(self, key: str, window_seconds: int) -> int: ...


class RedisRateLimitBackend:
    """Production rate-limit backend: enforces limits service-wide across
    all API pods, using a single atomic INCR plus a TTL set only on the
    window's first request."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def incr_with_expiry(self, key: str, window_seconds: int) -> int:
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, window_seconds)
        return count


class InMemoryRateLimitBackend:
    """Fixed-window counter kept in process memory.

    Same caveat as InMemoryCacheBackend: fine for tests/single-instance dev,
    not a substitute for Redis in a multi-pod deployment where limits must
    be enforced service-wide rather than per-pod.
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}  # key -> (count, window_start)

    async def incr_with_expiry(self, key: str, window_seconds: int) -> int:
        now = time.monotonic()
        count, window_start = self._counts.get(key, (0, now))
        if now - window_start >= window_seconds:
            count, window_start = 0, now
        count += 1
        self._counts[key] = (count, window_start)
        return count


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int = 0


class RateLimiter:
    """Simple fixed-window rate limiter, keyed per client.

    A fixed window is intentionally chosen over a token bucket here: it is
    trivial to reason about and implement atomically with a single INCR,
    and at this service's traffic profile (per-client limits, not global
    throughput shaping) the boundary-burst edge case of fixed windows is an
    acceptable tradeoff against the added complexity of a sliding log.
    """

    def __init__(self, backend: RateLimitBackend, limit: int, window_seconds: int):
        self.backend = backend
        self.limit = limit
        self.window_seconds = window_seconds

    async def check(self, client_id: str) -> RateLimitResult:
        key = f"ratelimit:{client_id}"
        count = await self.backend.incr_with_expiry(key, self.window_seconds)
        if count > self.limit:
            return RateLimitResult(
                allowed=False,
                limit=self.limit,
                remaining=0,
                retry_after_seconds=self.window_seconds,
            )
        return RateLimitResult(
            allowed=True, limit=self.limit, remaining=self.limit - count
        )
