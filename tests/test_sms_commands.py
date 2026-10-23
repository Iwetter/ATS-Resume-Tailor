"""Inbound SMS commands through the real Flask webhook with Twilio signatures:
authentication, duplicate protection, one-time approval IDs and mode commands."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import PUBLIC, TW_ACCOUNT, USER_PHONE
from autoapply import controls
from autoapply import db as S


@pytest.fixture
def env(smsenv):
    smsenv.verify_phone()
    smsenv.switch_on()
    return smsenv


def test_missing_or_forged_signature_is_rejected(env):
    app_id = env.ready_application()
    code, _, _ = env.sms.issue_approval_code(app_id, "hash-v1")
    assert env.inbound(f"YES {code}", signature="").status_code == 403
    assert env.inbound(f"YES {code}", signature="bm90IGEgc2lnbmF0dXJl").status_code == 403
    # A valid signature for a different URL (e.g. replayed against another endpoint) fails too.
    from twilio.request_validator import RequestValidator
    from conftest import TW_TOKEN
    params = {"MessageSid": "SM" + "a" * 32, "AccountSid": TW_ACCOUNT, "From": USER_PHONE, "Body": f"YES {code}"}
    wrong = RequestValidator(TW_TOKEN).compute_signature(PUBLIC + "/sms/status", params)
    assert env.client.post("/sms/inbound", data=params, headers={"X-Twilio-Signature": wrong}).status_code == 403
    assert env.auto.application(app_id)["state"] == S.READY
    assert any(a["action"] == "sms_signature_rejected" for a in env.auto.audit_log())


def test_webhook_refused_when_gateway_not_configured(tmp_path, master):
    from conftest import SmsEnv
    env = SmsEnv(tmp_path, master, public_base_url="")
    assert env.inbound("STATUS").status_code == 403


def test_unverified_sender_and_other_account_get_no_reply_and_no_effect(env):
    app_id = env.ready_application()
    code, _, _ = env.sms.issue_approval_code(app_id, "hash-v1")
    response = env.inbound(f"YES {code}", sender="+14045559999")
    assert response.status_code == 200 and env.reply(response) is None
    assert env.reply(env.inbound(f"YES {code}", account="AC" + "9" * 32)) is None
    assert env.auto.application(app_id)["state"] == S.READY
    assert env.sms.approval_code(code)["used_at"] is None
    with env.auto.connect() as db:
        senders = [r[0] for r in db.execute("SELECT sender FROM sms_inbound")]
    assert senders and all(s.startswith("•••") and "+1404" not in s for s in senders)  # stored masked


def test_yes_approves_exactly_that_version_once(env):
    app_id = env.ready_application()
    code, _, _ = env.sms.issue_approval_code(app_id, "hash-v1")
    sid = "SM" + "b" * 32
    reply = env.reply(env.inbound(f"yes {code.lower()}", sid=sid))
    assert reply.startswith(f"Approved {code}") and "Real submission is OFF" in reply
    app = env.auto.application(app_id)
    assert app["state"] == S.APPROVED and app["approval_hash"] == "hash-v1"
    assert any(a["action"] == "approved" and a["actor"] == "sms" for a in env.auto.audit_log(app_id))
    # Twilio retries the same webhook: answered from the record, executed once.
    again = env.reply(env.inbound(f"YES {code}", sid=sid))
    assert again == reply
    assert sum(a["action"] == "approved" for a in env.auto.audit_log(app_id)) == 1
    # A new message reusing the ID is refused.
    assert "already used" in env.reply(env.inbound(f"YES {code}"))


def test_yes_refused_for_changed_version_expired_or_unknown_ids(env):
    app_id = env.ready_application()
    code, _, _ = env.sms.issue_approval_code(app_id, "hash-v1")
    env.auto.update(app_id, approval_hash="hash-v2")  # form/resume/answers changed after the text was sent
    assert "changed since" in env.reply(env.inbound(f"YES {code}"))
    assert env.auto.application(app_id)["state"] == S.READY
    assert "already used" in env.reply(env.inbound(f"YES {code}"))  # stale IDs are burned
    fresh, _, _ = env.sms.issue_approval_code(app_id, "hash-v2")
    with env.auto.connect() as db:
        db.execute("UPDATE approval_codes SET expires_at=? WHERE code=?", ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), fresh))
    assert "expired" in env.reply(env.inbound(f"YES {fresh}"))
    assert "Unknown approval ID" in env.reply(env.inbound("YES ZZZZZZ"))
    assert "Include the approval ID" in env.reply(env.inbound("YES"))
    assert env.auto.application(app_id)["state"] == S.READY


def test_no_rejects_and_cannot_be_overwritten_by_the_worker(env):
    app_id = env.ready_application()
    code, _, _ = env.sms.issue_approval_code(app_id, "hash-v1")
    assert env.reply(env.inbound(f"NO {code}")).startswith(f"Rejected {code}")
    assert env.auto.application(app_id)["state"] == S.CANCELLED
    # A worker that was mid-preparation cannot resurrect it (compare-and-set).
    assert not env.auto.update(app_id, action="ready_for_review", expected=(S.PREPARING, S.APPROVED, S.SUBMITTING), state=S.READY)
    assert env.auto.application(app_id)["state"] == S.CANCELLED


def test_no_during_preparation_wins_over_worker(env):
    """Cancel while the worker holds the lease: the worker's later transition is refused."""
    key = env.add_job()
    app_id = env.auto.enqueue(key, __import__("conftest").aa_job(), "greenhouse:fixtureco", "d", "REVIEW", {})
    token = env.auto.acquire(app_id, (S.QUEUED,))
    env.auto.update(app_id, state=S.PREPARING)
    env.engine.cancel(app_id, actor="sms")
    assert env.engine._state(app_id) == S.CANCELLED
    assert not env.auto.begin_submission(app_id, token)  # no click can start
    with pytest.raises(ValueError):
        env.engine.requeue(app_id)


