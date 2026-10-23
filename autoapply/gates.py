"""Hard eligibility gates layered on Hunter's existing decide()/score_job().

``prepare_ok`` lets the worker tailor a resume and pre-fill a form for human
review. ``auto_ok`` additionally allows submission without approval. Every
gate that cannot be verified counts as *unknown*, which blocks AUTO.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import re

from hunter.filtering import us_location
from hunter.models import Decision, Job

from .profile import ApplicantProfile, MONTHS

PRIORITY_DISCIPLINES = ("Embedded / Firmware", "PCB / Hardware", "Electrical", "FPGA / Semiconductor", "Robotics / Controls")
RELATED_DISCIPLINES = ("Validation / Test", "Power / RF")
TARGET_TERMS = ("Spring 2027", "Summer 2027")
# Titles that Hunter's broad "hardware"/"test" patterns can admit but are not
# electrical-engineering roles. "Electromechanical" is deliberately allowed.
# Never rescued, even when "hardware" also appears in the title.
UNRELATED_TITLE = re.compile(r"\b(?:mechanical|civil|chemical|supply chain|procurement|sourcing|finance|financial|accounting|marketing|sales|business|legal|recruit\w*|human resources|people operations|data scien\w*|machine learning|product manag\w*|ux|ui designer|communications|facilities)\b", re.I)
# Rescued only when an EE term is also present (e.g. "Embedded Software Engineer").
SOFT_UNRELATED_TITLE = re.compile(r"\b(?:software engineer(?:ing)?|software developer|manufacturing engineer(?:ing)?|industrial engineer(?:ing)?|quality engineer(?:ing)?)\b", re.I)
EE_RESCUE = re.compile(r"\b(?:embedded|firmware|electrical|hardware|fpga|pcb|electronics)\b", re.I)


@dataclass
class Check:
    name: str
    status: str  # pass, fail, unknown
    detail: str
    blocks: str = "auto"  # "prepare" also blocks preparation


@dataclass
class GateResult:
    eligible: bool
    prepare_ok: bool
    auto_ok: bool
    fit: float | None
    discipline: str
    terms: tuple[str, ...]
    checks: list[Check] = field(default_factory=list)

    def as_dict(self):
        return {**asdict(self), "terms": list(self.terms)}

    @property
    def summary(self):
        failing = [c for c in self.checks if c.status != "pass"]
        return "; ".join(f"{c.name}: {c.detail}" for c in failing) or "All gates passed."


def _month(name):
    return MONTHS.index(name.casefold()) + 1


def graduation_check(description: str, graduation: str) -> tuple[str, str]:
    """Conservative graduation-date compatibility. Unclear wording → unknown."""
    if not re.fullmatch(r"\d{4}-\d{2}", graduation or ""):
        return "unknown", "Applicant graduation month is not verified."
    text = re.sub(r"\s+", " ", description)
    sentences = [s for s in re.split(r"(?<=[.!?;])\s+|\n", text) if re.search(r"graduat|class of", s, re.I) and re.search(r"20\d{2}", s)]
    if not sentences:
        return "pass", "No graduation-date restriction stated."
    month_re = r"(?:(" + "|".join(MONTHS) + r")\s+)?(20\d{2})"
    results = []
    for sentence in sentences:
        low = sentence.casefold()
        if re.search(r"preferred|nice to have|a plus", low):
            continue
        found = re.findall(month_re, low)
        bounds = [f"{y}-{_month(m):02}" if m else y for m, y in found]
        if "between" in low and len(bounds) >= 2:
            lo, hi = bounds[0], bounds[1]
            lo_cmp = lo if len(lo) == 7 else lo + "-01"
            hi_cmp = hi if len(hi) == 7 else hi + "-12"
            results.append(("pass" if lo_cmp <= graduation <= hi_cmp else "fail", f"Required graduation window {lo}–{hi}; applicant {graduation}."))
        elif len(bounds) == 1 and (re.search(month_re + r",?\s+or (?:later|after)", low) or re.search(r"(?:no earlier than|after)\s+" + month_re, low)):
            bound = bounds[0] if len(bounds[0]) == 7 else bounds[0] + "-01"
            results.append(("pass" if graduation >= bound else "fail", f"Graduation must be {bounds[0]} or later; applicant {graduation}."))
        elif len(bounds) == 1 and (re.search(r"(?:\bby|before|no later than|prior to)\s+" + month_re, low) or re.search(month_re + r",?\s+or (?:earlier|before)", low)):
            bound = bounds[0] if len(bounds[0]) == 7 else bounds[0] + "-12"
            results.append(("pass" if graduation <= bound else "fail", f"Graduation must be by {bounds[0]}; applicant {graduation}."))
        elif len(bounds) == 1 and re.search(r"graduating in|graduate in|graduation (?:date )?(?:in|of)|class of", low):
            bound = bounds[0]
            ok = graduation.startswith(bound) if len(bound) == 4 else graduation == bound
            results.append(("pass" if ok else "fail", f"Posting targets graduation {bound}; applicant {graduation}."))
        else:
            results.append(("unknown", "Graduation wording needs review: " + sentence.strip()[:200]))
    if not results:
        return "pass", "Only preferred graduation timing mentioned."
    for status in ("fail", "unknown"):
        hit = next((r for r in results if r[0] == status), None)
        if hit:
            return hit
    return "pass", results[0][1]


def start_check(description: str, terms, profile: ApplicantProfile) -> tuple[str, str]:
    """Compare any explicit start month with the verified earliest start."""
    match = re.search(r"(?:start(?:ing|s)?(?: date)?|begin(?:ning|s)?)\D{0,25}?(" + "|".join(MONTHS) + r")\s+(20\d{2})", description, re.I)
    if not match:
        return "pass", "No explicit start month in the posting."
    start = date(int(match[2]), _month(match[1]), 1)
    if not profile.earliest_start:
        return "unknown", f"Posting starts {match[1].title()} {match[2]}; earliest start date is not verified."
    earliest = date.fromisoformat(profile.earliest_start)
    if (start.year, start.month) < (earliest.year, earliest.month):
        return "fail", f"Posting starts {match[1].title()} {match[2]}, before your verified earliest start {profile.earliest_start}."
    return "pass", f"Starts {match[1].title()} {match[2]}; within verified availability."


def evaluate_gates(job: Job, decision: Decision, match: dict | None, profile: ApplicantProfile, *, master_hash,
                   permission: dict, settings, target_terms=TARGET_TERMS, allowed_disciplines=PRIORITY_DISCIPLINES + RELATED_DISCIPLINES) -> GateResult:
    checks: list[Check] = []

    def add(name, status, detail, blocks="auto"):
        checks.append(Check(name, status, detail, blocks))

    # Posting availability and source.
    if job.imported_at:
        add("Posting source", "unknown" if job.source_status == "user-confirmed" else "fail",
            "Imported listings can be prepared after user confirmation but are never auto-submitted.", "auto" if job.source_status == "user-confirmed" else "prepare")
    elif job.source_status != "open":
        add("Posting source", "fail", f"Posting availability is '{job.source_status}', not open.", "prepare")
    else:
        add("Posting source", "pass", "Open on an official public source at last check.")
    if len(job.description.split()) < 40:
        add("Job description", "fail", "Full description unavailable; nothing is tailored from a partial posting.", "prepare")
    # Hunter's existing exclusions (closed, not internship, wrong term/year, non-U.S., unmet hard requirement).
    for reason in decision.reasons:
        add("Hunter eligibility", "fail", reason, "prepare")
    if decision.eligible:
        add("Hunter eligibility", "pass", "Passes existing Hunter filters.")
    # Discipline.
    if UNRELATED_TITLE.search(job.title) or (SOFT_UNRELATED_TITLE.search(job.title) and not EE_RESCUE.search(job.title)):
        add("Discipline", "fail", "Title indicates an unrelated discipline.", "prepare")
    elif decision.discipline not in allowed_disciplines:
        add("Discipline", "fail", f"Discipline '{decision.discipline}' is not an enabled priority field.", "prepare")
    else:
        add("Discipline", "pass", decision.discipline)
    # Term and applicant availability (never assumed).
    terms = tuple(t for t in decision.terms if t in target_terms)
    if not decision.terms:
        add("Internship term", "unknown", "Posting does not state a term; availability cannot be matched.", "auto")
    elif not terms:
        add("Internship term", "fail", "Posting term is outside Spring/Summer 2027.", "prepare")
    else:
        add("Internship term", "pass", ", ".join(terms))
        need_all = bool(re.search(r"co[ -]?op", job.title, re.I)) and len(decision.terms) > 1
        available = set(profile.available_terms)
        ok = set(decision.terms) <= available if need_all else bool(set(terms) & available)
        if not available:
            add("Applicant availability", "fail", "No verified internship-term availability in the applicant profile.", "prepare")
        elif ok:
            add("Applicant availability", "pass", "Verified available for " + ", ".join(sorted(set(decision.terms) & available)))
        else:
            add("Applicant availability", "fail", "Applicant availability does not cover " + ", ".join(decision.terms) + ".", "prepare")
    status, detail = start_check(job.description, decision.terms, profile)
    add("Start date", status, detail, "prepare" if status == "fail" else "auto")
    status, detail = graduation_check(job.description, profile.graduation)
    add("Graduation date", status, detail, "prepare" if status == "fail" else "auto")
    location = us_location(job)
    add("U.S. location", {"met": "pass", "unmet": "fail", "unknown": "unknown"}[location],
        {"met": "United States", "unmet": "Outside the United States", "unknown": "Location needs confirmation"}[location], "prepare" if location == "unmet" else "auto")
    # Remaining eligibility flags from Hunter (authorization, standing, clearance...).
    unknown_flags = [f for f in decision.flags if f.status == "unknown"]
    unmet_flags = [f for f in decision.flags if f.status == "unmet"]
    if unmet_flags:
        add("Hard requirements", "fail", "; ".join(f.requirement[:160] for f in unmet_flags), "prepare")
    elif unknown_flags:
        add("Hard requirements", "unknown", "Unverified: " + "; ".join(f.requirement[:160] for f in unknown_flags[:6]))
    else:
        add("Hard requirements", "pass", "All detected conditions verified as met.")
    # Fit thresholds.
    fit = match["fit"] if match else None
    if fit is None:
        add("Fit threshold", "fail", "No fit score (verified master or full description missing).", "prepare")
    elif fit < settings.min_fit_review:
        add("Fit threshold", "fail", f"Fit {fit:g} is below the review minimum {settings.min_fit_review:g}.", "prepare")
    elif fit < settings.min_fit_auto:
        add("Fit threshold", "unknown", f"Fit {fit:g} is below the AUTO minimum {settings.min_fit_auto:g}; review required.")
    else:
        add("Fit threshold", "pass", f"Fit {fit:g} ≥ {settings.min_fit_auto:g}.")
    # Profile and employer permission.
    if profile.is_verified(master_hash):
        add("Applicant profile", "pass", "Verified and consistent with the active master resume.")
    else:
        add("Applicant profile", "fail", "Applicant profile is not verified against the active master resume.", "prepare")
    if not permission.get("prefill"):
        add("Employer permission", "fail", "Form preparation is not enabled for this employer.", "prepare")
    elif not (permission.get("auto_submit") and permission.get("terms_reviewed_at")):
        add("Employer permission", "unknown", "Automatic submission is not enabled for this employer; approval required.")
    else:
        add("Employer permission", "pass", "Preparation and automatic submission enabled after terms review.")
    prepare_ok = not any(c.status == "fail" for c in checks)
    auto_ok = prepare_ok and all(c.status == "pass" for c in checks)
    return GateResult(decision.eligible, prepare_ok, auto_ok, fit, decision.discipline, decision.terms, checks)
