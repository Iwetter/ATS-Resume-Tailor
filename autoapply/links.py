"""Expiring, application-specific review links for the phone gateway.

A link is a random 80-bit code stored server-side (``review_links``), so it can
expire and be revoked. It only *identifies* what to show: opening it still
requires signing in to the gateway over HTTPS.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets
import string

KINDS = {"application": "r", "job": "j", "summary": "s"}
ALPHABET = string.ascii_lowercase  # letters only: never mistaken for a phone number by redaction


def issue(auto, kind, target, ttl_hours) -> str:
    """Reuse a live link with more than half its lifetime left; otherwise create one."""
    if kind not in KINDS:
        raise ValueError("Unknown link kind.")
    now = datetime.now(timezone.utc)
    with auto.connect() as db:
        row = db.execute("SELECT code FROM review_links WHERE kind=? AND target=? AND revoked_at IS NULL AND expires_at > ? ORDER BY expires_at DESC LIMIT 1",
                         (kind, target, (now + timedelta(hours=ttl_hours / 2)).isoformat())).fetchone()
        if row:
            return row["code"]
        code = "".join(secrets.choice(ALPHABET) for _ in range(17))
        db.execute("INSERT INTO review_links(code,kind,target,created_at,expires_at) VALUES (?,?,?,?,?)",
                   (code, kind, target, now.isoformat(), (now + timedelta(hours=ttl_hours)).isoformat()))
    return code


def resolve(auto, kind, code):
    """The link row if it exists, matches ``kind``, is unexpired and unrevoked."""
    if not code or len(code) > 40 or not code.isalpha():
        return None
    with auto.connect() as db:
        row = db.execute("SELECT * FROM review_links WHERE code=? AND kind=? AND revoked_at IS NULL AND expires_at > ?",
                         (code, kind, datetime.now(timezone.utc).isoformat())).fetchone()
    return dict(row) if row else None


def revoke_all(auto) -> int:
    with auto.connect() as db:
        return db.execute("UPDATE review_links SET revoked_at=? WHERE revoked_at IS NULL", (datetime.now(timezone.utc).isoformat(),)).rowcount


def url(settings, kind, code) -> str:
    """Absolute HTTPS URL, or "" when no public gateway is configured."""
    base = settings.public_base_url
    return f"{base}/{KINDS[kind]}/{code}" if base.startswith("https://") else ""
