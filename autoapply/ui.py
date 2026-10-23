"""Auto Apply workspace for the existing Streamlit app."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from hunter.models import Job, utcnow
from hunter.service import DEFAULT_DB, WATCHLIST
from hunter.storage import Store

from . import config
from . import db as S
from .engine import Engine, employer_key
from .forms import attestation_hash
from .llm import LLMError
from .modes import CONFIRM_PHRASE, enable_auto, run_dry_run
from .profile import TERMS, ApplicantProfile, load_profile, save_profile, validate_profile, verify_profile
from .worker import health


def _age(ts):
    if not ts:
        return "never"
    seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    if seconds < 0:
        return "in " + _span(-seconds)
    return _span(seconds) + " ago"


def _span(seconds):
    seconds = int(seconds)
    return f"{seconds // 86400}d" if seconds >= 86400 else f"{seconds // 3600}h {seconds % 3600 // 60}m" if seconds >= 3600 else f"{seconds // 60}m"


def _engine():
    path = st.session_state.get("hunter_db_path", DEFAULT_DB)
    settings = config.load(db_path=Path(path))
    store = Store(settings.db_path)
    return Engine(store, settings), settings, store


def render():
    engine, settings, store = _engine()
    auto = engine.auto
    st.markdown('<div class="eyebrow">RESUME BENCH / AUTO APPLY</div>', unsafe_allow_html=True)
    st.title("Apply only where the evidence and the rules allow.")
    st.write("Always-on discovery, evidence-grounded tailoring, and guarded form completion for Greenhouse and Lever candidate pages.")
    stopped = engine.stopped()
    if stopped:
        st.error("EMERGENCY STOP ACTIVE — no application step will run.")
    if not settings.allow_real_submission:
        st.info("Real submission is disabled on this server (AUTOAPPLY_ALLOW_REAL_SUBMISSION=false). Forms are prepared for review only.")
    tabs = st.tabs(["Control", "Applicant profile", "Employers", "Queue", "Submitted", "Blocked & failed", "Discovery", "API usage", "Audit"])
    with tabs[0]:
        _control(engine, settings, store, auto, stopped)
    with tabs[1]:
        _profile(engine, store, auto)
    with tabs[2]:
        _employers(store, auto)
    with tabs[3]:
        _queue(engine, store, auto, [S.READY, S.APPROVED, S.QUEUED, S.PREPARING, S.SUBMITTING], "queue")
    with tabs[4]:
        _submitted(auto)
    with tabs[5]:
        _queue(engine, store, auto, [S.NEEDS_HUMAN, S.UNKNOWN, S.FAILED, S.BLOCKED, S.SKIPPED, S.CANCELLED], "blocked")
    with tabs[6]:
        _discovery(store, auto, settings)
    with tabs[7]:
        _usage(engine, settings, auto)
    with tabs[8]:
        _audit(auto)


# ---- control ---------------------------------------------------------------------

def _control(engine, settings, store, auto, stopped):
    ok, beat = health(store, settings)
    heartbeat = store.get("worker_heartbeat") or {}
    a, b, c, d = st.columns(4)
    a.metric("Mode", auto.mode() + (" (active)" if engine.auto_active() else ""))
    b.metric("Master switch", "On" if auto.enabled() else "Off")
    c.metric("Worker", "Healthy" if ok else "Not running", heartbeat.get("state", "—"))
    d.metric("Last heartbeat", _age(heartbeat.get("at")))
    st.caption(f"Scan interval {settings.scan_interval // 60} min (Workday {settings.workday_interval // 60} min) · caps {settings.max_per_day}/day, "
               f"{settings.max_per_company_per_day}/company/day, {settings.max_per_company_total}/company total · fit ≥ {settings.min_fit_review:g} to prepare, ≥ {settings.min_fit_auto:g} for AUTO.")
    left, right = st.columns(2)
    with left:
        st.markdown("**Emergency stop**")
        if stopped:
            if st.button("Clear emergency stop", key="aa_clear_stop"):
                auto.set_emergency_stop(False, reason="dashboard")
                st.rerun()
            if settings.stop_file.exists() or settings.emergency_stop_env:
                st.caption("A STOP file or AUTOAPPLY_EMERGENCY_STOP is also set on the server and must be removed there.")
        elif st.button("STOP ALL APPLICATIONS", type="primary", key="aa_stop"):
            auto.set_emergency_stop(True, reason="dashboard")
            st.rerun()
        enabled = st.toggle("Auto Apply master switch", value=auto.enabled(), key="aa_master")
        if enabled != auto.enabled():
            auto.set_enabled(enabled)
            st.rerun()
        mode = st.radio("Mode", ["OFF", "REVIEW"], index=0 if auto.mode() == "OFF" else 1, horizontal=True, key="aa_mode",
                        help="OFF: nothing is prepared or submitted. REVIEW (default): forms are prepared; you approve each submission. AUTO is enabled separately below.")
        if auto.mode() != "AUTO" and mode != auto.mode():
            auto.set_mode(mode)
            st.rerun()
        if auto.mode() == "AUTO" and st.button("Return to REVIEW mode", key="aa_review"):
            auto.set_mode("REVIEW")
            st.rerun()
    with right:
        st.markdown("**AUTO mode (separate explicit action)**")
        st.caption("Requires a passing dry run against fictional mocked forms in a sandboxed browser, a verified profile, and per-employer auto-submit permission. Any profile or resume change returns AUTO to inactive.")
        if st.button("Run dry-run validation", key="aa_dry"):
            with st.spinner("Running sandboxed dry run…"):
                run_dry_run(engine)
            st.rerun()
        dry = store.get("dryrun_validation")
        if dry:
            (st.success if dry["ok"] else st.error)(f"Dry run {'passed' if dry['ok'] else 'failed'} {_age(dry['at'])}.")
            st.dataframe(pd.DataFrame(dry["checks"]), hide_index=True, width="stretch")
        typed = st.text_input(f"Type {CONFIRM_PHRASE} to enable AUTO", key="aa_confirm")
        if st.button("Enable AUTO", disabled=not (dry and dry.get("ok")), key="aa_enable_auto"):
            try:
                enable_auto(engine, typed)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


# ---- profile ------------------------------------------------------------------------

def _profile(engine, store, auto):
    master = store.master()
    profile = load_profile(store)
    if master is None:
        st.warning("Save a verified master resume in Internship Hunter first; the profile is checked against it.")
    verified = profile.is_verified(master.source_hash if master else None)
    (st.success if verified else st.warning)("Profile verified " + _age(profile.verified_at) if verified else "Profile not verified — submission is disabled.")
    st.caption("Stored only in the private local database. Values are never sent to Claude; answers come only from here and your verified answer bank.")
    yn = [True, False, None]
    fmt = lambda v: "Yes" if v is True else "No" if v is False else "Not answered"
    with st.form("aa_profile"):
        c1, c2, c3 = st.columns(3)
        first = c1.text_input("First name", profile.first_name)
        last = c2.text_input("Last name", profile.last_name)
        preferred = c3.text_input("Preferred name (optional)", profile.preferred_name)
        email = c1.text_input("Email", profile.email)
        phone = c2.text_input("Phone", profile.phone)
        city = c3.text_input("City", profile.city)
        state = c1.text_input("State", profile.state)
        linkedin = c2.text_input("LinkedIn URL (optional)", profile.linkedin)
        github = c3.text_input("GitHub URL (optional)", profile.github)
        website = c1.text_input("Website (optional)", profile.website)
        school = c2.text_input("School", profile.school)
        degree = c3.text_input("Degree (e.g. Bachelor of Science)", profile.degree)
        major = c1.text_input("Major", profile.major)
        graduation = c2.text_input("Expected graduation (YYYY-MM)", profile.graduation)
        gpa = c3.text_input("GPA (blank = leave GPA questions for review)", profile.gpa)
        citizen = c1.selectbox("U.S. citizen", yn, index=yn.index(profile.us_citizen), format_func=fmt)
        authorized = c2.selectbox("Authorized to work in the U.S.", yn, index=yn.index(profile.work_authorized), format_func=fmt)
        sponsor = c3.selectbox("Will need visa sponsorship", yn, index=yn.index(profile.sponsorship_needed), format_func=fmt)
        relocate = c1.selectbox("Willing to relocate", yn, index=yn.index(profile.willing_to_relocate), format_func=fmt)
        terms = c2.multiselect("Terms you are verified available for", list(TERMS), default=list(profile.available_terms))
        start = c3.text_input("Earliest start date (YYYY-MM-DD; required for Spring)", profile.earliest_start)
        if st.form_submit_button("Save profile (clears verification)"):
            save_profile(store, ApplicantProfile(first, last, preferred, email, phone, city, state, "United States", linkedin, github, website,
                                                 school, degree, major, graduation, gpa, citizen, authorized, sponsor, tuple(terms), start, relocate), auto.audit)
            st.rerun()
    issues = validate_profile(profile, master)
    for issue in issues:
        (st.error if issue["level"] == "error" else st.warning)(issue["message"])
    confirm = st.checkbox("I confirm every value above is accurate and matches my verified resume", key="aa_profile_ok")
    if st.button("Verify profile", disabled=not confirm or any(i["level"] == "error" for i in issues), key="aa_verify"):
        try:
            verify_profile(store, master, auto.audit)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    st.divider()
    st.markdown("**Verified answer bank**")
    st.caption("Exact question text (or `re:` + a regular expression) → your answer. Only verified answers are used. Claude may suggest which verified answer fits a new question, but never writes answers.")
    answers = auto.answers()
    if answers:
        st.dataframe(pd.DataFrame([{"id": a["id"], "question": a["question"], "answer": a["answer"], "verified": bool(a["verified_at"]), "source": a["source"]} for a in answers]), hide_index=True, width="stretch")
    with st.form("aa_answer", clear_on_submit=True):
        q = st.text_input("Question")
        ans = st.text_area("Answer", height=80)
        ok = st.checkbox("This answer is true and I approve its use in applications")
        if st.form_submit_button("Save answer"):
            try:
                auto.save_answer(q, ans, verified=ok)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    if answers:
        remove = st.selectbox("Delete answer", [""] + [a["id"] for a in answers], key="aa_del_answer")
        if remove and st.button("Delete selected answer", key="aa_del_btn"):
            auto.delete_answer(remove)
            st.rerun()


# ---- employers -------------------------------------------------------------------------

def _employers(store, auto):
    st.caption("Default: no employer is permitted. Enable form preparation only after checking the employer's career site and its ATS terms allow automated assistance; automatic submission additionally requires that review.")
    rows = []
    for employer in store.employers(WATCHLIST):
        if employer.source not in ("greenhouse", "lever"):
            continue
        perm = auto.permission(employer.key)
        rows.append({"employer_key": employer.key, "name": employer.name, "prepare forms": perm["prefill"],
                     "terms reviewed": bool(perm["terms_reviewed_at"]), "auto-submit": perm["auto_submit"], "notes": perm.get("notes", "")})
    if not rows:
        st.info("No Greenhouse or Lever employers on the watchlist.")
        return
    edited = st.data_editor(pd.DataFrame(rows), hide_index=True, disabled=["employer_key", "name"], key="aa_perm_editor", width="stretch")
    if st.button("Save employer permissions", key="aa_perm_save"):
        try:
            for row in edited.to_dict("records"):
                auto.set_permission(row["employer_key"], row["name"], prefill=bool(row["prepare forms"]), auto_submit=bool(row["auto-submit"]),
                                    terms_reviewed=bool(row["terms reviewed"]), notes=str(row["notes"] or ""))
            st.success("Saved.")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    approved = [(r["name"], a) for r in rows for a in auto.permission(r["employer_key"])["approved_attestations"]]
    if approved:
        st.markdown("**Approved attestation texts**")
        st.dataframe(pd.DataFrame([{"employer": n, "hash": a["hash"], "text": a["text"], "approved": a["approved_at"]} for n, a in approved]), hide_index=True)


# ---- queue / details ----------------------------------------------------------------------

def _queue(engine, store, auto, states, key):
    apps = auto.applications(states)
    if not apps:
        st.info("Nothing here yet.")
        return
    st.dataframe(pd.DataFrame([{"State": a["state"], "Company": a["company"], "Title": a["title"], "Fit": a["gates"].get("fit"),
                                "Updated": a["updated_at"], "Reason": (a["reasons"] or [a["last_error"]])[0][:160] if (a["reasons"] or a["last_error"]) else ""} for a in apps]),
                 hide_index=True, width="stretch")
    labels = {a["id"]: f"{a['state']} · {a['company']} · {a['title']}" for a in apps}
    chosen = st.selectbox("Open application", list(labels), format_func=labels.get, key="aa_open_" + key)
    _detail(engine, store, auto, auto.application(chosen), key)


def _detail(engine, store, auto, app, key):
    record = store.job(app["job_key"])
    job = Job(**json.loads(record["payload"])) if record else None
    plan = app["plan"] or {}
    st.subheader(app["title"])
    st.write(f"{app['company']} · state **{app['state']}** · adapter {app['adapter'] or '—'} · attempts {app['attempts']}")
    if app.get("apply_url"):
        st.link_button("Open employer application page", app["apply_url"])
    for reason in app["reasons"]:
        st.warning(reason)
    if app["last_error"]:
        st.error(app["last_error"])
    gates = app["gates"] or {}
    with st.expander("Fit & eligibility explanation", expanded=app["state"] in (S.READY, S.NEEDS_HUMAN)):
        if gates.get("checks"):
            st.dataframe(pd.DataFrame(gates["checks"])[["status", "name", "detail"]], hide_index=True, width="stretch")
        st.caption(f"Fit before tailoring {plan.get('fit_before')} → after {plan.get('fit_after')} (internal estimate, not an employer ATS result).")
    if plan.get("pdf") and Path(plan["pdf"]).is_file():
        with st.expander("Tailored resume preview"):
            import pymupdf
            with pymupdf.open(plan["pdf"]) as pdf:
                st.image(pdf[0].get_pixmap(matrix=pymupdf.Matrix(1.3, 1.3)).tobytes("png"))
            st.caption("Projects: " + ", ".join(plan.get("projects", [])) + " · basis: " + plan.get("selection_basis", "") +
                       (" · omitted: " + ", ".join(plan.get("omitted_projects", [])) if plan.get("omitted_projects") else ""))
            st.download_button("Download tailored PDF", Path(plan["pdf"]).read_bytes(), "tailored-resume.pdf", key="aa_pdf_" + key + app["id"])
            pending = [p for p in plan.get("proposals", []) if p["validation"] == "review"]
            if pending:
                st.markdown("**Claude wording proposals (validated, awaiting your approval)**")
                picks = []
                for p in pending:
                    st.write("Original: " + p["original_text"])
                    st.write("Proposed: " + p["proposed_text"])
                    if st.checkbox("Approve this wording", value=p["source_ids"][0] in plan.get("approved_change_ids", []), key="aa_rw_" + key + app["id"] + p["source_ids"][0]):
                        picks.append(p["source_ids"][0])
                if st.button("Rebuild resume with approved wording", key="aa_rebuild_" + key + app["id"]):
                    auto.update(app["id"], actor="user", action="rewrites_approved", plan={**plan, "approved_change_ids": picks, "artifact_key": None})
                    engine.requeue(app["id"], reason="Resume rebuild with approved wording")
                    st.rerun()
            rejected = [p for p in plan.get("proposals", []) if p["validation"] == "rejected"]
            if rejected:
                st.caption(f"{len(rejected)} Claude proposal(s) rejected automatically for adding or removing facts.")
    run = plan.get("last_run") or {}
    if run:
        with st.expander("Form answers & screenshot", expanded=app["state"] in (S.READY, S.NEEDS_HUMAN)):
            st.dataframe(pd.DataFrame(run.get("fields", []))[["label", "required", "concept", "source", "value", "reason"]] if run.get("fields") else pd.DataFrame(), hide_index=True, width="stretch")
            if run.get("screenshot") and Path(run["screenshot"]).is_file():
                st.image(run["screenshot"], caption="Filled form before submission")
            for field in run.get("fields", []):
                if field["concept"] == "attestation" and field["required"] and field["source"] == "unresolved":
                    st.write("Attestation: " + field["label"])
                    if st.button("I have read this and approve checking it for this employer", key="aa_att_" + key + app["id"] + field["fid"]):
                        auto.approve_attestation(app["employer_key"], app["company"], attestation_hash(field["label"]), field["label"])
                        engine.requeue(app["id"], reason="Attestation approved")
                        st.rerun()
    _cover_letter(engine, store, auto, app, job, plan, key)
    c1, c2, c3 = st.columns(3)
    if app["state"] == S.READY:
        st.caption("Approval binds to this exact resume, form field set and answer set. Any change sends it back for review.")
        if c1.button("Approve submission", type="primary", key="aa_approve_" + key + app["id"]):
            engine.approve(app["id"], app["approval_hash"])
            st.rerun()
    if app["state"] in (S.READY, S.NEEDS_HUMAN, S.FAILED, S.BLOCKED, S.SKIPPED, S.APPROVED) and c2.button("Re-prepare", key="aa_req_" + key + app["id"]):
        engine.requeue(app["id"])
        st.rerun()
    if app["state"] not in (S.SUBMITTING, S.APPLIED, S.UNKNOWN, S.CANCELLED) and c3.button("Cancel application", key="aa_cancel_" + key + app["id"]):
        engine.cancel(app["id"])
        st.rerun()
    if app["state"] == S.UNKNOWN:
        st.error("Submission outcome is uncertain. Check your email and the employer portal before doing anything else.")
        note = st.text_input("What did you verify?", key="aa_unknown_note_" + app["id"])
        u1, u2 = st.columns(2)
        if u1.button("It was submitted — mark Applied", key="aa_unk_yes_" + app["id"]):
            try:
                engine.resolve_unknown(app["id"], True, note)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if u2.button("It was NOT submitted — re-prepare", key="aa_unk_no_" + app["id"]):
            try:
                engine.resolve_unknown(app["id"], False, note)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with st.expander("Audit history for this application"):
        st.dataframe(pd.DataFrame(auto.audit_log(app["id"])), hide_index=True, width="stretch")


def _cover_letter(engine, store, auto, app, job, plan, key):
    if app["state"] in (S.APPLIED, S.SUBMITTING, S.CANCELLED) or job is None:
        return
    letter = plan.get("cover_letter") or {}
    with st.expander("Cover letter (optional, human-approved only)"):
        if letter.get("problems"):
            for p in letter["problems"]:
                st.warning(p)
        if st.button("Draft with Claude from cited resume evidence", disabled=not engine.claude.configured()[0], key="aa_cl_draft_" + key + app["id"]):
            try:
                from hunter.service import score_job
                master = store.master()
                match = score_job(job, master, store) or {}
                ids = [e["id"] for e in match.get("strongest_evidence", [])][:6]
                profile = load_profile(store)
                paragraphs, problems, _ = engine.claude.cover_letter(master, job, ids, profile.school, profile.major)
                auto.update(app["id"], actor="user", action="cover_letter_drafted",
                            plan={**plan, "cover_letter": {"text": "\n\n".join(p["text"] for p in paragraphs), "approved": False, "problems": problems}})
                st.rerun()
            except LLMError as exc:
                st.error(str(exc))
        if not engine.claude.configured()[0]:
            st.caption(engine.claude.configured()[1])
        text = st.text_area("Cover letter text", letter.get("text", ""), height=220, key="aa_cl_text_" + key + app["id"])
        if st.button("Approve this cover letter for this application", disabled=not text.strip(), key="aa_cl_ok_" + key + app["id"]):
            auto.update(app["id"], actor="user", action="cover_letter_approved", plan={**plan, "cover_letter": {"text": text.strip(), "approved": True, "approved_at": utcnow(), "problems": letter.get("problems", [])}})
            if app["state"] in (S.READY, S.NEEDS_HUMAN):
                engine.requeue(app["id"], reason="Cover letter approved")
            st.rerun()


def _submitted(auto):
    apps = auto.applications([S.APPLIED])
    if not apps:
        st.info("No confirmed applications yet. Applied requires captured confirmation evidence or your explicit verification.")
        return
    for app in apps:
        with st.expander(f"{app['company']} · {app['title']} · {app['submitted_at'] or app['updated_at']}"):
            conf = app["confirmation"]
            st.write("Confirmation: " + (conf.get("matched_text") or conf.get("human_verification") or "—"))
            st.write("Page: " + str(conf.get("url", "")))
            if conf.get("screenshot") and Path(conf["screenshot"]).is_file():
                st.image(conf["screenshot"])


def _discovery(store, auto, settings):
    boards = auto.board_status()
    if boards:
        st.dataframe(pd.DataFrame([{"Employer": b["name"], "Last success": _age(b["last_success"]), "Data checked": _age(b["last_checked"]),
                                    "Next scan": _age(b["next_due"]), "Failures": b["consecutive_failures"], "Postings": b["jobs"],
                                    "New last scan": b["new_jobs"], "Error": b["last_error"][:160]} for b in boards]), hide_index=True, width="stretch")
    else:
        st.info("The worker has not scanned yet. Start it with `python -m autoapply.worker` (or docker compose).")
    st.caption("First discovery and verified publication are tracked separately; updates and republications are recorded as events, never as new postings.")
    events = store.events(limit=200)
    if events:
        st.markdown("**Recent job events**")
        jobs = {j["key"]: json.loads(j["payload"]) for j in store.jobs()}
        st.dataframe(pd.DataFrame([{"At": e["at"], "Event": e["event"], "Company": jobs.get(e["job_key"], {}).get("company", ""),
                                    "Title": jobs.get(e["job_key"], {}).get("title", ""), "Details": json.dumps(e["details"])[:160]} for e in events]), hide_index=True, width="stretch")
    decisions = auto.decisions(limit=200)
    if decisions:
        st.markdown("**Recent decisions**")
        st.dataframe(pd.DataFrame([{"At": d["at"], "Trigger": d["trigger"], "Job": d["job_key"][:10], "Prepare": bool(d["prepare_ok"]), "Auto": bool(d["auto_ok"]),
                                    "Fit": d["fit"], "Why": "; ".join(f"{c['name']}: {c['detail']}" for c in d["details"]["checks"] if c["status"] != "pass")[:200]} for d in decisions]),
                     hide_index=True, width="stretch")


def _usage(engine, settings, auto):
    ok, why = engine.claude.configured()
    (st.success if ok else st.info)("Claude: " + why)
    now = datetime.now(timezone.utc)
    day, month = auto.llm_spent(now - timedelta(days=1)), auto.llm_spent(now - timedelta(days=30))
    a, b = st.columns(2)
    a.metric("Spend, last 24 h", f"${day:.4f}", f"limit ${settings.claude_daily_usd:.2f}", delta_color="off")
    b.metric("Spend, last 30 days", f"${month:.4f}", f"limit ${settings.claude_monthly_usd:.2f}", delta_color="off")
    st.caption("Costs use the per-million-token prices you configure (CLAUDE_INPUT_USD_PER_MTOK / CLAUDE_OUTPUT_USD_PER_MTOK); verify them against your Anthropic console.")
    rows = auto.llm_usage()
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if st.button("Allow previously failed Claude requests to be retried", key="aa_llm_clear"):
        engine.claude.clear_failure()
        auto.audit("user", "llm_failures_cleared")
        st.rerun()


def _audit(auto):
    rows = auto.audit_log(limit=1000)
    if not rows:
        st.info("No audit entries yet.")
        return
    actions = sorted({r["action"] for r in rows})
    chosen = st.multiselect("Filter actions", actions, key="aa_audit_filter")
    rows = [r for r in rows if not chosen or r["action"] in chosen]
    st.dataframe(pd.DataFrame([{**r, "details": json.dumps(r["details"])[:300]} for r in rows]), hide_index=True, width="stretch")
