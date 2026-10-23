"""Grading accuracy regressions from a real report: Rocket Lab "Electrical Engineering Intern
Summer 2027" (job-boards.greenhouse.io/rocketlab/jobs/7998598003) graded at 49/100 with
Western Digital recommendations, an "unknown" degree, and teamwork / analysis /
communication "missing". The resume here is fictional but has the same kinds of facts."""
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ats.extraction import Extracted
from ats.matching import demonstrated, experience_months, extract_requirements, match_requirement, parse_resume
from ats.scoring import analyze

FIXTURES = Path(__file__).parent / "fixtures"
ROCKET_LAB = (FIXTURES / "rocket_lab_ee_intern_2027.txt").read_text(encoding="utf-8")
WESTERN_DIGITAL = (FIXTURES / "western_digital_job.txt").read_text(encoding="utf-8")
GRAD = date.today().year + 2  # always a future graduation, as on a current student's resume

RESUME_TEXT = f"""Alex Example
Atlanta, GA | alex@example.com | (404) 555-0100
EDUCATION
Example Institute of Technology, Atlanta, GA May {GRAD}
Bachelor of Science in Electrical Engineering
Cumulative GPA: 3.68 /4.00
EXPERIENCE
Example Lab | Embedded Systems Group Aug {GRAD - 3} – Present
Undergraduate Research Assistant
• Designed and built a 4-layer wearable sensing PCB in KiCad integrating an nRF52832 BLE controller and IMU.
• Routed low-noise signals and managed analog/digital separation on a 32-channel EEG board in Altium.
• Troubleshot regulator, grounding, and 5 V/3.3 V rail issues with a multimeter and oscilloscope.
Robotics Lab | Automation Feb {GRAD - 3} – May {GRAD - 3}
Undergraduate Research Assistant
• Restored two robotic systems by debugging 15+ motor, serial communication, and configuration problems.
• Developed Python motion routines for robotic manipulators over serial communication.
Campus IT | Support Aug {GRAD - 3} – Present
IT Intern
• Created technical setup and troubleshooting guides that helped staff operate equipment independently.
PROJECTS
ESP32 Audio Player | Embedded Hardware & Firmware
• Wrote C++ firmware for an ESP32 with I2S audio, microSD storage, and a TFT display.
• Debugged I2S/SPI interfaces, power issues, and firmware during hardware/software integration tests.
LEADERSHIP
Manufacturing Club Jan {GRAD - 4} – May {GRAD - 4}
Project Officer
• Coordinated CAD and fabrication activities on a 12-member team building an engine from technical drawings.
SKILLS
Programming: C, C++, Python, MATLAB, VHDL
PCB & Electrical: Altium Designer, KiCad, Schematic Capture, Soldering
Tools: LTspice, Quartus Prime, Questa/ModelSim, Linux, Git"""


@pytest.fixture(scope="module")
def resume():
    return parse_resume(RESUME_TEXT)


def grade(resume, job_text):
    return analyze(resume, Extracted(resume.text, "test"), extract_requirements(job_text))


def by_text(resume, job_text, fragment):
    req = next(r for r in extract_requirements(job_text) if fragment.casefold() in (r.text + " ".join(r.terms)).casefold())
    return req, match_requirement(req, resume)


# ---- the exact posting is graded ------------------------------------------------------------

def test_rocket_lab_requirements_come_only_from_rocket_lab(resume):
    reqs = extract_requirements(ROCKET_LAB)
    text = " ".join(r.text + " " + " ".join(r.terms) for r in reqs).casefold()
    for western_digital_only in ("asic", "silicon", "data storage", "analytical, problem-solving", "place and route"):
        assert western_digital_only not in text
    improvements = " ".join(grade(resume, ROCKET_LAB).improvements).casefold()
    assert "asic" not in improvements and "communication" not in improvements


def test_identical_inputs_give_identical_results(resume):
    first, second = grade(resume, ROCKET_LAB), grade(resume, ROCKET_LAB)
    assert first == second
    assert grade(resume, WESTERN_DIGITAL) == grade(resume, WESTERN_DIGITAL)


# ---- eligibility read from the resume ---------------------------------------------------------

def test_degree_and_remaining_semester_are_met_from_education(resume):
    _, match = by_text(resume, ROCKET_LAB, "Must be enrolled")
    assert match.status == "met" and "Bachelor of Science in Electrical Engineering" in match.explanation and match.evidence_ids


