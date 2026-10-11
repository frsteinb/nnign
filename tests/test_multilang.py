"""Fremdsprachige Quellen, Übersetzung und Abgleich nach einem Image-Update."""
from datetime import timedelta

from tests.conftest import NOW, rss


# ---------------------------------------------------------------- Erkennung

def test_detect_lang():
    from app.textutil import detect_lang
    assert detect_lang("Volkswagen recalls cars over airbag problems in the US") == "en"
    assert detect_lang("Volkswagen ruft Autos wegen Airbag-Problemen zurück") == "de"
    assert detect_lang("上汽大众9月销量下滑") == "zh"
    assert detect_lang("VW ID.2", default="en") == "en"


def test_cjk_relevance(env):
    from app.relevance import Matcher
    m = Matcher.from_config()
    assert m.score("上汽大众9月销量下滑", "") > 0
    assert m.score("大众消费持续增长", "") == 0          # „大众“ allein = Öffentlichkeit
    assert m.score("Volkswagen deliveries rise in China", "") > 0


def test_english_and_chinese_rules():
    from app import sentiment as s
    lab = lambda t, lang: s.label_for(s.analyze(t, "", lang))  # noqa: E731
    assert lab("Volkswagen recalls 50,000 cars over airbag problems", "en") == "negativ"
    assert lab("VW deliveries rise 10% in China", "en") == "positiv"
    assert lab("Porsche raises guidance after record quarter", "en") == "positiv"
    assert lab("Audi launch delayed by software glitches", "en") == "negativ"
    assert lab("大众汽车召回5万辆车", "zh") == "negativ"
    assert lab("上汽大众9月销量增长20%", "zh") == "positiv"
    # Deutsche Regeln greifen auf englischen Text kaum -> deshalb sprachabhängig
    assert lab("Volkswagen recalls 50,000 cars over airbag problems", "de") != "negativ"


# ---------------------------------------------------------------- Übersetzung im Lauf

EN_FEED = [
    dict(title="Volkswagen recalls 50,000 cars over airbag problems", link="http://localhost/en/recall",
         desc="Volkswagen is recalling about 50,000 vehicles in the United States because of faulty airbags.",
         date=NOW - timedelta(hours=5)),
]


def _english_world(mock):
    from app import db
    mock.routes["/feeds/en.rss"] = lambda p: (rss(EN_FEED), "application/rss+xml")
    with db.tx() as conn:
        conn.execute("DELETE FROM sources")
        conn.execute("INSERT INTO sources(name, domain, feeds, use_search, language, enabled, created_at) "
                     "VALUES(?,?,?,?,?,1,?)", ("Test EN", "localhost", f"{mock.base}/feeds/en.rss", 0, "en",
                                               db.utcnow()))


def _libre(mock):
    def handler(body, headers):
        q = body["q"] if isinstance(body["q"], list) else [body["q"]]
        out = ["Volkswagen ruft 50.000 Autos wegen Airbag-Problemen zurück" if "recalls" in t else "DE: " + t
               for t in q]
        return 200, {"translatedText": out}
    mock.post_routes["/translate"] = handler


def test_scan_translates_with_libretranslate(env):
    from app import config, db, pipeline, translate
    _english_world(env)
    _libre(env)
    config.set("libretranslate_url", env.base)
    assert translate.provider() == "libretranslate"
    pipeline.run_scan("initial")
    a = db.query_one("SELECT * FROM articles")
    assert a["lang"] == "en" and a["translated_by"] == "libretranslate"
    assert a["title"].startswith("Volkswagen ruft 50.000 Autos")
    assert a["title_orig"] == EN_FEED[0]["title"]
    assert a["summary"].startswith("DE: ") and a["summary_orig"].startswith("Volkswagen is recalling")
    body = env.posts[0][2]
    assert body["source"] == "en" and body["target"] == "de"
    t = db.query_one("SELECT * FROM topics")
    assert t["sentiment"] == "negativ" and t["title"].startswith("Volkswagen ruft")


