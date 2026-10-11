from fastapi.testclient import TestClient

from tests.conftest import setup_world


def test_initial_scan_clusters_and_filters(env):
    from app import config, db, pipeline
    setup_world(env)
    config.set("initial_recent_days", 14)
    run_id = pipeline.run_scan("initial")
    assert run_id
    run = db.query_one("SELECT * FROM runs WHERE id=?", (run_id,))
    assert run["status"] == "ok", run["log"]

    titles = [r["title"] for r in db.query("SELECT title FROM topics")]
    assert not any("Wolfsburg gewinnt" in t for t in titles)        # Ausschlussliste
    assert not any("BMW" in t for t in titles)                      # irrelevant
    assert not any("Fremdes Medium" in t for t in titles)           # Suchtreffer anderer Domain
    assert any("Porsche hebt Prognose an" == t for t in titles)     # Suchtreffer, Mediensuffix entfernt

    # Gleiche Geschichte aus zwei Medien -> ein Thema mit zwei Quellen
    t = db.query_one("SELECT * FROM topics WHERE title LIKE '%35.000%'")
    assert t["article_count"] == 2, titles
    assert t["sentiment"] == "negativ"
    assert t["abstract"]
    srcs = {r["source_name"] for r in db.query("SELECT source_name FROM articles WHERE topic_id=?", (t["id"],))}
    assert srcs == {"Testmedium A", "Testmedium B"}
    # Erstes Datum = früheste Meldung
    first = db.query_one("SELECT MIN(published_at) m FROM articles WHERE topic_id=?", (t["id"],))["m"]
    assert t["first_published_at"] == first

    # Tracking-Parameter entfernt
    assert db.query_one("SELECT 1 FROM articles WHERE url LIKE '%audi-q6' ")
    assert config.state_get("initial_done")

    # Zweiter Scan: nichts Neues
    n_topics = db.query_one("SELECT COUNT(*) n FROM topics")["n"]
    rid = pipeline.run_scan("regulär")
    run = db.query_one("SELECT * FROM runs WHERE id=?", (rid,))
    assert run["new_articles"] == 0 and run["new_topics"] == 0
    assert db.query_one("SELECT COUNT(*) n FROM topics")["n"] == n_topics


def test_backfill_archive_and_wayback(env):
    from app import config, db, fetcher, pipeline
    setup_world(env)
    config.set("backfill_since", "2015-09-18")
    config.set("backfill_until", "2015-09-24")
    pipeline.run_scan("initial")
    progress = pipeline.backfill_progress()
    assert progress["total"] > 0
    http = fetcher.Http()
    for _ in range(20):
        if not pipeline.backfill_batch(http):
            break
    progress = pipeline.backfill_progress()
    assert progress["counts"].get("offen", 0) == 0, progress

    arch = db.query_one("SELECT * FROM articles WHERE url LIKE '%/art/vw-abgas.html'")
    assert arch is not None
    assert "manipuliert" in arch["summary"]                 # Details aus Meta-Tags geladen
    assert arch["published_at"].startswith("2015-09-20")
    assert not db.query_one("SELECT 1 FROM articles WHERE url LIKE '%fremd.example%'")

    w = db.query_one("SELECT * FROM articles WHERE title LIKE '%Winterkorn%'")
    assert w is not None and w["via"] == "wayback" and w["published_at"].startswith("2015-09-23")
    # Archiv- und Wayback-Meldung zur Manipulation landen im selben Thema
    a2 = db.query_one("SELECT * FROM articles WHERE url LIKE '%abgas-usa'")
    assert a2["topic_id"] == arch["topic_id"]
    t = db.query_one("SELECT * FROM topics WHERE id=?", (arch["topic_id"],))
    assert t["sentiment"] == "negativ"
    assert t["first_published_at"].startswith("2015-09-20")

    # Planen ist idempotent
    assert pipeline.plan_backfill() == 0


def test_breaker_stops_broken_archive(env):
    from app import config, db, fetcher, pipeline
    setup_world(env)
    with db.tx() as conn:
        conn.execute("UPDATE sources SET archive_template=?, archive_since='' WHERE name='Testmedium A'",
                     (f"{env.base}/gibtsnicht/{{YYYY}}{{MM}}{{DD}}",))
    config.set("backfill_since", "2010-01-01")
    config.set("backfill_until", "2010-03-31")
    config.set("backfill_wayback", False)
    config.set("backfill_wayback_fallback", False)
    config.set("search_enabled", False)
    pipeline.run_scan("initial")
    http = fetcher.Http()
    for _ in range(30):
        if not pipeline.backfill_batch(http):
            break
    counts = pipeline.backfill_progress()["counts"]
    assert counts.get("übersprungen", 0) > 0, counts
    assert counts.get("offen", 0) == 0
    assert pipeline.reset_backfill() > 0


def test_web_and_admin(env):
    from app import config, db, main, pipeline
    setup_world(env)
    pipeline.run_scan("initial")
    import os
    os.environ["ADMIN_PASSWORD"] = "geheim123"
    try:
        with TestClient(main.app) as c:
            r = c.get("/")
            assert r.status_code == 200 and "35.000" in r.text and "Entwicklung seit" in r.text
            assert c.get("/?sentiment=positiv&year=2026&q=Audi").status_code == 200
            tid = db.query_one("SELECT id FROM topics LIMIT 1")["id"]
            assert c.get(f"/thema/{tid}").status_code == 200
            api = c.get("/api/topics").json()
            assert api["total"] >= 2 and {"kurzthema", "abstract", "bewertung", "erstes_datum", "quellen"} <= set(api["topics"][0])
            assert c.get("/api/status").json()["topics"] >= 2
            assert c.get("/healthz").json()["status"] == "ok"
            csv = c.get("/export.csv")
            assert csv.status_code == 200 and "kurzthema" in csv.text

            assert c.get("/admin").status_code == 401
            auth = ("admin", "geheim123")
            r = c.get("/admin", auth=auth)
            assert r.status_code == 200 and "Einstellungen" in r.text
            r = c.post("/admin/settings", auth=auth, data={"scan_interval_hours": "6", "keywords_exclude": "Bundesliga"},
                       follow_redirects=False)
            assert r.status_code == 303
            assert config.get("scan_interval_hours") == 6.0
            assert config.get("keywords_exclude") == ["Bundesliga"]
            # CSRF: fremde Herkunft abgelehnt
            r = c.post("/admin/scan", auth=auth, headers={"origin": "https://evil.example"})
            assert r.status_code == 403
            # Thema bearbeiten -> gesperrt
            r = c.post(f"/admin/topic/{tid}", auth=auth, data={"title": "Neu", "abstract": "x", "sentiment": "positiv"},
                       follow_redirects=False)
            assert r.status_code == 303
            t = db.query_one("SELECT * FROM topics WHERE id=?", (tid,))
            assert t["locked"] == 1 and t["sentiment_origin"] == "manuell"
            # Quelle anlegen
            r = c.post("/admin/sources", auth=auth, data={"name": "X", "domain": "www.x.de", "enabled": "on"},
                       follow_redirects=False)
            assert r.status_code == 303 and db.query_one("SELECT 1 FROM sources WHERE domain='x.de'")
            config.set("public_dashboard", False)
            assert c.get("/").status_code == 401
            assert c.get("/", auth=auth).status_code == 200
    finally:
        os.environ.pop("ADMIN_PASSWORD", None)