@pytest.mark.parametrize("fragment,status", [("GPA of 3.0", "met"), ("GPA of 3.5", "met"), ("3+ months", "met"), ("6+ months", "met")])
def test_stated_qualifications(resume, fragment, status):
    _, match = by_text(resume, ROCKET_LAB, fragment)
    assert match.status == status, match.explanation


def test_experience_months_count_engineering_roles_once():
    months, used = experience_months(parse_resume(RESUME_TEXT))
    names = " ".join(text for _, text, _ in used)
    assert "Example Lab" in names and "Robotics Lab" in names and "Campus IT" not in names  # IT support is not engineering
    assert months >= 6


@pytest.mark.parametrize("fragment", ["on-site", "International Traffic in Arms"])
def test_unverifiable_conditions_stay_unknown_and_unscored(resume, fragment):
    _, match = by_text(resume, ROCKET_LAB, fragment)
    assert match.status == "unknown"
    assert any(i.startswith("Verify (not scored)") and fragment.casefold() in i.casefold() for i in grade(resume, ROCKET_LAB).improvements)


def test_benefit_text_is_not_an_eligibility_condition(resume):
    reqs = extract_requirements(ROCKET_LAB)
    assert not any("stipend" in r.text or "perks" in r.text or "Eligibility for benefits" in r.text for r in reqs)


def test_degree_without_expected_or_future_date_stays_unknown():
    past = parse_resume("Alex Example\nEDUCATION\nExample Institute of Technology May 2019\nBachelor of Science in Electrical Engineering\nEXPERIENCE\nLab | 2018\n• Built a board.")
    _, match = by_text(past, ROCKET_LAB, "Must be enrolled")
    assert match.status == "unknown"  # a 2019 graduate is not shown to be currently enrolled


# ---- soft skills need cited evidence ----------------------------------------------------------

@pytest.mark.parametrize("job,skill,evidence", [
    (ROCKET_LAB, "Teamwork", "12-member team"),
    (ROCKET_LAB, "Leadership", None),
    (WESTERN_DIGITAL, "Problem solving", "Troubleshot"),
    (WESTERN_DIGITAL, "Communication", "troubleshooting guides"),
])
def test_soft_skills_are_shown_by_real_bullets(resume, job, skill, evidence):
    req = next(r for r in extract_requirements(job) if r.kind == "soft_skill" and r.terms[0].startswith(skill))
    match = match_requirement(req, resume)
    assert req.kind == "soft_skill" and match.status == "supported" and match.evidence_ids
    if evidence:
        assert any(evidence in resume.by_id[eid].text for eid in match.evidence_ids)


def test_generic_traits_are_not_graded(resume):
    reqs = extract_requirements(ROCKET_LAB)
    assert not any(r.kind == "responsibility" and "thrive in ambiguity" in r.text for r in reqs)
    assert not any(r.kind == "soft_skill" and "Adapt" in " ".join(r.terms) for r in reqs)


def test_past_tense_bullets_count_as_accomplishments(resume):
    lines = {x.text.split()[0]: x for x in resume.items if x.kind == "bullet"}
    assert all(demonstrated(lines[verb]) for verb in ("Coordinated", "Routed", "Restored", "Troubleshot", "Created"))


# ---- scoring structure and transparency --------------------------------------------------------

def test_unmeasured_components_are_left_out_not_scored_zero(resume):
    result = grade(resume, ROCKET_LAB)
    assert "responsibilities" in result.unmeasured  # Rocket Lab lists no duties to match
    assert result.components["qualifications"] == 100.0  # degree, GPA, experience all shown
    measured = [k for k in result.components if k not in result.unmeasured]
    assert min(result.components[k] for k in measured) > 0


def test_every_point_lost_is_explained(resume):
    result = grade(resume, ROCKET_LAB)
    reqs = {r.id: r for r in extract_requirements(ROCKET_LAB)}
    assert result.deductions and all(rid in reqs for rid, _ in result.deductions)
    worth = [i for i in result.improvements if "Worth up to" in i]
    assert worth and worth == sorted(worth, key=lambda i: -float(i.rsplit("up to ", 1)[1].split()[0]))
    # Requirement deductions plus the bullet-clarity component account for every point below 100.
    from ats.scoring import DEFAULT_WEIGHTS
    measured = sum(w for k, w in DEFAULT_WEIGHTS.items() if k not in result.unmeasured)
    clarity_loss = DEFAULT_WEIGHTS["clarity"] / measured * (100 - result.components["clarity"])
    assert sum(points for _, points in result.deductions) + clarity_loss == pytest.approx(100 - result.alignment, abs=0.6)


