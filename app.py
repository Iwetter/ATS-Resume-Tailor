from __future__ import annotations

from dataclasses import asdict, replace
from difflib import SequenceMatcher
from html import escape
import json
from pathlib import Path
import re

import pandas as pd
import streamlit as st

from ats.extraction import InputError, extract_document, fetch_job
from ats.exporting import ExportError, export_resume
from ats.matching import (HEADINGS, STATUS_LABELS, extract_requirements, make_resume, parse_resume, rank_bullets, rank_projects, select_projects, validate_requirements)
from ats.models import Change, Extracted, digest
from ats.rewriting import (ModelConfig, apply_changes, parse_changes, rewrite_request, tailor_once, validate_change)
from ats.scoring import DEFAULT_WEIGHTS, analyze
from autoapply import config as server_config

ROOT = Path(__file__).parent
WORKSPACES = ["Dashboard", "Find Internships", "Resume Grader & Tailor", "Auto Apply", "Applications", "Notifications", "Settings"]
st.set_page_config(page_title="Resume Bench | ATS Grader & Tailor", page_icon="📄", layout="wide")
st.markdown("""<style>
.block-container {max-width:1250px;padding-top:2.5rem}
h1 {letter-spacing:-.04em} h2 {letter-spacing:-.025em}
.eyebrow {font-size:.8rem;letter-spacing:.16em;color:#17635A;font-weight:700}
.diff {padding:1rem;background:white;border:1px solid #d9e0da;border-radius:8px;line-height:1.7}
del {background:#fce5e2;color:#8c2420;text-decoration:line-through}
ins {background:#d5efe2;color:#154f38;text-decoration:none}
[data-testid="stMetric"] {background:white;border:1px solid #d9e0da;border-radius:10px;padding:1rem}
</style>""", unsafe_allow_html=True)


def set_job_text(text, origin):
    """Replace the job description and forget everything derived from the previous one."""
    st.session_state.job_input = text
    st.session_state.job_origin = origin
    st.session_state.job_origin_hash = digest(text) if text else None
    for key in ("analysis", "analysis_hash", "own_bundle", "own_bundle_hash"):
        st.session_state.pop(key, None)
    reset_review()


def job_title(text):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not re.fullmatch(r"(?i)back to jobs|apply|job description", line.strip())]
    return lines[0][:90] if lines else "(untitled)"


def job_source_label(text):
    """Which posting is being graded, where it came from, and whether it was edited since."""
    origin = st.session_state.get("job_origin") or "Pasted text"
    edited = st.session_state.get("job_origin_hash") not in (None, digest(text))
    return f"Grading: {job_title(text)} · {origin}{' (edited after loading)' if edited else ''} · text fingerprint {digest(text)[:8]}"


def reset_review():
    for key in ("tailored", "proposals", "api_usage", "bundle", "bundle_hash", "import_error"):
        st.session_state.pop(key, None)


def diffs(before: str, after: str) -> tuple[str, str]:
    old, new = before.split(), after.split()
    left, right = [], []
    for op, i, j, k, l in SequenceMatcher(a=old, b=new).get_opcodes():
        a, b = escape(" ".join(old[i:j])), escape(" ".join(new[k:l]))
        if a:
            left.append(f"<del>{a}</del>" if op != "equal" else a)
        if b:
            right.append(f"<ins>{b}</ins>" if op != "equal" else b)
    return " ".join(left), " ".join(right)


def requirement_rows(reqs):
    return [{**asdict(r), "terms": "; ".join(r.terms)} for r in reqs]


