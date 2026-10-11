"""Admin-Aktionen per fetch: Ergebnis als JSON statt Seitensprung; Live-Status mit Hintergrundarbeiten."""
import json
import os

from fastapi.testclient import TestClient

from tests.conftest import setup_world

FETCH = {"origin": "http://testserver", "x-requested-with": "fetch"}


def _admin():
    from app import main
    os.environ["ADMIN_PASSWORD"] = "geheim123"
    c = TestClient(main.app)
    c.__enter__()
    c.post("/login", data={"password": "geheim123"}, headers={"origin": "http://testserver"})
    return c


def test_actions_answer_inline(env):
    from tests.test_maintenance import GOOD, _answer, _openai_mock
    c = _admin()
    try:
        r = c.post("/admin/llm-test", headers=FETCH)
        d = r.json()
        assert d["ok"] is False and "Nicht nutzbar" in d["msg"] and d["reload"] is False
        _openai_mock(env, lambda b: _answer(GOOD))
        d = c.post("/admin/llm-test", headers=FETCH).json()
        assert d["ok"] is True and d["msg"].startswith("OK")
        env.routes["/v1/models"] = lambda p: (json.dumps({"data": [{"id": "m-flash"}, {"id": "m-embed"}]}).encode(),
                                              "application/json")
        d = c.post("/admin/llm-models", headers=FETCH).json()
        assert d["models"] == ["m-flash"] and d["target"] == "f-llm_model"
        d = c.post("/admin/translate-test", headers=FETCH).json()
        assert d["reload"] is False and "Fehlgeschlagen (KI): KI-Übersetzung unbrauchbar" in d["msg"]   # Grund sichtbar
        # gewöhnliche Aktion: Meldung + Neuladen statt Umleitung
        d = c.post("/admin/backfill/toggle", headers=FETCH).json()
        assert d["ok"] and d["reload"] is True and d["msg"]
        d = c.post("/admin/scan", headers=FETCH).json()
        assert d["reload"] is False and "Scan gestartet" in d["msg"]
        # Fehler kommen als JSON
        r = c.post("/admin/topics/merge", data={"ids": ["1"]}, headers=FETCH)
        assert r.status_code == 400 and r.json()["ok"] is False
        d = c.post("/admin/settings", data={"page_size": "abc"}, headers=FETCH).json()
        assert d["ok"] is False and "Ungültiger Wert" in d["msg"]
        # ohne fetch weiterhin Umleitung (Bedienung ohne JavaScript)
        r = c.post("/admin/scan", headers={"origin": "http://testserver"}, follow_redirects=False)
        assert r.status_code == 303
    finally:
        c.__exit__(None, None, None)


def test_status_shows_background_work(env):
    from app import config, pipeline
    setup_world(env)
    pipeline.upgrade_on_start({})
    pipeline.run_scan("initial")
    c = _admin()
    try:
        page = c.get("/admin").text
        for needle in ("Hintergrundarbeiten", "Themen-Bündelung", "Neuberechnung der Bewertungen", 'id="dienste"',
                       'class="subnav"', "Einstellung suchen", 'class="setgroup"', "admin.js"):
            assert needle in page, needle
        pipeline.STATUS["regroup"] = {"done": 50, "total": 200}
        config.state_set("pending_reclassify", True)
        try:
            r = c.get("/fragment/admin-status")
            assert r.headers["X-Busy"] == "1"
            assert "50 von 200 Meldungen" in r.text and "steht an" in r.text and "width: 25%" in r.text
        finally:
            pipeline.STATUS["regroup"] = None
        pipeline.reclassify()
        config.state_set("pending_reclassify", False)
        t = c.get("/fragment/admin-status").text
        assert "zuletzt" in t and "Meldungen" in t
        assert c.get("/").text.count("admin.js") == 1                    # Admin-Skript auch auf der Startseite
    finally:
        c.__exit__(None, None, None)
    with TestClient(__import__("app.main", fromlist=["x"]).app) as anon:
        assert "admin.js" not in anon.get("/").text
        assert anon.get("/fragment/admin-status").status_code == 401


def test_zero_values_survive_settings_form(env):
    import re
    from app import config
    config.set("host_delay_seconds", 0)
    c = _admin()
    try:
        page = c.get("/admin").text
        assert re.search(r'id="f-host_delay_seconds"[^>]*value="0', page)
        d = c.post("/admin/settings", data={"host_delay_seconds": "0", "page_size": "40"}, headers=FETCH).json()
        assert d["ok"] and config.get("page_size") == 40
    finally:
        c.__exit__(None, None, None)


def test_backfill_state_finished_vs_running(env):
    from app import config, db, pipeline
    setup_world(env)
    c = _admin()                                           # Start (plant ggf. weitere Abrufe) vorab
    pipeline.run_scan("initial")
    config.set("backfill_enabled", True)
    p = pipeline.backfill_progress()
    assert p["open"] and p["state"] == "wartet" and p["percent"] < 100
    with db.tx() as conn:                                  # bis auf eine Aufgabe erledigt
        first = conn.execute("SELECT MIN(id) i FROM backfill_tasks").fetchone()["i"]
        conn.execute("UPDATE backfill_tasks SET status='offen'")
        conn.execute("UPDATE backfill_tasks SET status='ok' WHERE id!=?", (first,))
    p = pipeline.backfill_progress()
    assert p["open"] == 1 and p["percent"] < 100 and p["state"] != "fertig"
    with db.tx() as conn:
        conn.execute("UPDATE backfill_tasks SET status='fehler' WHERE id=?", (first,))
    p = pipeline.backfill_progress()
    assert p["state"] == "fertig" and p["percent"] == 100 and p["failed"] == 1
    try:
        t = c.get("/fragment/admin-status").text
        assert "abgeschlossen" in t and "1 fehlgeschlagen" in t and "fehlgeschlagene wiederholen" in t
        assert "· läuft" not in t and "Anhalten" not in t
    finally:
        c.__exit__(None, None, None)
