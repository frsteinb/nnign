"""Abruf von RSS-Feeds, Nachrichtensuche, Archivseiten und Internet-Archive-Schnappschüssen."""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin

import feedparser
import httpx

from . import config
from .textutil import domain_matches, normalize_ws, strip_html

log = logging.getLogger(__name__)

# Über Umgebungsvariablen überschreibbar (z. B. für Tests oder einen eigenen Proxy)
GNEWS_BASE = os.environ.get("NNIGN_GNEWS_BASE", "https://news.google.com/rss/search")
WAYBACK_BASE = os.environ.get("NNIGN_WAYBACK_BASE", "https://web.archive.org")


@dataclass
class Item:
    url: str
    title: str
    summary: str
    published: datetime
    source_name: str
    source_domain: str
    via: str  # rss | suche | archiv | wayback
    needs_details: bool = False
    extra: dict = field(default_factory=dict)


class FetchError(Exception):
    pass


class Http:
    """HTTP-Client mit Mindestabstand je Host."""

    def __init__(self):
        self.delay = float(config.get("host_delay_seconds"))
        self.wayback_delay = max(self.delay, float(config.get("wayback_delay_seconds")))
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=float(config.get("request_timeout")),
            headers={
                "User-Agent": config.get("user_agent"),
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
                "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml;q=0.9,*/*;q=0.8",
            },
        )
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = threading.Lock()

    def _host_lock(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def get(self, url: str) -> httpx.Response:
        host = httpx.URL(url).host
        delay = self.wayback_delay if host.endswith("archive.org") else self.delay
        with self._host_lock(host):
            for attempt in range(2):
                wait = self._last.get(host, 0) + delay - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    resp = self.client.get(url)
                finally:
                    self._last[host] = time.monotonic()
                if resp.status_code not in (429, 503) or attempt:
                    break
                # Ratelimit: einmal nach Retry-After (max. 2 Minuten) erneut versuchen
                try:
                    pause = min(120.0, float(resp.headers.get("retry-after", "30")))
                except ValueError:
                    pause = 30.0
                log.info("HTTP %s von %s – warte %.0f s", resp.status_code, host, pause)
                time.sleep(pause)
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} für {url}")
        return resp

    def close(self):
        self.client.close()


# ---------------------------------------------------------------- Datumshilfen

