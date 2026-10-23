"""Turn decisions and application events into SMS notifications.

The notifier reads two append-only logs that the engine already writes, the
``decisions`` table (new high-match jobs) and the ``audit`` table
(application state changes), through persisted cursors. Restarts therefore
neither lose nor repeat events, and nothing in the engine has to know about
SMS. Each message is queued with a semantic ``dedupe_key``; the outbox makes
duplicates impossible even if a cursor is replayed.

Messages contain only employer, position, estimated fit, action taken,
current status, a one-time approval ID and an expiring review link.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging

from . import db as S
from . import links
from .controls import activity, describe_mode
from .engine import MAX_PREP_ATTEMPTS
from .redaction import log
from .sms import clean_text

logger = logging.getLogger("autoapply.notify")

ACTION = {
    S.QUEUED: "queued for preparation", S.PREPARING: "preparing", S.READY: "prepared; awaiting your approval",
    S.APPROVED: "approved; submits after final checks", S.SUBMITTING: "submitting", S.APPLIED: "submitted with confirmation",
    S.NEEDS_HUMAN: "handed to you", S.UNKNOWN: "submit clicked; outcome unconfirmed", S.FAILED: "preparation failed",
    S.BLOCKED: "stopped by eligibility gates", S.SKIPPED: "skipped as a duplicate", S.CANCELLED: "rejected",
}
PRIORITY = [S.READY, S.NEEDS_HUMAN, S.UNKNOWN, S.FAILED, S.APPLIED, S.APPROVED, S.QUEUED, S.PREPARING, S.SUBMITTING, S.BLOCKED, S.SKIPPED, S.CANCELLED]
# Process gates: a high fit is worth an alert even if you have not yet approved the employer.
PROCESS_CHECKS = {"Employer permission", "Applicant profile"}
EVENT_CATEGORY = {"ready_for_review": "approval", "submission_refused": "approval", "applied_confirmed": "submitted",
                  "unknown_resolved_applied": "submitted", "needs_human": "manual_action", "submission_unknown": "error",
                  "preparation_failed": "error", "recovered_after_restart": "error"}
MAX_SINGLE_MATCH_ALERTS = 3


def fit_of(app) -> str:
    before = (app.get("gates") or {}).get("fit")
    after = (app.get("plan") or {}).get("fit_after")
    if before is None:
        return "fit n/a"
    return f"fit {before:g}" + (f"→{after:g}" if after is not None and after != before else "")


def headline(company, title) -> str:
    return f"{clean_text(company, 40)} – {clean_text(title, 70)}"


def reason_summary(app) -> str:
    """Why a human is needed, without echoing question text or answers."""
    reasons = app.get("reasons") or []
    kinds = []
    for r in reasons:
        low = r.lower()
        # Blocker reasons are "kind: detail"; the rest are fixed engine phrases.
        for key, label in (("captcha:", "CAPTCHA"), ("login:", "login required"), ("mfa:", "verification code"), ("attestation", "legal attestation"),
                           ("unanswered required question", "unanswered question"), ("no supported candidate-form", "unsupported application site"),
                           ("redirect:", "unexpected redirect"), ("closed:", "posting closed"), ("access:", "access blocked"), ("form:", "no form found"),
                           ("could not fill", "a field could not be filled"), ("asked for more after the click", "employer asked for a code after submit"),
                           ("real submission is disabled", "real submission is off on the server"), (" cap (", "submission cap reached"),
                           ("changed since approval", "form changed since approval"), ("emergency stop", "emergency stop")):
            if key in low and label not in kinds:
                kinds.append(label)
    unanswered = sum("unanswered required question" in r.lower() for r in reasons)
    if unanswered > 1:
        kinds = [k if k != "unanswered question" else f"{unanswered} unanswered questions" for k in kinds]
    return ", ".join(kinds) or ("see review page" if reasons else "")


class Notifier:
    def __init__(self, engine, sms):
        self.engine, self.sms = engine, sms
        self.store, self.auto = engine.store, engine.auto

    # ---- links -------------------------------------------------------------------
    def _link(self, kind, target):
        code = links.issue(self.auto, kind, target, self.engine.base_settings.review_link_ttl_hours)
        return links.url(self.engine.base_settings, kind, code) or "open the dashboard"

    # ---- composition ---------------------------------------------------------------
    def approval_message(self, app, code, expires_at) -> str:
        hours = max(1, round((datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)).total_seconds() / 3600))
        note = ""
        summary = reason_summary(app)
        if summary and summary != "see review page":
            note = f" Note: {summary}."
        if not self.engine.settings.allow_real_submission:
            note += " Real submission is off on the server, so approving will not send it."
        return (f"Approve? {headline(app['company'], app['title'])} ({fit_of(app)}). Action: {ACTION[S.READY]}. Status: {S.READY}.{note} "
                f"Reply YES {code} or NO {code} (expires in {hours}h). Review: {self._link('application', app['id'])}")

    def event_message(self, category, app) -> str:
        lead = {"submitted": "Submitted", "manual_action": "Action needed", "error": "Problem"}[category]
        why = reason_summary(app)
        if app["state"] == S.UNKNOWN:
            why = (why + "; " if why else "") + "check your email/the employer portal before any retry"
        return (f"{lead}: {headline(app['company'], app['title'])} ({fit_of(app)}). Action: {ACTION.get(app['state'], app['state'].lower())}. "
                f"Status: {app['state']}." + (f" Why: {why}." if why and category != "submitted" else "") + f" Review: {self._link('application', app['id'])}")

    def match_line(self, job, fit, app) -> str:
        action = ACTION.get(app["state"], "tracked") if app else "none yet (employer not approved for automation or switch off)"
        status = app["state"] if app else "Not queued"
        target = ("application", app["id"]) if app else ("job", job["key"])
        return f"{headline(job['company'], job['title'])} (fit {fit:g}). Action: {action}. Status: {status}. Review: {self._link(*target)}"

    # ---- collection --------------------------------------------------------------------
    def _cursor(self, name, table):
        value = self.store.get(name)
        if value is None:  # first run: start at the present, never replay history by SMS
            with self.auto.connect() as db:
                value = db.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            self.store.set(name, value)
        return value

    def collect_matches(self, prefs, deliver) -> int:
        cursor = self._cursor("notify_decision_cursor", "decisions")
        with self.auto.connect() as db:
            rows = db.execute("SELECT id, job_key, fit, details FROM decisions WHERE id > ? AND trigger='new_job' ORDER BY id LIMIT 500", (cursor,)).fetchall()
            last = db.execute("SELECT COALESCE(MAX(id), ?) FROM decisions", (cursor,)).fetchone()[0]
        threshold = self.engine.settings.min_fit_auto
        matches = []
        for row in rows:
            details = json.loads(row["details"])
            hard_fail = [c for c in details.get("checks", []) if c["status"] == "fail" and c["name"] not in PROCESS_CHECKS]
            if row["fit"] is not None and row["fit"] >= threshold and not hard_fail:
                record = self.store.job(row["job_key"])
                if record:
                    payload = json.loads(record["payload"])
                    matches.append(({"key": row["job_key"], "company": payload["company"], "title": payload["title"]}, row["fit"], self.auto.application(job_key=row["job_key"])))
        self.store.set("notify_decision_cursor", rows[-1]["id"] if len(rows) == 500 else last)
        if not (deliver and matches and prefs["delivery"] == "immediate" and self.sms.wants("new_match", prefs)):
            return 0
        matches.sort(key=lambda m: -m[1])
        if len(matches) <= MAX_SINGLE_MATCH_ALERTS:
            return sum(self.sms.enqueue("new_match", f"new_match:{job['key']}", "New match: " + self.match_line(job, fit, app), app["id"] if app else None)
                       for job, fit, app in matches)
        top = "\n".join(f"{i}) " + self.match_line(job, fit, app) for i, (job, fit, app) in enumerate(matches[:MAX_SINGLE_MATCH_ALERTS], 1))
        body = f"{len(matches)} new high-match internships.\n{top}\n+{len(matches) - MAX_SINGLE_MATCH_ALERTS} more: {self._summary_link()}"
        return int(self.sms.enqueue("new_match", "new_match_batch:" + ",".join(sorted(m[0]["key"] for m in matches))[:400], body))

    def collect_events(self, prefs, deliver) -> int:
        cursor = self._cursor("notify_audit_cursor", "audit")
        with self.auto.connect() as db:
            rows = db.execute("SELECT id, action, application_id, details FROM audit WHERE id > ? ORDER BY id LIMIT 500", (cursor,)).fetchall()
        if rows:
            self.store.set("notify_audit_cursor", rows[-1]["id"])
        created = 0
        for row in rows:
            category = EVENT_CATEGORY.get(row["action"])
            if not (deliver and category and row["application_id"] and self.sms.wants(category, prefs)):
                continue
            app = self.auto.application(row["application_id"])
            if not app:
                continue
            if category == "approval":
                created += self.request_approval(app)
                continue
            if prefs["delivery"] != "immediate":
                continue
            if category == "manual_action" and app["state"] != S.NEEDS_HUMAN:
                continue
            if category == "error":
                details = json.loads(row["details"] or "{}")
                if row["action"] == "recovered_after_restart" and details.get("state") != S.UNKNOWN:
                    continue
                if row["action"] == "preparation_failed" and app["attempts"] < MAX_PREP_ATTEMPTS:
                    continue  # retried automatically; alert only when retries are exhausted
                if app["state"] not in (S.UNKNOWN, S.FAILED):
                    continue
            created += self.sms.enqueue(category, f"{category}:{app['id']}:{row['id']}", self.event_message(category, app), app["id"])
        return created

    def request_approval(self, app) -> int:
        """One approval ID per exact prepared version; re-preparing an unchanged form reuses it."""
        if app["state"] != S.READY or not app.get("approval_hash"):
            return 0
        code, expires, _ = self.sms.issue_approval_code(app["id"], app["approval_hash"])
        return int(self.sms.enqueue("approval", f"approval:{code}", self.approval_message(app, code, expires), app["id"]))

    # ---- daily summary ----------------------------------------------------------------------
    def _summary_link(self):
        return self._link("summary", datetime.now(timezone.utc).date().isoformat())

    def daily_summary(self, now=None) -> str:
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(days=1)
        stats = activity(self.engine, since)
        apps = [a for a in self.auto.applications(limit=200) if a["updated_at"] >= since.isoformat() or a["state"] in (S.READY, S.NEEDS_HUMAN, S.UNKNOWN)]
        apps.sort(key=lambda a: (PRIORITY.index(a["state"]) if a["state"] in PRIORITY else 99, a["updated_at"]))
        changed = stats["changed"]
        head = (f"Auto Apply daily summary: {stats['new_matches']} new high-match, {changed.get(S.READY, 0) + changed.get(S.NEEDS_HUMAN, 0)} prepared, "
                f"{stats['awaiting_approval']} awaiting approval, {changed.get(S.APPLIED, 0)} submitted, {stats['needs_action']} need action, "
                f"{stats['uncertain']} uncertain, {stats['failed']} failed. Mode: {describe_mode(self.engine)}.")
        lines = []
        for i, app in enumerate(apps[:6], 1):
            line = f"{i}) {headline(app['company'], app['title'])} · {fit_of(app)} · {ACTION.get(app['state'], '')} · {app['state']} · {self._link('application', app['id'])}"
            if app["state"] == S.READY and app.get("approval_hash"):
                code, _, _ = self.sms.issue_approval_code(app["id"], app["approval_hash"])
                line += f" · YES/NO {code}"
            lines.append(line)
        if not apps:
            lines.append("No application activity in the last 24 hours.")
        more = f"\nAll {len(apps)}: {self._summary_link()}" if len(apps) > 6 else f"\nFull list: {self._summary_link()}"
        return head + "\n" + "\n".join(lines) + more

    def maybe_daily_summary(self, prefs, deliver, now=None) -> int:
        if not (deliver and self.sms.wants("daily_summary", prefs)):
            return 0
        local = self.sms.local_now(prefs, now)
        if local.hour < prefs["summary_hour"]:
            return 0
        return int(self.sms.enqueue("daily_summary", f"daily_summary:{local.date().isoformat()}", self.daily_summary(now)))

    # ---- one pass ------------------------------------------------------------------------------
    def run(self, now=None) -> dict:
        prefs = self.sms.prefs()
        deliver = self.sms.deliverable(prefs)[0]
        queued = 0
        for step in (lambda: self.collect_matches(prefs, deliver), lambda: self.collect_events(prefs, deliver),
                     lambda: self.maybe_daily_summary(prefs, deliver, now)):
            try:
                queued += step()
            except Exception as exc:  # one bad event never blocks the others
                log(logger, logging.ERROR, "notification step failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        result = self.sms.flush()
        result["queued"] = queued
        return result
