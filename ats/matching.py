"""Finite vocabulary, explicit aliases and binary text similarity."""
from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
import re
import unicodedata

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
    # Equivalents found in 207 real EE internship postings (October 2026 scan, tools/grader_audit.py).
    "Schematic capture": ("schematic capture", "schematic design", "schematics", "schematic"),
    "PCB layout": ("pcb layout", "board layout", "pcb routing", "layout and routing"),
    "Board bring-up": ("bring-up", "bring up", "bringup", "board bring-up"),
    "Soldering": ("soldering", "soldered", "solder", "rework"),
    "Microcontrollers": ("microcontroller", "microcontrollers", "mcu", "mcus"),
    "Embedded C": ("embedded c",), "Embedded Linux": ("embedded linux",),
    "Logic analyzer": ("logic analyzer", "logic analyzers"), "Spectrum analyzer": ("spectrum analyzer", "spectrum analyzers"),
    "Network analyzer": ("network analyzer", "vector network analyzer", "vna"),
    "Power supplies": ("power supply", "power supplies"),
    "SPICE": ("spice", "pspice", "hspice", "ngspice"), "OrCAD": ("orcad",), "Cadence": ("cadence", "cadence allegro", "allegro", "virtuoso"),
    "HDL": ("hdl", "hardware description language", "hardware description languages"),
    "ECAD": ("ecad", "pcb design software", "pcb design tools", "eda tools", "eda"),
    "BLE": ("ble", "bluetooth low energy", "bluetooth"), "Ethernet": ("ethernet",), "PCIe": ("pcie", "pci express", "pci-e"),
    "DDR": ("ddr", "ddr4", "ddr5", "lpddr"), "Wi-Fi": ("wi-fi", "wifi"),
    "ARM": ("arm cortex", "cortex-m", "arm-based", "arm processors"),
    "ROS": ("ros", "ros 2", "ros2", "robot operating system"),
    "Control systems": ("control systems", "control system", "controls engineering", "feedback control", "control theory", "pid control", "pid"),
    "PLC": ("plc", "plcs", "programmable logic controller", "programmable logic controllers"),
    "Structured Text": ("structured text", "iec 61131-3"), "Ladder logic": ("ladder logic",), "TwinCAT": ("twincat",),
    "Motor control": ("motor control", "motor controls", "motor drive", "motor drives", "bldc", "fan speed controller", "fan controller", "fan speed control", "servo control", "servo-control", "servo controller", "motor speed control"),
    "Battery management": ("battery management", "bms"), "High voltage": ("high voltage", "high-voltage"),
    "Wire harness": ("harness", "harnesses", "wire harness", "cable harness", "wiring harness"),
    "RF": ("rf", "radio frequency", "microwave"), "Antennas": ("antenna", "antennas"),
    "DSP": ("dsp", "digital signal processing", "signal processing"),
    "Vivado": ("vivado",), "Quartus": ("quartus", "quartus prime"), "ModelSim/Questa": ("modelsim", "questa"), "UVM": ("uvm",),
    "Mixed-signal": ("mixed-signal", "mixed signal"),
    "Test automation": ("test automation", "automated testing", "automated test"),
    "EMI/EMC": ("emi", "emc", "electromagnetic compatibility", "electromagnetic interference"),
    "IMU": ("imu", "imus", "inertial measurement unit"),
    "Lab test equipment": ("lab equipment", "test equipment", "laboratory equipment", "bench equipment", "lab instruments", "test and measurement equipment"),
    "Scripting": ("scripting", "scripting language", "scripting languages"),
    "Serial protocols": ("communication protocols", "serial protocols", "serial communication", "serial interfaces", "communication interfaces"),
    "Version control": ("version control", "source control"),
    "Rust": ("rust",), "Bash": ("bash", "shell scripting"),
    "nRF52": ("nrf52", "nrf52832", "nrf52840"), "Arduino": ("arduino",), "Raspberry Pi": ("raspberry pi",),
}
# A general requirement is met by evidence of a specific instance of it: oscilloscope work
# demonstrates "lab equipment", I2C/SPI work demonstrates "communication protocols".
IMPLIES = {
    "Lab test equipment": ("Oscilloscope", "Multimeter", "Logic analyzer", "Spectrum analyzer", "Network analyzer", "Power supplies"),
    "Microcontrollers": ("ESP32", "STM32", "nRF52", "Arduino", "ARM"),
    "HDL": ("VHDL", "Verilog", "SystemVerilog"),
    "ECAD": ("KiCad", "Altium", "OrCAD", "Cadence"),
    "Scripting": ("Python", "MATLAB", "Bash"),
    "Serial protocols": ("I2C", "SPI", "UART", "CAN", "USB"),
    "Embedded systems": ("Firmware", "Microcontrollers", "ESP32", "STM32", "nRF52", "RTOS", "Embedded C"),
    "SPICE": ("LTspice",),
    "FPGA": ("Quartus", "Vivado"),  # FPGA-specific tools; VHDL/Verilog alone are also used for ASICs
    "Simulation": ("LTspice", "SPICE", "Simulink", "ModelSim/Questa"),
    "Version control": ("Git",),
    "PLC": ("TwinCAT", "Structured Text", "Ladder logic"),
    "Schematic capture": ("KiCad", "Altium", "OrCAD"),
}
# What each match status means. A resume can only show what is on it: "not on your resume"
# is not "you lack this skill".
STATUS_LABELS = {"supported": "Demonstrated", "partial": "Listed, not demonstrated", "missing": "Not on your resume",
                 "met": "Met", "unmet": "Not met", "unknown": "Needs your confirmation"}