def _struct_to_dt(st) -> datetime | None:
    if not st:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    except (OverflowError, ValueError, TypeError):
        return None


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    for candidate in (v, v[:25], v[:19], v[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- Feeds

def parse_feed(content: bytes, source_name: str, source_domain: str, via: str,
               default_date: datetime | None = None, require_domain: bool = False) -> list[Item]:
    parsed = feedparser.parse(content)
    items = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = strip_html(e.get("title") or "")
        if not link or not title:
            continue
        if require_domain and not domain_matches(link, source_domain):
            continue
        summary = strip_html(e.get("summary") or e.get("description") or "")
        if summary.lower().startswith(title.lower()) and len(summary) < len(title) + 40:
            summary = ""
        published = (_struct_to_dt(e.get("published_parsed")) or _struct_to_dt(e.get("updated_parsed"))
                     or default_date or datetime.now(timezone.utc))
        items.append(Item(link, title, summary, published, source_name, source_domain, via))
    return items


def fetch_feed(http: Http, url: str, src) -> list[Item]:
    resp = http.get(url)
    return parse_feed(resp.content, src["name"], src["domain"], "rss")


# ---------------------------------------------------------------- Nachrichtensuche (Google News RSS)

def search_url(query: str, domain: str | None, after: date | None = None, before: date | None = None,
               when_days: int | None = None) -> str:
    q = f"({query})" if " OR " in query else query
    if domain:
        q += f" site:{domain}"
    if after:
        q += f" after:{after.isoformat()}"
    if before:
        q += f" before:{before.isoformat()}"
    if when_days and not (after or before):
        q += f" when:{when_days}d"
    return f"{GNEWS_BASE}?q={quote_plus(q)}&hl=de&gl=DE&ceid=DE:de"


_PUBLISHER_SUFFIX = re.compile(r"\s+[-–|]\s+[^-–|]{2,60}$")


def parse_search(content: bytes, src) -> list[Item]:
    parsed = feedparser.parse(content)
    items = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = strip_html(e.get("title") or "")
        if not link or not title:
            continue
        src_info = e.get("source") or {}
        publisher = (src_info.get("title") or "").strip()
        href = (src_info.get("href") or "").strip()
        if href and not domain_matches(href, src["domain"]):
            continue  # Treffer eines anderen Mediums
        if publisher and title.endswith(publisher):
            title = title[: -len(publisher)].rstrip(" -–|")
        else:
            title = _PUBLISHER_SUFFIX.sub("", title)
        published = (_struct_to_dt(e.get("published_parsed")) or datetime.now(timezone.utc))
        # Google-News-Beschreibungen enthalten nur Link-Listen -> kein Teaser
        items.append(Item(link, title, "", published, src["name"], src["domain"], "suche"))
    return items


def fetch_search(http: Http, src, query: str, after=None, before=None, when_days=None) -> list[Item]:
    resp = http.get(search_url(query, src["domain"], after, before, when_days))
    return parse_search(resp.content, src)


# ---------------------------------------------------------------- Archivseiten

def render_template(template: str, d: date) -> str:
    iso_year, iso_week, _ = d.isocalendar()
    return (template.replace("{YYYY}", f"{d.year:04d}").replace("{MM}", f"{d.month:02d}")
            .replace("{DD}", f"{d.day:02d}").replace("{M}", str(d.month)).replace("{D}", str(d.day))
            .replace("{WW}", f"{iso_week:02d}"))


def iter_periods(start: date, end: date, step: str):
    """Liefert (Periodenbeginn, Periodenende) von start bis end."""
    d = start
    if step == "week":
        d = d - timedelta(days=d.weekday())
    elif step == "month":
        d = d.replace(day=1)
    elif step == "year":
        d = d.replace(month=1, day=1)
    while d <= end:
        if step == "day":
            nxt = d + timedelta(days=1)
        elif step == "week":
            nxt = d + timedelta(days=7)
        elif step == "month":
            nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            nxt = d.replace(year=d.year + 1)
        yield d, min(nxt - timedelta(days=1), end)
        d = nxt


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._stack: list[list] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            a = dict(attrs)
            self._stack.append([a.get("href") or "", [], a.get("title") or ""])

    def handle_endtag(self, tag):
        if tag == "a" and self._stack:
            href, parts, title_attr = self._stack.pop()
            text = normalize_ws(" ".join(parts)) or normalize_ws(title_attr)
            self.links.append((href, text))

    def handle_data(self, data):
        for entry in self._stack:
            entry[1].append(data)


def extract_links(html_text: str, base_url: str, link_pattern: str, min_len: int = 25) -> list[tuple[str, str]]:
    parser = _LinkParser()
    try:
        parser.feed(html_text)
    except Exception:  # kaputtes HTML: nehmen, was bisher gefunden wurde
        pass
    pat = re.compile(link_pattern) if link_pattern else None
    seen = set()
    out = []
    for href, text in parser.links:
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(base_url, href)
        # Wayback-Links auf die Original-URL zurückführen
        m = re.match(r"https?://[^/]+/web/\d{8,14}[a-z_]*/(.+)", url)
        if m:
            url = m.group(1)
            if not url.startswith("http"):
                url = "https://" + url.lstrip("/")
        if pat and not pat.search(url):
            continue
        if len(text) < min_len or url in seen:
            continue
        seen.add(url)
        out.append((url, text))
    return out


def fetch_archive_page(http: Http, src, url: str, page_date: date, wayback_fallback: bool) -> list[Item]:
    try:
        resp = http.get(url)
        base = str(resp.url)
        via = "archiv"
    except (FetchError, httpx.HTTPError) as exc:
        if not wayback_fallback:
            raise
        stamp = page_date.strftime("%Y%m%d") + "235959"
        try:
            resp = http.get(f"{WAYBACK_BASE}/web/{stamp}/{url}")
        except (FetchError, httpx.HTTPError) as exc2:
            raise FetchError(f"{exc}; Wayback: {exc2}") from exc2
        base = url
        via = "wayback"
    links = extract_links(resp.text, base, src["link_pattern"])
    published = day_start(page_date)
    return [Item(u, t, "", published, src["name"], src["domain"], via, needs_details=True)
            for u, t in links if domain_matches(u, src["domain"])]


# ---------------------------------------------------------------- Artikeldetails (Meta-Tags)

_META = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR = re.compile(r'([a-zA-Z_:.-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')
_JSONLD_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')


def parse_meta(html_text: str) -> dict:
    head = html_text[:300_000]
    meta = {}
    for tag in _META.findall(head):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4))
                 for m in _ATTR.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key and "content" in attrs and key not in meta:
            meta[key] = strip_html(attrs["content"])
    out = {
        "title": meta.get("og:title") or meta.get("twitter:title") or "",
        "description": meta.get("og:description") or meta.get("description") or meta.get("twitter:description") or "",
        "published": None,
    }
    for key in ("article:published_time", "date", "datepublished", "dc.date", "dc.date.issued", "pubdate"):
        if meta.get(key):
            out["published"] = parse_iso(meta[key])
            if out["published"]:
                break
    if not out["published"]:
        m = _JSONLD_DATE.search(head)
        if m:
            out["published"] = parse_iso(m.group(1))
    return out


