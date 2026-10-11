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
        for needle in ("Status der Arbeiten", "Themenbildung (automatisch)", "Bewertung nach Regeln", "Lernendes Modell",
                       "Entscheidungsmodell (Zero-Shot)", "KI (Sprachmodell)", "Historie (Archive", "Was bedeuten die Zustände",
                       'class="subnav"', "Einstellung suchen", 'class="setgroup"', "admin.js"):
            assert needle in page, needle
        pipeline.STATUS["regroup"] = {"done": 50, "total": 200}
        config.state_set("pending_reclassify", True)
        try:
            r = c.get("/fragment/admin-status")
            assert r.headers["X-Busy"] == "1"
            assert "50 von 200 Meldungen" in r.text and "width: 25%" in r.text
            assert 'class="state queued"' in r.text and "Bewertungen werden neu berechnet" in r.text
        finally:
            pipeline.STATUS["regroup"] = None
        pipeline.reclassify()
        config.state_set("pending_reclassify", False)
        t = c.get("/fragment/admin-status").text
        assert "gerade eben" in t
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
        assert "Erfassungszeitraum vollständig abgerufen" in t and "1 fehlgeschlagen" in t
        assert "fehlgeschlagene wiederholen" in t and ">Anhalten<" not in t
    finally:
        c.__exit__(None, None, None)


def test_sources_table_and_toggle(env):
    from app import db, pipeline
    setup_world(env)
    pipeline.run_scan("initial")
    c = _admin()
    try:
        page = c.get("/admin").text
        assert 'class="list sources"' in page and "Testmedium A" in page and 'data-toggle-row="src-' in page
        sid = db.query_one("SELECT id FROM sources WHERE name='Testmedium A'")["id"]
        d = c.post(f"/admin/sources/{sid}/toggle", headers=FETCH).json()
        assert "deaktiviert" in d["msg"] and d["reload"]
        assert db.query_one("SELECT enabled FROM sources WHERE id=?", (sid,))["enabled"] == 0
        c.post(f"/admin/sources/{sid}/toggle", headers=FETCH)
        assert db.query_one("SELECT enabled FROM sources WHERE id=?", (sid,))["enabled"] == 1
    finally:
        c.__exit__(None, None, None)


def test_backends_section(env):
    from app import fetcher, maintenance, pipeline
    setup_world(env)
    pipeline.upgrade_on_start({})
    pipeline.run_scan("initial")
    fetcher._track("search", True)
    fetcher._stats["search"]["last_ok"] = "2020-01-01T00:00:00+00:00"    # Erfolg liegt vor dem Limit
    fetcher._track("search", False, 429, "HTTP 429 für news.google.com")
    rows = {r["key"]: r for r in maintenance.backends()}
    assert {"be-web", "be-search", "be-wayback", "be-llm-primary", "be-zeroshot", "be-translate"} <= set(rows)
    assert rows["be-search"]["state"] == "limit" and "1× Limit" in rows["be-search"]["numbers"]
    assert rows["be-wayback"]["state"] == "ready" and "erfolgreich" in rows["be-wayback"]["numbers"]
    assert rows["be-llm-primary"]["state"] == "setup"
    fetcher._track("search", True)                  # einzelner Erfolg danach: Limit bleibt 30 Min. sichtbar
    assert {r["key"]: r for r in maintenance.backends()}["be-search"]["state"] == "limit"
    assert {j["key"]: j for j in maintenance.jobs()}["scan"]["state"] == "limit"
    fetcher._stats["search"]["last_limit"] = "2020-01-01T00:00:00+00:00"
    assert {r["key"]: r for r in maintenance.backends()}["be-search"]["state"] == "ready"
    from app import llm
    from tests.test_maintenance import GOOD, _answer, _openai_mock
    _openai_mock(env, lambda b: _answer(GOOD))
    llm._record("ok")
    assert {r["key"]: r for r in maintenance.backends()}["be-llm-primary"]["state"] == "ready"
    llm._record("limit", "Quota exceeded", 429)                      # Pause schon vorbei, aber kein Erfolg seitdem
    row = {r["key"]: r for r in maintenance.backends()}["be-llm-primary"]
    assert row["state"] == "limit" and row["state_label"] == "Limit erreicht"
    c = _admin()
    try:
        t = c.get("/fragment/admin-status").text
        assert "Abrufwege und Dienste" in t and "Internet Archive (Wayback)" in t and "Hugging Face" in t
        assert "Zweck: Bewertung per Entscheidungsmodell" in t
    finally:
        c.__exit__(None, None, None)
