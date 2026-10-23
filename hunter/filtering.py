from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from .models import Decision, Filters, Flag, Job, Profile, timestamp

INTERNSHIP = re.compile(r"\bintern(?:ship)?s?\b|\bco[ -]?op\b|cooperative education|\bstudent (?:engineer|researcher|technician|trainee)|\bengineering student\b|\bstudent worker\b.*\bengineer", re.I)
DISCIPLINES = (
    ("Embedded / Firmware", r"embedded|firmware|microcontroller|\bmcu\b|\brtos\b|device driver|bootloader"),
    ("FPGA / Semiconductor", r"\bfpga\b|digital design|\basic\b|\bsoc\b|semiconductor|silicon|\brtl\b|verilog|vhdl|\bvlsi\b|chip design|mixed[- ]signal|analog design|physical design|design verification"),
    ("PCB / Hardware", r"\bpcb\b|hardware|electronics|circuit|board design|avionics"),
    ("Electrical", r"electrical|\bee\b|\becE\b|electronic engineering"),
    ("Robotics / Controls", r"robotics|robotic|control systems|controls?\b(?: engineer| intern| systems)|systems integration|mechatronic|automation|\bplc\b|motion control|guidance,? navigation|\bgnc\b|autonomy|autonomous systems"),
    ("Power / RF", r"power electronics|power systems|power engineer|\brf\b|radio frequency|antenna|microwave|battery|energy systems|substation|grid|motor drive"),
    ("Validation / Test", r"validation|verification|test engineer|hardware test|test (?:&|and) (?:measurement|evaluation)|reliability|quality engineer"),
)
# Evidence in a description for a title that only says "Engineering", "Systems" etc.
DESCRIPTION_TERMS = {
    "Embedded / Firmware": r"embedded|firmware|microcontroller|\bmcu\b|\brtos\b|stm32|esp32|arm cortex|\bi2c\b|\bspi\b|\buart\b|\bcan bus\b|device drivers?|bootloader|embedded c\b",
    "FPGA / Semiconductor": r"\bfpga\b|verilog|vhdl|systemverilog|\brtl\b|\basic\b|digital design|digital logic|timing closure|vivado|quartus|synthesis|semiconductor|silicon|\bsoc\b",
    "PCB / Hardware": r"\bpcb\b|schematic|altium|kicad|orcad|cadence allegro|board bring[- ]up|circuit design|hardware design|layout|oscilloscope|soldering|electronic components",
    "Electrical": r"electrical engineering|electrical systems|electrical design|circuits?|analog|power supplies|wiring|harness|signal integrity|avionics|multimeter|electronics",
    "Robotics / Controls": r"robotics|\bros2?\b|control systems|controls engineering|\bplc\b|motion control|mechatronics|kinematics|sensor fusion|simulink|feedback control|automation|actuators?|motors?\b",
    "Power / RF": r"power electronics|inverters?|converters?|battery|motor drives?|\brf\b|antennas?|microwave|transceivers?|power systems|high voltage|grid",
    "Validation / Test": r"hardware test|test fixtures?|labview|test automation|validation testing|environmental testing|test plans?|failure analysis",
}
# A title is still worth reading when it says engineering/technical but names no field.
GENERIC_ENGINEERING = re.compile(r"engineer|engineering|systems|technical|technology|r&d|research|development|design|test|science|stem|technician|lab\b", re.I)
# Interns in these fields are not engineering roles; rejected at retrieval with an explanation.
NON_TECHNICAL_TITLE = re.compile(r"\b(?:marketing|sales|finance|financial|accounting|account(?:ing)? (?:executive|manager)|business (?:development|operations|analyst)|human resources|\bhr\b|recruit\w*|talent|people (?:operations|team)|legal|paralegal|law clerk|communications|public relations|\bpr\b|social media|content|copywrit\w*|graphic design|ux|ui designer|product design(?:er)?|brand|events?|customer success|customer support|community|policy|government affairs|investor|treasury|tax|audit|procurement|purchasing|supply chain|logistics|real estate|facilities|administrative|executive assistant|office)\b", re.I)
# Fields outside electrical engineering that are named explicitly in a title.
OTHER_DISCIPLINE_TITLE = re.compile(r"\b(?:mechanical|civil|chemical|structural|aerospace structures|materials|biomedical|industrial engineer\w*|manufacturing engineer\w*|software|data scien\w*|data analy\w*|machine learning|\bml\b|\bai\b|security|cyber\w*|cloud|devops|site reliability|front[- ]?end|back[- ]?end|full[- ]?stack|web|mobile|ios|android|quant\w*|actuar\w*|thermal|propulsion|structures|aerodynamic\w*|aero design|program manag\w*|project manag\w*|product manag\w*|skillbridge)\b", re.I)
US_STATES = "Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida Georgia Hawaii Idaho Illinois Indiana Iowa Kansas Kentucky Louisiana Maine Maryland Massachusetts Michigan Minnesota Mississippi Missouri Montana Nebraska Nevada Ohio Oklahoma Oregon Pennsylvania Tennessee Texas Utah Vermont Virginia Washington Wisconsin Wyoming".split()
US_ABBREVIATIONS = "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()


