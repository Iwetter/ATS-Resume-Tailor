"""Explainable, reproducible heuristics; no vendor ATS claims and no LLM."""
from __future__ import annotations

import re
from .matching import ACTION, PAST_TENSE_LEAD, match_requirement, tokens
from .models import Analysis, Extracted, Requirement, Resume

# Stated qualifications that the resume can verify (degree, GPA, months of experience) count;
# ones it cannot (availability, export control) are left out, neither credited nor penalised.
DEFAULT_WEIGHTS = {"qualifications": 25.0, "skill_coverage": 30.0, "experience_evidence": 20.0, "responsibilities": 10.0, "soft_skills": 5.0, "clarity": 10.0}
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

    def value(m, evidence_only=False):
        if m.status in ("met", "unmet"):
            return 1.0 if m.status == "met" else 0.0
        return 1.0 if m.status == "supported" else .5 if m.status == "partial" and not evidence_only else 0.0

    def scored(m, kind):
        # An eligibility condition the resume cannot show ("unknown") is not part of the score.
        return by_req[m.requirement_id].kind == kind and not (kind == "eligibility" and m.status == "unknown")

    # Which requirements feed which component. Eligibility (mandatory conditions) is reported
    # separately and never scored; soft skills are their own small component.
    feeds = {"qualifications": ("eligibility", False), "skill_coverage": ("skill", False), "experience_evidence": ("skill", True), "responsibilities": ("responsibility", False), "soft_skills": ("soft_skill", False)}

    def weighted(component):
        kind, evidence_only = feeds[component]
        relevant = [m for m in matches if scored(m, kind)]
        if not relevant:
            return None  # not measured for this posting: excluded from the average, not scored 0
        total = sum(PRIORITY[by_req[m.requirement_id].priority] for m in relevant)
        return 100 * sum(value(m, evidence_only) * PRIORITY[by_req[m.requirement_id].priority] for m in relevant) / total

    target = set().union(*(tokens(" ".join(r.terms) or r.text) for r in requirements if r.kind != "eligibility")) if requirements else set()
    clarity = []
    for item in bullets:
        words = item.text.split()
        quality = .4 * bool(ACTION.match(item.text) or PAST_TENSE_LEAD.match(item.text)) + .3 * (8 <= len(words) <= 42) + .3 * bool(tokens(item.text) & target)
        # Repeating keywords cannot improve this component.
        unique_ratio = len(set(w.lower() for w in words)) / max(1, len(words))
        clarity.append(quality * min(1.0, unique_ratio / .65))
    raw = {name: weighted(name) for name in feeds}
    raw["clarity"] = 100 * sum(clarity) / max(1, len(clarity))
    measured = {k: v for k, v in raw.items() if v is not None and weights.get(k, 0) > 0}
    weight_total = sum(weights[k] for k in measured) or 1.0
    alignment = sum(measured[k] * weights[k] for k in measured) / weight_total
    components = {k: (round(v, 1) if v is not None else 0.0) for k, v in raw.items()}
    unmeasured = tuple(k for k, v in raw.items() if v is None)
    for name in unmeasured:
        warnings.append(f"No {name.replace('_', ' ')} requirements in this posting; that component is left out of the alignment score rather than scored 0.")

    # Points each requirement costs: its share of its component(s) times what is not yet shown.
    lost = {}
    for component, (kind, evidence_only) in feeds.items():
        if component not in measured:
            continue
        relevant = [m for m in matches if scored(m, kind)]
        total = sum(PRIORITY[by_req[m.requirement_id].priority] for m in relevant)
        for m in relevant:
            share = 100 * weights[component] / weight_total * PRIORITY[by_req[m.requirement_id].priority] / total
            lost[m.requirement_id] = lost.get(m.requirement_id, 0.0) + share * (1 - value(m, evidence_only))
    deductions = tuple(sorted(((rid, round(points, 1)) for rid, points in lost.items() if points >= .05), key=lambda x: -x[1]))

    improvements = []
    for r, m in zip(requirements, matches):
        if r.kind == "eligibility" and m.status != "met" and r.priority == "required":
            improvements.append(f"Verify (not scored): {r.text[:160]}")
    status_by_id = {m.requirement_id: m.status for m in matches}
    for rid, points in deductions:
        r = by_req[rid]
        label = "/".join(r.terms) if r.kind in ("skill", "soft_skill") else r.text[:120]
        status = status_by_id[rid]
        if r.kind == "eligibility":
            text = f"Stated qualification not met: {label}"
        elif r.kind == "skill" and status == "missing":
            text = f"Not on your resume: {label} ({r.priority}). If you have real experience with it, add a bullet that shows it"
        elif r.kind == "skill":
            text = f"Listed but not demonstrated: {label} ({r.priority}). Add or reword a bullet that shows you using it"
        elif r.kind == "soft_skill":
            text = f"{label}: {'make it explicit in a bullet' if status == 'partial' else 'no bullet shows it'} (who you worked with, what you documented or solved)"
        else:
            text = f"Duty with little related evidence: {label}. Highlight related work if you have it"
        improvements.append(f"{text}. Worth up to {points:g} alignment points.")
    for kind in ("skill", "responsibility"):
        if not any(r.kind == kind for r in requirements):
            warnings.append(f"No {kind} requirements measured. Check the requirement list.")
    counts = {}
    for r, m in zip(requirements, matches):
        group = {"eligibility": "Mandatory conditions (not scored)", "soft_skill": "Soft skills"}.get(r.kind) or ("Duties" if r.kind == "responsibility" else f"{r.priority.title()} technical skills")
        counts.setdefault(group, {}).setdefault(m.status, 0)
        counts[group][m.status] += 1
    return Analysis(round(sum(checks.values()), 1), round(alignment, 1), components, checks, matches, tuple(dict.fromkeys(warnings)), tuple(improvements[:20]),
                    deductions, unmeasured, counts)
