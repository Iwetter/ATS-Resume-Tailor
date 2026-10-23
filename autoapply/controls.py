"""Permission modes and persisted operator preferences.

The three permission modes are names for the existing state machine, which is
unchanged:

* **Full Autopilot** = ``AUTO``. Entered only through ``modes.enable_auto``
  (passing dry run + typed confirmation). Each application still needs every
  hard gate to pass, employer auto-submit permission, a verified profile and a
  completely answered form; anything else falls back to approval.
* **Approval Required** = ``REVIEW``. Applications are prepared and wait for a
  human approval of that exact version (dashboard, phone link or SMS).
* **Paused** = ``OFF``. Discovery, evaluation and alerts continue; nothing is
  prepared or submitted.

Pausing remembers the mode (and AUTO authorisation) it replaced, so RESUME can
restore it after re-running the checks ``enable_auto`` performs. Preferences
(fit thresholds, submission caps) override environment defaults within the
same hard bounds; real-submission and emergency-stop settings are never
changeable here.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from hunter.models import utcnow

from . import db as S
from .modes import DRY_RUN_MAX_AGE, enable_auto
from .profile import load_profile

AUTOPILOT, APPROVAL, PAUSED = "Full Autopilot", "Approval Required", "Paused"
PERMISSION_MODES = (AUTOPILOT, APPROVAL, PAUSED)
MODE_FOR = {AUTOPILOT: "AUTO", APPROVAL: "REVIEW", PAUSED: "OFF"}
NAME_FOR = {mode: name for name, mode in MODE_FOR.items()}

# Same bounds config.load() applies to the environment values.
PREF_BOUNDS = {"min_fit_review": (0.0, 100.0), "min_fit_auto": (0.0, 100.0), "max_per_day": (0, 50), "max_per_company_per_day": (0, 10)}


# ---- preferences --------------------------------------------------------------

def preferences(store) -> dict:
    saved = store.get("autoapply_preferences") or {}
    return {k: saved.get(k) for k in PREF_BOUNDS}


def save_preferences(store, values: dict, audit=None, actor="user") -> dict:
    """Validate and persist. ``None`` for a key restores the environment default."""
    clean = preferences(store)
    for key, value in values.items():
        if key not in PREF_BOUNDS:
            raise ValueError(f"Unknown preference {key}.")
        if value is None:
            clean[key] = None
            continue
        low, high = PREF_BOUNDS[key]
        cast = type(low)
        try:
            value = cast(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key.replace('_', ' ')} must be a number.") from None
        if not low <= value <= high:
            raise ValueError(f"{key.replace('_', ' ')} must be between {low:g} and {high:g}.")
        clean[key] = value
    review, auto = clean.get("min_fit_review"), clean.get("min_fit_auto")
    if review is not None and auto is not None and review > auto:
        raise ValueError("The minimum fit to prepare cannot be above the Full Autopilot minimum.")
    store.set("autoapply_preferences", clean)
    if audit:
        audit(actor, "preferences_saved", details={k: v for k, v in clean.items()})
    return clean


def effective(settings, store):
    overrides = {k: v for k, v in preferences(store).items() if v is not None}
    if not overrides:
        return settings
    merged = replace(settings, **overrides)
    if merged.min_fit_review > merged.min_fit_auto:  # one side overridden past the other's env value
        merged = replace(merged, min_fit_review=merged.min_fit_auto)
    return merged


# ---- permission modes -----------------------------------------------------------

def permission_mode(engine) -> str:
    return NAME_FOR[engine.auto.mode()]


def describe_mode(engine) -> str:
    name = permission_mode(engine)
    if name == AUTOPILOT and not engine.auto_active():
        return name + " (inactive: approval required until re-enabled)"
    return name


def pause(engine, actor="user", reason="") -> str:
    store, auto = engine.store, engine.auto
    current = auto.mode()
    if current != "OFF":
        store.set("paused_from", {"mode": current, "auto_enabled": store.get("auto_enabled"), "at": utcnow(), "actor": actor})
    auto.set_mode("OFF", actor=actor)
    auto.audit(actor, "paused", details={"previous": current, "reason": reason})
    previous = NAME_FOR[(store.get("paused_from") or {}).get("mode", "REVIEW")]
    return f"Paused. Monitoring continues; nothing will be prepared or submitted. RESUME restores {previous}."


def _auto_restorable(engine, enabled) -> str | None:
    """None when the remembered AUTO authorisation still passes enable_auto's checks."""
    store = engine.store
    dry = store.get("dryrun_validation")
    master = store.master()
    profile = load_profile(store)
    if not enabled:
        return "no Full Autopilot authorisation was recorded"
    if not dry or not dry.get("ok"):
        return "the dry-run validation has not passed"
    if datetime.now(timezone.utc) - datetime.fromisoformat(dry["at"]) > DRY_RUN_MAX_AGE:
        return "the dry run is older than 7 days"
    if not master or not profile.is_verified(master.source_hash):
        return "the applicant profile is not verified against the master resume"
    if not (enabled.get("profile_hash") == profile.content_hash == dry.get("profile_hash") and enabled.get("master") == master.source_hash == dry.get("master")):
        return "the profile or master resume changed since it was enabled"
    return None