def test_cancel_cannot_overwrite_a_started_submission(env):
    app_id = env.ready_application()
    env.auto.update(app_id, state=S.SUBMITTING)
    with pytest.raises(ValueError):
        env.engine.cancel(app_id)
    assert env.auto.application(app_id)["state"] == S.SUBMITTING


def test_pause_and_resume_approval_required(env):
    assert "Paused" in env.reply(env.inbound("PAUSE"))
    assert env.auto.mode() == "OFF" and controls.permission_mode(env.engine) == controls.PAUSED
    # Paused: monitoring continues (decisions recorded) but nothing is queued.
    key = env.add_job()
    env.permit()
    env.engine.evaluate(key)
    assert env.auto.decisions(key) and env.auto.application(job_key=key) is None
    assert env.engine.process() == []
    reply = env.reply(env.inbound("RESUME"))
    assert reply.startswith("Resumed: Approval Required") and env.auto.mode() == "REVIEW"
    assert "Not paused" in env.reply(env.inbound("RESUME"))


def test_resume_restores_full_autopilot_only_if_still_authorised(tmp_path, master, chromium_available):
    from conftest import SmsEnv
    from autoapply.modes import run_dry_run
    env = SmsEnv(tmp_path, master)
    env.verify_phone()
    env.switch_on()
    assert run_dry_run(env.engine)["ok"]
    controls.set_permission_mode(env.engine, controls.AUTOPILOT, confirmation="ENABLE AUTO")
    assert env.engine.auto_active()
    env.reply(env.inbound("PAUSE"))
    assert env.auto.mode() == "OFF" and not env.engine.auto_active()
    assert env.reply(env.inbound("RESUME")).startswith("Resumed: Full Autopilot")
    assert env.engine.auto_active()
    # Pause again, then let the dry run go stale: RESUME falls back to Approval Required.
    env.reply(env.inbound("PAUSE"))
    dry = env.store.get("dryrun_validation")
    env.store.set("dryrun_validation", {**dry, "at": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()})
    reply = env.reply(env.inbound("RESUME"))
    assert "Resumed: Approval Required" in reply and "older than 7 days" in reply
    assert env.auto.mode() == "REVIEW" and not env.engine.auto_active()


def test_resume_refused_during_emergency_stop(env):
    env.reply(env.inbound("PAUSE"))
    env.auto.set_emergency_stop(True, reason="test")
    assert "Emergency stop" in env.reply(env.inbound("RESUME"))
    assert env.auto.mode() == "OFF"


def test_status_reports_health_and_activity_without_personal_data(env):
    env.ready_application()
    env.store.set("worker_heartbeat", {"at": datetime.now(timezone.utc).isoformat(), "state": "idle"})
    reply = env.reply(env.inbound("STATUS"))
    assert "Worker: healthy" in reply and "Approval Required" in reply and "1 awaiting approval" in reply and "Real submission: off" in reply
    for secret in ("Alex", "alex@example.com", "555-0100", USER_PHONE, "fixture-auth-token"):
        assert secret not in reply


def test_unknown_text_gets_help_and_opt_out_pauses(env):
    assert "Auto Apply commands" in env.reply(env.inbound("please apply to everything"))
    response = env.inbound("STOP")
    assert env.reply(response) is None  # Twilio itself confirms opt-outs
    assert env.sms.prefs()["opted_out"] and env.auto.mode() == "OFF"
    env.inbound("START")
    assert not env.sms.prefs()["opted_out"]


def test_status_callback_requires_signature(env):
    from twilio.request_validator import RequestValidator
    from conftest import TW_TOKEN
    params = {"MessageSid": "SM" + "c" * 32, "MessageStatus": "delivered", "AccountSid": TW_ACCOUNT}
    assert env.client.post("/sms/status", data=params).status_code == 403
    sig = RequestValidator(TW_TOKEN).compute_signature(PUBLIC + "/sms/status", params)
    assert env.client.post("/sms/status", data=params, headers={"X-Twilio-Signature": sig}).status_code == 204
