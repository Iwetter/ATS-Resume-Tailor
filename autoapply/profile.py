"""Verified applicant profile: the only source of form answers.

The profile is stored in the private SQLite database (never in Git). Any edit
clears its verification. Submission stays disabled until the profile is both
complete and consistent with the verified master resume saved in Hunter.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
import re

from ats.models import digest
from hunter.models import utcnow
from hunter.service import profile_from_master
from hunter.models import Profile

from .redaction import register_secret

TERMS = ("Spring 2027", "Summer 2027", "Fall 2027")
MONTHS = "january february march april may june july august september october november december".split()


@dataclass(frozen=True)
class ApplicantProfile:
    first_name: str = ""
    last_name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""
    country: str = "United States"
    linkedin: str = ""
    github: str = ""
    website: str = ""
    school: str = ""
    degree: str = ""          # e.g. "Bachelor of Science"
    major: str = ""           # e.g. "Electrical Engineering"
    graduation: str = ""      # YYYY-MM
    gpa: str = ""             # blank = not disclosed; never estimated
    us_citizen: bool | None = None
    work_authorized: bool | None = None
    sponsorship_needed: bool | None = None
    available_terms: tuple[str, ...] = ()
    earliest_start: str = ""  # YYYY-MM-DD; required for any Spring term
    willing_to_relocate: bool | None = None
    verified_at: str | None = None
    verified_hash: str | None = None
    verified_master: str | None = None

    @property
    def content_hash(self):
        return digest({k: v for k, v in asdict(self).items() if not k.startswith("verified_")})

    @property
    def full_name(self):
        return " ".join(x for x in (self.first_name.strip(), self.last_name.strip()) if x)

    def is_verified(self, master_hash=None):
        return bool(self.verified_at and self.verified_hash == self.content_hash and (master_hash is None or self.verified_master == master_hash))

    def as_public_dict(self):
        """Field names and whether each is filled: safe to show to a model."""
        return {k: bool(v) if not isinstance(v, bool) else True for k, v in asdict(self).items() if not k.startswith("verified_")}


def load_profile(store) -> ApplicantProfile:
    raw = store.get("applicant_profile") or {}
    known = {f.name for f in fields(ApplicantProfile)}
    raw = {k: v for k, v in raw.items() if k in known}
    if "available_terms" in raw:
        raw["available_terms"] = tuple(raw["available_terms"])
    profile = ApplicantProfile(**raw)
    for value in (profile.full_name, profile.first_name, profile.last_name, profile.email, profile.phone):
        register_secret(value)
    return profile


def save_profile(store, profile: ApplicantProfile, audit=None):
    """Saving always clears verification; verification is a separate act."""
    profile = replace(profile, verified_at=None, verified_hash=None, verified_master=None,
                      available_terms=tuple(t for t in TERMS if t in profile.available_terms))
    store.set("applicant_profile", asdict(profile))
    if audit:
        audit("user", "profile_saved", details={"fields_filled": sorted(k for k, v in profile.as_public_dict().items() if v)})
    return profile


def verify_profile(store, master, audit=None):
    profile = load_profile(store)
    issues = validate_profile(profile, master)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        raise ValueError("Profile cannot be verified: " + "; ".join(i["message"] for i in errors))
    profile = replace(profile, verified_at=utcnow(), verified_hash=profile.content_hash, verified_master=master.source_hash)
    store.set("applicant_profile", asdict(profile))
    if audit:
        audit("user", "profile_verified", details={"master": master.source_hash[:16]})
    return profile


def _norm(text):
    return re.sub(r"[^a-z0-9@.+]", "", (text or "").casefold())


def validate_profile(profile: ApplicantProfile, master) -> list[dict]:
    issues = []

    def error(field_name, message):
        issues.append({"level": "error", "field": field_name, "message": message})

    def warn(field_name, message):
        issues.append({"level": "warning", "field": field_name, "message": message})

    for name in ("first_name", "last_name", "email", "phone", "city", "state", "school", "degree", "major", "graduation"):
        if not str(getattr(profile, name)).strip():
            error(name, f"{name.replace('_', ' ').title()} is required.")
    if profile.email and not re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", profile.email.strip()):
        error("email", "Email address is not valid.")
    if profile.phone and len(re.sub(r"\D", "", profile.phone)) not in (10, 11):
        error("phone", "Use a 10-digit U.S. phone number.")
    if profile.graduation:
        try:
            datetime.strptime(profile.graduation, "%Y-%m")
        except ValueError:
            error("graduation", "Graduation must be YYYY-MM.")
    if profile.gpa:
        try:
            if not 0 <= float(profile.gpa) <= 4.0:
                raise ValueError
        except ValueError:
            error("gpa", "GPA must be a number between 0 and 4.0, or blank to leave GPA questions for review.")
    for name, label in (("work_authorized", "U.S. work authorization"), ("sponsorship_needed", "Visa sponsorship need")):
        if getattr(profile, name) is None:
            error(name, f"{label} must be answered explicitly (Yes/No).")
    if profile.work_authorized is False and profile.sponsorship_needed is False:
        warn("sponsorship_needed", "Not authorized but no sponsorship needed is unusual; confirm both answers.")
    if not profile.available_terms:
        error("available_terms", "Select at least one internship term you are verified to be available for.")
    if "Spring 2027" in profile.available_terms:
        if not profile.earliest_start:
            error("earliest_start", "Spring 2027 availability needs an explicit earliest start date; January 2027 is never assumed.")
    if profile.earliest_start:
        try:
            date.fromisoformat(profile.earliest_start)
        except ValueError:
            error("earliest_start", "Earliest start must be YYYY-MM-DD.")
    for name in ("linkedin", "github", "website"):
        value = getattr(profile, name)
        if value and not re.match(r"https://", value):
            error(name, f"{name.title()} must be a full https:// URL.")
    if master is None:
        error("master", "Save a verified master resume in Internship Hunter first.")
        return issues
    header = "\n".join(x.text for x in master.items if x.section == "Header")
    education = "\n".join(x.text for x in master.items if x.section == "Education")
    if profile.email and _norm(profile.email) not in _norm(header):
        error("email", "Email does not appear in the verified master resume header.")
    if profile.full_name and _norm(profile.full_name) not in _norm(header) and _norm(profile.last_name) not in _norm(header):
        error("last_name", "Name does not match the verified master resume header.")
    if profile.school and _norm(profile.school) not in _norm(education):
        warn("school", "School name was not found verbatim in the resume Education section; confirm spelling.")
    if profile.major and _norm(profile.major) not in _norm(education):
        warn("major", "Major was not found verbatim in the resume Education section.")
    from_master = profile_from_master(Profile(graduation="", gpa=-1.0, degree=""), master)
    if from_master.graduation and profile.graduation and from_master.graduation != profile.graduation:
        error("graduation", f"Profile graduation {profile.graduation} differs from the resume ({from_master.graduation}).")
    if from_master.gpa >= 0 and profile.gpa and abs(from_master.gpa - float(profile.gpa or 0)) > 1e-6:
        error("gpa", f"Profile GPA {profile.gpa} differs from the resume GPA {from_master.gpa:g}.")
    if from_master.gpa >= 0 and not profile.gpa:
        warn("gpa", "Resume lists a GPA but the profile leaves it blank; GPA questions will need review.")
    return issues


def hunter_profile(profile: ApplicantProfile, fallback: Profile) -> Profile:
    """Feed verified applicant facts into Hunter's existing eligibility rules."""
    gpa = float(profile.gpa) if profile.gpa else fallback.gpa
    degree = " ".join(x for x in (profile.degree, profile.major) if x) or fallback.degree
    return replace(fallback, university=profile.school or fallback.university, degree=degree,
                   graduation=profile.graduation or fallback.graduation, gpa=gpa,
                   us_citizen=profile.us_citizen, us_authorized=profile.work_authorized,
                   sponsorship_needed=profile.sponsorship_needed)


