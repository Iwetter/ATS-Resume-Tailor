"""Application, decision, audit, permission and usage tables.

All tables live in Hunter's SQLite database next to the existing job data so a
single persistent volume holds everything. Creation is additive and
idempotent; no existing table is altered or dropped.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import uuid

from hunter.models import utcnow
from hunter.storage import Store

from .redaction import redact

MODES = ("OFF", "REVIEW", "AUTO")
DEFAULT_MODE = "REVIEW"

# Application lifecycle. APPLIED needs captured confirmation evidence.
QUEUED, PREPARING, NEEDS_HUMAN, READY, APPROVED, SUBMITTING = "Queued", "Preparing", "Needs Human Action", "Ready for Review", "Approved", "Submitting"
APPLIED, UNKNOWN, FAILED, BLOCKED, SKIPPED, CANCELLED = "Applied", "Unknown", "Failed", "Blocked", "Skipped", "Cancelled"
STATES = (QUEUED, PREPARING, NEEDS_HUMAN, READY, APPROVED, SUBMITTING, APPLIED, UNKNOWN, FAILED, BLOCKED, SKIPPED, CANCELLED)
# States that consume submission caps: a click may have happened.
SUBMISSION_STATES = (SUBMITTING, APPLIED, UNKNOWN)
TERMINAL = (APPLIED, BLOCKED, SKIPPED, CANCELLED)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY, job_key TEXT NOT NULL UNIQUE, company TEXT NOT NULL, title TEXT NOT NULL,
  dedupe_key TEXT NOT NULL, employer_key TEXT NOT NULL, adapter TEXT, apply_url TEXT,
  state TEXT NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, lock_token TEXT, lock_expires TEXT,
  submit_started_at TEXT, submitted_at TEXT, approval_hash TEXT, approved_at TEXT,
  artifact_key TEXT, gates TEXT NOT NULL DEFAULT '{}', plan TEXT NOT NULL DEFAULT '{}',
  reasons TEXT NOT NULL DEFAULT '[]', confirmation TEXT NOT NULL DEFAULT '{}', evidence_dir TEXT,
  last_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS applications_state ON applications(state, updated_at);
CREATE INDEX IF NOT EXISTS applications_dedupe ON applications(dedupe_key);
CREATE TABLE IF NOT EXISTS submission_intents (
  job_key TEXT PRIMARY KEY, application_id TEXT NOT NULL, token TEXT NOT NULL, started_at TEXT NOT NULL,
  outcome TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, at TEXT NOT NULL, trigger TEXT NOT NULL,
  eligible INTEGER NOT NULL, prepare_ok INTEGER NOT NULL, auto_ok INTEGER NOT NULL,
  fit REAL, details TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS decisions_job ON decisions(job_key, id);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  job_key TEXT, application_id TEXT, details TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS audit_app ON audit(application_id, id);
CREATE TABLE IF NOT EXISTS employer_permissions (
  employer_key TEXT PRIMARY KEY, name TEXT NOT NULL, prefill INTEGER NOT NULL DEFAULT 0,
  auto_submit INTEGER NOT NULL DEFAULT 0, terms_reviewed_at TEXT, approved_attestations TEXT NOT NULL DEFAULT '[]',
  notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS board_status (
  employer_key TEXT PRIMARY KEY, name TEXT NOT NULL, last_attempt TEXT, last_success TEXT,
  last_checked TEXT, last_error TEXT NOT NULL DEFAULT '', consecutive_failures INTEGER NOT NULL DEFAULT 0,
  next_due TEXT, jobs INTEGER NOT NULL DEFAULT 0, new_jobs INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS answer_bank (
  id TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'text',
  verified_at TEXT, source TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, task TEXT NOT NULL, model TEXT NOT NULL, cache_key TEXT NOT NULL,
  status TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  reserved_usd REAL NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, task TEXT NOT NULL, created TEXT NOT NULL, value TEXT NOT NULL);
'''


class AutoStore:
    """Thin layer over Hunter's :class:`Store`; shares its database file."""

    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        with self.store.connect() as db:
            yield db

    # ---- flags -------------------------------------------------------------
    def mode(self):
        value = self.store.get("autoapply_mode", DEFAULT_MODE)
        return value if value in MODES else DEFAULT_MODE

    def set_mode(self, mode, actor="user"):
        if mode not in MODES:
            raise ValueError("Mode must be OFF, REVIEW or AUTO.")
        if mode == "AUTO":
            raise ValueError("AUTO is enabled only through enable_auto() after a successful dry run.")
        self.store.set("autoapply_mode", mode)
        self.store.set("auto_enabled", None)
        self.audit(actor, "mode_changed", details={"mode": mode})

    def enabled(self):
        return bool(self.store.get("autoapply_enabled", False))

    def set_enabled(self, value: bool, actor="user"):
        self.store.set("autoapply_enabled", bool(value))
        self.audit(actor, "master_switch", details={"enabled": bool(value)})

    def emergency_stopped(self, settings=None):
        if self.store.get("emergency_stop", False):
            return True
        if settings is not None and (settings.emergency_stop_env or settings.stop_file.exists()):
            return True
        return False

    def set_emergency_stop(self, value: bool, actor="user", reason=""):
        self.store.set("emergency_stop", bool(value))
        self.audit(actor, "emergency_stop" if value else "emergency_stop_cleared", details={"reason": reason})

    # ---- audit -------------------------------------------------------------
    def audit(self, actor, action, job_key=None, application_id=None, details=None):
        with self.connect() as db:
            db.execute("INSERT INTO audit(at,actor,action,job_key,application_id,details) VALUES (?,?,?,?,?,?)",
                       (utcnow(), actor, action, job_key, application_id, json.dumps(redact(details or {}), ensure_ascii=False, default=str)))

    def audit_log(self, application_id=None, limit=500):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM audit" + (" WHERE application_id=?" if application_id else "") + " ORDER BY id DESC LIMIT ?",
                              ((application_id, limit) if application_id else (limit,))).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    # ---- decisions ---------------------------------------------------------
    def record_decision(self, job_key, trigger, gate):
        with self.connect() as db:
            db.execute("INSERT INTO decisions(job_key,at,trigger,eligible,prepare_ok,auto_ok,fit,details) VALUES (?,?,?,?,?,?,?,?)",
                       (job_key, utcnow(), trigger, int(gate.eligible), int(gate.prepare_ok), int(gate.auto_ok), gate.fit, json.dumps(gate.as_dict(), ensure_ascii=False)))

    def decisions(self, job_key=None, limit=500):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM decisions" + (" WHERE job_key=?" if job_key else "") + " ORDER BY id DESC LIMIT ?",
                              ((job_key, limit) if job_key else (limit,))).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    # ---- employer permissions ----------------------------------------------
    def permission(self, employer_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM employer_permissions WHERE employer_key=?", (employer_key,)).fetchone()
        if not row:
            return {"employer_key": employer_key, "prefill": False, "auto_submit": False, "terms_reviewed_at": None, "approved_attestations": [], "notes": ""}
        return {**dict(row), "prefill": bool(row["prefill"]), "auto_submit": bool(row["auto_submit"]), "approved_attestations": json.loads(row["approved_attestations"])}

    def set_permission(self, employer_key, name, *, prefill, auto_submit, terms_reviewed, notes="", actor="user"):
        if auto_submit and not (prefill and terms_reviewed):
            raise ValueError("Automatic submission needs form preparation enabled and the employer/ATS terms reviewed.")
        current = self.permission(employer_key)
        reviewed_at = current.get("terms_reviewed_at") if terms_reviewed and current.get("terms_reviewed_at") else (utcnow() if terms_reviewed else None)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO employer_permissions VALUES (?,?,?,?,?,?,?,?)",
                       (employer_key, name, int(prefill), int(auto_submit), reviewed_at, json.dumps(current["approved_attestations"]), notes, utcnow()))
        self.audit(actor, "employer_permission", details={"employer": employer_key, "prefill": prefill, "auto_submit": auto_submit, "terms_reviewed": terms_reviewed})

    def approve_attestation(self, employer_key, name, text_hash, text, actor="user"):
        current = self.permission(employer_key)
        values = [a for a in current["approved_attestations"] if a["hash"] != text_hash] + [{"hash": text_hash, "text": text[:500], "approved_at": utcnow()}]
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO employer_permissions VALUES (?,?,?,?,?,?,?,?)",
                       (employer_key, name, int(current["prefill"]), int(current["auto_submit"]), current.get("terms_reviewed_at"), json.dumps(values), current.get("notes", ""), utcnow()))
        self.audit(actor, "attestation_approved", details={"employer": employer_key, "hash": text_hash})

    # ---- board health ------------------------------------------------------
    def board_status(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM board_status ORDER BY name")]

    def board_get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM board_status WHERE employer_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def board_update(self, key, name, **values):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO board_status(employer_key,name) VALUES (?,?)", (key, name))
            if values:
                db.execute("UPDATE board_status SET " + ",".join(f"{k}=?" for k in values) + " WHERE employer_key=?", (*values.values(), key))

    # ---- answer bank -------------------------------------------------------
    def answers(self, verified_only=False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM answer_bank" + (" WHERE verified_at IS NOT NULL" if verified_only else "") + " ORDER BY question").fetchall()
        return [dict(r) for r in rows]

    def save_answer(self, question, answer, *, verified, source="user", kind="text", answer_id=None, actor="user"):
        question, answer = question.strip(), answer.strip()
        if not question or not answer:
            raise ValueError("Question and answer are both required.")
        answer_id = answer_id or uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO answer_bank VALUES (?,?,?,?,?,?,COALESCE((SELECT created_at FROM answer_bank WHERE id=?),?))",
                       (answer_id, question, answer, kind, utcnow() if verified else None, source, answer_id, utcnow()))
        self.audit(actor, "answer_saved", details={"id": answer_id, "verified": verified, "source": source})
        return answer_id

    def delete_answer(self, answer_id, actor="user"):
        with self.connect() as db:
            db.execute("DELETE FROM answer_bank WHERE id=?", (answer_id,))
        self.audit(actor, "answer_deleted", details={"id": answer_id})

    # ---- applications ------------------------------------------------------
    def application(self, app_id=None, job_key=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM applications WHERE " + ("id=?" if app_id else "job_key=?"), (app_id or job_key,)).fetchone()
        return _decode(row) if row else None

    def applications(self, states=None, limit=1000):
        query, args = "SELECT * FROM applications", []
        if states:
            query += " WHERE state IN (" + ",".join("?" * len(states)) + ")"
            args = list(states)
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY updated_at DESC LIMIT ?", (*args, limit)).fetchall()
        return [_decode(r) for r in rows]

    def enqueue(self, job_key, job, employer_key, dedupe_key, mode, gates):
        """Idempotent: one application row per job, ever."""
        now = utcnow()
        app_id = uuid.uuid4().hex[:16]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM applications WHERE job_key=?", (job_key,)).fetchone():
                return None
            duplicate = db.execute("SELECT id,state FROM applications WHERE dedupe_key=? AND state NOT IN (?,?,?)", (dedupe_key, SKIPPED, CANCELLED, BLOCKED)).fetchone()
            state, reasons = QUEUED, []
            if duplicate:
                state, reasons = SKIPPED, [f"Duplicate of application {duplicate['id']} ({duplicate['state']}) for the same employer and title."]
            db.execute("INSERT INTO applications(id,job_key,company,title,dedupe_key,employer_key,state,mode,created_at,updated_at,gates,reasons) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (app_id, job_key, job.company, job.title, dedupe_key, employer_key, state, mode, now, now, json.dumps(gates, ensure_ascii=False), json.dumps(reasons)))
        self.audit("worker", "enqueued" if state == QUEUED else "skipped_duplicate", job_key, app_id, {"mode": mode, "reasons": reasons})
        return app_id

    def update(self, app_id, actor="worker", action=None, expected=None, **values):
        """Compare-and-set on ``state`` when ``expected`` is given."""
        values["updated_at"] = utcnow()
        for key in ("gates", "plan", "confirmation"):
            if key in values and not isinstance(values[key], str):
                values[key] = json.dumps(values[key], ensure_ascii=False, default=str)
        if "reasons" in values and not isinstance(values["reasons"], str):
            values["reasons"] = json.dumps(list(values["reasons"]), ensure_ascii=False)
        query = "UPDATE applications SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?"
        args = [*values.values(), app_id]
        if expected:
            expected = (expected,) if isinstance(expected, str) else tuple(expected)
            query += " AND state IN (" + ",".join("?" * len(expected)) + ")"
            args += list(expected)
        with self.connect() as db:
            changed = db.execute(query, args).rowcount
        if changed and action:
            app = self.application(app_id)
            self.audit(actor, action, app["job_key"], app_id, {k: v for k, v in values.items() if k in {"state", "last_error", "reasons", "apply_url"}})
        return bool(changed)

    def acquire(self, app_id, states, minutes=20):
        """Short processing lease so two workers never touch one application."""
        token, now = uuid.uuid4().hex, datetime.now(timezone.utc)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,lock_expires FROM applications WHERE id=?", (app_id,)).fetchone()
            if not row or row["state"] not in states or (row["lock_expires"] and datetime.fromisoformat(row["lock_expires"]) > now):
                return None
            db.execute("UPDATE applications SET lock_token=?,lock_expires=?,attempts=attempts+1 WHERE id=?", (token, (now + timedelta(minutes=minutes)).isoformat(), app_id))
        return token

    def release(self, app_id, token):
        with self.connect() as db:
            db.execute("UPDATE applications SET lock_token=NULL,lock_expires=NULL WHERE id=? AND lock_token=?", (app_id, token))

    def begin_submission(self, app_id, token):
        """Atomically claim the one-and-only submit attempt for this job.

        Written *before* the click. If the process dies afterwards, recovery
        turns the row into Unknown and nothing ever re-clicks automatically.
        """
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            app = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
            if not app or app["lock_token"] != token or app["state"] not in (APPROVED, PREPARING):
                return False
            if db.execute("SELECT 1 FROM submission_intents WHERE job_key=?", (app["job_key"],)).fetchone():
                return False
            other = db.execute("SELECT 1 FROM applications WHERE dedupe_key=? AND id<>? AND state IN (?,?,?)", (app["dedupe_key"], app_id, *SUBMISSION_STATES)).fetchone()
            if other:
                return False
            db.execute("INSERT INTO submission_intents(job_key,application_id,token,started_at) VALUES (?,?,?,?)", (app["job_key"], app_id, token, now))
            db.execute("UPDATE applications SET state=?,submit_started_at=?,updated_at=? WHERE id=?", (SUBMITTING, now, now, app_id))
        self.audit("worker", "submission_started", app["job_key"], app_id, {})
        return True

    def finish_submission(self, app_id, outcome):
        with self.connect() as db:
            db.execute("UPDATE submission_intents SET outcome=?,finished_at=? WHERE application_id=?", (outcome, utcnow(), app_id))

    def submissions_since(self, since: datetime, employer_key=None, company=None):
        query = "SELECT COUNT(*) FROM applications WHERE submit_started_at >= ? AND state IN (?,?,?)"
        args = [since.isoformat(), *SUBMISSION_STATES]
        if employer_key:
            query += " AND employer_key=?"
            args.append(employer_key)
        if company:
            query += " AND lower(company)=lower(?)"
            args.append(company)
        with self.connect() as db:
            return db.execute(query, args).fetchone()[0]

    def recover(self, actor="worker"):
        """Crash recovery. Run at worker start, before any new work."""
        changed = []
        now = datetime.now(timezone.utc)
        with self.connect() as db:
            for row in db.execute("SELECT id,job_key FROM applications WHERE state=?", (SUBMITTING,)).fetchall():
                db.execute("UPDATE applications SET state=?,last_error=?,lock_token=NULL,lock_expires=NULL,updated_at=? WHERE id=?",
                           (UNKNOWN, "Worker stopped after the submit step began. Verify on the employer site before any retry.", utcnow(), row["id"]))
                db.execute("UPDATE submission_intents SET outcome=COALESCE(outcome,'unknown-after-restart') WHERE application_id=?", (row["id"],))
                changed.append((row["id"], row["job_key"], UNKNOWN))
            for row in db.execute("SELECT id,job_key,lock_expires FROM applications WHERE state=? ", (PREPARING,)).fetchall():
                # Preparing never clicks submit, so it is safe to requeue.
                db.execute("UPDATE applications SET state=?,lock_token=NULL,lock_expires=NULL,updated_at=? WHERE id=?", (QUEUED, utcnow(), row["id"]))
                changed.append((row["id"], row["job_key"], QUEUED))
            db.execute("UPDATE applications SET lock_token=NULL,lock_expires=NULL WHERE lock_expires IS NOT NULL AND lock_expires < ?", (now.isoformat(),))
        for app_id, job_key, state in changed:
            self.audit(actor, "recovered_after_restart", job_key, app_id, {"state": state})
        return changed

    # ---- LLM usage ---------------------------------------------------------
    def llm_spent(self, since: datetime):
        with self.connect() as db:
            row = db.execute("SELECT COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_usd ELSE cost_usd END),0) FROM llm_usage WHERE at >= ?", (since.isoformat(),)).fetchone()
        return float(row[0])

    def llm_usage(self, limit=500):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM llm_usage ORDER BY id DESC LIMIT ?", (limit,))]


def _decode(row):
    value = dict(row)
    for key in ("gates", "plan", "confirmation"):
        value[key] = json.loads(value[key] or "{}")
    value["reasons"] = json.loads(value["reasons"] or "[]")
    return value
