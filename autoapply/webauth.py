"""Password hashing and login throttling for the phone gateway.

The stored hash (``GATEWAY_PASSWORD_HASH``) is ``scrypt:N:r:p:salt:hash`` with
URL-safe base64 parts: no ``$`` characters, so it survives ``.env`` files and
Docker Compose interpolation unchanged. Generate it with
``python -m autoapply.cli hash-password``.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets

MIN_PASSWORD = 12
WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_CLIENT = 5
MAX_FAILURES_TOTAL = 20


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, n=2 ** 15, r=8, p=1) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Use at least {MIN_PASSWORD} characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt:{n}:{r}:{p}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split(":")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), maxmem=128 * 1024 * 1024, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, _unb64(digest))


class LoginThrottle:
    """Failed-login limits per client and overall, persisted in SQLite."""

    def __init__(self, auto):
        self.auto = auto

    def allowed(self, client: str) -> bool:
        since = (datetime.now(timezone.utc) - WINDOW).isoformat()
        with self.auto.connect() as db:
            mine = db.execute("SELECT COUNT(*) FROM login_attempts WHERE client=? AND ok=0 AND at>=?", (client, since)).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM login_attempts WHERE ok=0 AND at>=?", (since,)).fetchone()[0]
        return mine < MAX_FAILURES_PER_CLIENT and total < MAX_FAILURES_TOTAL

    def record(self, client: str, ok: bool):
        now = datetime.now(timezone.utc)
        with self.auto.connect() as db:
            db.execute("INSERT INTO login_attempts(at,client,ok) VALUES (?,?,?)", (now.isoformat(), client, int(ok)))
            db.execute("DELETE FROM login_attempts WHERE at < ?", ((now - timedelta(days=7)).isoformat(),))
            if ok:
                db.execute("DELETE FROM login_attempts WHERE client=? AND ok=0", (client,))