STATUS_HELP = {
    "supported": "An accomplishment bullet in your resume shows this.",
    "partial": "Named on your resume (for example in Skills) but no accomplishment bullet shows you using it. If you have, add a bullet that says how.",
    "missing": "Your resume does not mention this. That does not mean you lack it: if you have real experience, add it to your master resume or evidence library. It is never inserted automatically.",
}


# Soft skills a posting asks for, and what resume evidence shows them. Each match must cite
# a real bullet. Traits a resume cannot show ("thrives in ambiguity", "eager to learn") are
# not turned into gradable requirements.
SOFT_SKILLS = {
    "Teamwork & collaboration": (r"\bteam(?:s|work|mates?)?\b|collaborat\w*|cross[- ]functional|work(?:ing)? (?:closely )?with (?:others|engineers|teams)",
                                 r"\bteams?\b|collaborat\w*|coordinat\w*|cross[- ]functional|\d+[- ]member|with (?:engineers|students|staff|researchers|mentors)"),
    "Communication & documentation": (r"communicat\w*|\bwritten\b|\bverbal\b|presentations?\b|documentation|\bdocument(?:ing)?\b|reporting\b",
                                      # "serial communication" / "communication protocols" are electronics, not a soft skill
                                      r"document\w*|\bguides?\b|manuals?\b|present\w*|report\w*|(?<!serial )(?<!wireless )(?<!peripheral )(?<!device )communicat\w*(?!\s+(?:protocols?|interfaces?|bus|links?|problems?|issues?))|\btrained\b|\btaught\b|explained|instruct\w*"),
    "Problem solving & analysis": (r"problem[- ]solv\w*|analytical|troubleshoot\w*|debug\w*|root[- ]cause|critical thinking|solve (?:complex|challenging|real)",
                                   r"debug\w*|troubleshot|troubleshoot\w*|diagnos\w*|analy[sz]\w*|verif\w*|\btested\b|root[- ]cause|resolved|restored|fault isolation|isolat\w*"),
    "Leadership & ownership": (r"\blead\b|leadership|ownership|without supervision|independent(?:ly)?\b|self[- ](?:starter|directed|motivated)|initiative",
                               # "managed multi-chip interfaces" is not leadership: only managing people, teams or projects counts
                               r"\bled\b|\bleading\b|coordinated|officer|managed (?:a |the |\d+[- ]?(?:person|member) )?(?:team|group|project|students|interns|volunteers|members|budget)|\bowned\b|independently (?:designed|built|developed|led|completed|managed)|president|captain|mentor\w*|supervis\w*"),
}
EXPERIENCE_DURATION = re.compile(r"(\d+)\s*\+?\s*(?:or more\s+)?(months?|years?|yrs?)\b[^.\n]{0,60}?\bexperience", re.I)
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
DATE_RANGE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(20\d\d)\s*[-–—to]+\s*(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(20\d\d)|(present|current|now))", re.I)
# Skills too general to make a role count as engineering experience on their own.
GENERIC_SKILLS = {"Linux", "Microsoft Office", "Testing", "Debugging", "Data analysis", "Git", "SQL", "Java", "JavaScript", "Automation",
                  "Assembly", "Validation", "Verification", "Version control", "Scripting", "Python", "C", "CAD"}


