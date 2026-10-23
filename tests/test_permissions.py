"""Permission modes over the OFF/REVIEW/AUTO state machine, persisted
preferences, and the dashboard's SMS control panel."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from autoapply import controls
from autoapply import db as S

APP = Path(__file__).resolve().parents[1] / "app.py"


def test_mode_names_map_onto_the_existing_state_machine(aa):
    assert controls.MODE_FOR == {controls.AUTOPILOT: "AUTO", controls.APPROVAL: "REVIEW", controls.PAUSED: "OFF"}
    assert controls.permission_mode(aa.engine) == controls.APPROVAL  # default REVIEW
    controls.set_permission_mode(aa.engine, controls.PAUSED)
    assert aa.auto.mode() == "OFF" and aa.store.get("paused_from")["mode"] == "REVIEW"
    controls.set_permission_mode(aa.engine, controls.APPROVAL)
    assert aa.auto.mode() == "REVIEW" and aa.store.get("paused_from") is None
    with pytest.raises(ValueError):  # Autopilot still needs the dry run + typed confirmation
        controls.set_permission_mode(aa.engine, controls.AUTOPILOT, confirmation="ENABLE AUTO")
    with pytest.raises(ValueError):
        aa.auto.set_mode("AUTO")
    assert aa.auto.mode() == "REVIEW"
    assert any(r["action"] == "paused" for r in aa.auto.audit_log())


def test_pausing_twice_keeps_the_original_mode(aa):
    controls.pause(aa.engine)
    controls.pause(aa.engine)
    assert aa.store.get("paused_from")["mode"] == "REVIEW"
    assert controls.resume(aa.engine)[0] == controls.APPROVAL


def test_resume_without_a_record_is_approval_required(aa):
    aa.auto.set_mode("OFF")
    name, message = controls.resume(aa.engine)
    assert name == controls.APPROVAL and "master switch is off" in message


def test_preferences_validate_persist_and_apply_without_restart(aa):
    with pytest.raises(ValueError):
        controls.save_preferences(aa.store, {"max_per_day": 500})
    with pytest.raises(ValueError):
        controls.save_preferences(aa.store, {"min_fit_review": 80, "min_fit_auto": 70})
    with pytest.raises(ValueError):
        controls.save_preferences(aa.store, {"allow_real_submission": True})  # never overridable
    controls.save_preferences(aa.store, {"min_fit_auto": 99.5, "max_per_day": 2}, aa.auto.audit)
    assert aa.engine.settings.min_fit_auto == 99.5 and aa.engine.settings.max_per_day == 2
    assert aa.engine.settings.allow_real_submission is False and aa.engine.base_settings.min_fit_auto == 30
    key = aa.add_job()
    gate = aa.engine.evaluate(key, enqueue=False)
    fit = next(c for c in gate.checks if c.name == "Fit threshold")
    assert fit.status == "unknown" and "99.5" in fit.detail  # the worker's gates use the saved value
    controls.save_preferences(aa.store, {k: None for k in controls.PREF_BOUNDS})
    assert aa.engine.settings.min_fit_auto == 30
    assert any(r["action"] == "preferences_saved" for r in aa.auto.audit_log())


def test_effective_settings_keep_review_below_auto(aa):
    controls.save_preferences(aa.store, {"min_fit_auto": 10})  # env review threshold is 20
    assert aa.engine.settings.min_fit_review == 10 and aa.engine.settings.min_fit_auto == 10


def test_saved_daily_limit_is_enforced_at_the_final_gate(aa):
    controls.save_preferences(aa.store, {"max_per_day": 0})
    app = {"company": "Fixture Co"}
    assert any("Daily submission cap (0)" in r for r in aa.engine._caps(app))


def test_autopilot_readiness_checklist(aa):
    rows = {r["check"]: r["ok"] for r in controls.autopilot_readiness(aa.engine)}
    assert rows["Applicant profile verified"] and not rows["Dry-run validation passed in the last 7 days"]
    assert not rows["At least one employer and site approved for automatic submission"] and not rows["Real submission allowed on the server"]
    aa.permit(auto_submit=True)
    assert {r["check"]: r["ok"] for r in controls.autopilot_readiness(aa.engine)}["At least one employer and site approved for automatic submission"]


def test_activity_counts(aa):
    aa.switch_on()
    aa.permit()
    key = aa.add_job()
    aa.engine.on_scanned_job(key, None, True)
    from datetime import datetime, timedelta, timezone
    stats = controls.activity(aa.engine, datetime.now(timezone.utc) - timedelta(days=1))
    assert stats["new_matches"] == 1 and stats["current"] == {S.QUEUED: 1} and stats["cap"] == aa.engine.settings.max_per_day


# ---- dashboard -------------------------------------------------------------------

def open_workspace(store):
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.session_state["hunter_db_path"] = str(store.path)
    app.run()
    app.radio(key="workspace_page").set_value("Auto Apply").run()
    assert not app.exception, app.exception
    return app


def test_dashboard_permission_buttons_and_sms_panel(smsenv, monkeypatch):
    # The dashboard builds its own settings from the environment, like production.
    from conftest import SMS_SETTINGS
    for name, value in {"TWILIO_ACCOUNT_SID": SMS_SETTINGS["twilio_account_sid"], "TWILIO_AUTH_TOKEN": SMS_SETTINGS["twilio_auth_token"],
                        "TWILIO_FROM_NUMBER": SMS_SETTINGS["twilio_from_number"], "PUBLIC_BASE_URL": SMS_SETTINGS["public_base_url"]}.items():
        monkeypatch.setenv(name, value)
    app = open_workspace(smsenv.store)
    labels = [b.label for b in app.button]
    assert "Pause" in labels and "Send test SMS" in labels
    assert any("Application permission mode: Approval Required" in m.value for m in app.markdown)
    assert any("/sms/inbound" in c.value for c in app.caption)
    next(b for b in app.button if b.label == "Pause").click().run()
    assert not app.exception and smsenv.auto.mode() == "OFF"
    assert any(b.label == "Resume previous mode" for b in app.button)
    # Mode changed elsewhere (SMS) must not be undone by the next dashboard rerun.
    controls.resume(smsenv.engine, actor="sms")
    app.run()
    assert smsenv.auto.mode() == "REVIEW"
    # Limits form persists to the database.
    form_inputs = [n for n in app.number_input if n.label.startswith("Daily submission limit")]
    form_inputs[0].set_value(3)
    next(b for b in app.button if b.label == "Save limits").click().run()
    assert not app.exception
    assert controls.preferences(smsenv.store)["max_per_day"] == 3
    assert any("Twilio configured" in s.value for s in app.success)
