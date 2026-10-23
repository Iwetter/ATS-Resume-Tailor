"""SMS service with the official Twilio SDK over a fake transport: settings,
verification, outbox deduplication, retries, failures and message content."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeTwilioHttp, PUBLIC, TW_ACCOUNT, TW_FROM, USER_PHONE
from autoapply.sms import CATEGORIES, MAX_ATTEMPTS, clean_text, normalize_phone, sanitize_outgoing


def test_phone_normalisation():
    assert normalize_phone("(404) 555-0123") == "+14045550123"
    assert normalize_phone("1-404-555-0123") == "+14045550123"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    for bad in ("555-0123", "", "+0123", "call me"):
        with pytest.raises(ValueError):
            normalize_phone(bad)


def test_untrusted_text_and_outgoing_bodies_are_sanitised(smsenv):
    assert clean_text("Intern https://evil.example/login now\x07", 60) == "Intern now"
    assert clean_text("x" * 100, 20).endswith("...") and len(clean_text("x" * 100, 20)) <= 20
    from autoapply.profile import load_profile
    load_profile(smsenv.store)  # registers the applicant's name as a secret
    body = sanitize_outgoing("Alex Example alex@example.com (404) 555-0100 sk-ant-abcdefghijklmnop")
    assert "Alex" not in body and "alex@" not in body and "555-0100" not in body and "sk-ant" not in body


def test_phone_verification_flow_and_limits(smsenv):
    sms = smsenv.sms
    with pytest.raises(ValueError):
        sms.save_prefs({"enabled": True})  # no verified number yet
    sms.start_verification("404-555-0123")
    request = smsenv.twilio.requests[-1]
    assert request["data"]["To"] == USER_PHONE and request["data"]["From"] == TW_FROM and TW_ACCOUNT in request["url"]
    with pytest.raises(ValueError):
        sms.confirm_verification("000000" if "000000" not in smsenv.twilio.bodies[-1] else "111111")
    code = __import__("re").search(r"\d{6}", smsenv.twilio.bodies[-1])[0]
    assert sms.confirm_verification(code) == USER_PHONE
    with pytest.raises(ValueError):
        sms.confirm_verification(code)  # one-time
    assert sms.prefs()["phone_verified_at"]
    sms.save_prefs({"enabled": True, "delivery": "digest", "categories": ["approval", "error", "bogus"], "summary_hour": 7, "timezone": "America/Chicago"})
    prefs = sms.prefs()
    assert prefs["enabled"] and prefs["categories"] == ["approval", "error"] and prefs["summary_hour"] == 7
    for bad in ({"timezone": "Mars/Base"}, {"summary_hour": 25}, {"delivery": "sometimes"}, {"api_key": "x"}):
        with pytest.raises(ValueError):
            sms.save_prefs(bad)
    # Persisted in the database, not in the session.
    from autoapply.sms import SmsService
    assert SmsService(smsenv.store, smsenv.settings).prefs()["phone"] == USER_PHONE


def test_too_many_verification_codes_refused(smsenv):
    for _ in range(5):
        smsenv.sms.start_verification(USER_PHONE)
    with pytest.raises(ValueError):
        smsenv.sms.start_verification(USER_PHONE)


def test_unconfigured_twilio_is_reported_not_crashing(tmp_path, master):
    from conftest import AAEnv
    from autoapply.sms import SmsService
    env = AAEnv(tmp_path, master)
    sms = SmsService(env.store, env.settings)
    ok, why = sms.configured()
    assert not ok and "TWILIO_ACCOUNT_SID" in why
    with pytest.raises(ValueError):
        sms.start_verification(USER_PHONE)
    sms.enqueue("error", "k1", "hello")
    assert sms.flush()["suppressed"] == 1  # never delivered later as a backlog


def test_outbox_deduplicates_and_sends_once(smsenv):
    smsenv.verify_phone()
    sent_before = len(smsenv.twilio.requests)
    assert smsenv.sms.enqueue("error", "error:app1:9", "Problem one")
    assert not smsenv.sms.enqueue("error", "error:app1:9", "Problem one again")
    assert smsenv.sms.flush()["sent"] == 1
    assert smsenv.sms.flush()["sent"] == 0
    new = smsenv.twilio.requests[sent_before:]
    assert len(new) == 1 and new[0]["data"]["Body"] == "Problem one" and new[0]["data"]["StatusCallback"] == PUBLIC + "/sms/status"


def test_transient_failures_retry_with_backoff_then_give_up(smsenv):
    smsenv.verify_phone()
    smsenv.twilio.script = [ConnectionError("network down"), FakeTwilioHttp.error(500, 20500)]
    smsenv.sms.enqueue("error", "k", "Problem")
    assert smsenv.sms.flush()["retry"] == 1
    row = smsenv.sms.outbox()[0]
    assert row["status"] == "pending" and row["attempts"] == 1 and "ConnectionError" in row["last_error"]
    assert smsenv.sms.flush()["sent"] == 0  # backoff not elapsed
    for attempt in range(2, MAX_ATTEMPTS + 2):
        with smsenv.auto.connect() as db:
            db.execute("UPDATE sms_outbox SET next_attempt_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        if attempt == 2:
            smsenv.twilio.script = [FakeTwilioHttp.error(500, 20500)] * 10
        smsenv.sms.flush()
    row = smsenv.sms.outbox()[0]
    assert row["status"] == "failed" and row["attempts"] == MAX_ATTEMPTS
    assert any(a["action"] == "sms_failed" for a in smsenv.auto.audit_log())


def test_permanent_errors_fail_immediately_and_opt_out_is_remembered(smsenv):
    smsenv.verify_phone()
    smsenv.twilio.script = [FakeTwilioHttp.error(400, 21610)]
    smsenv.sms.enqueue("error", "k", "Problem")
    assert smsenv.sms.flush()["failed"] == 1
    assert smsenv.sms.prefs()["opted_out"]
    smsenv.sms.enqueue("error", "k2", "Another")
    assert smsenv.sms.flush()["suppressed"] == 1


def test_send_interrupted_by_a_crash_is_not_resent(smsenv):
    smsenv.verify_phone()
    smsenv.sms.enqueue("approval", "approval:ABC", "Approve?")
    with smsenv.auto.connect() as db:  # process died between claim and Twilio's answer
        db.execute("UPDATE sms_outbox SET status='sending', claimed_at=? WHERE dedupe_key='approval:ABC'",
                   ((datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),))
    before = len(smsenv.twilio.requests)
    assert smsenv.sms.flush()["interrupted"] == 1
    assert len(smsenv.twilio.requests) == before
    assert smsenv.sms.outbox()[0]["status"] == "failed"


def test_delivery_status_callback_recorded(smsenv):
    smsenv.verify_phone()
    smsenv.sms.enqueue("error", "k", "Problem")
    smsenv.sms.flush()
    with smsenv.auto.connect() as db:
        sid = db.execute("SELECT sid FROM sms_outbox WHERE dedupe_key='k'").fetchone()[0]
    assert smsenv.sms.record_status(sid, "undelivered", "30006")
    row = smsenv.sms.outbox()[0]
    assert row["delivery_status"] == "undelivered" and "30006" in row["last_error"]


def test_test_sms_button(smsenv):
    with pytest.raises(ValueError):
        smsenv.sms.send_test()  # not verified/enabled
    smsenv.verify_phone()
    assert smsenv.sms.send_test()["status"] == "sent"
    assert "test message" in smsenv.twilio.bodies[-1]


def test_api_key_credentials_are_used_when_configured(tmp_path, master):
    from conftest import SmsEnv
    env = SmsEnv(tmp_path, master, twilio_api_key_sid="SK" + "2" * 32, twilio_api_key_secret="secret-value")
    env.sms.start_verification(USER_PHONE)
    assert env.twilio.requests[-1]["auth_user"] == "SK" + "2" * 32


def test_approval_codes_are_one_time_and_bound_to_a_version(smsenv):
    app_id = smsenv.ready_application()
    code, expires, created = smsenv.sms.issue_approval_code(app_id, "hash-v1")
    assert created and len(code) == 6 and code.isalnum() and code.upper() == code
    assert smsenv.sms.issue_approval_code(app_id, "hash-v1")[0] == code  # same version → same live ID
    other, _, created = smsenv.sms.issue_approval_code(app_id, "hash-v2")
    assert created and other != code
    assert smsenv.sms.consume_code(code, "approved", "SMx")
    assert not smsenv.sms.consume_code(code, "approved", "SMy")
    assert smsenv.sms.issue_approval_code(app_id, "hash-v1")[0] != code  # used IDs are never reissued
    assert set(CATEGORIES) >= {"new_match", "approval", "submitted", "manual_action", "error", "daily_summary"}
