"""Inbound SMS commands. Call only after the Twilio signature has been validated.

YES <ID>  approve exactly the prepared version that ID was issued for
NO <ID>   reject (cancel) that application
PAUSE     stop new preparation and submission now; monitoring continues
RESUME    restore the mode in force before PAUSE, re-running its safety checks
STATUS    worker health and today's activity

Authentication layers: the gateway validates ``X-Twilio-Signature``; here the
message must come from the account's own ``AccountSid`` and from the single
verified phone number. ``MessageSid`` is recorded before anything runs, so a
Twilio retry of the same message is answered from the record and never
executed twice. Approval IDs are one-time, expire, and are bound to an
``approval_hash``: if the form, resume or answers changed, the ID is refused.
The message body is untrusted data and is only matched against these words.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from hunter.models import utcnow

from . import controls
from . import db as S
from .notify import headline
from .sms import mask, normalize_phone, sanitize_outgoing
from .worker import health

COMMAND = re.compile(r"^\s*(YES|NO|PAUSE|RESUME|STATUS|HELP)\b[\s#:]*([A-Za-z0-9]{0,12})\s*[.!]?\s*$", re.I)
# Handled by Twilio itself (opt-out / opt-in). Twilio also forwards them to us.
OPT_OUT = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
OPT_IN = {"START", "UNSTOP", "OPTIN"}
HELP = "Auto Apply commands: YES <ID> approve, NO <ID> reject, PAUSE, RESUME, STATUS."


def _record(auto, sid, sender, command, outcome, reply):
    with auto.connect() as db:
        db.execute("UPDATE sms_inbound SET command=?, outcome=?, reply=? WHERE message_sid=?", (command, outcome, reply or "", sid))
    auto.audit("sms", "sms_command", details={"command": command, "outcome": outcome, "from": mask(sender)})


def handle(engine, sms, params: dict) -> str | None:
    """Process one validated inbound message. Returns reply text, or None for no reply."""
    auto, settings = engine.auto, engine.base_settings
    sid = (params.get("MessageSid") or params.get("SmsSid") or "").strip()
    if not re.fullmatch(r"(SM|MM)[0-9a-fA-F]{32}", sid):
        return None
    if params.get("AccountSid") != settings.twilio_account_sid:
        auto.audit("sms", "sms_rejected", details={"reason": "account mismatch"})
        return None
    try:
        sender = normalize_phone(params.get("From", ""))
    except ValueError:
        sender = ""
    with auto.connect() as db:
        claimed = db.execute("INSERT OR IGNORE INTO sms_inbound(message_sid,received_at,sender,outcome) VALUES (?,?,?,?)",
                             (sid, utcnow(), mask(sender), "processing")).rowcount
        if not claimed:  # Twilio retry or concurrent delivery: never execute twice
            row = db.execute("SELECT reply FROM sms_inbound WHERE message_sid=?", (sid,)).fetchone()
            return row["reply"] or None
    prefs = sms.prefs()
    if not sender or not prefs["phone_verified_at"] or sender != prefs["phone"]:
        _record(auto, sid, sender, "", "rejected_sender", "")
        return None  # never reply to unknown numbers
    body = (params.get("Body") or "")[:200]
    word = (body.strip().split() or [""])[0].upper().strip(".!")
    if word in OPT_OUT:
        sms._write({"opted_out": True})
        try:
            controls.pause(engine, actor="sms", reason="SMS opt-out keyword")
        except Exception:
            pass
        _record(auto, sid, sender, word, "opted_out_and_paused", "")
        return None  # Twilio sends the opt-out confirmation; replies are blocked now
    if word in OPT_IN:
        sms._write({"opted_out": False})
        _record(auto, sid, sender, word, "opted_in", "")
        return None
    match = COMMAND.match(body)
    if not match:
        _record(auto, sid, sender, "", "unrecognised", HELP)
        return HELP
    command, arg = match[1].upper(), match[2].upper()
    try:
        if command in ("YES", "NO"):
            reply, outcome = _decide(engine, sms, arg, command == "YES", sid)
        elif command == "PAUSE":
            reply, outcome = controls.pause(engine, actor="sms", reason="SMS PAUSE"), "paused"
        elif command == "RESUME":
            _, reply = controls.resume(engine, actor="sms")
            outcome = "resumed"
        elif command == "STATUS":
            reply, outcome = status_text(engine), "status"
        else:
            reply, outcome = HELP, "help"
    except ValueError as exc:
        reply, outcome = str(exc), "refused"
    reply = sanitize_outgoing(reply, limit=600)
    _record(auto, sid, sender, command + (" " + arg if arg else ""), outcome, reply)
    return reply


def _decide(engine, sms, code, approve, sid) -> tuple[str, str]:
    if not code:
        return f"Include the approval ID, e.g. {'YES' if approve else 'NO'} K7Q4M2.", "missing_id"
    row = sms.approval_code(code)
    if not row:
        return f"Unknown approval ID {code}.", "unknown_id"
    if row["used_at"]:
        return f"{code} was already used ({row['used_action']}).", "used_id"
    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        return f"{code} expired. Open the dashboard or review link to approve.", "expired_id"
    app = engine.auto.application(row["application_id"])
    if not app:
        return f"Unknown approval ID {code}.", "unknown_id"
    label = headline(app["company"], app["title"])
    if approve:
        if app["state"] != S.READY or app["approval_hash"] != row["approval_hash"]:
            sms.consume_code(code, "stale", sid)
            return f"{label} changed since {code} was sent (now {app['state']}). Nothing was approved; a new request follows if needed.", "stale_id"
        if not sms.consume_code(code, "approved", sid):
            return f"{code} was already used.", "used_id"
        try:
            engine.approve(app["id"], row["approval_hash"], actor="sms")
        except ValueError:
            return f"{label} changed before it could be approved. Nothing was approved.", "stale_id"
        reply = f"Approved {code}: {label}. It submits on the next worker cycle if every final check passes."
        if not engine.settings.allow_real_submission:
            reply += " Real submission is OFF on the server, so it will not be sent."
        if engine.auto.mode() == "OFF":
            reply += " Auto Apply is paused; text RESUME to continue."
        return reply, "approved"
    if not sms.consume_code(code, "rejected", sid):
        return f"{code} was already used.", "used_id"
    try:
        engine.cancel(app["id"], actor="sms")
    except ValueError:
        return f"{label} can no longer be rejected (status {engine.auto.application(app['id'])['state']}).", "refused"
    return f"Rejected {code}: {label}. It will not be submitted.", "rejected"


def status_text(engine) -> str:
    ok, beat = health(engine.store, engine.base_settings)
    heartbeat = engine.store.get("worker_heartbeat") or {}
    if heartbeat.get("at"):
        minutes = int((datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat["at"])).total_seconds() // 60)
        seen = f"{minutes}m ago"
    else:
        seen = "never"
    stats = controls.activity(engine, datetime.now(timezone.utc) - timedelta(days=1))
    parts = [f"Worker: {'healthy' if ok else 'NOT healthy'} (seen {seen}).",
             f"Mode: {controls.describe_mode(engine)}." + ("" if engine.auto.enabled() else " Master switch OFF."),
             "EMERGENCY STOP active." if engine.stopped() else "",
             f"Real submission: {'on' if engine.settings.allow_real_submission else 'off'}.",
             f"Last 24h: {stats['new_matches']} new matches, {stats['submitted_24h']}/{stats['cap']} submissions.",
             f"Now: {stats['awaiting_approval']} awaiting approval, {stats['needs_action']} need action, {stats['uncertain']} uncertain, {stats['failed']} failed."]
    return " ".join(p for p in parts if p)
