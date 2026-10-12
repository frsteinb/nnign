"""Fortlaufender Lückenschluss bis zum Niveau der aktuellen Lage, mit Abschätzung von Dauer und Datenverkehr."""
from datetime import date, datetime, timedelta, timezone

from tests.conftest import setup_world


def _month(back: int) -> str:
    d = date.today().replace(day=1)
    for _ in range(back):
        d = (d - timedelta(days=1)).replace(day=1)
    return d.strftime("%Y-%m")


def _world(env):
    from app import config, db, pipeline
    setup_world(env)
    pipeline.run_scan("initial")
    config.set("collect_since", _month(8) + "-01")
    now = db.utcnow()
    with db.tx() as conn:
        for back, n in ((1, 30), (2, 30), (3, 30), (6, 2), (7, 10)):
            for i in range(n):
                conn.execute("INSERT INTO articles(url, title, title_key, summary, source_name, source_domain, published_at, "
                             "fetched_at, via, relevant) VALUES(?,?,?,?,?,?,?,?,?,1)",
                             (f"https://x.de/{back}/{i}", f"VW {back} {i}", f"k{back}-{i}", "", "X", "x.de",
                              f"{_month(back)}-10T10:00:00+00:00", now, "suche"))
        conn.execute("UPDATE backfill_tasks SET status='ok'")
    return f"{env.base}/feeds/a.rss"


def test_plan_targets_thinnest_month_with_spaced_wayback(env):
    from app import config, db, gapfill
    feed = _world(env)
    ms = gapfill.months_state()
    assert ms["target"] == 30 and len(ms["reference"]) == 3
    states = {m["month"]: m for m in ms["months"]}
    assert states[_month(6)]["state"] == "offen" and states[_month(6)]["target"] == 24     # 80 % von 30
    thin = _month(6)
    with db.tx() as conn:
        conn.executemany("INSERT INTO wayback_snaps(feed, ts) VALUES(?, ?)",
                         [(feed, f"{thin.replace('-', '')}{d:02d}120000") for d in range(1, 29)])
    config.set("search_enabled", False)
    n = gapfill.plan_round()
    rows = db.query("SELECT period_start FROM backfill_tasks WHERE method='wayback_feed' AND status='offen' "
                    "ORDER BY period_start")
    days = [date.fromisoformat(r["period_start"]) for r in rows]
    assert n == len(days) and 10 <= len(days) <= 14
    assert all((b - a).days >= gapfill.SPACING_DAYS for a, b in zip(days, days[1:]))
    assert all(d.strftime("%Y-%m") == thin for d in days)
    assert gapfill.plan_round() == 0 or db.query_one("SELECT COUNT(*) n FROM backfill_tasks WHERE method='wayback_feed'")[
        "n"] == n                                                     # dieselben Schnappschüsse nicht doppelt
    # nichts mehr zu holen: Monate gelten als ausgeschöpft
    with db.tx() as conn:
        conn.execute("UPDATE backfill_tasks SET status='ok'")
    config.set("backfill_wayback", False)
    assert gapfill.plan_round() == 0
    assert all(m["state"] != "offen" for m in gapfill.months_state()["months"])


def test_search_variants_and_query_kept_when_split(env):
    from app import config, db, gapfill, refine
    _world(env)
    config.set("backfill_wayback", False)
    config.set("search_query", "Volkswagen OR Audi OR Porsche")
    assert gapfill.plan_round() > 0
    rows = db.query("SELECT target, depth, priority FROM backfill_tasks WHERE status='offen' AND method='search'")
    queries = {refine.query_of(r["target"]) for r in rows}
    assert {"Volkswagen", "Audi", "Porsche"} <= queries
    assert all(r["depth"] == gapfill.SEARCH_DEPTH and r["priority"] >= gapfill.GAP_PRIORITY for r in rows)
    src = db.query_one("SELECT * FROM sources WHERE use_search=1 LIMIT 1")
    t = dict(db.query_one("SELECT * FROM backfill_tasks WHERE status='offen' AND method='search' AND source_id=? "
                          "ORDER BY period_start LIMIT 1", (src["id"],)))
    t["depth"] = 0
    subs = refine.search_splits(t, refine.SEARCH_CAP, src)
    assert subs and all(refine.query_of(s[1]) == refine.query_of(t["target"]) for s in subs)


def test_estimate_rates_and_status(env):
    from app import db, fetcher, gapfill, maintenance
    _world(env)
    gapfill.record_batch(48.0, 24, 2_400_000, 12, 1.0)
    r = gapfill.rate()
    assert r["sec_per_task"] == 2.0 and r["bytes_per_task"] == 100_000 and r["yield_per_task"] == 0.5
    est = gapfill.estimate(max_age=0)
    assert est["open_months"] >= 1 and est["tasks"] > 0
    assert est["seconds"] == est["tasks"] * 2.0 and est["bytes"] == est["tasks"] * 100_000
    row = {j["key"]: j for j in maintenance.jobs()}["gapfill"]
    assert "Monaten auf Zielniveau" in row["numbers"] and "Datenverkehr" in row["details"]
    assert gapfill.fmt_bytes(1_500_000) == "1,5 MB" and gapfill.fmt_duration(3 * 86400) == "etwa 3 Tage"
    http = fetcher.Http()
    http.get(f"{env.base}/feeds/a.rss")
    assert http.bytes > 0
    st = fetcher.backend_stats()
    assert sum(v.get("bytes_total", 0) for v in st.values()) >= http.bytes
    assert db.query_one("SELECT 1") is not None


def test_backfill_loop_hands_over_to_gapfill(env):
    from app import db, fetcher, gapfill, pipeline
    _world(env)
    http = fetcher.Http()
    assert pipeline.backfill_batch(http) == 0             # Grundplanung erledigt
    assert gapfill.plan_round() > 0                       # Lückenschluss plant weiter
    assert pipeline.backfill_batch(http) > 0
    assert (gapfill.rate()["tasks"]) > 0
    assert datetime.now(timezone.utc) and db.query_one("SELECT COUNT(*) n FROM backfill_tasks WHERE status='ok' AND "
                                                       "priority>=?", (gapfill.GAP_PRIORITY,))["n"] > 0
