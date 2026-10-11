"""Übersichtsseiten (z. B. NDR „VW – News und Hintergründe“) werden aufgelöst statt gespeichert."""
from datetime import timedelta

from tests.conftest import NOW, rss


def test_detection():
    from app import hubs
    for t in ["VW - News und Hintergründe", "Volkswagen: Aktuelle Nachrichten und Berichte", "Alle Artikel zu Volkswagen",
              "Nachrichten zum Thema Volkswagen", "VW | News", "Thema: Volkswagen", "Volkswagen – latest news and updates"]:
        assert hubs.is_hub_title(t), t
    for t in ["VW kündigt Abbau von 35.000 Stellen an", "Die Einigung bei VW im Überblick",
              "Porsche zeigt neuen 911 Turbo S", "Was der Kompromiss für die Beschäftigten bedeutet"]:
        assert not hubs.is_hub_title(t), t
    assert hubs.is_hub_url("https://www.spiegel.de/thema/volkswagen/")
    assert hubs.is_hub_url("https://www.tagesschau.de/thema/vw")
    assert not hubs.is_hub_url("https://www.spiegel.de/wirtschaft/vw-streicht-stellen-a-123.html")


def _world(mock):
    from app import db
    day = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    def art(desc, date=day):
        meta = f'<meta property="og:type" content="article"><meta property="og:description" content="{desc}">'
        if date:
            meta += f'<meta property="article:published_time" content="{date}">'
        return lambda p: (f"<html><head>{meta}</head><body>x</body></html>".encode(), "text/html")

    hub_html = """<html><head><meta property="og:type" content="website"></head><body>
      <nav><a href="/sport/vfl.html">VfL Wolfsburg gewinnt das Derby gegen Hannover 96</a></nav>
      <main>
        <a href="/nds/vw-tarif.html">VW und IG Metall einigen sich auf Tarifvertrag für die Werke</a>
        <a href="/nds/emden.html">Betriebsrat warnt vor weiteren Einschnitten im Werk Emden</a>
        <a href="/nds/ohne-datum.html">Volkswagen plant neues Ausbildungszentrum in Wolfsburg</a>
        <a href="/thema/porsche/">Porsche – alle Nachrichten und Hintergründe</a>
        <a href="https://fremd.example/vw">VW-Meldung auf einer fremden Seite ohne Bezug</a>
      </main></body></html>"""
    mock.routes["/hub/vw.html"] = lambda p: (hub_html.encode(), "text/html")
    mock.routes["/nds/vw-tarif.html"] = art("Nach langen Verhandlungen gibt es eine Einigung für 120.000 Beschäftigte.")
    mock.routes["/nds/emden.html"] = art("Der Betriebsrat sieht die Beschäftigung am Standort gefährdet.")
    mock.routes["/nds/ohne-datum.html"] = art("Ein neues Zentrum für Auszubildende.", date=None)
    feed = [dict(title="VW - News und Hintergründe", link=f"{mock.base}/hub/vw.html",
                 desc="Aktuelle Nachrichten zu Volkswagen aus Niedersachsen.", date=NOW - timedelta(hours=3))]
    mock.routes["/feeds/ndr.rss"] = lambda p: (rss(feed), "application/rss+xml")
    with db.tx() as conn:
        conn.execute("DELETE FROM sources")
        conn.execute("INSERT INTO sources(name, domain, feeds, use_search, enabled, created_at) VALUES(?,?,?,?,1,?)",
                     ("NDR", "127.0.0.1", f"{mock.base}/feeds/ndr.rss", 0, db.utcnow()))


def test_hub_is_expanded_not_stored(env):
    from app import db, pipeline
    _world(env)
    pipeline.run_scan("initial")
    titles = {r["title"]: r for r in db.query("SELECT * FROM articles WHERE relevant=1")}
    assert "VW - News und Hintergründe" not in titles                       # Übersicht selbst kein Artikel
    assert "VW und IG Metall einigen sich auf Tarifvertrag für die Werke" in titles
    a = titles["VW und IG Metall einigen sich auf Tarifvertrag für die Werke"]
    assert a["via"] == "übersicht" and "Einigung" in a["summary"]
    assert "Betriebsrat warnt vor weiteren Einschnitten im Werk Emden" in titles   # Relevanz von der Übersicht
    assert not any("VfL" in t for t in titles)                              # Navigation ignoriert
    assert not any("Ausbildungszentrum" in t for t in titles)              # ohne Datum nicht übernommen
    assert not any("fremden Seite" in t for t in titles)
    assert db.query_one("SELECT links FROM hub_pages")["links"] == 3
    # erneuter Scan: Übersicht erst nach 12 h wieder laden
    n = len(env.hits)
    pipeline.run_scan("manuell")
    assert not any("/hub/vw.html" in h for h in env.hits[n:])


def test_stored_hub_articles_are_cleaned_up(env):
    from app import config, db, maintenance, pipeline
    _world(env)
    with db.tx() as conn:
        conn.execute("INSERT INTO articles(url, title, title_key, summary, source_name, source_domain, published_at, "
                     "fetched_at, via, relevant) VALUES(?,?,?,?,?,?,?,?,?,1)",
                     (f"{env.base}/hub/vw.html", "VW - News und Hintergründe", "vw news und hintergrunde", "", "NDR",
                      "127.0.0.1", NOW.isoformat(), NOW.isoformat(), "rss"))
    config.state_set("bases", {**maintenance.bases(), "hubs": 0})
    pipeline.upgrade_on_start({})
    notes = maintenance.check()
    assert any("Übersichtsseiten" in n for n in notes)
    assert db.query_one("SELECT relevant FROM articles WHERE title='VW - News und Hintergründe'")["relevant"] == 0
    maintenance.work()
    assert db.query_one("SELECT 1 FROM articles WHERE via='übersicht' AND relevant=1")
