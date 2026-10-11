"""Übersichtsseiten erkennen und auflösen.

Manche Treffer sind keine Artikel, sondern Übersichts- oder Themenseiten („VW – News und Hintergründe“,
„Alle Artikel zu Volkswagen“, …/thema/volkswagen/). Sie werden nicht als Meldung gespeichert; stattdessen
lädt nnign die Seite, entnimmt ihr die Links auf die eigentlichen Artikel und verarbeitet diese einzeln
(mit Teaser und Datum von der Artikelseite). Eine Übersichtsseite wird höchstens alle 12 Stunden geladen.

Liefert eine Übersichtsseite mehrere relevante Artikel, wird sie „beobachtet“: Sie wird künftig bei jedem
regulären Abruf direkt geladen (unabhängig davon, ob Feed oder Suche sie liefern), verlinkte Unter-Übersichten mit
Konzernbezug werden mit aufgelöst (eine Ebene), und ihre Folgeseiten („Seite 2“, „Ältere Artikel“ …) laufen als
Aufgaben in den Historie-Abruf. Der Admin kann Übersichtsseiten auch von Hand hinzufügen oder abschalten.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import db

VERSION = 1            # bei geänderter Erkennung erhöhen -> gespeicherte Meldungen werden neu geprüft
REFETCH_HOURS = 12
MAX_LINKS = 40
WATCH_MIN = 2          # ab so vielen relevanten Artikeln wird eine Übersichtsseite beobachtet
MAX_SUBHUBS = 5        # verlinkte Unter-Übersichten je Seite
WATCH_LABELS = {0: "erkannt", 1: "beobachtet (automatisch)", 2: "beobachtet (von Hand)", -1: "abgeschaltet"}
SEED_HUBS = [   # bekannte, ergiebige Übersichtsseiten (Domain muss zu einer Quelle gehören)
    "https://www.braunschweiger-zeitung.de/wirtschaft/volkswagen/",
]

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


def _refetch_hours(watched: bool) -> float:
    if watched:      # beobachtete Seiten bei jedem regulären Abruf
        from . import config
        return max(0.5, float(config.get("scan_interval_hours")) * 0.8)
    return REFETCH_HOURS


def due(url: str) -> bool:
    row = db.query_one("SELECT last_fetch, watch FROM hub_pages WHERE url=?", (url,))
    if not row or not row["last_fetch"]:
        return True
    if row["watch"] < 0:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["last_fetch"])
    return age > timedelta(hours=_refetch_hours(row["watch"] > 0))


def remember(url: str, title: str, n_links: int, source_domain: str, parent: str = "") -> None:
    with db.tx() as conn:
        conn.execute("INSERT INTO hub_pages(url, title, source_domain, last_fetch, links, parent) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(url) DO UPDATE SET title=CASE WHEN excluded.title!='' THEN excluded.title ELSE title END, "
                     "last_fetch=excluded.last_fetch, links=excluded.links",
                     (url, title[:300], source_domain, db.utcnow(), n_links, parent))


def record_yield(url: str, kid_urls: list[str], new: int) -> bool:
    """Ertrag einer geladenen Übersichtsseite festhalten; ab WATCH_MIN relevanten Artikeln beobachten.
    Rückgabe True, wenn die Seite dadurch neu beobachtet wird."""
    rel = 0
    for i in range(0, len(kid_urls), 200):
        chunk = kid_urls[i:i + 200]
        if chunk:
            rel += db.query_one(f"SELECT COUNT(*) n FROM articles WHERE relevant=1 AND url IN ({','.join('?' * len(chunk))})",
                                chunk)["n"]
    with db.tx() as conn:
        row = conn.execute("SELECT watch FROM hub_pages WHERE url=?", (url,)).fetchone()
        start = bool(row) and row["watch"] == 0 and rel >= WATCH_MIN
        conn.execute("UPDATE hub_pages SET relevant=?, new_last=?" + (", watch=1, watched_at=?" if start else "") +
                     " WHERE url=?", (rel, new, db.utcnow(), url) if start else (rel, new, url))
    if start:
        plan_pages(url)
    return start


def watched(enabled_domains: list[str] | None = None) -> list:
    rows = db.query("SELECT * FROM hub_pages WHERE watch>0 ORDER BY url")
    if enabled_domains is None:
        return rows
    from .textutil import domain_matches
    return [r for r in rows if any(domain_matches(r["url"], d) for d in enabled_domains)]


def set_watch(url: str, watch: int, source_domain: str = "") -> None:
    with db.tx() as conn:
        conn.execute("INSERT INTO hub_pages(url, title, source_domain, last_fetch, links, watch, watched_at) "
                     "VALUES(?, '', ?, '', 0, ?, ?) ON CONFLICT(url) DO UPDATE SET watch=excluded.watch, "
                     "watched_at=excluded.watched_at", (url, source_domain, watch, db.utcnow()))
    if watch > 0:
        plan_pages(url)


def seed() -> int:
    """Bekannte Übersichtsseiten einmalig als beobachtet eintragen (vom Admin Abgeschaltetes bleibt aus)."""
    from .sources import all_sources
    from .textutil import domain_matches
    n = 0
    doms = [s["domain"] for s in all_sources()]
    for url in SEED_HUBS:
        dom = next((d for d in doms if domain_matches(url, d)), None)
        if dom and not db.query_one("SELECT 1 FROM hub_pages WHERE url=?", (url,)):
            set_watch(url, 2, dom)
            n += 1
    return n


# ------------------------------------------------ Folgeseiten für den Historie-Abruf

_NEXT_REL = re.compile(r"<(?:link|a)\b[^>]*\brel=[\"']?next\b[^>]*>", re.I)
_HREF = re.compile(r"\bhref=[\"']([^\"']+)", re.I)
_NEXT_TEXT = re.compile(r"^\s*(?:weiter|nächste(?:\s+seite)?|naechste(?:\s+seite)?|ältere\s+(?:artikel|beiträge|meldungen|nachrichten)"
                        r"|mehr\s+(?:artikel|laden|anzeigen|nachrichten)|next(?:\s+page)?|older(?:\s+(?:posts|articles|stories))?"
                        r"|vorherige\s+artikel|[»›>]+)\s*$", re.I)
_PAGE_URL = re.compile(r"(?:[?&](?:page|p|seite|pg)=(\d+)|/(?:page|seite)[/-](\d+)/?$|-(\d+)\.html?$)", re.I)


def next_page(html: str, base_url: str) -> str:
    """Link auf die nächste (ältere) Seite einer Übersicht, sonst ''."""
    from urllib.parse import urljoin

    from .fetcher import _LinkParser
    for m in _NEXT_REL.finditer(html[:400_000]):
        h = _HREF.search(m.group(0))
        if h:
            return urljoin(base_url, h.group(1).replace("&amp;", "&"))
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    cur = _PAGE_URL.search(base_url)
    cur_n = int(next(g for g in cur.groups() if g)) if cur else 1
    for href, text in parser.links:
        if href and _NEXT_TEXT.match(text or ""):
            return urljoin(base_url, href)
    for href, _text in parser.links:             # sonst: Link auf Seite n+1 im selben Pfad
        url = urljoin(base_url, href or "")
        m = _PAGE_URL.search(url)
        if m and int(next(g for g in m.groups() if g)) == cur_n + 1 and url.split("?")[0].rstrip("/").startswith(
                base_url.split("?")[0].rstrip("/").rsplit("/page", 1)[0].rsplit("/seite", 1)[0]):
            return url
    return ""


def plan_pages(url: str) -> int:
    """Erste Folgeseite einer beobachteten Übersicht in den Historie-Abruf legen (weitere folgen Seite für Seite)."""
    from .sources import all_sources
    from .textutil import domain_matches
    src = next((s for s in all_sources() if domain_matches(url, s["domain"])), None)
    if not src:
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    with db.tx() as conn:
        return conn.execute("INSERT OR IGNORE INTO backfill_tasks(source_id, method, target, period_start, period_end, "
                            "priority, depth) VALUES(?, 'hub_page', ?, ?, ?, 1, 0)", (src["id"], url, today, today)).rowcount


def item_for(row_or_url, src, **extra):
    """Übersichtsseite als Abruf-Eintrag (für beobachtete Seiten und Folgeseiten)."""
    from .fetcher import Item, src_lang
    url = row_or_url if isinstance(row_or_url, str) else row_or_url["url"]
    title = "" if isinstance(row_or_url, str) else row_or_url["title"]
    return Item(url, title, "", datetime.now(timezone.utc), src["name"], src["domain"], "übersicht",
                lang=src_lang(src), extra={"watched_hub": True, **extra})


def children(hub, html: str, base_url: str, relevant: bool, subhubs: list | None = None, matcher=None) -> list:
    """Artikel-Links einer Übersichtsseite als neue Meldungen (Details werden noch geladen).
    subhubs: wird (falls angegeben) um verlinkte Unter-Übersichten mit Konzernbezug ergänzt."""
    from .fetcher import Item, extract_links
    from .textutil import domain_matches
    out, seen = [], {hub.url}
    for url, text in extract_links(_main_part(html), base_url, "", min_len=25):
        if url in seen or not domain_matches(url, hub.source_domain):
            continue
        if is_hub(text, url):
            if (subhubs is not None and len(subhubs) < MAX_SUBHUBS and matcher is not None
                    and matcher.score(text, "") > 0):
                seen.add(url)
                subhubs.append(Item(url, text, "", hub.published, hub.source_name, hub.source_domain, "übersicht",
                                    lang=hub.lang, extra={"parent_hub": hub.url}))
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


def expand(http, hub, relevant: bool, subhubs: list | None = None, matcher=None) -> list:
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
            remember(hub.url, hub.title, 0, hub.source_domain, hub.extra.get("parent_hub", ""))
            return []
        html, base = resp.text, str(resp.url)
    if not hub.title or hub.extra.get("watched_hub"):
        from .fetcher import parse_meta
        m = re.search(r"<title[^>]*>(.*?)</title>", html[:200_000], re.I | re.S)
        from .textutil import strip_html
        hub.title = (parse_meta(html).get("title") or (strip_html(m.group(1)) if m else "") or hub.title
                     or hub.url)[:300]
    kids = children(hub, html, base or hub.url, relevant, subhubs, matcher)
    remember(hub.url, hub.title, len(kids), hub.source_domain, hub.extra.get("parent_hub", ""))
    return kids
