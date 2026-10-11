"""Selbstpflege: Änderungen erkennen, automatisch nachziehen oder vorschlagen; KI-Pausen und Nachbewertung."""
import json
import os
import time

from fastapi.testclient import TestClient

from tests.conftest import setup_world

ORIGIN = {"origin": "http://testserver"}


def _setup(env):
    from app import maintenance, pipeline
    setup_world(env)
    pipeline.upgrade_on_start({})
    pipeline.run_scan("initial")
    maintenance.check(startup=True)          # Ausgangsstand merken


def _openai_mock(env, behaviour):
    """OpenAI-kompatibler KI-Server; behaviour(body) -> (status, payload, headers)."""
    from app import config
    calls = []

    def handler(body, headers):
        calls.append(body)
        return behaviour(body)
    env.post_routes["/v1/chat/completions"] = handler
    config.set("llm_provider", "openai")
    os.environ["LLM_API_KEY"] = "x"
    config.set("llm_base_url", env.base + "/v1")
    config.set("llm_model", "test-modell")
    config.set("llm_enabled", True)
    return calls


def _answer(content: dict):
    return 200, {"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}]}


GOOD = {"relevant": True, "match_topic_id": 0, "topic_title": "KI-Titel", "abstract": "KI-Abstract.",
        "sentiment": "negativ", "begruendung": "Stellenabbau.", "confidence": 0.9}


def test_rule_config_change_reclassifies_automatically(env):
    from app import config, maintenance
    _setup(env)
    config.set("lexicon_extra", ["Stellen = 4"])
    notes = maintenance.check()
    assert any("neu berechnet" in n for n in notes) and config.state_get("pending_reclassify")
    maintenance.work()
    assert not config.state_get("pending_reclassify")
    assert maintenance.check() == []                     # nichts mehr zu tun


def test_threshold_change_auto_or_suggestion(env):
    from app import config, maintenance
    _setup(env)
    config.set("cluster_threshold", 0.3)
    assert any("neu gebündelt" in n for n in maintenance.check())    # wenige Meldungen -> automatisch
    assert config.state_get("pending_regroup")
    maintenance.work()
    assert not config.state_get("pending_regroup")
    # sehr viele Meldungen (simuliert) -> Vorschlag mit Aufwand
    config.state_set("regroup_rate", 100.0)
    config.set("cluster_threshold", 0.31)
    maintenance.check()
    sug = {s["id"]: s for s in maintenance.suggestions()}
    assert "regroup" in sug and "Rechenzeit" in sug["regroup"]["estimate"]
    assert not config.state_get("pending_regroup")
    maintenance.check()                                    # kein Doppel-Vorschlag
    assert len(maintenance.suggestions()) == 1
    maintenance.run_suggestion("regroup")
    assert config.state_get("pending_regroup") and not maintenance.suggestions()


def test_relevance_change_offers_cleanup(env):
    from app import config, db, maintenance
    _setup(env)
    n_before = db.query_one("SELECT COUNT(*) n FROM articles WHERE relevant=1")["n"]
    config.set("include_brands", False)                     # Audi/Porsche zählen nicht mehr
    maintenance.check()
    sug = {s["id"]: s for s in maintenance.suggestions()}
    assert "relevance_cleanup" in sug
    msg = maintenance.run_suggestion("relevance_cleanup")
    assert "ausgeblendet" in msg
    assert db.query_one("SELECT COUNT(*) n FROM articles WHERE relevant=1")["n"] < n_before
    assert not db.query_one("SELECT 1 FROM articles WHERE relevant=1 AND title LIKE 'Porsche%'")


def test_search_change_suggests_replan(env):
    from datetime import date, timedelta
    from app import config, db, maintenance, pipeline
    _setup(env)
    pipeline.apply_period(date.today() - timedelta(days=400), None)
    maintenance.check()
    assert db.query_one("SELECT COUNT(*) n FROM backfill_tasks WHERE method='search'")["n"]
    config.set("search_query", "Volkswagen OR Audi OR Porsche OR Cupra")
    maintenance.check()
    assert "search_replan" in {s["id"] for s in maintenance.suggestions()}
    maintenance.run_suggestion("search_replan")
    assert db.query_one("SELECT 1 FROM backfill_tasks WHERE method='search' AND status='übersprungen'")
    assert db.query_one("SELECT 1 FROM backfill_tasks WHERE method='search' AND status='offen' AND target LIKE '%Cupra%'")


def test_edited_source_is_caught_up(env):
    from app import config, db, maintenance
    _setup(env)
    sid = db.query_one("SELECT id FROM sources WHERE name='Testmedium B'")["id"]
    with db.tx() as conn:
        conn.execute("UPDATE sources SET feeds=feeds || '\n' || ? WHERE id=?", (env.base + "/feeds/a.rss", sid))
    notes = maintenance.check()
    assert any("Quelle" in n for n in notes) and sid in config.state_get("pending_catchup")


def test_rate_limit_pauses_and_rerates_later(env):
    from app import config, db, llm, maintenance, pipeline
    setup_world(env)
    pipeline.upgrade_on_start({})
    state = {"limit": True}

    def behaviour(body):
        if state["limit"]:
            return 429, {"error": "rate limit"}
        return _answer(GOOD)
    env_calls = _openai_mock(env, behaviour)
    # 429 liefert hier keinen Retry-After -> 60 s Pause
    pipeline.run_scan("initial")
    assert llm.paused_until() > time.time() and "Rate-Limit" in llm.blocked_reason()
    assert db.query_one("SELECT COUNT(*) n FROM articles WHERE llm_state='pending'")["n"] >= 1
    n_calls = len(env_calls)
    assert n_calls == 1                                    # nach dem 429 keine weiteren Anfragen
    assert maintenance.rerate_open() >= 1
    with TestClient(__import__("app.main", fromlist=["x"]).app) as c:
        assert "KI pausiert" in c.get("/fragment/activity").text
    # Pause vorbei, Limit aufgehoben -> Nachbewertung holt alles nach
    state["limit"] = False
    llm._pause.update(until=0.0, fails=0)
    config.state_set("llm_pause", None)
    done = maintenance.rerate_batch()
    assert done >= 1
    assert not db.query_one("SELECT 1 FROM articles WHERE llm_state='pending'")
    t = db.query_one("SELECT * FROM topics WHERE title='KI-Titel'")
    assert t and t["sentiment_origin"] == "llm" and t["llm_fp"] == llm.fingerprint()
    assert "nachbewertet" in t["sentiment_detail"]


def test_retry_after_header_is_respected(env):
    from app import llm
    env.post_routes["/v1/chat/completions"] = lambda b, h: (429, {"error": "x"})
    _openai_mock(env, lambda b: (429, {"error": "x"}))
    llm._limit_hit("900")
    assert 880 < llm.paused_until() - time.time() <= 900


def test_rerate_respects_budget_share(env):
    from app import config, llm, maintenance
    _setup(env)
    calls = _openai_mock(env, lambda b: _answer(GOOD))
    config.set("llm_max_calls_per_day", 4)
    config.set("llm_rerate_share", 0.5)                    # höchstens 2 Aufrufe für Nachbewertungen
    maintenance.check()
    assert maintenance.rerate_open() >= 3                  # aktuelle, nur regelbasierte Themen
    maintenance.rerate_batch()
    assert len(calls) == 2 and llm.calls_today() == 2


def test_new_llm_offers_rerating_old_topics(env):
    from app import db, maintenance
    _setup(env)
    with db.tx() as conn:                                  # Themen künstlich altern lassen
        conn.execute("UPDATE topics SET last_published_at='2015-01-01T00:00:00+00:00'")
    _openai_mock(env, lambda b: _answer(GOOD))
    maintenance.check()
    sug = {s["id"]: s for s in maintenance.suggestions()}
    assert "llm_rerate_old" in sug and "KI-Aufrufe" in sug["llm_rerate_old"]["estimate"]
    assert maintenance._rerate_count("recent") == 0
    maintenance.run_suggestion("llm_rerate_old")
    assert maintenance.rerate_open() >= 3 and maintenance.rerate_batch() >= 3


def test_suggestions_ui_for_admin_only(env):
    from app import config, main, maintenance
    _setup(env)
    config.state_set("regroup_rate", 100.0)
    config.set("cluster_threshold", 0.33)
    maintenance.check()
    os.environ["ADMIN_PASSWORD"] = "geheim123"
    with TestClient(main.app) as c:
        assert "Vorschläge zur Aktualisierung" not in c.get("/").text
        c.post("/login", data={"password": "geheim123"}, headers=ORIGIN)
        home = c.get("/").text
        assert "Vorschläge zur Aktualisierung" in home and "Rechenzeit" in home
        assert "Vorschläge zur Aktualisierung" in c.get("/admin").text
        r = c.post("/admin/suggestions/regroup/dismiss", headers={**ORIGIN, "referer": "http://testserver/"})
        assert r.status_code == 200 and "Vorschlag ausgeblendet" in r.text
    assert not maintenance.suggestions()
    assert maintenance.check() == []                       # ausgeblendet = akzeptiert, kommt nicht wieder


def test_deepl_quota_pauses_translation(env):
    from app import config, translate
    env.post_routes["/v2/translate"] = lambda b, h: (456, {"message": "Quota exceeded"})
    os.environ["DEEPL_API_KEY"] = "abc:fx"
    config.set("deepl_url", env.base + "/v2/translate")
    assert translate.translate_texts(["VW sales rise"], "en") == (None, "")
    assert "Kontingent" in translate.paused_reason() and "pausiert" in translate.describe()
    n = len(env.posts)
    translate.translate_texts(["VW sales rise"], "en")
    assert len(env.posts) == n                     # während der Pause keine Anfragen


def test_list_models(env):
    from app import config, llm
    env.routes["/v1/models"] = lambda p: (json.dumps({"data": [{"id": "models/gemini-x-flash"}, {"id": "embedding-1"}]}).encode(),
                                          "application/json")
    _openai_mock(env, lambda b: _answer(GOOD))
    config.set("llm_provider", "gemini")
    assert llm.list_models() == (["embedding-1", "gemini-x-flash"], "")


def test_fallback_provider_takes_over_on_limit(env):
    from app import config, llm, maintenance
    _setup(env)
    primary = _openai_mock(env, lambda b: (429, {"error": "limit"}))
    fb_calls = []

    def fb(body, headers):
        fb_calls.append(body)
        return _answer(GOOD)
    env.post_routes["/fb/v1/chat/completions"] = fb
    config.set("llm_fallback_provider", "groq")
    config.set("llm_fallback_model", "gpt-oss-test")
    config.set("llm_fallback_base_url", env.base + "/fb/v1")
    os.environ["GROQ_API_KEY"] = "gsk_test"
    assert llm.fallback_configured() and "Ausweich: groq" in llm.describe()
    r1 = llm.analyze({"title": "VW", "summary": "", "source": "x", "published": ""}, [])
    assert r1 is None and len(primary) == 1 and llm.pause_info("primary")[0]   # Haupt-Anbieter pausiert
    r2 = llm.analyze({"title": "VW", "summary": "", "source": "x", "published": ""}, [])
    assert r2 and r2["sentiment"] == "negativ" and len(fb_calls) == 1 and len(primary) == 1
    assert fb_calls[0]["model"] == "gpt-oss-test"
    assert llm.ready() and llm.blocked_reason() == ""
    t = llm.test_connection()
    assert "Haupt-Anbieter: pausiert" in t and "Ausweich-Anbieter: OK" in t
    assert maintenance.rerate_batch() >= 1 and len(primary) == 1           # Nachbewertung über Ausweich


def test_zeroshot_covers_while_llm_paused(env):
    from app import config, llm, maintenance
    from tests.test_classify import _zs_server, _scores
    _setup(env)
    _openai_mock(env, lambda b: _answer(GOOD))
    _zs_server(env, lambda b: _scores(b, "gut", 0.9))
    os.environ["HF_TOKEN"] = "hf"
    assert not maintenance.zeroshot_active()                 # KI arbeitet -> kein Zero-Shot
    llm.pause(600, "Rate-Limit des Anbieters")
    assert maintenance.zeroshot_active() and maintenance.zeroshot_batch() >= 1
    config.set("sentiment_engine", "auto")


def test_gemini_daily_quota_is_remembered_and_shown(env):
    from app import llm, maintenance
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                      "message": "Quota exceeded for metric: generate_content_free_tier_requests, limit: 250, "
                                 "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                      "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]}}
    _openai_mock(env, lambda b: (429, body))
    from app import config
    config.set("llm_provider", "gemini")
    t = llm.test_connection()
    assert "Fehlgeschlagen" in t or "pausiert" in t
    r = llm.last_result()
    assert r["kind"] == "limit" and r["http"] == 429 and "RESOURCE_EXHAUSTED" in r["detail"]
    until, reason = llm.pause_info("primary")
    assert reason == "Tageskontingent des Anbieters erschöpft" and until - time.time() > 3600   # bis zum Tageswechsel
    t2 = llm.test_connection()
    assert "pausiert bis" in t2 and "Tageskontingent" in t2 and "Quota exceeded" in t2
    row = next(j for j in maintenance.jobs() if j["key"] == "llm")
    assert row["state"] == "limit" and "letzte Antwort: Limit erreicht (HTTP 429)" in row["details"]


def test_minute_limit_uses_retry_delay_and_empty_answer_is_explained(env):
    from app import llm
    _openai_mock(env, lambda b: (429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "per minute",
                                                 "details": [{"retryDelay": "34s"}]}}))
    llm.test_connection()
    until, _ = llm.pause_info("primary")
    assert 25 < until - time.time() <= 34
    llm._pause.update(until=0.0, fails=0)
    from app import config
    config.state_set("llm_pause", None)
    _openai_mock(env, lambda b: (200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}))
    t = llm.test_connection()
    assert "leere Antwort" in t and "Antwortlimit" in t
