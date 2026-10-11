"""Vorkonfigurierte seriöse deutschsprachige Nachrichtenquellen.

Wird nur beim allerersten Start in die Datenbank übernommen; danach pflegt der Admin
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
]


def seed_sources() -> None:
    if db.query_one("SELECT 1 FROM sources LIMIT 1"):
        return
    with db.tx() as conn:
        for s in DEFAULT_SOURCES:
            conn.execute(
                "INSERT INTO sources(name, domain, feeds, use_search, archive_template, archive_step, "
                "archive_since, link_pattern, enabled, created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
                (s["name"], s["domain"], "\n".join(s.get("feeds", [])), 1,
                 s.get("archive_template", ""), s.get("archive_step", "day"),
                 s.get("archive_since", ""), s.get("link_pattern", ""), db.utcnow()))


def all_sources(enabled_only: bool = False):
    sql = "SELECT * FROM sources" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY name"
    return db.query(sql)


def feeds_of(src) -> list[str]:
    return [f.strip() for f in (src["feeds"] or "").splitlines() if f.strip()]
