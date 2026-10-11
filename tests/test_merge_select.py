"""Admin führt ausgewählte Themen zusammen."""
import os

from fastapi.testclient import TestClient

from tests.conftest import setup_world

AUTH = ("admin", "geheim123")
ORIGIN = {"origin": "http://testserver"}


def _client():
    from app import main
    os.environ["ADMIN_PASSWORD"] = "geheim123"
    return TestClient(main.app)


def test_select_mode_requires_admin_and_merges(env):
    from app import db, pipeline
    setup_world(env)
    pipeline.run_scan("initial")
    ids = [r["id"] for r in db.query("SELECT id FROM topics ORDER BY first_published_at")]
    assert len(ids) >= 3
    n_articles = db.query_one("SELECT COUNT(*) n FROM articles WHERE topic_id IN (?, ?, ?)", ids[:3])["n"]
    with _client() as c:
        assert c.get("/?auswahl=1").status_code == 401                     # öffentlich: Anmeldung nötig
        assert 'name="ids"' not in c.get("/").text
        page = c.get("/?auswahl=1", auth=AUTH).text
        assert 'name="ids"' in page and 'id="mergeform"' in page
        frag = c.get("/fragment/topics?auswahl=1&page=1", auth=AUTH).text
        assert 'name="ids"' in frag
        assert c.post("/admin/topics/merge", data={"ids": [ids[0]]}, auth=AUTH, headers=ORIGIN).status_code == 400
        r = c.post("/admin/topics/merge", auth=AUTH, headers=ORIGIN, follow_redirects=False,
                   data={"ids": ids[:3], "title": "Gemeinsames Thema", "back": "?auswahl=1&order=new"})
        assert r.status_code == 303 and r.headers["location"].startswith("/?auswahl=1")
        assert "Themen+zusammengef%C3%BChrt" in r.headers["location"] or "zusammengef" in r.headers["location"]
    t = db.query_one("SELECT * FROM topics WHERE id=?", (ids[0],))
    assert t["manual_group"] == 1 and t["title"] == "Gemeinsames Thema" and t["title_fixed"] == 1
    assert t["article_count"] == n_articles
    assert not db.query_one("SELECT 1 FROM topics WHERE id IN (?, ?)", ids[1:3])
    # automatisches Neu-Bündeln lässt die Bündelung und den Titel stehen
    pipeline.regroup()
    t2 = db.query_one("SELECT * FROM topics WHERE id=?", (ids[0],))
    assert t2["article_count"] == n_articles and t2["title"] == "Gemeinsames Thema"
