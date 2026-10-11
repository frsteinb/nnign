"""Erfassungszeitraum, Datenverwaltung, Neu-Bewertung, Statistik, Doku und Prompt-Verwaltung."""
import os
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.conftest import NOW, setup_world

AUTH = ("admin", "geheim123")
TODAY = datetime.now(timezone.utc).date()


def _client():
    from app import main
    os.environ["ADMIN_PASSWORD"] = "geheim123"
    return TestClient(main.app)


def test_default_period_is_four_weeks_and_fixed(env):
    from app import config, pipeline
    pipeline.ensure_default_period()
    since, until = pipeline.period()
    assert since == TODAY - timedelta(days=28) and until is None
    assert config.source_of("collect_since") == "default"
    # Festgehalten: ein zweiter Aufruf ändert nichts
    config.state_set("default_collect_since", "2001-01-01")
    pipeline.ensure_default_period()
    assert pipeline.period()[0] == date(2001, 1, 1)


def test_items_outside_period_are_ignored(env):
    from app import config, db, pipeline
    from app.fetcher import Item
    config.set("collect_since", (TODAY - timedelta(days=10)).isoformat())
    old = Item("https://x.de/alt", "VW meldet Rekordabsatz", "", NOW - timedelta(days=40), "X", "x.de", "rss")
    new = Item("https://x.de/neu", "Audi stellt neuen A6 vor", "", NOW - timedelta(days=2), "X", "x.de", "rss")
    pipeline.ingest([old, new], pipeline.Ctx("t"), allow_llm=False)
    urls = {r["url"] for r in db.query("SELECT url FROM articles")}
    assert urls == {"https://x.de/neu"}


def test_apply_period_plans_stops_and_purges(env):
    from app import config, db, fetcher, pipeline
    setup_world(env)
    config.set("backfill_wayback", False)
    config.set("search_enabled", False)
    with db.tx() as conn:   # Archiv der Testquelle ohne eigenes Startdatum
        conn.execute("UPDATE sources SET archive_since=''")
    pipeline.apply_period(date(2015, 9, 18), date(2015, 9, 22))
    n = db.query_one("SELECT COUNT(*) n FROM backfill_tasks WHERE status='offen'")["n"]
    assert n == 5                                            # 5 Archivtage
    http = fetcher.Http()
    while pipeline.backfill_batch(http):
        pass
    assert db.query_one("SELECT 1 FROM articles WHERE url LIKE '%vw-abgas%'")
    # Zeitraum erweitern -> zusätzliche Tage werden eingeplant
    r = pipeline.apply_period(date(2015, 9, 15), date(2015, 9, 22))
    assert r["planned"] == 3
    # Zeitraum verkleinern -> offene Aufgaben davor gestoppt, Daten außerhalb gelöscht
    r = pipeline.apply_period(date(2015, 9, 21), date(2015, 9, 22), purge=True)
    assert r["stopped"] == 3 and r["articles"] >= 1
    assert not db.query_one("SELECT 1 FROM articles WHERE url LIKE '%vw-abgas%'")
    assert config.get("collect_since") == "2015-09-21"
    # wieder erweitern -> gestoppte Aufgaben werden reaktiviert
    r = pipeline.apply_period(date(2015, 9, 15), None)
    assert r["revived"] == 3


def test_delete_range_and_wipe(env):
    from app import config, db, pipeline
    setup_world(env)
    pipeline.run_scan("initial")
    assert db.query_one("SELECT COUNT(*) n FROM topics")["n"] >= 2
    r = pipeline.delete_range(TODAY - timedelta(days=1), None)
    assert r["articles"] >= 2
    left = db.query("SELECT published_at FROM articles WHERE relevant=1")
    assert all(a["published_at"] < (TODAY - timedelta(days=1)).isoformat() for a in left)
    config.set("scan_interval_hours", 6)
    pipeline.wipe()
    assert db.query_one("SELECT COUNT(*) n FROM articles")["n"] == 0
    assert not config.state_get("initial_done")
    assert config.get("scan_interval_hours") == 6.0           # Einstellungen bleiben
    assert db.query_one("SELECT COUNT(*) n FROM sources")["n"] == 2
    pipeline.wipe(factory=True)
    assert config.source_of("scan_interval_hours") == "default"
    assert db.query_one("SELECT COUNT(*) n FROM sources")["n"] == 15   # Standardquellen


