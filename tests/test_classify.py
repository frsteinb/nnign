"""Entscheidungsmodelle: lernendes Modell, Zero-Shot-Klassifikation, Auswahl des Verfahrens."""
import json
import os
import time

from fastapi.testclient import TestClient

ORIGIN = {"origin": "http://testserver"}

NEG = ["ruft {m} wegen Airbag-Problemen zurück", "Streik legt Werk {m} lahm", "{m}: Gewinn bricht ein",
       "Ermittlungen gegen Manager von {m}", "{m} verschiebt Marktstart", "Kritik an Software im {m}"]
POS = ["{m} gewinnt Vergleichstest", "Rekordabsatz für {m}", "{m} erhält fünf Sterne", "Großauftrag für {m}",
       "{m} feiert Weltpremiere", "Kunden stürmen Händler wegen {m}"]
MODELS = ["Golf", "Passat", "Tiguan", "Polo", "ID.3", "ID.4", "Taycan", "Q4"]


def _topic(conn, title, sentiment, origin, day=1):
    from app import db
    now = db.utcnow()
    pub = f"2026-09-{day:02d}T10:00:00+00:00"
    tid = conn.execute("INSERT INTO topics(title, abstract, sentiment, sentiment_origin, sentiment_detail, "
                       "first_published_at, last_published_at, signature, created_at, updated_at, article_count) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                       (title, "", sentiment, origin, json.dumps({"confidence": 0.9}), pub, pub, "{}", now, now)).lastrowid
    conn.execute("INSERT INTO articles(url, title, title_key, summary, source_name, source_domain, published_at, "
                 "fetched_at, via, relevant, topic_id) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                 (f"https://x.de/{tid}", title, f"k{tid}", "", "X", "x.de", pub, now, "rss", tid))
    return tid


def _train_world():
    from app import db
    with db.tx() as conn:
        for i, m in enumerate(MODELS):
            for j, t in enumerate(NEG):
                _topic(conn, "VW " + t.format(m=m), "negativ", "manuell" if (i + j) % 2 else "llm", 1 + j)
            for j, t in enumerate(POS):
                _topic(conn, "VW " + t.format(m=m), "positiv", "manuell" if (i + j) % 2 else "llm", 1 + j)


def test_learned_model_trains_and_applies(env):
    from app import classify, config, db, topics
    _train_world()
    info = classify.train()
    assert info["n"] == len(MODELS) * 12 and info["accuracy"] >= 0.8
    assert classify.learned_ready() and "aktiv" in classify.learned_status()
    label, conf = classify.predict_learned("VW ruft Arteon wegen Airbag-Problemen zurück", "")
    assert label == "negativ" and conf > 0.7
    # ein regelbasiertes Thema ohne Signale wird vom Modell eingeordnet
    with db.tx() as conn:
        tid = _topic(conn, "Großauftrag für VW Amarok", "neutral", "lexikon")
        conn.execute("UPDATE articles SET sentiment_detail='{}' WHERE topic_id=?", (tid,))
        topics.refresh(conn, tid)
    t = db.query_one("SELECT * FROM topics WHERE id=?", (tid,))
    assert t["sentiment_origin"] == "lernend" and t["sentiment"] == "positiv"
    assert json.loads(t["sentiment_detail"])["lernend"]["regeln"]
    # Verfahren „regeln“: lernendes Modell bleibt außen vor
    config.set("sentiment_engine", "regeln")
    with db.tx() as conn:
        topics.refresh(conn, tid)
    assert db.query_one("SELECT sentiment_origin FROM topics WHERE id=?", (tid,))["sentiment_origin"] == "lexikon"


def test_learned_not_used_with_few_examples(env):
    from app import classify, db
    with db.tx() as conn:
        _topic(conn, "VW ruft Golf zurück", "negativ", "manuell")
    classify.train()
    assert not classify.learned_ready() and "wartet" in classify.learned_status()


def _zs_server(env, reply):
    from app import config
    calls = []

    def handler(body, headers):
        calls.append((body, dict(headers)))
        return reply(body)
    env.post_routes["/hf/models/test-nli"] = handler
    config.set("zeroshot_url", env.base + "/hf/models/{model}")
    config.set("zeroshot_model", "test-nli")
    return calls


def _scores(body, best="schlecht", p=0.8):
    labels = body["parameters"]["candidate_labels"]
    rest = (1 - p) / (len(labels) - 1)
    return 200, {"labels": labels, "scores": [p if lbl == best else rest for lbl in labels]}


def test_zeroshot_rates_topics_in_background(env):
    from app import classify, config, db, maintenance
    calls = _zs_server(env, lambda b: _scores(b, "schlecht" if "zurück" in b["inputs"] else "gut"))
    os.environ["HF_TOKEN"] = "hf_test"
    config.set("sentiment_engine", "zeroshot")
    with db.tx() as conn:
        neg = _topic(conn, "VW ruft Golf zurück", "neutral", "lexikon")
        pos = _topic(conn, "VW stellt neuen Polo vor", "neutral", "lexikon")
    assert maintenance.zeroshot_open() == 2
    assert maintenance.zeroshot_batch() == 2
    body, headers = calls[0]
    assert body["parameters"]["hypothesis_template"] == "Diese Nachricht ist {} für den Volkswagen-Konzern."
    assert headers.get("Authorization") == "Bearer hf_test"
    t1 = db.query_one("SELECT * FROM topics WHERE id=?", (neg,))
    t2 = db.query_one("SELECT * FROM topics WHERE id=?", (pos,))
    assert (t1["sentiment"], t1["sentiment_origin"]) == ("negativ", "zeroshot")
    assert (t2["sentiment"], t2["sentiment_origin"]) == ("positiv", "zeroshot")
    assert maintenance.zeroshot_open() == 0                  # erledigt, kein erneuter Aufruf
    # geänderte Hypothese -> neu bewerten
    config.set("zeroshot_hypothesis", "Für VW ist diese Meldung {}.")
    assert maintenance.zeroshot_open() == 2
    assert "OK" in classify.test_zeroshot()


def test_zeroshot_low_confidence_and_pause(env):
    from app import classify, config, db, maintenance
    state = {"mode": "unsicher"}

    def reply(body):
        if state["mode"] == "laden":
            return 503, {"error": "Model is loading", "estimated_time": 42}
        if state["mode"] == "liste":
            return 200, [{"label": "gut", "score": 0.9}, {"label": "schlecht", "score": 0.05},
                         {"label": "weder gut noch schlecht", "score": 0.05}]
        return _scores(body, "schlecht", 0.4)
    _zs_server(env, reply)
    config.set("sentiment_engine", "zeroshot")
    with db.tx() as conn:
        tid = _topic(conn, "VW ruft Golf zurück", "neutral", "lexikon")
    maintenance.zeroshot_batch()
    t = db.query_one("SELECT * FROM topics WHERE id=?", (tid,))
    assert t["sentiment_origin"] == "lexikon" and "zeroshot" in t["sentiment_detail"]   # zu unsicher
    state["mode"] = "laden"
    assert classify.predict_zeroshot("VW", "") is None
    assert "Modell lädt" in classify.zs_status() and not classify.zs_ready()
    classify._zs_pause.update(until=0.0)
    state["mode"] = "liste"                                  # neueres Antwortformat
    assert classify.predict_zeroshot("VW", "")[0] == "positiv"


def test_engine_choice_overrides_llm_sentiment(env):
    from app import config, db, pipeline
    from tests.conftest import setup_world
    from tests.test_maintenance import GOOD, _answer, _openai_mock
    setup_world(env)
    _openai_mock(env, lambda b: _answer({**GOOD, "sentiment": "positiv"}))
    config.set("sentiment_engine", "regeln")
    pipeline.run_scan("initial")
    t = db.query_one("SELECT * FROM topics WHERE title='KI-Titel'")
    assert t is not None and t["sentiment_origin"] == "lexikon"        # KI nur für Titel/Zuordnung


def test_engine_change_triggers_reclassify(env):
    from app import config, maintenance
    from tests.test_maintenance import _setup
    _setup(env)
    config.set("sentiment_engine", "lernend")
    assert any("Bewertungsverfahren" in n for n in maintenance.check())
    assert config.state_get("pending_reclassify")


def test_admin_engines_card_and_compare(env):
    from app import main
    _train_world()
    os.environ["ADMIN_PASSWORD"] = "geheim123"
    with TestClient(main.app) as c:
        c.post("/login", data={"password": "geheim123"}, headers=ORIGIN)
        page = c.get("/admin").text
        assert "Bewertungsverfahren" in page and "Zero-Shot-Klassifikation" in page and "HF_TOKEN" in page
        r = c.post("/admin/learned-train", headers=ORIGIN)
        assert "Trainiert:" in r.text
        r = c.post("/admin/classify-test", data={"title": "VW ruft Arteon wegen Airbag-Problemen zurück"},
                   headers=ORIGIN)
        assert r.status_code == 200 and "Lernendes Modell" in r.text and "negativ" in r.text
    assert time.time()
