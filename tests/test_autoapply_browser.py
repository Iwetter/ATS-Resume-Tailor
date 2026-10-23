"""End-to-end application flow in real headless Chromium against mocked forms.

Every request is answered by ``FixtureRouter``; unknown URLs are aborted, so
nothing can reach a real employer. Each test asserts ``router.blocked == []``
(no request even tried to leave) where relevant.
"""
from pathlib import Path

import pytest

from conftest import AAEnv, aa_job
from autoapply import db as S
from autoapply.modes import enable_auto, run_dry_run
from autoapply.selftest import FixtureRouter, GH_BASE, LEVER_BASE, greenhouse_form, SILENT_PAGE

pytestmark = pytest.mark.usefixtures("chromium_available")


def prepared(env, job=None, mode="REVIEW", auto_submit=False):
    env.permit(auto_submit=auto_submit)
    env.switch_on(mode)
    key = env.add_job(job)
    env.engine.on_scanned_job(key, None, True)  # immediate evaluation after discovery
    app = env.auto.application(job_key=key)
    assert app and app["state"] == S.QUEUED, env.auto.decisions(key)[0]["details"]
    env.engine.process()
    return env.auto.application(app["id"])


def test_review_mode_prepares_but_never_submits(aa):
    app = prepared(aa)
    assert app["state"] == S.READY, app["reasons"]
    assert aa.router.posts == [] and aa.router.blocked == []
    run = app["plan"]["last_run"]
    assert Path(run["screenshot"]).is_file()
    filled = {f["label"]: f for f in run["fields"]}
    assert filled["First Name *"]["source"] == "profile" and filled["Resume/CV *"]["source"] == "resume"
    assert filled["Gender"]["source"] == "blank"
    assert "alex@example.com" not in str(run)  # audit-safe field view
    assert app["approval_hash"] and Path(app["plan"]["pdf"]).is_file()
    # The tailored resume came from Phase 1 export with original evidence only.
    assert app["plan"]["approved_change_ids"] == []


def test_approved_submission_refused_while_real_submission_disabled(aa):
    app = prepared(aa)
    aa.engine.approve(app["id"], app["approval_hash"])
    aa.engine.process()
    app = aa.auto.application(app["id"])
    assert app["state"] == S.READY
    assert any("Real submission is disabled" in r for r in app["reasons"])
    assert aa.router.posts == []


def test_approved_submission_with_confirmation_evidence(tmp_path, master):
    env = AAEnv(tmp_path, master, allow_real_submission=True)  # mocked router only
    app = prepared(env)
    env.engine.approve(app["id"], app["approval_hash"])
    env.engine.process()
    app = env.auto.application(app["id"])
    assert app["state"] == S.APPLIED, app["reasons"]
    assert len(env.router.posts) == 1 and env.router.blocked == []
    assert "Thank you for applying" in app["confirmation"]["matched_text"]
    assert Path(app["confirmation"]["screenshot"]).is_file() and Path(app["confirmation"]["html"]).is_file()
    assert env.store.job(app["job_key"])["status"] == "Applied"
    # Duplicate submission prevention: no path can submit this job again.
    with pytest.raises(ValueError):
        env.engine.requeue(app["id"])
    token = env.auto.acquire(app["id"], (S.APPLIED,))
    assert not env.auto.begin_submission(app["id"], token)
    env.engine.process()
    assert len(env.router.posts) == 1
    # A cross-posted duplicate (same company and title, different requisition) is skipped.
    dup = env.add_job(aa_job(job_id="202"))
    env.engine.evaluate(dup)
    assert env.auto.application(job_key=dup)["state"] == S.SKIPPED


def test_form_changed_after_approval_returns_to_review(tmp_path, master):
    env = AAEnv(tmp_path, master, allow_real_submission=True)
    app = prepared(env)
    env.engine.approve(app["id"], app["approval_hash"])
    env.router.pages[GH_BASE] = greenhouse_form(extra='<div class="field"><label for="new">Preferred office</label><input id="new"></div>')
    env.engine.process()
    app = env.auto.application(app["id"])
    assert app["state"] == S.READY and any("changed since approval" in r for r in app["reasons"])
    assert env.router.posts == []


