"""PWA: Manifest, Service Worker, Icons, Offline-Seite."""
import json
import os

from fastapi.testclient import TestClient


def test_pwa_assets(env):
    from app import main
    os.environ["ADMIN_PASSWORD"] = "geheim123"
    with TestClient(main.app) as c:
        m = c.get("/manifest.webmanifest")
        assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
        data = json.loads(m.text)
        assert data["display"] == "standalone" and data["start_url"].startswith("/")
        assert any(i["purpose"] == "maskable" for i in data["icons"])
        for icon in data["icons"]:
            assert c.get(icon["src"]).status_code == 200, icon["src"]
        sw = c.get("/sw.js")
        assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
        assert main.VERSION in sw.text and sw.headers["service-worker-allowed"] == "/"
        for path in ["/offline", "/static/style.css?v=" + main.VERSION, "/static/logo.svg", "/static/icons/favicon-32.png"]:
            assert c.get(path).status_code == 200, path
        assert c.get("/favicon.ico").status_code == 200
        home = c.get("/").text
        assert 'rel="manifest"' in home and "serviceWorker" in home and 'class="brand-logo"' in home
