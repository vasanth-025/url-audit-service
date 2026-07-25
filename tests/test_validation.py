def test_rejects_missing_scheme(client):
    resp = client.post("/api/v1/audits", json={"url": "example.com"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "request_id" in body


def test_rejects_non_http_scheme(client):
    resp = client.post("/api/v1/audits", json={"url": "ftp://example.com/file"})
    assert resp.status_code == 422


def test_rejects_empty_url(client):
    resp = client.post("/api/v1/audits", json={"url": "   "})
    assert resp.status_code == 422


def test_rejects_oversized_url(client):
    long_url = "https://example.com/" + ("a" * 3000)
    resp = client.post("/api/v1/audits", json={"url": long_url})
    assert resp.status_code == 422


def test_rejects_missing_body_field(client):
    resp = client.post("/api/v1/audits", json={})
    assert resp.status_code == 422


def test_accepts_valid_https_url_shape(client, respx_mock):
    respx_mock.get("https://example.com/").respond(
        status_code=200, content=b"<html><title>Example</title></html>",
        headers={"content-type": "text/html"},
    )
    resp = client.post("/api/v1/audits", json={"url": "https://example.com"})
    assert resp.status_code == 200