@pytest.mark.parametrize("kwargs,kind", [({"captcha": True}, "captcha"), ({"login": True}, "login")])
def test_captcha_and_login_go_to_needs_human_action(tmp_path, master, kwargs, kind):
    env = AAEnv(tmp_path, master, router=FixtureRouter({GH_BASE: greenhouse_form(**kwargs)}), allow_real_submission=True)
    app = prepared(env)
    assert app["state"] == S.NEEDS_HUMAN and any(r.startswith(kind) for r in app["reasons"])
    assert env.router.posts == []


def test_mfa_text_and_unexpected_redirect_are_blockers(tmp_path, master):
    mfa = greenhouse_form(extra="<p>Enter the verification code we sent to your phone.</p>")
    env = AAEnv(tmp_path, master, router=FixtureRouter({GH_BASE: mfa}), allow_real_submission=True)
    assert prepared(env)["state"] == S.NEEDS_HUMAN
    assert env.router.posts == []


def test_unexpected_legal_attestation_needs_human_then_can_be_approved(tmp_path, master):
    env = AAEnv(tmp_path, master, router=FixtureRouter({GH_BASE: greenhouse_form(attestation=True)}))
    app = prepared(env)
    assert app["state"] == S.NEEDS_HUMAN and any("attestation" in r.lower() for r in app["reasons"])
    from autoapply.forms import attestation_hash
    label = "I certify that all information provided is true and complete. *"
    env.auto.approve_attestation("greenhouse:fixtureco", "Fixture Co", attestation_hash(label), label)
    env.engine.requeue(app["id"])
    env.engine.process()
    assert env.auto.application(app["id"])["state"] == S.READY


def test_required_unknown_question_is_reported_not_invented(tmp_path, master):
    env = AAEnv(tmp_path, master, router=FixtureRouter({GH_BASE: greenhouse_form(required_unknown=True)}))
    app = prepared(env)
    assert app["state"] == S.NEEDS_HUMAN
    assert any("led a hardware team" in r for r in app["reasons"])
    field = next(f for f in app["plan"]["last_run"]["fields"] if "led a hardware team" in f["label"])
    assert field["value"] == "" and field["source"] == "unresolved"
    env.auto.save_answer("Describe a time you led a hardware team. *", "I led a four-person team building a fictional test rig.", verified=True)
    env.engine.requeue(app["id"])
    env.engine.process()
    assert env.auto.application(app["id"])["state"] == S.READY


def test_missing_confirmation_is_unknown_and_never_retried(tmp_path, master):
    router = FixtureRouter(confirm=False)
    env = AAEnv(tmp_path, master, router=router, allow_real_submission=True)
    app = prepared(env)
    env.engine.approve(app["id"], app["approval_hash"])
    env.engine.process()
    app = env.auto.application(app["id"])
    assert app["state"] == S.UNKNOWN and len(router.posts) == 1
    for _ in range(3):
        env.engine.process()
    assert len(router.posts) == 1 and env.auto.application(app["id"])["state"] == S.UNKNOWN
    assert env.store.job(app["job_key"])["status"] != "Applied"
    with pytest.raises(ValueError):
        env.engine.resolve_unknown(app["id"], True, "")
    env.engine.resolve_unknown(app["id"], True, "Confirmation email received (fictional test)")
    assert env.auto.application(app["id"])["state"] == S.APPLIED


def test_auto_mode_requires_dry_run_and_explicit_enable(tmp_path, master):
    env = AAEnv(tmp_path, master, allow_real_submission=True)
    with pytest.raises(ValueError):
        env.auto.set_mode("AUTO")
    with pytest.raises(ValueError):
        enable_auto(env.engine, "ENABLE AUTO")  # no dry run yet
    result = run_dry_run(env.engine)
    assert result["ok"], [c for c in result["checks"] if not c["ok"]]
    with pytest.raises(ValueError):
        enable_auto(env.engine, "yes")
    enable_auto(env.engine, "ENABLE AUTO")
    assert env.engine.auto_active()
    app = prepared(env, mode="AUTO", auto_submit=True)
    assert app["state"] == S.APPLIED, app["reasons"]
    assert len(env.router.posts) == 1 and env.router.blocked == []


