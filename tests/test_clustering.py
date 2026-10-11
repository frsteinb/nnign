"""Qualität der Themenbildung und Neu-Bündeln."""
from collections import Counter

from tests.data.cluster_cases import CASES, HOLDOUT


def test_quality_reference_cases(env):
    from tests import cluster_eval
    r = cluster_eval.run(cases=CASES)
    assert r["precision"] >= 0.95 and r["recall"] >= 0.85, r


def test_quality_holdout_launch_week(env):
    """Viele Fahrzeugvorstellungen in derselben Woche dürfen nicht verschmelzen."""
    from tests import cluster_eval
    r = cluster_eval.run(cases=HOLDOUT)
    assert r["precision"] >= 0.95 and r["recall"] >= 0.85, r


def test_features_compounds_synonyms_models():
    from app import features
    v = features.vector("VW-Tarifabschluss: Betriebsrat verteidigt Stellenabbau")
    assert {"#einigung", "#stellen", "#abbau"} <= set(v)
    w = features.vector("Volkswagen streicht 35.000 Jobs")
    assert {"#stellen", "#abbau", "35000"} <= set(w)
    m = features.vector("VW stellt Elektro-Tiguan vor: ID.Tiguan soll 2026 kommen")
    assert features.models(m) == {"@id.tiguan"} and "#praesentation" in m
    assert "@911" in features.vector("711 PS: Porsche bringt den Elfer als Hybrid-Turbo")
    r = features.vector("Brandgefahr: VW ruft ID.3 und ID.4 wegen Batteriemodulen zurück")
    assert {"#rueckruf", "@id.3", "@id.4", "#batterie"} <= set(r)
    # Marke aus dem Titel bestimmt den Gegenstand, nicht Schwestermarken im Teaser
    c = features.vector("Cupra Raval vorgestellt", "Der Schwesterwagen des ID.Polo kommt auch von Skoda.")
    assert features.brands(c) == {"&seat"}


def test_model_and_brand_conflicts_lower_similarity():
    from app import features, topics

    class Flat:
        def weight(self, t):
            return 1.0
    a = features.vector("Rückruf: VW ruft Golf und Tiguan zurück")
    b = features.vector("Rückruf: VW ruft ID.3 und ID.4 zurück")
    assert topics.similarity(a, b, Flat()) < 0.6 * topics.cosine(a, b, Flat())
    p = features.vector("Porsche senkt die Prognose")
    v = features.vector("VW senkt die Prognose")
    assert topics.similarity(p, v, Flat()) < topics.cosine(p, v, Flat())


def _ingest(items):
    from app import pipeline
    ctx = pipeline.Ctx("t")
    pipeline.ingest(items, ctx, allow_llm=False)
    return ctx


def _items(rows):
    from datetime import datetime, timedelta, timezone
    from app.fetcher import Item
    base = datetime(2025, 9, 8, 8, tzinfo=timezone.utc)
    return [Item(url=f"https://m{h}.de/a{h}", title=t, summary=s, published=base + timedelta(hours=h),
                 source_name=f"M{h}", source_domain=f"m{h}.de", via="rss") for h, t, s in rows]


def test_bridge_merges_two_topics(env):
    from app import config, db
    config.set("collect_since", "2000-01-01")
    _ingest(_items([(0, "Warnstreik bei VW in Wolfsburg", "Die IG Metall ruft zum Warnstreik auf."),
                    (1, "Bänder stehen still: Tarifstreit bei Volkswagen eskaliert", "Zehntausende Beschäftigte.")]))
    n_before = db.query_one("SELECT COUNT(*) n FROM topics")["n"]
    _ingest(_items([(2, "Warnstreik bei Volkswagen: Bänder stehen still",
                     "Die IG Metall ruft zum Warnstreik auf, Zehntausende Beschäftigte legen die Arbeit nieder.")]))
    assert n_before == 2 and db.query_one("SELECT COUNT(*) n FROM topics")["n"] == 1


def test_regroup_rebuilds_and_keeps_manual(env):
    from app import config, db, pipeline
    config.set("collect_since", "2000-01-01")
    config.set("cluster_threshold", 0.99)                     # erst alles getrennt
    _ingest(_items([(0, "VW stellt ID.Tiguan vor", "Weltpremiere des Elektro-SUV ID.Tiguan auf der IAA."),
                    (1, "ID.Tiguan: Volkswagen zeigt den elektrischen Tiguan", "Das Elektro-SUV ID.Tiguan kommt 2026."),
                    (2, "Porsche zeigt neuen 911 Turbo S", "Weltpremiere des 911 Turbo S auf der IAA."),
                    (3, "Neuer Porsche 911 Turbo S mit Hybrid", "Der 911 Turbo S leistet 711 PS.")]))
    assert db.query_one("SELECT COUNT(*) n FROM topics")["n"] == 4
    manual = db.query_one("SELECT id FROM topics WHERE title LIKE 'Neuer Porsche%'")["id"]
    with db.tx() as conn:
        conn.execute("UPDATE topics SET sentiment_origin='manuell' WHERE id=?", (manual,))
    config.set("cluster_threshold", 0.26)
    r = pipeline.regroup()
    assert r["articles"] == 3
    rows = db.query("SELECT t.id, COUNT(a.id) n FROM topics t JOIN articles a ON a.topic_id=t.id GROUP BY t.id")
    sizes = Counter(r["n"] for r in rows)
    assert sizes == Counter({2: 2}), rows                       # ID.Tiguan und 911 je ein Thema
    assert db.query_one("SELECT 1 FROM topics WHERE id=?", (manual,))   # manuelles Thema blieb


def test_upgrade_triggers_regroup(env):
    from app import config, pipeline, topics
    from tests.conftest import setup_world
    setup_world(env)
    pipeline.upgrade_on_start({})
    pipeline.run_scan("initial")
    assert not config.state_get("pending_regroup")
    config.state_set("topics_algo", topics.ALGO_VERSION - 1)
    notes = pipeline.upgrade_on_start({})
    assert any("neu gebündelt" in n for n in notes) and config.state_get("pending_regroup") is True


def test_max_span_prevents_chaining(env):
    """Gleichlautende Folgemeldungen über Wochen wachsen nicht zu einem Riesen-Thema zusammen."""
    from app import config, db
    config.set("collect_since", "2000-01-01")
    rows = [(24 * 3 * k, "Warnstreik bei VW: Bänder stehen still", "Die IG Metall ruft zum Warnstreik bei Volkswagen auf.")
            for k in range(12)]                                   # alle 3 Tage, über 33 Tage
    for h, t, s in rows:
        _ingest(_items([(h, f"{t} ({h})", s)]))
    n = db.query_one("SELECT COUNT(*) n FROM topics")["n"]
    longest = db.query_one("SELECT MAX(julianday(last_published_at) - julianday(first_published_at)) d FROM topics")["d"]
    assert n >= 3 and longest <= 14
