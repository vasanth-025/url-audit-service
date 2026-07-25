import httpx


def test_successful_audit_extracts_fields(client, respx_mock):
    respx_mock.get("https://example.com/").respond(
        status_code=200,
        content=b"<html><head><title>Hello World</title></head></html>",
        headers={
            "content-type": "text/html; charset=utf-8",
            "strict-transport-security": "max-age=63072000",
        },
    )
    resp = client.post("/api/v1/audits", json={"url": "https://example.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status_code"] == 200
    assert body["title"] == "Hello World"
    assert body["has_hsts"] is True
    assert body["has_content_security_policy"] is False
    assert body["error"] is None
    assert body["response_time_ms"] >= 0


def test_repeat_request_is_served_from_cache(client, respx_mock):
    route = respx_mock.get("https://example.com/cached").respond(
        status_code=200, content=b"<html><title>Cached page</title></html>",
        headers={"content-type": "text/html"},
    )
    first = client.post("/api/v1/audits", json={"url": "https://example.com/cached"})
    second = client.post("/api/v1/audits", json={"url": "https://example.com/cached"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["title"] == "Cached page"
    # The target should only have been fetched once; the second response
    # came from cache.
    assert route.call_count == 1


def test_connection_error_reported_as_structured_failure(client, respx_mock):
    respx_mock.get("https://unreachable.example/").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    resp = client.post("/api/v1/audits", json={"url": "https://unreachable.example"})
    assert resp.status_code == 200  # request succeeded; audit result reports the failure
    body = resp.json()
    assert body["error"] is not None
    assert "connection_error" in body["error"]
    assert body["status_code"] is None


def test_timeout_reported_as_structured_failure(client, respx_mock):
    respx_mock.get("https://slow.example/").mock(side_effect=httpx.TimeoutException("timed out"))
    resp = client.post("/api/v1/audits", json={"url": "https://slow.example"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is not None
    assert "timeout" in body["error"]


def test_response_includes_request_id_header(client, respx_mock):
    respx_mock.get("https://example.com/").respond(status_code=200, content=b"<html></html>")
    resp = client.post("/api/v1/audits", json={"url": "https://example.com"})
    assert "x-request-id" in resp.headers


def test_incoming_request_id_is_propagated(client, respx_mock):
    respx_mock.get("https://example.com/").respond(status_code=200, content=b"<html></html>")
    resp = client.post(
        "/api/v1/audits",
        json={"url": "https://example.com"},
        headers={"x-request-id": "test-fixed-id-123"},
    )
    assert resp.headers["x-request-id"] == "test-fixed-id-123"
