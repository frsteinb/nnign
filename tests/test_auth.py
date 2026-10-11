"""Admin-Anmeldung per Cookie: 6 Monate gültig, höchstens 5 Geräte."""
import os
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

HTML = {"accept": "text/html"}
ORIGIN = {"origin": "http://testserver"}


def _client(ua="Test"):
    from app import main
    os.environ["ADMIN_PASSWORD"] = "geheim123"
    c = TestClient(main.app)
    c.headers.update({"user-agent": ua})
    return c


def _login(c, pw="geheim123", nxt="/admin"):
    return c.post("/login", data={"password": pw, "next": nxt}, headers=ORIGIN, follow_redirects=False)


def test_login_flow_cookie_and_redirect(env):
    with _client() as c:
        r = c.get("/admin", headers=HTML, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Fadmin")
        assert c.get("/admin").status_code == 401                         # Skripte: 401 statt Umleitung
        bad = _login(c, "falsch")
        assert "Passwort+falsch" in bad.headers["location"]
        ok = _login(c, nxt="/?auswahl=1")
        assert ok.status_code == 303 and ok.headers["location"] == "/?auswahl=1"
        cookie = ok.headers["set-cookie"]
        assert "nnign_session=" in cookie and "HttpOnly" in cookie and "Max-Age=15552000" in cookie
        assert "samesite=lax" in cookie.lower()
        page = c.get("/admin", headers=HTML)
        assert page.status_code == 200 and "Angemeldete Geräte" in page.text and "dieses Gerät" in page.text
        assert 'id="mergeform"' in c.get("/?auswahl=1").text              # Auswahlmodus mit Cookie
        assert "Abmelden" in c.get("/").text
        c.post("/logout", headers=ORIGIN)
        assert c.get("/admin").status_code == 401
    # offene Weiterleitung verhindert
    with _client() as c:
        assert _login(c, nxt="//evil.example").headers["location"] == "/admin"


def test_max_five_devices_and_expiry(env):
    from app import auth, db
    clients = []
    for i in range(6):
        c = _client(f"Device {i}")
        c.__enter__()
        _login(c)
        clients.append(c)
    try:
        assert len(auth.sessions()) == 5
        assert clients[0].get("/admin").status_code == 401                # ältestes Gerät abgemeldet
        assert clients[5].get("/admin").status_code == 200
        # nach 6 Monaten ohne Nutzung abgelaufen
        old = (datetime.now(timezone.utc) - timedelta(days=181)).isoformat()
        with db.tx() as conn:
            conn.execute("UPDATE admin_sessions SET last_seen=?", (old,))
        assert clients[5].get("/admin").status_code == 401
        assert clients[4].get("/admin").status_code == 401
        assert len(auth.sessions()) == 3
    finally:
        for c in clients:
            c.__exit__(None, None, None)


def test_password_change_logs_out_other_devices(env):
    a, b = _client("A"), _client("B")
    with a, b:
        _login(a)
        _login(b)
        r = a.post("/admin/password", data={"new": "neuesPasswort9"}, headers=ORIGIN)
        assert "1 andere Geräte abgemeldet" in r.text
        assert a.get("/admin").status_code == 200 and b.get("/admin").status_code == 401
        assert a.post("/admin/scan", headers={"origin": "https://evil.example"}).status_code == 403   # CSRF


def test_brute_force_lock(env):
    from app import auth
    auth._failed.clear()
    with _client() as c:
        for _ in range(auth.LOCK_AFTER):
            _login(c, "x")
        r = _login(c)                                                      # auch richtiges Passwort gesperrt
        assert "Fehlversuche" in r.headers["location"]
    auth._failed.clear()