def satisfied_by(term: str) -> tuple[str, ...]:
    """The term itself plus the specific skills that demonstrate it."""
    return (term,) + IMPLIES.get(term, ())

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


# Matching runs every alias against every resume line for every requirement, so the pure
# text functions below are cached (the alias table has ~110 entries).
@lru_cache(maxsize=None)
def _compiled(alias: str):
    return re.compile(_pattern(alias))


@lru_cache(maxsize=262144)
def has_term(text: str, term: str) -> bool:
    value = clean(text)
    return any(_compiled(a).search(value) for a in ALIASES.get(term, (term,)))


@lru_cache(maxsize=65536)
def _skills(text: str) -> frozenset:
    return frozenset(name for name in ALIASES if has_term(text, name))


def skills_in(text: str) -> set[str]:
    return set(_skills(text))


@lru_cache(maxsize=1)
def _substitutions():
    names = list(ALIASES)
    pairs = sorted(((a, name) for name, aliases in ALIASES.items() for a in aliases), key=lambda x: -len(x[0]))
    return tuple((_compiled(alias), "skill" + str(names.index(name)) + "token") for alias, name in pairs)


@lru_cache(maxsize=65536)
def normalized(text: str) -> str:
    value = clean(text)
    # Canonical placeholders avoid cascading aliases (C inside C++, etc.).
    for pattern, placeholder in _substitutions():
        value = pattern.sub(placeholder, value)
    return value


@lru_cache(maxsize=65536)
def _tokens(text: str) -> frozenset:
    return frozenset(x for x in re.findall(r"[\w]+", normalized(text)) if (len(x) > 2 or x.isdigit()) and x not in ENGLISH_STOP_WORDS and x not in {"experience", "required", "preferred", "skills", "ability", "strong", "excellent"})


def tokens(text: str) -> set[str]:
    return set(_tokens(text))


@lru_cache(maxsize=262144)
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


# Section headings as real postings write them ("REQUIRED QUALIFICATIONS", "You'll bring these
# qualifications", "Nice-to-have skills"). A heading is a short line; see _heading().
REQUIRED_HEADING = re.compile(r"^(?:(?:basic|minimum|required|key|core|must[- ]have|additional|technical)\s+)*(?:qualifications|requirements|skills|experience|skills and (?:experience|qualifications)|qualifications and (?:skills|experience))\s*$"
                              r"|^what (?:you'll|you will|we) (?:need|bring|require)|^what you bring|^you(?:'ll)? (?:have|bring)|^what we(?:'re| are) looking for|^who you are|^about you|^your (?:background|qualifications|skills)|^you might be a (?:good|great) fit|^you are\b|^required\b"
                              r"|^qualifications (?:you|we) (?:must have|require|need)|(?:good|great|strong) fit|^(?:what )?we hope you'll bring|^what you need|^minimum requirements")
PREFERRED_HEADING = re.compile(r"\b(?:preferred|prefer|bonus|nice[- ]to[- ]haves?|desired|good to have|extra credit|stand out|pluses|is a plus|added advantage)\b")
RESPONSIBILITY_HEADING = re.compile(r"^(?:what you'll do|what you will do|what you'll be doing|responsibilities|key responsibilities|essential (?:duties|functions|responsibilities)|job description|job summary|role summary|about the role|the role|your role|in this role|day[- ]to[- ]day|you will|your impact|the opportunity|duties|how you(?:'ll| will) make an impact)\b")
# Workday's public job record has no line breaks at all; these headings get them back.
STRUCTURE_HEADINGS = re.compile(r"(?<![\w-])(Qualifications You Must Have|Qualifications We Prefer|Basic Qualifications|Minimum Qualifications|Required Qualifications|Preferred Qualifications|"
                                r"Desired Qualifications|Desired Characteristics|Qualifications|Requirements|Responsibilities|Key Responsibilities|Essential Responsibilities|"
                                r"Job Description|Role Summary|Job Summary|What You Will Do|What You Will Learn|What We Offer|Benefits|Additional Information|"
                                r"Nice to Have|Bonus Points|Security Clearance Type|Travel Required)\s*:?(?=\s+[A-Z(])")


