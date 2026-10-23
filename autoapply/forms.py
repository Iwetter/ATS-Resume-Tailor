"""Classify application-form questions and resolve answers from verified data.

Resolution order: verified profile concept → verified answer bank → (optional)
Claude mapping to a verified field name. Anything else is unresolved. A
required unresolved field puts the application into Needs Human Action.
Nothing here composes free text.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re

from ats.models import digest

from .profile import ApplicantProfile, field_value


@dataclass
class FormField:
    fid: str                    # stable selector within the page
    label: str
    kind: str                   # text, email, tel, url, number, textarea, select, radio, checkbox, checkbox_group, file, combobox, date
    required: bool = False
    options: list[str] = field(default_factory=list)
    name: str = ""
    accept: str = ""

    def signature(self):
        return {"label": norm(self.label), "kind": self.kind, "required": self.required, "options": [norm(o) for o in self.options]}


@dataclass
class Resolution:
    fid: str
    label: str
    kind: str
    required: bool
    concept: str
    source: str                 # profile, answer_bank, resume, cover_letter, attestation, model_mapping, blank, unresolved
    value: object = None        # text, option label, bool for checkbox, or file path
    reason: str = ""

    @property
    def filled(self):
        return self.value not in (None, "")

    def public(self):
        """Audit-safe view: never includes the value itself."""
        data = asdict(self)
        data["value"] = "[file]" if self.kind == "file" and self.filled else ("[set]" if self.filled else "")
        return data


def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9?/ ]", " ", (text or "").casefold().replace("*", " "))).strip()


CONCEPTS = (
    ("resume", r"\b(resume|cv|curriculum vitae)\b"),
    ("cover_letter", r"\bcover letter\b"),
    ("eeo", r"\b(gender|sex|race|racial|ethnicity|hispanic|latino|veteran|disability|disabled|pronoun|sexual orientation|transgender|lgbtq)\b"),
    ("attestation", r"\b(certify|certification that|attest|acknowledge|i agree|agree to|consent|terms (of|and)|privacy (policy|notice)|true and (complete|correct|accurate)|accurate to the best|electronic signature|sign(ature)?)\b"),
    ("sponsorship_needed", r"\b(sponsor(ship)?|visa|h-?1b)\b"),
    ("work_authorized", r"\b(legally )?(authori[sz]ed|eligible|permitted) to work\b|\bwork authori[sz]ation\b|\bright to work\b"),
    ("us_citizen", r"\b(u\.?s\.? )?citizen(ship)?\b"),
    ("willing_to_relocate", r"\brelocat"),
    ("first_name", r"^(legal )?first name|^given name"),
    ("last_name", r"^(legal )?last name|^surname|^family name"),
    ("preferred_name", r"^preferred (first )?name"),
    ("full_name", r"^(full )?name$|^full legal name"),
    ("email", r"\be-?mail\b"),
    ("phone", r"\b(phone|mobile|cell)\b"),
    ("linkedin", r"\blinked ?in\b"),
    ("github", r"\bgithub\b"),
    ("website", r"\b(website|portfolio|personal site)\b"),
    ("gpa", r"\bgpa\b|grade point"),
    ("graduation_date", r"\b(graduation|grad) (date|month)\b|when (do|will) you graduate|expected graduation"),
    ("graduation_year", r"\b(graduation|grad) year\b"),
    ("school", r"\b(school|university|college|institution)\b"),
    ("degree", r"^degree\b|\bdegree (type|level|program)\b|highest (level of )?education"),
    ("major", r"\b(major|discipline|field of study|area of study)\b"),
    ("earliest_start", r"\b(earliest )?start date\b|when can you start|available to start"),
    ("location", r"^(current )?(location|city)\b|where are you (currently )?(located|based)"),
)
BOOLEAN_CONCEPTS = {"sponsorship_needed", "work_authorized", "us_citizen", "willing_to_relocate"}
# Questions whose phrasing inverts yes/no, which we refuse to guess about.
INVERTED = re.compile(r"\b(do not|don't|not require|without (needing|requiring))\b")


def classify(f: FormField) -> str:
    label = norm(f.label)
    if f.kind == "file":
        if re.search(CONCEPTS[1][1], label):
            return "cover_letter"
        return "resume" if re.search(CONCEPTS[0][1], label) or not label else "unknown_file"
    for concept, pattern in CONCEPTS:
        if concept in ("resume", "cover_letter"):
            if re.search(pattern, label) and f.kind in ("textarea",) and concept == "cover_letter":
                return "cover_letter"
            continue
        if re.search(pattern, label):
            if concept == "attestation" and f.kind not in ("checkbox", "checkbox_group", "radio", "select", "text"):
                continue
            return concept
    return "unknown"


def _yes_no(option):
    o = norm(option)
    if re.match(r"^(yes|y|true)\b", o):
        return True
    if re.match(r"^(no|n|false)\b", o):
        return False
    return None


def _choose(options, value):
    """Exact or unique containment match between a verified value and options."""
    target = norm(str(value))
    if not target:
        return None
    exact = [o for o in options if norm(o) == target]
    if len(exact) == 1:
        return exact[0]
    contains = [o for o in options if target in norm(o) or (norm(o) and norm(o) in target and len(norm(o)) > 3)]
    return contains[0] if len(contains) == 1 else None


def attestation_hash(label):
    return digest(norm(label))[:16]


def resolve(fields, profile: ApplicantProfile, answers, *, resume_path=None, cover_letter=None, approved_attestations=(), model_mappings=None):
    """Return a Resolution per field. Model mappings name *verified* sources only."""
    bank = {norm(a["question"]): a for a in answers if a.get("verified_at")}
    bank_patterns = [(re.compile(a["question"][3:], re.I), a) for a in answers if a.get("verified_at") and a["question"].startswith("re:")]
    by_id = {a["id"]: a for a in answers if a.get("verified_at")}
    approved = {a["hash"] if isinstance(a, dict) else a for a in approved_attestations}
    mappings = {m["question_id"]: m for m in (model_mappings or [])}
    out = []
    for f in fields:
        concept = classify(f)
        res = Resolution(f.fid, f.label, f.kind, f.required, concept, "unresolved")
        label = norm(f.label)
        if concept == "resume":
            res.source, res.value = ("resume", resume_path) if resume_path else ("unresolved", None)
            res.reason = "" if resume_path else "No tailored resume file."
        elif concept == "cover_letter":
            letter = cover_letter if isinstance(cover_letter, dict) or not cover_letter else {"text": cover_letter, "path": None}
            value = (letter or {}).get("path" if f.kind == "file" else "text")
            if value:
                res.source, res.value = "cover_letter", value
            elif not f.required:
                res.source, res.reason = "blank", "Optional cover letter left blank."
            else:
                res.reason = "Required cover letter needs a human-approved letter."
        elif concept == "unknown_file":
            res.source, res.reason = ("blank", "Optional attachment left blank.") if not f.required else ("unresolved", "Unrecognised required attachment.")
        elif concept == "eeo":
            res.source, res.reason = ("blank", "Voluntary self-identification left blank.") if not f.required else ("unresolved", "Required demographic question; answer it yourself.")
        elif concept == "attestation":
            h = attestation_hash(f.label)
            if h in approved and f.kind == "checkbox":
                res.source, res.value, res.reason = "attestation", True, "Attestation text previously approved for this employer."
            elif not f.required:
                res.source, res.reason = "blank", "Optional consent left unchecked."
            else:
                res.reason = "Legal attestation requires your review (hash " + h + ")."
        else:
            value = None
            if concept in BOOLEAN_CONCEPTS:
                value, why = boolean_answer(concept, label, profile)
                if value is None:
                    res.reason = why
            elif concept != "unknown":
                value = field_value(profile, concept)
            if value is not None:
                res.source = "profile"
            else:
                answer = bank.get(label) or next((a for p, a in bank_patterns if p.search(f.label)), None)
                mapping = mappings.get(f.fid)
                if answer:
                    value, res.source = answer["answer"], "answer_bank"
                elif mapping:
                    value = field_value(profile, mapping["key"]) if mapping["source"] == "profile" else by_id.get(mapping["key"], {}).get("answer")
                    res.source = "model_mapping" if value is not None else "unresolved"
                    if mapping["source"] == "profile" and mapping["key"] in BOOLEAN_CONCEPTS:
                        concept = mapping["key"]
            if value is not None:
                res.value = _coerce(f, value, concept)
                if res.value is None:
                    res.source, res.reason = "unresolved", "Verified value does not match any allowed option."
            elif not res.reason:
                res.reason = "No verified answer." if f.required else ""
            if res.value is None and not f.required:
                res.source = "blank"
        out.append(res)
    return out


def boolean_answer(concept, label, profile):
    """Yes/No from verified facts, only when the question's polarity is clear.

    Returns (bool, "") or (None, reason). Compound or inverted wording is
    answered only when the truth is unambiguous under every reading.
    """
    authorized, needs = profile.work_authorized, profile.sponsorship_needed
    if concept == "sponsorship_needed":
        if re.search(r"without (?:the )?(?:need(?:ing)? (?:for )?|requiring )?(?:(?:employer|visa|company) )*sponsor", label):
            if authorized is None or needs is None:
                return None, "Authorization/sponsorship not verified."
            return bool(authorized and not needs), ""
        if INVERTED.search(label):
            return None, "Question uses inverted wording; answer it yourself."
        if re.search(r"\b(require|need|will you|do you)\w*\b.*sponsor|sponsor\w*.*\b(require|need)", label):
            return (needs, "") if needs is not None else (None, "Sponsorship need not verified.")
        return None, "Sponsorship question wording is ambiguous; answer it yourself."
    if INVERTED.search(label):
        return None, "Question uses inverted wording; answer it yourself."
    if concept == "us_citizen":
        if re.search(r"\bor\b|permanent|green card|asylee|refugee|resident", label):
            return (True, "") if profile.us_citizen is True else (None, "Compound citizenship/residency question; answer it yourself.")
        return (profile.us_citizen, "") if profile.us_citizen is not None else (None, "Citizenship not verified.")
    if concept == "work_authorized":
        if re.search(r"\bor\b.*\b(visa|opt|cpt|student)\b|\b(opt|cpt)\b", label):
            return None, "Question mentions specific visa programs; answer it yourself."
        return (authorized, "") if authorized is not None else (None, "Work authorization not verified.")
    value = field_value(profile, concept)
    return (value, "") if value is not None else (None, "Not verified in profile.")


def _coerce(f, value, concept):
    if f.kind in ("select", "radio", "combobox"):
        if isinstance(value, bool):
            matches = [o for o in f.options if _yes_no(o) is value]
            return matches[0] if len(matches) == 1 else None
        return _choose(f.options, value)
    if f.kind == "checkbox":
        return value if isinstance(value, bool) else None
    if f.kind == "checkbox_group":
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def missing_required(resolutions):
    return [r for r in resolutions if r.required and not r.filled]


def answers_hash(resolutions):
    """Binds an approval to exactly which questions were answered from where."""
    return digest([(r.fid, norm(r.label), r.kind, r.required, r.source, str(r.value) if r.kind != "file" else "file") for r in resolutions])