def test_rocket_lab_and_western_digital_are_graded_independently(resume):
    rocket, western = grade(resume, ROCKET_LAB), grade(resume, WESTERN_DIGITAL)
    assert rocket.alignment != western.alignment
    assert {"Soft skills", "Mandatory conditions (not scored)"} <= set(rocket.breakdown)


# ---- the app grades the posting on screen, and only it ------------------------------------------

APP = Path(__file__).resolve().parents[1] / "app.py"


def test_switching_postings_in_the_app(monkeypatch, resume):
    import ats.extraction
    monkeypatch.setattr(ats.extraction, "fetch_job", lambda url: ROCKET_LAB)
    app = AppTest.from_file(str(APP), default_timeout=120)
    app.run()
    app.radio(key="workspace_page").set_value("Resume Grader & Tailor").run()
    app.session_state["master"] = resume
    app.session_state["extracted"] = Extracted(resume.text, "test")
    app.session_state["demo"] = False
    app.run()
    assert app.text_input(key="job_url").value == ""  # no pre-filled Western Digital URL

    def run(step):
        if step == "wd":
            next(b for b in app.button if b.label == "Use saved Western Digital example").click().run()
        else:
            app.text_input(key="job_url").set_value("https://job-boards.greenhouse.io/rocketlab/jobs/7998598003").run()
            next(b for b in app.button if b.label == "Fetch job URL").click().run()
        next(b for b in app.button if b.label == "Analyze Resume").click().run()
        assert not app.exception, app.exception
        banner = next(i.value for i in app.info if i.value.startswith("Grading:"))
        table = next(d.value for d in app.dataframe if "Resume evidence" in d.columns)
        alignment = next(m.value for m in app.metric if m.label == "Job alignment · original")
        return banner, table, alignment

    wd_banner, wd_table, wd_score = run("wd")
    rl_banner, rl_table, rl_score = run("rl")
    wd2_banner, wd2_table, wd2_score = run("wd")
    assert "Summer 2027 Intern - Hardware Engineering" in wd_banner and "Western Digital" in wd_banner
    assert "Electrical Engineering Intern Summer 2027" in rl_banner and "rocketlab" in rl_banner
    assert not any("ASIC" in str(x) for x in rl_table["Requirement"])
    assert wd_score == wd2_score and wd_table.equals(wd2_table) and rl_score != wd_score


def test_failed_fetch_clears_the_previous_posting(monkeypatch, resume):
    import ats.extraction
    from ats.extraction import InputError

    def fail(url):
        raise InputError("The posting is blocked or incomplete.")
    monkeypatch.setattr(ats.extraction, "fetch_job", fail)
    app = AppTest.from_file(str(APP), default_timeout=120)
    app.run()
    app.radio(key="workspace_page").set_value("Resume Grader & Tailor").run()
    next(b for b in app.button if b.label == "Use saved Western Digital example").click().run()
    assert app.text_area(key="job_input").value.startswith("Summer 2027 Intern")
    app.text_input(key="job_url").set_value("https://job-boards.greenhouse.io/rocketlab/jobs/7998598003").run()
    next(b for b in app.button if b.label == "Fetch job URL").click().run()
    assert app.text_area(key="job_input").value == ""  # Western Digital text is gone, not silently graded
    assert any("previous job description was cleared" in e.value for e in app.error)


def test_managing_interfaces_is_not_leadership():
    from ats.models import Requirement
    resume = parse_resume("Alex Example\nEXPERIENCE\nLab | 2026\n• Routed low-noise signals and managed multi-chip interfaces on an EEG board.")
    req = Requirement("R1", "ability to lead development projects", "soft_skill", "required", ("Leadership & ownership",))
    assert match_requirement(req, resume).status == "missing"
    led = parse_resume("Alex Example\nLEADERSHIP\nClub | 2025\n• Coordinated fabrication activities on a 12-member team.")
    assert match_requirement(req, led).status == "supported"
