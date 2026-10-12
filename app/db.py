"""SQLite-Persistenz. Eine Datei, keine externen Dienste."""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "nnign.sqlite3"))

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    domain        TEXT NOT NULL,
    feeds         TEXT NOT NULL DEFAULT '',   -- RSS/Atom-URLs, eine pro Zeile
    use_search    INTEGER NOT NULL DEFAULT 1, -- zusätzlich Google-News-Suche mit site:domain
    archive_template TEXT NOT NULL DEFAULT '', -- URL-Vorlage einer Archivseite, z. B. .../artikel-{DD}.{MM}.{YYYY}.html
    archive_step  TEXT NOT NULL DEFAULT 'day', -- day | week | month | year
    archive_since TEXT NOT NULL DEFAULT '',   -- frühestes Datum der Archivseiten
    link_pattern  TEXT NOT NULL DEFAULT '',   -- Regex, dem Artikel-Links auf Archivseiten entsprechen müssen
    language      TEXT NOT NULL DEFAULT 'de', -- Sprache der Quelle: de | en | zh
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_fetch_at TEXT,
    last_status   TEXT,
    last_items    INTEGER,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT NOT NULL,
    abstract           TEXT NOT NULL DEFAULT '',
    sentiment          TEXT NOT NULL,            -- 'positiv' | 'neutral' | 'negativ'
    sentiment_detail   TEXT NOT NULL DEFAULT '{}', -- Begründung (Regeltreffer bzw. KI-Begründung)
    sentiment_score    REAL NOT NULL DEFAULT 0,
    sentiment_origin   TEXT NOT NULL DEFAULT 'lexikon', -- lexikon | llm | manuell
    first_published_at TEXT NOT NULL,            -- erstes Erscheinungsdatum einer Quelle
    last_published_at  TEXT NOT NULL,
    signature          TEXT NOT NULL DEFAULT '{}', -- Token-Gewichte für die Themenzuordnung
    created_at         TEXT NOT NULL,            -- Erfassungsdatum im System
    updated_at         TEXT NOT NULL,
    article_count      INTEGER NOT NULL DEFAULT 0,
    hidden             INTEGER NOT NULL DEFAULT 0,
    locked             INTEGER NOT NULL DEFAULT 0 -- vom Admin bearbeitet: nicht automatisch überschreiben
);
CREATE INDEX IF NOT EXISTS idx_topics_first ON topics(first_published_at);
CREATE INDEX IF NOT EXISTS idx_topics_updated ON topics(updated_at);
CREATE INDEX IF NOT EXISTS idx_topics_last ON topics(last_published_at);

CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    title_key     TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',   -- deutsch (bei fremdsprachigen Meldungen: Übersetzung)
    lang          TEXT NOT NULL DEFAULT 'de', -- Originalsprache
    title_orig    TEXT NOT NULL DEFAULT '',   -- Originaltitel, falls übersetzt oder fremdsprachig
    summary_orig  TEXT NOT NULL DEFAULT '',
    translated_by TEXT NOT NULL DEFAULT '',   -- leer = nicht übersetzt
    source_name   TEXT NOT NULL,
    source_domain TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    via           TEXT NOT NULL,                 -- rss | suche
    relevant      INTEGER NOT NULL DEFAULT 1,
    sentiment_score REAL NOT NULL DEFAULT 0,
    sentiment_detail TEXT NOT NULL DEFAULT '{}',
    topic_id      INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    UNIQUE(title_key, source_domain)
);
CREATE INDEX IF NOT EXISTS idx_articles_topic ON articles(topic_id);
CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published_at);

CREATE TABLE IF NOT EXISTS token_df (
    token TEXT PRIMARY KEY,
    df    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS backfill_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    method       TEXT NOT NULL,                  -- archive | wayback_index | wayback_feed | search
    target       TEXT NOT NULL,                  -- URL bzw. Suchanfrage
    period_start TEXT NOT NULL,                  -- Datum, für das die Aufgabe steht (Sortierung)
    period_end   TEXT NOT NULL,
    priority     INTEGER NOT NULL DEFAULT 0,      -- Grob-nach-fein-Reihenfolge
    status       TEXT NOT NULL DEFAULT 'offen',  -- offen | ok | fehler
    attempts     INTEGER NOT NULL DEFAULT 0,
    items        INTEGER NOT NULL DEFAULT 0,
    relevant     INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    done_at      TEXT,
    UNIQUE(method, target)
);
CREATE INDEX IF NOT EXISTS idx_backfill_open ON backfill_tasks(status, priority, attempts, period_start);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,                  -- initial | regulär | manuell
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'läuft',  -- läuft | ok | fehler
    fetched      INTEGER NOT NULL DEFAULT 0,
    candidates   INTEGER NOT NULL DEFAULT 0,
    new_articles INTEGER NOT NULL DEFAULT 0,
    new_topics   INTEGER NOT NULL DEFAULT 0,
    llm_calls    INTEGER NOT NULL DEFAULT 0,
    log          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS wayback_snaps (   -- alle verfügbaren Tages-Schnappschüsse eines Feeds
    feed TEXT NOT NULL,
    ts   TEXT NOT NULL,
    PRIMARY KEY (feed, ts)
);

