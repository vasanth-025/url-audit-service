from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.audit import run_audit
from app.cache import AuditResultCache, InMemoryCacheBackend, RedisCacheBackend
from app.config import settings
from app.logging_config import configure_logging, get_logger, log_extra, request_id_ctx
from app.models import AuditRequest, AuditResult, ErrorDetail, ErrorResponse
from app.rate_limit import InMemoryRateLimitBackend, RateLimiter, RedisRateLimitBackend

configure_logging(settings.log_level)
logger = get_logger("audit_service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Try Redis first (the production backend, shared across all pods).
    # Fall back to in-memory backends if Redis isn't reachable so the
    # service still runs for local development and the test suite without
    # requiring a live Redis instance.
    cache_backend: object
    rate_limit_backend: object
    try:
        redis_client = redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2
        )
        await redis_client.ping()
        cache_backend = RedisCacheBackend(redis_client)
        rate_limit_backend = RedisRateLimitBackend(redis_client)
        app.state.redis_client = redis_client
        logger.info("connected to redis", extra=log_extra(redis_url=settings.redis_url))
    except Exception as exc:
        cache_backend = InMemoryCacheBackend()
        rate_limit_backend = InMemoryRateLimitBackend()
        app.state.redis_client = None
        logger.warning(
            "redis unavailable, using in-memory backends (not safe for multi-pod deployments)",
            extra=log_extra(error=str(exc)),
        )

    app.state.cache = AuditResultCache(
        backend=cache_backend, ttl_seconds=settings.cache_ttl_seconds
    )
    app.state.rate_limiter = RateLimiter(
        backend=rate_limit_backend,
        limit=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )
    app.state.audit_semaphore = asyncio.Semaphore(settings.max_concurrent_audits)
    app.state.http_client = httpx.AsyncClient()
    logger.info("service starting", extra=log_extra(
        cache_ttl_seconds=settings.cache_ttl_seconds,
        max_concurrent_audits=settings.max_concurrent_audits,
        rate_limit=f"{settings.rate_limit_requests}/{settings.rate_limit_window_seconds}s",
    ))
    yield
    await app.state.http_client.aclose()
    if app.state.redis_client is not None:
        await app.state.redis_client.aclose()
    logger.info("service shutting down")


app = FastAPI(title="URL Audit Service", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware: request ID + structured access logging
# ---------------------------------------------------------------------------
@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    incoming_id = request.headers.get("x-request-id")
    request_id = incoming_id or str(uuid.uuid4())
    token = request_id_ctx.set(request_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["x-request-id"] = request_id
    logger.info(
        "request completed",
        extra=log_extra(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        ),
    )
    return response


# ---------------------------------------------------------------------------
# Structured error responses
# ---------------------------------------------------------------------------
def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(
        request_id=request_id_ctx.get(),
        error=ErrorDetail(code=code, message=message),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    # Raised by FastAPI when the request body fails pydantic validation
    # (including our AuditRequest.validate_url field validator).
    return _error_response(422, "validation_error", str(exc))


@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: ValidationError):
    return _error_response(422, "validation_error", str(exc))


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return _error_response(exc.status_code, "http_error", str(exc.detail))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("unhandled exception", exc_info=exc, extra=log_extra(
        request_id=request_id_ctx.get(),
    ))
    return _error_response(500, "internal_error", "An unexpected error occurred")


# ---------------------------------------------------------------------------
# Rate limiting dependency
# ---------------------------------------------------------------------------
async def enforce_rate_limit(request: Request) -> None:
    client_id = request.headers.get("x-api-key") or (
        request.client.host if request.client else "unknown"
    )
    result = await request.app.state.rate_limiter.check(client_id)
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail=f"rate limit exceeded: {result.limit} requests per "
                   f"{settings.rate_limit_window_seconds}s. Retry after "
                   f"{result.retry_after_seconds}s.",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/v1/audits",
    response_model=AuditResult,
    dependencies=[Depends(enforce_rate_limit)],
)
async def create_audit(payload: AuditRequest, request: Request) -> AuditResult:
    cache: AuditResultCache = request.app.state.cache
    semaphore: asyncio.Semaphore = request.app.state.audit_semaphore
    client: httpx.AsyncClient = request.app.state.http_client

    cached = await cache.get(payload.url)
    if cached is not None:
        logger.info("cache hit", extra=log_extra(url=payload.url))
        return cached

    # Bound how long a request will wait for a free concurrency slot before
    # failing fast with 503, rather than queueing indefinitely under a
    # sustained burst and blowing past the client-facing SLA.
    acquire_timeout = 2.0
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=acquire_timeout)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="service is at capacity, please retry shortly",
        )
    try:
        fetch_timeout = settings.fetch_connect_timeout_seconds + settings.fetch_read_timeout_seconds + 1
        result = await asyncio.wait_for(
            run_audit(payload.url, settings, client), timeout=fetch_timeout
        )
    except asyncio.TimeoutError:
        result = AuditResult(url=payload.url, error="timeout: audit exceeded time budget")
    finally:
        semaphore.release()

    if result.error is None:
        await cache.set(payload.url, result)
        logger.info(
            "audit completed",
            extra=log_extra(url=payload.url, status_code=result.status_code,
                             response_time_ms=result.response_time_ms),
        )
    else:
        logger.warning("audit failed", extra=log_extra(url=payload.url, error=result.error))

    return result
