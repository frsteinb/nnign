"""Fortlaufender Lückenschluss der Historie bis zum Niveau der aktuellen Lage.

Ist der reguläre Historie-Abruf (Grundplanung und Verdichtung) abgearbeitet, sucht nnign laufend die am
dünnsten erfassten Monate und plant für sie weitere Abrufe ein – Stufe für Stufe:
1. fehlgeschlagene Abrufe dieses Monats erneut versuchen (nach einem Tag Abstand),
2. weitere Wayback-Schnappschüsse der Feeds, sodass zwischen zwei abgerufenen höchstens zwei Tage liegen,
3. die Nachrichtensuche mit den Einzelbegriffen der Suchanfrage (Volkswagen, Audi, Porsche …) in Wochenfenstern;
   volle Fenster werden wie gewohnt weiter halbiert.
Ziel je Monat ist ein (einstellbarer) Anteil der Meldungsdichte der letzten vollständigen Monate. Ein Monat ist
fertig, wenn er das Ziel erreicht oder alle Wege ausgeschöpft sind (dann wird er nach 30 Tagen erneut geprüft).

Für die Abschätzung von Dauer und Datenverkehr misst der Historie-Abruf laufend Sekunden, Bytes und neue
Meldungen je Abruf (gleitender Mittelwert).
"""
from __future__ import annotations

import bisect
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone

from . import config, db, fetcher, refine

log = logging.getLogger("nnign.gapfill")

GAP_PRIORITY = 20            # nach Grundplanung (0..5) und Verdichtung (6..)
REFERENCE_MONTHS = 3
WINDOW_DAYS = 7
SPACING_DAYS = 2             # Abstand der Wayback-Schnappschüsse
MAX_ATTEMPTS = 4
EXHAUSTED_DAYS = 30
SEARCH_DEPTH = 3             # Wochenfenster werden bei Bedarf noch auf 3–4 Tage halbiert
ROUND_MONTHS = 3

_cache = {"at": 0.0, "data": None}


# ---------------------------------------------------------------------------------- Messwerte

def record_batch(seconds: float, tasks: int, nbytes: int, new_articles: int, refine_share: float) -> None:
    """Gleitende Mittelwerte je Abruf: Dauer, Datenverkehr und (bei Verdichtung/Lückenschluss) Ertrag."""
    if tasks <= 0:
        return
    m = config.state_get("backfill_rate") or {}
    a = 0.2 if m.get("tasks") else 1.0

    def ew(key, val):
        m[key] = val if key not in m else (1 - a) * m[key] + a * val
    ew("sec_per_task", seconds / tasks)
    ew("bytes_per_task", nbytes / tasks)
    if refine_share >= 0.5:
        ew("yield_per_task", new_articles / tasks)
    m["tasks"] = m.get("tasks", 0) + tasks
    m["bytes"] = m.get("bytes", 0) + nbytes
    m["at"] = db.utcnow()
    config.state_set("backfill_rate", m)


def rate() -> dict:
    m = config.state_get("backfill_rate") or {}
    return {"sec_per_task": m.get("sec_per_task") or 3.0, "bytes_per_task": m.get("bytes_per_task") or 60_000,
            "yield_per_task": m.get("yield_per_task"), "measured": bool(m.get("tasks")),
            "tasks": m.get("tasks", 0), "bytes": m.get("bytes", 0)}


def fmt_bytes(n: float) -> str:
    for unit, f in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= f:
            v = n / f
            return (f"{v:.1f}" if v < 10 else f"{v:.0f}").replace(".", ",") + " " + unit
    return f"{int(n)} B"


def fmt_duration(secs: float) -> str:
    if secs < 90:
        return "unter 2 Minuten"
    if secs < 5400:
        return f"etwa {round(secs / 60)} Minuten"
    if secs < 172800:
        return f"etwa {secs / 3600:.0f} Stunden"
    if secs < 60 * 86400:
        return f"etwa {secs / 86400:.0f} Tage"
    return f"etwa {secs / (30 * 86400):.0f} Monate"


# ---------------------------------------------------------------------------------- Monate und Ziel

def _months(a: date, b: date) -> list[str]:
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


