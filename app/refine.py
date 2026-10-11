"""Verdichtung des Historie-Abrufs: Lücken automatisch schließen.

Zwei Quellen begrenzen die Historie:
- Die Nachrichtensuche liefert je Anfrage höchstens rund 100 Treffer. Ist ein Zeitfenster „voll“, fehlen
  wahrscheinlich Meldungen – das Fenster wird halbiert und beide Hälften werden erneut abgefragt (bis auf
  wenige Tage herunter).
- Ein Wayback-Schnappschuss eines Feeds enthält nur die letzten Meldungen (oft nur ein bis zwei Tage). Zwischen
  monatlichen Schnappschüssen bleiben Lücken. Ist eine Lücke größer als zwei Tage und gibt es dazwischen weitere
  Schnappschüsse, wird der mittlere davon zusätzlich abgerufen (Halbierung, begrenzt auf einige Stufen).
Die Verfeinerungen laufen nach der Grundplanung (niedrigere Priorität) im normalen Historie-Abruf mit.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import config, db, fetcher

VERSION = 1
SEARCH_CAP = 80            # ab so vielen Treffern gilt ein Suchfenster als voll
MIN_WINDOW_DAYS = 3
MAX_SEARCH_DEPTH = 6       # 90 → 45 → 22 → 11 → 5 → 3 Tage
WAYBACK_GAP_DAYS = 2
BASE_PRIORITY = 6          # nach der Grundplanung (0..5)


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def wayback_depth() -> int:
    return max(0, int(config.get("backfill_wayback_refine_depth")))


def search_splits(task, n_items: int, src) -> list[tuple]:
    """Volles Suchfenster halbieren."""
    if task["method"] != "search" or n_items < SEARCH_CAP or task["depth"] >= MAX_SEARCH_DEPTH:
        return []
    start, end = _d(task["period_start"]), _d(task["period_end"])
    if (end - start).days + 1 < MIN_WINDOW_DAYS * 2:
        return []
    from .pipeline import _query_for
    mid = start + (end - start) // 2
    out = []
    for a, b in ((start, mid), (mid + timedelta(days=1), end)):
        url = fetcher.search_url(_query_for(src), src["domain"], after=a - timedelta(days=1),
                                 before=b + timedelta(days=1), lang=fetcher.src_lang(src))
        out.append(("search", url, a, b, BASE_PRIORITY + task["depth"], task["depth"] + 1))
    return out


def _feed_of(target: str) -> str:
    return target.split("id_/", 1)[1] if "id_/" in target else target


def _snap_between(conn, feed: str, lo: date, hi: date) -> str | None:
    """Schnappschuss strikt zwischen lo und hi, möglichst in der Mitte."""
    if (hi - lo).days < 2:
        return None
    mid = lo + (hi - lo) / 2
    rows = conn.execute("SELECT ts FROM wayback_snaps WHERE feed=? AND ts>? AND ts<? ", (
        feed, (lo + timedelta(days=1)).strftime("%Y%m%d"), hi.strftime("%Y%m%d"))).fetchall()
    if not rows:
        return None
    return min((r["ts"] for r in rows), key=lambda ts: abs((datetime.strptime(ts[:8], "%Y%m%d").date() - mid).days))


def wayback_gaps(conn, task, covers_from: date) -> list[tuple]:
    """Lücken vor und nach einem Schnappschuss mit weiteren Schnappschüssen füllen."""
    if task["method"] != "wayback_feed" or task["depth"] >= wayback_depth():
        return []
    feed = _feed_of(task["target"])
    me = _d(task["period_start"])
    like = "%id_/" + feed
    prev = conn.execute("SELECT period_start FROM backfill_tasks WHERE method='wayback_feed' AND target LIKE ? "
                        "AND period_start<? ORDER BY period_start DESC LIMIT 1", (like, task["period_start"])).fetchone()
    nxt = conn.execute("SELECT period_start, covers_from FROM backfill_tasks WHERE method='wayback_feed' AND target LIKE ? "
                       "AND period_start>? ORDER BY period_start LIMIT 1", (like, task["period_start"])).fetchone()
    out = []
    if prev and (covers_from - _d(prev["period_start"])).days > WAYBACK_GAP_DAYS:
        ts = _snap_between(conn, feed, _d(prev["period_start"]), covers_from)
        if ts:
            out.append(ts)
    if nxt and nxt["covers_from"] and (_d(nxt["covers_from"]) - me).days > WAYBACK_GAP_DAYS:
        ts = _snap_between(conn, feed, me, _d(nxt["covers_from"]))
        if ts:
            out.append(ts)
    return [("wayback_feed", fetcher.wayback_snapshot_url(ts, feed), datetime.strptime(ts[:8], "%Y%m%d").date(),
             datetime.strptime(ts[:8], "%Y%m%d").date(), BASE_PRIORITY + task["depth"], task["depth"] + 1)
            for ts in out]


def sample(snaps: list[tuple[str, str]], granularity: str) -> list[tuple[str, str]]:
    """Aus allen Tages-Schnappschüssen je Woche bzw. Monat einen auswählen (Grundplanung)."""
    seen, out = set(), []
    for ts, original in snaps:
        d = datetime.strptime(ts[:8], "%Y%m%d").date()
        key = {"day": d, "week": d.isocalendar()[:2], "month": (d.year, d.month)}.get(granularity, (d.year, d.month))
        if key in seen:
            continue
        seen.add(key)
        out.append((ts, original))
    return out


def legacy_plan() -> dict:
    """Einmalig für bestehende Daten: volle Suchfenster verfeinern, Wayback-Indizes für Lückenfüllung neu laden."""
    from .sources import all_sources
    srcs = {s["id"]: s for s in all_sources()}
    n_search = n_index = 0
    with db.tx() as conn:
        for t in conn.execute("SELECT * FROM backfill_tasks WHERE method='search' AND status='ok' AND items>=?",
                              (SEARCH_CAP,)).fetchall():
            src = srcs.get(t["source_id"])
            if not src:
                continue
            for m, target, a, b, prio, depth in search_splits(t, t["items"], src):
                n_search += conn.execute("INSERT OR IGNORE INTO backfill_tasks(source_id, method, target, period_start, "
                                         "period_end, priority, depth) VALUES(?,?,?,?,?,?,?)",
                                         (t["source_id"], m, target, a.isoformat(), b.isoformat(), prio, depth)).rowcount
        if wayback_depth():
            for t in conn.execute("SELECT * FROM backfill_tasks WHERE method='wayback_index' AND status='ok'").fetchall():
                feed = t["target"].split("#", 1)[0]
                if conn.execute("SELECT 1 FROM wayback_snaps WHERE feed=? LIMIT 1", (feed,)).fetchone():
                    continue
                n_index += conn.execute("INSERT OR IGNORE INTO backfill_tasks(source_id, method, target, period_start, "
                                        "period_end, priority) VALUES(?,?,?,?,?,?)",
                                        (t["source_id"], "wayback_index", feed + "#lueckenfuellung", "0001-01-01",
                                         "0001-01-01", -1)).rowcount
    return {"search": n_search, "index": n_index}


def refine_existing_wayback(conn, feed: str, source_id: int) -> int:
    """Nach dem Laden eines Tages-Index: Lücken zwischen bereits abgerufenen Schnappschüssen planen."""
    rows = conn.execute("SELECT * FROM backfill_tasks WHERE method='wayback_feed' AND status='ok' AND target LIKE ? "
                        "ORDER BY period_start", ("%id_/" + feed,)).fetchall()
    n = 0
    for t in rows:
        cov = t["covers_from"]
        if not cov:   # ältere Aufgaben: frühestes bekanntes Datum aus gespeicherten Wayback-Meldungen schätzen
            prev = conn.execute("SELECT MAX(period_start) p FROM backfill_tasks WHERE method='wayback_feed' AND target LIKE ? "
                                "AND period_start<?", ("%id_/" + feed, t["period_start"])).fetchone()["p"]
            row = conn.execute("SELECT MIN(published_at) m FROM articles WHERE via='wayback' AND published_at<=? AND "
                               "published_at>? AND source_domain=(SELECT domain FROM sources WHERE id=?)",
                               (t["period_start"] + "T23:59:59", prev or "0000", source_id)).fetchone()["m"]
            cov = (row or t["period_start"])[:10]
            conn.execute("UPDATE backfill_tasks SET covers_from=? WHERE id=?", (cov, t["id"]))
        for m, target, a, b, prio, depth in wayback_gaps(conn, t, _d(cov)):
            n += conn.execute("INSERT OR IGNORE INTO backfill_tasks(source_id, method, target, period_start, period_end, "
                              "priority, depth) VALUES(?,?,?,?,?,?,?)",
                              (source_id, m, target, a.isoformat(), b.isoformat(), prio, depth)).rowcount
    return n