def enrich(http: Http, item: Item) -> Item:
    """Ergänzt Teaser und genaues Datum aus der Artikelseite (best effort)."""
    try:
        resp = http.get(item.url)
    except (FetchError, httpx.HTTPError):
        if item.via != "wayback":
            return item
        try:
            stamp = item.published.strftime("%Y%m%d")
            resp = http.get(f"{WAYBACK_BASE}/web/{stamp}/{item.url}")
        except (FetchError, httpx.HTTPError):
            return item
    meta = parse_meta(resp.text)
    if meta["description"]:
        item.summary = meta["description"]
    pub = meta["published"]
    if pub and abs((pub - item.published).days) <= 31:
        item.published = pub
    return item


# ---------------------------------------------------------------- Internet Archive (Wayback)

def wayback_index(http: Http, feed_url: str, since: date, until: date, granularity: str) -> list[tuple[str, str]]:
    collapse = {"day": "timestamp:8", "week": "timestamp:8", "month": "timestamp:6"}[granularity]
    url = (f"{WAYBACK_BASE}/cdx/search/cdx?url={quote_plus(feed_url)}&output=json"
           f"&from={since.strftime('%Y%m%d')}&to={until.strftime('%Y%m%d')}"
           f"&filter=statuscode:200&collapse={collapse}&fl=timestamp,original")
    resp = http.get(url)
    text = resp.text.strip()
    if not text:
        return []
    rows = json.loads(text)
    if rows and rows[0] and rows[0][0] == "timestamp":
        rows = rows[1:]
    out, seen_weeks = [], set()
    for ts, original in rows:
        if granularity == "week":
            d = datetime.strptime(ts[:8], "%Y%m%d").date()
            wk = d.isocalendar()[:2]
            if wk in seen_weeks:
                continue
            seen_weeks.add(wk)
        out.append((ts, original))
    return out


def wayback_snapshot_url(ts: str, original: str) -> str:
    return f"{WAYBACK_BASE}/web/{ts}id_/{original}"


def fetch_wayback_feed(http: Http, src, ts: str, original: str) -> list[Item]:
    resp = http.get(wayback_snapshot_url(ts, original))
    snap_date = datetime.strptime(ts[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
    return parse_feed(resp.content, src["name"], src["domain"], "wayback", default_date=snap_date)
