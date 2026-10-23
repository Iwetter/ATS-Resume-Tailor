"""End to end: worker discovery → evaluation → Chromium preparation against
mocked forms → SMS alerts → signed YES webhook → guarded submission →
confirmation SMS → daily summary. Twilio is the official SDK over a fake
transport; every browser request is answered by the fixture router."""
import re
from unittest.mock import Mock

import pytest

from conftest import USER_PHONE, SmsEnv, aa_job
from autoapply import db as S
from autoapply.worker import Worker
from hunter.models import Snapshot, utcnow

pytestmark = pytest.mark.usefixtures("chromium_available")
PII = ("Alex", "alex@example.com", "555-0100", "fixture-auth-token")


def setup(tmp_path, master, monkeypatch, **settings):
    env = SmsEnv(tmp_path, master, **settings)
    env.store.set("expanded_boards_v2", True)
    env.store.set("employers", [dict(name="Fixture Co", source="greenhouse", board="fixtureco", career_url="https://job-boards.greenhouse.io/fixtureco",
                                     enabled=True, verified_at=None)])
    env.verify_phone()
    env.sms.save_prefs({"summary_hour": 23, "timezone": "Etc/GMT+12"})  # keep the daily summary out of the way until asked for
    env.permit()
    env.switch_on()
    monkeypatch.setattr("hunter.service.fetch_board", lambda e, c: Snapshot((aa_job(),), frozenset({"101"}), utcnow()))
    worker = Worker(env.settings, env.store, env.engine, client_factory=lambda: Mock(requests=0, cache_hits=0), notifier=env.notifier)
    env.notifier.run()
    env.twilio.requests.clear()
    return env, worker


def code_from(body):
    return re.search(r"Reply YES ([A-Z0-9]{6})", body)[1]


def test_full_approval_flow_over_sms(tmp_path, master, monkeypatch):
    env, worker = setup(tmp_path, master, monkeypatch, allow_real_submission=True)  # mocked router only
    worker.run_once()
    bodies = env.twilio.bodies
    assert any(b.startswith("New match: Fixture Co") for b in bodies)
    approval = next(b for b in bodies if b.startswith("Approve?"))
    app = env.auto.applications([S.READY])[0]
    assert env.router.posts == []  # Approval Required: nothing submitted before YES

    reply = env.reply(env.inbound(f"YES {code_from(approval)}"))
    assert reply.startswith("Approved") and "OFF on the server" not in reply
    assert env.auto.application(app["id"])["state"] == S.APPROVED

    worker.run_once()
    app = env.auto.application(app["id"])
    assert app["state"] == S.APPLIED and len(env.router.posts) == 1 and env.router.blocked == []
    assert env.twilio.bodies[-1].startswith("Submitted: Fixture Co")

    env.sms.save_prefs({"summary_hour": 0, "timezone": "UTC"})
    env.notifier.run()
    summary = env.twilio.bodies[-1]
    assert summary.startswith("Auto Apply daily summary") and "1 submitted" in summary and "submitted with confirmation" in summary
    assert all(r["data"]["To"] == USER_PHONE for r in env.twilio.requests)
    for body in env.twilio.bodies:
        assert body.isascii() and not any(p in body for p in PII), body
    # Worker restarts: nothing re-sent, nothing re-submitted.
    Worker(env.settings, env.store, env.engine, client_factory=lambda: Mock(requests=0, cache_hits=0)).run_once()
    assert len(env.router.posts) == 1


def test_real_submission_disabled_keeps_everything_prepared_only(tmp_path, master, monkeypatch):
    env, worker = setup(tmp_path, master, monkeypatch)  # AUTOAPPLY_ALLOW_REAL_SUBMISSION unset
    worker.run_once()
    first = next(b for b in env.twilio.bodies if b.startswith("Approve?"))
    assert "Real submission is off on the server" in first
    reply = env.reply(env.inbound(f"YES {code_from(first)}"))
    assert "Real submission is OFF" in reply
    worker.run_once()
    assert env.router.posts == []
    app = env.auto.applications()[0]
    assert app["state"] == S.READY and any("Real submission is disabled" in r for r in app["reasons"])
    again = [b for b in env.twilio.bodies if b.startswith("Approve?")]
    assert len(again) == 2 and code_from(again[1]) != code_from(first)  # used IDs are never reused


def test_no_by_sms_then_rescans_never_resurrect(tmp_path, master, monkeypatch):
    env, worker = setup(tmp_path, master, monkeypatch, allow_real_submission=True)
    worker.run_once()
    approval = next(b for b in env.twilio.bodies if b.startswith("Approve?"))
    assert env.reply(env.inbound(f"NO {code_from(approval)}")).startswith("Rejected")
    for status in env.auto.board_status():
        env.auto.board_update(status["employer_key"], status["name"], next_due=None)
    worker.run_once()
    assert [a["state"] for a in env.auto.applications()] == [S.CANCELLED]
    assert env.router.posts == []


def test_pause_by_sms_stops_an_approved_submission(tmp_path, master, monkeypatch):
    env, worker = setup(tmp_path, master, monkeypatch, allow_real_submission=True)
    worker.run_once()
    approval = next(b for b in env.twilio.bodies if b.startswith("Approve?"))
    env.inbound(f"YES {code_from(approval)}")
    env.inbound("PAUSE")
    worker.run_once()
    assert env.router.posts == [] and env.auto.applications()[0]["state"] == S.APPROVED
    env.inbound("RESUME")
    worker.run_once()
    assert len(env.router.posts) == 1 and env.auto.applications()[0]["state"] == S.APPLIED


def test_interrupted_submission_after_restart_alerts_once_and_never_reclicks(tmp_path, master, monkeypatch):
    env, worker = setup(tmp_path, master, monkeypatch, allow_real_submission=True)
    worker.run_once()
    app = env.auto.applications([S.READY])[0]
    token = env.auto.acquire(app["id"], (S.READY,))
    env.auto.update(app["id"], state=S.APPROVED)
    assert env.auto.begin_submission(app["id"], token)  # the process dies right here, mid-click
    restarted = Worker(env.settings, env.store, env.engine, client_factory=lambda: Mock(requests=0, cache_hits=0), notifier=env.notifier)
    restarted.recover()
    restarted.run_once()
    restarted.run_once()
    problems = [b for b in env.twilio.bodies if b.startswith("Problem:")]
    assert len(problems) == 1 and "outcome unconfirmed" in problems[0] and "check your email" in problems[0]
    assert env.auto.application(app["id"])["state"] == S.UNKNOWN and env.router.posts == []