def title_screen(title: str) -> str:
    """Retrieval-stage verdict on a title alone: "keep", "not_internship" or "non_technical".

    Deliberately broad: any internship/co-op title that is not clearly a
    non-technical role is kept and judged on its full description later, so
    "Engineering Intern" or "Summer 2027 Engineering Co-op" is never dropped
    for lacking a field name.
    """
    if not INTERNSHIP.search(title):
        return "not_internship"
    if NON_TECHNICAL_TITLE.search(title) and not any(re.search(p, title, re.I) for _, p in DISCIPLINES):
        return "non_technical"
    return "keep"


def potential_title(title: str) -> bool:
    return title_screen(title) == "keep"


def _description_evidence(description: str) -> dict[str, list[str]]:
    found = {}
    for label, pattern in DESCRIPTION_TERMS.items():
        hits = sorted({m.group(0).casefold() for m in re.finditer(pattern, description, re.I)})
        if hits:
            found[label] = hits
    return found


def classify_discipline(title: str, description: str = "") -> tuple[str, str]:
    """(discipline, basis). Title first; generic engineering titles are read from the description.

    Returns ("Other", why) only with an explanation the UI can show.
    """
    for label, pattern in DISCIPLINES:
        if re.search(pattern, title, re.I):
            return label, "title"
    for pattern in (OTHER_DISCIPLINE_TITLE, NON_TECHNICAL_TITLE):
        if pattern.search(title):
            return "Other", f"Title names another field ({pattern.search(title)[0]})."
    if not description.strip():
        return "Other", "No electrical field in the title and no description to read yet."
    evidence = _description_evidence(description)
    # Two distinct terms in one field, or an explicit EE/ECE major requirement, is enough.
    major = re.search(r"electrical(?: and computer)? engineering|computer engineering|\bECE\b|\bEE\b", description, re.I)
    ranked = sorted(evidence.items(), key=lambda kv: (-len(kv[1]), list(DESCRIPTION_TERMS).index(kv[0])))
    if ranked and (len(ranked[0][1]) >= 2 or (major and GENERIC_ENGINEERING.search(title))):
        label, hits = ranked[0]
        return label, "description: " + ", ".join(hits[:5])
    if major and GENERIC_ENGINEERING.search(title):
        return "Electrical", "description asks for " + major[0]
    return "Other", "Neither the title nor the description shows electrical, embedded, hardware, FPGA, robotics/controls or power/RF work."


def discipline(title: str, description="") -> str:
    return classify_discipline(title, description)[0]


def internship_terms(title: str, description: str) -> tuple[str, ...]:
    def find(text):
        values = set()
        for season, year in re.findall(r"\b(spring|summer|fall|autumn|winter)\s*(?:co[ -]?op|intern(?:ship)?|term|semester|[(/-])?\s*(20\d{2})\b", text, re.I):
            values.add(("Fall" if season.lower() == "autumn" else season.title()) + " " + year)
        for year, season in re.findall(r"\b(20\d{2})\s*[-/:(]?\s*(spring|summer|fall|winter)\b", text, re.I):
            values.add(season.title() + " " + year)
        return values
    title_terms = find(title)
    if title_terms:
        return tuple(sorted(title_terms))
    values = find(description)
    for start, end, year in re.findall(r"\b(January|February|March|May|June)\s*[-–to]+\s*(April|May|July|August)\s+(20\d{2})", description, re.I):
        values.add(("Spring" if start.lower() in {"january", "february", "march"} else "Summer") + " " + year)
    return tuple(sorted(values))


