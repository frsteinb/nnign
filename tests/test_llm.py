import json
from types import SimpleNamespace

from tests.conftest import setup_world


class FakeMessages:
    def __init__(self, decide):
        self.decide = decide
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        data = json.loads(kw["messages"][0]["content"].split("Daten:\n", 1)[1])
        out = self.decide(data)
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps(out))])


def test_llm_path(env, monkeypatch):
    from app import config, db, llm, pipeline
    setup_world(env)
    config.set("anthropic_api_key", "sk-test")

    def decide(data):
        title = data["meldung"]["titel"]
        cands = data["kandidaten_themen"]
        if "Audi" in title:
            return dict(relevant=False, match_topic_id=0, topic_title="", abstract="", sentiment="positiv",
                        confidence=0.9)
        if "35.000" in title and cands:
            return dict(relevant=True, match_topic_id=cands[0]["id"], topic_title="x", abstract="x",
                        sentiment="negativ", confidence=0.9)
        return dict(relevant=True, match_topic_id=0, topic_title="KI: " + title[:40],
                    abstract="Zusammenfassung der KI.", sentiment="negativ" if "35.000" in title else "positiv",
                    confidence=0.8)

    fake = FakeMessages(decide)
    client = SimpleNamespace(beta=SimpleNamespace(messages=fake), messages=fake)
    monkeypatch.setattr(llm, "_get_client", lambda: client)

    pipeline.run_scan("initial")
    assert fake.calls, "KI wurde nicht aufgerufen"
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"

    # Audi-Meldung als nicht relevant markiert, kein Thema
    a = db.query_one("SELECT * FROM articles WHERE title LIKE 'Audi%'")
    assert a["relevant"] == 0 and a["topic_id"] is None
    # Stellenabbau: ein Thema (KI-Titel), zwei Meldungen
    t = db.query_one("SELECT * FROM topics WHERE title LIKE 'KI: %35.000%'")
    assert t is not None and t["sentiment_origin"] == "llm" and t["article_count"] == 2
    assert t["abstract"] == "Zusammenfassung der KI."
    # Tageszähler
    assert llm.calls_today() == len(fake.calls)


def test_llm_budget_and_refusal(env, monkeypatch):
    from app import config, llm
    config.set("anthropic_api_key", "sk-test")
    config.set("llm_max_calls_per_day", 0)
    assert llm.analyze({"title": "x"}, []) is None   # Budget erschöpft
    config.set("llm_max_calls_per_day", 10)
    refusal = SimpleNamespace(create=lambda **kw: SimpleNamespace(stop_reason="refusal", content=[]))
    monkeypatch.setattr(llm, "_get_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=refusal)))
    assert llm.analyze({"title": "x"}, []) is None
