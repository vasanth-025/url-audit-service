# URL Audit Service

A production-hardened service that audits a URL (fetches it, records status,
timing, headers, and title) with input validation, timeouts, concurrency
limits, per-client rate limiting, caching, and structured logging.

## Running locally

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

The service works without Redis (falls back to in-memory cache/rate-limit
backends, logged as a warning on startup) but for any multi-instance
deployment set `REDIS_URL` so cache entries and rate limits are shared
across pods:

```bash
export REDIS_URL=redis://localhost:6379/0
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Or with Docker:

```bash
docker build -t url-audit-service .
docker run -p 8000:8000 -e REDIS_URL=redis://host.docker.internal:6379/0 url-audit-service
```

## Configuration

All configuration is via environment variables (see `app/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `CACHE_TTL_SECONDS` | `300` | How long a completed audit result is served from cache before re-fetching |
| `FETCH_CONNECT_TIMEOUT_SECONDS` | `3.0` | Connect timeout for the outbound fetch of the audited URL |
| `FETCH_READ_TIMEOUT_SECONDS` | `5.0` | Read timeout for the outbound fetch |
| `MAX_CONCURRENT_AUDITS` | `50` | Max audits this instance runs concurrently |
| `RATE_LIMIT_REQUESTS` | `60` | Requests allowed per client per window |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window, in seconds |
| `LOG_LEVEL` | `INFO` | Log level |

## Running tests

```bash
pytest -v
```

CI (`.github/workflows/ci.yml`) runs the full suite on every push and pull
request against Python 3.11 and 3.12.

## API contract

### `POST /api/v1/audits`

Audits a URL. Serves from cache if the same normalized URL was audited
within `CACHE_TTL_SECONDS`; otherwise fetches it under a strict timeout
budget and caches the result.

**Request**

```json
{ "url": "https://example.com" }
```

`url` must be an absolute `http(s)` URL, non-empty, and under 2048 characters.

**Response — `200 OK`**

```json
{
  "url": "https://example.com",
  "status_code": 200,
  "response_time_ms": 87.42,
  "content_type": "text/html; charset=utf-8",
  "content_length_bytes": 1256,
  "title": "Example Domain",
  "has_hsts": true,
  "has_content_security_policy": false,
  "error": null
}
```

If the target URL couldn't be fetched (timeout, connection error, etc.),
the response is still `200 OK` — the audit itself succeeded, and the
failure to reach the target is data, not a service error:

```json
{
  "url": "https://unreachable.example",
  "status_code": null,
  "response_time_ms": null,
  "content_type": null,
  "content_length_bytes": null,
  "title": null,
  "has_hsts": null,
  "has_content_security_policy": null,
  "error": "timeout: target did not respond in time"
}
```

**Response — `422 Unprocessable Entity`** (invalid input)

```json
{
  "request_id": "b3f1...-uuid",
  "error": { "code": "validation_error", "message": "..." }
}
```

**Response — `429 Too Many Requests`** (rate limit exceeded, keyed by the
`x-api-key` header if present, else client IP)

```json
{
  "request_id": "b3f1...-uuid",
  "error": { "code": "http_error", "message": "rate limit exceeded: 60 requests per 60s. Retry after 60s." }
}
```

**Response — `503 Service Unavailable`** (at concurrency capacity; retry shortly)

Every response carries an `x-request-id` header (generated per request, or
echoed back if the caller supplies one) and the same ID appears in every
structured log line for that request, so a single request's path through
the service can be reconstructed from logs alone.

### `GET /health`

Liveness check.

```json
{ "status": "ok" }
```

### `GET /`

HTML landing page with links to docs, health check, and the architecture doc.

### `GET /architecture`

Renders the Task B scale-architecture document (`app/static/architecture.md`)
as an HTML page with its diagram, for the live-build submission requirement.

## Design notes

- **Caching** is keyed on a normalized form of the URL (lowercased
  scheme/host, trailing slash and fragment stripped) so trivially
  equivalent URLs share a cache entry.
- **Concurrency limiting** uses an `asyncio.Semaphore` sized by
  `MAX_CONCURRENT_AUDITS`; a request that can't acquire a slot within 2s
  fails fast with `503` rather than queueing indefinitely.
- **Rate limiting** is a fixed-window counter, chosen over a token bucket
  or sliding log for its simplicity and O(1) atomic Redis operation, at
  the cost of allowing a burst at window boundaries — an accepted
  tradeoff at this service's traffic profile.
- **Structured logging**: every log line is a single JSON object
  (`app/logging_config.py`) carrying the request ID via a `contextvars`
  context, so it's attached automatically without threading it through
  every function call.