def diagnostics(result, resume, requirements):
    req_map = {r.id: r for r in requirements}
    lost = dict(result.deductions)
    rows = []
    for match in result.matches:
        r = req_map[match.requirement_id]
        evidence = "\n".join(f"{resume.by_id[eid].text}" for eid in match.evidence_ids if eid in resume.by_id)
        status = STATUS_LABELS.get(match.status, match.status) if r.kind != "responsibility" else {"supported": "Related experience shown", "partial": "Some related experience", "missing": "No related experience shown"}[match.status]
        scored = not (r.kind == "eligibility" and match.status == "unknown")
        rows.append({"Requirement": "/".join(r.terms) if r.kind in ("skill", "soft_skill") else r.text, "Priority": r.priority,
                     "Type": {"skill": "Technical skill", "responsibility": "Duty", "eligibility": "Stated qualification", "soft_skill": "Soft skill"}.get(r.kind, r.kind),
                     "Status": status, "Points lost": lost.get(match.requirement_id, 0.0) if scored else None,
                     "Resume evidence": evidence or ("(none on resume)" if scored else "(verify yourself; not scored)"), "Why": match.explanation})
    categories = st.tabs(["Requirements & evidence", "Score breakdown", "What would raise the score"])
    with categories[0]:
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
                "Resume evidence": st.column_config.TextColumn(width="large"), "Requirement": st.column_config.TextColumn(width="large"),
                "Points lost": st.column_config.NumberColumn(format="%.1f", help="Alignment points this requirement costs now")})
        else:
            st.warning("No requirements extracted. Add requirements in the editor above.")
        st.caption("Every match cites the resume lines behind it. Stated qualifications the resume cannot show (availability, export control) are listed for you to verify and are not scored either way.")
    with categories[1]:
        a, b = st.columns(2)
        a.dataframe(pd.DataFrame([{"Alignment component": k.replace("_", " "), "Score / 100": (None if k in result.unmeasured else v),
                                   "Note": "not in this posting; left out of the average" if k in result.unmeasured else ""} for k, v in result.components.items()]), hide_index=True)
        b.dataframe(pd.DataFrame([{"Readability check": k, "Points": v} for k, v in result.checks.items()]), hide_index=True)
        if result.breakdown:
            st.dataframe(pd.DataFrame([{"Category": cat, **{STATUS_LABELS.get(k, k): v for k, v in counts.items()}} for cat, counts in sorted(result.breakdown.items())]).fillna(0), hide_index=True, width="stretch")
        for warning in result.warnings:
            st.warning(warning)
        st.caption("Heuristics, not an employer ATS algorithm or interview probability. A component with nothing to measure in this posting is left out instead of scored 0. Repeated terms do not raise any score.")
    with categories[2]:
        if result.improvements:
            for improvement in result.improvements:
                st.write("• " + improvement)
        else:
            st.write("Nothing measurable is missing. Review the evidence table and eligibility conditions before applying.")


if st.session_state.get("navigate_to"):
    st.session_state.workspace_page = st.session_state.pop("navigate_to")
if st.session_state.pop("hunter_navigate", False):
    st.session_state.workspace_page = "Resume Grader & Tailor"
    pending = st.session_state.pop("hunter_pending_job", None)
    if pending:
        st.session_state.pop("phase1_upload", None)
        st.session_state.hunter_job = pending
        set_job_text(pending["description"], "Internship Hunter · " + pending["company"] + " · " + pending["title"])