def test_deepl_provider(env):
    from app import config, translate
    seen = {}

    def handler(body, headers):
        seen["auth"] = headers.get("Authorization")
        seen["body"] = body
        return 200, {"translations": [{"text": "Übersetzt: " + t} for t in body["text"]]}
    env.post_routes["/v2/translate"] = handler
    config.set("deepl_api_key", "abc:fx")
    config.set("deepl_url", env.base + "/v2/translate")
    assert translate.provider() == "deepl"
    out, used = translate.translate_texts(["VW sales rise"], "en")
    assert used == "deepl" and out == ["Übersetzt: VW sales rise"]
    assert seen["auth"] == "DeepL-Auth-Key abc:fx" and seen["body"]["source_lang"] == "EN"
    assert "OK (DeepL)" in translate.test_connection()


def test_without_translator_keeps_original_and_uses_english_rules(env):
    from app import db, pipeline, translate
    _english_world(env)
    assert translate.provider() == ""
    pipeline.run_scan("initial")
    a = db.query_one("SELECT * FROM articles")
    assert a["lang"] == "en" and a["translated_by"] == "" and a["title"] == EN_FEED[0]["title"]
    assert db.query_one("SELECT sentiment FROM topics")["sentiment"] == "negativ"


def test_translate_pending_later(env):
    from app import config, db, pipeline
    _english_world(env)
    pipeline.run_scan("initial")
    _libre(env)
    config.set("libretranslate_url", env.base)
    assert pipeline.translate_pending() == 1
    a = db.query_one("SELECT * FROM articles")
    assert a["translated_by"] == "libretranslate" and a["title_orig"] == EN_FEED[0]["title"]
    assert db.query_one("SELECT title FROM topics")["title"].startswith("Volkswagen ruft")
    assert pipeline.translate_pending() == 0


def test_llm_translate(env):
    import json
    from app import config, translate
    env.post_routes["/v1/chat/completions"] = lambda body, h: (200, {"choices": [{"message": {
        "content": json.dumps({"uebersetzungen": ["VW-Absatz steigt"]})}}]})
    config.set("llm_provider", "openai")
    config.set("llm_api_key", "x")
    config.set("llm_base_url", env.base + "/v1")
    config.set("llm_model", "test-modell")
    config.set("llm_enabled", True)
    assert translate.provider() == "llm"
    assert translate.translate_texts(["VW sales rise"], "en") == (["VW-Absatz steigt"], "llm")
    assert translate.translate_items([], allow_llm=False) == 0


# ---------------------------------------------------------------- Abgleich nach Image-Update

def test_seed_sources_upgrade(env, monkeypatch):
    from app import config, db, sources
    base = [dict(name="Alt A", domain="a.example", feeds=["https://a.example/rss"]),
            dict(name="Alt B", domain="b.example", feeds=["https://b.example/rss"])]
    monkeypatch.setattr(sources, "DEFAULT_SOURCES", base)
    with db.tx() as conn:
        conn.execute("DELETE FROM sources")
    config.state_set("seeded_domains", [])
    config.state_set("source_defaults", None)
    first = sources.seed_sources()
    assert len(first["added"]) == 2
    # Admin ändert B und löscht nichts; neues Image: A mit neuem Feed, B geändert, C neu
    with db.tx() as conn:
        conn.execute("UPDATE sources SET feeds='https://b.example/eigener' WHERE domain='b.example'")
    monkeypatch.setattr(sources, "DEFAULT_SOURCES", [
        dict(name="Alt A", domain="a.example", feeds=["https://a.example/neu"]),
        dict(name="Alt B", domain="b.example", feeds=["https://b.example/neu"]),
        dict(name="Neu C", domain="c.example", feeds=[], language="en")])
    second = sources.seed_sources()
    assert len(second["added"]) == 1 and len(second["updated"]) == 1
    rows = {r["domain"]: r for r in db.query("SELECT * FROM sources")}
    assert rows["a.example"]["feeds"] == "https://a.example/neu"
    assert rows["b.example"]["feeds"] == "https://b.example/eigener"     # Admin-Änderung bleibt
    assert rows["c.example"]["language"] == "en"
    # gelöschte Standardquelle kommt nicht wieder, erneuter Start ändert nichts
    with db.tx() as conn:
        conn.execute("DELETE FROM sources WHERE domain='c.example'")
    assert sources.seed_sources() == {"added": [], "updated": []}


