"""Vorkonfigurierte seriöse Nachrichtenquellen (deutsch- und englischsprachig).

Wird beim Start mit der Datenbank abgeglichen (siehe seed_sources); danach pflegt der Admin
die Quellen im UI. Archiv-Vorlagen unterstützen die Platzhalter
{YYYY} {MM} {DD} {M} {D} {WW} (ISO-Kalenderwoche, zweistellig).
"""
from __future__ import annotations

from . import db

DEFAULT_SOURCES = [
    dict(name="tagesschau.de", domain="tagesschau.de",
         feeds=["https://www.tagesschau.de/wirtschaft/index~rss2.xml",
                "https://www.tagesschau.de/index~rss2.xml"],
         archive_template="https://www.tagesschau.de/archiv?datum={YYYY}-{MM}-{DD}",
         archive_step="day", archive_since="2007-01-01",
         link_pattern=r"tagesschau\.de/[a-z].*\.html"),
    dict(name="DER SPIEGEL", domain="spiegel.de",
         feeds=["https://www.spiegel.de/wirtschaft/index.rss",
                "https://www.spiegel.de/wirtschaft/unternehmen/index.rss",
                "https://www.spiegel.de/auto/index.rss"],
         archive_template="https://www.spiegel.de/nachrichtenarchiv/artikel-{DD}.{MM}.{YYYY}.html",
         archive_step="day", archive_since="2000-01-01",
         link_pattern=r"spiegel\.de/(?!nachrichtenarchiv|impressum|abo|plus/?$)[a-z].+"),
    dict(name="ZEIT ONLINE", domain="zeit.de",
         feeds=["https://newsfeed.zeit.de/wirtschaft/index",
                "https://newsfeed.zeit.de/mobilitaet/index"],
         archive_template="https://www.zeit.de/{YYYY}/{WW}/index",
         archive_step="week", archive_since="2000-01-01",
         link_pattern=r"zeit\.de/(?:\d{4}/\d{2}|wirtschaft|mobilitaet)/[^/?#]+"),
    dict(name="heise online", domain="heise.de",
         feeds=["https://www.heise.de/rss/heise-atom.xml"],
         archive_template="https://www.heise.de/newsticker/archiv/{YYYY}/{MM}",
         archive_step="month", archive_since="2000-01-01",
         link_pattern=r"heise\.de/(?:newsticker/meldung|news|meldung)/"),
    dict(name="FAZ", domain="faz.net",
         feeds=["https://www.faz.net/rss/aktuell/wirtschaft/",
                "https://www.faz.net/rss/aktuell/wirtschaft/unternehmen/",
                "https://www.faz.net/rss/aktuell/technik-motor/"]),
    dict(name="Süddeutsche Zeitung", domain="sueddeutsche.de",
         feeds=["https://rss.sueddeutsche.de/rss/Wirtschaft"]),
    dict(name="Handelsblatt", domain="handelsblatt.com",
         feeds=["https://www.handelsblatt.com/contentexport/feed/unternehmen",
                "https://www.handelsblatt.com/contentexport/feed/schlagzeilen"]),
    dict(name="WirtschaftsWoche", domain="wiwo.de",
         feeds=["https://www.wiwo.de/contentexport/feed/rss/unternehmen"]),
    dict(name="manager magazin", domain="manager-magazin.de",
         feeds=["https://www.manager-magazin.de/unternehmen/index.rss"]),
    dict(name="n-tv", domain="n-tv.de",
         feeds=["https://www.n-tv.de/wirtschaft/rss"]),
    dict(name="Deutschlandfunk", domain="deutschlandfunk.de",
         feeds=["https://www.deutschlandfunk.de/nachrichten-100.rss"]),
    dict(name="NDR (Niedersachsen)", domain="ndr.de",
         feeds=["https://www.ndr.de/nachrichten/niedersachsen/index-rss.xml"]),
    dict(name="Tagesspiegel", domain="tagesspiegel.de",
         feeds=["https://www.tagesspiegel.de/contentexport/feed/wirtschaft"]),
    dict(name="WELT", domain="welt.de",
         feeds=["https://www.welt.de/feeds/section/wirtschaft.rss"]),
    dict(name="Automobilwoche", domain="automobilwoche.de", feeds=[]),
    # Regional am Konzernsitz (eigener RSS-Feed unbekannt -> Nachrichtensuche)
    dict(name="Braunschweiger Zeitung", domain="braunschweiger-zeitung.de", feeds=[]),
    # Internationale, englischsprachige Quellen
    dict(name="Reuters", domain="reuters.com", feeds=[], language="en"),
    dict(name="Bloomberg", domain="bloomberg.com", language="en",
         feeds=["https://feeds.bloomberg.com/markets/news.rss",
                "https://feeds.bloomberg.com/business/news.rss"]),
    dict(name="Caixin Global", domain="caixinglobal.com", feeds=[], language="en"),
    dict(name="CnEVPost", domain="cnevpost.com", feeds=["https://cnevpost.com/feed/"], language="en"),
    dict(name="Automotive News", domain="autonews.com", feeds=[], language="en"),
]

