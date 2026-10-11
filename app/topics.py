"""Themenbildung: ordnet Meldungen bestehenden Themen zu oder legt neue an."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timedelta

from . import config, db, sentiment
from .textutil import shorten, tokens

SIG_SIZE = 60
DF_TOTAL = "__N__"


def article_vector(title: str, summary: str) -> Counter:
    vec = Counter()
    for t in tokens(title):
        vec[t] += 2.0
    for t in tokens(summary):
        vec[t] += 1.0
    return vec


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


def candidates(conn, published: datetime, window_days: int) -> list:
    lo = (published - timedelta(days=window_days)).isoformat()
    hi = (published + timedelta(days=window_days)).isoformat()
    return conn.execute(
        "SELECT id, title, abstract, signature, first_published_at, last_published_at, locked, sentiment_origin "
        "FROM topics WHERE hidden=0 AND last_published_at>=? AND first_published_at<=?", (lo, hi)).fetchall()


def best_matches(conn, vec: Counter, published: datetime, idf: Idf, window_days: int, top: int = 5):
    scored = []
    for row in candidates(conn, published, window_days):
        sim = cosine(vec, json.loads(row["signature"] or "{}"), idf)
        if sim > 0:
            scored.append((sim, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top]


def create_topic(conn, title: str, abstract: str, senti: str, score: float, origin: str,
                 published: str, vec: Counter) -> int:
    now = db.utcnow()
    cur = conn.execute(
        "INSERT INTO topics(title, abstract, sentiment, sentiment_score, sentiment_origin, first_published_at, "
        "last_published_at, signature, created_at, updated_at, article_count) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
        (title, abstract, senti, score, origin, published, published, json.dumps(dict(vec.most_common(SIG_SIZE))),
         now, now))
    return cur.lastrowid


def attach(conn, topic_id: int, vec: Counter, published: str) -> None:
    row = conn.execute("SELECT signature FROM topics WHERE id=?", (topic_id,)).fetchone()
    sig = merge_signature(json.loads(row["signature"] or "{}"), vec)
    conn.execute("UPDATE topics SET signature=?, updated_at=? WHERE id=?", (json.dumps(sig), db.utcnow(), topic_id))


def refresh(conn, topic_id: int) -> None:
    """Abgeleitete Felder eines Themas aus seinen Meldungen neu berechnen."""
    t = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if t is None:
        return
    arts = conn.execute("SELECT title, summary, published_at, sentiment_score FROM articles "
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
        total = sum(a["sentiment_score"] for a in arts)
        fields["sentiment_score"] = round(total / len(arts), 2)
        fields["sentiment"] = sentiment.label(total, config.get("sentiment_tie"))
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
        conn.execute("UPDATE articles SET topic_id=? WHERE topic_id=?", (target_id, source_id))
        conn.execute("DELETE FROM topics WHERE id=?", (source_id,))
        rebuild_signature(conn, target_id)
        refresh(conn, target_id)


def detach_article(article_id: int) -> int | None:
    """Meldung aus ihrem Thema lösen und als eigenes Thema anlegen."""
    with db.tx() as conn:
        a = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        if a is None:
            return None
        old = a["topic_id"]
        vec = article_vector(a["title"], a["summary"])
        new_id = create_topic(conn, a["title"], shorten(a["summary"] or ""),
                              sentiment.label(a["sentiment_score"], config.get("sentiment_tie")),
                              a["sentiment_score"], "lexikon", a["published_at"], vec)
        conn.execute("UPDATE articles SET topic_id=? WHERE id=?", (new_id, article_id))
        refresh(conn, new_id)
        if old:
            rebuild_signature(conn, old)
            refresh(conn, old)
        return new_id
