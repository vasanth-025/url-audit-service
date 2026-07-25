def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_landing_page_has_required_footer_credit(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Built for Digital Heroes Training Task" in resp.text
    assert 'href="https://digitalheroesco.com"' in resp.text
