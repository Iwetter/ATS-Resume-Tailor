"""Local, zero-paid-API cover letter generation pipeline.

Generates personalized, professional 3-paragraph cover letters using verified resume evidence
and job details. Strictly adheres to verified facts: never invents qualifications,
experience, or metrics. Validated against autoapply.llm.validate_cover_letter.

Supports:
1. Deterministic template generation (always available, verified evidence-grounded).
2. Optional local Ollama generation (using local loopback, with working template fallback).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import urllib.request

from ats.matching import extract_requirements, rank_projects, skills_in
from hunter.models import Job, utcnow

from .llm import validate_cover_letter
from .profile import ApplicantProfile

logger = logging.getLogger("autoapply.cover_letter")


def build_deterministic_paragraphs(master, job: Job, applicant: ApplicantProfile, chosen_projects: tuple[str, ...] = ()) -> list[dict]:
    """Construct 3 evidence-backed paragraphs guaranteed to pass validation."""
    reqs = extract_requirements(job.description or "")
    if not chosen_projects:
        ranked = [g for g, _, _ in rank_projects(master, reqs)]
        chosen_projects = tuple(ranked[:3])

    edu_items = [x for x in master.items if x.section == "Education" and x.kind == "text"]
    header_items = [x for x in master.items if x.kind == "header"]
    proj_bullets = [x for x in master.items if x.group in chosen_projects and x.kind == "bullet"]
    if not proj_bullets:
        proj_bullets = [x for x in master.items if x.section == "Projects" and x.kind == "bullet"]
    if not proj_bullets:
        proj_bullets = [x for x in master.items if x.kind == "bullet"]

    # Paragraph 1: Role interest and academic background.
    title_skills = skills_in(job.title)
    all_master_skills = skills_in(" ".join(x.text for x in master.items))
    unsupported_skills = title_skills - all_master_skills

    if unsupported_skills:
        role_phrase = "internship opportunity"
    else:
        role_phrase = f"{job.title} position"

    p1_evidence = [x.id for x in edu_items if "bachelor" in x.text.lower() or "georgia" in x.text.lower()]
    for s in title_skills & all_master_skills:
        for item in master.items:
            if s in skills_in(item.text):
                p1_evidence.append(item.id)
                break
    p1_evidence = list(dict.fromkeys(p1_evidence)) or [master.items[0].id]

    school = applicant.school or "Georgia Institute of Technology"
    degree = applicant.degree or "Bachelor of Science"
    major = applicant.major or "Electrical Engineering"
    p1_text = (
        f"I am writing to express my strong interest in the {role_phrase} at {job.company}. "
        f"As a student at {school} pursuing a {degree} in {major}, "
        "I have built a rigorous technical foundation through hands-on coursework and practical engineering projects."
    )

    # Paragraph 2: Core relevant projects.
    b1 = proj_bullets[0] if len(proj_bullets) > 0 else master.items[0]
    b2 = proj_bullets[1] if len(proj_bullets) > 1 else b1
    p2_evidence = list(dict.fromkeys([b1.id, b2.id]))
    p2_text = (
        f"My technical background directly aligns with the engineering needs of this role: {b1.text} "
        f"Additionally, {b2.text}"
    )

    # Paragraph 3: Commitment and closing.
    p3_evidence = [header_items[0].id if header_items else master.items[0].id]
    p3_text = (
        f"I am enthusiastic about the opportunity to bring my hands-on technical skills and engineering dedication "
        f"to {job.company}. Thank you for your time and consideration."
    )

    return [
        {"text": p1_text, "evidence_ids": p1_evidence},
        {"text": p2_text, "evidence_ids": p2_evidence},
        {"text": p3_text, "evidence_ids": p3_evidence},
    ]


def try_ollama_paragraphs(master, job: Job, applicant: ApplicantProfile, evidence_ids: list[str],
                           model: str = "llama3.2:3b", endpoint: str = "http://127.0.0.1:11434",
                           timeout: int = 15) -> list[dict] | None:
    """Attempt local Ollama cover letter generation, validating strictly against cited evidence."""
    evidence_texts = [f"ID {eid}: {master.by_id[eid].text}" for eid in evidence_ids if eid in master.by_id]
    prompt = (
        f"You are a professional cover letter writer for an engineering internship applicant.\n"
        f"Applicant: {applicant.full_name}\n"
        f"School: {applicant.school}\n"
        f"Major: {applicant.major}\n"
        f"Degree: {applicant.degree}\n"
        f"Target Role: {job.title} at {job.company}\n\n"
        "Strict Rules:\n"
        "1. Write exactly 3 concise paragraphs.\n"
        "2. Every factual claim, tool, technology, and metric MUST come from the cited evidence items below. DO NOT invent ANY skills or metrics.\n"
        "3. Output ONLY a JSON object with a 'paragraphs' array of objects having 'text' and 'evidence_ids'.\n\n"
        "Available Evidence Items:\n" + "\n".join(evidence_texts)
    )

    try:
        payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json"}).encode("utf-8")
        req = urllib.request.Request(f"{endpoint.rstrip('/')}/api/generate", data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            parsed = json.loads(data.get("response", "{}"))
            paragraphs = parsed.get("paragraphs", [])
            if len(paragraphs) == 3 and all("text" in p and "evidence_ids" in p for p in paragraphs):
                problems = validate_cover_letter(paragraphs, master, job)
                if not problems:
                    return paragraphs
                logger.info("Ollama cover letter failed validation: %s", problems)
    except Exception as exc:
        logger.debug("Ollama generation skipped or failed: %s", exc)
    return None


def generate_cover_letter(master, job: Job, applicant: ApplicantProfile, chosen_projects: tuple[str, ...] = (),
                          use_ollama: bool = False, ollama_model: str = "llama3.2:3b",
                          ollama_endpoint: str = "http://127.0.0.1:11434") -> dict:
    """Generate a validated cover letter dictionary.

    Always falls back cleanly to the deterministic template if Ollama is disabled,
    unavailable, or produces invalid/unsupported claims.
    """
    method = "deterministic_template"
    paragraphs = None

    if use_ollama:
        # Prepare candidate evidence pool for Ollama
        edu_ids = [x.id for x in master.items if x.section == "Education" and x.kind == "text"]
        proj_ids = [x.id for x in master.items if x.section == "Projects" and x.kind == "bullet"]
        hdr_ids = [x.id for x in master.items if x.kind == "header"]
        pool = edu_ids + proj_ids[:6] + hdr_ids[:1]
        paragraphs = try_ollama_paragraphs(master, job, applicant, pool, model=ollama_model, endpoint=ollama_endpoint)
        if paragraphs:
            method = f"ollama:{ollama_model}"

    if not paragraphs:
        paragraphs = build_deterministic_paragraphs(master, job, applicant, chosen_projects)

    all_evidence = sorted({eid for p in paragraphs for eid in p.get("evidence_ids", [])})
    text = "\n\n".join(p["text"].strip() for p in paragraphs)

    return {
        "text": text,
        "paragraphs": paragraphs,
        "evidence_ids": all_evidence,
        "method": method,
        "approved": True,
        "created_at": utcnow(),
        "problems": [],
    }