def field_value(profile: ApplicantProfile, key: str):
    """Canonical verified value for a form concept, or None if unverified."""
    grad = profile.graduation
    month_name = MONTHS[int(grad[5:7]) - 1].title() if re.fullmatch(r"\d{4}-\d{2}", grad or "") else ""
    values = {
        "first_name": profile.first_name, "last_name": profile.last_name, "full_name": profile.full_name,
        "preferred_name": profile.preferred_name or profile.first_name, "email": profile.email, "phone": profile.phone,
        "city": profile.city, "state": profile.state, "country": profile.country,
        "location": ", ".join(x for x in (profile.city, profile.state) if x),
        "linkedin": profile.linkedin, "github": profile.github, "website": profile.website or profile.github,
        "school": profile.school, "degree": profile.degree, "major": profile.major,
        "graduation_date": f"{month_name} {grad[:4]}" if month_name else "", "graduation_year": grad[:4] if grad else "",
        "graduation_month": month_name, "gpa": profile.gpa,
        "us_citizen": profile.us_citizen, "work_authorized": profile.work_authorized,
        "sponsorship_needed": profile.sponsorship_needed, "willing_to_relocate": profile.willing_to_relocate,
        "earliest_start": profile.earliest_start,
    }
    value = values.get(key)
    return None if value in ("", None) else value