def _restore_structure(text: str) -> str:
    """Put line breaks back around section headings in single-paragraph postings (Workday)."""
    if text.count("\n") * 400 >= len(text):  # already has line structure
        return text
    return STRUCTURE_HEADINGS.sub(lambda m: "\n" + m.group(1) + ":\n", text)
CONTEXT_HEADING = re.compile(r"^(?:benefits|perks|compensation|salary|pay|about (?:us|the company|the team)|who we are|why (?:join|work)|our (?:mission|values|culture)|equal (?:employment|opportunity)|eeo|how to apply|application (?:process|instructions)|additional information|company description|technologies you)")


def _heading(raw: str) -> str | None:
    """'required', 'preferred', 'responsibilities', 'context' or None for an ordinary line."""
    line = raw.strip(" \t•*-;・")
    words = line.rstrip(":").split()
    if not words or len(words) > 8 or re.search(r"[.!?]$", line):
        return None
    titled = line.endswith(":") or line.isupper() or words[0][:1].isupper()  # the patterns below decide what it is
    if not titled:
        return None
    if skills_in(line):  # "Experience with PCB design in Altium" is a requirement, not a heading
        return None
    low = clean(line).rstrip(":").replace("’", "'").strip()
    if CONTEXT_HEADING.search(low):
        return "context"
    if PREFERRED_HEADING.search(low):
        return "preferred"
    if RESPONSIBILITY_HEADING.search(low):
        return "responsibilities"
    if REQUIRED_HEADING.search(low):
        return "required"
    return None


def extract_requirements(text: str) -> tuple[Requirement, ...]:
    if len(text.split()) < 15:
        raise ValueError("Paste at least 15 words of job description before analyzing.")
    if len(text) > 45000:
        raise ValueError("Keep the job description under 45,000 characters.")
    text = _restore_structure(text)
    requirements, seen = [], set()
    priority, in_qualifications, in_responsibilities = "context", False, False
    in_benefits = False
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
        heading = _heading(raw)
        in_benefits = heading == "context" or (in_benefits and heading is None)  # any other heading ends a benefits section
        if heading == "required":
            priority, in_qualifications, in_responsibilities = "required", True, False
            continue
        if heading == "preferred":
            priority, in_qualifications, in_responsibilities = "preferred", True, False
            continue
        if heading == "responsibilities":
            priority, in_responsibilities, in_qualifications = "context", True, False
            continue
        if heading == "context":
            priority, in_qualifications, in_responsibilities = "context", False, False
            continue
        current = "preferred" if re.search(r"\b(preferred|plus|nice.to.have|bonus)\b", low) else ("required" if re.search(r"\b(required|must|minimum)\b", low) else priority)
        hard = bool(re.search(r"\b(degree|bachelor|master'?s|ph\.?d|gpa|citizen|citizenship|authorized|authorisation|authorization|visa|sponsorship|eligible|enrolled|graduat|relocat|available|availability|clearance|pursuing)\w*", low)
                    or re.search(r"\bon-?site\b.*\b(?:weeks?|months?)\b|consecutive weeks|full[- ]time.*\b(?:weeks|summer|semester)\b|beginning (?:jan|feb|mar|apr|may|jun)", low))
        if hard and in_benefits and not re.search(r"citizen|nationalit|u\.?s\.? person|itar|export|clearance|security", low):
            hard = False  # "eligible for a relocation stipend" is a perk, not a condition
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
        duration = not hard and bool(EXPERIENCE_DURATION.search(line))
        soft = [] if hard or duration or not (in_qualifications or current != "context") else [name for name, (asked, _) in SOFT_SKILLS.items() if re.search(asked, low)]
        if duration:
            # "3+ months of applied engineering experience": checked against dated roles, reported as mandatory.
            candidates.append((line, "eligibility", (), "all"))
        for name in soft:
            candidates.append((line, "soft_skill", (name,), "all"))
        if not hard and not duration and not soft and ((in_responsibilities and re.match(r"(?i)^(design|build|support|optimize|collaborate|create|perform|analy[sz]e|improve|write|integrate|troubleshoot|define|coordinate|develop|conduct|partner|install|investigate|help|assist|test|verify|validate)\b", line)) or (in_qualifications and not terms and len(line.split()) > 5)):
            candidates.append((line, "responsibility", terms, "all"))
        for body, kind, req_terms, mode in candidates:
            key = (kind, req_terms if kind in ("skill", "soft_skill") else clean(body))
            if key in seen:
                # Promote a term mentioned again under Required; never count twice.
                for index, req in enumerate(requirements):
                    old_key = (req.kind, req.terms if req.kind in ("skill", "soft_skill") else clean(req.text))
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
        if kind not in {"skill", "responsibility", "eligibility", "soft_skill"} or priority not in {"required", "preferred", "context"} or mode not in {"all", "any"} or assertion not in {"unknown", "met", "unmet"}:
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


