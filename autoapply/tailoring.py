"""Job-specific resume generation that reuses Phase 1/2 functions unchanged.

Deterministic by default: relevant whole projects are selected and existing
skill entries reordered (``hunter.service.order_skills``). No wording changes
are made. When Claude is configured, it may (a) choose projects by ID and
(b) propose bullet rewrites through ``ats.rewriting.tailor_once``. Proposals
pass ``validate_change`` and are *held for human approval* unless the
operator explicitly enables AUTOAPPLY_AUTO_ACCEPT_VALIDATED_REWRITES.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import logging

from ats.exporting import ExportError, export_resume
from ats.matching import extract_requirements, rank_bullets, rank_projects, select_projects
from ats.models import Change, digest
from ats.rewriting import ModelConfig, apply_changes, tailor_once, validate_change
from hunter.service import RewriteCache, order_skills, score_job

from .llm import LLMError

logger = logging.getLogger("autoapply.tailoring")


def tailor_for_job(store, job_key, job, master, settings, claude=None, approved_change_ids=(), max_projects=4, previous_plan=None):
    requirements = extract_requirements(job.description)
    notes = []
    ranked = [g for g, _, _ in rank_projects(master, requirements)]
    chosen, selection_basis = ranked[:max_projects], "deterministic local relevance ranking"
    if previous_plan and previous_plan.get("projects"):
        chosen = [g for g in previous_plan["projects"] if g in ranked] or chosen
        selection_basis = previous_plan.get("selection_basis", selection_basis)
    elif claude is not None and claude.configured()[0]:
        try:
            chosen, _ = claude.select_projects(master, requirements, max_projects)
            selection_basis = "Claude selection from existing project IDs"
        except LLMError as exc:
            notes.append("Claude project selection unavailable; used local ranking: " + str(exc)[:200])
    proposals = [Change(tuple(c["source_ids"]), c["original_text"], c["proposed_text"], c["justification"]) for c in (previous_plan or {}).get("proposals", [])]
    if not proposals and claude is not None and claude.configured()[0]:
        draft = order_skills(select_projects(master, chosen), requirements)
        selected = [eid for eid, _ in rank_bullets(draft, requirements)][:6]
        if selected:
            try:
                value = tailor_once(master, job.description, requirements, selected, ModelConfig(settings.claude_model, min(3000, settings.claude_max_output_tokens)),
                                    "managed-by-environment", intentional=True, enabled=True, cache=RewriteCache(store), transport=claude.rewrite_transport())
                proposals = list(value["changes"])
            except ValueError as exc:
                notes.append("Claude rewriting unavailable; original wording kept: " + str(exc)[:200])
    reviewed_proposals = []
    for change in proposals:
        verdict = validate_change(change, master)
        reviewed_proposals.append({**asdict(change), "validation": verdict.status, "reasons": list(verdict.reasons)})
    approved = {c["source_ids"][0] for c in reviewed_proposals if c["validation"] == "review" and (c["source_ids"][0] in set(approved_change_ids) or settings.auto_accept_validated_rewrites)}
    bundle, final, attempt_projects = None, None, list(chosen)
    while True:
        draft = order_skills(select_projects(master, attempt_projects), requirements)
        final = apply_changes(master, draft, tuple(proposals), approved)
        try:
            bundle = export_resume(final)
            break
        except ExportError as exc:
            if len(attempt_projects) <= 1 or "page" not in str(exc).lower():
                raise
            notes.append(f"Dropped lowest-ranked project to fit one page: {attempt_projects[-1]}")
            attempt_projects = attempt_projects[:-1]
    before, after = score_job(job, master, store), score_job(job, final, store)
    omitted = [g for g in ranked if g not in attempt_projects]
    audit = {"source_hash": master.source_hash, "job_key": job_key, "selection_basis": selection_basis, "projects": attempt_projects,
             "omitted_projects": omitted, "omitted_bullet_ids": [], "approved_changes": [c for c in reviewed_proposals if c["source_ids"][0] in approved],
             "held_proposals": [c for c in reviewed_proposals if c["source_ids"][0] not in approved],
             "requirements": [asdict(r) for r in requirements], "pdf_pages": bundle.pages, "notes": notes, "generated_by": "autoapply"}
    metadata = {"company": job.company, "title": job.title, "source_hash": master.source_hash,
                "original_fit_estimate": before["fit"] if before else None, "tailored_fit_estimate": after["fit"] if after else None,
                "approved_change_ids": sorted(approved), "pdf_pages": bundle.pages, "official_url": job.url, "generated_by": "autoapply"}
    artifact_key = store.save_artifact(job_key, bundle, audit, metadata)
    artifact = next(a for a in store.artifacts() if a["key"] == artifact_key)
    return {"artifact_key": artifact_key, "pdf": artifact["pdf"], "docx": artifact["docx"], "projects": attempt_projects,
            "selection_basis": selection_basis, "omitted_projects": omitted, "proposals": reviewed_proposals,
            "approved_change_ids": sorted(approved), "fit_before": metadata["original_fit_estimate"],
            "fit_after": metadata["tailored_fit_estimate"], "notes": notes, "resume_hash": digest(bundle.pdf)}