with st.sidebar:
    server_settings = server_config.load()
    st.markdown("### Resume Bench")
    workspace_page = st.radio("Workspace", WORKSPACES, key="workspace_page")
    st.caption("EVIDENCE FIRST · ENGINEERING INTERNSHIPS")
    st.write("1. Verify extraction\n\n2. Analyze evidence\n\n3. Review tailoring\n\n4. Validate & download")
    st.divider()
    st.markdown("**Optional AI rewriting**")
    enabled = st.toggle("Enable paid AI rewriting", value=False)
    provider = st.selectbox("Provider", ["Claude (Anthropic)", "OpenAI"], disabled=not enabled, key="rewrite_provider",
                            help="Claude uses the official Anthropic SDK with the server's spending limits and cache.")
    if provider == "OpenAI":
        api_key = st.text_input("OpenAI API key", type="password", disabled=not enabled, help="Kept only in this local session; never read automatically from environment or saved to disk.")
        model = st.text_input("Model ID", disabled=not enabled, placeholder="A model you have configured for structured outputs")
    else:
        api_key = st.text_input("Anthropic API key (blank = server ANTHROPIC_API_KEY)", type="password", disabled=not enabled,
                                help="Kept only in this session. Leave blank to use the key configured on the server.") or server_settings.anthropic_api_key
        model = st.text_input("Claude model ID", value=server_settings.claude_model, disabled=not enabled, placeholder="e.g. the model in CLAUDE_MODEL")
        p1, p2 = st.columns(2)
        price_in = p1.number_input("Input $/M tokens", min_value=0.0, value=max(0.0, server_settings.claude_input_usd_per_mtok or 0.0), step=0.25, disabled=not enabled,
                                   help="Copy the current price for this model from your Anthropic console. Needed so the daily/monthly spend limits can be enforced.")
        price_out = p2.number_input("Output $/M tokens", min_value=0.0, value=max(0.0, server_settings.claude_output_usd_per_mtok or 0.0), step=0.25, disabled=not enabled)
    output_budget = st.number_input("Maximum output tokens", min_value=256, max_value=3000, value=1800, step=128, disabled=not enabled)
    if provider != "OpenAI":
        from autoapply.llm import claude_status
        ready_ai, why_ai = claude_status(enabled, api_key, model, price_in, price_out)
        (st.success if ready_ai else st.warning)("Claude: " + why_ai)
    st.caption("Only Tailor Resume can send selected bullets and relevant requirements. One attempt per unique operation, including failures. No automatic retries.")
    st.divider()
    with st.expander("Alignment weights"):
        weights = {key: st.number_input(key.replace("_", " ").title(), 0.0, 100.0, float(value), 5.0, key="weight_"+key) for key, value in DEFAULT_WEIGHTS.items()}
    st.caption("Phase 1 sessions stay in memory. Hunter saves verified masters, jobs, approved exports and its rewrite cache locally in data/. Downloads are explicit local copies.")
    if st.button("Clear current session"):
        st.session_state.clear()
        st.rerun()

if workspace_page == "Dashboard":
    from dashboard import render as render_dashboard
    render_dashboard()
    st.stop()
if workspace_page == "Find Internships":
    from hunter.ui import render
    render()
    st.stop()
if workspace_page in ("Applications", "Notifications", "Settings"):
    from autoapply.ui import render_section
    render_section(workspace_page)
    st.stop()
if workspace_page == "Auto Apply":
    from autoapply.ui import render as render_auto
    render_auto()
    st.stop()

st.markdown('<div class="eyebrow">RESUME BENCH / APPLICATION WORKSPACE</div>', unsafe_allow_html=True)
st.title("A stronger application, backed by evidence.")
st.write("Check ATS readability, compare job requirements, and tailor your engineering resume with every fact traceable to its source.")
st.caption("Local grading is free of API calls. Your uploaded resume is authoritative; profile defaults never add claims.")
if st.session_state.get("hunter_job"):
    context = st.session_state.hunter_job
    st.info("Hunter job: " + context["company"] + " · " + context["title"] + ". Analyze, review changes, then save the validated job-specific copy below.")
    st.link_button("Official Hunter application link", context["url"])

left, right = st.columns(2, gap="large")
with left:
    st.subheader("01  Master resume")
    upload = st.file_uploader("Upload PDF or DOCX", type=["pdf", "docx"], key="phase1_upload")
    if st.button("Use fictional test resume", help="Demonstration only. This is not your verified resume."):
        st.session_state.demo = True
        st.session_state.pop("source_hash", None)
    if upload:
        source_bytes, source_name = upload.getvalue(), upload.name
        source_hash = digest(source_bytes)
        if st.session_state.get("source_hash") != source_hash:
            try:
                extracted = extract_document(source_bytes, source_name)
                st.session_state.update(source_hash=source_hash, extracted=extracted, master=parse_resume(extracted.text, source_hash), original_bytes=source_bytes, original_name=source_name, demo=False)
                reset_review()
                st.session_state.pop("analysis", None)
            except InputError as exc:
                st.error(str(exc))
                st.stop()
    elif st.session_state.get("demo") and "source_hash" not in st.session_state:
        text = (ROOT / "tests/fixtures/fictional_resume.txt").read_text(encoding="utf-8")
        source_hash = digest(text)
        st.session_state.update(source_hash=source_hash, extracted=Extracted(text, "demo"), master=parse_resume(text, source_hash))
        st.session_state.pop("original_bytes", None)
        reset_review()
        st.session_state.pop("analysis", None)
    if st.session_state.get("demo"):
        st.warning("Fictional test data is loaded. Upload your resume for an actual application.")
    if "extracted" in st.session_state:
        with st.expander("Preview extracted resume", expanded=True):
            st.text(st.session_state.extracted.text)
            for message in st.session_state.extracted.warnings:
                st.warning(message)
        if st.session_state.get("original_bytes"):
            st.download_button("Download untouched master", st.session_state.original_bytes, "master-original" + Path(st.session_state.original_name).suffix.lower())
