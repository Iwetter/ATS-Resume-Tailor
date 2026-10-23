"""Public gateway: Twilio webhooks and the authenticated phone review flow.

This small Flask app is the only internet-facing component. It is served by
waitress behind a TLS-terminating reverse proxy (Caddy in docker-compose),
which forwards *only* these paths. The Streamlit dashboard is never proxied
and stays on loopback.

* ``POST /sms/inbound``, ``POST /sms/status`` — authenticated by Twilio's
  ``X-Twilio-Signature`` (HMAC-SHA1 with the auth token over the exact public
  URL and parameters). Invalid or missing signature → 403.
* ``/r/<code>`` (application), ``/j/<code>`` (job), ``/s/<code>`` (summary) —
  expiring, server-side link codes. Every page also requires a signed-in
  session (scrypt password, throttled), with CSRF tokens on every POST.
* Approving binds to the exact ``approval_hash`` shown on the page.

Run: ``python -m autoapply.gateway``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import json
import logging
from pathlib import Path
import re
import secrets

from flask import Flask, Response, abort, redirect, render_template_string, request, send_file, session

from hunter.storage import Store

from . import commands, config, controls, links
from . import db as S
from .engine import Engine
from .notify import ACTION, fit_of, reason_summary
from .redaction import configure_logging, log
from .sms import SmsService
from .webauth import LoginThrottle, verify_password

logger = logging.getLogger("autoapply.gateway")

LAYOUT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>{{ title }} · Auto Apply</title><style>
:root{--bg:#f7f8f5;--card:#fff;--ink:#182d2a;--muted:#5a6b67;--line:#d9e0da;--accent:#17635a;--warn:#8c5a00;--bad:#8c2420}
@media (prefers-color-scheme:dark){:root{--bg:#111816;--card:#1a2421;--ink:#e6eeeb;--muted:#9fb1ac;--line:#2c3a36;--accent:#5cc2b2;--warn:#e0b25c;--bad:#f08a80}}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:640px;margin:0 auto;padding:16px}h1{font-size:1.35rem;margin:.2rem 0}h2{font-size:1.05rem;margin:1.2rem 0 .4rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}.muted{color:var(--muted)}
.pill{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:1px 10px;font-size:.85rem;margin:2px 4px 2px 0}
.pass{color:var(--accent)}.unknown{color:var(--warn)}.fail{color:var(--bad)}table{width:100%;border-collapse:collapse;font-size:.9rem}
td{border-top:1px solid var(--line);padding:6px 4px;vertical-align:top}button,input{font:inherit}
button{border:0;border-radius:10px;padding:12px 16px;width:100%;margin:6px 0;cursor:pointer;background:var(--accent);color:#fff;font-weight:600}
button.secondary{background:transparent;color:var(--ink);border:1px solid var(--line)}input[type=password]{width:100%;box-sizing:border-box;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink)}
a{color:var(--accent);overflow-wrap:anywhere}img{max-width:100%;border:1px solid var(--line);border-radius:8px}.warn{color:var(--warn)}.bad{color:var(--bad)}
</style></head><body><main>{{ body|safe }}</main></body></html>"""

LOGIN = """<h1>Sign in</h1><div class="card">
{% if not configured %}<p class="bad">Sign-in is not configured. Set GATEWAY_PASSWORD_HASH on the server.</p>{% else %}
{% if error %}<p class="bad">{{ error }}</p>{% endif %}
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="next" value="{{ next }}">
<label>Password<input type="password" name="password" autocomplete="current-password" required autofocus></label><button>Sign in</button></form>{% endif %}</div>"""