def months_state() -> dict:
    """Ziel je Monat, Ist-Zahl und Zustand (erreicht / offen / ausgeschöpft) für alle Monate vor dem Referenzzeitraum."""
    from . import pipeline
    today = datetime.now(timezone.utc).date()
    since, until = pipeline.period()
    end = min(until or today, today)
    counts = {r["m"]: r["n"] for r in db.query(
        "SELECT substr(published_at,1,7) m, COUNT(*) n FROM articles WHERE relevant=1 GROUP BY m")}
    complete = [m for m in _months(since, end) if _month_bounds(m)[0] >= since and _month_bounds(m)[1] < end
                and m != today.strftime("%Y-%m")]
    ref = complete[-REFERENCE_MONTHS:]
    if not ref:
        return {"target": 0, "reference": [], "months": [], "pct": int(config.get("gapfill_target_percent"))}
    ref_counts = sorted(counts.get(m, 0) for m in ref)
    density = ref_counts[len(ref_counts) // 2]                       # Median der Referenzmonate
    pct = max(1, min(100, int(config.get("gapfill_target_percent"))))
    exhausted = config.state_get("gapfill_exhausted") or {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=EXHAUSTED_DAYS)).isoformat()
    months = []
    for m in _months(since, end):
        if m >= ref[0]:
            break
        a, b = _month_bounds(m)
        share = ((min(b, end) - max(a, since)).days + 1) / ((b - a).days + 1)
        target = math.ceil(density * pct / 100 * share)
        n = counts.get(m, 0)
        if n >= target:
            state = "erreicht"
        elif exhausted.get(m, "") > cutoff:
            state = "ausgeschöpft"
        else:
            state = "offen"
        months.append({"month": m, "articles": n, "target": target, "state": state,
                       "ratio": n / target if target else 1.0})
    return {"target": density, "reference": ref, "months": months, "pct": pct}


# ---------------------------------------------------------------------------------- Kandidaten

def _query_terms(src) -> list[str]:
    q = config.get("search_query_zh") if fetcher.src_lang(src) == "zh" else config.get("search_query")
    terms = [q]
    if fetcher.src_lang(src) != "zh" and config.get("include_partners"):
        terms.append(config.get("search_query_partners") or "")
    out = []
    for t in terms:
        for part in t.split(" OR "):
            part = part.strip().strip("()").strip()
            if part and part not in out:
                out.append(part)
    return out if len(out) > 1 else []                              # ein Begriff = schon abgefragt


class _World:
    """Einmal geladener Bestand: Quellen, Wayback-Schnappschüsse, vorhandene Aufgaben."""

    def __init__(self, conn):
        from .sources import all_sources, feeds_of
        self.sources = [s for s in all_sources(enabled_only=True)]
        self.feeds = {s["id"]: feeds_of(s) for s in self.sources}
        self.snaps: dict[str, list[str]] = {}
        for r in conn.execute("SELECT feed, ts FROM wayback_snaps ORDER BY feed, ts"):
            self.snaps.setdefault(r["feed"], []).append(r["ts"])
        self.targets = {r["target"] for r in conn.execute("SELECT target FROM backfill_tasks WHERE method IN "
                                                          "('wayback_feed','search')")}
        self.fetched: dict[str, list[date]] = {}
        for r in conn.execute("SELECT target, period_start FROM backfill_tasks WHERE method='wayback_feed'"):
            self.fetched.setdefault(refine._feed_of(r["target"]), []).append(date.fromisoformat(r["period_start"][:10]))
        for v in self.fetched.values():
            v.sort()

    def wayback(self, src, a: date, b: date) -> list[tuple]:
        if not config.get("backfill_wayback"):
            return []
        out = []
        for feed in self.feeds.get(src["id"], []):
            snaps = self.snaps.get(feed, [])
            lo = bisect.bisect_left(snaps, a.strftime("%Y%m%d"))
            hi = bisect.bisect_left(snaps, (b + timedelta(days=1)).strftime("%Y%m%d"))
            have = list(self.fetched.get(feed, []))
            for ts in snaps[lo:hi]:
                d = datetime.strptime(ts[:8], "%Y%m%d").date()
                i = bisect.bisect_left(have, d)
                near = [x for x in have[max(0, i - 1):i + 1]]
                if any(abs((d - x).days) < SPACING_DAYS for x in near):
                    continue
                url = fetcher.wayback_snapshot_url(ts, feed)
                if url in self.targets:
                    continue
                bisect.insort(have, d)
                out.append(("wayback_feed", url, d, d, GAP_PRIORITY, refine.wayback_depth() + 1))
        return out

    def search(self, src, a: date, b: date) -> list[tuple]:
        if not (config.get("search_enabled") and src["use_search"]):
            return []
        out = []
        for term in _query_terms(src):
            d = a
            while d <= b:
                e = min(d + timedelta(days=WINDOW_DAYS - 1), b)
                url = fetcher.search_url(term, src["domain"], after=d - timedelta(days=1), before=e + timedelta(days=1),
                                         lang=fetcher.src_lang(src))
                if url not in self.targets:
                    out.append(("search", url, d, e, GAP_PRIORITY + 1, SEARCH_DEPTH))
                d = e + timedelta(days=1)
        return out