with right:
    st.subheader("02  Job description")
    url = st.text_input("Job posting URL", key="job_url", placeholder="https://job-boards.greenhouse.io/…")
    a, b = st.columns(2)
    if a.button("Fetch job URL", disabled=not url.strip()):
        try:
            with st.spinner("Retrieving job description…"):
                fetched = fetch_job(url.strip())
            set_job_text(fetched, "Fetched from " + url.strip())
        except InputError as exc:
            # Fail closed: never leave the previous posting's text in place to be graded by mistake.
            set_job_text("", "")
            st.error(str(exc) + " The previous job description was cleared.")
    if b.button("Use saved Western Digital example"):
        set_job_text((ROOT / "tests/fixtures/western_digital_job.txt").read_text(encoding="utf-8"), "Saved Western Digital example (snapshot retrieved October 20, 2026)")
    job_text = st.text_area("Paste or review job description", key="job_input", height=340, placeholder="Paste the complete responsibilities and qualifications here.")
    if job_text.strip():
        st.caption(job_source_label(job_text))
    st.caption("Review fetched text before analysis. For a posting covering multiple tracks, remove unrelated requirements in the editor.")

if "master" not in st.session_state or not job_text.strip():
    st.info("Add a master resume and job description to open the evidence workspace.")
    st.stop()

master, extracted = st.session_state.master, st.session_state.extracted
with st.expander("Verify evidence ledger & section grouping"):
    st.caption("All original text is retained. Correct only line classification/grouping here. Shared project group IDs keep titles, dates, and bullets together. Non-project evidence is always retained.")
    ledger_rows = [asdict(x) for x in master.items]
    ledger_edit = st.data_editor(pd.DataFrame(ledger_rows), hide_index=True, disabled=["id", "text"], key="ledger_"+master.source_hash,
        column_config={"kind": st.column_config.SelectboxColumn(options=["header", "section", "entry", "bullet", "skill", "text"], required=True), "section": st.column_config.SelectboxColumn(options=["Header", *dict.fromkeys(HEADINGS.values())], required=True)})
    if st.button("Apply grouping corrections"):
        allowed_kinds = {"header", "section", "entry", "bullet", "skill", "text"}
        allowed_sections = {"Header", *HEADINGS.values()}
        try:
            rows = ledger_edit.to_dict("records")
            revised = []
            for original, row in zip(master.items, rows):
                if row["kind"] not in allowed_kinds or row["section"] not in allowed_sections:
                    raise ValueError("Choose a supported evidence kind and section.")
                revised.append(replace(original, kind=row["kind"], section=row["section"], group=str(row["group"] or "")))
            st.session_state.master = make_resume(tuple(revised), master.source_hash)
            st.session_state.pop("analysis", None)
            reset_review()
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    st.dataframe(pd.DataFrame([asdict(f) for f in master.facts]), hide_index=True)
    st.download_button("Download factual evidence ledger", json.dumps({"source_hash": master.source_hash, "items": ledger_rows, "facts": [asdict(f) for f in master.facts]}, indent=2, ensure_ascii=False), "evidence-ledger.json", "application/json")

version = digest([master.source_hash, job_text])[:14]
try:
    extracted_reqs = extract_requirements(job_text)
except ValueError as exc:
    st.warning(str(exc))
    st.stop()

