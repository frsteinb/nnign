"""Die Entwicklung gemäß Filter berücksichtigt alle Filter; die Startseite zeigt leere Filterfelder."""


def _topics():
    from app import config, db
    config.set("collect_since", "2020-01-01")
    rows = [("A", "positiv", "2021-03-05"), ("B", "negativ", "2021-03-20"), ("C", "negativ", "2022-07-01"),
            ("D", "neutral", "2024-01-10"), ("E", "negativ", "2024-02-11")]
    with db.tx() as conn:
        for title, senti, d in rows:
            tid = conn.execute("INSERT INTO topics(title, sentiment, first_published_at, last_published_at, created_at, "
                               "updated_at) VALUES(?,?,?,?,?,?)", (f"VW {title}", senti, d + "T10:00:00+00:00",
                                                                   d + "T10:00:00+00:00", db.utcnow(), db.utcnow())).lastrowid
            conn.execute("INSERT INTO articles(url, title, title_key, summary, source_name, source_domain, published_at, "
                         "fetched_at, via, relevant, topic_id) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                         (f"https://x/{title}", f"VW {title}", title, "", "X", "spiegel.de" if title in "AC" else "zeit.de",
                          d + "T10:00:00+00:00", db.utcnow(), "rss", tid))


def _sum(tl, k):
    return sum(p[k] for p in tl)


def test_timeline_respects_all_filters(env):
    from app import main
    _topics()
    empty = {k: "" for k in ("q", "sentiment", "year", "source", "date_from", "date_to", "scope")}
    tl = main.timeline({**empty, "sentiment": "negativ"})
    assert _sum(tl, "pos") == 0 and _sum(tl, "neu") == 0 and _sum(tl, "neg") == 3          # Bewertung wirkt
    tl = main.timeline({**empty, "source": "spiegel.de"})
    assert _sum(tl, "pos") + _sum(tl, "neg") + _sum(tl, "neu") == 2
    tl = main.timeline({**empty, "date_from": "2021-02-01", "date_to": "2021-04-30"})      # kurzer Bereich: Monate
    assert [p["key"] for p in tl] == ["2021-02", "2021-03", "2021-04"] and tl[1]["pos"] == 1 and tl[1]["neg"] == 1
    tl = main.timeline({**empty, "date_from": "2021-01-01", "date_to": "2024-12-31"})      # langer Bereich: Jahre
    assert [p["key"] for p in tl] == ["2021", "2022", "2023", "2024"]
    tl = main.timeline({**empty, "year": "2024", "sentiment": "negativ"})
    assert len(tl) == 12 and tl[1]["neg"] == 1 and _sum(tl, "neu") == 0


def test_start_page_has_empty_filters_and_filtered_chart(env):
    from fastapi.testclient import TestClient

    from app import main
    _topics()
    with TestClient(main.app) as c:
        page = c.get("/").text
        assert 'name="q" value=""' in page and "Gefilterter Datenbestand" not in page
        assert "selected" not in page.split('name="sentiment"')[1].split("</select>")[0]
        page = c.get("/?sentiment=negativ&date_from=2021-01-01&date_to=2021-12-31").text
        assert "Entwicklung 2021 nach Monat gemäß Filter" in page
        assert 'date_to=2021-03-31' in page                                     # Monatsende korrekt
