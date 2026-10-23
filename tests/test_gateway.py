"""Phone gateway: authentication, expiring links, CSRF, version-bound approval,
file confinement and security headers. The private dashboard is never served."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import GATEWAY_PASSWORD
from autoapply import db as S
from autoapply import links
from autoapply.webauth import hash_password, verify_password


def test_password_hashing():
    stored = hash_password("a long enough password", n=2 ** 12)
    assert "$" not in stored and stored.startswith("scrypt:")
    assert verify_password("a long enough password", stored)
    assert not verify_password("wrong password!!", stored)
    assert not verify_password("anything", "garbage")
    with pytest.raises(ValueError):
        hash_password("short")


def test_gateway_refuses_to_start_without_a_secret_key(smsenv):
    from dataclasses import replace
    from autoapply.gateway import create_app
    with pytest.raises(RuntimeError):
        create_app(replace(smsenv.settings, gateway_secret_key="short"), smsenv.store, smsenv.engine, smsenv.sms)


def test_review_link_requires_login_and_expires(smsenv):
    app_id = smsenv.ready_application()
    code = links.issue(smsenv.auto, "application", app_id, 72)
    response = smsenv.client.get(f"/r/{code}")
    assert response.status_code == 302 and response.headers["Location"].startswith("/login?next=/r/")
    assert smsenv.login().status_code == 302
    page = smsenv.client.get(f"/r/{code}")
    assert page.status_code == 200 and "Hardware Engineering Intern" in page.get_data(as_text=True)
    assert smsenv.client.get("/r/notarealcodeatall").status_code == 404
    assert smsenv.client.get("/j/" + code).status_code == 404  # a link is only valid for its own kind
    with smsenv.auto.connect() as db:
        db.execute("UPDATE review_links SET expires_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    assert smsenv.client.get(f"/r/{code}").status_code == 404


def test_wrong_passwords_are_throttled(smsenv):
    page = smsenv.client.get("/login").get_data(as_text=True)
    csrf = __import__("re").search(r'name="csrf" value="([^"]+)"', page)[1]
    for _ in range(5):
        assert smsenv.client.post("/login", data={"csrf": csrf, "password": "nope nope nope"}).status_code == 401
    blocked = smsenv.client.post("/login", data={"csrf": csrf, "password": GATEWAY_PASSWORD})
    assert blocked.status_code == 401 and "Too many attempts" in blocked.get_data(as_text=True)
    assert smsenv.client.post("/login", data={"password": GATEWAY_PASSWORD}).status_code == 400  # no CSRF token


def test_phone_approval_binds_to_the_version_shown(smsenv):
    app_id = smsenv.ready_application()
    code = links.issue(smsenv.auto, "application", app_id, 72)
    smsenv.login()
    csrf = smsenv.csrf(f"/r/{code}")
    assert smsenv.client.post(f"/r/{code}/decide", data={"csrf": "forged", "decision": "approve", "approval_hash": "hash-v1"}).status_code == 400
    stale = smsenv.client.post(f"/r/{code}/decide", data={"csrf": csrf, "decision": "approve", "approval_hash": "old-hash"})
    assert stale.status_code == 409 and smsenv.auto.application(app_id)["state"] == S.READY
    ok = smsenv.client.post(f"/r/{code}/decide", data={"csrf": csrf, "decision": "approve", "approval_hash": "hash-v1"})
    assert ok.status_code == 302 and smsenv.auto.application(app_id)["state"] == S.APPROVED
    assert any(a["action"] == "approved" and a["actor"] == "phone" for a in smsenv.auto.audit_log(app_id))


def test_phone_reject(smsenv):
    app_id = smsenv.ready_application()
    code = links.issue(smsenv.auto, "application", app_id, 72)
    smsenv.login()
    csrf = smsenv.csrf(f"/r/{code}")
    smsenv.client.post(f"/r/{code}/decide", data={"csrf": csrf, "decision": "reject"})
    assert smsenv.auto.application(app_id)["state"] == S.CANCELLED


def test_files_are_confined_to_the_data_directory(smsenv, tmp_path):
    app_id = smsenv.ready_application()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 not yours")
    inside = smsenv.settings.data_dir / "tailored" / "r.pdf"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"%PDF-1.4 resume")
    code = links.issue(smsenv.auto, "application", app_id, 72)
    smsenv.login()
    smsenv.auto.update(app_id, plan={"pdf": str(outside)})
    assert smsenv.client.get(f"/r/{code}/resume.pdf").status_code == 404
    smsenv.auto.update(app_id, plan={"pdf": str(smsenv.settings.data_dir / ".." / "secret.pdf")})
    assert smsenv.client.get(f"/r/{code}/resume.pdf").status_code == 404
    smsenv.auto.update(app_id, plan={"pdf": str(inside)})
    assert smsenv.client.get(f"/r/{code}/resume.pdf").data == b"%PDF-1.4 resume"


def test_security_headers_and_no_dashboard(smsenv):
    response = smsenv.client.get("/login")
    for header in ("Content-Security-Policy", "X-Frame-Options", "Referrer-Policy", "X-Content-Type-Options"):
        assert header in response.headers
    assert response.headers["Referrer-Policy"] == "no-referrer" and response.headers["Cache-Control"] == "no-store"
    for path in ("/", "/_stcore/health", "/dashboard", "/admin"):
        assert smsenv.client.get(path).status_code == 404
    assert smsenv.client.get("/healthz").json == {"ok": True}


def test_session_cookie_is_secure_httponly_and_revocable(tmp_path, master):
    from conftest import SmsEnv
    env = SmsEnv(tmp_path, master, gateway_cookie_secure=True)
    page = env.client.get("/login")
    cookie = page.headers.get("Set-Cookie", "")
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Strict-Transport-Security" in page.headers
    env2 = SmsEnv(tmp_path / "b", master)
    env2.login()
    app_id = env2.ready_application()
    code = links.issue(env2.auto, "application", app_id, 72)
    assert env2.client.get(f"/r/{code}").status_code == 200
    env2.store.set("gateway_session_epoch", 1)  # python -m autoapply.cli revoke-sessions
    assert env2.client.get(f"/r/{code}").status_code == 302


def test_summary_page_can_pause_and_resume(smsenv):
    smsenv.switch_on()
    smsenv.ready_application()
    code = links.issue(smsenv.auto, "summary", "2026-10-23", 72)
    smsenv.login()
    page = smsenv.client.get(f"/s/{code}").get_data(as_text=True)
    assert "Fixture Co" in page and "Approval Required" in page
    csrf = smsenv.csrf(f"/s/{code}")
    assert "Paused" in smsenv.client.post(f"/s/{code}/mode", data={"csrf": csrf, "action": "pause"}).get_data(as_text=True)
    assert smsenv.auto.mode() == "OFF"
    smsenv.client.post(f"/s/{code}/mode", data={"csrf": csrf, "action": "resume"})
    assert smsenv.auto.mode() == "REVIEW"