st.subheader("03  Requirements & diagnostics")
with st.expander("Edit extracted requirements before analysis", expanded=True):
    st.caption("Each skill row uses semicolon-separated terms. “any” represents alternatives; “all” requires every term. Required, preferred and contextual priorities have weights 2, 1 and 0.5. Eligibility is reported separately from alignment. Verify a hard condition with an explicit note, or leave it unknown.")
    columns = ["id", "text", "kind", "priority", "terms", "mode", "assertion", "note"]
    edited = st.data_editor(pd.DataFrame(requirement_rows(extracted_reqs), columns=columns), key="reqs_"+version, num_rows="dynamic", hide_index=True, disabled=["id"], width="stretch", column_config={
        "kind": st.column_config.SelectboxColumn(options=["skill", "responsibility", "eligibility", "soft_skill"], required=True),
        "priority": st.column_config.SelectboxColumn(options=["required", "preferred", "context"], required=True),
        "mode": st.column_config.SelectboxColumn(options=["all", "any"], required=True),
        "assertion": st.column_config.SelectboxColumn(options=["unknown", "met", "unmet"], required=True),
        "text": st.column_config.TextColumn(width="large")})
try:
    requirements = validate_requirements(edited.fillna("").to_dict("records"))
except ValueError as exc:
    st.error(str(exc))
    st.stop()
analysis_hash = digest([[asdict(x) for x in master.items], job_text, [asdict(x) for x in requirements], weights])
if st.button("Analyze Resume", type="primary"):
    try:
        with st.spinner("Checking requirements against original evidence…"):
            result = analyze(master, extracted, requirements, weights)
        if st.session_state.get("analysis_hash") != analysis_hash:
            reset_review()
        st.session_state.update(analysis=result, analysis_hash=analysis_hash)
    except ValueError as exc:
        st.error(str(exc))
if st.session_state.get("analysis_hash") != analysis_hash or "analysis" not in st.session_state:
    st.info("Click Analyze Resume to score the current source, requirements, and weights.")
    st.stop()
before = st.session_state.analysis
st.info(job_source_label(job_text))  # the exact posting these results belong to
a, b = st.columns(2)
a.metric("ATS readability · original", f"{before.readability:.0f} / 100")
b.metric("Job alignment · original", f"{before.alignment:.0f} / 100")
diagnostics(before, master, requirements)

st.divider()
st.subheader("04  Tailor & review")
ranked = rank_projects(master, requirements)
names = {group: f"{title} · relevance {score:.2f}" for group, score, title in ranked}
groups = [g for g, _, _ in ranked]
selected_groups = st.multiselect("Projects to include, in export order", groups, default=groups[:4] if st.session_state.get("hunter_job") else groups, format_func=lambda g: names[g], key="projects_"+analysis_hash[:12])
st.caption(("Hunter proposes the four most relevant projects; review the selection and omissions. " if st.session_state.get("hunter_job") else "Projects are ranked locally; all are retained by default. ") + "Usually choose 3–4 for a student resume. Selecting fewer is an explicit omission; relevant experience is never dropped automatically. Remove and re-add a project to change its order.")
omitted = [names[g] for g in groups if g not in selected_groups]
if omitted:
    st.warning("Projects omitted from this draft: " + "; ".join(omitted))
draft = select_projects(master, selected_groups)
if st.session_state.get("hunter_job"):
    from hunter.service import order_skills
    draft = order_skills(draft, requirements)
bullet_ranking = rank_bullets(draft, requirements)
all_bullet_ids = [eid for eid, _ in bullet_ranking]
bullet_labels = {eid: f"{draft.by_id[eid].text[:125]} · relevance {score:.2f}" for eid, score in bullet_ranking}
with st.expander("Select accomplishment bullets for the export"):
    included_bullets = st.multiselect("Accomplishment bullets to include", all_bullet_ids, default=all_bullet_ids, format_func=lambda eid: bullet_labels[eid], key="included_"+digest([analysis_hash, selected_groups])[:12])
    st.caption("Ranked locally; every bullet is included by default. Deselecting a bullet explicitly omits it. Its role/project title and dates remain. Prefer keeping verified evidence for required skills.")
omitted_bullets = [eid for eid in all_bullet_ids if eid not in included_bullets]
if omitted_bullets:
    st.warning("Accomplishment bullets explicitly omitted: " + ", ".join(omitted_bullets))
