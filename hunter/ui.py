from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from ats.extraction import extract_document, InputError
from ats.matching import parse_resume
from ats.models import digest

from .models import Employer, Filters, Profile, utcnow
from .service import DEFAULT_DB, WATCHLIST, evaluate, scan
from .sources import employer_from_url
from .storage import STATUSES, Store
from .boards_ui import render_boards


def open_workspace(job=None):
    st.session_state.hunter_pending_job = job
    st.session_state.hunter_navigate = True
    st.rerun()


def render():
    store = Store(st.session_state.get("hunter_db_path", DEFAULT_DB))
    st.markdown('<div class="eyebrow">RESUME BENCH / INTERNSHIP HUNTER</div>', unsafe_allow_html=True)
    st.title("Find the work your evidence supports.")
    st.write("Official engineering internships and co-ops, local fit estimates, and application tracking. No LLM calls during discovery or scoring.")
    master = store.master()
    with st.expander("Verified master resume", expanded=master is None):
        if master:
            st.success("A verified master is saved locally. Source: " + master.source_hash[:16])
            st.text(master.text)
        current = st.session_state.get("master") if not st.session_state.get("demo") else None
        upload = st.file_uploader("Upload a master for Hunter", type=["pdf", "docx"], key="hunter_upload")
        candidate, original, filename = current, st.session_state.get("original_bytes"), st.session_state.get("original_name", "")
        if upload:
            try:
                extracted = extract_document(upload.getvalue(), upload.name)
                candidate = parse_resume(extracted.text, digest(upload.getvalue()))
                original, filename = upload.getvalue(), upload.name
                st.text(extracted.text)
                for warning in extracted.warnings:
                    st.warning(warning)
            except InputError as exc:
                st.error(str(exc))
                candidate = None
        if candidate:
            if not upload:
                st.caption("Current resume from the Phase 1 workspace is available.")
                st.text(candidate.text)
            verified = st.checkbox("I verified the extracted facts and section grouping against my original resume", key="hunter_verify_"+candidate.source_hash)
            if st.button("Save verified master for Hunter", disabled=not verified):
                store.save_master(candidate, original, filename)
                st.session_state.master = candidate
                st.session_state.extracted = extract_document(original, filename) if original and filename else st.session_state.get("extracted")
                st.session_state.demo = False
                st.rerun()
        else:
            st.info("Upload your resume here or in the Phase 1 workspace. The fictional demo is never saved as your verified master.")
        if st.button("Open resume workspace"):
            open_workspace()

    profile = Profile(**store.get("profile", asdict(Profile())))
    with st.expander("Candidate eligibility profile"):
        st.caption("Defaults are the facts you supplied for Phase 2. Academic standing, future enrollment, co-op availability and clearance remain unverified. These facts affect eligibility only; technical claims come from the master.")
        st.caption("Explicit GPA, degree and expected-graduation facts detected in the verified master take precedence over the declared defaults when filtering.")
        with st.form("hunter_profile"):
            degree = st.text_input("Declared degree", profile.degree)
            graduation = st.text_input("Graduation month (YYYY-MM)", profile.graduation)
            gpa = st.number_input("Declared GPA", min_value=0.0, max_value=4.0, value=profile.gpa, step=.01)
            choices = [True, False, None]
            fmt = lambda v: "Yes" if v is True else "No" if v is False else "Unknown"
            citizen = st.selectbox("U.S. citizen", choices, index=choices.index(profile.us_citizen), format_func=fmt)
            authorized = st.selectbox("Authorized to work in the U.S.", choices, index=choices.index(profile.us_authorized), format_func=fmt)
            sponsorship = st.selectbox("Visa sponsorship needed", choices, index=choices.index(profile.sponsorship_needed), format_func=fmt)
            if st.form_submit_button("Save eligibility profile"):
                try:
                    datetime.strptime(graduation, "%Y-%m")
                    store.set("profile", asdict(replace(profile, degree=degree, graduation=graduation, gpa=gpa, us_citizen=citizen, us_authorized=authorized, sponsorship_needed=sponsorship)))
                    st.rerun()
                except ValueError:
                    st.error("Graduation must be a valid YYYY-MM month.")

    a, b, c = st.columns(3)
    days = a.selectbox("Publication window", [1, 3, 7, 14, 30], index=2, format_func=lambda d: "24 hours" if d == 1 else f"{d} days")
    terms = b.multiselect("Internship term", ["Spring 2027", "Summer 2027"], default=["Spring 2027", "Summer 2027"])
    minimum = c.slider("Minimum fit estimate", 0, 100, 0)
    filters = Filters(days, tuple(terms))
    employers = store.employers(WATCHLIST)
    scan_now = st.button("Search Now", type="primary")
    render_boards(store, days)
    with st.expander("Local daily scanning & source settings"):
        st.write("A daily CLI scan can run through Windows Task Scheduler while this computer is on. The UI does not create a cloud scheduler or submit applications.")
        st.code('python -m hunter.cli --days 7', language="powershell")
        st.caption("See SCHEDULING.md for a daily scheduled task, executable path, and working-directory instructions. Scans reuse a one-hour HTTP cache and persist first-discovered dates.")
        scan_now = st.button("Scan Now") or scan_now
        watch = st.data_editor(pd.DataFrame([asdict(e) for e in employers]), hide_index=True, disabled=["source", "board", "career_url", "verified_at"], key="hunter_watchlist")
        if st.button("Save watchlist settings"):
            rows = watch.to_dict("records")
            store.set("employers", [{**asdict(e), "enabled": bool(r["enabled"]), "name": str(r["name"])} for e, r in zip(employers, rows)])
            st.rerun()
        with st.form("add_employer"):
            name = st.text_input("Employer name")
            career_url = st.text_input("Official career-board URL")
            manual = st.checkbox("Keep as a manual bookmark (unsupported provider)")
            if st.form_submit_button("Add employer"):
                try:
                    if manual:
                        from .models import canonical_url
                        employer = Employer(name.strip(), "manual", digest(career_url)[:12], canonical_url(career_url), False)
                        if not name.strip():
                            raise ValueError("Enter the employer name.")
                    else:
                        employer = employer_from_url(name, career_url)
                    if employer.key in {e.key for e in employers}:
                        raise ValueError("This board is already on the watchlist.")
                    store.set("employers", [asdict(e) for e in employers] + [asdict(employer)])
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        for employer in employers:
            if employer.source == "manual":
                st.link_button("Open " + employer.name + " careers (manual)", employer.career_url)
    if scan_now:
        meter = st.progress(0, text="Starting local scan…")
        try:
            result = scan(store, employers, master, profile, filters, progress=lambda n, total, name: meter.progress(n/max(1, total), text=f"{n+1}/{total} · {name}"))
            meter.progress(1.0, text="Scan finished")
            successes = sum(x["success"] for x in result["boards"])
            st.success(f"{successes}/{len(result['boards'])} boards returned data; {len(result['new_ids'])} newly discovered engineering internships/co-ops. New discovery is not proof of recent publication.")
        except ValueError as exc:
            st.error(str(exc))
    last = store.get("last_scan")
    st.caption("Last Scanned: " + (last.get("finished", last["started"]) + " (UTC)" if last else "Never"))
    if last:
        with st.expander("Actual scan coverage & source errors"):
            st.dataframe(pd.DataFrame(last["boards"]), hide_index=True)
            st.caption("Open means returned by an official public feed or structured job page at Last Checked. User-confirmed imports have a separate label. Availability can change after that timestamp. Failed/incomplete scans do not mark absent jobs closed. Cached data retains its original checked timestamp.")
    values = evaluate(store, master, profile, filters)
    if master is None:
        st.warning("No verified master is saved. Discovery works, but personalized fit scores and tailoring need your resume.")
    recent = [x for x in values if x["decision"].eligible and x["confirmed_scope"] and x["decision"].recency == "recent"]
    ca, cb, cc = st.columns(3)
    ca.metric("Confirmed recent in target scope", len(recent))
    cb.metric("Saved Jobs", sum(x["record"]["status"] == "Saved" for x in values))
    cc.metric("Applied", sum(x["record"]["status"] == "Applied" for x in values))
    view = st.radio("Opportunity view", ["Best Matches", "Recently Posted", "New Postings", "Imported jobs", "Posting date unknown", "Needs confirmation", "Older postings", "Saved Jobs", "Applied", "Rejected / Closed", "Tailored Resumes"], horizontal=True, key="hunter_view")
    if view == "Tailored Resumes":
        artifacts = store.artifacts()
        if not artifacts:
            st.info("Open a job in the resume workspace, approve any changes, validate the export, and save its job-specific copy.")
        for artifact in artifacts:
            record = store.job(artifact["job_key"])
            job = json.loads(record["payload"])
            with st.expander(job["company"] + " · " + job["title"] + " · " + artifact["created"]):
                st.json(json.loads(artifact["metadata"]))
                for extension in ("pdf", "docx", "audit"):
                    path = Path(artifact[extension])
                    if path.is_file() and path.resolve().is_relative_to(store.path.parent.resolve()):
                        st.download_button("Download " + extension.upper(), path.read_bytes(), path.name, key=artifact["key"]+extension)
                    else:
                        st.error("Saved artifact file is missing from the local data directory.")
        return
    newest = set(last["new_ids"]) if last else set()
    if view in {"Best Matches", "Recently Posted"}:
        visible = recent
    elif view == "New Postings":
        visible = [x for x in values if x["record"]["key"] in newest and x["decision"].eligible]
    elif view == "Imported jobs":
        imported = {r["job_key"] for r in store.imports()}
        visible = [x for x in values if x["record"]["key"] in imported]
    elif view == "Posting date unknown":
        visible = [x for x in values if x["decision"].eligible and x["decision"].recency == "unknown"]
    elif view == "Needs confirmation":
        visible = [x for x in values if x["decision"].eligible and (not x["confirmed_scope"] or any(f.status == "unknown" for f in x["decision"].flags))]
    elif view == "Older postings":
        visible = [x for x in values if x["decision"].eligible and x["decision"].recency in {"older", "future"}]
    elif view == "Saved Jobs":
        visible = [x for x in values if x["record"]["status"] == "Saved"]
    elif view == "Applied":
        visible = [x for x in values if x["record"]["status"] in {"Applied", "Interview"}]
    else:
        visible = [x for x in values if not x["decision"].eligible or x["record"]["status"] in {"Rejected", "Withdrawn"}]
    disciplines = st.multiselect("Discipline", sorted({x["decision"].discipline for x in values}))
    sources = st.multiselect("Job source", sorted({x["job"].source for x in values}))
    visible = [x for x in visible if not sources or x["job"].source in sources]
    visible = [x for x in visible if (not disciplines or x["decision"].discipline in disciplines) and (x["match"] is None and minimum == 0 or x["match"] and x["match"]["fit"] >= minimum)]
    order = st.selectbox("Sort opportunities by", ["Fit", "Recency", "Employer", "Location"], index=1 if view == "Recently Posted" else 0)
    if order == "Fit":
        visible.sort(key=lambda x: (-(x["match"]["fit"] if x["match"] else -1), x["job"].company, x["job"].title))
    elif order == "Recency":
        visible.sort(key=lambda x: x["job"].published_at or "", reverse=True)
    else:
        visible.sort(key=lambda x: (x["job"].company if order == "Employer" else x["job"].location).casefold())
    if not visible:
        st.info("No jobs meet this view and its filters. Check date-unknown/needs-confirmation views, broaden the publication window, or scan more employers.")
        return
    rows = [{"Company": x["job"].company, "Title": x["job"].title, "Location": x["job"].location, "Source": x["job"].source, "Availability": x["job"].source_status, "Term": ", ".join(x["decision"].terms) or "Confirm term", "Published": x["job"].published_at or "Posting date unknown", "Date basis": x["job"].publication_basis, "Fit": x["match"]["fit"] if x["match"] else None, "Eligibility flags": sum(f.status == "unknown" for f in x["decision"].flags), "Status": x["record"]["status"]} for x in visible]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Fit is a reproducible internal estimate, not an employer ATS result or an interview probability. Unknown publication dates and first discovery never prove a job was newly posted.")
    keys = [x["record"]["key"] for x in visible]
    by_key = {x["record"]["key"]: x for x in visible}
    selected = st.selectbox("Open job details", keys, format_func=lambda k: by_key[k]["job"].company + " · " + by_key[k]["job"].title)
    value = by_key[selected]
    job, record, decision, match = value["job"], value["record"], value["decision"], value["match"]
    st.subheader(job.title)
    st.write(f"{job.company} · {job.location} · {', '.join(decision.terms) or 'Term confirmation needed'}")
    st.link_button("Open listing" if job.imported_at and job.url == job.origin_url else "Open official application page", job.url)
    st.caption(f"Publication: {job.published_at or 'Posting date unknown'} · Last updated: {job.updated_at or 'Unknown'} · First discovered: {record['discovered_at']} · Last checked: {record['checked_at']} · Source: {job.source_status}")
    st.caption("Availability basis: " + job.availability_basis)
    for imported in store.imports(selected):
        st.link_button("Original imported listing", imported["origin_url"])
        st.caption("Pasted listing imported " + imported["imported_at"] + "; import time is separate from publication.")
    if job.imported_at:
        st.info("Imported listing: dates and availability are user-reported. It stays separate from automatically verified recent results. Confirm the employer application page below to enable tailoring.")
        with st.form("confirm_import_" + selected):
            application_url = st.text_input("Employer application page for this import", job.url)
            confirmed = st.checkbox("I verified this employer page is accepting applications", value=job.source_status == "user-confirmed")
            if st.form_submit_button("Save availability confirmation"):
                try:
                    store.confirm_import(selected, application_url, confirmed)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    if job.compensation:
        st.text("Explicitly listed compensation: " + job.compensation)
    for reason in decision.reasons:
        st.error(reason)
    if decision.flags:
        st.dataframe(pd.DataFrame([asdict(f) for f in decision.flags]), hide_index=True)
    if match:
        st.metric("Original resume fit estimate", match["fit"])
        st.write("Matched skills: " + (", ".join(match["matched_skills"]) or "No demonstrated technical skills detected"))
        st.write("Missing qualifications: " + ("; ".join(match["missing"]) or "No extracted requirement marked missing"))
        with st.expander("Why this score & strongest evidence", expanded=True):
            st.json(match["components"])
            st.caption(f"Evidence representation: {match['representation']:g}% of mentioned skill requirements have accomplishment evidence. This is separate from fit.")
            st.dataframe(pd.DataFrame(match["strongest_evidence"]), hide_index=True)
            for detail in match["details"]:
                req = detail["requirement"]
                st.write(f"**{detail['status']} · {req['priority']}** — {' / '.join(req['terms']) or req['text']}")
                for evidence in detail["evidence"]:
                    st.write(evidence["id"] + ": " + evidence["text"])
            for note in match["notes"]:
                st.caption(note)
    with st.expander("Complete available job description"):
        st.text(job.description or "Description retrieval incomplete; retry a later scan.")
    with st.form("status_"+selected):
        status = st.selectbox("Application status", STATUSES, index=STATUSES.index(record["status"]))
        notes = st.text_area("Application notes", record["notes"])
        if st.form_submit_button("Save application status"):
            store.status(selected, status, notes)
            st.rerun()
    if st.button("Tailor Resume for this job", type="primary", disabled=master is None or not job.description or job.source_status not in {"open", "user-confirmed"} or not decision.eligible):
        st.session_state.master = master
        from ats.models import Extracted
        st.session_state.extracted = Extracted(master.text, "saved verified master")
        st.session_state.source_hash = master.source_hash
        st.session_state.demo = False
        st.session_state.pop("original_bytes", None)
        open_workspace({"key": selected, "description": job.description, "company": job.company, "title": job.title, "url": job.url, "db": str(store.path), "source_hash": master.source_hash})
