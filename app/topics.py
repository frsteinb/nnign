"""Themenbildung: ordnet Meldungen bestehenden Themen zu oder legt neue an."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timedelta

from . import db, features, sentiment
from .textutil import shorten

SIG_SIZE = 80
DF_TOTAL = "__N__"
ALGO_VERSION = 2   # bei Änderungen an Merkmalen/Zuordnung erhöhen -> Themen werden beim Start neu gebündelt

# Anteil des Vergleichs mit der ähnlichsten einzelnen Meldung eines Themas (Rest: Themen-Signatur)
ARTICLE_WEIGHT = 0.7
RECENT_ARTICLES = 25


def article_vector(title: str, summary: str) -> Counter:
    return features.vector(title, summary)


class Idf:
    """Dokumenthäufigkeiten (inkrementell in token_df gepflegt)."""

    def __init__(self, conn):
        self.conn = conn
        row = conn.execute("SELECT df FROM token_df WHERE token=?", (DF_TOTAL,)).fetchone()
        self.n = row["df"] if row else 0
        self.cache: dict[str, int] = {}

    def df(self, tok: str) -> int:
        if tok not in self.cache:
            row = self.conn.execute("SELECT df FROM token_df WHERE token=?", (tok,)).fetchone()
            self.cache[tok] = row["df"] if row else 0
        return self.cache[tok]

    def weight(self, tok: str) -> float:
        return math.log((self.n + 1) / (self.df(tok) + 1)) + 1.0

    def add_document(self, toks) -> None:
        uniq = set(toks)
        for t in uniq:
            self.conn.execute("INSERT INTO token_df(token, df) VALUES(?, 1) "
                              "ON CONFLICT(token) DO UPDATE SET df=df+1", (t,))
            self.cache.pop(t, None)
        self.conn.execute("INSERT INTO token_df(token, df) VALUES(?, 1) "
                          "ON CONFLICT(token) DO UPDATE SET df=df+1", (DF_TOTAL,))
        self.n += 1


MODEL_CONFLICT = 0.55   # Faktor, wenn beide Seiten Modelle nennen, aber kein gemeinsames
BRAND_CONFLICT = 0.55   # beide nennen Konzernmarken, aber keine gemeinsame (Audi vs. Porsche)
BRAND_MISSING = 0.8     # nur eine Seite nennt eine Konzernmarke (Porsche vs. nur „VW“)
THIN_EVIDENCE = 0.6     # nur ein einziger gemeinsamer Begriff (z. B. nur „Rückruf“)


def similarity(a: Counter, b: dict, idf: Idf) -> float:
    """Kosinus-Ähnlichkeit mit Abschlägen für Widersprüche: verschiedene Modelle (ID.3 vs. Golf),
    verschiedene Marken (VW vs. Porsche) oder nur ein einziger gemeinsamer Begriff."""
    sim = cosine(a, b, idf)
    if not sim:
        return 0.0
    ma, mb = features.models(a), features.models(b)
    if ma and mb and not (ma & mb):
        sim *= MODEL_CONFLICT
    ba, bb = features.brands(a), features.brands(b)
    if ba and bb and not (ba & bb):
        sim *= BRAND_CONFLICT
    elif bool(ba) != bool(bb):
        sim *= BRAND_MISSING
    if sum(1 for t in a if t in b) < 2:
        sim *= THIN_EVIDENCE
    return sim


def cosine(a: Counter, b: dict, idf: Idf) -> float:
    if not a or not b:
        return 0.0
    num = 0.0
    for t, w in a.items():
        if t in b:
            num += (w * idf.weight(t)) * (b[t] * idf.weight(t))
    if num == 0:
        return 0.0
    na = math.sqrt(sum((w * idf.weight(t)) ** 2 for t, w in a.items()))
    nb = math.sqrt(sum((w * idf.weight(t)) ** 2 for t, w in b.items()))
    return num / (na * nb) if na and nb else 0.0


def merge_signature(sig: dict, vec: Counter) -> dict:
    c = Counter(sig)
    c.update(vec)
    return dict(c.most_common(SIG_SIZE))


def _max_span() -> int:
    try:
        from . import config
        return int(config.get("cluster_max_span_days"))
    except Exception:
        return 14


def candidates(conn, published: datetime, window_days: int) -> list:
    """Themen, zu denen die Meldung zeitlich passt: letzte Meldung höchstens window_days entfernt und
    das Thema insgesamt nicht länger als cluster_max_span_days (gegen Kettenbildung)."""
    span = timedelta(days=_max_span())
    lo = (published - timedelta(days=window_days)).isoformat()
    hi = (published + timedelta(days=window_days)).isoformat()
    return conn.execute(
        "SELECT id, title, abstract, signature, first_published_at, last_published_at, locked, sentiment_origin, "
        "manual_group FROM topics WHERE hidden=0 AND last_published_at>=? AND first_published_at<=? "
        "AND first_published_at>=? AND last_published_at<=?",
        (lo, hi, (published - span).isoformat(), (published + span).isoformat())).fetchall()


_vec_cache: dict[tuple, Counter] = {}


def _article_vec(a) -> Counter:
    key = (a["id"], a["title"], len(a["summary"] or ""))   # Titel im Schlüssel: Übersetzung/andere DB
    v = _vec_cache.get(key)
    if v is None:
        if len(_vec_cache) > 20000:
            _vec_cache.clear()
        v = _vec_cache[key] = article_vector(a["title"], a["summary"] or "")
    return v


def best_matches(conn, vec: Counter, published: datetime, idf: Idf, window_days: int, top: int = 5,
                 exclude: int | None = None):
    """Ähnlichste Themen im Zeitfenster. Ähnlichkeit = Mischung aus bester Einzelmeldung des Themas
    (findet Folgemeldungen, die nur einer Meldung ähneln) und Themen-Signatur (Gesamtbild)."""
    rows = [r for r in candidates(conn, published, window_days) if r["id"] != exclude]
    if not rows:
        return []
    by_topic: dict[int, list] = {}
    ids = [r["id"] for r in rows]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for a in conn.execute(
                f"SELECT id, topic_id, title, summary FROM articles WHERE topic_id IN ({','.join('?' * len(chunk))}) "
                "ORDER BY published_at DESC", chunk):
            lst = by_topic.setdefault(a["topic_id"], [])
            if len(lst) < RECENT_ARTICLES:
                lst.append(a)
    scored = []
    for row in rows:
        sig = similarity(vec, json.loads(row["signature"] or "{}"), idf)
        best = max((similarity(vec, _article_vec(a), idf) for a in by_topic.get(row["id"], [])), default=sig)
        sim = ARTICLE_WEIGHT * best + (1 - ARTICLE_WEIGHT) * sig
        if sim > 0:
            scored.append((round(sim, 4), row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top]


def create_topic(conn, title: str, abstract: str, senti: str, score: float, origin: str,
                 published: str, vec: Counter, detail: dict | None = None) -> int:
    now = db.utcnow()
    cur = conn.execute(
        "INSERT INTO topics(title, abstract, sentiment, sentiment_score, sentiment_origin, sentiment_detail, "
        "first_published_at, last_published_at, signature, created_at, updated_at, article_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
        (title, abstract, senti, score, origin, json.dumps(detail or {}, ensure_ascii=False), published, published,
         json.dumps(dict(vec.most_common(SIG_SIZE))), now, now))
    return cur.lastrowid


def aggregate_sentiment(details: list[dict]) -> tuple[str, float, dict]:
    """Tonalität eines Themas aus den Regeltreffern seiner Meldungen."""
    res = sentiment.Result()
    for d in details:
        res.pos += float(d.get("pos", 0))
        res.neg += float(d.get("neg", 0))
        res.hits += d.get("hits", [])
    detail = res.detail(10)
    detail["articles"] = len(details)
    return sentiment.label_for(res), res.score, detail


def attach(conn, topic_id: int, vec: Counter, published: str) -> None:
    row = conn.execute("SELECT signature FROM topics WHERE id=?", (topic_id,)).fetchone()
    sig = merge_signature(json.loads(row["signature"] or "{}"), vec)
    conn.execute("UPDATE topics SET signature=?, updated_at=? WHERE id=?", (json.dumps(sig), db.utcnow(), topic_id))


def refresh(conn, topic_id: int) -> None:
    """Abgeleitete Felder eines Themas aus seinen Meldungen neu berechnen."""
    t = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if t is None:
        return
    arts = conn.execute("SELECT title, summary, published_at, sentiment_score, sentiment_detail FROM articles "
                        "WHERE topic_id=? ORDER BY published_at", (topic_id,)).fetchall()
    if not arts:
        conn.execute("DELETE FROM topics WHERE id=?", (topic_id,))
        return
    fields = {
        "article_count": len(arts),
        "first_published_at": arts[0]["published_at"],
        "last_published_at": arts[-1]["published_at"],
    }
    if not t["locked"] and t["sentiment_origin"] == "lexikon":
        label, score, detail = aggregate_sentiment([json.loads(a["sentiment_detail"] or "{}") for a in arts])
        fields["sentiment"] = label
        fields["sentiment_score"] = score
        fields["sentiment_detail"] = json.dumps(detail, ensure_ascii=False)
        if not t["title_fixed"]:
            fields["title"] = arts[0]["title"]
            with_summary = [a for a in arts if len(a["summary"] or "") >= 60]
            if with_summary:
                fields["abstract"] = shorten(with_summary[0]["summary"])
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE topics SET {sets} WHERE id=?", (*fields.values(), topic_id))


def rebuild_signature(conn, topic_id: int) -> None:
    sig = Counter()
    for a in conn.execute("SELECT title, summary FROM articles WHERE topic_id=?", (topic_id,)):
        sig.update(article_vector(a["title"], a["summary"]))
    conn.execute("UPDATE topics SET signature=? WHERE id=?", (json.dumps(dict(sig.most_common(SIG_SIZE))), topic_id))


def merge(target_id: int, source_id: int) -> None:
    with db.tx() as conn:
        merge_in(conn, target_id, source_id)


def merge_in(conn, target_id: int, source_id: int) -> None:
    """Thema source in target aufgehen lassen (innerhalb einer laufenden Transaktion)."""
    conn.execute("UPDATE articles SET topic_id=? WHERE topic_id=?", (target_id, source_id))
    conn.execute("DELETE FROM topics WHERE id=?", (source_id,))
    rebuild_signature(conn, target_id)
    refresh(conn, target_id)


def mergeable(row) -> bool:
    """Automatisch zusammenlegen nur, was nicht von Hand bearbeitet, gesperrt oder gebündelt ist."""
    return not row["locked"] and row["sentiment_origin"] != "manuell" and not row["manual_group"]


def merge_many(ids: list[int], title: str = "") -> int | None:
    """Mehrere Themen auf Anordnung des Admins zu einem zusammenführen. Ziel ist das älteste Thema;
    das Ergebnis gilt als von Hand gebündelt (automatisches Neu-Bündeln trennt es nicht wieder).
    Gibt die ID des Ziel-Themas zurück."""
    ids = sorted(set(int(i) for i in ids))
    with db.tx() as conn:
        rows = conn.execute(f"SELECT id FROM topics WHERE id IN ({','.join('?' * len(ids))}) "
                            "ORDER BY first_published_at, id", ids).fetchall() if ids else []
        if len(rows) < 2:
            return None
        target = rows[0]["id"]
        for r in rows[1:]:
            merge_in(conn, target, r["id"])
        conn.execute("UPDATE topics SET manual_group=1, updated_at=? WHERE id=?", (db.utcnow(), target))
        if title.strip():
            conn.execute("UPDATE topics SET title=?, title_fixed=1 WHERE id=?", (title.strip()[:200], target))
        refresh(conn, target)
        return target


def compatible(conn, a_id: int, b_id: int, idf: Idf) -> bool:
    """Passen zwei Themen zusammen? (keine widersprüchlichen Modelle/Marken im Gesamtbild)"""
    rows = {r["id"]: json.loads(r["signature"] or "{}") for r in conn.execute(
        "SELECT id, signature FROM topics WHERE id IN (?, ?)", (a_id, b_id))}
    if len(rows) < 2:
        return False
    sa, sb = Counter(rows[a_id]), Counter(rows[b_id])
    ma, mb = features.models(sa), features.models(sb)
    ba, bb = features.brands(sa), features.brands(sb)
    return not (ma and mb and not (ma & mb)) and not (ba and bb and not (ba & bb))


def detach_article(article_id: int) -> int | None:
    """Meldung aus ihrem Thema lösen und als eigenes Thema anlegen."""
    with db.tx() as conn:
        a = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        if a is None:
            return None
        old = a["topic_id"]
        vec = article_vector(a["title"], a["summary"])
        label, score, detail = aggregate_sentiment([json.loads(a["sentiment_detail"] or "{}")])
        new_id = create_topic(conn, a["title"], shorten(a["summary"] or ""), label, score, "lexikon",
                              a["published_at"], vec, detail)
        conn.execute("UPDATE articles SET topic_id=? WHERE id=?", (new_id, article_id))
        refresh(conn, new_id)
        if old:
            rebuild_signature(conn, old)
            refresh(conn, old)
        return new_id