# Any past-tense lead verb is an accomplishment ("Coordinated", "Routed", "Restored", "Repaired"),
# not only the ~30 verbs in ACTION; plus common irregular forms.
PAST_TENSE_LEAD = re.compile(r"^(?:[A-Z][a-z]+ed|Built|Wrote|Led|Ran|Made|Drew|Taught|Won|Set up|Brought up|Rebuilt|Rewrote|Laid out|Taught|Held|Sold|Spent|Kept)\b")


def demonstrated(item: Evidence) -> bool:
    return item.kind == "bullet" and item.section in {"Experience", "Projects", "Leadership", "Activities"} and bool(ACTION.match(item.text) or PAST_TENSE_LEAD.match(item.text)) and not negated(item.text)


def _eligibility(req: Requirement, resume: Resume) -> Match:
    if req.assertion != "unknown":
        return Match(req.id, req.assertion, (), "User-verified: " + req.note)
    low = clean(req.text)
    education = [x for x in resume.items if x.section == "Education" and x.kind != "section"]
    source = "\n".join(x.text for x in education)
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
    if EXPERIENCE_DURATION.search(req.text) and not re.search(r"degree|bachelor|master|gpa|enrolled|pursuing", low):
        return _experience_duration(req, resume)
    # Compound conditions ("Must be enrolled in a bachelor's ... program in electrical engineering and
    # have at least one semester remaining") are checked clause by clause; every clause must be shown.
    grad = graduation_date(source)
    clauses = [c.strip(" ,") for c in re.split(r"\band\b|;", low) if c.strip(" ,")]
    results = []
    for clause in clauses:
        if re.search(r"semester|quarter|term|year of (?:school|study)|returning to school|return to (?:school|campus)|remaining", clause):
            results.append(_remaining_after_internship(grad))
        elif re.search(r"degree|bachelor|master|ph\.?d|doctora|pursuing|enrolled|program|student|major", clause):
            results.append(_degree_clause(clause, source, grad))
        elif "gpa" in clause:
            results.append(("unknown", "GPA condition combined with other conditions; check it on its own row."))
        elif re.search(r"\b(?:have|has|be|with)\b", clause) and len(clause.split()) <= 3:
            continue  # connective fragment
        else:
            results.append(("unknown", f"'{clause[:80]}' is not something a resume can show; verify it."))
    if results and all(status == "met" for status, _ in results):
        return Match(req.id, "met", tuple(x.id for x in education), " ".join(why for _, why in results))
    unclear = "; ".join(why for status, why in results if status != "met")
    return Match(req.id, "unknown", tuple(x.id for x in education) if any(s == "met" for s, _ in results) else (),
                 ("Partly shown: " + " ".join(why for s, why in results if s == "met") + " Still to verify: " + unclear) if any(s == "met" for s, _ in results)
                 else (unclear or "Not safely inferable from resume text. Verify explicitly; missing evidence does not mean ineligible."))


def _soft_skill(req: Requirement, resume: Resume) -> Match:
    """Teamwork, communication, problem solving, leadership: shown only by cited resume lines."""
    names = [t for t in req.terms if t in SOFT_SKILLS] or [n for n, (asked, _) in SOFT_SKILLS.items() if re.search(asked, clean(req.text))]
    if not names:
        return Match(req.id, "missing", (), "Not a recognised soft skill; edit the requirement type.")
    pattern = "|".join(SOFT_SKILLS[n][1] for n in names)
    pool = [x for x in resume.items if x.section in {"Experience", "Projects", "Leadership", "Activities"} and x.kind in ("bullet", "entry")]
    shown = [x for x in pool if x.kind == "bullet" and demonstrated(x) and re.search(pattern, clean(x.text))]
    named = [x for x in pool if x not in shown and re.search(pattern, clean(x.text))]
    if shown:
        return Match(req.id, "supported", tuple(x.id for x in shown[:3]), f"{', '.join(names)}: shown by {len(shown)} accomplishment line(s), cited.")
    if named:
        return Match(req.id, "partial", tuple(x.id for x in named[:3]), f"{', '.join(names)}: suggested by a role or title but no accomplishment bullet describes it.")
    return Match(req.id, "missing", (), f"{', '.join(names)}: no resume line describes it. If you have done it, say so in a bullet (e.g. who you worked with, what you documented).")


