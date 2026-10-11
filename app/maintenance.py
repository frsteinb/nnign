"""Selbstpflege: erkennt geänderte Grundlagen und zieht den Datenbestand nach.

Grundlagen sind Einstellungen, Regeln, Quellen, Zeitraum, Übersetzer und KI-Anbindung. Ändert sich
eine davon (im Admin-Bereich, per Umgebungsvariable oder mit einem neuen Image), passiert Folgendes:

- Günstiges geschieht automatisch: Bewertungen neu berechnen, nachübersetzen, neue/geänderte Quellen
  nachholen, Zeitraum abgleichen, Themenbildung neu durchlaufen (wenn es voraussichtlich nur kurz dauert), KI-
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
import math
import time
from datetime import date, datetime, timedelta, timezone

from . import classify, config, db, gapfill, hubs, llm, refine, sentiment, topics, translate

log = logging.getLogger(__name__)

AUTO_REGROUP_SECONDS = 180      # Neu-Bündeln automatisch, wenn voraussichtlich kürzer
AUTO_RERATE_FP = 30             # nach Prompt-/Modellwechsel automatisch nachbewerten, wenn so wenige Themen
RECENT_DAYS = 30                # unbewertete Themen der letzten Tage bewertet die KI automatisch nach
STATUS: dict = {"rerate": None, "zeroshot": None, "last_check": None}


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
                        config.get("include_brands"), config.get("min_summary_hits"), config.get("keywords_partners"),
                        config.get("include_partners")),
        "search": _h(config.get("search_query"), config.get("search_query_zh"), config.get("search_enabled"),
                     config.get("backfill_search_window_days"), config.get("search_query_partners"),
                     config.get("include_partners")),
        "period": _h(since, until),
        "sources": srcs,
        "llm": llm.fingerprint() if llm.available() else "",
        "translate": translate.provider(),
        "hubs": hubs.VERSION,
        "refine": _h(refine.VERSION, config.get("backfill_wayback_refine_depth")),
        "engine": _h(config.get("sentiment_engine"), config.get("learned_min_examples"),
                     config.get("learned_min_accuracy"), config.get("learned_min_confidence")),
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
    _suggest("regroup", "Themenbildung neu durchlaufen",
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
            n_scope = rescope()
            if n_scope:
                notes.append(f"Relevanzbegriffe geändert – {n_scope} Meldungen neu als Konzern/Partner eingeordnet")
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

    # Bewertungsverfahren gewechselt -> Tonalität neu bestimmen (Regeln/lernend sofort, Zero-Shot/KI im Hintergrund)
    if cur["engine"] != old.get("engine"):
        if has_data:
            config.state_set("pending_reclassify", True)
            notes.append(f"Bewertungsverfahren: {config.get('sentiment_engine')} – Bewertungen werden neu bestimmt")
        _accept("engine", cur["engine"])

    # Verdichtung des Historie-Abrufs (neu oder geändert) -> volle Suchfenster und Wayback-Lücken nachplanen
    if cur["refine"] != old.get("refine"):
        if db.query_one("SELECT 1 FROM backfill_tasks WHERE status='ok' LIMIT 1"):
            r = refine.legacy_plan()
            if r["search"] or r["index"]:
                notes.append(f"Historie wird verdichtet: {r['search']} volle Suchfenster werden feiner abgefragt, "
                             f"{r['index']} Feeds für die Lückenfüllung per Wayback vorbereitet")
        _accept("refine", cur["refine"])

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


def rescope() -> int:
    """Einordnung Konzern/Partner für alle Meldungen und Themen neu bestimmen (schnell, ohne Abrufe)."""
    from .relevance import Matcher
    m = Matcher.from_config()
    changed = 0
    with db.tx() as conn:
        for a in conn.execute("SELECT id, title, summary, title_orig, summary_orig, scope FROM articles WHERE relevant=1"):
            sc = m.scope(a["title_orig"] or a["title"], a["summary_orig"] or a["summary"] or "")
            if sc != a["scope"]:
                conn.execute("UPDATE articles SET scope=? WHERE id=?", (sc, a["id"]))
                changed += 1
        if changed:
            conn.execute("UPDATE topics SET scope=CASE WHEN NOT EXISTS (SELECT 1 FROM articles a WHERE "
                         "a.topic_id=topics.id AND a.scope!='partner') THEN 'partner' ELSE 'konzern' END")
    return changed


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
    auto = "t.sentiment_origin IN ('lexikon','lernend','zeroshot')"
    if kind == "recent":
        return base + f" AND {auto} AND t.llm_rated_at='' AND t.last_published_at>=?", (recent,)
    if kind == "old":
        return base + f" AND {auto} AND t.llm_rated_at='' AND t.last_published_at<?", (recent,)
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
    if not llm.available() or not classify.allowed("llm"):
        return 0
    return sum(_rerate_count(k) for k in rerate_kinds())


def _share_ok() -> bool:
    limit = int(config.get("llm_max_calls_per_day"))
    return llm.calls_today() < limit * float(config.get("llm_rerate_share"))


def rerate_batch(max_calls: int = 20) -> int:
    """Einige Themen von der KI (nach)bewerten – nur mit freiem Anteil am Tageslimit."""
    from . import pipeline
    if not llm.available() or not config.get("llm_enabled") or not classify.allowed("llm"):
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


# ---------------------------------------------------------------------------------- Zero-Shot im Hintergrund

def zeroshot_active() -> bool:
    """Zero-Shot bewertet, wenn ausdrücklich gewählt – oder bei auto, solange keine KI arbeiten kann."""
    e = classify.engine()
    # bei „auto“ auch, solange die KI pausiert (Limit/Störung) – die KI bewertet später ohnehin nach
    return classify.zs_configured() and (e == "zeroshot" or (e == "auto" and not llm.ready()))


def _zs_where() -> tuple[str, tuple]:
    return ("t.hidden=0 AND t.locked=0 AND (t.sentiment_origin IN ('lexikon','lernend') AND (t.zs_fp!=? OR "
            "(t.zs_rated_count>0 AND t.article_count>=2*t.zs_rated_count AND t.article_count-t.zs_rated_count>=2)) "
            "OR (t.sentiment_origin='zeroshot' AND (t.zs_fp!=? OR (t.article_count>=2*t.zs_rated_count AND "
            "t.article_count-t.zs_rated_count>=2))))", (classify.zs_fingerprint(), classify.zs_fingerprint()))


def zeroshot_open() -> int:
    if not zeroshot_active():
        return 0
    where, params = _zs_where()
    return db.query_one(f"SELECT COUNT(*) n FROM topics t WHERE {where}", params)["n"]


def zeroshot_batch(max_calls: int = 30) -> int:
    if not zeroshot_active():
        return 0
    where, params = _zs_where()
    rows = db.query(f"SELECT t.* FROM topics t WHERE {where} ORDER BY t.last_published_at DESC LIMIT ?",
                    (*params, max_calls))
    done = 0
    for t in rows:
        if not classify.zs_ready():
            break
        STATUS["zeroshot"] = {"title": t["title"][:80]}
        arts = db.query("SELECT title FROM articles WHERE topic_id=? ORDER BY published_at LIMIT 6", (t["id"],))
        title, body = classify.topic_text(t, arts)
        res = classify.predict_zeroshot(title, body)
        if res is None:
            continue
        label, conf, scores = res
        detail = json.loads(t["sentiment_detail"] or "{}")
        detail["zeroshot"] = {"label": label, "scores": {k: round(v, 3) for k, v in scores.items()},
                              "model": config.get("zeroshot_model")}
        fields = {"zs_rated_count": t["article_count"], "zs_fp": classify.zs_fingerprint(),
                  "sentiment_detail": json.dumps(detail, ensure_ascii=False)}
        if conf >= float(config.get("zeroshot_min_confidence")):
            fields.update(sentiment=label, sentiment_origin="zeroshot", updated_at=db.utcnow())
        elif t["sentiment_origin"] == "zeroshot":
            fields["sentiment_origin"] = "lexikon"       # unsicher geworden -> Regeln/lernend übernehmen
        sets = ", ".join(f"{k}=?" for k in fields)
        with db.tx() as conn:
            conn.execute(f"UPDATE topics SET {sets} WHERE id=?", (*fields.values(), t["id"]))
            if fields.get("sentiment_origin") == "lexikon":
                topics.refresh(conn, t["id"])
        done += 1
    STATUS["zeroshot"] = None
    if done:
        log.info("Zero-Shot-Bewertung: %d Themen", done)
    return done


# ---------------------------------------------------------------------------------- Status für den Admin

def _pct(done, total):
    return round(100 * done / total) if total else None


# ---------------------------------------------------------------------------------- Datenlage je Jahr

def coverage_by_year() -> list[dict]:
    """Wie vollständig ist jedes Jahr erfasst? Meldungen, Herkunft, Stand des Historie-Abrufs und eine
    grobe Einstufung (gut / mittel / dünn) relativ zu den am besten erfassten Jahren."""
    from . import pipeline
    since, until = pipeline.period()
    rows = {r["y"]: dict(r) for r in db.query(
        "SELECT substr(published_at,1,4) y, COUNT(*) n, SUM(via IN ('rss','suche')) live, SUM(via='archiv') archiv, "
        "SUM(via='wayback') wayback, SUM(via='übersicht') hub FROM articles WHERE relevant=1 GROUP BY y")}
    tasks = {r["y"]: dict(r) for r in db.query(
        "SELECT substr(period_start,1,4) y, COUNT(*) total, SUM(status='offen') open, SUM(status='ok') ok, "
        "SUM(status='fehler') failed, SUM(depth>0) refined FROM backfill_tasks WHERE method!='wayback_index' GROUP BY y")}
    years = range(since.year, (until or _now().date()).year + 1)
    counts = sorted((rows.get(str(y), {}).get("n", 0) for y in years), reverse=True)
    top = counts[: max(1, len(counts) // 4)]
    ref = max(1.0, sum(top) / len(top)) if top else 1.0
    gap = gapfill.months_state()
    if gap["target"]:                                 # Maßstab: Meldungsdichte der aktuellen Lage
        ref = max(1.0, gap["target"] * 12.0)
    gap_open: dict[str, int] = {}
    for m in gap["months"]:
        if m["state"] == "offen":
            gap_open[m["month"][:4]] = gap_open.get(m["month"][:4], 0) + 1
    out = []
    today = _now().date()
    for y in years:
        r = rows.get(str(y), {"n": 0, "live": 0, "archiv": 0, "wayback": 0, "hub": 0})
        t = tasks.get(str(y), {"total": 0, "open": 0, "ok": 0, "failed": 0, "refined": 0})
        n = r["n"] or 0
        span = 1.0
        if y == today.year:                           # laufendes Jahr hochrechnen
            span = max(0.05, (today - date(y, 1, 1)).days / 365)
        if y == since.year:
            span = min(span, max(0.05, (date(y, 12, 31) - since).days / 365))
        ratio = (n / span) / ref
        level = "gut" if ratio >= 0.6 else ("mittel" if ratio >= 0.25 else "dünn")
        out.append({"year": y, "articles": n, "level": level, "ratio": round(min(ratio, 1.0), 2),
                    "open": t["open"] or 0, "ok": t["ok"] or 0, "failed": t["failed"] or 0, "total": t["total"] or 0,
                    "refined": t["refined"] or 0,
                    "sources": {k: r.get(k) or 0 for k in ("live", "archiv", "wayback", "hub")},
                    "gap_months": gap_open.get(str(y), 0),
                    "growing": bool(t["open"] or gap_open.get(str(y)))})
    return out


# ---------------------------------------------------------------------------------- Arbeiten-Übersicht

STATES = {   # Zustand -> (Anzeige, Bedeutung)
    "run": ("läuft", "arbeitet gerade"),
    "ok": ("fertig", "alles erledigt – wartet auf Neues"),
    "queued": ("geplant", "startet von selbst in Kürze"),
    "limit": ("pausiert", "Limit des Anbieters erreicht – macht danach selbst weiter"),
    "paused": ("angehalten", "vom Admin angehalten"),
    "confirm": ("wartet auf Bestätigung", "aufwendig – oben unter „Vorschläge“ bestätigen"),
    "problem": ("Problem", "funktioniert gerade nicht – siehe Details"),
    "setup": ("nicht eingerichtet", "optional – siehe Details"),
    "inactive": ("nicht aktiv", "anderes Verfahren gewählt"),
    "learning": ("sammelt Beispiele", "wird aktiv, sobald genug Beispiele da sind"),
}


def _ago(iso: str | None) -> str:
    if not iso:
        return "–"
    try:
        secs = (_now() - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return "–"
    if secs < 90:
        return "gerade eben"
    if secs < 5400:
        return f"vor {round(secs / 60)} Min."
    if secs < 172800:
        return f"vor {secs / 3600:.0f} Std."
    return f"vor {secs / 86400:.0f} Tagen"


def _hhmm(ts: float) -> str:
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(config.get("timezone"))
    except Exception:
        tz = timezone.utc
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def _queued_note() -> str:
    from . import pipeline
    if pipeline.STATUS.get("scan"):
        return "startet nach dem laufenden Abruf"
    return "startet in Kürze (die Hintergrundsteuerung prüft alle 30 Sekunden)"


def _gapfill_row(add, bf) -> None:
    """Lückenschluss: Monate auf Zielniveau, Restaufwand (Zeit, Datenverkehr)."""
    est = gapfill.estimate()
    if not est["target"]:
        add("Abruf", "gapfill", "Lückenschluss bis zum Niveau der aktuellen Lage", "ok", "–", "–",
            "nichts zu tun – der Erfassungszeitraum reicht nicht vor die letzten vollständigen Monate zurück")
        return
    total = len(est["months"])
    done = est["reached"] + est["exhausted"]
    ref = f"{est['reference'][0]} bis {est['reference'][-1]}"
    nums = (f"{est['reached']} von {total} Monaten auf Zielniveau" + (f", {est['exhausted']} ausgeschöpft"
            if est["exhausted"] else "") + f" · Ziel ≥ {math.ceil(est['target'] * est['pct'] / 100)} Meldungen/Monat "
            f"({est['pct']} % von {est['target']}, Referenz {ref})")
    thin = sorted((m for m in est["months"] if m["state"] == "offen"), key=lambda m: m["ratio"])[:3]
    thin_txt = ", ".join(f"{m['month']} ({m['articles']}/{m['target']})" for m in thin)
    work = (f"noch bis zu {est['tasks']} Abrufe · {gapfill.fmt_duration(est['seconds'])} · "
            f"≈ {gapfill.fmt_bytes(est['bytes'])} Datenverkehr"
            + ("" if est["rate"]["measured"] else " (grobe Annahme, wird gemessen)"))
    if not config.get("gapfill_enabled"):
        state, details = "inactive", "in den Einstellungen abgeschaltet (Archiv / Backfill → Lücken fortlaufend schließen)"
    elif not est["open_months"]:
        state = "ok"
        details = ("alle Monate auf Zielniveau" if not est["exhausted"] else
                   "alle erreichbaren Lücken geschlossen; ausgeschöpfte Monate werden nach 30 Tagen erneut geprüft")
    elif bf["state"] == "angehalten":
        state, details = "paused", "Historie-Abruf ist angehalten · " + work
    else:
        state = "run" if bf["state"] == "aktiv" else "queued"
        details = work + (f" · dünnste Monate: {thin_txt}" if thin_txt else "")
    add("Abruf", "gapfill", "Lückenschluss bis zum Niveau der aktuellen Lage", state,
        _ago(config.state_get("gapfill_last")), nums, details, pct=_pct(done, total) if est["open_months"] else None)


def jobs() -> list[dict]:
    """Alle Arbeiten mit Zustand, letzter Ausführung, Zahlen, Details und Aktionen – für den Admin-Status."""
    from . import pipeline
    st = pipeline.STATUS
    out: list[dict] = []

    def add(group, key, name, state, last, numbers, details, actions=(), pct=None):
        label, meaning = STATES[state]
        out.append({"group": group, "key": key, "name": name, "state": state, "state_label": label,
                    "state_meaning": meaning, "last": last, "numbers": numbers, "details": details,
                    "actions": list(actions), "pct": pct})

    # ------------------------------------------------ Abruf
    run = db.query_one("SELECT * FROM runs WHERE kind IN ('regulär','manuell','initial','nachholen') ORDER BY id DESC LIMIT 1")
    srcs = db.query("SELECT name, last_status FROM sources WHERE enabled=1")
    bad = [r["name"] for r in srcs if r["last_status"] and r["last_status"] != "ok"]
    numbers = (f"letzter Lauf: {run['fetched']} abgerufen, {run['new_articles']} neu gespeichert, {run['new_topics']} neue "
               "Themen") if run else "–"
    if st.get("scan"):
        sc = st["scan"]
        state, details = "run", f"{sc.get('kind', '')}: {sc.get('phase', '')}"
        if sc.get("sources_total"):
            details += f" ({sc.get('sources_done', 0)} von {sc['sources_total']} Quellen)"
    elif not config.state_get("initial_done"):
        state = "problem" if run and run["status"] == "fehler" else "queued"
        details = ("Keine Quelle erreichbar – Netzwerk/Proxy prüfen; neuer Versuch alle 10 Minuten." if state == "problem"
                   else "Initial-Scan " + _queued_note())
    elif run and run["status"] == "fehler":
        state, details = "problem", (run["log"] or "").strip().splitlines()[-1][:200] if run["log"] else "Lauf fehlgeschlagen"
    elif srcs and len(bad) > len(srcs) / 2:
        state, details = "problem", f"{len(bad)} von {len(srcs)} Quellen liefern Fehler – siehe „Quellen“"
    else:
        nxt = pipeline.next_scan_time()
        state = "ok"
        details = (f"nächster Abruf um {_hhmm(nxt.timestamp())} (alle {config.get('scan_interval_hours'):g} Std.)")
        if bad:
            details += f" · {len(bad)} Quelle(n) mit Fehlern: {', '.join(bad[:3])}{' …' if len(bad) > 3 else ''}"
    add("Abruf", "scan", "Aktuelle Meldungen (Feeds und Suche)", state, _ago(run["started_at"] if run else None),
        numbers, details, [("Jetzt abrufen", "/admin/scan", "")])

    bf = pipeline.backfill_progress()
    last_bf = (db.query_one("SELECT MAX(done_at) t FROM backfill_tasks") or {"t": None})["t"]
    nums = f"{bf['done']} von {bf['total']} Abrufen erledigt" + (f", {bf['failed']} fehlgeschlagen" if bf["failed"] else "")
    actions = []
    if bf["state"] == "fertig":
        state, details = "ok", ("Erfassungszeitraum vollständig abgerufen. Wird er erweitert oder eine Quelle ergänzt, "
                                "plant nnign neue Abrufe automatisch ein.")
    elif bf["state"] == "leer":
        state, details = "ok", "nichts einzuplanen"
    elif bf["state"] == "angehalten":
        state, details = "paused", f"noch {bf['open']} Abrufe offen"
        actions.append(("Fortsetzen", "/admin/backfill/toggle", ""))
    else:
        state = "run" if bf["state"] == "aktiv" else "queued"
        details = bf["state_text"] if state == "run" else f"noch {bf['open']} Abrufe offen – nächster Durchgang in Kürze"
        r = gapfill.rate()
        details += (f" · noch {gapfill.fmt_duration(bf['open'] * r['sec_per_task'])}, "
                    f"≈ {gapfill.fmt_bytes(bf['open'] * r['bytes_per_task'])} Datenverkehr")
        actions.append(("Anhalten", "/admin/backfill/toggle", "secondary"))
    if bf["failed"]:
        actions.append((f"{bf['failed']} fehlgeschlagene wiederholen", "/admin/backfill/reset", "secondary"))
    r = gapfill.rate()
    if r["bytes"]:
        nums += f" · bisher {gapfill.fmt_bytes(r['bytes'])} Datenverkehr"
    add("Abruf", "backfill", "Historie (Archive, Suche, Internet Archive)", state, _ago(last_bf), nums, details, actions,
        pct=bf["percent"] if state in ("run", "queued", "paused") else None)
    _gapfill_row(add, bf)

    catch = config.state_get("pending_catchup") or []
    if catch:
        add("Abruf", "catchup", "Neue/geänderte Quellen nachholen", "run" if (st.get("scan") or {}).get("kind") == "nachholen"
            else "queued", "–", f"{len(catch)} Quelle(n)", "aktueller Zeitraum wird für diese Quellen nachgeholt – "
            + _queued_note())
    hub_n = db.query_one("SELECT COUNT(*) n, SUM(links) l, MAX(last_fetch) t FROM hub_pages")
    hubs_q = config.state_get("pending_hubs") or []
    add("Abruf", "hubs", "Übersichtsseiten auflösen", "queued" if hubs_q else "ok", _ago(hub_n["t"]),
        f"{hub_n['n'] or 0} Übersichtsseiten, {hub_n['l'] or 0} Artikel-Links",
        (f"{len(hubs_q)} warten – " + _queued_note()) if hubs_q else
        "Übersichtsseiten werden beim Abruf erkannt und höchstens alle 12 Std. neu geladen")

    # ------------------------------------------------ Aufbereitung
    n_tr = db.query_one("SELECT COUNT(*) n, SUM(translated_by!='') done FROM articles WHERE lang!='de' AND relevant=1")
    open_tr = (n_tr["n"] or 0) - (n_tr["done"] or 0)
    prov = translate.provider()
    nums = f"{n_tr['done'] or 0} übersetzt, {open_tr} offen"
    if not prov:
        state = "setup" if config.get("translate_provider") != "none" else "inactive"
        details = ("Kein Übersetzer – fremdsprachige Meldungen bleiben im Original. Kostenlos: LibreTranslate-Container "
                   "(COMPOSE_PROFILES=uebersetzung) oder DEEPL_API_KEY in .env.") if state == "setup" else "abgeschaltet"
    elif translate.paused_reason():
        state, details = "limit", translate.paused_reason()
    elif st.get("translate"):
        state, details = "run", f"{st['translate']['n']} Meldungen"
    elif open_tr and (translate.last_problem.recent() or llm.last_problem.recent()):
        state, details = "problem", translate.last_problem.recent() or llm.last_problem.recent()
    elif open_tr:
        state, details = "queued", f"{open_tr} Meldungen – " + _queued_note()
    else:
        state, details = "ok", f"{translate.NAMES[prov]} · neue Meldungen werden beim Abruf übersetzt"
    add("Aufbereitung", "translate", "Übersetzung ins Deutsche", state, _ago(config.state_get("translate_last")), nums,
        details, [("Testen", "/admin/translate-test", "")] + ([("Jetzt nachübersetzen", "/admin/translate-pending",
                                                                "secondary")] if open_tr and prov else []))

    t = db.query_one("SELECT COUNT(*) n, SUM(article_count) a, SUM(manual_group) m FROM topics WHERE hidden=0")
    rg, last = st.get("regroup"), config.state_get("last_regroup")
    nums = f"{t['n'] or 0} Themen aus {t['a'] or 0} Meldungen" + (f", {t['m']} von Hand gebündelt" if t["m"] else "")
    if rg:
        state, details = "run", f"{rg.get('done', 0)} von {rg.get('total', 0)} Meldungen neu zugeordnet"
    elif config.state_get("pending_regroup"):
        state, details = "queued", "Themenbildung wird neu durchlaufen – " + _queued_note()
    elif any(x["id"] == "regroup" for x in suggestions()):
        state, details = "confirm", "Grundlagen haben sich geändert; Neu-Durchlauf ist aufwendig"
    else:
        state = "ok"
        details = (f"neue Meldungen werden beim Abruf sofort zugeordnet (Schwelle {config.get('cluster_threshold')})"
                   + (f" · letzter Neu-Durchlauf: {last['before']} → {last['after']} Themen" if last else ""))
    add("Aufbereitung", "topics", "Themenbildung (automatisch)", state,
        _ago(last["at"]) if last else "–", nums, details,
        [("Neu durchlaufen", "/admin/regroup", "secondary"), ("Von Hand zusammenführen …", "/?auswahl=1", "link")],
        pct=_pct(rg.get("done", 0), rg.get("total", 0)) if rg else None)

    # ------------------------------------------------ Bewertung
    counts = {r["o"]: r["n"] for r in db.query("SELECT sentiment_origin o, COUNT(*) n FROM topics WHERE hidden=0 "
                                                 "GROUP BY sentiment_origin")}
    rc, last = st.get("reclassify"), config.state_get("last_reclassify")
    if rc:
        state, details = "run", f"{rc.get('phase', '')}: {rc.get('done', 0)} von {rc.get('total', 0)}"
    elif config.state_get("pending_reclassify"):
        state, details = "queued", "Bewertungen werden neu berechnet – " + _queued_note()
    else:
        state, details = "ok", "immer aktiv; Rückfall, wenn kein anderes Verfahren entscheidet"
    add("Bewertung", "rules", "Bewertung nach Regeln", state, _ago(last["at"]) if last else "–",
        f"{counts.get('lexikon', 0)} Themen", details, [("Neu berechnen", "/admin/reclassify", "secondary")],
        pct=_pct(rc.get("done", 0), rc.get("total", 0)) if rc else None)

    info = classify.learned_info()
    need = int(config.get("learned_min_examples"))
    acc = f"{info['accuracy'] * 100:.0f} %" if info["accuracy"] is not None else "–"
    nums = f"{info['n']} Beispiele, Genauigkeit {acc}, {counts.get('lernend', 0)} Themen"
    if not classify.allowed("lernend"):
        state, details = "inactive", f"Bewertungsverfahren ist „{classify.engine()}“"
    elif classify.learned_ready():
        state, details = "ok", "ordnet regelbasierte Themen ein, wenn es sicher genug ist; trainiert stündlich nach"
    elif info["n"] < need:
        state, details = "learning", (f"{info['n']} von {need} Beispielen – lernt aus Bewertungen des Admins und sicheren "
                                      "KI-Bewertungen")
    else:
        state, details = "learning", (f"Genauigkeit {acc} unter der Mindestgenauigkeit "
                                      f"{float(config.get('learned_min_accuracy')) * 100:.0f} % – braucht mehr/klarere Beispiele")
    add("Bewertung", "learned", "Lernendes Modell", state, _ago(info.get("trained_at")), nums, details,
        [("Jetzt trainieren", "/admin/learned-train", "secondary")])

    zs_open = zeroshot_open()
    calls = classify._zs_calls_today()
    nums = (f"{counts.get('zeroshot', 0)} Themen, {zs_open} offen · heute {calls} von "
            f"{config.get('zeroshot_max_calls_per_day')} Aufrufen")
    if not classify.zs_configured():
        state, details = "setup", "optional, kostenlos: HF_TOKEN in .env (huggingface.co/settings/tokens)"
    elif not zeroshot_active():
        state = "inactive"
        details = ("springt ein, solange die KI pausiert" if classify.engine() == "auto" else
                   f"Bewertungsverfahren ist „{classify.engine()}“")
    elif classify._zs_pause["until"] > time.time():
        state = "problem" if any(w in classify._zs_pause["reason"] for w in ("ungültig", "nicht verfügbar")) else "limit"
        details = f"{classify._zs_pause['reason']} – weiter ab {_hhmm(classify._zs_pause['until'])}"
    elif calls >= int(config.get("zeroshot_max_calls_per_day")):
        state, details = "limit", f"Tageslimit erreicht – weiter ab {_hhmm(llm._next_utc_midnight())} Uhr"
    elif STATUS.get("zeroshot"):
        state, details = "run", f"„{STATUS['zeroshot']['title']}“"
    elif zs_open:
        state, details = "queued", f"{zs_open} Themen – " + _queued_note()
    else:
        state, details = "ok", f"{config.get('zeroshot_model')} · bewertet neue Themen im Hintergrund"
    if classify._zs_pause.get("error") and state in ("queued", "ok"):
        state, details = "problem", classify._zs_pause["error"]
    add("Bewertung", "zeroshot", "Entscheidungsmodell (Zero-Shot)", state, _ago(config.state_get("zs_last")), nums,
        details, [("Testen", "/admin/zeroshot-test", "")])

    limit = int(config.get("llm_max_calls_per_day"))
    nums = f"{counts.get('llm', 0)} Themen · heute {llm.calls_today()} von {limit} Aufrufen"
    if not llm.available():
        state = "setup"
        details = ("optional; kostenlos z. B. mit Gemini oder Groq: Anbieter und Modell unter Einstellungen → KI, "
                   "Key in .env") + (f" ({llm.missing()})" if config.get("llm_enabled") else " (KI-Analyse ist abgeschaltet)")
    elif not llm.ready():
        state, details = "limit", llm.blocked_reason()
        if "Key" in details or "verweigert" in details:
            state = "problem"
    elif llm.last_problem.recent(1):
        state, details = "problem", llm.last_problem.recent(1)
    else:
        state, details = "ok", llm.describe().replace("aktiv: ", "")
    for slot in llm.slots():
        r = llm.last_result(slot)
        if r and r.get("kind") != "ok":
            details += f" · {llm.slot_label(slot)}: letzte Antwort {llm.describe_result(r)}"
    add("Bewertung", "llm", "KI (Sprachmodell) für neue Meldungen", state, _ago(config.state_get("llm_last_ok")), nums,
        details, [("Verbindung testen", "/admin/llm-test", ""), ("Verfügbare Modelle", "/admin/llm-models", "secondary")])

    if llm.available() and classify.allowed("llm"):
        kinds = {"pending": "nach Ausfall/Limit", "grown": "stark gewachsen", "recent": "letzte 30 Tage",
                 "old": "ältere", "fp": "neues Modell/Prompt"}
        cnt = {k: _rerate_count(k) for k in rerate_kinds()}
        open_n = sum(cnt.values())
        share = int(limit * float(config.get("llm_rerate_share")))
        nums = (f"{open_n} offen" + (" (" + ", ".join(f"{n} {kinds[k]}" for k, n in cnt.items() if n) + ")" if open_n else "")
                + f" · Anteil am Tageslimit bis {share} Aufrufe")
        last_r = (db.query_one("SELECT MAX(llm_rated_at) t FROM topics") or {"t": None})["t"]
        if STATUS.get("rerate"):
            state, details = "run", f"„{STATUS['rerate']['title']}“"
        elif not open_n:
            state, details = "ok", "nichts offen – Themen werden nachbewertet, wenn die KI ausfiel oder sie stark wachsen"
        elif not llm.ready():
            state, details = "limit", llm.blocked_reason()
        elif not _share_ok():
            state, details = "limit", f"Anteil für Nachbewertungen heute ausgeschöpft – weiter ab {_hhmm(llm._next_utc_midnight())} Uhr"
        else:
            state, details = "queued", f"{open_n} Themen – startet in Kürze (alle 2 Minuten ein Durchgang)"
        old_n = _rerate_count("old") if not config.state_get("llm_rerate_old") else 0
        if old_n:
            details += f" · {old_n} ältere Themen nur nach Bestätigung (Vorschläge)"
        add("Bewertung", "rerate", "KI-Nachbewertung", state, _ago(last_r), nums, details)

    # ------------------------------------------------ Pflege
    sugs = suggestions()
    add("Pflege", "check", "Selbstpflege (Änderungen erkennen)", "confirm" if sugs else "ok",
        _ago(STATUS.get("last_check")), f"{len(sugs)} Vorschläge offen" if sugs else "keine Vorschläge offen",
        ("Vorschläge oben bestätigen oder ablehnen" if sugs else
         "prüft jede Minute Einstellungen, Regeln, Quellen und Dienste und zieht den Bestand nach"))
    return out


# ---------------------------------------------------------------------------------- Arbeit

_last = {"check": 0.0, "translate": 0.0, "rerate": 0.0, "zeroshot": 0.0, "train": 0.0}


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
    if now - _last["train"] > 600:
        _last["train"] = now
        was_ready = classify.learned_ready()
        if classify.maybe_retrain() and classify.allowed("lernend") and (was_ready or classify.learned_ready()):
            config.state_set("pending_reclassify", True)    # neues Modell auf bestehende Themen anwenden
    if now - _last["zeroshot"] > 120 and zeroshot_active() and classify.zs_ready():
        _last["zeroshot"] = now
        if zeroshot_batch():
            _last["zeroshot"] = 0.0
    if now - _last["rerate"] > 120 and llm.ready():
        _last["rerate"] = now
        if rerate_batch():
            _last["rerate"] = 0.0


# ---------------------------------------------------------------------------------- Abrufwege und Dienste

def _later(a: str | None, b: str | None) -> str | None:
    return max((x for x in (a, b) if x), default=None)


def backends() -> list[dict]:
    """Abrufwege (Web, Suche, Wayback) und KI-/Übersetzungsdienste mit Zustand, letztem Zugriff und Zahlen."""
    from . import fetcher
    out: list[dict] = []

    def add(group, key, name, state, last, numbers, details, actions=()):
        label, meaning = STATES[state]
        out.append({"group": group, "key": key, "name": name, "state": state, "state_label": label,
                    "state_meaning": meaning, "last": last, "numbers": numbers, "details": details,
                    "actions": list(actions), "pct": None})

    # ------------------------------------------------ Abrufwege für Meldungen
    stats = fetcher.backend_stats()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    extra = {}
    snaps = db.query_one("SELECT COUNT(*) n, COUNT(DISTINCT feed) f FROM wayback_snaps") or {"n": 0, "f": 0}
    extra["wayback"] = f" · {snaps['n']} Schnappschüsse von {snaps['f']} Feeds bekannt" if snaps["n"] else ""
    hub_n = (db.query_one("SELECT COUNT(*) n FROM hub_pages") or {"n": 0})["n"]
    extra["web"] = f" · {hub_n} Übersichtsseiten erkannt" if hub_n else ""
    use = {"web": True, "search": bool(config.get("search_enabled")), "wayback": bool(config.get("backfill_wayback"))}
    for key, name in fetcher.BACKEND_NAMES.items():
        s = stats.get(key, {})
        cur = s.get("day") == today
        ok_n, err_n, lim_n = (s.get("ok", 0), s.get("err", 0), s.get("limit", 0)) if cur else (0, 0, 0)
        last = _later(s.get("last_ok"), s.get("last_err"))
        numbers = f"heute {ok_n} erfolgreich, {err_n} Fehler" + (f", {lim_n}× Limit" if lim_n else "")
        if s.get("bytes_total"):
            numbers += (f" · Datenverkehr heute {gapfill.fmt_bytes(s.get('bytes', 0) if cur else 0)}, "
                        f"gesamt {gapfill.fmt_bytes(s['bytes_total'])}")
        numbers += extra.get(key, "")
        err_after_ok = s.get("last_err") and (not s.get("last_ok") or s["last_err"] > s["last_ok"])
        recent_limit = s.get("last_limit") and (not s.get("last_ok") or s["last_limit"] > s["last_ok"])
        if not use.get(key, True):
            state, details = "inactive", "in den Einstellungen abgeschaltet"
        elif not last:
            state, details = "queued", "noch kein Zugriff – wird beim nächsten passenden Abruf genutzt"
        elif recent_limit:
            state, details = "limit", (f"Anbieter bremst (HTTP {s.get('last_err_status')}) – nnign wartet automatisch "
                                       "länger zwischen den Abrufen und macht weiter")
        elif err_after_ok and (err_n > 3 * max(ok_n, 1) or not s.get("last_ok")):
            state, details = "problem", f"zuletzt fehlgeschlagen: {s.get('last_err_msg') or 'unbekannter Fehler'}"
        else:
            state = "ok"
            details = "erreichbar"
            if s.get("last_err_msg"):
                details += f" · letzter Fehler {_ago(s.get('last_err'))}: {s['last_err_msg']}"
        add("Abrufwege für Meldungen", "be-" + key, name, state, _ago(last), numbers, details)

    # ------------------------------------------------ KI-Anbieter (Haupt- und Ausweich-Anbieter)
    for slot in llm.slots():
        with llm.using(slot):
            prov, mdl, miss = llm.provider_name(), llm.model(), llm.missing()
        until, reason = llm.pause_info(slot)
        r = llm.last_result(slot) or {}
        name = f"KI: {llm.slot_label(slot)}" + (f" ({prov} · {mdl})" if prov else "")
        numbers = f"heute {llm.calls_today()} Aufrufe insgesamt" if slot == "primary" else "–"
        if miss:
            state, details = ("inactive" if miss == "deaktiviert" else "setup"), miss
        elif until > time.time():
            state = "problem" if r.get("kind") in ("auth",) else "limit"
            details = f"{reason} – weiter ab {_hhmm(until)} Uhr"
        elif r.get("kind") in ("auth", "error", "offline", "bad_answer"):
            state, details = "problem", llm.describe_result(r)
        elif not r:
            state, details = "queued", "noch nicht benutzt" + ("" if slot == "primary" else
                                                              " – springt ein, wenn die vorherigen pausieren")
        else:
            state, details = "ok", "letzte Antwort: " + llm.describe_result(r)
        add("KI-Dienste", "be-llm-" + slot, name, state, _ago(r.get("at")), numbers, details,
            [("Testen", "/admin/llm-test", "secondary")] if slot == "primary" and not miss else [])

    # ------------------------------------------------ Zero-Shot (Hugging Face)
    if not classify.zs_configured():
        state, details = "setup", classify.zs_status()
    elif classify._zs_pause["until"] > time.time():
        state = "problem" if any(w in classify._zs_pause["reason"] for w in ("ungültig", "nicht verfügbar")) else "limit"
        details = f"{classify._zs_pause['reason']} – weiter ab {_hhmm(classify._zs_pause['until'])} Uhr"
    elif classify._zs_pause.get("error"):
        state, details = "problem", classify._zs_pause["error"]
    else:
        state, details = "ok", classify.zs_status()
    add("KI-Dienste", "be-zeroshot", f"Zero-Shot-Modell ({config.get('zeroshot_model')})", state,
        _ago(config.state_get("zs_last")),
        f"heute {classify._zs_calls_today()} von {int(config.get('zeroshot_max_calls_per_day'))} Aufrufen", details,
        [("Testen", "/admin/zeroshot-test", "secondary")] if classify.zs_configured() else [])

    # ------------------------------------------------ Übersetzer
    prov = translate.provider()
    lt, deepl = translate.libretranslate_url(), bool((config.get("deepl_api_key") or "").strip())
    avail = ", ".join(n for n, ok in (("LibreTranslate", lt), ("DeepL", deepl), ("KI", llm.available())) if ok)
    if not prov:
        state = "inactive" if config.get("translate_provider") == "none" else "setup"
        details = translate.describe()
    elif translate.paused_reason():
        state, details = "limit", translate.paused_reason() + " – macht danach selbst weiter"
    elif translate.last_problem.recent(1):
        state, details = "problem", translate.last_problem.recent(1)
    else:
        state, details = "ok", f"genutzt: {translate.NAMES[prov]}" + (f" (unter {lt})" if prov == "libretranslate" else "")
    add("KI-Dienste", "be-translate", "Übersetzer", state, _ago(config.state_get("translate_last")),
        f"verfügbar: {avail or 'keiner'}", details, [("Testen", "/admin/translate-test", "secondary")] if prov else [])
    return out
