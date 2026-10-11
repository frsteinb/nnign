"""Nachladen beim Scrollen und Live-Anzeige laufender Abrufe."""
import os

from fastapi.testclient import TestClient

from tests.conftest import setup_world


def _client():
    from app import main
    os.environ["ADMIN_PASSWORD"] = "geheim123"
    return TestClient(main.app)


def test_infinite_scroll_fragments(env):
    from app import config, db, pipeline
    setup_world(env)
    pipeline.run_scan("initial")
    n = db.query_one("SELECT COUNT(*) n FROM topics")["n"]
    assert n >= 3
    config.set("page_size", 1)
    with _client() as c:
        home = c.get("/").text
        assert 'id="more"' in home and "/fragment/topics?" in home and "Seite 1 von" not in home
        assert home.count('class="topic ') == 1
        ids, url = [], "/fragment/topics?order=new&page=2"
        while url:
            r = c.get(url)
            assert r.status_code == 200
            ids += [int(x.split('"')[0]) for x in r.text.split('data-id="')[1:]]
            url = r.headers["X-More"]
        assert len(ids) == n - 1 and len(set(ids)) == len(ids)
        # Filter werden mitgenommen
        r = c.get("/fragment/topics?sentiment=negativ&page=1")
        assert 'class="topic negativ"' in r.text and 'class="topic positiv"' not in r.text


def test_activity_live(env):
    from app import config, pipeline
    setup_world(env)
    with _client() as c:
        r = c.get("/fragment/activity")
        assert r.headers["X-Active"] == "1" and "Initial-Scan" in r.text       # vor dem ersten Scan
        pipeline.run_scan("initial")
        config.set("backfill_enabled", True)
        r = c.get("/fragment/activity")
        assert r.headers["X-Active"] == "1" and "Historie wird abgerufen" in r.text
        home = c.get("/").text
        assert 'id="statusline" hidden' in home and "Historie wird abgerufen" in home
        # laufender Scan mit Fortschritt
        pipeline.STATUS["scan"] = {"kind": "regulär", "phase": "Abruf der Quellen", "started": "2026-01-01T00:00:00+00:00",
                                   "sources_total": 4, "sources_done": 1, "fetched": 12}
        try:
            t = c.get("/fragment/activity").text
            assert "Aktualisierung" in t and "1 von 4 Quellen" in t and "25 %" in t
        finally:
            pipeline.STATUS["scan"] = None
        config.set("backfill_enabled", False)
        assert "Historie-Abruf angehalten" in c.get("/fragment/activity").text
        pipeline.reset_backfill(only_failed=False)
        with __import__("app.db", fromlist=["x"]).tx() as conn:
            conn.execute("UPDATE backfill_tasks SET status='ok'")
        r = c.get("/fragment/activity")
        assert r.headers["X-Active"] == "0" and r.text.strip() == ""
        home = c.get("/").text
        assert 'id="statusline" hidden' not in home and "Letzter Scan" in home