def us_location(job: Job) -> str:
    countries = {x.strip().upper() for x in job.country.split(";") if x.strip()}
    if countries & {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
        return "met"
    if countries:
        return "unmet"
    loc = job.location
    if re.search(r"\b(United States|USA|U\.S\.|US Remote|Remote.*US)\b", loc, re.I):
        return "met"
    if any(re.search(r"\b" + re.escape(s) + r"\b", loc) for s in US_STATES) or re.search(r",\s*(?:" + "|".join(US_ABBREVIATIONS) + r")\b", loc):
        return "met"
    if re.search(r"Canada|United Kingdom|London|Germany|France|India|Japan|Australia|Singapore|Mexico|Netherlands|Sweden|Taiwan|China|Korea", loc, re.I):
        return "unmet"
    return "unknown"


def eligibility(job: Job, profile: Profile) -> tuple[Flag, ...]:
    flags = []
    description = job.description
    # Do not split initialisms such as U.S. away from citizenship conditions.
    chunks = re.split(r"\n|(?<=[a-z0-9][.!?])\s+", description)
    for line in chunks:
        low = line.casefold()
        if not line.strip() or re.search(r"equal opportunity|regardless of|does not discriminate|without regard|benefits package|candidate ai guidelines|relocation assistance|insurance|perk", low):
            continue
        if re.search(r"preferred|nice to have|is a plus", low):
            continue
        conditions = []
        if re.search(r"(?:minimum|at least|>=|of|:)\s*(\d\.\d+)\s*(?:gpa)?|gpa\s*(?:of|:|>=|at least|minimum)?\s*(\d\.\d+)", low) and "gpa" in low:
            threshold = re.search(r"\b[0-4]\.\d+\b", low)
            if threshold:
                value = float(threshold[0])
                conditions.append(("met" if profile.gpa >= value else "unmet", f"Declared GPA {profile.gpa:g}; threshold {value:g}."))
        if re.search(r"(?:must|required|only|requirement|requires).{0,45}(?:u\.?s\.?|united states).{0,15}citizen|(?:u\.?s\.?|united states).{0,10}citizenship.{0,25}(?:required|requirement)|u\.?s\.? citizens only", low):
            conditions.append(("met" if profile.us_citizen is True else "unmet" if profile.us_citizen is False else "unknown", "U.S. citizenship explicitly declared in candidate profile."))
        if re.search(r"authori[sz]ed to work|work authori[sz]ation|eligible to work", low):
            conditions.append(("met" if profile.us_authorized is True else "unmet" if profile.us_authorized is False else "unknown", "U.S. work authorization explicitly declared in candidate profile."))
        if re.search(r"(?:no|not|unable|cannot|without).{0,35}sponsor|sponsor.{0,30}(?:not|unavailable)", low):
            conditions.append(("met" if profile.sponsorship_needed is False else "unmet" if profile.sponsorship_needed is True else "unknown", "Candidate declared no visa sponsorship needed."))
        hard_advanced = re.search(r"(?:must|required|pursuing|enrolled|enrollment).{0,65}(?:master|ph\.?d|doctoral)|(?:master|ph\.?d|doctoral).{0,40}(?:required|only)", low)
        if hard_advanced and not re.search(r"bachelor|undergraduate|\bbs\b|b\.s", low):
            program = profile.degree.casefold()
            matching_level = bool(re.search(r"ph\.?d|doctoral", program)) or bool(re.search(r"master|m\.s", program) and re.search(r"master", low))
            conditions.append(("met" if matching_level else "unmet", "Compare the required graduate program with the declared degree: " + profile.degree))
        if re.search(r"(?:completed|earned|hold|obtained).{0,25}bachelor|bachelor.{0,30}(?:completed|completion required)", low) and not re.search(r"\b(?:or|pursuing|currently)\b", low):
            conditions.append(("unmet", "A completed bachelor's degree is required; declared graduation is May 2028."))
        if re.search(r"(?:graduate|graduating|graduation).{0,25}between", low):
            dates = re.findall(r"(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+)?(20\d{2})", line, re.I)
            if len(dates) == 2 and all(month for month, _ in dates):
                months = "january february march april may june july august september october november december".split()
                lo, hi = [f"{year}-{months.index(month.casefold())+1:02}" for month, year in dates]
                conditions.append(("met" if lo <= profile.graduation <= hi else "unmet", f"Declared graduation {profile.graduation}; required window {lo}–{hi}."))
            else:
                conditions.append(("unknown", "Confirm the precise required graduation window."))
        if re.search(r"clearance|sophomore|junior|senior standing|academic standing|return.{0,25}school|enrolled|enrollment|full.time student|(?:available|availability).{0,35}(?:week|month|start|intern|co.op|spring|summer|full.time)|co.op.{0,35}(?:semester|rotation)|consecutive.{0,10}(?:months|terms)", low):
            conditions.append(("unknown", "Confirm academic standing, enrollment, co-op availability, or clearance; these are not inferred from citizenship or graduation."))
        for status, explanation in conditions:
            flags.append(Flag(line.strip(), status, explanation))
    # Keep Phase 1 extraction as a second safety net for unusual conditions.
    from ats.matching import extract_requirements
    try:
        reqs = extract_requirements(description)
        seen = {f.requirement for f in flags}
        for req in reqs:
            if req.kind == "eligibility" and req.priority == "required" and req.text not in seen and not re.search(r"benefit|package|guideline|insurance|relocation assistance", req.text, re.I):
                flags.append(Flag(req.text, "unknown", "Confirm this condition; no automatic eligibility assertion was made."))
    except ValueError:
        pass
    return tuple(dict.fromkeys(flags))


def decide(job: Job, profile: Profile, filters: Filters, now: str) -> Decision:
    current = datetime.fromisoformat(timestamp(now))
    reasons = []
    if job.source_status == "closed" or (job.expires_at and datetime.fromisoformat(job.expires_at) < current):
        reasons.append("Closed or expired posting")
    if not INTERNSHIP.search(job.title) and not re.search(r"\bintern(?:ship)?\b|\bco[ -]?op\b", job.employment_type, re.I):
        reasons.append("Not an internship or co-op")
    field, basis = classify_discipline(job.title, job.description)
    if field == "Other":
        reasons.append("Unrelated discipline: " + basis)
    terms = internship_terms(job.title, job.description)
    if terms and not set(terms) & set(filters.terms):
        reasons.append("Outside requested internship term")
    # A title explicitly tied to another year is not rescued by graduation
    # dates in the qualifications section.
    title_years = set(re.findall(r"\b20\d{2}\b", job.title))
    if title_years and not title_years & {t[-4:] for t in filters.terms}:
        reasons.append("Title specifies a different year")
    location = us_location(job)
    flags = list(eligibility(job, profile))
    if job.imported_at:
        flags.append(Flag("Imported listing verification", "unknown", job.availability_basis + "; publication dates are user-reported, not independently verified."))
    if location == "unmet":
        reasons.append("Outside the United States")
    elif location == "unknown":
        flags.append(Flag("U.S. location", "unknown", "Confirm the work location or U.S. remote eligibility."))
    if not terms:
        flags.append(Flag("Internship term", "unknown", "Confirm Spring/Summer 2027 availability; no term was inferred from discovery date."))
    if any(f.status == "unmet" for f in flags):
        reasons.append("Clearly unmet hard requirement")
    if not job.description.strip():
        flags.append(Flag("Full job description", "unknown", "Description retrieval incomplete; do not rank or tailor until available."))
    if job.published_at:
        published = datetime.fromisoformat(job.published_at)
        recency = "future" if published > current + timedelta(minutes=5) else "recent" if published >= current-timedelta(days=filters.days) else "older"
    else:
        recency = "unknown"
    return Decision(not reasons, field, terms, recency, tuple(dict.fromkeys(reasons)), tuple(flags))
