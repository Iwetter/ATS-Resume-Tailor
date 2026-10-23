"""Dry-run validation and the separate, explicit AUTO enable action.

The dry run exercises the real pipeline (verified profile, verified master,
one-page export, answer resolution, Playwright filling, blocker detection and
confirmation detection) against fictional mocked forms in a browser whose
every request is intercepted. It cannot reach a real employer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from ats.exporting import export_resume
from hunter.models import utcnow

from .browser import BrowserFactory, GreenhouseAdapter, LeverAdapter, run_application
from .forms import missing_required, resolve
from .profile import load_profile, validate_profile
from .selftest import FixtureRouter, GH_BASE, LEVER_BASE, greenhouse_form

CONFIRM_PHRASE = "ENABLE AUTO"
DRY_RUN_MAX_AGE = timedelta(days=7)


def run_dry_run(engine) -> dict:
    store, settings = engine.store, engine.settings
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    master = store.master()
    profile = load_profile(store)
    check("Verified master resume saved", master is not None)
    errors = [i["message"] for i in validate_profile(profile, master) if i["level"] == "error"]
    check("Applicant profile complete and consistent", not errors, "; ".join(errors))
    check("Applicant profile verified", master is not None and profile.is_verified(master.source_hash))
    check("Submission caps configured", settings.max_per_day > 0 and settings.max_per_company_per_day > 0,
          f"{settings.max_per_day}/day, {settings.max_per_company_per_day}/company/day, {settings.max_per_company_total}/company total")
    check("Emergency stop clear", not engine.stopped())
    if master is not None and profile.is_verified(master.source_hash):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            try:
                bundle = export_resume(master)
                pdf = tmp / "resume.pdf"
                pdf.write_bytes(bundle.pdf)
                check("Master resume exports to a verified one-page PDF", bundle.pages == 1)
            except Exception as exc:
                check("Master resume exports to a verified one-page PDF", False, str(exc)[:200])
                pdf = None
            if pdf:
                answers = engine.auto.answers(verified_only=True)
                resolver = lambda fields: resolve(fields, profile, answers, resume_path=str(pdf))
                for adapter, url, label in ((GreenhouseAdapter(), GH_BASE, "Greenhouse"), (LeverAdapter(), LEVER_BASE, "Lever")):
                    router = FixtureRouter()
                    try:
                        with BrowserFactory(settings, router).page() as page:
                            report = run_application(page, adapter, url, resolver, tmp / label, before_submit=lambda r: True)
                        missing = missing_required(report.resolutions)
                        check(f"{label} mock form: all required fields answered from verified data", not missing and not report.fill_errors,
                              "; ".join([r.label for r in missing] + report.fill_errors))
                        check(f"{label} mock form: confirmation detected", report.outcome == "confirmed" and len(router.posts) == 1)
                        check(f"{label} mock form: no request left the sandbox", not router.blocked, ", ".join(router.blocked[:3]))
                    except Exception as exc:
                        check(f"{label} mock form run", False, f"{type(exc).__name__}: {str(exc)[:200]}")
                for variant, kwargs, expect in (("CAPTCHA", {"captcha": True}, "captcha"), ("Login", {"login": True}, "login")):
                    router = FixtureRouter({GH_BASE: greenhouse_form(**kwargs)})
                    try:
                        with BrowserFactory(settings, router).page() as page:
                            report = run_application(page, GreenhouseAdapter(), GH_BASE, resolver, tmp / variant, before_submit=lambda r: True)
                        check(f"{variant} detected and handed to a human", any(b.kind == expect for b in report.blockers) and not report.submitted and not router.posts)
                    except Exception as exc:
                        check(f"{variant} detection", False, f"{type(exc).__name__}: {str(exc)[:200]}")
                router = FixtureRouter({GH_BASE: greenhouse_form(required_unknown=True)})
                try:
                    with BrowserFactory(settings, router).page() as page:
                        report = run_application(page, GreenhouseAdapter(), GH_BASE, resolver, tmp / "unknown", before_submit=None)
                    check("Unknown required question reported, not invented", any("led a hardware team" in r.label for r in missing_required(report.resolutions)))
                except Exception as exc:
                    check("Unknown required question detection", False, str(exc)[:200])
    ok = all(c["ok"] for c in checks)
    result = {"at": utcnow(), "ok": ok, "checks": checks, "profile_hash": profile.content_hash,
              "master": master.source_hash if master else None}
    store.set("dryrun_validation", result)
    engine.auto.audit("user", "dry_run", details={"ok": ok, "failed": [c["name"] for c in checks if not c["ok"]]})
    return result


def enable_auto(engine, confirmation: str, actor="user"):
    if confirmation.strip() != CONFIRM_PHRASE:
        raise ValueError(f"Type {CONFIRM_PHRASE} to confirm.")
    store = engine.store
    dry = store.get("dryrun_validation")
    master = store.master()
    profile = load_profile(store)
    if not dry or not dry.get("ok"):
        raise ValueError("Run a successful dry-run validation first.")
    if datetime.now(timezone.utc) - datetime.fromisoformat(dry["at"]) > DRY_RUN_MAX_AGE:
        raise ValueError("The last dry run is older than 7 days; run it again.")
    if not master or dry.get("master") != master.source_hash or dry.get("profile_hash") != profile.content_hash or not profile.is_verified(master.source_hash):
        raise ValueError("Profile or master resume changed after the dry run; verify and run it again.")
    if engine.stopped():
        raise ValueError("Clear the emergency stop first.")
    store.set("autoapply_mode", "AUTO")
    store.set("auto_enabled", {"at": utcnow(), "profile_hash": profile.content_hash, "master": master.source_hash, "dry_run_at": dry["at"]})
    engine.auto.audit(actor, "auto_enabled", details={"dry_run_at": dry["at"]})
