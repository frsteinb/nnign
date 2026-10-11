"""Scans, historischer Backfill und Zeitsteuerung."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import httpx

from . import config, db, fetcher, llm, sentiment, topics
from .fetcher import FetchError, Item
from .relevance import Matcher
from .sources import all_sources, feeds_of
from .textutil import normalize_url, shorten, title_key

log = logging.getLogger(__name__)

_ingest_lock = threading.Lock()
_scan_lock = threading.Lock()
_wake = threading.Event()
_stop = threading.Event()
_manual_request: dict = {}

STATUS: dict = {
    "scan": None,          # {"kind", "phase", "started"} während eines Scans
    "backfill": None,      # {"phase", "batch", ...}
    "next_scan_at": None,
    "last_error": None,
    "reclassify": None,
}

# Abbruch einer Archivquelle nach so vielen Fehlschlägen bzw. leeren Seiten in Folge
BREAKER_LIMIT = 15


class Ctx:
    def __init__(self, kind: str):
        self.kind = kind
        self.fetched = 0
        self.candidates = 0
        self.new_articles = 0
        self.new_topics = 0
        self.llm_calls = 0
        self.lines: list[str] = []

    def log(self, msg: str) -> None:
        log.info("[%s] %s", self.kind, msg)
        self.lines.append(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}")
        if len(self.lines) > 400:
            self.lines = self.lines[-400:]


# ====================================================================== Verarbeitung

def ingest(items: list[Item], ctx: Ctx, allow_llm: bool, http: fetcher.Http | None = None,
           fetch_details: bool = False, max_age_days: int | None = None) -> None:
    """Neue, relevante Meldungen speichern und Themen zuordnen."""
    ctx.fetched += len(items)
    matcher = Matcher.from_config()
    now = datetime.now(timezone.utc)
    window = int(config.get("cluster_window_days"))
    threshold = float(config.get("cluster_threshold"))

    # 1. Zeitraum, Relevanz (billig) und Dubletten innerhalb des Abrufs
    fresh: dict[tuple, Item] = {}
    for it in items:
        it.url = normalize_url(it.url)
        if max_age_days is not None and (now - it.published).days > max_age_days:
            continue
        if not in_period(it.published.date()):
            continue
        if it.published > now + timedelta(hours=6):
            it.published = now
        if matcher.score(it.title, it.summary) <= 0:
            continue
        key = (title_key(it.title), it.source_domain)
        prev = fresh.get(key)
        if prev is None or (not prev.summary and it.summary):
            fresh[key] = it
    if not fresh:
        return

    # 2. Bereits bekannte Meldungen aussortieren
    new_items = []
    for (tkey, dom), it in fresh.items():
        if db.query_one("SELECT 1 FROM articles WHERE url=? OR (title_key=? AND source_domain=?)",
                        (it.url, tkey, dom)):
            continue
        new_items.append(it)
    ctx.candidates += len(new_items)
    if not new_items:
        return

    # 3. Details nachladen (Archivtreffer ohne Teaser)
    if fetch_details and http is not None:
        need = [it for it in new_items if it.needs_details and not it.summary]
        if need:
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda x: fetcher.enrich(http, x), need))
            new_items = [it for it in new_items if matcher.score(it.title, it.summary) > 0]

    new_items.sort(key=lambda x: x.published)
    use_llm = allow_llm and llm.available()

    # 4. Themen zuordnen (serialisiert, damit parallele Läufe keine Doppel-Themen erzeugen).
    #    Pro Meldung eine kurze Schreibtransaktion; der KI-Aufruf läuft außerhalb davon.
    with _ingest_lock:
        conn = db.connect()
        idf = topics.Idf(conn)
        for it in new_items:
            tkey = title_key(it.title)
            if conn.execute("SELECT 1 FROM articles WHERE url=? OR (title_key=? AND source_domain=?)",
                            (it.url, tkey, it.source_domain)).fetchone():
                continue
            vec = topics.article_vector(it.title, it.summary)
            pub = it.published.replace(microsecond=0).isoformat()
            senti = sentiment.analyze(it.title, it.summary)
            art_score = senti.score
            art_detail = senti.detail()
            matches = topics.best_matches(conn, vec, it.published, idf, window)
            best_sim, best_row = matches[0] if matches else (0.0, None)

            res = None
            if use_llm and best_sim < 0.6:
                cands = [{"id": r["id"], "title": r["title"], "abstract": r["abstract"],
                          "first": r["first_published_at"], "last": r["last_published_at"]}
                         for sim, r in matches if sim >= 0.08]
                res = llm.analyze({"title": it.title, "summary": it.summary, "source": it.source_name,
                                   "published": pub}, cands)
                if res is not None:
                    ctx.llm_calls += 1

            with db.tx() as conn:
                topic_id = None
                relevant = 1
                if res is not None:
                    if not res["relevant"]:
                        relevant = 0
                    elif res["match_topic_id"] and conn.execute(
                            "SELECT 1 FROM topics WHERE id=?", (res["match_topic_id"],)).fetchone():
                        topic_id = res["match_topic_id"]
                    else:
                        topic_id = topics.create_topic(
                            conn, res["topic_title"].strip() or it.title, res["abstract"].strip(),
                            res["sentiment"], art_score, "llm", pub, vec,
                            {"llm": res.get("begruendung", ""), "confidence": res.get("confidence"),
                             "model": llm.describe(), **art_detail})
                        ctx.new_topics += 1
                else:
                    topic_id = _heuristic_topic(conn, it, vec, pub, senti, best_sim, best_row,
                                                threshold, ctx)
                conn.execute(
                    "INSERT OR IGNORE INTO articles(url, title, title_key, summary, source_name, source_domain, "
                    "published_at, fetched_at, via, relevant, sentiment_score, sentiment_detail, topic_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it.url, it.title, tkey, it.summary, it.source_name, it.source_domain, pub, db.utcnow(),
                     it.via, relevant, art_score, json.dumps(art_detail, ensure_ascii=False),
                     topic_id if relevant else None))
                if relevant:
                    idf.add_document(vec.keys())
                    ctx.new_articles += 1
                    if topic_id:
                        topics.attach(conn, topic_id, vec, pub)
                        topics.refresh(conn, topic_id)


def _heuristic_topic(conn, it, vec, pub, senti, best_sim, best_row, threshold, ctx) -> int:
    if best_row is not None and best_sim >= threshold:
        return best_row["id"]
    ctx.new_topics += 1
    return topics.create_topic(conn, it.title, shorten(it.summary or ""), sentiment.label_for(senti),
                               senti.score, "lexikon", pub, vec, senti.detail())


def _source_status(src_id: int, status: str, n: int) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE sources SET last_fetch_at=?, last_status=?, last_items=? WHERE id=?",
                     (db.utcnow(), status[:300], n, src_id))


def reclassify() -> dict:
    """Regelbasierte Bewertung aller Meldungen neu berechnen (z. B. nach geänderten Regeln).
    KI-bewertete und manuell bearbeitete Themen behalten ihre Bewertung."""
    STATUS["reclassify"] = "läuft"
    n_art = n_topic = 0
    try:
        rows = db.query("SELECT id, title, summary FROM articles")
        with _ingest_lock:
            for i in range(0, len(rows), 500):
                with db.tx() as conn:
                    for a in rows[i:i + 500]:
                        r = sentiment.analyze(a["title"], a["summary"])
                        conn.execute("UPDATE articles SET sentiment_score=?, sentiment_detail=? WHERE id=?",
                                     (r.score, json.dumps(r.detail(), ensure_ascii=False), a["id"]))
                        n_art += 1
            ids = [r["id"] for r in db.query("SELECT id FROM topics WHERE locked=0 AND sentiment_origin='lexikon'")]
            for i in range(0, len(ids), 500):
                with db.tx() as conn:
                    for tid in ids[i:i + 500]:
                        topics.refresh(conn, tid)
                        n_topic += 1
    finally:
        STATUS["reclassify"] = None
    log.info("Neu bewertet: %d Meldungen, %d Themen", n_art, n_topic)
    return {"articles": n_art, "topics": n_topic}


# ====================================================================== Scans

def _collect_current(http: fetcher.Http, ctx: Ctx, initial: bool) -> list[Item]:
    items: list[Item] = []
    search = bool(config.get("search_enabled"))
    query = config.get("search_query")
    recent_days = int(config.get("initial_recent_days"))
    today = datetime.now(timezone.utc).date()
    since, until = period()
    search_from = max(since, today - timedelta(days=recent_days))
    search_to = min(until or today, today)

    def work(src):
        got, errors = [], []
        for url in feeds_of(src):
            try:
                got += fetcher.fetch_feed(http, url, src)
            except (FetchError, httpx.HTTPError, ValueError) as exc:
                errors.append(f"Feed {url}: {exc}")
        if search and src["use_search"]:
            try:
                if initial:
                    for start, end in fetcher.iter_periods(search_from, search_to, "week"):
                        got += fetcher.fetch_search(http, src, query, after=start - timedelta(days=1),
                                                    before=end + timedelta(days=1))
                else:
                    got += fetcher.fetch_search(http, src, query, when_days=2)
            except (FetchError, httpx.HTTPError, ValueError) as exc:
                errors.append(f"Suche: {exc}")
        status = "ok" if not errors else ("teilweise: " if got else "fehler: ") + "; ".join(errors)
        _source_status(src["id"], status, len(got))
        return src, got, errors

    srcs = all_sources(enabled_only=True)
    with ThreadPoolExecutor(max_workers=max(1, int(config.get("backfill_workers")))) as pool:
        for src, got, errors in pool.map(work, srcs):
            items += got
            ctx.log(f"{src['name']}: {len(got)} Meldungen" + (f" ({len(errors)} Fehler)" if errors else ""))
            for e in errors:
                ctx.log(f"  ! {e}")
    return items


def run_scan(kind: str) -> int | None:
    """kind: initial | regulär | manuell. Gibt die Run-ID zurück (None, wenn bereits ein Scan läuft)."""
    if not _scan_lock.acquire(blocking=False):
        return None
    ctx = Ctx(kind)
    with db.tx() as conn:
        run_id = conn.execute("INSERT INTO runs(kind, started_at) VALUES(?, ?)", (kind, db.utcnow())).lastrowid
    STATUS["scan"] = {"kind": kind, "phase": "Abruf der Quellen", "started": db.utcnow(), "run_id": run_id}
    status = "ok"
    http = fetcher.Http()
    try:
        initial = kind == "initial"
        items = _collect_current(http, ctx, initial)
        STATUS["scan"]["phase"] = f"Analyse von {len(items)} Meldungen"
        ingest(items, ctx, allow_llm=True, http=http, fetch_details=False,
               max_age_days=None if initial else int(config.get("regular_lookback_days")))
        ctx.log(f"Fertig: {ctx.candidates} neue relevante Kandidaten, {ctx.new_articles} gespeichert, "
                f"{ctx.new_topics} neue Themen, {ctx.llm_calls} KI-Aufrufe")
        if initial:
            if ctx.fetched == 0:
                # Vermutlich kein Netz: Initial-Scan später wiederholen statt als erledigt zu markieren
                raise RuntimeError("Keine einzige Meldung abrufbar – Netzwerk/Proxy prüfen. "
                                   "Der Initial-Scan wird in 10 Minuten wiederholt.")
            config.state_set("initial_done", db.utcnow())
            plan_backfill()
    except Exception as exc:  # Scan darf den Dienst nie beenden
        log.exception("Scan fehlgeschlagen")
        ctx.log(f"FEHLER: {exc}")
        STATUS["last_error"] = str(exc)
        status = "fehler"
    finally:
        http.close()
        with db.tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, fetched=?, candidates=?, new_articles=?, new_topics=?, "
                "llm_calls=?, log=? WHERE id=?",
                (db.utcnow(), status, ctx.fetched, ctx.candidates, ctx.new_articles, ctx.new_topics,
                 ctx.llm_calls, "\n".join(ctx.lines), run_id))
        STATUS["scan"] = None
        _scan_lock.release()
    return run_id


# ====================================================================== Historischer Backfill

def _priority(i: int) -> int:
    """Grob-nach-fein-Reihenfolge: zuerst jede 32. Periode, dann jede 16. usw.
    So entsteht früh ein Überblick über den gesamten Zeitraum."""
    if i == 0:
        return 0
    tz = (i & -i).bit_length() - 1
    return max(0, 5 - min(5, tz))


DEFAULT_PERIOD_DAYS = 28  # Standard beim Erststart: die letzten vier Wochen


def ensure_default_period() -> None:
    """Beim allerersten Start den Standard-Beginn festhalten, damit er nicht täglich mitwandert."""
    if not config.state_get("default_collect_since"):
        start = datetime.now(timezone.utc).date() - timedelta(days=DEFAULT_PERIOD_DAYS)
        config.state_set("default_collect_since", start.isoformat())


def _parse_date(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()) if raw and str(raw).strip() else None
    except ValueError:
        return None


def period() -> tuple[date, date | None]:
    """Erfassungszeitraum (von, bis); bis = None bedeutet laufend."""
    today = datetime.now(timezone.utc).date()
    since = (_parse_date(config.get("collect_since")) or _parse_date(config.state_get("default_collect_since"))
             or today - timedelta(days=DEFAULT_PERIOD_DAYS))
    until = _parse_date(config.get("collect_until"))
    return since, until


def in_period(d: date) -> bool:
    since, until = period()
    return d >= since and (until is None or d <= until)


def plan_backfill() -> int:
    """Legt (idempotent) Backfill-Aufgaben für den Erfassungszeitraum an.
    Gibt die Zahl neu angelegter Aufgaben zurück."""
    today = datetime.now(timezone.utc).date()
    since, until = period()
    until = min(until or today, today)
    recent_days = int(config.get("initial_recent_days"))
    win = max(7, int(config.get("backfill_search_window_days")))
    search = bool(config.get("search_enabled"))
    query = config.get("search_query")
    created = 0
    with db.tx() as conn:
        for src in all_sources(enabled_only=True):
            rows = []
            if src["archive_template"]:
                a_since = since
                if src["archive_since"]:
                    try:
                        a_since = max(since, date.fromisoformat(src["archive_since"]))
                    except ValueError:
                        pass
                for i, (start, end) in enumerate(fetcher.iter_periods(a_since, until, src["archive_step"] or "day")):
                    url = fetcher.render_template(src["archive_template"], start)
                    rows.append(("archive", url, start, end, _priority(i)))
            if config.get("backfill_wayback"):
                for feed in feeds_of(src):
                    rows.append(("wayback_index", feed, date(1, 1, 1), date(1, 1, 1), -1))
            if search and src["use_search"]:
                end_hist = min(until, today - timedelta(days=recent_days))
                d, i = since, 0
                while d <= end_hist:
                    e = min(d + timedelta(days=win - 1), end_hist)
                    url = fetcher.search_url(query, src["domain"], after=d - timedelta(days=1),
                                             before=e + timedelta(days=1))
                    rows.append(("search", url, d, e, _priority(i)))
                    d, i = e + timedelta(days=1), i + 1
            for method, target, start, end, prio in rows:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO backfill_tasks(source_id, method, target, period_start, period_end, "
                    "priority) VALUES(?,?,?,?,?,?)", (src["id"], method, target, start.isoformat(),
                                                      end.isoformat(), prio))
                created += cur.rowcount
    return created


OUTSIDE_MARK = "Außerhalb des Erfassungszeitraums"


def apply_period(since: date, until: date | None, purge: bool = False) -> dict:
    """Erfassungszeitraum setzen: Aufgaben außerhalb stoppen, innerhalb (wieder) einplanen,
    optional Daten außerhalb löschen."""
    config.set("collect_since", since.isoformat())
    config.set("collect_until", until.isoformat() if until else "")
    hi = (until or date(9999, 12, 31)).isoformat()
    with db.tx() as conn:
        stopped = conn.execute(
            "UPDATE backfill_tasks SET status='übersprungen', error=? WHERE status='offen' "
            "AND method!='wayback_index' AND (period_end<? OR period_start>?)",
            (OUTSIDE_MARK, since.isoformat(), hi)).rowcount
        revived = conn.execute(
            "UPDATE backfill_tasks SET status='offen', attempts=0, error='' WHERE status='übersprungen' "
            "AND error=? AND period_end>=? AND period_start<=?", (OUTSIDE_MARK, since.isoformat(), hi)).rowcount
    planned = plan_backfill()
    deleted = {"articles": 0, "topics": 0}
    if purge:
        a = delete_range(None, since - timedelta(days=1))
        b = delete_range(until + timedelta(days=1), None) if until else {"articles": 0, "topics": 0}
        deleted = {k: a[k] + b[k] for k in a}
    _wake.set()
    return {"stopped": stopped, "revived": revived, "planned": planned, **deleted}


def delete_range(d_from: date | None, d_to: date | None) -> dict:
    """Meldungen mit Erscheinungsdatum im Bereich löschen; leere Themen entfallen, übrige werden neu berechnet."""
    lo = d_from.isoformat() if d_from else "0000"
    hi = (d_to.isoformat() + "T23:59:59") if d_to else "9999"
    with _ingest_lock, db.tx() as conn:
        affected = [r["topic_id"] for r in conn.execute(
            "SELECT DISTINCT topic_id FROM articles WHERE published_at>=? AND published_at<=? "
            "AND topic_id IS NOT NULL", (lo, hi))]
        n_art = conn.execute("DELETE FROM articles WHERE published_at>=? AND published_at<=?", (lo, hi)).rowcount
        n_top = 0
        for tid in affected:
            if not conn.execute("SELECT 1 FROM articles WHERE topic_id=?", (tid,)).fetchone():
                conn.execute("DELETE FROM topics WHERE id=?", (tid,))
                n_top += 1
            else:
                topics.rebuild_signature(conn, tid)
                topics.refresh(conn, tid)
    log.info("Gelöscht: %d Meldungen, %d Themen (%s bis %s)", n_art, n_top, lo, hi)
    return {"articles": n_art, "topics": n_top}


def wipe(factory: bool = False) -> None:
    """Alle erfassten Daten löschen und neu initialisieren. factory=True setzt zusätzlich Einstellungen
    und Quellen auf den Auslieferungszustand zurück (Admin-Passwort bleibt erhalten)."""
    with _ingest_lock, _scan_lock, db.tx() as conn:
        for table in ("articles", "topics", "token_df", "runs", "backfill_tasks"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM settings WHERE key IN ('_initial_done')")
        if factory:
            conn.execute("DELETE FROM settings WHERE key NOT IN ('_admin_pw_hash')")
            conn.execute("DELETE FROM sources")
    _breaker_counts.clear()
    if factory:
        from .sources import seed_sources
        seed_sources()
        ensure_default_period()
    log.warning("Datenbestand gelöscht (%s) – Initial-Scan folgt", "Werkseinstellung" if factory else "Daten")
    _wake.set()


def backfill_progress() -> dict:
    rows = db.query("SELECT status, COUNT(*) n FROM backfill_tasks GROUP BY status")
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())
    done = total - counts.get("offen", 0)
    per_method = db.query(
        "SELECT s.name, t.method, SUM(t.status='offen') offen, SUM(t.status='ok') ok, "
        "SUM(t.status='fehler') fehler, SUM(t.status='übersprungen') skipped, SUM(t.relevant) relevant, "
        "MIN(CASE WHEN t.status='ok' AND t.method!='wayback_index' THEN t.period_start END) earliest "
        "FROM backfill_tasks t LEFT JOIN sources s ON s.id=t.source_id GROUP BY s.name, t.method ORDER BY s.name")
    return {"counts": counts, "total": total, "done": done,
            "percent": round(100 * done / total, 1) if total else 0.0,
            "per_source": [dict(r) for r in per_method]}


def _run_task(http: fetcher.Http, task, src) -> tuple[list[Item], list[tuple]]:
    """Führt eine Aufgabe aus. Rückgabe: (Meldungen, neue Unteraufgaben)."""
    method = task["method"]
    if method == "archive":
        d = date.fromisoformat(task["period_start"])
        return fetcher.fetch_archive_page(http, src, task["target"], d,
                                          bool(config.get("backfill_wayback_fallback"))), []
    if method == "search":
        resp = http.get(task["target"])
        return fetcher.parse_search(resp.content, src), []
    if method == "wayback_index":
        today = datetime.now(timezone.utc).date()
        since, until = period()
        until = min(until or today, today)
        snaps = fetcher.wayback_index(http, task["target"], since, until,
                                      config.get("backfill_wayback_granularity"))
        subs = []
        for i, (ts, original) in enumerate(snaps):
            d = datetime.strptime(ts[:8], "%Y%m%d").date()
            subs.append(("wayback_feed", fetcher.wayback_snapshot_url(ts, original), d, d, _priority(i)))
        return [], subs
    if method == "wayback_feed":
        url = task["target"]
        ts = url.split("/web/")[1].split("id_/")[0]
        original = url.split("id_/", 1)[1]
        return fetcher.fetch_wayback_feed(http, src, ts, original), []
    raise ValueError(f"Unbekannte Methode {method}")


def backfill_batch(http: fetcher.Http) -> int:
    """Einen Durchgang Backfill-Aufgaben abarbeiten. Gibt die Zahl erledigter Aufgaben zurück."""
    limit = max(1, int(config.get("backfill_batch")))
    tasks = db.query(
        "SELECT t.*, s.enabled FROM backfill_tasks t JOIN sources s ON s.id=t.source_id "
        "WHERE t.status='offen' AND s.enabled=1 ORDER BY t.priority, t.attempts, t.period_start LIMIT ?", (limit,))
    if not tasks:
        return 0
    srcs = {s["id"]: s for s in all_sources()}
    ctx = Ctx("backfill")
    STATUS["backfill"] = {"phase": f"Abruf von {len(tasks)} Aufgaben",
                          "range": f"{tasks[0]['period_start']} …", "started": db.utcnow()}

    def work(task):
        try:
            items, subs = _run_task(http, task, srcs[task["source_id"]])
            return task, items, subs, None
        except (FetchError, httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            return task, [], [], str(exc)

    with ThreadPoolExecutor(max_workers=max(1, int(config.get("backfill_workers")))) as pool:
        results = list(pool.map(work, tasks))

    all_items: list[Item] = []
    for task, items, subs, err in results:
        all_items += items
    STATUS["backfill"]["phase"] = f"Analyse von {len(all_items)} Meldungen"
    before = ctx.new_articles
    ingest(all_items, ctx, allow_llm=bool(config.get("llm_for_backfill")), http=http,
           fetch_details=bool(config.get("backfill_fetch_details")))

    matcher = Matcher.from_config()
    with db.tx() as conn:
        for task, items, subs, err in results:
            relevant = sum(1 for it in items if matcher.score(it.title, it.summary) > 0)
            if err:
                status = "fehler" if task["attempts"] + 1 >= 2 else "offen"
                conn.execute("UPDATE backfill_tasks SET status=?, attempts=attempts+1, error=?, done_at=? WHERE id=?",
                             (status, err[:500], db.utcnow(), task["id"]))
            else:
                conn.execute("UPDATE backfill_tasks SET status='ok', attempts=attempts+1, items=?, relevant=?, "
                             "error='', done_at=? WHERE id=?",
                             (len(items), relevant, db.utcnow(), task["id"]))
            for method, target, start, end, prio in subs:
                conn.execute("INSERT OR IGNORE INTO backfill_tasks(source_id, method, target, period_start, "
                             "period_end, priority) VALUES(?,?,?,?,?,?)",
                             (task["source_id"], method, target, start.isoformat(), end.isoformat(), prio))
            _breaker(conn, task, err, len(items))
    STATUS["backfill"] = None
    log.info("Backfill: %d Aufgaben, %d Meldungen, %d neu gespeichert", len(tasks), len(all_items),
             ctx.new_articles - before)
    return len(tasks)


_breaker_counts: dict[tuple, int] = {}


def _breaker(conn, task, err, n_items: int) -> None:
    """Archivquelle stoppen, wenn sie dauerhaft nichts liefert (z. B. falsche URL-Vorlage)."""
    if task["method"] not in ("archive", "search"):
        return
    key = (task["source_id"], task["method"])
    failed = bool(err) or (task["method"] == "archive" and n_items == 0)
    if not failed:
        _breaker_counts[key] = 0
        return
    _breaker_counts[key] = _breaker_counts.get(key, 0) + 1
    if _breaker_counts[key] >= BREAKER_LIMIT:
        conn.execute("UPDATE backfill_tasks SET status='übersprungen', error=? WHERE source_id=? AND method=? "
                     "AND status='offen'",
                     (f"Automatisch gestoppt nach {BREAKER_LIMIT} Fehlschlägen/leeren Seiten in Folge – "
                      f"Vorlage bzw. Erreichbarkeit prüfen. Letzter Fehler: {(err or 'keine passenden Links')[:200]}",
                      task["source_id"], task["method"]))
        _breaker_counts[key] = 0
        log.warning("Backfill-Quelle %s/%s gestoppt (zu viele Fehlschläge)", task["source_id"], task["method"])


def reset_backfill(only_failed: bool = True) -> int:
    with db.tx() as conn:
        if only_failed:
            cur = conn.execute("UPDATE backfill_tasks SET status='offen', attempts=0, error='' "
                               "WHERE status IN ('fehler', 'übersprungen')")
        else:
            cur = conn.execute("UPDATE backfill_tasks SET status='offen', attempts=0, error=''")
    _breaker_counts.clear()
    return cur.rowcount


# ====================================================================== Zeitsteuerung

def next_scan_time() -> datetime:
    row = db.query_one("SELECT MAX(started_at) t FROM runs WHERE kind IN ('regulär', 'initial', 'manuell')")
    interval = timedelta(hours=max(0.05, float(config.get("scan_interval_hours"))))
    if not row or not row["t"]:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(row["t"]) + interval


def request_scan(kind: str = "manuell") -> None:
    _manual_request["kind"] = kind
    _wake.set()


def _scheduler_loop() -> None:
    time.sleep(2)
    while not _stop.is_set():
        try:
            kind = _manual_request.pop("kind", None)
            if kind:
                run_scan(kind)
            elif not config.state_get("initial_done"):
                last = db.query_one("SELECT MAX(started_at) t FROM runs WHERE kind='initial'")
                if not last["t"] or datetime.now(timezone.utc) - datetime.fromisoformat(last["t"]) > timedelta(minutes=10):
                    run_scan("initial")
            elif datetime.now(timezone.utc) >= next_scan_time():
                run_scan("regulär")
            STATUS["next_scan_at"] = next_scan_time().isoformat()
        except Exception:
            log.exception("Fehler in der Zeitsteuerung")
        _wake.wait(30)
        _wake.clear()


def _backfill_loop() -> None:
    time.sleep(5)
    http = None
    while not _stop.is_set():
        worked = 0
        try:
            if config.get("backfill_enabled") and config.state_get("initial_done"):
                if http is None or http.delay != float(config.get("host_delay_seconds")):
                    if http:
                        http.close()
                    http = fetcher.Http()
                worked = backfill_batch(http)
        except Exception:
            log.exception("Fehler im Backfill")
            STATUS["backfill"] = None
        if not worked:
            _stop.wait(60)


def start_background() -> None:
    threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True).start()
    threading.Thread(target=_backfill_loop, name="backfill", daemon=True).start()


def stop_background() -> None:
    _stop.set()
    _wake.set()
