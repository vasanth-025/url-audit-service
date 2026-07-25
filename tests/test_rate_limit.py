import pytest

from app.rate_limit import InMemoryRateLimitBackend, RateLimiter


@pytest.mark.asyncio
async def test_allows_requests_under_limit():
    limiter = RateLimiter(InMemoryRateLimitBackend(), limit=3, window_seconds=60)
    for _ in range(3):
        result = await limiter.check("client-a")
        assert result.allowed is True


@pytest.mark.asyncio
async def test_blocks_requests_over_limit():
    limiter = RateLimiter(InMemoryRateLimitBackend(), limit=2, window_seconds=60)
    await limiter.check("client-b")
    await limiter.check("client-b")
    result = await limiter.check("client-b")
    assert result.allowed is False
    assert result.retry_after_seconds == 60


@pytest.mark.asyncio
async def test_limits_are_independent_per_client():
    limiter = RateLimiter(InMemoryRateLimitBackend(), limit=1, window_seconds=60)
    result_a = await limiter.check("client-c")
    result_b = await limiter.check("client-d")
    assert result_a.allowed is True
    assert result_b.allowed is True


def test_api_returns_429_with_structured_body_when_limited(client, respx_mock):
    respx_mock.get("https://example.com/").respond(status_code=200, content=b"<html></html>")

    # Tighten the limit for this test so we can trigger it deterministically
    # without depending on the service's configured default.
    client.app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitBackend(), limit=1, window_seconds=60
    )

    first = client.post("/api/v1/audits", json={"url": "https://example.com"})
    second = client.post("/api/v1/audits", json={"url": "https://example.com"})

    assert first.status_code == 200
    assert second.status_code == 429
    body = second.json()
    assert body["error"]["code"] == "http_error"
    assert "request_id" in body