draft = make_resume(tuple(x for x in draft.items if x.kind != "bullet" or x.id in included_bullets), master.source_hash)
labels = {eid: draft.by_id[eid].text[:130] for eid in all_bullet_ids if eid in included_bullets}
selected_ids = st.multiselect("Bullets to review or send for rewriting (at most 8)", list(labels), default=list(labels)[:8], format_func=lambda eid: labels[eid], max_selections=8, key="bullets_"+digest([analysis_hash, selected_groups])[:12])
request = None
if selected_ids:
    try:
        request = rewrite_request(master, requirements, selected_ids)
        st.download_button("Download compact Codex rewrite request", json.dumps(request, ensure_ascii=False, indent=2), "rewrite-request.json", "application/json")
        with st.expander("Inspect exact AI payload"):
            st.json(request)
            st.caption(f"{len(json.dumps(request, ensure_ascii=False)):,} characters total, with at most {int(output_budget):,} output tokens. Actual token counts vary by model.")
    except ValueError as exc:
        st.warning(str(exc))
if st.button("Tailor Resume", type="primary", disabled=not selected_ids or request is None):
    reset_review()
    if enabled:
        try:
            with st.spinner("Making one bounded rewrite request…"):
                if st.session_state.get("hunter_job"):
                    from hunter.storage import Store
                    from hunter.service import RewriteCache
                    rewrite_cache = RewriteCache(Store(st.session_state.hunter_job["db"]))
                else:
                    rewrite_cache = st.session_state.setdefault("rewrite_cache", {})
                transport = None
                if provider == "Claude (Anthropic)":
                    from autoapply import config as aa_config
                    from autoapply.db import AutoStore
                    from autoapply.llm import Claude
                    from hunter.service import DEFAULT_DB
                    from hunter.storage import Store as HunterStore
                    usage_store = HunterStore(st.session_state.hunter_job["db"] if st.session_state.get("hunter_job") else st.session_state.get("hunter_db_path", DEFAULT_DB))
                    claude = Claude(AutoStore(usage_store), aa_config.load(db_path=usage_store.path, claude_enabled=True, anthropic_api_key=api_key, claude_model=model,
                                                                         claude_max_output_tokens=int(output_budget), claude_input_usd_per_mtok=price_in or None,
                                                                         claude_output_usd_per_mtok=price_out or None))
                    ready, why = claude.configured()
                    if not ready:
                        raise ValueError(why)
                    transport = claude.rewrite_transport()
                value = tailor_once(master, job_text, requirements, selected_ids, ModelConfig(model, int(output_budget)), api_key, intentional=True, enabled=True, cache=rewrite_cache,
                                    **({"transport": transport} if transport else {}))
            st.session_state.proposals = {c.source_ids[0]: c for c in value["changes"]}
            st.session_state.api_usage = {"cached": value["cache_hit"], **value["usage"]}
            st.session_state.tailored = True
        except ValueError as exc:
            st.error(str(exc))
    else:
        st.session_state.tailored = True
        st.session_state.proposals = {}

if not st.session_state.get("tailored"):
    st.info("Click Tailor Resume to open the review editor. With AI disabled, selection and manual editing run locally.")
    st.stop()
if not enabled:
    st.info("Local mode: projects were ranked and selected and skills reordered, but no AI rewriting happened, so bullets keep their original wording. "
            "Turn on AI rewriting in the sidebar (Anthropic API key, model and prices) for real rewording, or edit bullets below.")
elif not st.session_state.get("proposals"):
    st.warning("No AI rewrite was applied: the request failed or proposed nothing. The draft below is NOT Claude-tailored.")
if st.session_state.get("api_usage"):
    st.caption("API usage: " + json.dumps(st.session_state.api_usage))
with st.expander("Import proposed changes from Codex"):
    imported = st.text_area("Structured rewrite JSON", key="import_json", height=150)
    if st.button("Load proposals for review"):
        try:
            changes = parse_changes(imported, set(selected_ids))
            st.session_state.proposals = {c.source_ids[0]: c for c in changes}
            st.session_state.pop("bundle", None)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