def _bounds_in_period(month: str) -> tuple[date, date]:
    from . import pipeline
    today = datetime.now(timezone.utc).date()
    since, until = pipeline.period()
    a, b = _month_bounds(month)
    return max(a, since), min(b, until or today, today)


def _retries(conn, month: str, count_only: bool = False):
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    sql = ("FROM backfill_tasks WHERE status='fehler' AND attempts<? AND substr(period_start,1,7)=? "
           "AND (done_at IS NULL OR done_at<?)")
    if count_only:
        return conn.execute("SELECT COUNT(*) n " + sql, (MAX_ATTEMPTS, month, day_ago)).fetchone()["n"]
    return conn.execute("UPDATE backfill_tasks SET status='offen', error='' " + sql.replace("FROM backfill_tasks ", ""),
                        (MAX_ATTEMPTS, month, day_ago)).rowcount


# ---------------------------------------------------------------------------------- Planung

def plan_round() -> int:
    """Neue Abrufe für die dünnsten Monate einplanen (nur, wenn der übrige Historie-Abruf leer ist)."""
    if not config.get("gapfill_enabled") or not config.get("backfill_enabled"):
        return 0
    ms = months_state()
    todo = sorted((m for m in ms["months"] if m["state"] == "offen"), key=lambda m: m["ratio"])
    if not todo:
        return 0
    cap = max(48, 2 * int(config.get("backfill_batch")))
    created, planned_months = 0, 0
    exhausted = config.state_get("gapfill_exhausted") or {}
    with db.tx() as conn:
        world = _World(conn)
        for m in todo:
            if created >= cap or planned_months >= ROUND_MONTHS:
                break
            a, b = _bounds_in_period(m["month"])
            n = _retries(conn, m["month"])
            if not n:
                rows = []
                for src in world.sources:            # je Quelle die niedrigste Stufe, die noch etwas bringt
                    rows += [(src["id"],) + t for t in (world.wayback(src, a, b) or world.search(src, a, b))]
                rows = rows[: cap - created]
                for src_id, method, target, start, end, prio, depth in rows:
                    n += conn.execute("INSERT OR IGNORE INTO backfill_tasks(source_id, method, target, period_start, "
                                      "period_end, priority, depth) VALUES(?,?,?,?,?,?,?)",
                                      (src_id, method, target, start.isoformat(), end.isoformat(), prio, depth)).rowcount
                    world.targets.add(target)
            if n:
                created += n
                planned_months += 1
            else:
                exhausted[m["month"]] = datetime.now(timezone.utc).isoformat()
    config.state_set("gapfill_exhausted", exhausted)
    _cache["at"] = 0.0
    if created:
        log.info("Lückenschluss: %d Abrufe für %d Monate eingeplant", created, planned_months)
        config.state_set("gapfill_last", db.utcnow())
    return created


# ---------------------------------------------------------------------------------- Abschätzung

def estimate(max_age: float = 600) -> dict:
    """Wie viel ist noch zu tun? Monate, Abrufe, Dauer und Datenverkehr (zwischengespeichert)."""
    if _cache["data"] is not None and time.time() - _cache["at"] < max_age:
        return _cache["data"]
    ms = months_state()
    r = rate()
    open_months = [m for m in ms["months"] if m["state"] == "offen"]
    queued = (db.query_one("SELECT COUNT(*) n FROM backfill_tasks t JOIN sources s ON s.id=t.source_id "
                           "WHERE t.status='offen' AND s.enabled=1") or {"n": 0})["n"]
    future = 0
    conn = db.connect()
    world = _World(conn) if open_months else None
    for m in open_months:
        a, b = _bounds_in_period(m["month"])
        cand = _retries(conn, m["month"], count_only=True)
        for src in world.sources:
            cand += len(world.wayback(src, a, b)) + len(world.search(src, a, b))
        need = cand
        if r["yield_per_task"]:
            need = min(cand, math.ceil((m["target"] - m["articles"]) / max(r["yield_per_task"], 0.02)))
        m["candidates"] = cand
        future += need
    tasks = queued + future
    data = {**ms, "open_months": len(open_months),
            "reached": sum(1 for m in ms["months"] if m["state"] == "erreicht"),
            "exhausted": sum(1 for m in ms["months"] if m["state"] == "ausgeschöpft"),
            "queued": queued, "future": future, "tasks": tasks,
            "seconds": tasks * r["sec_per_task"], "bytes": tasks * r["bytes_per_task"],
            "rate": r, "at": time.time()}
    _cache.update(at=time.time(), data=data)
    return data


def invalidate() -> None:
    _cache["at"] = 0.0
