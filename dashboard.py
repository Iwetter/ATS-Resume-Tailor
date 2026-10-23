"""Dashboard workspace: the state of discovery, applications and the worker at a glance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from hunter.service import DEFAULT_DB


def _go(page):
    # The sidebar radio already exists in this run; app.py applies this before drawing it next run.
    st.session_state.navigate_to = page
    st.rerun()


def render():
    from autoapply import config, controls
    from autoapply import db as S
    from autoapply.engine import Engine
    from autoapply.sms import SmsService, mask
    from autoapply.worker import health
    from hunter.models import Filters, Profile
    from hunter.service import evaluate
    from hunter.storage import Store
    from dataclasses import asdict

    store = Store(st.session_state.get("hunter_db_path", DEFAULT_DB))
    settings = config.load(db_path=store.path)
    engine = Engine(store, settings)
    st.markdown('<div class="eyebrow">RESUME BENCH / DASHBOARD</div>', unsafe_allow_html=True)
    st.title("Where your search stands")
    if engine.stopped():
        st.error("EMERGENCY STOP ACTIVE — no application step will run.")
    master = store.master()
    profile = Profile(**store.get("profile", asdict(Profile())))
    values = evaluate(store, master, profile, Filters(7))
    relevant = [x for x in values if x["decision"].eligible and x["job"].source_status != "closed"]
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    a, b, c, d = st.columns(4)
    a.metric("Relevant now", len(relevant))
    b.metric("Published, 7 days", sum(x["decision"].recency == "recent" for x in relevant))
    c.metric("Discovered, 7 days", sum(x["record"]["discovered_at"] >= week for x in relevant))
    d.metric("Strong matches", sum(1 for x in relevant if x["match"] and x["match"]["fit"] >= engine.settings.min_fit_auto) if master else "—", help=f"Fit at or above the Full Autopilot minimum ({engine.settings.min_fit_auto:g}). Needs a saved master resume.")
    states = {s: len(engine.auto.applications([s])) for s in (S.READY, S.NEEDS_HUMAN, S.UNKNOWN, S.APPLIED)}
    e, f, g, h = st.columns(4)
    e.metric("Awaiting approval", states[S.READY])
    f.metric("Need you", states[S.NEEDS_HUMAN])
    g.metric("Uncertain outcome", states[S.UNKNOWN])
    h.metric("Submitted", states[S.APPLIED])
    ok, beat = health(store, settings)
    sms = SmsService(store, settings)
    ready, why = sms.deliverable()
    st.markdown("**System**")
    st.dataframe(pd.DataFrame([
        {"Item": "Worker", "Status": ("Healthy" if ok else "Not running") + (f" (state {beat.get('state')})" if beat.get("state") else "")},
        {"Item": "Permission mode", "Status": controls.describe_mode(engine) + ("" if engine.auto.enabled() else " · master switch off")},
        {"Item": "Real submission on this server", "Status": "ON" if settings.allow_real_submission else "Off (forms are prepared only)"},
        {"Item": "SMS alerts", "Status": ("On, to " + mask(sms.prefs()["phone"])) if ready else why},
        {"Item": "Verified master resume", "Status": ("Saved" + (" (DOCX: formatting preserved)" if (store.master_original() or ("", ""))[1].lower().endswith(".docx") else " (not DOCX: standard template)")) if master else "Not saved"},
    ]), hide_index=True, width="stretch")
    last = store.get("last_scan")
    if last and last.get("diagnostics"):
        dg = last["diagnostics"]
        st.caption(f"Last scan {last.get('finished', '')[:16]} UTC: {sum(b['success'] for b in last['boards'])}/{len(last['boards'])} boards, "
                   f"{dg['listed']} listings, {dg['retained']} internships kept, {dg['eligible']} relevant.")
    top = sorted((x for x in relevant if x["match"]), key=lambda x: -x["match"]["fit"])[:8]
    if top:
        st.markdown("**Best current matches**")
        st.dataframe(pd.DataFrame([{"Company": x["job"].company, "Title": x["job"].title, "Fit": x["match"]["fit"], "Location": x["job"].location,
                                    "Official page": x["job"].url} for x in top]), hide_index=True, width="stretch",
                     column_config={"Official page": st.column_config.LinkColumn(display_text="Open")})
    cols = st.columns(4)
    for col, page in zip(cols, ("Find Internships", "Resume Grader & Tailor", "Applications", "Notifications")):
        if col.button(page, key="dash_" + page):
            _go(page)