proposals = st.session_state.get("proposals", {})
approved, changes = set(), []
review_key = digest([analysis_hash, selected_ids, [asdict(c) for c in proposals.values()]])[:12]
for eid in selected_ids:
    source = master.by_id[eid]
    proposal = proposals.get(eid)
    with st.expander(f"{eid} · {source.text[:85]}", expanded=True):
        st.caption("Source: " + eid + (" · " + proposal.justification if proposal else " · Manual editor"))
        proposed_text = st.text_area("Proposed bullet", value=proposal.proposed_text if proposal else source.text, key="edit_"+review_key+eid)
        change = Change((eid,), source.text, proposed_text, proposal.justification if proposal else "Manual edit")
        result = validate_change(change, master)
        before_html, after_html = diffs(source.text, proposed_text)
        old_col, new_col = st.columns(2)
        old_col.caption("ORIGINAL")
        old_col.markdown('<div class="diff">'+before_html+'</div>', unsafe_allow_html=True)
        new_col.caption("PROPOSED")
        new_col.markdown('<div class="diff">'+after_html+'</div>', unsafe_allow_html=True)
        if result.status == "rejected":
            for reason in result.reasons:
                st.error(reason)
        elif result.status == "review":
            st.warning(result.reasons[0])
        decision = st.radio("Decision", ["Keep original", "Approve change", "Reject change"], horizontal=True, disabled=result.status != "review", key="approve_"+review_key+eid+digest(proposed_text)[:8])
        changes.append(change)
        if decision == "Approve change" and result.status == "review":
            approved.add(eid)

reviewed = apply_changes(master, draft, tuple(changes), approved)
after = analyze(reviewed, Extracted(reviewed.text, "draft"), requirements, weights)
a, b = st.columns(2)
a.metric("Job alignment · reviewed draft", f"{after.alignment:.0f} / 100", f"{after.alignment-before.alignment:+.1f} points")
b.metric("Approved bullet changes", len(approved))
st.caption("Draft alignment uses only selected projects and explicitly approved edits. Unchanged scores are expected when tailoring improves wording without adding evidence. Export readability is checked on the generated PDF below.")
with st.expander("Review final draft and evidence"):
    st.text(reviewed.text)
    diagnostics(after, reviewed, requirements)

st.subheader("05  Validate & download")
bundle_hash = digest([analysis_hash, [asdict(x) for x in reviewed.items]])
own_original = st.session_state.get("original_bytes")
own_docx = bool(own_original) and st.session_state.get("original_name", "").lower().endswith(".docx")
export_mode = st.radio("Output", ["Edit my original DOCX (keeps its formatting)", "Standard ATS template"], index=0 if own_docx else 1, horizontal=True,
                       disabled=not own_docx, key="export_mode",
                       help="Your uploaded DOCX is the source of truth: wording changes go into its existing paragraphs and runs; margins, fonts, styles and spacing are untouched. Upload a DOCX master to enable it.")
if own_docx and export_mode.startswith("Edit my original"):
    from ats.docx_tailor import docx_to_pdf, export_tailored
    if st.button("Generate tailored DOCX & PDF from my original", type="primary"):
        with st.spinner("Editing your DOCX and rendering it with Word/LibreOffice…"):
            st.session_state.own_bundle = export_tailored(own_original, master, reviewed)
            st.session_state.own_bundle_hash = bundle_hash
            if "own_original_pdf" not in st.session_state:
                st.session_state.own_original_pdf = docx_to_pdf(own_original)[0]
    own = st.session_state.get("own_bundle") if st.session_state.get("own_bundle_hash") == bundle_hash else None
    if own:
        report = own["report"]
        (st.success if own["ok"] else st.warning)(
            f"{len(report['edited'])} line(s) reworded, {len(report['removed'])} removed, {len(report['moved'])} project(s) reordered in your document · "
            + (f"{own['pages']} page(s) rendered by {own['converter']}" if own["pdf"] else "PDF not rendered"))
        for issue in own["issues"]:
            st.error(issue)
        if report["unmapped"]:
            st.caption(f"{len(report['unmapped'])} of {report['items']} resume lines could not be matched to a DOCX paragraph; they were left exactly as in your file.")
        c1, c2 = st.columns(2)
        c1.download_button("Download tailored DOCX (your formatting)", own["docx"], "tailored-resume.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if own["pdf"]:
            c2.download_button("Download tailored PDF", own["pdf"], "tailored-resume.pdf", "application/pdf")
            import pymupdf
            left, right = st.columns(2)
            for column, label, data in ((left, "Original", st.session_state.get("own_original_pdf")), (right, "Tailored", own["pdf"])):
                if data:
                    with pymupdf.open(stream=data, filetype="pdf") as pdf:
                        column.image(pdf[0].get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2)).tobytes("png"), caption=label)
        changed = [(master.by_id[i].text, reviewed.by_id[i].text) for i in report["edited"]]
        if changed:
            with st.expander("Exact wording changes", expanded=True):
                for before_text, after_text in changed:
                    old_html, new_html = diffs(before_text, after_text)
                    a, b = st.columns(2)
                    a.markdown(f'<div class="diff">{old_html}</div>', unsafe_allow_html=True)
                    b.markdown(f'<div class="diff">{new_html}</div>', unsafe_allow_html=True)
    st.stop()
