from __future__ import annotations

import hashlib
import json
from typing import Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

from app.models import AuditResult


def normalize_url(url: str) -> str:
    """Normalize a URL so trivially-equivalent requests share a cache key.

    Lowercases scheme/host, drops a trailing slash on a bare path, and
    strips URL fragments (which never affect server-side responses).
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def cache_key(url: str) -> str:
    normalized = normalize_url(url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"audit:result:{digest}"


class CacheBackend(Protocol):
    async def get(self, key: str) -> Optional[str]: ...
    async def set(self, key: str, value: str, ex: int) -> None: ...


class RedisCacheBackend:
    """Production cache backend. Shared across all API pods so a cache
    entry written by one pod is visible to every other pod."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def get(self, key: str) -> Optional[str]:
        return await self._redis.get(key)

    async def set(self, key: str, value: str, ex: int) -> None:
        await self._redis.set(key, value, ex=ex)


class InMemoryCacheBackend:
    """Fallback/test backend with the same interface as the Redis client.

    Not for production use across multiple instances (state is per-process),
    but keeps the service and its tests runnable without a live Redis.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int) -> None:
        # `ex` (TTL) is intentionally not enforced here; this backend only
        # exists for environments without Redis (tests, local dev).
        self._store[key] = value


class AuditResultCache:
    def __init__(self, backend: CacheBackend, ttl_seconds: int):
        self.backend = backend
        self.ttl_seconds = ttl_seconds

    async def get(self, url: str) -> Optional[AuditResult]:
        raw = await self.backend.get(cache_key(url))
        if raw is None:
            return None
        return AuditResult.model_validate_json(raw)

    async def set(self, url: str, result: AuditResult) -> None:
        await self.backend.set(
            cache_key(url), result.model_dump_json(), ex=self.ttl_seconds
        )