def test_upgrade_on_start_catches_up_new_sources(env):
    from app import config, db, pipeline
    from tests.conftest import setup_world
    setup_world(env)
    pipeline.run_scan("initial")
    # neue englische Quelle kommt mit dem Image
    env.routes["/feeds/en.rss"] = lambda p: (rss(EN_FEED), "application/rss+xml")
    with db.tx() as conn:
        new_id = conn.execute(
            "INSERT INTO sources(name, domain, feeds, use_search, language, enabled, created_at) VALUES(?,?,?,?,?,1,?)",
            ("Test EN", "neu.example", f"{env.base}/feeds/en.rss", 0, "en", db.utcnow())).lastrowid
    notes = pipeline.upgrade_on_start({"added": [new_id], "updated": []})
    assert any("Test EN" in n for n in notes)
    assert config.state_get("pending_catchup") == [new_id]
    assert db.query_one("SELECT 1 FROM runs WHERE kind='aktualisierung'")
    before = len(env.hits)
    pipeline.run_scan("nachholen", [new_id])
    assert all("/feeds/en.rss" in h for h in env.hits[before:])            # nur die neue Quelle
    assert config.state_get("pending_catchup") == []
    assert db.query_one("SELECT 1 FROM articles WHERE lang='en'")


def test_upgrade_on_start_reclassifies_after_rule_change(env):
    from app import config, pipeline
    from tests.conftest import setup_world
    setup_world(env)
    pipeline.upgrade_on_start({})          # Erststart: Regelstand merken, nichts zu tun
    pipeline.run_scan("initial")
    assert pipeline.upgrade_on_start({}) == [] and not config.state_get("pending_reclassify")
    config.state_set("rules_fingerprint", "alt")
    notes = pipeline.upgrade_on_start({})
    assert notes and config.state_get("pending_reclassify") is True


def test_ui_shows_language_and_original(env):
    import os
    from fastapi.testclient import TestClient
    from app import config, db, main, pipeline
    _english_world(env)
    _libre(env)
    config.set("libretranslate_url", env.base)
    pipeline.run_scan("initial")
    os.environ["ADMIN_PASSWORD"] = "geheim123"
    auth = ("admin", "geheim123")
    with TestClient(main.app) as c:
        tid = db.query_one("SELECT id FROM topics")["id"]
        page = c.get(f"/thema/{tid}").text
        assert "EN→DE" in page and "Original (Englisch)" in page and EN_FEED[0]["title"] in page
        assert "EN" in c.get("/").text
        api = c.get(f"/api/topics/{tid}").json()
        assert api["quellen"][0]["sprache"] == "en" and api["quellen"][0]["uebersetzt_von"] == "libretranslate"
        doc = c.get("/doku").text
        assert "Fremdsprachige Quellen und Übersetzung" in doc and "aktiv: LibreTranslate" in doc
        admin = c.get("/admin", auth=auth).text
        assert "Übersetzung testen" in admin and "Englisch: 1 Meldungen, davon 1 übersetzt" in admin
        r = c.post("/admin/translate-test", auth=auth, headers={"origin": "http://testserver"})
        assert r.status_code == 200 and "OK (LibreTranslate)" in r.text
        sid = db.query_one("SELECT id FROM sources WHERE domain='localhost'")["id"]
        c.post(f"/admin/sources/{sid}", auth=auth, headers={"origin": "http://testserver"},
               data={"name": "Test ZH", "domain": "localhost", "language": "zh", "enabled": "on"})
        assert db.query_one("SELECT language FROM sources WHERE id=?", (sid,))["language"] == "zh"