CREATE TABLE IF NOT EXISTS hub_pages (       -- erkannte Übersichtsseiten (keine Artikel)
    url           TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    source_domain TEXT NOT NULL DEFAULT '',
    last_fetch    TEXT NOT NULL,
    links         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    user_agent  TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT ''
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != DB_PATH:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _local.path = DB_PATH
    return conn


@contextmanager
def tx():
    """Schreib-Transaktion; serialisiert Schreibzugriffe aus mehreren Threads."""
    with _write_lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params=()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def query_one(sql: str, params=()):
    return connect().execute(sql, params).fetchone()


# Spalten, die nach der ersten Version hinzugekommen sind (für bestehende Datenbanken)
MIGRATIONS = [
    ("backfill_tasks", "depth", "INTEGER NOT NULL DEFAULT 0"),      # Verfeinerungsstufe (0 = Grundplanung)
    ("backfill_tasks", "covers_from", "TEXT NOT NULL DEFAULT ''"),   # Wayback: ältestes Datum im Schnappschuss
    ("articles", "scope", "TEXT NOT NULL DEFAULT 'konzern'"),   # konzern | partner (nur Partner/Beteiligung)
    ("topics", "scope", "TEXT NOT NULL DEFAULT 'konzern'"),
    ("topics", "zs_rated_count", "INTEGER NOT NULL DEFAULT 0"),   # Zero-Shot: Meldungen bei der Bewertung
    ("topics", "zs_fp", "TEXT NOT NULL DEFAULT ''"),              # Zero-Shot: Modell/Hypothese der Bewertung
    ("articles", "llm_state", "TEXT NOT NULL DEFAULT ''"),       # '' | pending (KI fehlte/Limit) | done
    ("topics", "llm_rated_at", "TEXT NOT NULL DEFAULT ''"),
    ("topics", "llm_rated_count", "INTEGER NOT NULL DEFAULT 0"),  # Meldungen zum Zeitpunkt der KI-Bewertung
    ("topics", "llm_fp", "TEXT NOT NULL DEFAULT ''"),             # Anbieter/Modell/Prompt der KI-Bewertung
    ("topics", "manual_group", "INTEGER NOT NULL DEFAULT 0"),   # vom Admin zusammengeführt -> bleibt zusammen
    ("topics", "title_fixed", "INTEGER NOT NULL DEFAULT 0"),    # Titel vom Admin gesetzt
    ("topics", "sentiment_detail", "TEXT NOT NULL DEFAULT '{}'"),
    ("articles", "sentiment_detail", "TEXT NOT NULL DEFAULT '{}'"),
    ("sources", "language", "TEXT NOT NULL DEFAULT 'de'"),
    ("articles", "lang", "TEXT NOT NULL DEFAULT 'de'"),
    ("articles", "title_orig", "TEXT NOT NULL DEFAULT ''"),
    ("articles", "summary_orig", "TEXT NOT NULL DEFAULT ''"),
    ("articles", "translated_by", "TEXT NOT NULL DEFAULT ''"),
    ("hub_pages", "watch", "INTEGER NOT NULL DEFAULT 0"),      # 0 erkannt | 1 automatisch beobachtet | 2 von Hand | -1 aus
    ("hub_pages", "relevant", "INTEGER NOT NULL DEFAULT 0"),   # relevante Artikel beim letzten Laden
    ("hub_pages", "new_last", "INTEGER NOT NULL DEFAULT 0"),   # davon neu gespeichert
    ("hub_pages", "parent", "TEXT NOT NULL DEFAULT ''"),       # gefunden auf dieser Übersichtsseite
    ("hub_pages", "watched_at", "TEXT NOT NULL DEFAULT ''"),
]


# Indizes für Admin- und Statusabfragen; nach den Migrationen angelegt (manche Spalten kamen erst später hinzu)
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_articles_domain ON articles(source_domain, relevant)",
    "CREATE INDEX IF NOT EXISTS idx_articles_lang ON articles(lang, relevant)",
    "CREATE INDEX IF NOT EXISTS idx_backfill_method ON backfill_tasks(method, status)",
    "CREATE INDEX IF NOT EXISTS idx_backfill_source ON backfill_tasks(source_id, method)",
    "CREATE INDEX IF NOT EXISTS idx_topics_sentiment ON topics(hidden, sentiment)",
]


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        for sql in INDEXES:
            conn.execute(sql)
        conn.execute("PRAGMA optimize")          # Statistiken für den Abfrageplaner aktuell halten (billig)
