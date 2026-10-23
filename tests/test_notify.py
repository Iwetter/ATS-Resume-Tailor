"""Notifications: which events produce texts, deduplication, delivery modes,
the daily summary, restarts and SMS failures."""
from datetime import datetime, timedelta, timezone
import re

import pytest

from conftest import PUBLIC, USER_PHONE, aa_job
from autoapply import db as S
from autoapply.notify import Notifier

PII = ("Alex", "Example", "alex@example.com", "555-0100", USER_PHONE, "fixture-auth-token")


@pytest.fixture
def env(smsenv):
    smsenv.verify_phone()
    smsenv.switch_on()
    smsenv.permit()
    # The daily summary depends on the wall clock; only the tests about it switch it on.
    from autoapply.sms import CATEGORIES
    smsenv.sms.save_prefs({"categories": [c for c in CATEGORIES if c != "daily_summary"]})
    smsenv.notifier.run()  # first run sets the cursors at "now"
    smsenv.twilio.requests.clear()
    return smsenv


def assert_safe(body):
    for secret in PII:
        assert secret not in body, secret
    assert body.isascii(), "non-GSM characters would switch the SMS to UCS-2 and triple its cost"


def test_new_high_match_alert_has_required_fields_once(env):
    key = env.add_job()
    env.engine.on_scanned_job(key, None, True)
    env.notifier.run()
    body = env.twilio.bodies[-1]
    assert body.startswith("New match: Fixture Co - Hardware Engineering Intern - Summer 2027 (fit ")
    assert "Action: queued for preparation" in body and "Status: Queued" in body and re.search(PUBLIC + r"/r/[a-z]{17}", body)
    assert_safe(body)
    env.engine.on_scanned_job(key, None, False)  # rescans never re-alert
    env.notifier.run()
    assert len(env.twilio.requests) == 1


def test_low_fit_or_ineligible_jobs_do_not_alert(env):
    unrelated = env.add_job(aa_job(job_id="9", title="Marketing Intern - Summer 2027"))
    env.engine.on_scanned_job(unrelated, None, True)
    env.notifier.run()
    assert env.twilio.requests == []


def test_match_at_unapproved_employer_still_alerts_with_job_link(env):
    key = env.add_job(aa_job(job_id="77", board="otherco", company="Other Co"))
    env.engine.on_scanned_job(key, None, True)
    env.notifier.run()
    body = env.twilio.bodies[-1]
    assert "Other Co" in body and "Status: Not queued" in body and "/j/" in body


def test_many_matches_are_batched(env):
    for n in range(5):
        key = env.add_job(aa_job(job_id=str(500 + n), board=f"b{n}", company=f"Company {n}"))
        env.engine.on_scanned_job(key, None, True)
    env.notifier.run()
    assert len(env.twilio.requests) == 1 and env.twilio.bodies[0].startswith("5 new high-match internships")
    assert "+2 more:" in env.twilio.bodies[0]


def test_no_backlog_is_sent_after_enabling(env):
    env.sms.save_prefs({"enabled": False})
    key = env.add_job()
    env.engine.on_scanned_job(key, None, True)
    env.notifier.run()
    env.sms.save_prefs({"enabled": True})
    env.notifier.run()
    assert env.twilio.requests == []


def test_approval_request_is_sent_once_per_version_even_in_digest_mode(env):
    env.sms.save_prefs({"delivery": "digest"})
    app_id = env.ready_application()
    env.notifier.run()
    body = env.twilio.bodies[-1]
    code = re.search(r"Reply YES ([A-Z0-9]{6}) or NO \1", body)[1]
    assert body.startswith("Approve? Fixture Co - Hardware Engineering Intern") and "fit 72->75" in body and "Status: Ready for Review" in body
    assert "Real submission is off" in body and "/r/" in body
    assert_safe(body)
    env.engine.requeue(app_id)
    env.auto.update(app_id, action="ready_for_review", state=S.READY, approval_hash="hash-v1")  # same version again
    env.notifier.run()
    assert len(env.twilio.requests) == 1
    env.auto.update(app_id, action="ready_for_review", state=S.READY, approval_hash="hash-v2")  # changed version → new ID
    env.notifier.run()
    assert len(env.twilio.requests) == 2 and code not in env.twilio.bodies[-1]


def test_event_categories_and_toggles(env):
    app_id = env.ready_application()
    env.notifier.run()
    env.twilio.requests.clear()
    env.auto.update(app_id, action="needs_human", state=S.NEEDS_HUMAN, reasons=["captcha: Security challenge present. Not bypassed."])
    env.notifier.run()
    assert env.twilio.bodies[-1].startswith("Action needed:") and "Why: CAPTCHA" in env.twilio.bodies[-1]
    env.auto.update(app_id, action="submission_unknown", state=S.UNKNOWN, reasons=["Submit was clicked but no confirmation was observed."])
    env.notifier.run()
    assert env.twilio.bodies[-1].startswith("Problem:") and "check your email" in env.twilio.bodies[-1]
    env.auto.update(app_id, action="unknown_resolved_applied", state=S.APPLIED)
    env.notifier.run()
    assert env.twilio.bodies[-1].startswith("Submitted:") and "Status: Applied" in env.twilio.bodies[-1]
    env.sms.save_prefs({"categories": ["approval"]})
    other = env.ready_application(aa_job(job_id="31", board="b31", company="Thirty One"))
    env.auto.update(other, action="needs_human", state=S.NEEDS_HUMAN, reasons=["login: Login field."])
    count = len(env.twilio.requests)
    env.notifier.run()
    assert all("Action needed" not in b for b in env.twilio.bodies[count:])
    for body in env.twilio.bodies:
        assert_safe(body)