APPLICATION = """<p class="muted">Auto Apply · application review</p><h1>{{ app.title }}</h1><p><strong>{{ app.company }}</strong></p>
<div class="card"><span class="pill">{{ app.state }}</span><span class="pill">{{ fit }}</span><span class="pill">mode: {{ mode }}</span>
<p>Action taken: {{ action }}</p>{% if why %}<p class="warn">Why: {{ why }}</p>{% endif %}
{% for r in app.reasons %}<p class="muted">• {{ r }}</p>{% endfor %}
{% if not real %}<p class="warn">Real submission is off on the server: approving records your decision but nothing is sent.</p>{% endif %}
{% if app.state == 'Ready for Review' %}
<form method="post" action="{{ request.path }}/decide"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="approval_hash" value="{{ app.approval_hash }}">
<button name="decision" value="approve">Approve this exact version</button><button class="secondary" name="decision" value="reject">Reject</button></form>
<p class="muted">Approval binds to this resume, this form's questions and these answer sources. Any change sends it back for review.</p>{% endif %}
{% if app.state == 'Unknown' %}<p class="bad">Submit was clicked but no confirmation was seen. Check your email or the employer portal; record the outcome on the dashboard.</p>{% endif %}
{% if app.state == 'Needs Human Action' and app.apply_url %}<p>Finish it yourself: <a href="{{ app.apply_url }}" rel="noreferrer noopener" target="_blank">open the employer application page</a>. The tailored resume is below.</p>{% endif %}
</div>
<div class="card"><h2>Files</h2>{% if has_pdf %}<p><a href="{{ request.path }}/resume.pdf">Tailored resume (PDF)</a></p>{% endif %}
{% if has_shot %}<p class="muted">Form as prepared:</p><img alt="Prepared application form" src="{{ request.path }}/screenshot.png">{% endif %}
{% if not has_pdf and not has_shot %}<p class="muted">Nothing prepared yet.</p>{% endif %}</div>
{% if fields %}<div class="card"><h2>Questions and answer sources</h2><table>{% for f in fields %}<tr><td>{{ f.label }}{% if f.required %} *{% endif %}</td>
<td class="{{ 'pass' if f.source not in ('unresolved',) else 'fail' }}">{{ f.source }}</td></tr>{% endfor %}</table>
<p class="muted">Values are not shown here; they come only from your verified profile and answer bank.</p></div>{% endif %}
{% if checks %}<div class="card"><h2>Fit and eligibility</h2><table>{% for c in checks %}<tr><td class="{{ c.status }}">{{ c.status }}</td><td><strong>{{ c.name }}</strong><br><span class="muted">{{ c.detail }}</span></td></tr>{% endfor %}</table></div>{% endif %}"""

JOB = """<p class="muted">Auto Apply · new match</p><h1>{{ job.title }}</h1><p><strong>{{ job.company }}</strong> · {{ job.location }}</p>
<div class="card"><span class="pill">fit {{ fit }}</span><p>{{ note }}</p><p><a href="{{ job.url }}" rel="noreferrer noopener" target="_blank">Official posting</a></p></div>
{% if checks %}<div class="card"><h2>Fit and eligibility</h2><table>{% for c in checks %}<tr><td class="{{ c.status }}">{{ c.status }}</td><td><strong>{{ c.name }}</strong><br><span class="muted">{{ c.detail }}</span></td></tr>{% endfor %}</table></div>{% endif %}"""

SUMMARY = """<p class="muted">Auto Apply · summary</p><h1>Applications</h1>
<div class="card"><p>Mode: <strong>{{ mode }}</strong>{% if stopped %} · <span class="bad">EMERGENCY STOP</span>{% endif %}</p>
<p class="muted">Last 24h: {{ stats.new_matches }} new high-match · {{ stats.submitted_24h }}/{{ stats.cap }} submissions · {{ stats.awaiting_approval }} awaiting approval · {{ stats.needs_action }} need action · {{ stats.uncertain }} uncertain</p>
<form method="post" action="{{ request.path }}/mode"><input type="hidden" name="csrf" value="{{ csrf }}">
{% if paused %}<button name="action" value="resume">Resume</button>{% else %}<button class="secondary" name="action" value="pause">Pause new submissions</button>{% endif %}</form>
{% if message %}<p>{{ message }}</p>{% endif %}</div>
{% for a in apps %}<div class="card"><a href="{{ a.link }}"><strong>{{ a.company }}</strong> – {{ a.title }}</a><br>
<span class="pill">{{ a.state }}</span><span class="pill">{{ a.fit }}</span><br><span class="muted">{{ a.action }}</span></div>{% else %}<div class="card">No applications yet.</div>{% endfor %}"""


def _within(path, folder) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(folder).resolve()) and Path(path).is_file()
    except (OSError, ValueError, TypeError):
        return False


