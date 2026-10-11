"""Selbstpflege: erkennt geänderte Grundlagen und zieht den Datenbestand nach.

Grundlagen sind Einstellungen, Regeln, Quellen, Zeitraum, Übersetzer und KI-Anbindung. Ändert sich
eine davon (im Admin-Bereich, per Umgebungsvariable oder mit einem neuen Image), passiert Folgendes:

- Günstiges geschieht automatisch: Bewertungen neu berechnen, nachübersetzen, neue/geänderte Quellen
  nachholen, Zeitraum abgleichen, Themen neu bündeln (wenn es voraussichtlich nur kurz dauert), KI-
  Nachbewertung von Meldungen, bei denen die KI ausgefallen war, und von stark gewachsenen Themen.
- Aufwendiges wird dem Admin oben als Vorschlag mit Aufwandsschätzung angeboten: großes Neu-Bündeln,
  Historie mit neuen Suchbegriffen neu durchsuchen, nicht mehr passende Meldungen ausblenden, ältere
  Themen von der KI nachbewerten.

KI-Arbeiten im Hintergrund nutzen nur einen Teil des Tageslimits (llm_rerate_share), damit neue
Meldungen immer Vorrang haben. Nach Limits oder Störungen geht es automatisch weiter, sobald möglich.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from . import config, db, hubs, llm, sentiment, topics, translate

log = logging.getLogger(__name__)

AUTO_REGROUP_SECONDS = 180      # Neu-Bündeln automatisch, wenn voraussichtlich kürzer
AUTO_RERATE_FP = 30             # nach Prompt-/Modellwechsel automatisch nachbewerten, wenn so wenige Themen
RECENT_DAYS = 30                # unbewertete Themen der letzten Tage bewertet die KI automatisch nach
STATUS: dict = {"rerate": None, "last_check": None}


def _h(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dur(secs: float) -> str:
    if secs < 90:
        return "unter 2 Minuten"
    if secs < 5400:
        return f"etwa {round(secs / 60)} Minuten"
    if secs < 172800:
        return f"etwa {secs / 3600:.1f} Stunden".replace(".", ",")
    return f"etwa {round(secs / 86400)} Tage"


# ---------------------------------------------------------------------------------- Grundlagen

def bases() -> dict:
    from . import pipeline
    since, until = pipeline.period()
    srcs = {str(r["id"]): _h(r["feeds"], r["archive_template"], r["archive_step"], r["archive_since"],
                             r["link_pattern"], r["language"], r["use_search"], r["enabled"])
            for r in db.query("SELECT * FROM sources")}
    return {
        "rules": _h(sentiment.fingerprint(), config.get("sentiment_neutral_margin"),
                    config.get("sentiment_mixed_ratio"), config.get("lexicon_extra")),
        "topics": _h(topics.ALGO_VERSION, config.get("cluster_threshold"), config.get("cluster_window_days"),
                     config.get("cluster_max_span_days")),
        "relevance": _h(config.get("keywords_strong"), config.get("keywords_brands"), config.get("keywords_exclude"),
                        config.get("include_brands"), config.get("min_summary_hits")),
        "search": _h(config.get("search_query"), config.get("search_query_zh"), config.get("search_enabled"),
                     config.get("backfill_search_window_days")),
        "period": _h(since, until),
        "sources": srcs,
        "llm": llm.fingerprint() if llm.available() else "",
        "translate": translate.provider(),
        "hubs": hubs.VERSION,
    }


def _stored() -> dict:
    return config.state_get("bases") or {}


def _accept(key: str, value) -> None:
    b = _stored()
    b[key] = value
    config.state_set("bases", b)


# ---------------------------------------------------------------------------------- Vorschläge

def suggestions() -> list[dict]:
    return list((config.state_get("suggestions") or {}).values())


def _suggest(sid: str, title: str, text: str, estimate: str, basis_key: str, basis) -> None:
    sug = config.state_get("suggestions") or {}
    sug[sid] = {"id": sid, "title": title, "text": text, "estimate": estimate, "basis_key": basis_key,
                "basis": basis, "created": db.utcnow()}
    config.state_set("suggestions", sug)


def _drop(sid: str) -> dict | None:
    sug = config.state_get("suggestions") or {}
    item = sug.pop(sid, None)
    config.state_set("suggestions", sug)
    return item


def dismiss(sid: str) -> None:
    item = _drop(sid)
    if item and item.get("basis_key"):
        _accept(item["basis_key"], item["basis"])


def run_suggestion(sid: str) -> str:
    """Vom Admin bestätigten Vorschlag ausführen (schwere Arbeit läuft im Hintergrund)."""
    from . import pipeline
    item = _drop(sid)
    if not item:
        return "Vorschlag nicht mehr vorhanden"
    if item.get("basis_key"):
        _accept(item["basis_key"], item["basis"])
    if sid == "regroup":
        config.state_set("pending_regroup", True)
    elif sid == "relevance_cleanup":
        n = relevance_cleanup()
        return f"{n} Meldungen ausgeblendet"
    elif sid == "rescan":
        with db.tx() as conn:
            n = conn.execute("UPDATE backfill_tasks SET status='offen', attempts=0, error='' "
                             "WHERE status='ok' AND method!='wayback_index'").rowcount
        pipeline._wake.set()
        return f"{n} Abrufe neu eingeplant"
    elif sid == "search_replan":
        return search_replan()
    elif sid == "llm_rerate_old":
        config.state_set("llm_rerate_old", True)
    elif sid == "llm_rerate_fp":
        config.state_set("llm_rerate_fp", llm.fingerprint())
    pipeline._wake.set()
    return "gestartet – Fortschritt unter „Läuft gerade“"


# ---------------------------------------------------------------------------------- Prüfung

def _offered(key: str, basis) -> bool:
    """Gibt es schon einen offenen Vorschlag zu genau diesem Stand? (dann nicht erneut rechnen)"""
    return any(s.get("basis_key") == key and s.get("basis") == basis for s in suggestions())


def decide_regroup(note: str) -> str | None:
    """Neu bündeln: automatisch, wenn es kurz dauert, sonst als Vorschlag. Gibt eine Notiz zurück."""
    n_articles = db.query_one("SELECT COUNT(*) n FROM articles WHERE relevant=1")["n"]
    if not n_articles:
        return None
    secs = n_articles * float(config.state_get("regroup_rate") or 0.005)
    if secs <= AUTO_REGROUP_SECONDS:
        config.state_set("pending_regroup", True)
        return note + " – Themen werden neu gebündelt"
    _suggest("regroup", "Themen neu bündeln",
             f"{note}. Neu bündeln ordnet die {n_articles} Meldungen automatisch gebildeter Themen neu zu (von Hand "
             "gebündelte, gesperrte und KI-Themen bleiben).", f"{_dur(secs)} Rechenzeit, keine Abrufe",
             "topics", bases()["topics"])
    return note + " – Neu-Bündeln wird dem Admin vorgeschlagen"

def check(startup: bool = False) -> list[str]:
    """Grundlagen vergleichen; Automatisches anstoßen, Aufwendiges vorschlagen. Gibt Notizen zurück."""
    from . import pipeline
    notes: list[str] = []
    cur, old = bases(), _stored()
    first = not old
    has_data = bool(db.query_one("SELECT 1 FROM articles LIMIT 1"))

    if first:   # Erststart oder Update von einer Version ohne diese Prüfung: aktuellen Stand übernehmen
        config.state_set("bases", cur)       # (Regel- und Verfahrens-Updates erkennt upgrade_on_start)
        old = dict(cur)

    # Bewertungsregeln -> automatisch neu berechnen (schnell, nur Regeln)
    if cur["rules"] != old.get("rules"):
        if has_data:
            config.state_set("pending_reclassify", True)
            notes.append("Bewertungsregeln oder -einstellungen geändert – Bewertungen werden neu berechnet")
        _accept("rules", cur["rules"])

    # Themenbildung -> neu bündeln (automatisch, wenn kurz; sonst Vorschlag)
    if cur["topics"] != old.get("topics") and not _offered("topics", cur["topics"]):
        note = decide_regroup("Schwelle, Zeitfenster oder Verfahren der Themenbildung geändert")
        if note:
            notes.append(note)
        if not _offered("topics", cur["topics"]):
            _accept("topics", cur["topics"])

    # Relevanzbegriffe -> nicht mehr passende Meldungen und erneutes Durchsuchen vorschlagen
    if cur["relevance"] != old.get("relevance") and not _offered("relevance", cur["relevance"]):
        if has_data:
            n = len(_no_longer_relevant())
            if n:
                _suggest("relevance_cleanup", "Nicht mehr passende Meldungen ausblenden",
                         f"Nach den geänderten Relevanzbegriffen betreffen {n} gespeicherte Meldungen den Konzern nicht "
                         "mehr. Sie würden als nicht relevant markiert und aus ihren Themen entfernt.",
                         "wenige Sekunden", "relevance", cur["relevance"])
            done = db.query_one("SELECT COUNT(*) n FROM backfill_tasks WHERE status='ok' AND method!='wayback_index'")["n"]
            if done:
                _suggest("rescan", "Historie mit den neuen Begriffen erneut auswerten",
                         "Bereits abgerufene Archiv- und Suchseiten werden erneut geladen, damit Meldungen gefunden "
                         "werden, die erst nach den neuen Begriffen relevant sind.",
                         f"{done} Abrufe, {_dur(done * _task_seconds())} im Hintergrund", "relevance", cur["relevance"])
            if not n and not done:
                _accept("relevance", cur["relevance"])
        else:
            _accept("relevance", cur["relevance"])

    # Suchbegriffe -> Historie neu durchsuchen (Vorschlag; aktuelle Scans nutzen sie sofort)
    if cur["search"] != old.get("search") and not _offered("search", cur["search"]):
        n = db.query_one("SELECT COUNT(*) n FROM backfill_tasks WHERE method='search'")["n"]
        if n and config.get("search_enabled"):
            _suggest("search_replan", "Historie mit den neuen Suchbegriffen durchsuchen",
                     "Die Suchbegriffe wurden geändert. Aktuelle Scans verwenden sie bereits; für den Erfassungszeitraum "
                     "würde die Nachrichtensuche mit den neuen Begriffen erneut durchlaufen.",
                     f"etwa {n} Suchabrufe, {_dur(n * _task_seconds())} im Hintergrund", "search", cur["search"])
        else:
            _accept("search", cur["search"])

    # Erfassungszeitraum (z. B. per Umgebungsvariable geändert) -> Abrufe abgleichen
    if cur["period"] != old.get("period"):
        if config.state_get("initial_done"):
            r = pipeline.sync_period()
            if r["planned"] or r["stopped"] or r["revived"]:
                notes.append(f"Erfassungszeitraum geändert – {r['planned']} Abrufe eingeplant, {r['stopped']} gestoppt")
        _accept("period", cur["period"])

    # Quellen: neue oder geänderte (aktive) Quellen nachholen
    if cur["sources"] != old.get("sources"):
        changed = [int(i) for i, h in cur["sources"].items() if old.get("sources", {}).get(i) != h]
        active = {r["id"] for r in db.query("SELECT id FROM sources WHERE enabled=1")}
        changed = [i for i in changed if i in active]
        if changed and config.state_get("initial_done") and not startup:
            config.state_set("pending_catchup", sorted(set(config.state_get("pending_catchup") or []) | set(changed)))
            pipeline.plan_backfill()
            notes.append(f"{len(changed)} neue oder geänderte Quelle(n) – aktueller Zeitraum wird nachgeholt")
        _accept("sources", cur["sources"])

    # Erkennung von Übersichtsseiten neu/verbessert -> gespeicherte Übersichten entfernen und auflösen
    if cur["hubs"] != old.get("hubs"):
        n = hub_cleanup() if has_data else 0
        if n:
            notes.append(f"{n} gespeicherte Übersichtsseiten erkannt – entfernt, ihre Artikel werden einzeln geladen")
        _accept("hubs", cur["hubs"])

    # Übersetzer neu verfügbar -> Nachübersetzung läuft automatisch (work())
    if cur["translate"] != old.get("translate"):
        if cur["translate"] and has_data:
            n = db.query_one("SELECT COUNT(*) n FROM articles WHERE lang!='de' AND translated_by='' AND relevant=1")["n"]
            if n:
                notes.append(f"Übersetzer verfügbar ({translate.NAMES[cur['translate']]}) – {n} Meldungen werden "
                             "nachübersetzt")
        _accept("translate", cur["translate"])

    # KI-Anbindung neu oder geändert
    if cur["llm"] != old.get("llm"):
        if cur["llm"] and has_data:
            old_unrated = _rerate_count("old")
            if old_unrated:
                _suggest("llm_rerate_old", "Ältere Themen von der KI bewerten lassen",
                         f"{old_unrated} ältere Themen sind nur regelbasiert bewertet. Die KI kann Titel, Abstract und "
                         "Tonalität nachträglich verbessern (Themen der letzten 30 Tage geschieht automatisch).",
                         _calls_estimate(old_unrated), "", None)
            changed_fp = _rerate_count("fp")
            if changed_fp and changed_fp <= AUTO_RERATE_FP:
                config.state_set("llm_rerate_fp", cur["llm"])
            elif changed_fp:
                _suggest("llm_rerate_fp", "KI-Bewertungen mit neuem Modell/Prompt erneuern",
                         f"{changed_fp} Themen wurden mit einem anderen KI-Modell oder Prompt bewertet.",
                         _calls_estimate(changed_fp), "", None)
            notes.append("KI-Anbindung verfügbar oder geändert – " + llm.describe())
        _accept("llm", cur["llm"])

    STATUS["last_check"] = db.utcnow()
    if notes:
        now = db.utcnow()
        with db.tx() as conn:
            conn.execute("INSERT INTO runs(kind, started_at, finished_at, status, log) VALUES(?,?,?,?,?)",
                         ("aktualisierung", now, now, "ok", "\n".join(notes)))
        for n in notes:
            log.info("Selbstpflege: %s", n)
        pipeline._wake.set()
    return notes


def _task_seconds() -> float:
    workers = max(1, int(config.get("backfill_workers")))
    return (float(config.get("host_delay_seconds")) + 1.0) / workers


def _calls_estimate(n: int) -> str:
    per_day = max(1, int(int(config.get("llm_max_calls_per_day")) * float(config.get("llm_rerate_share"))))
    days = n / per_day
    when = "heute" if days <= 1 else f"etwa {days:.0f} Tage".replace(".", ",")
    return f"{n} KI-Aufrufe, bei {per_day} pro Tag {when}"


# ---------------------------------------------------------------------------------- Relevanz

def _no_longer_relevant() -> list[int]:
    from .relevance import Matcher
    m = Matcher.from_config()
    out = []
    for a in db.query("SELECT a.id, a.title, a.summary FROM articles a LEFT JOIN topics t ON t.id=a.topic_id "
                      "WHERE a.relevant=1 AND (a.llm_state!='done') AND (t.id IS NULL OR (t.locked=0 AND "
                      "t.sentiment_origin!='manuell' AND t.manual_group=0))"):
        if m.score(a["title"], a["summary"] or "") <= 0:
            out.append(a["id"])
    return out


def relevance_cleanup() -> int:
    from . import pipeline
    ids = _no_longer_relevant()
    with pipeline._ingest_lock, db.tx() as conn:
        touched = set()
        for i in ids:
            row = conn.execute("SELECT topic_id FROM articles WHERE id=?", (i,)).fetchone()
            if row and row["topic_id"]:
                touched.add(row["topic_id"])
            conn.execute("UPDATE articles SET relevant=0, topic_id=NULL WHERE id=?", (i,))
        for tid in touched:
            topics.rebuild_signature(conn, tid)
            topics.refresh(conn, tid)
    return len(ids)


def search_replan() -> str:
    """Offene Suchaufgaben mit alten Begriffen stoppen, neue einplanen."""
    from . import pipeline
    with db.tx() as conn:
        conn.execute("UPDATE backfill_tasks SET status='übersprungen', error='alte Suchbegriffe' "
                     "WHERE method='search' AND status='offen'")
    n = pipeline.plan_backfill()
    pipeline._wake.set()
    return f"{n} Suchabrufe mit den neuen Begriffen eingeplant"


# ---------------------------------------------------------------------------------- Übersichtsseiten

def hub_cleanup() -> int:
    """Als Meldung gespeicherte Übersichtsseiten entfernen und zum Auflösen vormerken."""
    from . import pipeline
    rows = [r for r in db.query(
        "SELECT a.id, a.url, a.title, a.source_name, a.source_domain, a.published_at, a.lang, a.topic_id "
        "FROM articles a LEFT JOIN topics t ON t.id=a.topic_id WHERE a.relevant=1 AND "
        "(t.id IS NULL OR (t.locked=0 AND t.sentiment_origin!='manuell' AND t.manual_group=0))")
        if hubs.is_hub(r["title"], r["url"])]
    if not rows:
        return 0
    with pipeline._ingest_lock, db.tx() as conn:
        touched = set()
        for r in rows:
            conn.execute("UPDATE articles SET relevant=0, topic_id=NULL WHERE id=?", (r["id"],))
            if r["topic_id"]:
                touched.add(r["topic_id"])
        for tid in touched:
            topics.rebuild_signature(conn, tid)
            topics.refresh(conn, tid)
    queue = config.state_get("pending_hubs") or []
    queue += [{k: r[k] for k in ("url", "title", "source_name", "source_domain", "published_at", "lang")} for r in rows]
    config.state_set("pending_hubs", queue[-200:])
    return len(rows)


def expand_pending_hubs(limit: int = 10) -> int:
    from . import pipeline
    from .fetcher import Item
    queue = config.state_get("pending_hubs") or []
    if not queue:
        return 0
    batch, rest = queue[:limit], queue[limit:]
    config.state_set("pending_hubs", rest)
    items = [Item(q["url"], q["title"], "", datetime.fromisoformat(q["published_at"]), q["source_name"],
                  q["source_domain"], "übersicht", lang=q.get("lang") or "de") for q in batch]
    ctx = pipeline.Ctx("übersicht")
    pipeline._expand_hubs(items, ctx, True, None, None, None, 0)
    return len(batch)


# ---------------------------------------------------------------------------------- KI-Nachbewertung

def _rerate_where(kind: str) -> tuple[str, tuple]:
    base = "t.hidden=0 AND t.locked=0 AND t.sentiment_origin!='manuell'"
    recent = (_now() - timedelta(days=RECENT_DAYS)).isoformat()
    if kind == "pending":
        return base + " AND t.id IN (SELECT topic_id FROM articles WHERE llm_state='pending')", ()
    if kind == "grown":
        return (base + " AND t.sentiment_origin='llm' AND t.llm_rated_count>0 AND t.article_count>=2*t.llm_rated_count "
                "AND t.article_count-t.llm_rated_count>=2", ())
    if kind == "recent":
        return base + " AND t.sentiment_origin='lexikon' AND t.llm_rated_at='' AND t.last_published_at>=?", (recent,)
    if kind == "old":
        return base + " AND t.sentiment_origin='lexikon' AND t.llm_rated_at='' AND t.last_published_at<?", (recent,)
    if kind == "fp":
        return base + " AND t.sentiment_origin='llm' AND t.llm_fp!='' AND t.llm_fp!=?", (llm.fingerprint(),)
    raise ValueError(kind)


def _rerate_count(kind: str) -> int:
    where, params = _rerate_where(kind)
    return db.query_one(f"SELECT COUNT(*) n FROM topics t WHERE {where}", params)["n"]


def rerate_kinds() -> list[str]:
    kinds = ["pending", "grown", "recent"]
    if config.state_get("llm_rerate_old"):
        kinds.append("old")
    if config.state_get("llm_rerate_fp") == llm.fingerprint():
        kinds.append("fp")
    return kinds


def rerate_open() -> int:
    if not llm.available():
        return 0
    return sum(_rerate_count(k) for k in rerate_kinds())


def _share_ok() -> bool:
    limit = int(config.get("llm_max_calls_per_day"))
    return llm.calls_today() < limit * float(config.get("llm_rerate_share"))


def rerate_batch(max_calls: int = 20) -> int:
    """Einige Themen von der KI (nach)bewerten – nur mit freiem Anteil am Tageslimit."""
    from . import pipeline
    if not llm.available() or not config.get("llm_enabled"):
        return 0
    done = 0
    for kind in rerate_kinds():
        where, params = _rerate_where(kind)
        rows = db.query(f"SELECT t.* FROM topics t WHERE {where} ORDER BY t.last_published_at DESC LIMIT ?",
                        (*params, max_calls - done))
        for t in rows:
            if not llm.ready() or not _share_ok():
                return done
            STATUS["rerate"] = {"kind": kind, "title": t["title"][:80], "open": rerate_open()}
            arts = db.query("SELECT title, summary, source_name, published_at FROM articles WHERE topic_id=? "
                            "ORDER BY published_at LIMIT 8", (t["id"],))
            res = llm.rate_topic(dict(t), [dict(a) for a in arts])
            if res is None:
                continue          # Limit/Störung: llm pausiert sich selbst, später erneut
            _apply_rating(t, res)
            done += 1
            if done >= max_calls:
                break
        if done >= max_calls:
            break
    STATUS["rerate"] = None
    if done:
        log.info("KI-Nachbewertung: %d Themen", done)
        pipeline._wake.set()
    return done


def _apply_rating(t, res: dict) -> None:
    detail = json.loads(t["sentiment_detail"] or "{}")
    detail.update({"llm": res["begruendung"], "confidence": res["confidence"], "model": llm.describe(),
                   "nachbewertet": db.utcnow()})
    fields = {"sentiment": res["sentiment"], "sentiment_origin": "llm",
              "sentiment_detail": json.dumps(detail, ensure_ascii=False), "llm_rated_at": db.utcnow(),
              "llm_rated_count": t["article_count"], "llm_fp": llm.fingerprint(), "updated_at": db.utcnow()}
    if not t["title_fixed"]:
        if res["topic_title"].strip():
            fields["title"] = res["topic_title"].strip()
        if res["abstract"].strip():
            fields["abstract"] = res["abstract"].strip()
    if not res["relevant"]:
        fields["hidden"] = 1
        detail["llm"] = "KI: betrifft den Konzern nicht – ausgeblendet. " + detail["llm"]
        fields["sentiment_detail"] = json.dumps(detail, ensure_ascii=False)
    sets = ", ".join(f"{k}=?" for k in fields)
    with db.tx() as conn:
        conn.execute(f"UPDATE topics SET {sets} WHERE id=?", (*fields.values(), t["id"]))
        conn.execute("UPDATE articles SET llm_state='done' WHERE topic_id=? AND llm_state='pending'", (t["id"],))


# ---------------------------------------------------------------------------------- Arbeit

_last = {"check": 0.0, "translate": 0.0, "rerate": 0.0}


def work() -> None:
    """Von der Zeitsteuerung regelmäßig aufgerufen: prüfen und anstehende Arbeiten erledigen."""
    from . import pipeline
    now = time.time()
    if now - _last["check"] > 60:
        _last["check"] = now
        check()
    if config.state_get("pending_regroup") and not pipeline.STATUS["regroup"]:
        t0 = time.time()
        r = pipeline.regroup()
        config.state_set("pending_regroup", False)
        if r["articles"] > 50:
            config.state_set("regroup_rate", max(0.001, (time.time() - t0) / r["articles"]))
    if config.state_get("pending_reclassify") and not pipeline.STATUS["reclassify"]:
        pipeline.reclassify()
        config.state_set("pending_reclassify", False)
    if config.state_get("pending_hubs"):
        expand_pending_hubs()
    if translate.provider() and now - _last["translate"] > 300:
        _last["translate"] = now
        if pipeline.translate_pending():
            _last["translate"] = 0.0          # weiter, solange es etwas zu tun gibt
    if now - _last["rerate"] > 120 and llm.ready():
        _last["rerate"] = now
        if rerate_batch():
            _last["rerate"] = 0.0