def test_preparation_failure_alerts_only_when_retries_are_exhausted(env):
    app_id = env.ready_application()
    env.notifier.run()
    env.twilio.requests.clear()
    env.auto.update(app_id, action="preparation_failed", state=S.FAILED, attempts=1)
    env.notifier.run()
    assert env.twilio.requests == []
    env.auto.update(app_id, action="preparation_failed", state=S.FAILED, attempts=3)
    env.notifier.run()
    assert env.twilio.bodies[-1].startswith("Problem:") and "preparation failed" in env.twilio.bodies[-1]


def test_digest_mode_holds_events_for_the_summary(env):
    env.sms.save_prefs({"delivery": "digest", "summary_hour": 0, "categories": ["approval", "manual_action", "daily_summary"]})
    app_id = env.ready_application()
    env.auto.update(app_id, action="needs_human", state=S.NEEDS_HUMAN, reasons=["captcha: x"])
    env.notifier.run()
    bodies = env.twilio.bodies
    assert not any(b.startswith("Action needed") for b in bodies)
    assert any(b.startswith("Auto Apply daily summary") for b in bodies)


def test_daily_summary_once_per_day_with_every_field(env):
    env.sms.save_prefs({"summary_hour": 0, "categories": ["daily_summary", "approval"]})
    env.ready_application()
    needs = env.ready_application(aa_job(job_id="42", board="b42", company="Forty Two"))
    env.auto.update(needs, action="needs_human", state=S.NEEDS_HUMAN, reasons=["captcha: x"])
    env.notifier.run()
    summary = next(b for b in env.twilio.bodies if b.startswith("Auto Apply daily summary"))
    assert "1 awaiting approval" in summary and "1 need action" in summary and "Mode: Approval Required" in summary
    line = next(l for l in summary.splitlines() if "Fixture Co" in l)
    assert "fit 72->75" in line and "prepared; awaiting your approval" in line and S.READY in line and "/r/" in line and re.search(r"YES/NO [A-Z0-9]{6}", line)
    assert "/s/" in summary
    assert_safe(summary)
    count = len(env.twilio.requests)
    env.notifier.run()
    Notifier(env.engine, env.sms).run()  # a restarted worker does not resend today's summary
    assert len(env.twilio.requests) == count


def test_daily_summary_waits_for_the_configured_local_hour(env):
    env.sms.save_prefs({"summary_hour": 23, "timezone": "Pacific/Kiritimati", "categories": ["daily_summary"]})
    local = env.sms.local_now()
    early = datetime(local.year, local.month, local.day, 22, 0, tzinfo=local.tzinfo).astimezone(timezone.utc)
    assert env.notifier.maybe_daily_summary(env.sms.prefs(), True, now=early) == 0
    assert env.notifier.maybe_daily_summary(env.sms.prefs(), True, now=early + timedelta(hours=1, minutes=5)) == 1


def test_restart_neither_loses_nor_repeats_events(env):
    app_id = env.ready_application()
    env.auto.update(app_id, state=S.SUBMITTING, submit_started_at=datetime.now(timezone.utc).isoformat())
    from autoapply.worker import Worker
    from unittest.mock import Mock
    worker = Worker(env.settings, env.store, env.engine, client_factory=lambda: Mock(requests=0, cache_hits=0), notifier=Notifier(env.engine, env.sms))
    worker.recover()  # interrupted application → Unknown
    worker.notify()
    problems = [b for b in env.twilio.bodies if b.startswith("Problem:")]
    assert len(problems) == 1 and "outcome unconfirmed" in problems[0]
    Notifier(env.engine, env.sms).run()  # new process, same database
    assert len([b for b in env.twilio.bodies if b.startswith("Problem:")]) == 1


def test_sms_outage_never_breaks_the_worker(env, monkeypatch):
    from autoapply.worker import Worker
    from unittest.mock import Mock
    env.store.set("expanded_boards_v2", True)
    env.store.set("expanded_boards_v3", True)
    env.store.set("employers", [])
    worker = Worker(env.settings, env.store, env.engine, client_factory=lambda: Mock(requests=0, cache_hits=0), notifier=Notifier(env.engine, env.sms))
    env.ready_application()
    env.twilio.script = [ConnectionError("Twilio unreachable")] * 3
    worker.run_once()
    assert env.store.get("worker_heartbeat")["state"] == "idle"
    assert env.sms.outbox()[0]["status"] == "pending"  # retried next cycle

    def explode():
        raise RuntimeError("database locked")
    monkeypatch.setattr(worker.notifier, "run", explode)
    worker.run_once()
    assert env.store.get("worker_heartbeat")["sms"] == {"error": "RuntimeError"}
