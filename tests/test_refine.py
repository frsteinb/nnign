"""Verdichtung des Historie-Abrufs und Datenlage je Jahr."""
import json
from datetime import date, datetime, timedelta, timezone

from tests.conftest import rss, setup_world


def test_full_search_window_is_split(env):
    from app import config, db, pipeline, refine
    setup_world(env)
    pipeline.run_scan("initial")
    config.set("collect_since", (date.today() - timedelta(days=400)).isoformat())
    pipeline.sync_period()
    items = [dict(title=f"VW Meldung Nummer {i} zum Konzern", link=f"https://news.google.com/rss/articles/x{i}",
                  src="http://localhost/", pub_name="Testmedium B", date=datetime.now(timezone.utc) - timedelta(days=200))
             for i in range(refine.SEARCH_CAP + 5)]
    env.routes["/gnews"] = lambda p: (rss(items), "application/rss+xml")
    t = db.query_one("SELECT * FROM backfill_tasks WHERE method='search' AND status='offen' ORDER BY period_start LIMIT 1")
    with db.tx() as conn:
        conn.execute("UPDATE backfill_tasks SET status='übersprungen' WHERE id!=? AND status='offen'", (t["id"],))
    http = __import__("app.fetcher", fromlist=["x"]).Http()
    pipeline.backfill_batch(http)
    subs = db.query("SELECT * FROM backfill_tasks WHERE method='search' AND depth=1")
    assert len(subs) == 2
    assert subs[0]["period_start"] == t["period_start"] and subs[1]["period_end"] == t["period_end"]
    assert all(s["priority"] >= refine.BASE_PRIORITY for s in subs)


def test_wayback_gaps_are_filled(env):
    from app import config, db, pipeline
    setup_world(env)
    config.set("collect_since", "2015-01-01")
    pipeline.run_scan("initial")
    feed = f"{env.base}/feeds/a.rss"
    # Tages-Schnappschüsse im Januar 2016 und am 1. Februar
    days = ["20160101", "20160110", "20160115", "20160120", "20160131", "20160201"]
    env.routes["/cdx/search/cdx"] = lambda p: (json.dumps([["timestamp", "original"]] + [[d + "120000", feed] for d in days]).encode(),
                                               "application/json")

    def snap(path):
        d = datetime.strptime(path.split("/web/")[1][:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        return rss([dict(title=f"VW Meldung vom {d:%d.%m.%Y}", link=f"{env.base}/a/{d:%Y%m%d}", desc="Volkswagen",
                         date=d)]), "application/rss+xml"
    env.routes["/web/*"] = snap
    config.set("backfill_wayback_granularity", "month")
    with db.tx() as conn:
        conn.execute("DELETE FROM backfill_tasks")
        conn.execute("INSERT INTO backfill_tasks(source_id, method, target, period_start, period_end, priority) "
                     "SELECT id, 'wayback_index', ?, '0001-01-01', '0001-01-01', -1 FROM sources WHERE name='Testmedium A'",
                     (feed,))
    http = __import__("app.fetcher", fromlist=["x"]).Http()
    for _ in range(8):
        pipeline.backfill_batch(http)
    done = [r["period_start"] for r in db.query("SELECT period_start FROM backfill_tasks WHERE method='wayback_feed' "
                                                 "AND status='ok' ORDER BY period_start")]
    assert "2016-01-01" in done and len(done) >= 4           # Lücken im Januar mit weiteren Schnappschüssen gefüllt
    assert db.query_one("SELECT COUNT(*) n FROM wayback_snaps")["n"] == 6


def test_coverage_by_year_and_overview(env):
    from fastapi.testclient import TestClient
    from app import config, db, main, maintenance
    config.set("collect_since", "2020-01-01")
    now = db.utcnow()
    with db.tx() as conn:
        for y, n in ((2021, 2), (2024, 40), (2025, 50)):
            for i in range(n):
                conn.execute("INSERT INTO articles(url, title, title_key, summary, source_name, source_domain, published_at, "
                             "fetched_at, via, relevant) VALUES(?,?,?,?,?,?,?,?,?,1)",
                             (f"https://x.de/{y}/{i}", f"VW {y} {i}", f"k{y}{i}", "", "X", "x.de", f"{y}-05-01T10:00:00+00:00",
                              now, "suche"))
        conn.execute("INSERT INTO backfill_tasks(source_id, method, target, period_start, period_end) VALUES(NULL,'search','u',"
                     "'2021-03-01','2021-05-30')")
    cov = {c["year"]: c for c in maintenance.coverage_by_year()}
    assert cov[2025]["level"] == "gut" and cov[2021]["level"] == "dünn" and cov[2021]["growing"]
    assert cov[2022]["articles"] == 0
    with TestClient(main.app) as c:
        page = c.get("/").text
        assert 'class="cov dünn growing"' in page and "nur dünn erfasst" in page and "ergänzt noch" in page


def test_fineness_per_source_and_method():
    from app import pipeline
    f = pipeline._fineness
    assert f({"method": "search", "ok": 5, "win_min": 3, "win_avg": 40.2, "depth_max": 5, "queries": 9}) == \
        "feinstes Fenster 3 Tage, Ø 40 Tage · 5× halbiert · 9 Suchanfragen"
    assert f({"method": "archive", "ok": 9, "win_min": 1, "win_avg": 1}) == "feinstes Fenster 1 Tag, Ø 1 Tag"
    assert f({"method": "wayback_feed", "ok": 10, "earliest": "2020-01-01", "latest": "2020-01-30", "days_ok": 10}) == \
        "10 Schnappschuss-Tage · Ø alle 3 Tage"
    assert f({"method": "hub_page", "ok": 2, "depth_max": 3}) == "bis Seite 4"
    assert f({"method": "search", "ok": 0}) == "–"
