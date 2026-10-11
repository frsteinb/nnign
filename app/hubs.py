"""Übersichtsseiten erkennen und auflösen.

Manche Treffer sind keine Artikel, sondern Übersichts- oder Themenseiten („VW – News und Hintergründe“,
„Alle Artikel zu Volkswagen“, …/thema/volkswagen/). Sie werden nicht als Meldung gespeichert; stattdessen
lädt nnign die Seite, entnimmt ihr die Links auf die eigentlichen Artikel und verarbeitet diese einzeln
(mit Teaser und Datum von der Artikelseite). Eine Übersichtsseite wird höchstens alle 12 Stunden geladen.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import db

VERSION = 1            # bei geänderter Erkennung erhöhen -> gespeicherte Meldungen werden neu geprüft
REFETCH_HOURS = 12
MAX_LINKS = 40

_TITLE = re.compile("|".join([
    r"\bnews\s*(?:und|&|\+)\s*hintergr(?:ü|ue)nde\b",
    r"\bnachrichten\s*(?:und|&|\+)\s*hintergr(?:ü|ue)nde\b",
    r"\bnews\s*(?:und|&|\+)\s*infos?\b",
    r"\baktuelle\s+(?:nachrichten|news|meldungen|informationen|artikel|berichte)\b",
    r"\balle\s+(?:artikel|nachrichten|news|meldungen|beitr(?:ä|ae)ge|infos?|berichte|themen)\b",
    r"\b(?:news|nachrichten|artikel|meldungen|beitr(?:ä|ae)ge|infos|berichte|themen)\s+(?:zu|zum|zur|über|ueber|rund\s+um)\b",
    r"\bthemenseite\b", r"\bdossier\b", r"\bthemen-?special\b",
    r"^(?:thema|themen|stichwort|schlagwort|tag|tags|topic|archiv)\s*[:–\-|]",
    r"\blatest\s+(?:news|stories|headlines|updates)\b", r"\bnews\s+and\s+(?:updates|analysis|insights)\b",
    r"\ball\s+(?:the\s+)?(?:latest\s+)?(?:news|stories|articles)\b", r"\btopic\s*:",
]), re.I)
# „VW – News“, „Volkswagen: Aktuelles“, „Porsche | Nachrichten“
_NAME_NEWS = re.compile(r"^[\w .&\-]{2,40}\s*[–\-:|]\s*(?:news|nachrichten|aktuell(?:es)?|alle\s+infos|übersicht)\s*$",
                        re.I)
_URL = re.compile(r"/(?:thema|themen|tag|tags|schlagwort|schlagworte|stichwort|stichworte|topic|topics|dossier|"
                  r"dossiers|person|personen|suche|search)(?:/|$|\?)|[,_/]thema[,_\-.]|/themen-|\btag=|/archiv/?$",
                  re.I)
_JSONLD_LIST = re.compile(r'"@type"\s*:\s*"(?:CollectionPage|ItemList|SearchResultsPage)"')
_ARTICLE_TAG = re.compile(r"<article\b", re.I)


def is_hub_title(title: str) -> bool:
    t = (title or "").strip()
    return bool(t) and (bool(_TITLE.search(t)) or bool(_NAME_NEWS.match(t)))


def is_hub_url(url: str) -> bool:
    path = re.sub(r"^https?://[^/]+", "", url or "")
    return bool(_URL.search(path))


def is_hub(title: str, url: str) -> bool:
    return is_hub_title(title) or is_hub_url(url)


def page_is_hub(meta: dict, html: str) -> bool:
    """Inhaltliche Prüfung einer geladenen Seite: Sammelseite statt Artikel?"""
    head = html[:300_000]
    if _JSONLD_LIST.search(head) and meta.get("type") != "article":
        return True
    if meta.get("type") == "article" or meta.get("published"):
        return False
    return len(_ARTICLE_TAG.findall(html)) >= 6 or is_hub_title(meta.get("title", ""))


def _main_part(html: str) -> str:
    """Nur den Hauptinhalt betrachten (ohne Navigation/Seitenleisten), falls erkennbar."""
    m = re.search(r"<main\b.*?</main>", html, re.I | re.S)
    return m.group(0) if m else html


def due(url: str) -> bool:
    row = db.query_one("SELECT last_fetch FROM hub_pages WHERE url=?", (url,))
    if not row:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(row["last_fetch"]) > timedelta(hours=REFETCH_HOURS)


def remember(url: str, title: str, n_links: int, source_domain: str) -> None:
    with db.tx() as conn:
        conn.execute("INSERT INTO hub_pages(url, title, source_domain, last_fetch, links) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(url) DO UPDATE SET title=excluded.title, last_fetch=excluded.last_fetch, "
                     "links=excluded.links", (url, title[:300], source_domain, db.utcnow(), n_links))


def children(hub, html: str, base_url: str, relevant: bool) -> list:
    """Artikel-Links einer Übersichtsseite als neue Meldungen (Details werden noch geladen)."""
    from .fetcher import Item, extract_links
    from .textutil import domain_matches
    out, seen = [], {hub.url}
    for url, text in extract_links(_main_part(html), base_url, "", min_len=25):
        if url in seen or not domain_matches(url, hub.source_domain) or is_hub(text, url):
            continue
        if len(text.split()) < 4:
            continue
        seen.add(url)
        out.append(Item(url, text, "", hub.published, hub.source_name, hub.source_domain, "übersicht",
                        needs_details=True, lang=hub.lang,
                        extra={"hub": hub.url, "hub_relevant": relevant, "any_date": True, "date_required": True}))
        if len(out) >= MAX_LINKS:
            break
    return out


def expand(http, hub, relevant: bool) -> list:
    """Übersichtsseite laden (falls fällig) und ihre Artikel als Meldungen zurückgeben."""
    import httpx

    from .fetcher import FetchError
    if not due(hub.url):
        return []
    html, base = hub.extra.get("hub_html"), hub.extra.get("hub_base")
    if not html:
        try:
            resp = http.get(hub.url)
        except (FetchError, httpx.HTTPError):
            remember(hub.url, hub.title, 0, hub.source_domain)
            return []
        html, base = resp.text, str(resp.url)
    kids = children(hub, html, base or hub.url, relevant)
    remember(hub.url, hub.title, len(kids), hub.source_domain)
    return kids
