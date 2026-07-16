"""Auth, sessions, CSRF, host validation, rate limiting.

AIDEV-CAUTION: the LAN is not a trust boundary (invariant 4). Every mutating
admin route checks CSRF; the host middleware is the DNS-rebinding defense;
login is rate-limited. Weakening any of these is a security regression.
"""

import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.core import db, snapshots

_ph = PasswordHasher()          # argon2id defaults
SESSION_COOKIE = "ss_session"
SESSION_MAX_AGE = 12 * 3600

_ALLOWED_HOST_PREFIXES = ("localhost", "127.0.0.1", "signalshack.local", "testserver")


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(hashed: str, pw: str) -> bool:
    try:
        return _ph.verify(hashed, pw)
    except VerifyMismatchError:
        return False


def app_secret() -> str:
    conn = db.connect()
    try:
        sec = snapshots.kv_get(conn, "app_secret", None)
        if not sec:
            sec = secrets.token_hex(32)
            snapshots.kv_set(conn, "app_secret", sec)
        return sec
    finally:
        conn.close()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app_secret(), salt="ss-session")


def make_session() -> tuple[str, str]:
    """-> (cookie_value, csrf_token)"""
    csrf = secrets.token_hex(16)
    return _serializer().dumps({"admin": True, "csrf": csrf}), csrf


def read_session(request: Request) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


def check_csrf(session: dict, form_token: str | None) -> bool:
    return bool(form_token) and secrets.compare_digest(session.get("csrf", ""),
                                                       form_token or "")


# ---- display unlock cookie (optional display PIN, plan V1.1)

DISPLAY_COOKIE = "ss_display"


def make_display_cookie() -> str:
    return URLSafeTimedSerializer(app_secret(), salt="ss-display").dumps({"ok": True})


def display_unlocked(request: Request) -> bool:
    raw = request.cookies.get(DISPLAY_COOKIE)
    if not raw:
        return False
    try:
        URLSafeTimedSerializer(app_secret(), salt="ss-display").loads(
            raw, max_age=365 * 24 * 3600)      # a TV unlocks once a year at most
        return True
    except BadSignature:
        return False


def host_allowed(request: Request, extra: str | None = None) -> bool:
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host.startswith(_ALLOWED_HOST_PREFIXES):
        return True
    if extra and host == extra.lower():
        return True
    # allow direct LAN-IP access (the documented mDNS fallback)
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


# ---- login rate limit: 5 attempts / 15 min per client IP (in-memory)
_attempts: dict[str, list[float]] = {}


def rate_limited(ip: str, limit: int = 5, window_s: int = 900) -> bool:
    now = time.time()
    recent = [t for t in _attempts.get(ip, []) if now - t < window_s]
    _attempts[ip] = recent
    return len(recent) >= limit


def record_attempt(ip: str) -> None:
    _attempts.setdefault(ip, []).append(time.time())