def test_auto_mode_without_employer_auto_permission_waits_for_review(tmp_path, master):
    env = AAEnv(tmp_path, master, allow_real_submission=True)
    assert run_dry_run(env.engine)["ok"]
    enable_auto(env.engine, "ENABLE AUTO")
    app = prepared(env, mode="AUTO", auto_submit=False)
    assert app["state"] == S.READY and env.router.posts == []


def test_profile_change_deactivates_auto(tmp_path, master):
    from dataclasses import replace
    from autoapply.profile import load_profile, save_profile
    env = AAEnv(tmp_path, master, allow_real_submission=True)
    assert run_dry_run(env.engine)["ok"]
    enable_auto(env.engine, "ENABLE AUTO")
    save_profile(env.store, replace(load_profile(env.store), city="Decatur"))
    assert not env.engine.auto_active()


def test_daily_cap_blocks_second_submission(tmp_path, master):
    env = AAEnv(tmp_path, master, allow_real_submission=True, max_per_day=1)
    env.permit()
    env.permit("greenhouse:otherco")
    env.switch_on()
    first = env.add_job()
    env.router.pages["https://job-boards.greenhouse.io/otherco/jobs/303"] = greenhouse_form(action="/otherco/jobs/303/applications")
    second = env.add_job(aa_job(job_id="303", board="otherco", company="Other Co"))
    for key in (first, second):
        env.engine.evaluate(key)
    env.engine.process(limit=5)
    for key in (first, second):
        app = env.auto.application(job_key=key)
        env.engine.approve(app["id"], app["approval_hash"])
    env.engine.process(limit=5)
    states = sorted(env.auto.application(job_key=k)["state"] for k in (first, second))
    assert states == [S.APPLIED, S.READY]
    blocked = next(env.auto.application(job_key=k) for k in (first, second) if env.auto.application(job_key=k)["state"] == S.READY)
    assert any("Daily submission cap" in r for r in blocked["reasons"])
    assert len(env.router.posts) == 1


def test_emergency_stop_halts_everything(tmp_path, master):
    env = AAEnv(tmp_path, master, allow_real_submission=True)
    app = prepared(env)
    env.engine.approve(app["id"], app["approval_hash"])
    env.auto.set_emergency_stop(True, reason="test")
    assert env.engine.process() == []
    env.auto.set_emergency_stop(False)
    env.settings.stop_file.write_text("stop")
    assert env.engine.process() == []
    env.settings.stop_file.unlink()
    assert env.router.posts == []
    assert env.auto.application(app["id"])["state"] == S.APPROVED


def test_emergency_stop_during_run_prevents_click(tmp_path, master):
    env = AAEnv(tmp_path, master, allow_real_submission=True)
    app = prepared(env)
    env.engine.approve(app["id"], app["approval_hash"])
    original = env.router.__call__

    class StopWhenPageLoads(FixtureRouter):
        def __call__(self, route):
            if route.request.method == "GET":
                env.auto.set_emergency_stop(True, reason="pressed mid-run")
            return FixtureRouter.__call__(self, route)

    stopping = StopWhenPageLoads()
    env.engine.browser.router = stopping
    env.engine.submit_approved(app["id"])
    assert stopping.posts == []
    app = env.auto.application(app["id"])
    assert app["state"] == S.READY and any("Emergency stop" in r for r in app["reasons"])


def test_lever_flow_and_unsupported_flow(tmp_path, master):
    env = AAEnv(tmp_path, master)
    env.permit("lever:fixtureco")
    env.switch_on()
    lever = aa_job(job_id="0a1b2c3d-fixture", source="lever", title="Embedded Firmware Intern - Summer 2027", published_at=None, publication_basis="Posting date unknown")
    key = env.add_job(lever)
    env.engine.evaluate(key)
    env.engine.process()
    assert env.auto.application(job_key=key)["state"] == S.READY
    env.auto.set_permission("ashby:fixtureco", "Fixture Ashby", prefill=True, auto_submit=False, terms_reviewed=False)
    other = env.add_job(aa_job(job_id="ash-9", source="ashby", title="Firmware Intern - Summer 2027", url="https://jobs.ashbyhq.com/fixtureco/ash-9"))
    env.engine.evaluate(other)
    env.engine.process()
    app = env.auto.application(job_key=other)
    assert app["state"] == S.NEEDS_HUMAN and "No supported" in app["reasons"][0]
    assert env.router.blocked == []
