"""Webserver (FastAPI): Dashboard, JSON-API, CSV-Export und Admin-Bereich."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, classify, config, db, gapfill, llm, maintenance, pipeline, prompts, sentiment, sources, topics, translate

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

HERE = os.path.dirname(__file__)
VERSION = "1.14.1"


# ====================================================================== Admin-Passwort

def _hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()


def init_admin_password() -> None:
    if config.state_get("admin_pw_hash"):
        return
    pw = os.environ.get("ADMIN_PASSWORD")
    generated = not pw
    if generated:
        pw = secrets.token_urlsafe(12)
    set_admin_password(pw)
    if generated:
        path = os.path.join(db.DATA_DIR, "admin_password.txt")
        try:
            with open(path, "w") as fh:
                fh.write(pw + "\n")
        except OSError:
            path = "(nicht schreibbar)"
        log.warning("=" * 70)
        log.warning("Admin-Zugang: Benutzer 'admin', Passwort '%s'", pw)
        log.warning("Auch abgelegt in %s. Änderbar unter /admin.", path)
        log.warning("=" * 70)


def set_admin_password(pw: str) -> None:
    salt = secrets.token_hex(8)
    config.state_set("admin_pw_hash", {"salt": salt, "hash": _hash(pw, salt)})


def check_password(pw: str) -> bool:
    env_pw = os.environ.get("ADMIN_PASSWORD")
    if env_pw and hmac.compare_digest(pw.encode(), env_pw.encode()):
        return True
    stored = config.state_get("admin_pw_hash")
    return bool(stored) and hmac.compare_digest(_hash(pw, stored["salt"]), stored["hash"])


class LoginRequired(Exception):
    """Nicht angemeldet: Browser werden zum Anmeldeformular geleitet, Skripte bekommen 401."""


def _wants_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


def is_admin(request: Request) -> bool:
    return auth.is_admin(request, check_password)


def _check_origin(request: Request) -> None:
    if request.method == "POST":  # einfacher CSRF-Schutz (zusätzlich zu SameSite-Cookies)
        origin = request.headers.get("origin") or request.headers.get("referer")
        allowed = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
        if origin and urlsplit(origin).netloc not in allowed:
            raise HTTPException(403, "Ungültige Herkunft der Anfrage")


def require_admin(request: Request) -> str:
    if not is_admin(request):
        raise LoginRequired()
    _check_origin(request)
    return "admin"


def _select_mode(request: Request) -> bool:
    """Auswahlmodus der Startseite (?auswahl=1) – nur für den Admin; fordert sonst die Anmeldung an."""
    if request.query_params.get("auswahl") != "1":
        return False
    if not is_admin(request):
        raise LoginRequired()
    return True


def maybe_public(request: Request) -> None:
    if not config.get("public_dashboard") and not is_admin(request):
        raise LoginRequired()


# ====================================================================== App

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    changes = sources.seed_sources()
    pipeline.ensure_default_period()
    llm.migrate_settings()
    pipeline.upgrade_on_start(changes)
    maintenance.check(startup=True)
    init_admin_password()
    if config.purge_stored_secrets():
        log.warning("Im Admin-Bereich gespeicherte API-Keys wurden entfernt – API-Keys bitte in .env eintragen")
    if os.environ.get("NNIGN_DISABLE_SCHEDULER") != "1":
        pipeline.start_background()
    log.info("nnign %s gestartet, Daten unter %s", VERSION, db.DB_PATH)
    yield
    pipeline.stop_background()


app = FastAPI(title="nnign – No News Is Good News", version=VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def fmt_dt(value, fmt="%d.%m.%Y %H:%M"):
    if not value:
        return "–"
    try:
        dt = datetime.fromisoformat(value) if isinstance(value, str) else value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(config.get("timezone"))).strftime(fmt)
    except Exception:
        return str(value)


templates.env.filters["dt"] = fmt_dt
templates.env.filters["d"] = lambda v: fmt_dt(v, "%d.%m.%Y")
templates.env.globals["version"] = VERSION
templates.env.globals["is_admin"] = lambda request: is_admin(request)


def _job_groups(jobs):
    out: list = []
    for j in jobs:
        if not out or out[-1][0] != j["group"]:
            out.append((j["group"], []))
        out[-1][1].append(j)
    return out


templates.env.globals["job_groups"] = _job_groups
templates.env.globals["fmt_duration"] = gapfill.fmt_duration
templates.env.globals["suggestions"] = lambda: maintenance.suggestions()
templates.env.globals["symbol"] = {"positiv": "▲", "neutral": "●", "negativ": "▼"}
templates.env.globals["languages"] = sources.LANGUAGES
templates.env.globals["lang_short"] = {"en": "EN", "zh": "ZH"}
templates.env.globals["translator_names"] = {"libretranslate": "LibreTranslate", "deepl": "DeepL", "llm": "KI"}


# ====================================================================== Abfragen

def topic_filters(q: str = "", sentiment: str = "", year: str = "", source: str = "",
                  date_from: str = "", date_to: str = "", include_hidden: bool = False, scope: str = ""):
    where, params = [], []
    if scope in ("konzern", "partner"):
        where.append("t.scope=?")
        params.append(scope)
    if not include_hidden:
        where.append("t.hidden=0")
    if q:
        where.append("(t.title LIKE ? OR t.abstract LIKE ? OR EXISTS (SELECT 1 FROM articles a2 WHERE "
                     "a2.topic_id=t.id AND a2.title LIKE ?))")
        params += [f"%{q}%"] * 3
    if sentiment in ("positiv", "neutral", "negativ"):
        where.append("t.sentiment=?")
        params.append(sentiment)
    if year.isdigit():
        where.append("substr(t.first_published_at,1,4)=?")
        params.append(year)
    if source:
        where.append("EXISTS (SELECT 1 FROM articles a3 WHERE a3.topic_id=t.id AND a3.source_domain=?)")
        params.append(source)
    if date_from:
        where.append("t.first_published_at>=?")
        params.append(date_from)
    if date_to:
        where.append("t.first_published_at<=?")
        params.append(date_to + "T23:59:59")
    return (" WHERE " + " AND ".join(where)) if where else "", params


def topic_sources(topic_id: int, limit: int | None = None):
    limit = limit or int(config.get("max_sources_per_topic"))
    # je Medium die früheste Meldung, sortiert nach Datum
    return db.query(
        "SELECT a.* FROM articles a WHERE a.topic_id=? AND a.id IN ("
        "  SELECT MIN(id) FROM articles WHERE topic_id=? GROUP BY source_domain) "
        "ORDER BY a.published_at LIMIT ?", (topic_id, topic_id, limit))


def list_topics(args: dict, page: int, page_size: int, order: str = "new"):
    where, params = topic_filters(**args)
    total = db.query_one(f"SELECT COUNT(*) n FROM topics t{where}", params)["n"]
    order_sql = {"new": "t.first_published_at DESC", "old": "t.first_published_at ASC",
                 "size": "t.article_count DESC, t.first_published_at DESC",
                 "updated": "t.updated_at DESC"}.get(order, "t.first_published_at DESC") + ", t.id DESC"
    rows = db.query(f"SELECT t.* FROM topics t{where} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    (*params, page_size, (page - 1) * page_size))
    return total, rows


def timeline(args: dict):
    """Positive/neutrale/negative Themen je Jahr (oder je Monat, wenn ein Jahr gewählt ist)."""
    a = dict(args)
    year = a.pop("year", "")
    a.pop("sentiment", None)
    where, params = topic_filters(**a)
    if year.isdigit():
        key = "substr(t.first_published_at,1,7)"
        where = (where + " AND " if where else " WHERE ") + "substr(t.first_published_at,1,4)=?"
        params = [*params, year]
    else:
        key = "substr(t.first_published_at,1,4)"
    rows = db.query(f"SELECT {key} k, SUM(t.sentiment='positiv') pos, SUM(t.sentiment='neutral') neu, "
                    f"SUM(t.sentiment='negativ') neg FROM topics t{where} GROUP BY k ORDER BY k", params)
    data = {r["k"]: (r["pos"] or 0, r["neu"] or 0, r["neg"] or 0) for r in rows}
    if year.isdigit():
        keys = [f"{year}-{m:02d}" for m in range(1, 13)]
    else:
        first = int(min(data)) if data else datetime.now().year
        start = min(first, pipeline.period()[0].year)
        keys = [str(y) for y in range(start, datetime.now().year + 1)]
    return [{"key": k, "pos": data.get(k, (0, 0, 0))[0], "neu": data.get(k, (0, 0, 0))[1],
             "neg": data.get(k, (0, 0, 0))[2]} for k in keys]


def stats(args: dict) -> dict:
    """Kompakte Statistik zum aktuell ausgewählten Datenbestand."""
    where, params = topic_filters(**args)
    t = db.query_one(
        f"SELECT COUNT(*) n, SUM(t.sentiment='positiv') pos, SUM(t.sentiment='neutral') neu, "
        f"SUM(t.sentiment='negativ') neg, SUM(t.article_count) arts, MIN(t.first_published_at) first, "
        f"MAX(t.last_published_at) last, SUM(t.article_count > 1) multi, "
        f"SUM(t.sentiment_origin='lexikon') o_lex, SUM(t.sentiment_origin='llm') o_llm, "
        f"SUM(t.sentiment_origin='manuell') o_man FROM topics t{where}", params)
    n = t["n"] or 0

    def pct(x):
        return round(100 * (x or 0) / n) if n else 0

    media = db.query(
        f"SELECT a.source_name name, COUNT(DISTINCT a.topic_id) c FROM articles a JOIN topics t ON t.id=a.topic_id"
        f"{where} GROUP BY a.source_name ORDER BY c DESC", params)
    return {
        "topics": n, "articles": t["arts"] or 0, "first": t["first"], "last": t["last"],
        "pos": t["pos"] or 0, "neu": t["neu"] or 0, "neg": t["neg"] or 0,
        "pos_pct": pct(t["pos"]), "neu_pct": pct(t["neu"]), "neg_pct": pct(t["neg"]),
        "multi_pct": pct(t["multi"]), "per_topic": round((t["arts"] or 0) / n, 1) if n else 0,
        "media_count": len(media), "top_media": [dict(m) for m in media[:5]],
        "origin": {"Regeln": t["o_lex"] or 0, "KI": t["o_llm"] or 0, "manuell": t["o_man"] or 0},
        "filtered": any(args.values()),
    }


def status_info() -> dict:
    counts = db.query_one(
        "SELECT COUNT(*) n, SUM(sentiment='positiv') pos, SUM(sentiment='neutral') neu, "
        "SUM(sentiment='negativ') neg, MIN(first_published_at) first FROM topics WHERE hidden=0")
    arts = db.query_one("SELECT COUNT(*) n FROM articles WHERE relevant=1")["n"]
    last = db.query_one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    return {
        "topics": counts["n"] or 0, "positive": counts["pos"] or 0, "neutral": counts["neu"] or 0,
        "negative": counts["neg"] or 0,
        "earliest_topic": counts["first"], "articles": arts,
        "last_run": dict(last) if last else None,
        "last_scan_at": (db.query_one("SELECT MAX(started_at) t FROM runs WHERE kind IN "
                                      "('regulär', 'initial', 'manuell', 'nachholen')") or {"t": None})["t"],
        "scan_running": pipeline.STATUS["scan"],
        "backfill_running": pipeline.STATUS["backfill"],
        "next_scan_at": pipeline.next_scan_time().isoformat() if config.state_get("initial_done") else None,
        "initial_done": config.state_get("initial_done"),
        "backfill": pipeline.backfill_progress(),
        "llm_active": llm.available(),
        "llm_status": llm.describe(),
        "llm_calls_today": llm.calls_today(),
        "reclassify_running": pipeline.STATUS.get("reclassify"),
        "translate_status": translate.describe(),
        "foreign": {r["lang"]: {"n": r["n"], "done": r["done"]} for r in db.query(
            "SELECT lang, COUNT(*) n, SUM(translated_by!='') done FROM articles WHERE relevant=1 AND lang!='de' "
            "GROUP BY lang")},
        "pending_catchup": config.state_get("pending_catchup") or [],
        "pending_reclassify": bool(config.state_get("pending_reclassify")),
    }


def _since(iso: str | None) -> str:
    """„seit 3 min“ aus einem ISO-Zeitstempel."""
    if not iso:
        return ""
    try:
        secs = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return ""
    if secs < 90:
        return f"seit {int(secs)} s"
    if secs < 5400:
        return f"seit {int(secs // 60)} min"
    return f"seit {secs / 3600:.1f} h"


def _duration(secs: float) -> str:
    if secs < 90:
        return f"{int(secs)} s"
    if secs < 5400:
        return f"{int(secs // 60)} min"
    if secs < 172800:
        return f"{secs / 3600:.1f} h".replace(".", ",")
    return f"{secs / 86400:.0f} Tage"


def _pct(done, total) -> float | None:
    return round(100 * done / total, 1) if total else None


def activity() -> list[dict]:
    """Was läuft gerade? Für die Live-Anzeige auf der Startseite (leer = Ruhe)."""
    out = []
    st = pipeline.STATUS
    names = {"initial": "Initial-Scan", "regulär": "Aktualisierung", "manuell": "Aktualisierung (manuell)",
             "nachholen": "Nachholen neuer Quellen"}
    scan = st.get("scan")
    if scan:
        steps = []
        pct = None
        if scan.get("to_analyse"):
            pct = _pct(scan.get("analysed", 0), scan["to_analyse"])
            steps.append(f"{scan.get('analysed', 0)} von {scan['to_analyse']} neuen Meldungen verarbeitet")
        elif scan.get("sources_total"):
            pct = _pct(scan.get("sources_done", 0), scan["sources_total"])
            steps.append(f"{scan.get('sources_done', 0)} von {scan['sources_total']} Quellen abgerufen")
        if scan.get("fetched"):
            steps.append(f"{scan['fetched']} Meldungen gefunden")
        if scan.get("new_articles"):
            steps.append(f"{scan['new_articles']} neu gespeichert")
        out.append({"kind": "scan", "title": names.get(scan.get("kind"), "Scan"), "phase": scan.get("phase", ""),
                    "detail": " · ".join(steps), "pct": pct, "since": _since(scan.get("started"))})
    elif not config.state_get("initial_done"):
        last = db.query_one("SELECT status FROM runs WHERE kind='initial' ORDER BY id DESC LIMIT 1")
        if last and last["status"] == "fehler":
            out.append({"kind": "warn", "title": "Initial-Scan fehlgeschlagen",
                        "phase": "Keine Quelle erreichbar – neuer Versuch alle 10 Minuten.", "detail": "", "pct": None})
        else:
            out.append({"kind": "scan", "title": "Initial-Scan", "phase": "wird vorbereitet …", "detail": "",
                        "pct": None})
    pending = config.state_get("pending_catchup") or []
    if pending and not (scan and scan.get("kind") == "nachholen"):
        out.append({"kind": "wait", "title": "Nachholen neuer Quellen", "phase": f"{len(pending)} Quelle(n) nach "
                    "Update – startet in Kürze", "detail": "", "pct": None})
    prog = pipeline.backfill_progress()
    open_n = prog["counts"].get("offen", 0)
    if open_n and config.state_get("initial_done"):
        bf = st.get("backfill")
        session = st.get("backfill_session") or {}
        if not config.get("backfill_enabled"):
            out.append({"kind": "paused", "title": "Historie-Abruf angehalten",
                        "phase": f"{prog['percent']:g} % abgerufen, {open_n} Aufgaben offen".replace(".", ","),
                        "detail": "", "pct": prog["percent"]})
        else:
            steps = [f"{prog['done']} von {prog['total']} Abrufen erledigt"]
            if session.get("tasks") and time.time() - session.get("started", time.time()) > 60:
                rate = session["tasks"] / (time.time() - session["started"])
                steps.append(f"noch etwa {_duration(open_n / rate)}")
            if session.get("new_articles"):
                steps.append(f"{session['new_articles']} Meldungen neu seit Start")
            phase = "wartet auf nächsten Durchgang"
            if bf:
                phase = bf.get("phase", "")
                if bf.get("to_analyse"):
                    phase += f" ({bf.get('analysed', 0)}/{bf['to_analyse']})"
                elif bf.get("tasks"):
                    phase += f" ({bf.get('tasks_done', 0)}/{bf['tasks']})"
                phase += f" · Zeitraum {bf.get('range', '')}"
                if bf.get("sources"):
                    phase += " · " + ", ".join(bf["sources"][:4]) + (" …" if len(bf["sources"]) > 4 else "")
            out.append({"kind": "backfill", "title": "Historie wird abgerufen", "phase": phase,
                        "detail": " · ".join(steps), "pct": prog["percent"]})
    if st.get("reclassify") or config.state_get("pending_reclassify"):
        rc = st.get("reclassify") or {}
        out.append({"kind": "reclassify", "title": "Bewertungen werden neu berechnet",
                    "phase": f"{rc.get('phase', '')}: {rc.get('done', 0)} von {rc['total']}" if rc.get("total")
                    else "nach geänderten Regeln oder Verfahren", "detail": "",
                    "pct": _pct(rc.get("done", 0), rc["total"]) if rc.get("total") else None})
    if st.get("regroup") or config.state_get("pending_regroup"):
        rg = st.get("regroup") or {}
        out.append({"kind": "regroup", "title": "Themen werden neu gebündelt",
                    "phase": "verbessertes Verfahren der Themenbildung" if not rg.get("total") else
                    f"{rg.get('done', 0)} von {rg['total']} Meldungen neu zugeordnet",
                    "detail": "", "pct": _pct(rg.get("done", 0), rg["total"]) if rg.get("total") else None})
    if llm.available():
        rr = maintenance.STATUS.get("rerate")
        open_n = maintenance.rerate_open()
        blocked = llm.blocked_reason()
        if rr:
            out.append({"kind": "rerate", "title": "KI-Nachbewertung", "phase": f"„{rr['title']}“",
                        "detail": f"noch {open_n} Themen", "pct": None})
        elif open_n and blocked:
            out.append({"kind": "wait", "title": "KI pausiert", "phase": blocked,
                        "detail": f"{open_n} Themen werden danach automatisch von der KI bewertet", "pct": None})
        elif open_n and not maintenance._share_ok():
            out.append({"kind": "wait", "title": "KI-Nachbewertung wartet",
                        "phase": f"Anteil des Tageslimits für Nachbewertungen ausgeschöpft – weiter ab {llm._local(llm._next_utc_midnight())} Uhr",
                        "detail": f"{open_n} Themen offen", "pct": None})
    zs = maintenance.STATUS.get("zeroshot")
    if zs:
        out.append({"kind": "rerate", "title": "Zero-Shot-Bewertung", "phase": f"„{zs['title']}“",
                    "detail": f"noch {maintenance.zeroshot_open()} Themen", "pct": None})
    if st.get("translate"):
        out.append({"kind": "translate", "title": "Nachübersetzung",
                    "phase": f"{st['translate']['n']} fremdsprachige Meldungen", "detail": "", "pct": None})
    return out


def _args(request: Request) -> dict:
    qp = request.query_params
    return {k: (qp.get(k) or "").strip() for k in ("q", "sentiment", "year", "source", "date_from", "date_to", "scope")}


# ====================================================================== Öffentliche Seiten

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, page: int = 1, order: str = "new", _=Depends(maybe_public)):
    args = _args(request)
    page = max(1, page)
    size = int(config.get("page_size"))
    select = _select_mode(request)
    total, rows = list_topics(args, page, size, order)
    items = [{"t": t, "sources": topic_sources(t["id"])} for t in rows]
    acts = activity()
    tl = timeline(args)
    # Skala für das gestapelte Diagramm: neutral liegt mittig auf der Nulllinie
    max_v = max([max(x["pos"] + x["neu"] / 2, x["neg"] + x["neu"] / 2) for x in tl] + [1])
    domains = db.query("SELECT source_domain d, MIN(source_name) n, COUNT(*) c FROM articles "
                       "WHERE relevant=1 GROUP BY source_domain ORDER BY n")
    base_q = {k: v for k, v in args.items() if v}
    if select:
        base_q["auswahl"] = "1"
    return templates.TemplateResponse(request, "index.html", {
        "select": select, "msg": request.query_params.get("msg", ""),
        "items": items, "total": total, "page": page, "pages": max(1, -(-total // size)), "args": args,
        "order": order, "timeline": tl, "max_v": max_v, "domains": domains, "status": status_info(),
        "coverage": maintenance.coverage_by_year(),
        "gap": gapfill.estimate() if config.get("gapfill_enabled") else None,
        "activity": acts, "more_url": "/fragment/topics?" + urlencode({**base_q, "order": order, "page": page + 1})
        if page * size < total else "",
        "stats": stats(args), "period_since": pipeline.period()[0].isoformat(),
        "qs": lambda **kw: "?" + urlencode({**base_q, "order": order, **kw}),
    })


@app.get("/fragment/topics", response_class=HTMLResponse)
def fragment_topics(request: Request, page: int = 2, order: str = "new", _=Depends(maybe_public)):
    """Weitere Themen für das Nachladen beim Scrollen (HTML-Ausschnitt)."""
    args = _args(request)
    page = max(1, page)
    size = int(config.get("page_size"))
    select = _select_mode(request)
    total, rows = list_topics(args, page, size, order)
    base_q = {k: v for k, v in args.items() if v}
    if select:
        base_q["auswahl"] = "1"
    more = ("/fragment/topics?" + urlencode({**base_q, "order": order, "page": page + 1})
            if page * size < total else "")
    resp = templates.TemplateResponse(request, "_topic_items.html", {
        "items": [{"t": t, "sources": topic_sources(t["id"])} for t in rows], "select": select})
    resp.headers["X-More"] = more
    return resp


@app.get("/fragment/activity", response_class=HTMLResponse)
def fragment_activity(request: Request, _=Depends(maybe_public)):
    """Live-Anzeige laufender Abrufe; die Startseite fragt sie regelmäßig ab."""
    acts = activity()
    resp = templates.TemplateResponse(request, "_activity.html", {"activity": acts, "status": status_info()})
    resp.headers["X-Active"] = "1" if acts else "0"
    resp.headers["X-Topics"] = str(db.query_one("SELECT COUNT(*) n FROM topics WHERE hidden=0")["n"])
    return resp


@app.get("/thema/{topic_id}", response_class=HTMLResponse)
def topic_page(request: Request, topic_id: int, _=Depends(maybe_public)):
    t = db.query_one("SELECT * FROM topics WHERE id=?", (topic_id,))
    if t is None:
        raise HTTPException(404, "Thema nicht gefunden")
    arts = db.query("SELECT * FROM articles WHERE topic_id=? ORDER BY published_at", (topic_id,))
    return templates.TemplateResponse(request, "topic.html", {
        "t": t, "articles": arts, "sources": topic_sources(topic_id),
        "detail": _json(t["sentiment_detail"]),
        "art_labels": {a["id"]: sentiment.label_for(_result(_json(a["sentiment_detail"]))) for a in arts},
    })


def _json(raw) -> dict:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def _result(d: dict) -> sentiment.Result:
    return sentiment.Result(float(d.get("pos", 0)), float(d.get("neg", 0)), d.get("hits", []))


@app.get("/doku", response_class=HTMLResponse)
def doku(request: Request, _=Depends(maybe_public)):
    sample = {"title": "Volkswagen: Absatz in China sinkt im dritten Quartal deutlich",
              "summary": "Der Konzern lieferte in China 15 Prozent weniger Fahrzeuge aus als im Vorjahr.",
              "source": "DER SPIEGEL", "published": "2026-10-09T08:15:00+00:00"}
    cands = [{"id": 4711, "title": "VW senkt Prognose wegen schwacher Nachfrage in China",
              "abstract": "Volkswagen hat seine Jahresprognose gesenkt.", "first": "2026-10-07T10:00:00+00:00",
              "last": "2026-10-08T16:00:00+00:00"}]
    since, until = pipeline.period()
    settings_rows = [{"s": s, "envs": ["NNIGN_" + s.key.upper(), *s.env],
                      "default": ("\n".join(s.default) if isinstance(s.default, list) else s.default)}
                     for s in config.SETTINGS]
    return templates.TemplateResponse(request, "doku.html", {
        "rules": sentiment.RULES, "pair_weight": sentiment.PAIR_WEIGHT,
        "metrics_good": sentiment.GOOD_METRICS.replace("|", ", "),
        "metrics_bad": sentiment.BAD_METRICS.replace("|", ", "),
        "up": sentiment.UP.replace("|", ", "), "down": sentiment.DOWN.replace("|", ", "),
        "margin": config.get("sentiment_neutral_margin"), "mixed": config.get("sentiment_mixed_ratio"),
        "extra_terms": config.get("lexicon_extra"),
        "system_prompt": llm.system_prompt(), "instructions": llm.instructions(),
        "prompt_custom": bool((config.get("llm_system_prompt") or "").strip() or
                              (config.get("llm_instructions") or "").strip()),
        "example_user": llm.build_user_message(sample, cands),
        "schema": json.dumps(llm.SCHEMA, ensure_ascii=False, indent=2),
        "format_hint": llm.FORMAT_HINT.strip(), "llm_status": llm.describe(),
        "providers": llm.PROVIDERS, "provider": llm.provider_name(), "model": llm.model(),
        "settings_rows": settings_rows, "cfg": config.get, "sources": sources.all_sources(),
        "since": since.isoformat(), "until": until.isoformat() if until else "",
        "default_days": pipeline.DEFAULT_PERIOD_DAYS, "breaker": pipeline.BREAKER_LIMIT,
        "translate_status": translate.describe(),
    })


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(os.path.join(HERE, "static", "manifest.webmanifest"), media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    with open(os.path.join(HERE, "static", "sw.js"), encoding="utf-8") as fh:
        js = fh.read().replace("__VERSION__", VERSION)
    return Response(js, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/offline", response_class=HTMLResponse)
def offline(request: Request):
    return templates.TemplateResponse(request, "offline.html", {})


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(HERE, "static", "icons", "favicon-32.png"), media_type="image/png")


@app.get("/healthz")
def healthz():
    db.query_one("SELECT 1")
    return {"status": "ok", "version": VERSION}


# ====================================================================== JSON-API & Export

def topic_json(t) -> dict:
    return {
        "id": t["id"], "datum": t["created_at"], "kurzthema": t["title"], "abstract": t["abstract"],
        "bewertung": t["sentiment"], "score": t["sentiment_score"], "bewertung_quelle": t["sentiment_origin"],
        "bewertung_begruendung": _json(t["sentiment_detail"]),
        "erstes_datum": t["first_published_at"], "letztes_datum": t["last_published_at"],
        "anzahl_meldungen": t["article_count"],
        "quellen": [{"medium": a["source_name"], "titel": a["title"], "url": a["url"], "datum": a["published_at"],
                     "sprache": a["lang"], "titel_original": a["title_orig"] or None,
                     "uebersetzt_von": a["translated_by"] or None}
                    for a in topic_sources(t["id"])],
    }


@app.get("/api/topics")
def api_topics(request: Request, page: int = 1, size: int = 100, order: str = "new", _=Depends(maybe_public)):
    size = max(1, min(size, 1000))
    total, rows = list_topics(_args(request), max(1, page), size, order)
    return {"total": total, "page": page, "size": size, "topics": [topic_json(t) for t in rows]}


@app.get("/api/topics/{topic_id}")
def api_topic(topic_id: int, _=Depends(maybe_public)):
    t = db.query_one("SELECT * FROM topics WHERE id=?", (topic_id,))
    if t is None:
        raise HTTPException(404, "Thema nicht gefunden")
    out = topic_json(t)
    out["meldungen"] = [dict(a) for a in db.query("SELECT id, url, title, summary, source_name, published_at, via "
                                                  "FROM articles WHERE topic_id=? ORDER BY published_at",
                                                  (topic_id,))]
    return out


@app.get("/api/stats")
def api_stats(request: Request, _=Depends(maybe_public)):
    return stats(_args(request))


@app.get("/api/timeline")
def api_timeline(request: Request, _=Depends(maybe_public)):
    return timeline(_args(request))


@app.get("/api/status")
def api_status(_=Depends(maybe_public)):
    return status_info()


@app.get("/export.csv")
def export_csv(request: Request, _=Depends(maybe_public)):
    where, params = topic_filters(**_args(request))
    rows = db.query(f"SELECT t.* FROM topics t{where} ORDER BY t.first_published_at", params)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "datum_erfasst", "erstes_datum", "kurzthema", "abstract", "bewertung", "score",
                "anzahl_meldungen", "quelle_1", "quelle_2", "quelle_3", "quelle_4", "quelle_5"])
    for t in rows:
        srcs = [f"{a['source_name']}: {a['url']}" for a in topic_sources(t["id"], 5)]
        w.writerow([t["id"], t["created_at"], t["first_published_at"], t["title"], t["abstract"], t["sentiment"],
                    t["sentiment_score"], t["article_count"], *srcs, *[""] * (5 - len(srcs))])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=nnign-themen.csv"})


# ====================================================================== Admin

TEXT_DEFAULTS = {"llm_system_prompt": prompts.DEFAULT_SYSTEM_PROMPT,
                 "llm_instructions": prompts.DEFAULT_INSTRUCTIONS}


def back(msg: str = "", anchor: str = "", info: bool = False) -> RedirectResponse:
    """Zurück in den Admin-Bereich. Aus der Seite heraus (fetch) wird daraus eine JSON-Antwort, die direkt am
    Knopf angezeigt wird (siehe Middleware); info=True: nur Auskunft, Seite nicht neu laden."""
    q = {"msg": msg} if msg else {}
    if info:
        q["info"] = "1"
    url = "/admin" + (("?" + urlencode(q)) if q else "") + (f"#{anchor}" if anchor else "")
    return RedirectResponse(url, status_code=303)


def _is_fetch(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


def respond(request: Request, msg: str, ok: bool = True, info: bool = True, **extra):
    """Antwort einer Admin-Aktion: JSON für Aufrufe aus der Seite, sonst Umleitung mit Meldung."""
    if _is_fetch(request):
        return JSONResponse({"ok": ok, "msg": msg, "reload": not info, **extra})
    return back(msg, info=info)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, msg: str = "", _=Depends(require_admin)):
    groups: dict[str, list] = {}
    for s in config.SETTINGS:
        val = config.get(s.key)
        if s.key in TEXT_DEFAULTS and not val:
            val = TEXT_DEFAULTS[s.key]
        if val is None:
            val = ""
        if s.type == "list":
            val = "\n".join(val)
        if s.type == "secret":
            val = ("•" * 8 + val[-4:]) if val else ""
        groups.setdefault(s.group, []).append({"s": s, "value": val, "origin": config.source_of(s.key),
                                               "env": config.env_names(s)[0]})
    runs = db.query("SELECT id, kind, started_at, finished_at, status, fetched, candidates, new_articles, "
                    "new_topics, llm_calls FROM runs ORDER BY id DESC LIMIT 25")
    failed = db.query("SELECT t.*, s.name FROM backfill_tasks t LEFT JOIN sources s ON s.id=t.source_id "
                      "WHERE t.status IN ('fehler','übersprungen') ORDER BY t.done_at DESC LIMIT 30")
    since, until = pipeline.period()
    span = db.query_one("SELECT MIN(published_at) a, MAX(published_at) b, COUNT(*) n FROM articles WHERE relevant=1")
    return templates.TemplateResponse(request, "admin.html", {
        "groups": groups, "sources": sources.all_sources(), "runs": runs, "msg": msg,
        "status": status_info(), "failed": failed,
        "period": {"since": since.isoformat(), "until": until.isoformat() if until else "",
                   "origin": config.source_of("collect_since"), "data_from": span["a"], "data_to": span["b"],
                   "data_n": span["n"]},
        "backfill_enabled": config.get("backfill_enabled"),
        "sessions": [{**dict(r), "device": auth.describe_agent(r["user_agent"])} for r in auth.sessions()],
        "current_session": (auth.current_session(request) or {"id": None})["id"],
        "session_days": auth.LIFETIME_DAYS, "session_max": auth.MAX_SESSIONS,
        "engines": engines_info(),
        "jobs": maintenance.jobs(), "backends": maintenance.backends(), "states": maintenance.STATES,
        "source_counts": {r["d"]: r["n"] for r in db.query(
            "SELECT source_domain d, COUNT(*) n FROM articles WHERE relevant=1 GROUP BY source_domain")},
        "llm_name": llm.describe(), "translate_name": translate.describe(), "zs_name": classify.zs_status(),
    })


@app.post("/admin/settings")
async def admin_settings(request: Request, _=Depends(require_admin)):
    form = await request.form()
    changed = 0
    bools_in_form = set(form.getlist("__bools"))  # nur Checkboxen, die das Formular wirklich enthält
    for s in config.SETTINGS:
        if s.type == "bool":
            if s.key not in bools_in_form:
                continue
            new = form.get(s.key) == "on"
        elif s.key in form:
            new = form.get(s.key)
            if s.type == "secret":
                continue                      # API-Keys nur über .env
        else:
            continue
        if s.key in TEXT_DEFAULTS and str(new).strip().replace("\r\n", "\n") == TEXT_DEFAULTS[s.key].strip():
            if config.source_of(s.key) == "admin":
                config.reset(s.key)   # unveränderter Standardtext wird nicht gespeichert
                changed += 1
            continue
        try:
            parsed = config.parse_value(s, new)
        except (ValueError, TypeError):
            return respond(request, f"Ungültiger Wert für „{s.label}“: {new}", ok=False)
        if parsed != config.get(s.key):
            config.set(s.key, parsed)
            changed += 1
    pipeline._wake.set()
    return back(f"{changed} Einstellung(en) gespeichert", "einstellungen")


@app.post("/admin/settings/reset/{key}")
def admin_setting_reset(key: str, _=Depends(require_admin)):
    if key in config.BY_KEY:
        config.reset(key)
    return back("Auf Standard zurückgesetzt", "einstellungen")


def _source_form(form) -> tuple:
    name = (form.get("name") or "").strip()
    domain = (form.get("domain") or "").strip().lower().removeprefix("www.")
    if not name or not domain:
        raise HTTPException(400, "Name und Domain sind Pflicht")
    return (name, domain, (form.get("feeds") or "").strip(), 1 if form.get("use_search") == "on" else 0,
            (form.get("archive_template") or "").strip(), form.get("archive_step") or "day",
            (form.get("archive_since") or "").strip(), (form.get("link_pattern") or "").strip(),
            form.get("language") if form.get("language") in sources.LANGUAGES else "de",
            1 if form.get("enabled") == "on" else 0)


@app.post("/admin/sources")
async def admin_source_add(request: Request, _=Depends(require_admin)):
    vals = _source_form(await request.form())
    with db.tx() as conn:
        conn.execute("INSERT INTO sources(name, domain, feeds, use_search, archive_template, archive_step, "
                     "archive_since, link_pattern, language, enabled, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (*vals, db.utcnow()))
    return back("Quelle angelegt – Backfill ggf. neu planen", "quellen")


@app.post("/admin/sources/{sid}")
async def admin_source_update(sid: int, request: Request, _=Depends(require_admin)):
    vals = _source_form(await request.form())
    with db.tx() as conn:
        conn.execute("UPDATE sources SET name=?, domain=?, feeds=?, use_search=?, archive_template=?, "
                     "archive_step=?, archive_since=?, link_pattern=?, language=?, enabled=? WHERE id=?", (*vals, sid))
    return back("Quelle gespeichert", "quellen")


@app.post("/admin/sources/{sid}/toggle")
def admin_source_toggle(sid: int, _=Depends(require_admin)):
    row = db.query_one("SELECT name, enabled FROM sources WHERE id=?", (sid,))
    if row is None:
        raise HTTPException(404, "Quelle nicht gefunden")
    with db.tx() as conn:
        conn.execute("UPDATE sources SET enabled=? WHERE id=?", (0 if row["enabled"] else 1, sid))
    return back(f"„{row['name']}“ {'deaktiviert' if row['enabled'] else 'aktiviert – wird automatisch nachgeholt'}")


@app.post("/admin/sources/{sid}/delete")
def admin_source_delete(sid: int, _=Depends(require_admin)):
    with db.tx() as conn:
        conn.execute("DELETE FROM backfill_tasks WHERE source_id=?", (sid,))
        conn.execute("DELETE FROM sources WHERE id=?", (sid,))
    return back("Quelle gelöscht (bereits gespeicherte Meldungen bleiben erhalten)", "quellen")


@app.post("/admin/scan")
def admin_scan(_=Depends(require_admin)):
    pipeline.request_scan("manuell")
    return back("Scan gestartet – Fortschritt unter „Abruf“", info=True)


@app.post("/admin/initial")
def admin_initial(_=Depends(require_admin)):
    pipeline.request_scan("initial")
    return back("Initial-Scan angestoßen")


@app.post("/admin/backfill/plan")
def admin_backfill_plan(_=Depends(require_admin)):
    n = pipeline.plan_backfill()
    return back(f"{n} neue Backfill-Aufgaben angelegt", "backfill")


@app.post("/admin/backfill/reset")
def admin_backfill_reset(all: str = Form(""), _=Depends(require_admin)):  # noqa: A002
    n = pipeline.reset_backfill(only_failed=(all != "1"))
    return back(f"{n} Aufgaben wieder auf „offen“ gesetzt", "backfill")


def _date_or_none(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, f"Ungültiges Datum: {raw}")


@app.post("/admin/data/period")
def admin_period(since: str = Form(...), until: str = Form(""), purge: str = Form(""), _=Depends(require_admin)):
    d_since, d_until = _date_or_none(since), _date_or_none(until)
    if d_since is None:
        return back("Bitte ein Startdatum angeben", "daten")
    if d_until and d_until < d_since:
        return back("Das Enddatum liegt vor dem Startdatum", "daten")
    r = pipeline.apply_period(d_since, d_until, purge == "on")
    msg = (f"Erfassungszeitraum gesetzt: {d_since:%d.%m.%Y} bis {f'{d_until:%d.%m.%Y}' if d_until else 'laufend'}. "
           f"{r['planned']} Abrufaufgaben neu eingeplant, {r['revived']} reaktiviert, {r['stopped']} gestoppt")
    if purge == "on":
        msg += f"; {r['articles']} Meldungen und {r['topics']} Themen außerhalb gelöscht"
    return back(msg + ".", "daten")


@app.post("/admin/data/delete-range")
def admin_delete_range(d_from: str = Form(""), d_to: str = Form(""), confirm: str = Form(""),
                       _=Depends(require_admin)):
    a, b = _date_or_none(d_from), _date_or_none(d_to)
    if not (a or b):
        return back("Bitte mindestens ein Datum angeben", "daten")
    if confirm != "on":
        return back("Bitte das Löschen bestätigen (Häkchen)", "daten")
    r = pipeline.delete_range(a, b)
    return back(f"{r['articles']} Meldungen gelöscht, {r['topics']} Themen entfallen.", "daten")


@app.post("/admin/data/wipe")
def admin_wipe(word: str = Form(""), factory: str = Form(""), _=Depends(require_admin)):
    if word.strip().upper() != "LÖSCHEN":
        return back("Zur Sicherheit bitte LÖSCHEN eintippen", "daten")
    threading.Thread(target=pipeline.wipe, kwargs={"factory": factory == "on"}, daemon=True).start()
    return back("Alle Daten werden gelöscht; anschließend startet automatisch ein neuer Initial-Scan"
                + (" mit Werkseinstellungen." if factory == "on" else "."), "daten")


@app.post("/admin/backfill/toggle")
def admin_backfill_toggle(_=Depends(require_admin)):
    new = not config.get("backfill_enabled")
    config.set("backfill_enabled", new)
    return back("Backfill " + ("fortgesetzt" if new else "angehalten"), "daten")


@app.post("/admin/reclassify")
def admin_reclassify(_=Depends(require_admin)):
    if pipeline.STATUS.get("reclassify"):
        return back("Neu-Bewertung läuft bereits")
    threading.Thread(target=pipeline.reclassify, name="reclassify", daemon=True).start()
    return back("Neuberechnung gestartet (Regeln und lernendes Modell; KI- und manuell bewertete Themen bleiben) – "
                "Fortschritt unter „Hintergrundarbeiten“", info=True)


@app.get("/fragment/admin-status", response_class=HTMLResponse)
def fragment_admin_status(request: Request, _=Depends(require_admin)):
    """Live-Teil des Admin-Status (Abruf, Historie, Hintergrundarbeiten)."""
    jobs = maintenance.jobs()
    resp = templates.TemplateResponse(request, "_admin_status.html", {"status": status_info(), "jobs": jobs,
                                                                      "backends": maintenance.backends(),
                                                                      "states": maintenance.STATES})
    busy = any(j["state"] in ("run", "queued") for j in jobs)
    resp.headers["X-Busy"] = "1" if busy else "0"
    return resp


@app.post("/admin/suggestions/{sid}/run")
def admin_suggestion_run(sid: str, request: Request, _=Depends(require_admin)):
    msg = maintenance.run_suggestion(sid)
    return _back_to(request, msg)


@app.post("/admin/suggestions/{sid}/dismiss")
def admin_suggestion_dismiss(sid: str, request: Request, _=Depends(require_admin)):
    maintenance.dismiss(sid)
    return _back_to(request, "Vorschlag ausgeblendet")


def _back_to(request: Request, msg: str) -> RedirectResponse:
    ref = request.headers.get("referer") or ""
    if urlsplit(ref).path == "/":
        return RedirectResponse("/?" + urlencode({"msg": msg}), status_code=303)
    return back(msg)


def engines_info() -> dict:
    counts = {r["o"]: r["n"] for r in db.query(
        "SELECT sentiment_origin o, COUNT(*) n FROM topics WHERE hidden=0 GROUP BY sentiment_origin")}
    return {
        "choice": config.get("sentiment_engine"),
        "rows": [
            {"key": "llm", "name": classify.NAMES["llm"], "status": llm.describe() + (
                f" – {llm.blocked_reason()}" if llm.blocked_reason() else ""), "n": counts.get("llm", 0),
             "allowed": classify.allowed("llm"), "cost": "je nach Anbieter (Gemini/Groq kostenlos, Ollama lokal)"},
            {"key": "zeroshot", "name": classify.NAMES["zeroshot"], "status": classify.zs_status(),
             "n": counts.get("zeroshot", 0), "allowed": classify.allowed("zeroshot"),
             "cost": "kostenloses Kontingent bei Hugging Face", "open": maintenance.zeroshot_open()},
            {"key": "lernend", "name": classify.NAMES["lernend"], "status": classify.learned_status(),
             "n": counts.get("lernend", 0), "allowed": classify.allowed("lernend"), "cost": "kostenlos, offline"},
            {"key": "regeln", "name": classify.NAMES["regeln"], "status": "immer aktiv (Rückfall)",
             "n": counts.get("lexikon", 0), "allowed": True, "cost": "kostenlos, offline"},
        ],
        "manual": counts.get("manuell", 0),
    }


@app.post("/admin/learned-train")
def admin_learned_train(request: Request, _=Depends(require_admin)):
    info = classify.train()
    if classify.allowed("lernend"):
        config.state_set("pending_reclassify", True)
    acc = f"{info['accuracy'] * 100:.0f} %" if info["accuracy"] is not None else "– (zu wenige Beispiele)"
    return respond(request, f"Trainiert: {info['n']} Beispiele, Genauigkeit {acc}. " + classify.learned_status(),
                   info=False)


@app.post("/admin/zeroshot-test")
def admin_zeroshot_test(request: Request, _=Depends(require_admin)):
    msg = classify.test_zeroshot()
    return respond(request, msg, ok=msg.startswith("OK"))


@app.post("/admin/classify-test", response_class=HTMLResponse)
def admin_classify_test(request: Request, title: str = Form(...), body: str = Form(""), _=Depends(require_admin)):
    results = classify.compare(title.strip(), body.strip())
    return templates.TemplateResponse(request, "classify_test.html", {
        "title": title, "body": body, "results": results, "names": classify.NAMES})


@app.post("/admin/regroup")
def admin_regroup(_=Depends(require_admin)):
    if pipeline.STATUS.get("regroup"):
        return back("Neu-Bündelung läuft bereits")
    threading.Thread(target=pipeline.regroup, name="regroup", daemon=True).start()
    return back("Neu-Bündelung gestartet (von Hand gebündelte, gesperrte und KI-Themen bleiben) – Fortschritt unter "
                "„Hintergrundarbeiten“", info=True)


@app.post("/admin/llm-models")
def admin_llm_models(request: Request, _=Depends(require_admin)):
    ids, err = llm.list_models()
    if err:
        return respond(request, "Modelle: " + err, ok=False)
    text_models = [i for i in ids if not any(x in i for x in ("embed", "image", "tts", "audio", "vision", "aqa"))]
    if _is_fetch(request):
        return respond(request, f"{len(text_models)} Modelle bei {llm.provider_name()} – zum Übernehmen anklicken, "
                       "dann speichern:", models=text_models[:80], target="f-llm_model")
    return back(f"{len(text_models)} Modelle bei {llm.provider_name()}: " + ", ".join(text_models[:40]), info=True)


@app.post("/admin/llm-test")
def admin_llm_test(request: Request, _=Depends(require_admin)):
    msg = llm.test_connection()
    return respond(request, msg, ok=msg.startswith("OK"))


@app.post("/admin/translate-test")
def admin_translate_test(request: Request, _=Depends(require_admin)):
    msg = translate.test_connection()
    return respond(request, msg, ok=msg.startswith("OK"))


@app.post("/admin/translate-pending")
def admin_translate_pending(_=Depends(require_admin)):
    n = pipeline.translate_pending(limit=500)
    return back(f"{n} Meldungen nachübersetzt" if n else "Nichts nachzuübersetzen bzw. kein Übersetzer verfügbar – "
                + translate.describe())


@app.post("/admin/password")
def admin_password(request: Request, new: str = Form(...), _=Depends(require_admin)):
    if len(new) < 8:
        return back("Passwort muss mindestens 8 Zeichen haben")
    set_admin_password(new)
    n = auth.revoke_others(request)    # andere Geräte müssen sich neu anmelden
    return back("Passwort geändert" + (f" – {n} andere Geräte abgemeldet" if n else ""))


@app.get("/admin/runs/{run_id}", response_class=HTMLResponse)
def admin_run(request: Request, run_id: int, _=Depends(require_admin)):
    r = db.query_one("SELECT * FROM runs WHERE id=?", (run_id,))
    if r is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "run.html", {"r": r})


@app.post("/admin/topic/{topic_id}")
def admin_topic_edit(topic_id: int, title: str = Form(...), abstract: str = Form(""),
                     sentiment: str = Form(...), hidden: str = Form(""), _=Depends(require_admin)):
    if sentiment not in ("positiv", "neutral", "negativ"):
        raise HTTPException(400)
    with db.tx() as conn:
        conn.execute("UPDATE topics SET title=?, abstract=?, sentiment=?, sentiment_origin='manuell', locked=1, "
                     "hidden=?, updated_at=? WHERE id=?",
                     (title.strip(), abstract.strip(), sentiment, 1 if hidden == "on" else 0, db.utcnow(), topic_id))
    return RedirectResponse(f"/thema/{topic_id}", status_code=303)


@app.post("/admin/topic/{topic_id}/delete")
def admin_topic_delete(topic_id: int, _=Depends(require_admin)):
    with db.tx() as conn:
        # Meldungen bleiben als "nicht relevant" gespeichert, damit sie nicht erneut auftauchen
        conn.execute("UPDATE articles SET topic_id=NULL, relevant=0 WHERE topic_id=?", (topic_id,))
        conn.execute("DELETE FROM topics WHERE id=?", (topic_id,))
    return RedirectResponse("/", status_code=303)


@app.post("/admin/topic/{topic_id}/merge")
def admin_topic_merge(topic_id: int, target: int = Form(...), _=Depends(require_admin)):
    if target == topic_id or not db.query_one("SELECT 1 FROM topics WHERE id=?", (target,)):
        raise HTTPException(400, "Ziel-Thema existiert nicht")
    topics.merge(target, topic_id)
    return RedirectResponse(f"/thema/{target}", status_code=303)


@app.post("/admin/topics/merge")
async def admin_topics_merge(request: Request, _=Depends(require_admin)):
    """Ausgewählte Themen (Auswahlmodus der Startseite) zu einem zusammenführen."""
    form = await request.form()
    try:
        ids = [int(x) for x in form.getlist("ids")]
    except ValueError:
        raise HTTPException(400, "Ungültige Auswahl")
    if len(set(ids)) < 2:
        raise HTTPException(400, "Bitte mindestens zwei Themen auswählen")
    target = topics.merge_many(ids, str(form.get("title") or ""))
    if target is None:
        raise HTTPException(400, "Themen nicht gefunden")
    back_qs = str(form.get("back") or "")
    if back_qs.startswith("?"):   # zurück in den Auswahlmodus mit denselben Filtern
        msg = f"{len(set(ids))} Themen zusammengeführt"
        return RedirectResponse("/" + back_qs + "&" + urlencode({"msg": msg}) + f"#t{target}", status_code=303)
    return RedirectResponse(f"/thema/{target}", status_code=303)


@app.post("/admin/article/{article_id}/detach")
def admin_article_detach(article_id: int, _=Depends(require_admin)):
    new_id = topics.detach_article(article_id)
    return RedirectResponse(f"/thema/{new_id}" if new_id else "/", status_code=303)


@app.middleware("http")
async def fetch_actions(request: Request, call_next):
    """Admin-Aktionen per fetch: Umleitungen in JSON umwandeln (Meldung am Knopf statt Seitensprung)."""
    resp = await call_next(request)
    if request.method == "POST" and _is_fetch(request) and resp.status_code in (302, 303):
        from urllib.parse import parse_qs
        loc = resp.headers.get("location", "")
        parts = urlsplit(loc)
        q = parse_qs(parts.query)
        msg = (q.get("msg") or [""])[0]
        if parts.path == "/admin":
            return JSONResponse({"ok": True, "msg": msg, "reload": "info" not in q})
        return JSONResponse({"ok": True, "msg": msg, "location": loc})
    return resp


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    if _wants_html(request):
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?" + urlencode({"next": nxt}), status_code=303)
    return JSONResponse({"detail": "Anmeldung erforderlich"}, status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="nnign Admin"'})


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/admin"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/admin", error: str = ""):  # noqa: A002
    if is_admin(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": error,
                                                              "days": auth.LIFETIME_DAYS, "max": auth.MAX_SESSIONS})


@app.post("/login")
def login_submit(request: Request, password: str = Form(""), next: str = Form("/admin")):  # noqa: A002
    _check_origin(request)
    ip = auth.client_ip(request)
    nxt = _safe_next(next)
    if auth.locked(ip):
        return RedirectResponse("/login?" + urlencode({"next": nxt, "error": "Zu viele Fehlversuche – bitte in "
                                                       "einigen Minuten erneut versuchen."}), status_code=303)
    if not check_password(password):
        auth.note_failure(ip)
        time.sleep(0.5)
        return RedirectResponse("/login?" + urlencode({"next": nxt, "error": "Passwort falsch."}), status_code=303)
    token = auth.create_session(request)
    resp = RedirectResponse(nxt, status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(auth.COOKIE, token, max_age=auth.LIFETIME_DAYS * 86400, httponly=True, samesite="lax",
                    secure=secure, path="/")
    return resp


@app.post("/logout")
def logout(request: Request):
    _check_origin(request)
    auth.logout(request)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.post("/admin/sessions/{sid}/revoke")
def admin_session_revoke(sid: int, _=Depends(require_admin)):
    auth.revoke(sid)
    return back("Gerät abgemeldet", "geraete")


@app.post("/admin/sessions/revoke-others")
def admin_sessions_revoke_others(request: Request, _=Depends(require_admin)):
    n = auth.revoke_others(request)
    return back(f"{n} andere Geräte abgemeldet", "geraete")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if _is_fetch(request):
        return JSONResponse({"ok": False, "msg": str(exc.detail)}, status_code=exc.status_code)
    if request.url.path.startswith("/api") or exc.status_code == 401:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p><p><a href='/'>Zur Übersicht</a></p>",
                        status_code=exc.status_code)
