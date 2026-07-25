from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    redis_url: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    # Caching: how long a completed audit result is considered fresh enough
    # to serve without re-fetching the target URL. Configurable per deliverable spec.
    cache_ttl_seconds: int = _env_int("CACHE_TTL_SECONDS", 300)

    # Outbound HTTP timeouts when fetching the audited URL.
    fetch_connect_timeout_seconds: float = _env_float("FETCH_CONNECT_TIMEOUT_SECONDS", 3.0)
    fetch_read_timeout_seconds: float = _env_float("FETCH_READ_TIMEOUT_SECONDS", 5.0)

    # How many audits this instance will run concurrently. Bounds resource
    # usage under the 500-concurrent-request burst scenario.
    max_concurrent_audits: int = _env_int("MAX_CONCURRENT_AUDITS", 50)

    # Per-client rate limit (token bucket): requests allowed per window.
    rate_limit_requests: int = _env_int("RATE_LIMIT_REQUESTS", 60)
    rate_limit_window_seconds: int = _env_int("RATE_LIMIT_WINDOW_SECONDS", 60)

    log_level: str = os.environ.get("LOG_LEVEL", "INFO")


settings = Settings()