def create_app(settings=None, store=None, engine=None, sms=None):
    settings = settings or config.load()
    if len(settings.gateway_secret_key) < 32:
        raise RuntimeError("Set GATEWAY_SECRET_KEY to at least 32 random characters (python -c \"import secrets;print(secrets.token_urlsafe(48))\").")
    store = store or Store(settings.db_path)
    engine = engine or Engine(store, settings)
    sms = sms or SmsService(store, settings)
    auto = engine.auto
    throttle = LoginThrottle(auto)

    app = Flask("autoapply.gateway")
    app.config.update(SECRET_KEY=settings.gateway_secret_key, SESSION_COOKIE_NAME="aa_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SECURE=settings.gateway_cookie_secure, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=settings.gateway_session_hours), MAX_CONTENT_LENGTH=64 * 1024)
    app.extensions["autoapply"] = {"engine": engine, "sms": sms, "store": store}

    def page(title, template, status=200, **context):
        body = render_template_string(template, request=request, **context)
        return render_template_string(LAYOUT, title=title, body=body), status

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def check_csrf():
        sent = request.form.get("csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400)

    def signed_in():
        return bool(session.get("auth")) and session.get("epoch") == (store.get("gateway_session_epoch") or 0)

    def require_login():
        if not signed_in():
            return redirect("/login?next=" + request.path)
        return None

    def twilio_valid():
        from twilio.request_validator import RequestValidator
        if not (settings.twilio_auth_token and settings.public_base_url.startswith("https://")):
            return False
        url = settings.public_base_url + request.path + ("?" + request.query_string.decode() if request.query_string else "")
        return RequestValidator(settings.twilio_auth_token).validate(url, request.form, request.headers.get("X-Twilio-Signature", ""))

    @app.after_request
    def headers(resp):
        resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"  # link codes never leak in Referer
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if settings.gateway_cookie_secure:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---- Twilio ----------------------------------------------------------------
    @app.post("/sms/inbound")
    def sms_inbound():
        from twilio.twiml.messaging_response import MessagingResponse
        if not twilio_valid():
            auto.audit("gateway", "sms_signature_rejected", details={"path": request.path})
            abort(403)
        try:
            reply = commands.handle(engine, sms, request.form.to_dict())
        except Exception as exc:
            log(logger, logging.ERROR, "inbound command failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
            reply = "Sorry, that command failed on the server. Nothing was changed if no confirmation followed."
        twiml = MessagingResponse()
        if reply:
            twiml.message(reply)
        return Response(str(twiml), mimetype="application/xml")

    @app.post("/sms/status")
    def sms_status():
        if not twilio_valid():
            abort(403)
        sms.record_status(request.form.get("MessageSid", ""), request.form.get("MessageStatus", ""), request.form.get("ErrorCode"))
        return "", 204

    # ---- sign-in -------------------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        target = request.values.get("next", "/")
        if not re.fullmatch(r"/[a-z]/[a-z]{1,40}", target):
            target = ""
        error = ""
        if request.method == "POST":
            check_csrf()
            client = request.remote_addr or "?"
            if not throttle.allowed(client):
                error = "Too many attempts. Wait 15 minutes."
            elif settings.gateway_password_hash and verify_password(request.form.get("password", ""), settings.gateway_password_hash):
                throttle.record(client, True)
                session.clear()
                session.permanent = True
                session.update(auth=True, epoch=store.get("gateway_session_epoch") or 0, csrf=secrets.token_urlsafe(32), at=datetime.now(timezone.utc).isoformat())
                auto.audit("gateway", "gateway_login", details={"ok": True})
                return redirect(target or "/signed-in")
            else:
                throttle.record(client, False)
                auto.audit("gateway", "gateway_login", details={"ok": False})
                error = "Wrong password."
        return page("Sign in", LOGIN, 200 if not error else 401, csrf=csrf_token(), next=target, error=error, configured=bool(settings.gateway_password_hash))

    @app.get("/signed-in")
    def signed_in_page():
        if (redirect_to := require_login()) is not None:
            return redirect_to
        return page("Signed in", "<h1>Signed in</h1><div class='card'>Open a review link from your texts.</div>")

    @app.post("/logout")
    def logout():
        check_csrf()
        session.clear()
        return redirect("/login")

    # ---- review pages -------------------------------------------------------------------
    def application_for(code):
        link = links.resolve(auto, "application", code)
        row = auto.application(link["target"]) if link else None
        if not row:
            abort(404)
        return row

    @app.get("/r/<code>")
    def review(code):
        if (redirect_to := require_login()) is not None:
            return redirect_to
        row = application_for(code)
        plan = row["plan"] or {}
        fields = (plan.get("last_run") or {}).get("fields", [])
        return page(row["company"], APPLICATION, app=row, fit=fit_of(row), mode=controls.describe_mode(engine), action=ACTION.get(row["state"], ""),
                    why=reason_summary(row), real=engine.settings.allow_real_submission, csrf=csrf_token(), fields=fields,
                    checks=(row["gates"] or {}).get("checks", []), has_pdf=_within(plan.get("pdf", ""), settings.data_dir),
                    has_shot=_within((plan.get("last_run") or {}).get("screenshot", ""), settings.data_dir))

    @app.post("/r/<code>/decide")
    def decide(code):
        if (redirect_to := require_login()) is not None:
            return redirect_to
        check_csrf()
        row = application_for(code)
        try:
            if request.form.get("decision") == "approve":
                engine.approve(row["id"], request.form.get("approval_hash", ""), actor="phone")
            elif request.form.get("decision") == "reject":
                engine.cancel(row["id"], actor="phone")
            else:
                abort(400)
        except ValueError as exc:
            return page("Not changed", "<h1>Not changed</h1><div class='card'>{{ m }}</div><p><a href='{{ back }}'>Back</a></p>", 409, m=str(exc), back=f"/r/{code}")
        return redirect(f"/r/{code}")

    @app.get("/r/<code>/resume.pdf")
    def resume_pdf(code):
        if (redirect_to := require_login()) is not None:
            return redirect_to
        path = (application_for(code)["plan"] or {}).get("pdf", "")
        if not _within(path, settings.data_dir):
            abort(404)
        return send_file(path, mimetype="application/pdf", download_name="tailored-resume.pdf")

    @app.get("/r/<code>/screenshot.png")
    def screenshot(code):
        if (redirect_to := require_login()) is not None:
            return redirect_to
        path = ((application_for(code)["plan"] or {}).get("last_run") or {}).get("screenshot", "")
        if not _within(path, settings.data_dir):
            abort(404)
        return send_file(path, mimetype="image/png")

    @app.get("/j/<code>")
    def job(code):
        if (redirect_to := require_login()) is not None:
            return redirect_to
        link = links.resolve(auto, "job", code)
        record = store.job(link["target"]) if link else None
        if not record:
            abort(404)
        payload = json.loads(record["payload"])
        decision = (auto.decisions(link["target"], limit=1) or [{}])[0]
        url = payload.get("url", "") if str(payload.get("url", "")).startswith("https://") else ""
        return page(payload["company"], JOB, job={**payload, "url": url}, fit=decision.get("fit", "n/a"),
                    checks=(decision.get("details") or {}).get("checks", []),
                    note="Not queued: approve this employer for form preparation (dashboard → Employers) or apply from the official posting.")

    @app.route("/s/<code>", methods=["GET"])
    def summary(code, message=""):
        if (redirect_to := require_login()) is not None:
            return redirect_to
        if not links.resolve(auto, "summary", code):
            abort(404)
        since = datetime.now(timezone.utc) - timedelta(days=7)
        rows = [a for a in auto.applications(limit=200) if a["updated_at"] >= since.isoformat() or a["state"] in (S.READY, S.NEEDS_HUMAN, S.UNKNOWN)]
        apps = [{"company": a["company"], "title": a["title"], "state": a["state"], "fit": fit_of(a), "action": ACTION.get(a["state"], ""),
                 "link": "/r/" + links.issue(auto, "application", a["id"], settings.review_link_ttl_hours)} for a in rows]
        return page("Summary", SUMMARY, mode=controls.describe_mode(engine), stopped=engine.stopped(), paused=auto.mode() == "OFF",
                    stats=controls.activity(engine, datetime.now(timezone.utc) - timedelta(days=1)), apps=apps, csrf=csrf_token(), message=message)

    @app.post("/s/<code>/mode")
    def summary_mode(code):
        if (redirect_to := require_login()) is not None:
            return redirect_to
        check_csrf()
        if not links.resolve(auto, "summary", code):
            abort(404)
        try:
            if request.form.get("action") == "pause":
                message = controls.pause(engine, actor="phone")
            elif request.form.get("action") == "resume":
                message = controls.resume(engine, actor="phone")[1]
            else:
                abort(400)
        except ValueError as exc:
            message = str(exc)
        return summary(code, message=message)

    return app


def main():
    settings = config.load()
    configure_logging(settings.log_level)
    from waitress import serve
    import os
    app = create_app(settings)
    if not settings.public_base_url.startswith("https://"):
        log(logger, logging.WARNING, "PUBLIC_BASE_URL is not an https:// URL; Twilio webhooks will be rejected and review links are disabled")
    proxy = os.environ.get("GATEWAY_TRUSTED_PROXY", "")
    options = {"trusted_proxy": proxy, "trusted_proxy_headers": "x-forwarded-for x-forwarded-proto", "trusted_proxy_count": 1} if proxy else {}
    log(logger, logging.INFO, "gateway listening", host=settings.gateway_host, port=settings.gateway_port)
    serve(app, host=settings.gateway_host, port=settings.gateway_port, threads=8, ident="", clear_untrusted_proxy_headers=True, **options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