def test_reclassify_and_neutral(env):
    from app import db, pipeline
    from app.fetcher import Item
    items = [Item("https://x.de/1", "VW-Chef besucht Messe in München", "", NOW, "X", "x.de", "rss"),
             Item("https://x.de/2", "Rückruf bei Audi wegen Software-Problemen", "", NOW, "X", "x.de", "rss")]
    pipeline.ingest(items, pipeline.Ctx("t"), allow_llm=False)
    labels = {r["title"][:8]: r["sentiment"] for r in db.query("SELECT title, sentiment FROM topics")}
    assert labels == {"VW-Chef ": "neutral", "Rückruf ": "negativ"}
    # Regel nachträglich ergänzen: „Messe“ positiv -> nach Neu-Bewertung positiv
    from app import config
    config.set("lexicon_extra", ["messe = 3"])
    r = pipeline.reclassify()
    assert r["articles"] == 2
    t = db.query_one("SELECT sentiment, sentiment_detail FROM topics WHERE title LIKE 'VW-Chef%'")
    assert t["sentiment"] == "positiv" and "Eigener Begriff" in t["sentiment_detail"]


def test_stats_doku_prompt_and_admin_forms(env):
    from app import config, db, llm, pipeline
    setup_world(env)
    pipeline.run_scan("initial")
    try:
        with _client() as c:
            st = c.get("/api/stats").json()
            assert st["topics"] == st["pos"] + st["neu"] + st["neg"] and st["media_count"] == 2
            assert st["pos_pct"] + st["neu_pct"] + st["neg_pct"] in (99, 100, 101)
            assert c.get("/api/stats?sentiment=negativ").json()["topics"] == st["neg"]
            home = c.get("/")
            assert "Datenbestand" in home.text and "% neutral" in home.text
            assert c.get("/?sentiment=neutral").status_code == 200
            tid = db.query_one("SELECT id FROM topics WHERE title LIKE '%35.000%'")["id"]
            page = c.get(f"/thema/{tid}").text
            assert "Warum diese Bewertung?" in page and "Stellenabbau" in page

            doc = c.get("/doku")
            assert doc.status_code == 200
            for needle in ("Hobbyprojekt", "Kriterien und die gewählten Annahmen", "System-Prompt",
                           "Antwortformat (JSON-Schema)", "chat/completions", "Rückruf", "NNIGN_COLLECT_SINCE"):
                assert needle in doc.text, needle

            # Prompt: unveränderter Standard wird nicht gespeichert, eigener Text schon
            from app.prompts import DEFAULT_INSTRUCTIONS
            c.post("/admin/settings", auth=AUTH, data={"llm_instructions": DEFAULT_INSTRUCTIONS})
            assert config.source_of("llm_instructions") == "default"
            c.post("/admin/settings", auth=AUTH, data={"llm_instructions": "Bewerte streng.\n1. relevant …"})
            assert config.source_of("llm_instructions") == "admin"
            msg = llm.build_user_message({"title": "VW"}, [])
            assert msg.startswith("Bewerte streng.") and "Daten:" in msg
            assert "Bewerte streng." in c.get("/doku").text
            c.post("/admin/settings/reset/llm_instructions", auth=AUTH)
            assert llm.instructions() == DEFAULT_INSTRUCTIONS

            # Datenverwaltung über das Formular
            r = c.post("/admin/data/period", auth=AUTH, data={"since": "2026-01-01", "until": ""},
                       follow_redirects=False)
            assert r.status_code == 303 and config.get("collect_since") == "2026-01-01"
            r = c.post("/admin/data/period", auth=AUTH, data={"since": "2026-05-01", "until": "2026-01-01"},
                       follow_redirects=False)
            assert "Enddatum" in r.headers["location"] or "Enddatum" in c.get(r.headers["location"], auth=AUTH).text
            r = c.post("/admin/data/wipe", auth=AUTH, data={"word": "falsch"}, follow_redirects=False)
            assert db.query_one("SELECT COUNT(*) n FROM topics")["n"] > 0     # ohne Bestätigung nichts gelöscht
            admin = c.get("/admin", auth=AUTH).text
            assert "Daten &amp; Erfassungszeitraum" in admin and "Neu initialisieren" in admin
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)


def test_settings_form_checkboxes(env):
    from app import config
    try:
        with _client() as c:
            c.post("/admin/settings", auth=AUTH, data={"scan_interval_hours": "5"})
            assert config.get("public_dashboard") is True            # nicht im Formular -> unverändert
            c.post("/admin/settings", auth=AUTH, data={"__bools": ["public_dashboard", "search_enabled"],
                                                       "search_enabled": "on"})
            assert config.get("public_dashboard") is False and config.get("search_enabled") is True
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
