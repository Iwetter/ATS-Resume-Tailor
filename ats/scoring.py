"""Explainable, reproducible heuristics; no vendor ATS claims and no LLM."""
from __future__ import annotations

import re
from .matching import ACTION, demonstrated, match_requirement, tokens
from .models import Analysis, Extracted, Requirement, Resume

DEFAULT_WEIGHTS = {"skill_coverage": 40.0, "experience_evidence": 30.0, "responsibilities": 20.0, "clarity": 10.0}
PRIORITY = {"required": 2.0, "preferred": 1.0, "context": .5}


def analyze(resume: Resume, extracted: Extracted, requirements: tuple[Requirement, ...], weights: dict[str, float] | None = None) -> Analysis:
    weights = DEFAULT_WEIGHTS if weights is None else weights
    if set(weights) != set(DEFAULT_WEIGHTS) or any(v < 0 for v in weights.values()) or sum(weights.values()) <= 0:
        raise ValueError("Scoring weights must be nonnegative with a positive total.")
    matches = tuple(match_requirement(r, resume) for r in requirements)
    by_req = {r.id: r for r in requirements}
    warnings = list(extracted.warnings)
    sections = {x.section for x in resume.items if x.kind == "section"}
    body = resume.text
    email = bool(re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", body))
    phone = bool(re.search(r"(?:\+?\d[\d ().-]{7,}\d)", "\n".join(x.text for x in resume.items if x.section == "Header")))
    name = any(x.kind == "header" and len(x.text.split()) >= 2 and re.search(r"[A-Za-z]", x.text) for x in resume.items[:2])
    bullets = list({x.text: x for x in resume.items if x.kind == "bullet"}.values())
    checks = {
        "extraction / 30": 30.0 if len(body.split()) >= 80 else 20.0 if len(body.split()) >= 30 else 5.0,
        "sections / 25": (10.0 if "Education" in sections else 0) + (10.0 if sections & {"Experience", "Projects"} else 0) + (5.0 if "Skills" in sections else 0),
        "contact / 20": (8.0 if email else 0) + (6.0 if phone else 0) + (6.0 if name else 0),
        "structure / 15": (10.0 if bullets else 0) + (5.0 if not any("column" in w.lower() or "table" in w.lower() for w in warnings) else 0),
        "format / 10": max(0.0, 10.0 - 2.0 * len(set(warnings))),
    }
    if not email:
        warnings.append("No email address detected.")
    if not phone:
        warnings.append("No phone number detected in the header.")
    if "Education" not in sections:
        warnings.append("Education section not detected; review section labels.")
    if not sections & {"Experience", "Projects"}:
        warnings.append("Experience or Projects section not detected.")
    if not bullets:
        warnings.append("No accomplishment bullets detected; review the evidence ledger.")

    def weighted(kind: str, evidence_only=False) -> float:
        relevant = [m for m in matches if by_req[m.requirement_id].kind == kind]
        if not relevant:
            return 0.0  # No inferred perfect score for unmeasured dimensions.
        numerator = 0.0
        for m in relevant:
            value = 1.0 if m.status == "supported" else .5 if m.status == "partial" and not evidence_only else 0.0
            numerator += value * PRIORITY[by_req[m.requirement_id].priority]
        return 100 * numerator / sum(PRIORITY[by_req[m.requirement_id].priority] for m in relevant)

    target = set().union(*(tokens(" ".join(r.terms) or r.text) for r in requirements if r.kind != "eligibility")) if requirements else set()
    clarity = []
    for item in bullets:
        words = item.text.split()
        quality = .4 * bool(ACTION.match(item.text)) + .3 * (8 <= len(words) <= 42) + .3 * bool(tokens(item.text) & target)
        # Repeating keywords cannot improve this component.
        unique_ratio = len(set(w.lower() for w in words)) / max(1, len(words))
        clarity.append(quality * min(1.0, unique_ratio / .65))
    components = {"skill_coverage": weighted("skill"), "experience_evidence": weighted("skill", True), "responsibilities": weighted("responsibility"), "clarity": 100 * sum(clarity) / max(1, len(clarity))}
    alignment = sum(components[k] * weights[k] for k in weights) / sum(weights.values())
    improvements = []
    for r, m in zip(requirements, matches):
        label = "/".join(r.terms) or r.text
        if r.kind == "eligibility" and m.status != "met":
            improvements.append(f"Verify eligibility ({m.status}): {r.text}")
        elif r.priority == "required" and m.status == "missing" and r.kind == "skill":
            improvements.append(f"Not on your resume: {label}. If you have real experience with it, add a bullet showing it to your master; it is never added automatically.")
        elif r.priority == "required" and m.status == "partial" and r.kind == "skill":
            improvements.append(f"Listed but not demonstrated: {label}. Add or reword a bullet that shows you using it, if you have.")
        elif r.priority == "required" and m.status == "missing":
            improvements.append(f"No closely related experience shown for: {label[:120]}. Highlight related work if you have it.")
    for kind in ("skill", "responsibility"):
        if not any(r.kind == kind for r in requirements):
            warnings.append(f"No {kind} requirements measured; that score component is 0. Edit the requirement list.")
    return Analysis(round(sum(checks.values()), 1), round(alignment, 1), {k: round(v, 1) for k, v in components.items()}, checks, matches, tuple(dict.fromkeys(warnings)), tuple(improvements[:15]))
