import pytest

from app.cache import AuditResultCache, InMemoryCacheBackend, cache_key, normalize_url
from app.models import AuditResult


def test_normalize_url_lowercases_scheme_and_host():
    assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


def test_normalize_url_strips_trailing_slash():
    assert normalize_url("https://example.com/path/") == "https://example.com/path"


def test_normalize_url_keeps_root_slash():
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_normalize_url_strips_fragment():
    assert normalize_url("https://example.com/page#section") == "https://example.com/page"


def test_equivalent_urls_share_cache_key():
    assert cache_key("https://example.com") == cache_key("HTTPS://EXAMPLE.com/")


@pytest.mark.asyncio
async def test_cache_roundtrip():
    cache = AuditResultCache(backend=InMemoryCacheBackend(), ttl_seconds=60)
    result = AuditResult(url="https://example.com", status_code=200, response_time_ms=12.3)

    assert await cache.get("https://example.com") is None
    await cache.set("https://example.com", result)
    fetched = await cache.get("https://example.com")

    assert fetched is not None
    assert fetched.status_code == 200
    assert fetched.response_time_ms == 12.3
