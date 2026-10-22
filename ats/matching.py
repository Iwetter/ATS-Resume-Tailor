"""Finite vocabulary, explicit aliases and binary text similarity."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer

from .models import Evidence, Fact, Match, Requirement, Resume, digest

# Canonical entries are distinct technologies. In particular VHDL != Verilog,
# SystemVerilog != Verilog, ESP32 != STM32, and C != C++.
ALIASES = {
    "I2C": ("i2c", "i²c", "iic", "inter integrated circuit"),
    "PCB": ("pcb", "pcbs", "printed circuit board", "printed circuit boards", "circuit boards"),
    "KiCad": ("kicad",), "Altium": ("altium", "altium designer"),
    "ESP32": ("esp32", "esp-32"), "STM32": ("stm32",),
    "C++": ("c++", "cpp"), "C": ("c",), "Python": ("python",),
    "VHDL": ("vhdl",), "Verilog": ("verilog",), "SystemVerilog": ("systemverilog",),
    "FPGA": ("fpga", "field programmable gate array", "field-programmable gate array"),
    "ASIC": ("asic", "application specific integrated circuit"),
    "RTL": ("rtl", "register transfer level"),
    "SoC": ("soc", "system on chip", "system-on-chip"),
    "DC-DC": ("dc-dc", "dc–dc", "dc to dc"),
    "PWM": ("pwm", "pulse width modulation", "pulse-width modulation"),
    "SPI": ("spi", "serial peripheral interface"), "UART": ("uart",),
    "CAN": ("can bus", "canbus"), "JTAG": ("jtag",), "USB": ("usb",),
    "RTOS": ("rtos", "real-time operating system", "real time operating system"),
    "Embedded systems": ("embedded systems", "embedded system"),
    "Firmware": ("firmware",), "Robotics": ("robotics", "robotic"),
    "EEG": ("eeg", "electroencephalography"), "MATLAB": ("matlab",),
    "Simulink": ("simulink",), "LabVIEW": ("labview",), "LTspice": ("ltspice",),
    "Oscilloscope": ("oscilloscope", "oscilloscopes"),
    "Multimeter": ("multimeter", "multimeters", "dmm"),
    "Signal integrity": ("signal integrity",), "Power electronics": ("power electronics",),
    "Digital logic": ("digital logic",), "Analog circuits": ("analog circuits", "analog circuit"),
    "Verification": ("verification", "verified", "verify"),
    "Validation": ("validation", "validated", "validate"),
    "Simulation": ("simulation", "simulations", "simulated", "simulate"),
    "Testing": ("testing", "tested", "test"),
    "Debugging": ("debugging", "debugged", "debug", "troubleshoot", "troubleshooting", "troubleshot"),
    "Root cause analysis": ("root cause analysis", "root-cause analysis"),
    "Data analysis": ("data analysis",), "Timing analysis": ("timing analysis", "timing closure"),
    "Synthesis": ("synthesis",), "Place and route": ("place-and-route", "place and route"),
    "Git": ("git",), "Linux": ("linux",), "Assembly": ("assembly",),
    "Java": ("java",), "JavaScript": ("javascript",), "SQL": ("sql",),
    "CAD": ("cad", "computer aided design"), "SolidWorks": ("solidworks",),
    "Automation": ("automation", "automated"), "Microsoft Office": ("microsoft office",),
}

HEADINGS = {
    "education": "Education", "academic background": "Education",
    "experience": "Experience", "work experience": "Experience", "professional experience": "Experience",
    "engineering experience": "Experience", "research experience": "Experience",
    "projects": "Projects", "technical projects": "Projects", "engineering projects": "Projects",
    "selected projects": "Projects", "project experience": "Projects",
    "skills": "Skills", "technical skills": "Skills", "technical proficiencies": "Skills",
    "summary": "Summary", "profile": "Summary", "objective": "Summary",
    "leadership": "Leadership", "leadership experience": "Leadership", "activities": "Activities",
    "awards": "Awards", "honors": "Awards", "certifications": "Certifications",
    "relevant coursework": "Coursework", "coursework": "Coursework",
}
BULLET = re.compile(r"^\s*[•●▪‣◦\u00b7\-–*]\s*")
ACTION = re.compile(r"^(?:designed|built|developed|implemented|verified|validated|tested|debugged|troubleshot|constructed|analyzed|integrated|measured|created|programmed|wrote|assembled|simulated|optimized|automated|led|collaborated|supported|performed|fabricated|characterized|evaluated|documented|improved|reduced|completed|maintained)\b", re.I)
METRIC = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)*(?:\s?(?:%|[munpkMGTµμ]?(?:V|A|W|Hz|s|F|H|Ω)|ohms?|mm|cm|GHz|MHz|kHz|ms|us|ns|dB|layers?|bits?|hours?|weeks?|months?|years?))?(?![A-Za-z])")


def clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("–", "-").replace("—", "-").replace("−", "-").casefold()


def _pattern(alias: str) -> str:
    return r"(?<![\w+])" + re.escape(clean(alias)).replace(r"\ ", r"\s+") + r"(?![\w+])"


def has_term(text: str, term: str) -> bool:
    return any(re.search(_pattern(a), clean(text)) for a in ALIASES.get(term, (term,)))


def skills_in(text: str) -> set[str]:
    return {name for name in ALIASES if has_term(text, name)}


def normalized(text: str) -> str:
    value = clean(text)
    substitutions = sorted(((a, name) for name, aliases in ALIASES.items() for a in aliases), key=lambda x: -len(x[0]))
    # Canonical placeholders avoid cascading aliases (C inside C++, etc.).
    for alias, name in substitutions:
        placeholder = "skill" + str(list(ALIASES).index(name)) + "token"
        value = re.sub(_pattern(alias), placeholder, value)
    return value


def tokens(text: str) -> set[str]:
    return {x for x in re.findall(r"[\w]+", normalized(text)) if (len(x) > 2 or x.isdigit()) and x not in ENGLISH_STOP_WORDS and x not in {"experience", "required", "preferred", "skills", "ability", "strong", "excellent"}}


def similarity(a: str, b: str) -> float:
    if not tokens(a) or not tokens(b):
        return 0.0
    vec = TfidfVectorizer(binary=True, tokenizer=lambda t: sorted(tokens(t)), token_pattern=None, lowercase=False)
    matrix = vec.fit_transform([a, b])
    return float((matrix[0] @ matrix[1].T).toarray()[0, 0])


def parse_resume(text: str, source_hash: str | None = None) -> Resume:
    """Keep all lines, including unknown sections. Never fill facts from defaults."""
    lines = [re.sub(r"[ \t]+", " ", x).strip() for x in text.splitlines() if x.strip()]
    logical: list[str] = []
    for line in lines:
        # Join conservative PDF continuation lines; ledger review allows users
        # to correct grouping, while original upload remains untouched.
        if logical and BULLET.match(logical[-1]) and not BULLET.match(line) and clean(line).rstrip(":") not in HEADINGS and re.match(r"^[a-z]", line) and not ACTION.match(line):
            logical[-1] += " " + line
        else:
            logical.append(line)
    items = []
    section, group, was_bullet = "Header", "", False
    for i, line in enumerate(logical):
        heading = HEADINGS.get(clean(line).strip(" :"))
        eid = "E" + digest([i, line])[:9]
        if heading:
            section, group, was_bullet = heading, "", False
            item = Evidence(eid, line, "section", section)
        else:
            is_bullet = bool(BULLET.match(line)) or (section in {"Experience", "Projects", "Leadership", "Activities"} and bool(ACTION.match(line)))
            if is_bullet:
                item = Evidence(eid, BULLET.sub("", line), "bullet", section, group)
                was_bullet = True
            elif section == "Header":
                item = Evidence(eid, line, "header", section)
            elif section == "Skills":
                item = Evidence(eid, line, "skill", section)
            elif section in {"Projects", "Experience", "Leadership", "Activities"}:
                if not group or was_bullet:
                    group = "P" + digest([section, i, line])[:8]
                item = Evidence(eid, line, "entry", section, group)
                was_bullet = False
            else:
                item = Evidence(eid, line, "text", section)
        items.append(item)
    return make_resume(tuple(items), source_hash or digest(text))


def make_resume(items: tuple[Evidence, ...], source_hash: str) -> Resume:
    facts = []
    for item in items:
        values = [("metric", m.group()) for m in METRIC.finditer(item.text)]
        values += [("skill", s) for s in sorted(skills_in(item.text))]
        if item.kind == "entry" and item.group:
            values.append(("project" if item.section == "Projects" else "entry", item.text))
        for n, (kind, value) in enumerate(values):
            facts.append(Fact("F" + digest([item.id, kind, n, value])[:9], item.id, kind, value))
    return Resume(items, tuple(facts), source_hash)


def extract_requirements(text: str) -> tuple[Requirement, ...]:
    if len(text.split()) < 15:
        raise ValueError("Paste at least 15 words of job description before analyzing.")
    if len(text) > 45000:
        raise ValueError("Keep the job description under 45,000 characters.")
    requirements, seen = [], set()
    priority, in_qualifications, in_responsibilities = "context", False, False
    lines = re.split(r"\n+|(?<=[.;])\s+(?=[A-Z])", text)
    for raw in lines:
        line = raw.strip(" \t•*-:;・")
        low = clean(line)
        if len(line) < 3:
            continue
        if low in {"additional information", "compensation & benefits details", "company description"}:
            in_qualifications = in_responsibilities = False
            priority = "context"
            # Company/EEO/compensation text is not used as requirements.
        if low in {"required", "minimum qualifications", "basic qualifications", "requirements", "qualifications"}:
            priority, in_qualifications, in_responsibilities = "required", True, False
            continue
        if low in {"preferred", "preferred qualifications", "nice to have", "bonus"}:
            priority, in_qualifications, in_responsibilities = "preferred", True, False
            continue
        if low in {"what you'll do", "what you’ll do", "responsibilities", "job description"}:
            priority, in_responsibilities = "context", True
            in_qualifications = False
            continue
        if low.startswith(("why you", "technologies you")):
            in_responsibilities = False
            priority = "context"
            continue
        current = "preferred" if re.search(r"\b(preferred|plus|nice.to.have|bonus)\b", low) else ("required" if re.search(r"\b(required|must|minimum)\b", low) else priority)
        hard = bool(re.search(r"\b(degree|bachelor|master'?s|ph\.?d|gpa|citizen|citizenship|authorized|authorisation|authorization|visa|sponsorship|eligible|enrolled|graduat|relocat|available|availability|clearance|pursuing)\w*", low))
        terms = tuple(sorted(skills_in(line)))
        candidates = []
        if hard:
            candidates.append((line, "eligibility", terms, "any" if " or " in low else "all"))
        elif terms:
            alternatives = bool(re.search(r"\b(or|such as|including|one or more|e\.g)\b", low))
            if alternatives:
                candidates.append((line, "skill", terms, "any"))
            else:
                for term in terms:
                    candidates.append((line, "skill", (term,), "all"))
        if not hard and ((in_responsibilities and re.match(r"(?i)^(design|build|support|optimize|collaborate|create|perform|analy[sz]e|improve|write|integrate|troubleshoot|define|coordinate|develop|conduct|partner|install|investigate|help|assist|test|verify|validate)\b", line)) or (in_qualifications and not terms and len(line.split()) > 5)):
            candidates.append((line, "responsibility", terms, "all"))
        for body, kind, req_terms, mode in candidates:
            key = (kind, req_terms if kind == "skill" else clean(body))
            if key in seen:
                # Promote a term mentioned again under Required; never count twice.
                for index, req in enumerate(requirements):
                    old_key = (req.kind, req.terms if req.kind == "skill" else clean(req.text))
                    if old_key == key and {"context": 0, "preferred": 1, "required": 2}[current] > {"context": 0, "preferred": 1, "required": 2}[req.priority]:
                        requirements[index] = replace(req, priority=current, text=body)
                continue
            seen.add(key)
            requirements.append(Requirement("R" + digest([kind, body, req_terms])[:9], body, kind, current, req_terms, mode))
    return tuple(requirements)


def validate_requirements(rows) -> tuple[Requirement, ...]:
    out = []
    for index, row in enumerate(rows):
        text = str(row.get("text", "")).strip()
        if not text:
            continue
        kind = row.get("kind", "skill")
        priority = row.get("priority", "required")
        mode = row.get("mode", "all")
        assertion = row.get("assertion", "unknown")
        if kind not in {"skill", "responsibility", "eligibility"} or priority not in {"required", "preferred", "context"} or mode not in {"all", "any"} or assertion not in {"unknown", "met", "unmet"}:
            raise ValueError("Invalid requirement type, priority, mode, or eligibility status.")
        terms = tuple(x.strip() for x in str(row.get("terms", "")).split(";") if x.strip())
        if kind == "skill" and not terms:
            terms = (text,)
        note = str(row.get("note", "")).strip()
        if assertion != "unknown" and not note:
            raise ValueError("An eligibility assertion needs a verification note.")
        out.append(Requirement("R" + digest([index, text, kind, terms])[:9], text, kind, priority, terms, mode, assertion, note))
    if len(out) > 150:
        raise ValueError("Limit the editable requirement list to 150 rows.")
    return tuple(out)


def negated(text: str) -> bool:
    return bool(re.search(r"\b(no experience|not familiar|never used|without experience|plan(?:ned)? to|planning to|intend to|will (?:design|build|develop)|in progress)\b", clean(text)))


def demonstrated(item: Evidence) -> bool:
    return item.kind == "bullet" and item.section in {"Experience", "Projects", "Leadership", "Activities"} and bool(ACTION.match(item.text)) and not negated(item.text)


def _eligibility(req: Requirement, resume: Resume) -> Match:
    if req.assertion != "unknown":
        return Match(req.id, req.assertion, (), "User-verified: " + req.note)
    low = clean(req.text)
    education = [x for x in resume.items if x.section == "Education" and x.kind != "section"]
    source = " ".join(x.text for x in education)
    externally_verified = re.search(r"citizen|authori|visa|sponsor|clearance|available|availability|relocat", low)
    if externally_verified:
        return Match(req.id, "unknown", (), "Authorization, citizenship, clearance, location and availability require explicit verification.")
    # Only auto-check a plain GPA threshold or an education/enrolment condition.
    # Dates, citizenship, sponsorship, availability and years stay unknown.
    if "gpa" in low and re.search(r"\d\.\d+", low) and not re.search(r"\band\b|degree|bachelor|master|graduat|pursuing|enrolled", low):
        threshold = re.search(r"\d\.\d+", low).group()
        own = re.search(r"(?i)GPA\s*[:=]?\s*(\d\.\d+)", source)
        if own:
            return Match(req.id, "met" if float(own[1]) >= float(threshold) else "unmet", tuple(x.id for x in education if own[0] in x.text), f"GPA {own[1]} compared with stated threshold {threshold}.")
    unsafe = re.search(r"\bgpa\b|\b20\d\d\b|\byears?\b|\bmust.*and\b", low)
    degree = re.search(r"bachelor|b\.s\.?|bsee|bsece|bs\b|master|m\.s\.?|ph\.?d", clean(source))
    if not unsafe and degree and re.search(r"degree|bachelor|master|pursuing|enrolled", low):
        fields = [f for f in ("electrical engineering", "computer engineering", "computer science", "mechanical engineering", "systems engineering") if f in low]
        field_ok = not fields or any(f in clean(source) for f in fields)
        degree_patterns = {"bachelor": r"bachelor|b\.s\.?|bsee|bsece|\bbs\b", "master": r"master|m\.s\.?|\bms\b", "doctorate": r"ph\.?d|doctorate|doctoral"}
        accepted_levels = {key for key, pattern in degree_patterns.items() if re.search(pattern, low)}
        source_levels = {key for key, pattern in degree_patterns.items() if re.search(pattern, clean(source))}
        level_ok = not accepted_levels or bool(accepted_levels & source_levels)
        # Never infer current enrolment from institution alone: an explicit
        # expected graduation/enrolment statement is needed.
        current_needed = "currently" in low or "enrolled" in low or ("pursuing" in low and "recently completed" not in low)
        current_ok = not current_needed or bool(re.search(r"expected|pursuing|currently enrolled", clean(source)))
        if field_ok and level_ok and current_ok:
            return Match(req.id, "met", tuple(x.id for x in education), "Education text supports the listed degree condition; verify current enrolment if necessary.")
    return Match(req.id, "unknown", (), "Not safely inferable from resume text. Verify explicitly; missing evidence does not mean ineligible.")


def match_requirement(req: Requirement, resume: Resume) -> Match:
    if req.kind == "eligibility":
        return _eligibility(req, resume)
    if req.kind == "skill":
        all_hits, demonstrated_hits, satisfied, demonstrated_terms = [], [], [], []
        for term in req.terms or (req.text,):
            hits = [x for x in resume.items if x.kind != "section" and has_term(x.text, term) and not negated(x.text)]
            all_hits += hits
            evidence = [x for x in hits if demonstrated(x)]
            demonstrated_hits += evidence
            satisfied.append(bool(hits))
            demonstrated_terms.append(bool(evidence))
        aggregate = any if req.mode == "any" else all
        status = "supported" if aggregate(demonstrated_terms) else "partial" if any(satisfied) else "missing"
        hits = demonstrated_hits + all_hits
        explanation = {"supported": "Technical term supported by accomplishment evidence.", "partial": "Mentioned without complete demonstrated evidence for this requirement.", "missing": "No positive evidence found; do not insert this skill."}[status]
        return Match(req.id, status, tuple(dict.fromkeys(x.id for x in hits))[:6], explanation)
    ranked = []
    for item in resume.items:
        if not demonstrated(item):
            continue
        overlap = len(tokens(req.text) & tokens(item.text)) / max(1, len(tokens(req.text)))
        score = max(overlap, similarity(req.text, item.text))
        ranked.append((score, item.id))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    top = ranked[0][0] if ranked else 0
    status = "supported" if top >= .60 else "partial" if top >= .25 else "missing"
    return Match(req.id, status, tuple(eid for value, eid in ranked[:3] if value >= .25), f"Binary lexical similarity {top:.2f}; review the cited bullet for actual scope.")


def rank_projects(resume: Resume, requirements: tuple[Requirement, ...]) -> list[tuple[str, float, str]]:
    groups = list(dict.fromkeys(x.group for x in resume.items if x.section == "Projects" and x.group))
    target = " ".join(" ".join(r.terms) if r.terms else r.text for r in requirements if r.kind != "eligibility")
    ranked = []
    for group in groups:
        rows = [x for x in resume.items if x.group == group]
        text = " ".join(x.text for x in rows)
        matched = sum(any(has_term(text, term) for term in r.terms) for r in requirements if r.kind == "skill")
        ranked.append((group, round(matched + similarity(target, text), 4), next((x.text for x in rows if x.kind == "entry"), group)))
    return sorted(ranked, key=lambda x: -x[1])


def rank_bullets(resume: Resume, requirements: tuple[Requirement, ...]) -> list[tuple[str, float]]:
    target = " ".join(" ".join(r.terms) or r.text for r in requirements if r.kind != "eligibility")
    values = []
    for item in resume.items:
        if item.kind != "bullet":
            continue
        hits = sum((2 if r.priority == "required" else 1 if r.priority == "preferred" else .5) for r in requirements if r.kind == "skill" and any(has_term(item.text, term) for term in r.terms))
        values.append((item.id, round(hits + similarity(target, item.text), 4)))
    return sorted(values, key=lambda x: -x[1])


def select_projects(resume: Resume, ordered_groups: list[str]) -> Resume:
    """Explicitly select whole projects. All non-project evidence is retained."""
    valid = {x.group for x in resume.items if x.section == "Projects" and x.group}
    if len(ordered_groups) != len(set(ordered_groups)) or not set(ordered_groups) <= valid:
        raise ValueError("Invalid or duplicate project selection.")
    rows, inserted = [], False
    for item in resume.items:
        if item.section == "Projects" and item.group:
            if not inserted:
                rows.extend(x for g in ordered_groups for x in resume.items if x.section == "Projects" and x.group == g)
                inserted = True
            continue
        if item.kind == "section" and item.section == "Projects" and not ordered_groups:
            continue
        rows.append(item)
    return make_resume(tuple(rows), resume.source_hash)
