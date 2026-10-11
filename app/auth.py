"""Admin-Anmeldung.

Zwei Wege: Anmeldeformular mit Sitzungs-Cookie (für Browser; gilt 6 Monate ab der letzten Nutzung,
höchstens 5 Geräte gleichzeitig – beim sechsten wird die am längsten unbenutzte Sitzung beendet) und
HTTP-Basic-Auth (für Skripte und die API). Im Cookie steht nur ein Zufallswert; gespeichert wird dessen Hash.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import db

COOKIE = "nnign_session"
LIFETIME_DAYS = 180
MAX_SESSIONS = 5
_TOUCH_SECONDS = 3600          # last_seen höchstens stündlich aktualisieren
_failed: dict[str, list[float]] = {}
LOCK_AFTER, LOCK_SECONDS = 10, 300


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else ""))[:60]


def create_session(request) -> str:
    token = secrets.token_urlsafe(32)
    now = _now().isoformat()
    with db.tx() as conn:
        conn.execute("INSERT INTO admin_sessions(token_hash, created_at, last_seen, user_agent, ip) VALUES(?,?,?,?,?)",
                     (_hash(token), now, now, (request.headers.get("user-agent") or "")[:200], client_ip(request)))
        ids = [r["id"] for r in conn.execute("SELECT id FROM admin_sessions ORDER BY last_seen DESC, id DESC")]
        for old in ids[MAX_SESSIONS:]:
            conn.execute("DELETE FROM admin_sessions WHERE id=?", (old,))
    return token


def current_session(request):
    """Gültige Sitzung zum Cookie der Anfrage oder None (abgelaufene werden gelöscht)."""
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = db.query_one("SELECT * FROM admin_sessions WHERE token_hash=?", (_hash(token),))
    if row is None:
        return None
    last = datetime.fromisoformat(row["last_seen"])
    if _now() - last > timedelta(days=LIFETIME_DAYS):
        with db.tx() as conn:
            conn.execute("DELETE FROM admin_sessions WHERE id=?", (row["id"],))
        return None
    if (_now() - last).total_seconds() > _TOUCH_SECONDS:
        with db.tx() as conn:
            conn.execute("UPDATE admin_sessions SET last_seen=?, ip=? WHERE id=?",
                         (_now().isoformat(), client_ip(request), row["id"]))
    return row


def basic_ok(request, check_password) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user.encode(), b"admin") and check_password(pw)


def is_admin(request, check_password) -> bool:
    cached = getattr(request.state, "nnign_admin", None)
    if cached is None:
        cached = current_session(request) is not None or basic_ok(request, check_password)
        request.state.nnign_admin = cached
    return cached


def sessions():
    return db.query("SELECT * FROM admin_sessions ORDER BY last_seen DESC")


def revoke(session_id: int) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE id=?", (session_id,))


def revoke_others(request) -> int:
    cur = current_session(request)
    with db.tx() as conn:
        return conn.execute("DELETE FROM admin_sessions WHERE id!=?", (cur["id"] if cur else -1,)).rowcount


def logout(request) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        with db.tx() as conn:
            conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (_hash(token),))


def locked(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _failed.get(ip, []) if now - t < LOCK_SECONDS]
    _failed[ip] = hits
    return len(hits) >= LOCK_AFTER


def note_failure(ip: str) -> None:
    _failed.setdefault(ip, []).append(time.time())


def describe_agent(ua: str) -> str:
    """Kurzbeschreibung des Geräts aus dem User-Agent."""
    ua = ua or ""
    os_ = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS", "macOS"),
                                ("Windows", "Windows"), ("Linux", "Linux")) if k in ua), "")
    br = next((n for k, n in (("Edg/", "Edge"), ("Firefox", "Firefox"), ("Chrome", "Chrome"), ("Safari", "Safari"))
               if k in ua), "")
    return " · ".join(x for x in (br, os_) if x) or (ua[:60] or "unbekannt")
