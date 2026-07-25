def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_landing_page_has_required_footer_credit(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Built for Digital Heroes Training Task" in resp.text
    assert 'href="https://digitalheroesco.com"' in resp.text


def test_architecture_page_renders_doc_diagram_and_footer(client):
    resp = client.get("/architecture")
    assert resp.status_code == 200
    assert "/static/architecture-diagram.svg" in resp.text
    assert "Technology decision record" in resp.text
    assert "Built for Digital Heroes Training Task" in resp.text


def test_architecture_diagram_svg_is_served(client):
    resp = client.get("/static/architecture-diagram.svg")
    assert resp.status_code == 200
    assert "svg" in resp.headers["content-type"]
