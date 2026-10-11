from datetime import date

from app import fetcher, sentiment, textutil
from app.relevance import Matcher, _compile
from app.config import DEFAULT_BRANDS, DEFAULT_EXCLUDE, DEFAULT_STRONG


def matcher():
    return Matcher(_compile(DEFAULT_STRONG + DEFAULT_BRANDS), _compile(DEFAULT_EXCLUDE), 2)


def test_relevance():
    m = matcher()
    assert m.score("VW streicht Stellen", "") > 0
    assert m.score("Volkswagens Absatz sinkt", "") > 0
    assert m.score("VW-Konzern im Umbruch", "") > 0
    assert m.score("Audi investiert in Ingolstadt", "") > 0
    assert m.score("VfL Wolfsburg gewinnt", "Volkswagen-Klub") == 0
    assert m.score("BMW wächst", "Auch VW profitiert") == 0          # nur ein Teaser-Treffer
    assert m.score("Autoindustrie in der Krise", "VW und Audi leiden") > 0  # zwei Teaser-Treffer
    assert m.score("Neue Vwxyz-Regeln", "") == 0
    assert m.score("Kommt man heute noch mit", "") == 0               # 'man' ≠ MAN Truck


def test_sentiment_basics():
    # ausführliche Referenzfälle: tests/test_sentiment.py
    assert sentiment.score("VW meldet Rekordgewinn") > 0
    assert sentiment.score("Volkswagen streicht 35.000 Stellen") < 0
    assert sentiment.label(0) == "neutral"
    assert sentiment.label(4) == "positiv" and sentiment.label(-4) == "negativ"


def test_textutil():
    assert textutil.normalize_url("http://www.X.de/a?utm_source=rss&id=3#top") == "http://www.x.de/a?id=3"
    assert textutil.title_key("VW: Abbau – 35.000 Stellen!") == "vw abbau 35 000 stellen"
    assert "volkswagen" not in textutil.tokens("Volkswagen streicht Stellen")
    assert textutil.domain_matches("https://www.spiegel.de/x", "spiegel.de")
    assert not textutil.domain_matches("https://spiegel.de.evil.com/x", "spiegel.de")
    assert textutil.shorten("a" * 1000, 50).endswith("…") or len(textutil.shorten("a" * 1000, 50)) <= 52


def test_periods_and_template():
    days = list(fetcher.iter_periods(date(2000, 1, 30), date(2000, 2, 2), "day"))
    assert len(days) == 4
    months = list(fetcher.iter_periods(date(2000, 1, 15), date(2000, 3, 1), "month"))
    assert [m[0] for m in months] == [date(2000, 1, 1), date(2000, 2, 1), date(2000, 3, 1)]
    assert fetcher.render_template("x/{DD}.{MM}.{YYYY}/{WW}", date(2005, 3, 7)) == "x/07.03.2005/10"


def test_priority_coarse_to_fine():
    from app.pipeline import _priority
    pr = [_priority(i) for i in range(64)]
    assert pr[0] == 0 and pr[32] == 0 and pr[16] == 1 and pr[1] == 5


def test_extract_links_and_meta():
    html = ('<a href="/wirtschaft/vw-a-1.html"><span>Kicker</span> VW streicht 35.000 Stellen bis 2030</a>'
            '<a href="https://web.archive.org/web/20050101/http://www.spiegel.de/wirtschaft/0,1518,1,00.html">'
            'Porsche steigt bei Volkswagen ein – Wayback-Link</a><a href="#">kurz</a>')
    links = fetcher.extract_links(html, "https://www.spiegel.de/nachrichtenarchiv/x.html", r"spiegel\.de/wirtschaft")
    assert links[0] == ("https://www.spiegel.de/wirtschaft/vw-a-1.html", "Kicker VW streicht 35.000 Stellen bis 2030")
    assert links[1][0] == "http://www.spiegel.de/wirtschaft/0,1518,1,00.html"
    meta = fetcher.parse_meta('<meta name="description" content="Teaser &amp; mehr">'
                              '<script>{"datePublished": "2009-05-06T10:00:00Z"}</script>')
    assert meta["description"] == "Teaser & mehr"
    assert meta["published"].year == 2009


def test_search_url():
    u = fetcher.search_url("Volkswagen OR VW", "spiegel.de", after=date(2000, 1, 1), before=date(2000, 4, 1))
    assert "site%3Aspiegel.de" in u and "after%3A2000-01-01" in u and "hl=de" in u


def test_partners_are_relevant_and_marked(env):
    from app.relevance import Matcher
    m = Matcher.from_config()
    for t in ["Gotion baut Batteriefabrik in Göttingen aus", "Xpeng meldet Rekordauslieferungen",
              "Rivian verschiebt Start des R2", "Scout Motors eröffnet Werk in South Carolina", "小鹏汽车销量增长"]:
        assert m.score(t, "") > 0, t
    assert m.scope("Xpeng meldet Rekordauslieferungen", "") == "partner"
    assert m.scope("VW und Xpeng bauen gemeinsames Modell", "") == "konzern"
    assert m.scope("Scout Motors eröffnet Werk", "") == "konzern"                # eigene Tochter
    assert m.score("Boy Scouts feiern Jubiläum", "") == 0


def test_partner_scope_in_pipeline_and_filter(env):
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from app import config, db, main, pipeline
    from app.fetcher import Item
    config.set("collect_since", "2000-01-01")
    now = datetime.now(timezone.utc)
    pipeline.ingest([Item("https://a.de/1", "Xpeng meldet Rekordauslieferungen im September", "", now, "A", "a.de", "rss"),
                     Item("https://a.de/2", "VW kündigt Sparprogramm an", "", now, "A", "a.de", "rss")],
                    pipeline.Ctx("t"), allow_llm=False)
    assert db.query_one("SELECT scope FROM topics WHERE title LIKE 'Xpeng%'")["scope"] == "partner"
    assert db.query_one("SELECT scope FROM topics WHERE title LIKE 'VW%'")["scope"] == "konzern"
    assert "Xpeng" in pipeline._query_for({"language": "de"})
    with TestClient(main.app) as c:
        assert "Partner</span>" in c.get("/").text
        assert "Xpeng" in c.get("/?scope=partner").text and "Sparprogramm" not in c.get("/?scope=partner").text
