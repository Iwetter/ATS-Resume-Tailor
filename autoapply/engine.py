"""Evaluation, queueing, preparation and guarded submission.

Submission invariants (all enforced here, re-checked immediately before the
click):

* emergency stop not set; master switch on; mode not OFF;
* ``AUTOAPPLY_ALLOW_REAL_SUBMISSION`` true;
* AUTO path: mode AUTO enabled after a passing dry run for the current
  verified profile, every gate ``pass``, employer auto-submit permitted,
  every answer from a verified source;
* REVIEW path: human approval whose hash still matches the resume, the
  form's field set and every answer source;
* daily, per-company-daily and per-company-total caps not exceeded;
* an atomic, unique submission intent recorded *before* clicking.

After the click, only explicit confirmation evidence produces Applied.
Everything else is Unknown and is never retried automatically.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import shutil

from ats.models import digest
from hunter.filtering import decide
from hunter.models import Filters, Job, Profile, utcnow
from hunter.service import score_job

from . import db as S
from .browser import BrowserFactory, adapter_for, run_application
from .db import AutoStore
from .forms import answers_hash, missing_required, norm, resolve
from .gates import TARGET_TERMS, evaluate_gates
from .profile import hunter_profile, load_profile
from .redaction import log

logger = logging.getLogger("autoapply.engine")
MAX_PREP_ATTEMPTS = 3


def write_cover_letter(text, applicant, path):
    """Plain one-page PDF of a human-approved cover letter."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape
    styles = getSampleStyleSheet()
    flow = [Paragraph(escape(applicant.full_name), styles["Title"]), Spacer(1, 8)]
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        flow += [Paragraph(escape(para).replace("\n", "<br/>"), styles["BodyText"]), Spacer(1, 8)]
    SimpleDocTemplate(str(path), pagesize=letter, title="Cover Letter", author="").build(flow)
    return path


def employer_key(job: Job):
    return f"{job.source}:{job.board}"


def dedupe_key(job: Job):
    return digest([norm(job.company), norm(re.sub(r"\(.*?\)|-\s*(spring|summer|fall)\s*20\d\d", "", job.title, flags=re.I))])[:24]


