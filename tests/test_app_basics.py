import pytest
import app as app_module


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    app_module._RATE.clear()
    return app_module.app.test_client()


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_security_headers_on_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_robots_manifest_favicon(client):
    assert "Disallow: /api/" in client.get("/robots.txt").get_data(as_text=True)
    m = client.get("/manifest.webmanifest").get_json()
    assert m["lang"] == "zh-TW" and m["icons"]
    assert client.get("/favicon.ico").status_code == 200


def test_rate_limit_returns_429(client, monkeypatch):
    import protect
    monkeypatch.setattr(protect, "RATE_LIMIT", 3)
    protect._rate.clear()
    codes = [client.get("/api/market/inst-streak").status_code for _ in range(5)]
    assert 429 in codes[3:]


def test_dashboard_has_theme_and_guide(client):
    html = client.get("/").get_data(as_text=True)
    for needle in ('id="theme-btn"', 'id="page-guide"', 'data-theme', 'rel="manifest"'):
        assert needle in html