LANGUAGES = {"de": "Deutsch", "en": "Englisch", "zh": "Chinesisch"}


FIELDS = ("name", "feeds", "archive_template", "archive_step", "archive_since", "link_pattern", "language")


def _default_row(s: dict) -> dict:
    return {"name": s["name"], "feeds": "\n".join(s.get("feeds", [])),
            "archive_template": s.get("archive_template", ""), "archive_step": s.get("archive_step", "day"),
            "archive_since": s.get("archive_since", ""), "link_pattern": s.get("link_pattern", ""),
            "language": s.get("language", "de")}


def seed_sources() -> dict:
    """Standardquellen mit dem Stand dieses Images abgleichen (bei jedem Start).

    - neue Standardquellen werden einmalig ergänzt; vom Admin gelöschte kommen nicht wieder,
    - Standardquellen, die der Admin nicht verändert hat, erhalten geänderte Vorgaben (z. B. neue Feed-URLs),
    - vom Admin bearbeitete Quellen bleiben unangetastet.
    Rückgabe: {"added": [ids], "updated": [ids]}."""
    from . import config
    seeded = set(config.state_get("seeded_domains", []))
    baseline: dict = config.state_get("source_defaults", None) or {}
    legacy = not config.state_get("source_defaults", None)
    rows = {r["domain"]: r for r in db.query("SELECT * FROM sources")}
    added, updated = [], []
    with db.tx() as conn:
        for s in DEFAULT_SOURCES:
            new = _default_row(s)
            dom = s["domain"]
            row = rows.get(dom)
            if row is None:
                if dom in seeded:
                    continue
                cur = conn.execute(
                    "INSERT INTO sources(name, domain, feeds, use_search, archive_template, archive_step, "
                    "archive_since, link_pattern, language, enabled, created_at) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                    (new["name"], dom, new["feeds"], 1, new["archive_template"], new["archive_step"],
                     new["archive_since"], new["link_pattern"], new["language"], db.utcnow()))
                added.append(cur.lastrowid)
                continue
            old = new if legacy else baseline.get(dom)
            current = {f: (row[f] or "") for f in FIELDS}
            if old and current == {f: old.get(f, "") for f in FIELDS} and current != new:
                conn.execute("UPDATE sources SET " + ", ".join(f"{f}=?" for f in FIELDS) + " WHERE id=?",
                             (*[new[f] for f in FIELDS], row["id"]))
                updated.append(row["id"])
    config.state_set("seeded_domains", sorted(seeded | set(rows) | {s["domain"] for s in DEFAULT_SOURCES}))
    config.state_set("source_defaults", {s["domain"]: _default_row(s) for s in DEFAULT_SOURCES})
    return {"added": added, "updated": updated}


def all_sources(enabled_only: bool = False):
    sql = "SELECT * FROM sources" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY name"
    return db.query(sql)


def feeds_of(src) -> list[str]:
    return [f.strip() for f in (src["feeds"] or "").splitlines() if f.strip()]
