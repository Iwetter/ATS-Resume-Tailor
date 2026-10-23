"""Two-way SMS over Twilio (official ``twilio`` Python SDK).

* **Outbox.** Every notification is a row in ``sms_outbox`` with a unique
  ``dedupe_key``: the same event can never be queued twice, across restarts
  and processes. Rows are claimed atomically before sending. Transient
  failures retry with exponential backoff (bounded); permanent Twilio errors
  fail at once. A send interrupted by a crash is marked failed and *not*
  resent (at most once), because Twilio may already have accepted it.
* **What may be sent.** Employer, position, fit, action, status, one-time
  approval IDs and expiring review links. Nothing from the applicant profile,
  no answers and no credentials. Every body passes ``sanitize_outgoing``,
  which strips links copied from job postings and redacts contact details and
  the applicant's name.
* **Verified number.** Alerts go only to a number confirmed with a one-time
  code, and inbound commands are accepted only from that number.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hunter.models import utcnow

from .db import AutoStore
from .redaction import redact_text

CATEGORIES = {
    "new_match": "Newly discovered high-match internships",
    "approval": "Approval requests (Approval Required)",
    "submitted": "Successfully submitted applications",
    "manual_action": "Applications requiring manual action",
    "error": "Submission errors and uncertain outcomes",
    "daily_summary": "Daily summary",
}
DELIVERY = {"immediate": "Immediate alerts + daily summary", "digest": "Daily summary only (approval requests still immediate)"}
DEFAULTS = {"enabled": False, "phone": "", "phone_verified_at": None, "delivery": "immediate", "categories": list(CATEGORIES),
            "summary_hour": 18, "timezone": "America/New_York", "opted_out": False}
MAX_ATTEMPTS = 5
STALE_SENDING = timedelta(minutes=10)
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
# Twilio errors that will not succeed on retry (invalid/unreachable number, opted out, bad credentials…).
# Typographic characters outside GSM-7 switch a whole SMS to UCS-2 (70 instead of
# 160 characters per segment, so roughly triple the cost). Plain equivalents instead.
GSM_FRIENDLY = str.maketrans({"–": "-", "—": "-", "→": "->", "…": "...", "“": '"', "”": '"', "‘": "'", "’": "'", "•": "*", "·": "|", " ": " ", "✱": "*"})
PERMANENT_CODES = {20003, 20404, 21211, 21408, 21606, 21610, 21612, 21614, 30003, 30004, 30005, 30006, 30007}
OPTED_OUT_CODE = 21610


def normalize_phone(raw: str) -> str:
    """E.164. Bare 10-digit numbers are taken as U.S. numbers."""
    value = (raw or "").strip()
    digits = re.sub(r"\D", "", value)
    if value.startswith("+"):
        phone = "+" + digits
    elif len(digits) == 10:
        phone = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        phone = "+" + digits
    else:
        phone = ""
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        raise ValueError("Enter a mobile number such as +14045550100.")
    return phone


def mask(phone: str) -> str:
    return "•••" + phone[-4:] if phone else ""


def clean_text(value, limit=60) -> str:
    """Untrusted posting text for an SMS: no links, no control characters, bounded."""
    text = re.sub(r"https?://\S+|www\.\S+", "", str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def sanitize_outgoing(body: str, limit=1500) -> str:
    """Redact everything except our own https links. Posting text has already had its
    links stripped by ``clean_text``, so the only URLs left are gateway review links,
    which must survive intact even if the hostname happens to contain a redacted name."""
    body = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", body).translate(GSM_FRIENDLY)
    body = "".join(part if part.startswith("https://") else redact_text(part) for part in re.split(r"(https://[A-Za-z0-9.\-]+(?::\d+)?/[a-z]/[a-z]+)", body)).strip()
    return body if len(body) <= limit else body[: limit - 3] + "..."


def _hash_code(code: str, salt: str) -> str:
    return hashlib.sha256((salt + code).encode()).hexdigest()


class SmsService:
    """Settings, outbox, phone verification and approval IDs. ``client`` / ``http_client`` are injectable."""

    def __init__(self, store, settings, client=None, http_client=None):
        self.store, self.settings = store, settings
        self.auto = AutoStore(store)
        self._client, self._http_client = client, http_client

    # ---- configuration ---------------------------------------------------------
    def configured(self) -> tuple[bool, str]:
        s = self.settings
        if not s.twilio_account_sid or not s.twilio_auth_token:
            return False, "Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN (the token also validates webhooks)."
        if not (s.twilio_from_number or s.twilio_messaging_service_sid):
            return False, "Set TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID."
        return True, "Twilio configured."

    @property
    def client(self):
        if self._client is None:
            from twilio.rest import Client
            s = self.settings
            if s.twilio_api_key_sid and s.twilio_api_key_secret:
                self._client = Client(s.twilio_api_key_sid, s.twilio_api_key_secret, s.twilio_account_sid, http_client=self._http_client)
            else:
                self._client = Client(s.twilio_account_sid, s.twilio_auth_token, http_client=self._http_client)
        return self._client

    def prefs(self) -> dict:
        saved = self.store.get("sms_settings") or {}
        return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}

    def _write(self, values):
        self.store.set("sms_settings", {**self.prefs(), **values})

    def save_prefs(self, values: dict, actor="user") -> dict:
        allowed = {"enabled", "delivery", "categories", "summary_hour", "timezone"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("Unknown SMS setting: " + ", ".join(sorted(unknown)))
        current = self.prefs()
        if values.get("enabled") and not current["phone_verified_at"]:
            raise ValueError("Verify your phone number before enabling SMS.")
        if "delivery" in values and values["delivery"] not in DELIVERY:
            raise ValueError("Delivery must be immediate or digest.")
        if "categories" in values:
            values["categories"] = [c for c in CATEGORIES if c in set(values["categories"])]
        if "summary_hour" in values:
            hour = int(values["summary_hour"])
            if not 0 <= hour <= 23:
                raise ValueError("Summary hour must be 0–23.")
            values["summary_hour"] = hour
        if "timezone" in values:
            try:
                ZoneInfo(values["timezone"])
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Unknown time zone; use a name such as America/New_York.") from None
        self._write(values)
        self.auto.audit(actor, "sms_settings_saved", details={k: v for k, v in values.items()})
        return self.prefs()

    def deliverable(self, prefs=None) -> tuple[bool, str]:
        prefs = prefs or self.prefs()
        ok, why = self.configured()
        if not ok:
            return False, why
        if not prefs["enabled"]:
            return False, "SMS is turned off."
        if not prefs["phone"] or not prefs["phone_verified_at"]:
            return False, "No verified phone number."
        if prefs["opted_out"]:
            return False, "Your number opted out (texted STOP). Text START to the Twilio number to opt back in."
        return True, "Ready."

    def wants(self, category, prefs=None) -> bool:
        prefs = prefs or self.prefs()
        return category in prefs["categories"]

    def local_now(self, prefs=None, now=None):
        prefs = prefs or self.prefs()
        return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(prefs["timezone"]))

    # ---- sending -----------------------------------------------------------------
    def _send_now(self, to, body) -> str:
        s = self.settings
        kwargs = {"to": to, "body": body}
        if s.twilio_messaging_service_sid:
            kwargs["messaging_service_sid"] = s.twilio_messaging_service_sid
        else:
            kwargs["from_"] = s.twilio_from_number
        if s.public_base_url.startswith("https://"):
            kwargs["status_callback"] = s.public_base_url + "/sms/status"
        return self.client.messages.create(**kwargs).sid

    def enqueue(self, category, dedupe_key, body, application_id=None) -> bool:
        """Queue once per ``dedupe_key``. Returns False when it was already queued."""
        with self.auto.connect() as db:
            inserted = db.execute("INSERT OR IGNORE INTO sms_outbox(dedupe_key,category,application_id,body,created_at,status,next_attempt_at) VALUES (?,?,?,?,?,?,?)",
                                  (dedupe_key, category, application_id, sanitize_outgoing(body), utcnow(), "pending", utcnow())).rowcount
        return bool(inserted)

    def flush(self, limit=20) -> dict:
        counts = {"sent": 0, "retry": 0, "failed": 0, "suppressed": 0, "interrupted": 0}
        now = datetime.now(timezone.utc)
        with self.auto.connect() as db:
            counts["interrupted"] = db.execute(
                "UPDATE sms_outbox SET status='failed', last_error='Interrupted while sending; not resent to avoid a duplicate text.' WHERE status='sending' AND claimed_at < ?",
                ((now - STALE_SENDING).isoformat(),)).rowcount
        prefs = self.prefs()
        ok, why = self.deliverable(prefs)
        with self.auto.connect() as db:
            rows = db.execute("SELECT * FROM sms_outbox WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT ?",
                              (now.isoformat(), limit)).fetchall()
            if not ok:
                # Never deliver a backlog later: an alert that could not go out now is dropped visibly.
                for row in rows:
                    db.execute("UPDATE sms_outbox SET status='suppressed', last_error=? WHERE id=? AND status='pending'", (why, row["id"]))
                counts["suppressed"] = len(rows)
                return counts
        for row in rows:
            with self.auto.connect() as db:
                if not db.execute("UPDATE sms_outbox SET status='sending', claimed_at=? WHERE id=? AND status='pending'", (utcnow(), row["id"])).rowcount:
                    continue  # another process claimed it
            counts[self._deliver(row, prefs["phone"])] += 1
        return counts

    def _deliver(self, row, to) -> str:
        from twilio.base.exceptions import TwilioRestException
        attempts = row["attempts"] + 1
        try:
            sid = self._send_now(to, row["body"])
        except TwilioRestException as exc:
            permanent = exc.code in PERMANENT_CODES or exc.status in (400, 401, 403, 404)
            if exc.code == OPTED_OUT_CODE:
                self._write({"opted_out": True})
            return self._retry_or_fail(row, attempts, f"Twilio {exc.status} error {exc.code}: {str(exc.msg)[:160]}", permanent)
        except Exception as exc:  # network, timeout, SDK error: transient
            return self._retry_or_fail(row, attempts, f"{type(exc).__name__}: {str(exc)[:160]}", False)
        with self.auto.connect() as db:
            db.execute("UPDATE sms_outbox SET status='sent', sid=?, sent_at=?, attempts=?, last_error='' WHERE id=?", (sid, utcnow(), attempts, row["id"]))
        return "sent"

    def _retry_or_fail(self, row, attempts, error, permanent) -> str:
        error = redact_text(error)
        with self.auto.connect() as db:
            if permanent or attempts >= MAX_ATTEMPTS:
                db.execute("UPDATE sms_outbox SET status='failed', attempts=?, last_error=? WHERE id=?", (attempts, error, row["id"]))
                outcome = "failed"
            else:
                delay = min(3600, 60 * 2 ** (attempts - 1))
                db.execute("UPDATE sms_outbox SET status='pending', attempts=?, last_error=?, next_attempt_at=? WHERE id=?",
                           (attempts, error, (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(), row["id"]))
                outcome = "retry"
        self.auto.audit("worker", "sms_" + outcome, application_id=row["application_id"], details={"category": row["category"], "error": error})
        return outcome

    def send_test(self, actor="user") -> dict:
        ok, why = self.deliverable()
        if not ok:
            raise ValueError(why)
        key = "test:" + secrets.token_hex(6)
        self.enqueue("test", key, "Auto Apply test message: SMS alerts reach this phone. Reply STATUS for a summary.")
        self.flush()
        self.auto.audit(actor, "sms_test")
        with self.auto.connect() as db:
            return dict(db.execute("SELECT status, last_error, sid FROM sms_outbox WHERE dedupe_key=?", (key,)).fetchone())

    def record_status(self, sid, status, error_code=None) -> bool:
        with self.auto.connect() as db:
            changed = db.execute("UPDATE sms_outbox SET delivery_status=?, last_error=CASE WHEN ? IS NULL OR ?='' THEN last_error ELSE ? END WHERE sid=?",
                                 (status, error_code, error_code, f"Delivery error {error_code}", sid)).rowcount
        if str(error_code) == str(OPTED_OUT_CODE):
            self._write({"opted_out": True})
        return bool(changed)

    def outbox(self, limit=50):
        with self.auto.connect() as db:
            return [dict(r) for r in db.execute("SELECT id, created_at, category, status, attempts, delivery_status, last_error, body FROM sms_outbox ORDER BY id DESC LIMIT ?", (limit,))]

    # ---- phone verification ----------------------------------------------------------
    def start_verification(self, raw_phone, actor="user") -> str:
        ok, why = self.configured()
        if not ok:
            raise ValueError(why)
        phone = normalize_phone(raw_phone)
        pending = self.store.get("sms_phone_pending") or {}
        recent = [t for t in pending.get("sent", []) if datetime.fromisoformat(t) > datetime.now(timezone.utc) - timedelta(hours=1)]
        if len(recent) >= 5:
            raise ValueError("Too many verification codes this hour; try again later.")
        code, salt = f"{secrets.randbelow(10 ** 6):06d}", secrets.token_hex(8)
        self._send_now(phone, f"Auto Apply verification code: {code}. It expires in 10 minutes. If you did not request it, ignore this text.")
        self.store.set("sms_phone_pending", {"phone": phone, "salt": salt, "hash": _hash_code(code, salt), "attempts": 0,
                                             "expires": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), "sent": recent + [utcnow()]})
        self.auto.audit(actor, "sms_verification_sent", details={"to": mask(phone)})
        return phone

    def confirm_verification(self, code, actor="user") -> str:
        pending = self.store.get("sms_phone_pending") or {}
        if not pending.get("hash"):
            raise ValueError("Request a verification code first.")
        if datetime.fromisoformat(pending["expires"]) < datetime.now(timezone.utc):
            raise ValueError("The code expired; request a new one.")
        if pending["attempts"] >= 5:
            raise ValueError("Too many wrong codes; request a new one.")
        if not hmac.compare_digest(_hash_code(str(code).strip(), pending["salt"]), pending["hash"]):
            self.store.set("sms_phone_pending", {**pending, "attempts": pending["attempts"] + 1})
            raise ValueError("That code is not correct.")
        self._write({"phone": pending["phone"], "phone_verified_at": utcnow(), "opted_out": False})
        self.store.set("sms_phone_pending", {"sent": pending.get("sent", [])})
        self.auto.audit(actor, "sms_phone_verified", details={"phone": mask(pending["phone"])})
        return pending["phone"]

    def forget_phone(self, actor="user"):
        self._write({"phone": "", "phone_verified_at": None, "enabled": False})
        self.auto.audit(actor, "sms_phone_removed")

    # ---- one-time approval IDs ---------------------------------------------------------
    def issue_approval_code(self, application_id, approval_hash) -> tuple[str, str, bool]:
        """(code, expires_at, created). Reuses the live code for this exact version."""
        now = datetime.now(timezone.utc)
        with self.auto.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT code, expires_at FROM approval_codes WHERE application_id=? AND approval_hash=? AND used_at IS NULL AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
                             (application_id, approval_hash, now.isoformat())).fetchone()
            if row:
                return row["code"], row["expires_at"], False
            expires = (now + timedelta(hours=self.settings.sms_approval_ttl_hours)).isoformat()
            while True:
                code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
                if not db.execute("SELECT 1 FROM approval_codes WHERE code=?", (code,)).fetchone():
                    break
            db.execute("INSERT INTO approval_codes(code,application_id,approval_hash,created_at,expires_at) VALUES (?,?,?,?,?)",
                       (code, application_id, approval_hash, now.isoformat(), expires))
        return code, expires, True

    def approval_code(self, code):
        with self.auto.connect() as db:
            row = db.execute("SELECT * FROM approval_codes WHERE code=?", ((code or "").upper(),)).fetchone()
        return dict(row) if row else None

    def consume_code(self, code, action, via) -> bool:
        """Atomic one-time use. False if it was already used."""
        with self.auto.connect() as db:
            return bool(db.execute("UPDATE approval_codes SET used_at=?, used_action=?, used_via=? WHERE code=? AND used_at IS NULL",
                                   (utcnow(), action, via, code)).rowcount)