def resume(engine, actor="user") -> tuple[str, str]:
    """Restore the mode in force before PAUSE. Returns (mode name, message)."""
    store, auto = engine.store, engine.auto
    if engine.stopped():
        raise ValueError("Emergency stop is active; clear it on the dashboard or CLI first.")
    if auto.mode() != "OFF":
        return describe_mode(engine), f"Not paused. Current mode: {describe_mode(engine)}."
    previous = store.get("paused_from") or {"mode": "REVIEW"}
    target, note = previous.get("mode", "REVIEW"), ""
    if target == "AUTO":
        problem = _auto_restorable(engine, previous.get("auto_enabled"))
        if problem is None:
            store.set("autoapply_mode", "AUTO")
            store.set("auto_enabled", previous["auto_enabled"])
        else:
            target, note = "REVIEW", f" Full Autopilot could not resume because {problem}; re-enable it on the dashboard."
    if target != "AUTO":
        auto.set_mode("REVIEW", actor=actor)
    store.set("paused_from", None)
    auto.audit(actor, "resumed", details={"mode": target, "note": note.strip()})
    switch = "" if auto.enabled() else " The master switch is off, so nothing will be prepared until it is turned on."
    return NAME_FOR[target], f"Resumed: {NAME_FOR[target]}.{note}{switch}"


def set_permission_mode(engine, name, actor="user", confirmation="") -> str:
    """Dashboard entry point. Full Autopilot keeps its dry run + typed confirmation."""
    if name not in PERMISSION_MODES:
        raise ValueError("Unknown permission mode.")
    if name == PAUSED:
        return pause(engine, actor)
    if name == APPROVAL:
        engine.auto.set_mode("REVIEW", actor=actor)
        engine.store.set("paused_from", None)
        return "Approval Required: each prepared application waits for your approval."
    enable_auto(engine, confirmation, actor=actor)
    engine.store.set("paused_from", None)
    return "Full Autopilot enabled for qualifying applications."


def autopilot_readiness(engine) -> list[dict]:
    """Everything Full Autopilot needs, as a checklist for the dashboard."""
    store, auto, settings = engine.store, engine.auto, engine.settings
    master, profile = store.master(), load_profile(store)
    dry = store.get("dryrun_validation") or {}
    fresh = bool(dry.get("ok")) and datetime.now(timezone.utc) - datetime.fromisoformat(dry["at"]) <= DRY_RUN_MAX_AGE
    with auto.connect() as db:
        employers = db.execute("SELECT COUNT(*) FROM employer_permissions WHERE auto_submit=1 AND prefill=1 AND terms_reviewed_at IS NOT NULL").fetchone()[0]
    rows = [
        ("Applicant profile verified", bool(master and profile.is_verified(master.source_hash)), "Auto Apply → Applicant profile"),
        ("Dry-run validation passed in the last 7 days", fresh, "Control → Run dry-run validation"),
        ("At least one employer and site approved for automatic submission", employers > 0, f"{employers} approved (Employers tab)"),
        ("Emergency stop clear", not engine.stopped(), "Control → Clear emergency stop"),
        ("Master switch on", auto.enabled(), "Control → master switch"),
        ("Real submission allowed on the server", settings.allow_real_submission, "AUTOAPPLY_ALLOW_REAL_SUBMISSION=true (otherwise forms are prepared only)"),
    ]
    return [{"check": name, "ok": bool(ok), "how": how} for name, ok, how in rows]


# ---- status ------------------------------------------------------------------------

def activity(engine, since: datetime) -> dict:
    """Counts used by STATUS and the daily summary."""
    with engine.auto.connect() as db:
        new_matches = db.execute("SELECT COUNT(DISTINCT job_key) FROM decisions WHERE trigger='new_job' AND at >= ? AND fit >= ?",
                                 (since.isoformat(), engine.settings.min_fit_auto)).fetchone()[0]
        changed = {row[0]: row[1] for row in db.execute("SELECT state, COUNT(*) FROM applications WHERE updated_at >= ? GROUP BY state", (since.isoformat(),))}
        current = {row[0]: row[1] for row in db.execute("SELECT state, COUNT(*) FROM applications GROUP BY state")}
    day = datetime.now(timezone.utc) - timedelta(days=1)
    return {"new_matches": new_matches, "changed": changed, "current": current,
            "submitted_24h": engine.auto.submissions_since(day), "cap": engine.settings.max_per_day,
            "awaiting_approval": current.get(S.READY, 0), "needs_action": current.get(S.NEEDS_HUMAN, 0),
            "uncertain": current.get(S.UNKNOWN, 0), "failed": current.get(S.FAILED, 0)}