def graduation_date(education_text: str) -> tuple[int, int] | None:
    """(year, month) of the latest future month-year in the Education section, e.g. 'May 2028'."""
    from datetime import date
    today = date.today()
    found = [(int(y), MONTHS.index(m[:3].lower()) + 1) for m, y in re.findall(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(20\d\d)\b", education_text, re.I)]
    future = [d for d in found if d >= (today.year, today.month)]
    return max(future) if future else None


def _degree_clause(clause: str, source: str, grad) -> tuple[str, str]:
    if re.search(r"\b20\d\d\b|\byears?\b|graduat", clause):
        return "unknown", "Graduation-date wording needs your check."
    low_source = clean(source)
    if not re.search(r"bachelor|b\.s\.?|bsee|bsece|\bbs\b|master|m\.s\.?|ph\.?d", low_source):
        return "unknown", "No degree found in the Education section."
    fields = [f for f in ("electrical engineering", "computer engineering", "computer science", "mechanical engineering", "systems engineering") if f in clause]
    related = bool(re.search(r"related (?:discipline|field)|an engineering\b|engineering discipline|stem\b", clause))
    field_ok = not fields or any(f in low_source for f in fields) or (related and "engineering" in low_source)
    degree_patterns = {"bachelor": r"bachelor|b\.s\.?|bsee|bsece|\bbs\b", "master": r"master|m\.s\.?|\bms\b", "doctorate": r"ph\.?d|doctorate|doctoral"}
    accepted = {k for k, p in degree_patterns.items() if re.search(p, clause)}
    held = {k for k, p in degree_patterns.items() if re.search(p, low_source)}
    level_ok = not accepted or bool(accepted & held)
    # Current enrolment: an explicit statement, or a graduation date that is still in the future.
    current_needed = bool(re.search(r"currently|enrolled|pursuing|student", clause))
    current_ok = not current_needed or bool(re.search(r"expected|pursuing|currently enrolled", low_source)) or grad is not None
    degree_line = next((line for line in source.split("\n") if re.search(r"bachelor|master|b\.s\.|m\.s\.|ph\.?d", line, re.I)), "the listed degree")
    if field_ok and level_ok and current_ok:
        when = f", graduating {MONTHS[grad[1] - 1].title()} {grad[0]}" if grad else ""
        return "met", f"Education shows {degree_line.strip()}{when}."
    missing = [name for name, ok in (("field of study", field_ok), ("degree level", level_ok), ("current enrolment", current_ok)) if not ok]
    return "unknown", "Could not confirm " + ", ".join(missing) + " from the Education section."


def _remaining_after_internship(grad) -> tuple[str, str]:
    """'At least one semester remaining after the internship': needs graduation a year or more away."""
    from datetime import date
    if grad is None:
        return "unknown", "No future graduation date in the Education section to confirm remaining semesters."
    today = date.today()
    months_left = (grad[0] - today.year) * 12 + grad[1] - today.month
    if months_left >= 12:
        return "met", f"Graduation {MONTHS[grad[1] - 1].title()} {grad[0]} leaves at least one semester after an internship in the coming year."
    return "unknown", f"Graduation {MONTHS[grad[1] - 1].title()} {grad[0]} is under a year away; confirm a semester remains after the internship."


def experience_months(resume: Resume) -> tuple[int, list[tuple[str, str, int]]]:
    """Months of dated engineering roles and projects (overlaps counted once), with the entries used."""
    from datetime import date
    today = date.today()
    covered, used = set(), []
    for entry in (x for x in resume.items if x.kind == "entry" and x.section in {"Experience", "Projects", "Leadership", "Activities"}):
        group = [x.text for x in resume.items if entry.group and x.group == entry.group] or [entry.text]
        if not (skills_in(" ".join(group)) - GENERIC_SKILLS):
            continue  # not an engineering role (e.g. IT support only)
        dated = next((e for e in [entry.text] + group if DATE_RANGE.search(e)), None)
        if not dated:
            continue
        m = DATE_RANGE.search(dated)
        start = int(m[2]) * 12 + MONTHS.index(m[1][:3].lower())
        end = today.year * 12 + today.month - 1 if m[5] else int(m[4]) * 12 + MONTHS.index(m[3][:3].lower())
        months = set(range(start, max(start, end)))  # end month excluded: conservative
        if months - covered:
            covered |= months
            used.append((entry.id, entry.text, len(months)))
    return len(covered), used


def _experience_duration(req: Requirement, resume: Resume) -> Match:
    m = EXPERIENCE_DURATION.search(req.text)
    need = int(m[1]) * (12 if m[2].lower().startswith(("y", "yr")) else 1)
    have, used = experience_months(resume)
    ids = tuple(eid for eid, _, _ in used)
    listing = "; ".join(f"{text.split('|')[0].strip()[:40]} ({n} mo)" for _, text, n in used) or "no dated engineering roles"
    if have >= need:
        return Match(req.id, "met", ids, f"About {have} months of dated engineering work: {listing}. Needs {need}.")
    return Match(req.id, "unknown", ids, f"About {have} months of dated engineering work found ({listing}); needs {need}. Undated projects also count, so confirm rather than treat this as unmet.")


def match_requirement(req: Requirement, resume: Resume) -> Match:
    if req.kind == "eligibility":
        return _eligibility(req, resume)
    if req.kind == "soft_skill":
        return _soft_skill(req, resume)
    if req.kind == "skill":
        all_hits, demonstrated_hits, satisfied, demonstrated_terms, via = [], [], [], [], []
        for term in req.terms or (req.text,):
            hits = []
            for option in satisfied_by(term):
                # A bullet also carries the technologies named in its own project/role heading
                # ("FPGA Neural Network Accelerator | VHDL, Quartus" -> its bullets show FPGA work).
                headers = {x.group for x in resume.items if x.kind == "entry" and x.group and has_term(x.text, option)}
                found = [x for x in resume.items if x.kind != "section" and not negated(x.text)
                         and (has_term(x.text, option) or (x.kind == "bullet" and x.group in headers))]
                if found and option != term:
                    via.append(option)
                hits += found
            all_hits += hits
            evidence = [x for x in hits if demonstrated(x)]
            demonstrated_hits += evidence
            satisfied.append(bool(hits))
            demonstrated_terms.append(bool(evidence))
        aggregate = any if req.mode == "any" else all
        status = "supported" if aggregate(demonstrated_terms) else "partial" if any(satisfied) else "missing"
        hits = demonstrated_hits + all_hits
        explanation = STATUS_LABELS[status] + ": " + STATUS_HELP[status]
        if via and status != "missing":
            explanation += " Shown through equivalent evidence: " + ", ".join(dict.fromkeys(via)) + "."
        return Match(req.id, status, tuple(dict.fromkeys(x.id for x in hits))[:6], explanation)
    # A duty of the job ("Bring up and debug new hardware"): compare word stems and the named
    # skills it involves with your accomplishment bullets.
    wanted, wanted_skills = _stems(req.text), skills_in(req.text)
    ranked = []
    for item in resume.items:
        if not demonstrated(item):
            continue
        stems = _stems(item.text)
        overlap = len(wanted & stems) / max(1, len(wanted))
        skill_share = len(wanted_skills & {s for s in wanted_skills if any(has_term(item.text, o) for o in satisfied_by(s))}) / len(wanted_skills) if wanted_skills else 0
        score = max(overlap, similarity(req.text, item.text), skill_share * .8)
        ranked.append((score, item.id))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    top = ranked[0][0] if ranked else 0
    status = "supported" if top >= .55 else "partial" if top >= .25 else "missing"
    label = {"supported": "Closely related experience shown", "partial": "Some related experience shown", "missing": "No closely related experience shown"}[status]
    return Match(req.id, status, tuple(eid for value, eid in ranked[:3] if value >= .25),
                 f"{label} (similarity {top:.2f}). This is a duty of the job, not a missing qualification; review the cited bullet for actual scope.")


_SUFFIX = re.compile(r"(?:ing|ed|es|s|ment|ion|ions)$")


def _stems(text: str) -> set[str]:
    """Content-word stems, so debug/debugged/debugging and design/designs/designed meet."""
    return {(_SUFFIX.sub("", t) if len(t) > 5 else t) for t in tokens(text)}


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
