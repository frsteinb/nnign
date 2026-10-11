import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NNIGN_DISABLE_SCHEDULER"] = "1"
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")


def rss(items, title="Testfeed"):
    out = [f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{title}</title>']
    for it in items:
        src = f'<source url="{it["src"]}">{it.get("pub_name", "X")}</source>' if it.get("src") else ""
        out.append(f'<item><title>{it["title"]}</title><link>{it["link"]}</link>'
                   f'<description>{it.get("desc", "")}</description>'
                   f'<pubDate>{format_datetime(it["date"])}</pubDate>{src}</item>')
    out.append("</channel></rss>")
    return "".join(out).encode()


class Mock:
    """Minimaler Webserver, der Feeds, Suche, Archivseiten und Wayback simuliert."""

    def __init__(self):
        self.routes = {}
        self.post_routes = {}   # Pfad -> fn(body: dict, headers) -> (status, dict)
        self.posts = []         # (Pfad, Header, Body) aller POST-Anfragen
        self.hits = []
        mock = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                mock.posts.append((self.path, dict(self.headers), body))
                handler = mock.post_routes.get(path)
                status, payload = handler(body, self.headers) if handler else (404, {"error": "unbekannt"})
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                mock.hits.append(self.path)
                path = urlsplit(self.path).path
                handler = mock.routes.get(path)
                if handler is None:
                    for prefix, h in mock.routes.items():
                        if prefix.endswith("*") and path.startswith(prefix[:-1]):
                            handler = h
                            break
                if handler is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body, ctype = handler(self.path)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    mock = Mock()
    for var in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "GEMINI_API_KEY", "MISTRAL_API_KEY", "OLLAMA_HOST"):
        monkeypatch.delenv(var, raising=False)
    from app import llm as _llm
    _llm._format_level.clear()
    _llm._pause.update(until=0.0, reason="", fails=0)
    from app import translate as _tr
    _tr._pause.update(until=0.0, reason="")
    monkeypatch.setenv("NNIGN_GNEWS_BASE", f"http://localhost:{mock.port}/gnews")
    monkeypatch.setenv("NNIGN_WAYBACK_BASE", mock.base)
    from app import db, fetcher
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetcher, "GNEWS_BASE", f"http://localhost:{mock.port}/gnews")
    monkeypatch.setattr(fetcher, "WAYBACK_BASE", mock.base)
    db.init_db()
    from app import config
    config.set("host_delay_seconds", 0)
    config.set("wayback_delay_seconds", 0)
    config.reset("anthropic_api_key")
    from app import pipeline
    pipeline._breaker_counts.clear()
    yield mock
    mock.close()


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def setup_world(mock):
    """Zwei Testquellen mit realistischen Meldungen."""
    from app import db
    a_feed = [
        dict(title="VW kündigt Abbau von 35.000 Stellen in Deutschland an", link=f"{mock.base}/a/stellenabbau",
             desc="Volkswagen will bis 2030 in Deutschland 35.000 Stellen abbauen. Werksschließungen sind vom Tisch.",
             date=NOW - timedelta(days=1)),
        dict(title="VfL Wolfsburg gewinnt gegen Bayern München", link=f"{mock.base}/a/fussball",
             desc="Volkswagen-Klub siegt 2:1.", date=NOW - timedelta(days=1)),
        dict(title="BMW meldet Rekordabsatz in China", link=f"{mock.base}/a/bmw", desc="BMW wächst.",
             date=NOW - timedelta(days=2)),
        dict(title="Audi startet Produktion des Q6 e-tron in Ingolstadt",
             link=f"{mock.base}/a/audi-q6?utm_source=rss",
             desc="Der Elektro-SUV läuft vom Band, Audi investiert Milliarden in den Standort.",
             date=NOW - timedelta(days=2)),
    ]
    b_feed = [
        dict(title="Volkswagen: 35.000 Stellen fallen weg – IG Metall kritisiert Abbau",
             link="http://localhost/b/vw-stellen",
             desc="Beim Autobauer Volkswagen fallen in Deutschland 35.000 Stellen weg, die Gewerkschaft kritisiert den Abbau.",
             date=NOW - timedelta(hours=20)),
    ]
    search_items = [
        dict(title="Porsche hebt Prognose an - Testmedium B", link="https://news.google.com/rss/articles/abc",
             src="http://localhost/", pub_name="Testmedium B", date=NOW - timedelta(days=3)),
        dict(title="Fremdes Medium über VW - Anderes Blatt", link="https://news.google.com/rss/articles/xyz",
             src="https://anderes-blatt.de/", pub_name="Anderes Blatt", date=NOW - timedelta(days=3)),
    ]
    mock.routes["/feeds/a.rss"] = lambda p: (rss(a_feed), "application/rss+xml")
    mock.routes["/feeds/b.rss"] = lambda p: (rss(b_feed), "application/rss+xml")
    mock.routes["/gnews"] = lambda p: (rss(search_items), "application/rss+xml")

    # Archivseiten (Backfill) – nur der 20.09.2015 enthält eine VW-Meldung
    def archive(path):
        if "20.09.2015" in path:
            html = ('<html><body><a href="/art/vw-abgas.html">VW gibt Manipulation von Abgaswerten in den USA zu</a>'
                    '<a href="/art/wetter.html">Das Wetter am Wochenende wird sonnig und warm</a>'
                    '<a href="https://fremd.example/vw">VW-Link auf fremde Domain wird ignoriert</a></body></html>')
        else:
            html = '<html><body><a href="/art/x.html">Eine ganz andere Meldung ohne Bezug heute</a></body></html>'
        return html.encode(), "text/html"

    mock.routes["/archiv/*"] = archive
    mock.routes["/art/vw-abgas.html"] = lambda p: (
        b'<html><head><meta property="og:description" content="Volkswagen hat eingeraeumt, Abgaswerte von '
        b'Dieselautos in den USA manipuliert zu haben. Die US-Umweltbehoerde droht mit Strafen."> '
        b'<meta property="article:published_time" content="2015-09-20T18:30:00+02:00"></head></html>', "text/html")

    # Wayback: CDX-Index und Schnappschuss des Feeds a
    def cdx(path):
        rows = [["timestamp", "original"], ["20150922120000", f"{mock.base}/feeds/a.rss"]]
        return json.dumps(rows).encode(), "application/json"

    old_feed = [
        dict(title="Abgas-Skandal: Winterkorn tritt als VW-Chef zurück", link=f"{mock.base}/a/winterkorn",
             desc="Nach dem Abgas-Skandal übernimmt Martin Winterkorn die Verantwortung und tritt zurück.",
             date=datetime(2015, 9, 23, 15, tzinfo=timezone.utc)),
        dict(title="VW räumt Manipulation der Abgaswerte in den USA ein", link=f"{mock.base}/a/abgas-usa",
             desc="Volkswagen hat Abgaswerte von Dieselautos in den USA manipuliert.",
             date=datetime(2015, 9, 21, 9, tzinfo=timezone.utc)),
    ]
    mock.routes["/cdx/search/cdx"] = cdx
    mock.routes["/web/*"] = lambda p: (rss(old_feed), "application/rss+xml")

    with db.tx() as conn:
        conn.execute("DELETE FROM sources")
        now = db.utcnow()
        conn.execute("INSERT INTO sources(name, domain, feeds, use_search, archive_template, archive_step, "
                     "archive_since, link_pattern, enabled, created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
                     ("Testmedium A", "127.0.0.1", f"{mock.base}/feeds/a.rss", 0,
                      f"{mock.base}/archiv/artikel-{{DD}}.{{MM}}.{{YYYY}}.html", "day", "2015-09-18", r"/art/", now))
        conn.execute("INSERT INTO sources(name, domain, feeds, use_search, enabled, created_at) VALUES(?,?,?,?,1,?)",
                     ("Testmedium B", "localhost", f"{mock.base}/feeds/b.rss", 1, now))
