import os
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
    from app import db, llm, pipeline
    setup_world(env)
    os.environ["ANTHROPIC_API_KEY"] = "sk-test"

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
    os.environ["ANTHROPIC_API_KEY"] = "sk-test"
    config.set("llm_max_calls_per_day", 0)
    assert llm.analyze({"title": "x"}, []) is None   # Budget erschöpft
    config.set("llm_max_calls_per_day", 10)
    refusal = SimpleNamespace(create=lambda **kw: SimpleNamespace(stop_reason="refusal", content=[]))
    monkeypatch.setattr(llm, "_get_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=refusal)))
    assert llm.analyze({"title": "x"}, []) is None


def _answer(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


GOOD = {"relevant": True, "match_topic_id": 0, "topic_title": "VW mit Rekordabsatz", "abstract": "Kurz.",
        "sentiment": "positiv", "confidence": 0.9}


def test_openai_compatible_with_format_fallback(env, monkeypatch):
    """Server ohne json_schema-Unterstützung: Rückfall auf json_object, Antwort in Codeblock."""
    from app import config, llm
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_MODEL", "lokales-modell")
    monkeypatch.setenv("LLM_BASE_URL", env.base + "/v1")
    monkeypatch.setenv("LLM_API_KEY", "geheim")

    def chat(body, headers):
        if body.get("response_format", {}).get("type") == "json_schema":
            return 400, {"error": "response_format json_schema not supported"}
        return 200, _answer("```json\n" + json.dumps(GOOD) + "\n```")

    env.post_routes["/v1/chat/completions"] = chat
    assert llm.available(), llm.missing()
    assert config.source_of("llm_provider") == "env"
    res = llm.analyze({"title": "VW mit Rekordabsatz"}, [])
    assert res["sentiment"] == "positiv" and res["topic_title"] == "VW mit Rekordabsatz"
    (_, h1, b1), (_, h2, b2) = env.posts
    assert b1["response_format"]["type"] == "json_schema" and b2["response_format"]["type"] == "json_object"
    assert b2["model"] == "lokales-modell" and h2["Authorization"] == "Bearer geheim"
    # Das funktionierende Format wird gemerkt: nächster Aufruf direkt mit json_object
    llm.analyze({"title": "VW mit Rekordabsatz"}, [])
    assert env.posts[-1][2]["response_format"]["type"] == "json_object" and len(env.posts) == 3


def test_azure_request_shape(env, monkeypatch):
    from app import llm
    monkeypatch.setenv("LLM_PROVIDER", "azure")
    monkeypatch.setenv("LLM_MODEL", "mein-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", env.base)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azkey")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    env.post_routes["/openai/deployments/mein-deployment/chat/completions"] = \
        lambda body, headers: (200, _answer(json.dumps(GOOD)))
    assert llm.analyze({"title": "VW"}, [])["sentiment"] == "positiv"
    path, headers, body = env.posts[0]
    assert "api-version=2024-10-21" in path and headers["api-key"] == "azkey" and "model" not in body
    assert body["response_format"]["json_schema"]["strict"] is True


def test_provider_requirements(env, monkeypatch):
    from app import config, llm
    assert llm.missing().startswith("kein API-Key")          # Standard anthropic ohne Key
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert llm.available() and llm.model() == "claude-opus-5-5"
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    assert "kein Modell" in llm.missing()                    # andere Anbieter brauchen ein Modell
    monkeypatch.setenv("LLM_MODEL", "irgendein-modell")
    assert llm.available() and llm.base_url() == "https://api.openai.com/v1"
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("OPENAI_API_KEY")
    assert llm.available()                                   # Ollama ohne Key
    monkeypatch.setenv("OLLAMA_HOST", "gpu-server:11434")
    assert llm.base_url() == "http://gpu-server:11434/v1"
    monkeypatch.setenv("LLM_PROVIDER", "")                   # leer = Standard
    assert llm.provider_name() == "anthropic"
    config.set("llm_provider", "mistral")                    # Admin-Wert hat Vorrang vor ENV
    assert llm.provider_name() == "mistral"


def test_openai_compatible_errors_fall_back(env, monkeypatch):
    from app import llm
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_BASE_URL", env.base + "/v1")
    env.post_routes["/v1/chat/completions"] = lambda body, headers: (401, {"error": "bad key"})
    assert llm.analyze({"title": "VW"}, []) is None
    env.post_routes["/v1/chat/completions"] = lambda body, headers: (200, _answer("kein json"))
    assert llm.analyze({"title": "VW"}, []) is None
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")       # nicht erreichbar
    assert llm.analyze({"title": "VW"}, []) is None