if st.button("Generate validated PDF & DOCX", type="primary"):
    st.session_state.pop("bundle", None)
    try:
        with st.spinner("Generating one-page files and checking all selected text…"):
            bundle = export_resume(reviewed)
        st.session_state.update(bundle=bundle, bundle_hash=bundle_hash)
    except (ExportError, ValueError) as exc:
        st.error(str(exc))
if st.session_state.get("bundle_hash") == bundle_hash and "bundle" in st.session_state:
    bundle = st.session_state.bundle
    verified = extract_document(bundle.pdf, "tailored.pdf")
    final = analyze(reviewed, verified, requirements, weights)
    st.success(f"PDF verified: {bundle.pages} page, {bundle.font_size:g} pt body; all selected evidence is extractable. DOCX text is verified.")
    c1, c2 = st.columns(2)
    c1.metric("ATS readability · exported PDF", f"{final.readability:.0f} / 100", f"{final.readability-before.readability:+.1f} points")
    c2.metric("Job alignment · exported resume", f"{final.alignment:.0f} / 100", f"{final.alignment-before.alignment:+.1f} points")
    c1.download_button("Download tailored PDF", bundle.pdf, "tailored-resume.pdf", "application/pdf")
    c2.download_button("Download editable DOCX", bundle.docx, "tailored-resume.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    audit = {"source_hash": master.source_hash, "omitted_projects": omitted, "omitted_bullet_ids": omitted_bullets, "approved_changes": [asdict(c) for c in changes if c.source_ids[0] in approved], "requirements": [asdict(r) for r in requirements], "weights": weights, "before": asdict(before), "after": asdict(final), "pdf_pages": bundle.pages}
    st.download_button("Download analysis & approval record", json.dumps(audit, ensure_ascii=False, indent=2), "analysis-record.json", "application/json")
    hunter_context = st.session_state.get("hunter_job")
    if hunter_context:
        source_matches = master.source_hash == hunter_context["source_hash"] and job_text == hunter_context["description"]
        if not source_matches:
            st.warning("Resume source or job text changed. Return to Hunter and select the intended job before saving a linked copy.")
        if st.button("Save validated resume to Internship Hunter", disabled=not source_matches):
            from hunter.storage import Store
            from hunter.models import Job
            from hunter.service import score_job
            hunter_store = Store(hunter_context["db"])
            stored_job = Job(**json.loads(hunter_store.job(hunter_context["key"])["payload"]))
            original_fit = score_job(stored_job, master, hunter_store)
            tailored_fit = score_job(stored_job, reviewed, hunter_store)
            metadata = {"company": stored_job.company, "title": stored_job.title, "source_hash": master.source_hash,
                        "original_fit_estimate": original_fit["fit"] if original_fit else None, "tailored_fit_estimate": tailored_fit["fit"] if tailored_fit else None,
                        "approved_change_ids": sorted(approved), "pdf_pages": bundle.pages, "official_url": stored_job.url}
            artifact_key = hunter_store.save_artifact(hunter_context["key"], bundle, audit, metadata)
            st.success("Saved job-specific PDF, DOCX and approval record to Hunter: " + artifact_key[:12])
    with st.expander("Preview generated PDF"):
        import pymupdf
        with pymupdf.open(stream=bundle.pdf, filetype="pdf") as pdf:
            st.image(pdf[0].get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4)).tobytes("png"), caption="Verified one-page PDF")
    st.caption("Open the DOCX in Word to confirm pagination: Word's layout engine can differ from the verified PDF. Review final links and factual meaning before submitting.")
elif "bundle" in st.session_state:
    st.info("Draft or analysis settings changed. Generate fresh validated files before downloading.")