class Engine:
    def __init__(self, store, settings, browser=None, claude=None):
        from .llm import Claude
        self.store, self.settings = store, settings
        self.auto = AutoStore(store)
        self.browser = browser or BrowserFactory(settings)
        self.claude = claude if claude is not None else Claude(self.auto, settings)

    # ---- state helpers -------------------------------------------------------
    def stopped(self):
        return self.auto.emergency_stopped(self.settings)

    def auto_active(self, profile=None):
        if self.auto.mode() != "AUTO":
            return False
        enabled = self.store.get("auto_enabled")
        dry = self.store.get("dryrun_validation")
        profile = profile or load_profile(self.store)
        master = self.store.master()
        return bool(enabled and dry and dry.get("ok") and master and profile.is_verified(master.source_hash)
                    and enabled.get("profile_hash") == profile.content_hash == dry.get("profile_hash")
                    and enabled.get("master") == master.source_hash)

    def _job(self, job_key):
        record = self.store.job(job_key)
        if not record:
            return None, None
        job = replace(Job(**json.loads(record["payload"])), source_status=record["availability"], checked_at=record["checked_at"])
        return record, job

    def _context(self):
        applicant = load_profile(self.store)
        base = Profile(**self.store.get("profile", asdict(Profile())))
        return self.store.master(), applicant, hunter_profile(applicant, base)

    # ---- evaluation ------------------------------------------------------------
    def evaluate(self, job_key, trigger="scan", enqueue=True):
        record, job = self._job(job_key)
        if job is None:
            return None
        master, applicant, hunter_prof = self._context()
        decision = decide(job, hunter_prof, Filters(self.settings.days, TARGET_TERMS), utcnow())
        match = score_job(job, master, self.store) if decision.eligible and master is not None and job.description else None
        gate = evaluate_gates(job, decision, match, applicant, master_hash=master.source_hash if master else None,
                              permission=self.auto.permission(employer_key(job)), settings=self.settings)
        latest = self.auto.decisions(job_key, limit=1)
        if not latest or latest[0]["details"] != json.loads(json.dumps(gate.as_dict())):
            self.auto.record_decision(job_key, trigger, gate)
        if enqueue and gate.prepare_ok and self.auto.enabled() and self.auto.mode() != "OFF" and not self.stopped():
            app_id = self.auto.enqueue(job_key, job, employer_key(job), dedupe_key(job), self.auto.mode(), gate.as_dict())
            if app_id:
                log(logger, logging.INFO, "application queued", app=app_id, job=job_key, company=job.company)
        return gate

    def on_scanned_job(self, key, job, fresh):
        """Hunter scan callback: evaluate new and re-observed jobs immediately."""
        try:
            self.evaluate(key, "new_job" if fresh else "rescan")
        except Exception as exc:  # never break a scan because of evaluation
            log(logger, logging.ERROR, "evaluation failed", job=key, error=str(exc)[:300])

    # ---- queue -------------------------------------------------------------------
    def process(self, limit=3):
        if self.stopped() or not self.auto.enabled() or self.auto.mode() == "OFF":
            return []
        self._requeue_failed()
        done = []
        for app in self.auto.applications([S.APPROVED])[:limit]:
            if self.stopped():
                break
            done.append(self.submit_approved(app["id"]))
        for app in sorted(self.auto.applications([S.QUEUED]), key=lambda a: a["created_at"])[:max(0, limit - len(done))]:
            if self.stopped():
                break
            done.append(self.prepare(app["id"]))
        return done

    def _requeue_failed(self):
        now = datetime.now(timezone.utc)
        for app in self.auto.applications([S.FAILED]):
            if app["attempts"] < MAX_PREP_ATTEMPTS and datetime.fromisoformat(app["updated_at"]) < now - timedelta(minutes=10 * 2 ** app["attempts"]):
                self.auto.update(app["id"], action="retry_scheduled", expected=S.FAILED, state=S.QUEUED)

    # ---- preparation ---------------------------------------------------------------
    def prepare(self, app_id):
        token = self.auto.acquire(app_id, (S.QUEUED,))
        if not token:
            return app_id, "locked"
        claimed = {"value": False}
        try:
            app = self.auto.application(app_id)
            record, job = self._job(app["job_key"])
            gate = self.evaluate(app["job_key"], "prepare", enqueue=False)
            if gate is None or not gate.prepare_ok:
                self.auto.update(app_id, action="blocked", state=S.BLOCKED, gates=gate.as_dict() if gate else {},
                                 reasons=[gate.summary if gate else "Job no longer exists."])
                return app_id, S.BLOCKED
            self.auto.update(app_id, action="preparing", expected=S.QUEUED, state=S.PREPARING, gates=gate.as_dict())
            master, applicant, _ = self._context()
            plan = self._plan(app, job, master)
            adapter, url = adapter_for(job)
            if adapter is None:
                self.auto.update(app_id, action="needs_human", state=S.NEEDS_HUMAN, plan=plan,
                                 reasons=["No supported candidate-form adapter for this job (Greenhouse and Lever are supported). Apply manually."])
                return app_id, S.NEEDS_HUMAN
            self.auto.update(app_id, adapter=adapter.name, apply_url=url, plan=plan)
            auto_path = self.auto.mode() == "AUTO" and self.auto_active(applicant) and gate.auto_ok
            refusal = []
            before = self._before_submit(app_id, token, gate, refusal, claimed, approval_hash=None) if auto_path else None
            report = self._run(app_id, adapter, url, plan, applicant, before)
            return app_id, self._settle(app_id, job, plan, report, refusal, auto_path)
        except Exception as exc:
            return app_id, self._failure(app_id, exc, claimed["value"])
        finally:
            self.auto.release(app_id, token)

    def _plan(self, app, job, master):
        from .tailoring import tailor_for_job
        plan = app.get("plan") or {}
        artifact = next((a for a in self.store.artifacts() if a["key"] == plan.get("artifact_key")), None)
        if artifact and plan.get("source_hash") == master.source_hash and Path(artifact["pdf"]).is_file():
            return plan
        new = tailor_for_job(self.store, app["job_key"], job, master, self.settings, self.claude,
                             approved_change_ids=plan.get("approved_change_ids", []), previous_plan=plan if plan.get("source_hash") == master.source_hash else None)
        new.update(source_hash=master.source_hash, cover_letter=plan.get("cover_letter"))
        self.auto.audit("worker", "resume_tailored", app["job_key"], app["id"], {"artifact": new["artifact_key"], "projects": new["projects"], "fit_before": new["fit_before"], "fit_after": new["fit_after"]})
        return new

    def _evidence_dir(self, app_id):
        app = self.auto.application(app_id)
        return self.settings.data_dir / "applications" / app_id / f"attempt-{app['attempts']}"

    def _run(self, app_id, adapter, url, plan, applicant, before_submit):
        evidence = self._evidence_dir(app_id)
        evidence.mkdir(parents=True, exist_ok=True)
        resume = evidence / (re.sub(r"[^A-Za-z0-9]+", "_", applicant.full_name).strip("_") + "_Resume.pdf")
        shutil.copyfile(plan["pdf"], resume)
        cover = None
        letter = plan.get("cover_letter") or {}
        if letter.get("approved") and letter.get("text"):
            cover = {"text": letter["text"], "path": str(write_cover_letter(letter["text"], applicant, evidence / (resume.stem.replace("_Resume", "") + "_Cover_Letter.pdf")))}
        app = self.auto.application(app_id)
        permission = self.auto.permission(app["employer_key"])
        answers = self.auto.answers(verified_only=True)
        mappings = (app.get("plan") or {}).get("model_mappings")

        def resolver(fields):
            resolutions = resolve(fields, applicant, answers, resume_path=str(resume), cover_letter=cover,
                                  approved_attestations=permission["approved_attestations"], model_mappings=mappings)
            unresolved = [r for r in resolutions if r.required and not r.filled and r.concept == "unknown"]
            if unresolved and mappings is None and self.claude.configured()[0]:
                try:
                    profile_fields = sorted(k for k, filled in applicant.as_public_dict().items() if filled)
                    found, _ = self.claude.map_questions([{"id": r.fid, "label": r.label, "kind": r.kind, "options": next(f.options for f in fields if f.fid == r.fid)} for r in unresolved],
                                                         profile_fields, [a["id"] for a in answers])
                    plan_now = self.auto.application(app_id)["plan"]
                    plan_now["model_mappings"] = found
                    self.auto.update(app_id, plan=plan_now)
                    resolutions = resolve(fields, applicant, answers, resume_path=str(resume), cover_letter=cover,
                                          approved_attestations=permission["approved_attestations"], model_mappings=found)
                except Exception as exc:
                    log(logger, logging.WARNING, "question mapping unavailable", app=app_id, error=str(exc)[:200])
            return resolutions

        with self.browser.page() as page:
            return run_application(page, adapter, url, resolver, evidence, before_submit)

    def _approval_hash(self, plan, report):
        return digest([plan.get("artifact_key"), report.field_set_hash, answers_hash(report.resolutions), (plan.get("cover_letter") or {}).get("text")])

    def _caps(self, app):
        now = datetime.now(timezone.utc)
        day = now - timedelta(days=1)
        reasons = []
        if self.auto.submissions_since(day) >= self.settings.max_per_day:
            reasons.append(f"Daily submission cap ({self.settings.max_per_day}) reached.")
        if self.auto.submissions_since(day, company=app["company"]) >= self.settings.max_per_company_per_day:
            reasons.append(f"Per-company daily cap ({self.settings.max_per_company_per_day}) reached for {app['company']}.")
        if self.auto.submissions_since(datetime(1970, 1, 1, tzinfo=timezone.utc), company=app["company"]) >= self.settings.max_per_company_total:
            reasons.append(f"Per-company total cap ({self.settings.max_per_company_total}) reached for {app['company']}.")
        return reasons

    def _before_submit(self, app_id, token, gate, refusal, claimed, approval_hash):
        def check(report):
            app = self.auto.application(app_id)
            reasons = []
            if self.stopped():
                reasons.append("Emergency stop is active.")
            if not self.auto.enabled() or self.auto.mode() == "OFF":
                reasons.append("Auto Apply is switched off.")
            if not self.settings.allow_real_submission:
                reasons.append("Real submission is disabled (AUTOAPPLY_ALLOW_REAL_SUBMISSION=false). Form was prepared only.")
            missing = missing_required(report.resolutions)
            if missing:
                reasons.append("Required fields unanswered: " + "; ".join(r.label[:80] for r in missing))
            if approval_hash is None:
                if gate is None or not gate.auto_ok:
                    reasons.append("Not every hard gate passed; approval required.")
                if not self.auto_active():
                    reasons.append("AUTO mode is not active for the current verified profile.")
                untrusted = [r for r in report.resolutions if r.source == "model_mapping"] if not self.settings.trust_model_mappings else []
                if untrusted:
                    reasons.append("Some answers were mapped by Claude and need your review: " + "; ".join(r.label[:60] for r in untrusted))
            elif self._approval_hash(app["plan"], report) != approval_hash:
                reasons.append("The form, resume or answers changed since approval; review again.")
            reasons += self._caps(app)
            if reasons:
                refusal.extend(reasons)
                return False
            if not self.auto.begin_submission(app_id, token):
                refusal.append("Another submission for this job (or a duplicate posting) was already started.")
                return False
            claimed["value"] = True
            return True
        return check

    def _settle(self, app_id, job, plan, report, refusal, auto_path):
        app = self.auto.application(app_id)
        details = {"url": report.url, "screenshot": report.screenshot, "fields": [r.public() for r in report.resolutions],
                   "blockers": [asdict(b) for b in report.blockers], "fill_errors": report.fill_errors}
        plan = {**app["plan"], "last_run": details}
        if report.submitted:
            self.auto.finish_submission(app_id, report.outcome)
            confirmation = {**report.confirmation, "at": utcnow()}
            if report.outcome == "confirmed":
                self.auto.update(app_id, action="applied_confirmed", state=S.APPLIED, submitted_at=utcnow(), confirmation=confirmation, plan=plan,
                                 evidence_dir=str(Path(report.screenshot).parent) if report.screenshot else None, reasons=[])
                self.store.status(app["job_key"], "Applied", f"Auto Apply confirmed submission {utcnow()} ({confirmation.get('matched_text') or confirmation.get('url')}). Application {app_id}.")
                return S.APPLIED
            self.auto.update(app_id, action="submission_unknown", state=S.UNKNOWN, confirmation=confirmation, plan=plan,
                             reasons=["Submit was clicked but no confirmation was observed. Check your email/the employer portal before any retry."])
            return S.UNKNOWN
        if report.blockers:
            self.auto.update(app_id, action="needs_human", state=S.NEEDS_HUMAN, plan=plan,
                             reasons=[f"{b.kind}: {b.detail}" for b in report.blockers])
            return S.NEEDS_HUMAN
        missing = missing_required(report.resolutions)
        if report.fill_errors or missing:
            self.auto.update(app_id, action="needs_human", state=S.NEEDS_HUMAN, plan=plan,
                             reasons=[f"Unanswered required question: {r.label[:120]} ({r.reason or 'no verified answer'})" for r in missing] + report.fill_errors)
            return S.NEEDS_HUMAN
        approval = self._approval_hash(plan, report)
        reasons = refusal or (["Ready for your approval (REVIEW mode)."] if not auto_path else [])
        self.auto.update(app_id, action="ready_for_review", state=S.READY, plan=plan, approval_hash=approval, reasons=reasons)
        return S.READY

    def _failure(self, app_id, exc, claimed):
        message = f"{type(exc).__name__}: {str(exc)[:400]}"
        if claimed:
            # The submit claim exists: outcome is uncertain, never retried.
            self.auto.finish_submission(app_id, "error-after-claim")
            self.auto.update(app_id, action="submission_unknown", state=S.UNKNOWN, last_error=message,
                             reasons=["An error occurred after the submission step began; verify on the employer site."])
            return S.UNKNOWN
        self.auto.update(app_id, action="preparation_failed", state=S.FAILED, last_error=message, reasons=["Preparation failed; it will be retried with backoff."])
        log(logger, logging.ERROR, "preparation failed", app=app_id, error=message)
        return S.FAILED

    # ---- approved (REVIEW) submissions ------------------------------------------
    def submit_approved(self, app_id):
        token = self.auto.acquire(app_id, (S.APPROVED,))
        if not token:
            return app_id, "locked"
        claimed = {"value": False}
        try:
            app = self.auto.application(app_id)
            record, job = self._job(app["job_key"])
            gate = self.evaluate(app["job_key"], "pre_submit", enqueue=False)
            if gate is None or not gate.prepare_ok:
                self.auto.update(app_id, action="blocked", state=S.BLOCKED, reasons=[gate.summary if gate else "Job no longer exists."])
                return app_id, S.BLOCKED
            master, applicant, _ = self._context()
            if app["plan"].get("source_hash") != master.source_hash:
                self.auto.update(app_id, action="approval_invalidated", state=S.QUEUED, approval_hash=None, reasons=["Master resume changed after approval."])
                return app_id, S.QUEUED
            adapter, url = adapter_for(job)
            refusal = []
            before = self._before_submit(app_id, token, gate, refusal, claimed, approval_hash=app["approval_hash"])
            report = self._run(app_id, adapter, url, app["plan"], applicant, before)
            if not report.submitted and not report.blockers and not missing_required(report.resolutions) and not report.fill_errors:
                # Refused at the final gate: return to review with the reasons.
                self.auto.update(app_id, action="submission_refused", state=S.READY, reasons=refusal,
                                 approval_hash=self._approval_hash(app["plan"], report))
                return app_id, S.READY
            return app_id, self._settle(app_id, job, app["plan"], report, refusal, False)
        except Exception as exc:
            return app_id, self._failure(app_id, exc, claimed["value"])
        finally:
            self.auto.release(app_id, token)

    # ---- human actions -----------------------------------------------------------
    def approve(self, app_id, approval_hash, actor="user"):
        app = self.auto.application(app_id)
        if not app or app["state"] != S.READY or app["approval_hash"] != approval_hash:
            raise ValueError("Only the exact Ready for Review version shown can be approved.")
        self.auto.update(app_id, actor=actor, action="approved", expected=S.READY, state=S.APPROVED, approved_at=utcnow())

    def requeue(self, app_id, actor="user", reason="Re-prepare requested"):
        app = self.auto.application(app_id)
        if app["state"] in (S.SUBMITTING, S.APPLIED):
            raise ValueError("A submitted application cannot be re-prepared.")
        if app["state"] == S.UNKNOWN:
            raise ValueError("Resolve the Unknown outcome first (mark Applied or Not submitted).")
        self.auto.update(app_id, actor=actor, action="requeued", state=S.QUEUED, approval_hash=None, reasons=[reason], attempts=0)

    def resolve_unknown(self, app_id, submitted: bool, note, actor="user"):
        """Human investigation result for an Unknown outcome."""
        app = self.auto.application(app_id)
        if app["state"] != S.UNKNOWN:
            raise ValueError("Only Unknown outcomes can be resolved here.")
        if not note.strip():
            raise ValueError("Describe the evidence you checked (confirmation email, portal status…).")
        if submitted:
            self.auto.update(app_id, actor=actor, action="unknown_resolved_applied", state=S.APPLIED,
                             confirmation={**app["confirmation"], "human_verification": note, "at": utcnow()})
            self.store.status(app["job_key"], "Applied", "Human-verified after uncertain automated submission: " + note)
        else:
            with self.auto.connect() as db:
                db.execute("DELETE FROM submission_intents WHERE application_id=?", (app_id,))
            self.auto.update(app_id, actor=actor, action="unknown_resolved_not_submitted", state=S.QUEUED,
                             approval_hash=None, attempts=0, reasons=["Human verified it was not submitted: " + note + ". It will be re-prepared for a fresh approval."])

    def cancel(self, app_id, actor="user"):
        app = self.auto.application(app_id)
        if app["state"] in (S.SUBMITTING, S.APPLIED, S.UNKNOWN):
            raise ValueError("This application can no longer be cancelled.")
        self.auto.update(app_id, actor=actor, action="cancelled", state=S.CANCELLED)
