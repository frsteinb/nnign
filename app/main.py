"""Webserver (FastAPI): Dashboard, JSON-API, CSV-Export und Admin-Bereich."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, llm, pipeline, sources, topics

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

HERE = os.path.dirname(__file__)
VERSION = "1.0.0"


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


security = HTTPBasic(realm="VW-Nachrichtenmonitor Admin")


def require_admin(request: Request, creds: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = hmac.compare_digest(creds.username.encode(), b"admin")
    if not (ok_user and check_password(creds.password)):
        raise HTTPException(401, "Anmeldung erforderlich", headers={"WWW-Authenticate": "Basic"})
    if request.method == "POST":  # einfacher CSRF-Schutz
        origin = request.headers.get("origin") or request.headers.get("referer")
        allowed = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
        if origin and urlsplit(origin).netloc not in allowed:
            raise HTTPException(403, "Ungültige Herkunft der Anfrage")
    return creds.username


def maybe_public(request: Request) -> None:
    if not config.get("public_dashboard"):
        creds = None
        try:
            import base64
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("basic "):
                user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                creds = (user, pw)
        except Exception:
            creds = None
        if not creds or creds[0] != "admin" or not check_password(creds[1]):
            raise HTTPException(401, "Anmeldung erforderlich", headers={"WWW-Authenticate": "Basic"})


# ====================================================================== App

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    sources.seed_sources()
    init_admin_password()
    if os.environ.get("NNIGN_DISABLE_SCHEDULER") != "1":
        pipeline.start_background()
    log.info("VW-Nachrichtenmonitor %s gestartet, Daten unter %s", VERSION, db.DB_PATH)
    yield
    pipeline.stop_background()


app = FastAPI(title="VW-Nachrichtenmonitor", version=VERSION, lifespan=lifespan)
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


# ====================================================================== Abfragen

def topic_filters(q: str = "", sentiment: str = "", year: str = "", source: str = "",
                  date_from: str = "", date_to: str = "", include_hidden: bool = False):
    where, params = [], []
    if not include_hidden:
        where.append("t.hidden=0")
    if q:
        where.append("(t.title LIKE ? OR t.abstract LIKE ? OR EXISTS (SELECT 1 FROM articles a2 WHERE "
                     "a2.topic_id=t.id AND a2.title LIKE ?))")
        params += [f"%{q}%"] * 3
    if sentiment in ("positiv", "negativ"):
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
                 "updated": "t.updated_at DESC"}.get(order, "t.first_published_at DESC")
    rows = db.query(f"SELECT t.* FROM topics t{where} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    (*params, page_size, (page - 1) * page_size))
    return total, rows


def timeline(args: dict):
    """Positive/negative Themen je Jahr (oder je Monat, wenn ein Jahr gewählt ist)."""
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
    rows = db.query(f"SELECT {key} k, SUM(t.sentiment='positiv') pos, SUM(t.sentiment='negativ') neg "
                    f"FROM topics t{where} GROUP BY k ORDER BY k", params)
    data = {r["k"]: (r["pos"] or 0, r["neg"] or 0) for r in rows}
    if year.isdigit():
        keys = [f"{year}-{m:02d}" for m in range(1, 13)]
    else:
        first = int(min(data)) if data else datetime.now().year
        start = min(first, int((config.get("backfill_since") or "2000")[:4] or 2000))
        keys = [str(y) for y in range(start, datetime.now().year + 1)]
    return [{"key": k, "pos": data.get(k, (0, 0))[0], "neg": data.get(k, (0, 0))[1]} for k in keys]


def status_info() -> dict:
    counts = db.query_one(
        "SELECT COUNT(*) n, SUM(sentiment='positiv') pos, SUM(sentiment='negativ') neg, "
        "MIN(first_published_at) first FROM topics WHERE hidden=0")
    arts = db.query_one("SELECT COUNT(*) n FROM articles WHERE relevant=1")["n"]
    last = db.query_one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    return {
        "topics": counts["n"] or 0, "positive": counts["pos"] or 0, "negative": counts["neg"] or 0,
        "earliest_topic": counts["first"], "articles": arts,
        "last_run": dict(last) if last else None,
        "scan_running": pipeline.STATUS["scan"],
        "backfill_running": pipeline.STATUS["backfill"],
        "next_scan_at": pipeline.next_scan_time().isoformat() if config.state_get("initial_done") else None,
        "initial_done": config.state_get("initial_done"),
        "backfill": pipeline.backfill_progress(),
        "llm_active": llm.available(),
        "llm_status": llm.describe(),
        "llm_calls_today": llm.calls_today(),
    }


def _args(request: Request) -> dict:
    qp = request.query_params
    return {k: (qp.get(k) or "").strip() for k in ("q", "sentiment", "year", "source", "date_from", "date_to")}


# ====================================================================== Öffentliche Seiten

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, page: int = 1, order: str = "new", _=Depends(maybe_public)):
    args = _args(request)
    page = max(1, page)
    size = int(config.get("page_size"))
    total, rows = list_topics(args, page, size, order)
    items = [{"t": t, "sources": topic_sources(t["id"])} for t in rows]
    tl = timeline(args)
    max_v = max([max(x["pos"], x["neg"]) for x in tl] + [1])
    domains = db.query("SELECT source_domain d, MIN(source_name) n, COUNT(*) c FROM articles "
                       "WHERE relevant=1 GROUP BY source_domain ORDER BY n")
    base_q = {k: v for k, v in args.items() if v}
    return templates.TemplateResponse(request, "index.html", {
        "items": items, "total": total, "page": page, "pages": max(1, -(-total // size)), "args": args,
        "order": order, "timeline": tl, "max_v": max_v, "domains": domains, "status": status_info(),
        "qs": lambda **kw: "?" + urlencode({**base_q, "order": order, **kw}),
    })


@app.get("/thema/{topic_id}", response_class=HTMLResponse)
def topic_page(request: Request, topic_id: int, _=Depends(maybe_public)):
    t = db.query_one("SELECT * FROM topics WHERE id=?", (topic_id,))
    if t is None:
        raise HTTPException(404, "Thema nicht gefunden")
    arts = db.query("SELECT * FROM articles WHERE topic_id=? ORDER BY published_at", (topic_id,))
    return templates.TemplateResponse(request, "topic.html", {"t": t, "articles": arts,
                                                              "sources": topic_sources(topic_id)})


@app.get("/healthz")
def healthz():
    db.query_one("SELECT 1")
    return {"status": "ok", "version": VERSION}


# ====================================================================== JSON-API & Export

def topic_json(t) -> dict:
    return {
        "id": t["id"], "datum": t["created_at"], "kurzthema": t["title"], "abstract": t["abstract"],
        "bewertung": t["sentiment"], "score": t["sentiment_score"], "bewertung_quelle": t["sentiment_origin"],
        "erstes_datum": t["first_published_at"], "letztes_datum": t["last_published_at"],
        "anzahl_meldungen": t["article_count"],
        "quellen": [{"medium": a["source_name"], "titel": a["title"], "url": a["url"], "datum": a["published_at"]}
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
                    headers={"Content-Disposition": "attachment; filename=vw-nachrichtenthemen.csv"})


# ====================================================================== Admin

def back(msg: str = "", anchor: str = "") -> RedirectResponse:
    url = "/admin" + (("?" + urlencode({"msg": msg})) if msg else "") + (f"#{anchor}" if anchor else "")
    return RedirectResponse(url, status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, msg: str = "", _=Depends(require_admin)):
    groups: dict[str, list] = {}
    for s in config.SETTINGS:
        val = config.get(s.key)
        if s.type == "list":
            val = "\n".join(val)
        if s.type == "secret":
            val = ("•" * 8 + val[-4:]) if val else ""
        groups.setdefault(s.group, []).append({"s": s, "value": val, "origin": config.source_of(s.key)})
    runs = db.query("SELECT id, kind, started_at, finished_at, status, fetched, candidates, new_articles, "
                    "new_topics, llm_calls FROM runs ORDER BY id DESC LIMIT 25")
    failed = db.query("SELECT t.*, s.name FROM backfill_tasks t LEFT JOIN sources s ON s.id=t.source_id "
                      "WHERE t.status IN ('fehler','übersprungen') ORDER BY t.done_at DESC LIMIT 30")
    return templates.TemplateResponse(request, "admin.html", {
        "groups": groups, "sources": sources.all_sources(), "runs": runs, "msg": msg,
        "status": status_info(), "failed": failed,
    })


@app.post("/admin/settings")
async def admin_settings(request: Request, _=Depends(require_admin)):
    form = await request.form()
    changed = 0
    for s in config.SETTINGS:
        if s.type == "bool":
            new = form.get(s.key) == "on"
        elif s.key in form:
            new = form.get(s.key)
            if s.type == "secret" and (not new or str(new).startswith("•")):
                continue
        else:
            continue
        try:
            parsed = config.parse_value(s, new)
        except (ValueError, TypeError):
            return back(f"Ungültiger Wert für „{s.label}“")
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
            1 if form.get("enabled") == "on" else 0)


@app.post("/admin/sources")
async def admin_source_add(request: Request, _=Depends(require_admin)):
    vals = _source_form(await request.form())
    with db.tx() as conn:
        conn.execute("INSERT INTO sources(name, domain, feeds, use_search, archive_template, archive_step, "
                     "archive_since, link_pattern, enabled, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (*vals, db.utcnow()))
    return back("Quelle angelegt – Backfill ggf. neu planen", "quellen")


@app.post("/admin/sources/{sid}")
async def admin_source_update(sid: int, request: Request, _=Depends(require_admin)):
    vals = _source_form(await request.form())
    with db.tx() as conn:
        conn.execute("UPDATE sources SET name=?, domain=?, feeds=?, use_search=?, archive_template=?, "
                     "archive_step=?, archive_since=?, link_pattern=?, enabled=? WHERE id=?", (*vals, sid))
    return back("Quelle gespeichert", "quellen")


@app.post("/admin/sources/{sid}/delete")
def admin_source_delete(sid: int, _=Depends(require_admin)):
    with db.tx() as conn:
        conn.execute("DELETE FROM backfill_tasks WHERE source_id=?", (sid,))
        conn.execute("DELETE FROM sources WHERE id=?", (sid,))
    return back("Quelle gelöscht (bereits gespeicherte Meldungen bleiben erhalten)", "quellen")


@app.post("/admin/scan")
def admin_scan(_=Depends(require_admin)):
    pipeline.request_scan("manuell")
    return back("Scan angestoßen – Fortschritt siehe Status")


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


@app.post("/admin/llm-test")
def admin_llm_test(_=Depends(require_admin)):
    return back("KI-Test: " + llm.test_connection(), "einstellungen")


@app.post("/admin/password")
def admin_password(new: str = Form(...), _=Depends(require_admin)):
    if len(new) < 8:
        return back("Passwort muss mindestens 8 Zeichen haben")
    set_admin_password(new)
    return back("Passwort geändert – bitte neu anmelden")


@app.get("/admin/runs/{run_id}", response_class=HTMLResponse)
def admin_run(request: Request, run_id: int, _=Depends(require_admin)):
    r = db.query_one("SELECT * FROM runs WHERE id=?", (run_id,))
    if r is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "run.html", {"r": r})


@app.post("/admin/topic/{topic_id}")
def admin_topic_edit(topic_id: int, title: str = Form(...), abstract: str = Form(""),
                     sentiment: str = Form(...), hidden: str = Form(""), _=Depends(require_admin)):
    if sentiment not in ("positiv", "negativ"):
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


@app.post("/admin/article/{article_id}/detach")
def admin_article_detach(article_id: int, _=Depends(require_admin)):
    new_id = topics.detach_article(article_id)
    return RedirectResponse(f"/thema/{new_id}" if new_id else "/", status_code=303)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api") or exc.status_code == 401:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p><p><a href='/'>Zur Übersicht</a></p>",
                        status_code=exc.status_code)
