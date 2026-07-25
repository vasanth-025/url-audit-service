from __future__ import annotations

import re
import time

import httpx

from app.config import Settings
from app.models import AuditResult

_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_MAX_BODY_BYTES = 1_000_000  # cap how much of the response body we read/inspect


async def run_audit(url: str, settings: Settings, client: httpx.AsyncClient) -> AuditResult:
    """Fetch `url` under strict timeouts and extract a small set of audit signals.

    Never raises for expected failure modes (timeout, connection error, bad
    status) -- those are reported in AuditResult.error so callers can persist
    a structured failure rather than handling exceptions at every call site.
    """
    timeout = httpx.Timeout(
        connect=settings.fetch_connect_timeout_seconds,
        read=settings.fetch_read_timeout_seconds,
        write=settings.fetch_read_timeout_seconds,
        pool=settings.fetch_connect_timeout_seconds,
    )

    start = time.perf_counter()
    try:
        async with client.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) >= _MAX_BODY_BYTES:
                    break
            elapsed_ms = (time.perf_counter() - start) * 1000

            title = None
            match = _TITLE_RE.search(body)
            if match:
                title = match.group(1).decode("utf-8", errors="replace").strip()[:200]

            headers = response.headers
            return AuditResult(
                url=url,
                status_code=response.status_code,
                response_time_ms=round(elapsed_ms, 2),
                content_type=headers.get("content-type"),
                content_length_bytes=len(body),
                title=title,
                has_hsts="strict-transport-security" in headers,
                has_content_security_policy="content-security-policy" in headers,
            )
    except httpx.TimeoutException:
        return AuditResult(url=url, error="timeout: target did not respond in time")
    except httpx.ConnectError as exc:
        return AuditResult(url=url, error=f"connection_error: {exc}")
    except httpx.HTTPError as exc:
        return AuditResult(url=url, error=f"http_error: {exc}")
