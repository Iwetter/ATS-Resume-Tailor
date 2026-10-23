"""Phase 2 grader: real-posting headings, terminology equivalents and honest status labels.

Cases come from 207 real EE internship postings (October 2026 scan, tools/grader_audit.py)."""
from collections import Counter

import pytest

from ats.evidence import verify_rewrite
from ats.matching import _heading, _restore_structure, extract_requirements, match_requirement, parse_resume, skills_in, tokens
from ats.models import Change, Requirement

RESUME = parse_resume("""Alex Example
EXPERIENCE
Hardware Lab | Research Assistant | 2026
• Debugged power rails during board bring-up using an oscilloscope and multimeter.
• Wrote ESP32 firmware reading sensors over I2C and SPI.
PROJECTS
FPGA Counter | 2026
• Implemented a counter in VHDL and verified it in simulation.
SKILLS
KiCad, Altium, LTspice, Python, Rust""")


@pytest.mark.parametrize("line,kind", [
    ("REQUIRED QUALIFICATIONS", "required"), ("Basic Qualifications:", "required"), ("You’ll bring these qualifications:", "required"),
    ("What makes you a good fit:", "required"), ("What We Hope You’ll Bring", "required"), ("Qualifications You Must Have", "required"),
    ("Who you are", "required"), ("Preferred Qualifications", "preferred"), ("Nice-to-have skills", "preferred"), ("Bonus Points", "preferred"),
    ("Qualifications We Prefer", "preferred"), ("Desired Characteristics", "preferred"), ("What You’ll Do", "responsibilities"),
    ("How you'll make an impact:", "responsibilities"), ("Benefits", "context"),
    ("Experience with PCB design in Altium", None), ("Strong communication skills", None), ("Design and bring up boards.", None),
])
def test_real_heading_styles(line, kind):
    assert _heading(line) == kind


def test_single_line_workday_description_gets_its_sections_back():
    text = ("Date Posted: 2026-10-23 Country: United States Job Description: You will support avionics hardware test. "
            "Qualifications You Must Have Currently pursuing a BS in Electrical Engineering. Experience with oscilloscopes and soldering. "
            "Qualifications We Prefer Experience with Python scripting and LabVIEW. What We Offer Relocation support.")
    assert "\nQualifications You Must Have:\n" in _restore_structure(text)
    reqs = extract_requirements(text)
    by_terms = {r.terms: r.priority for r in reqs if r.kind == "skill"}
    assert by_terms[("Oscilloscope",)] == "required" and by_terms[("LabVIEW",)] == "preferred"


def test_structured_text_is_left_alone():
    text = "Requirements:\nExperience with KiCad.\n" * 3
    assert _restore_structure(text) == text


@pytest.mark.parametrize("term", ["schematics", "bring-up", "communication protocols", "lab equipment", "soldering", "microcontroller",
                                  "control systems", "Ethernet", "RF", "SPICE", "harness", "HDL", "scripting", "version control", "BLE"])
def test_terms_from_real_postings_are_known(term):
    assert skills_in("Experience with " + term + ".")


@pytest.mark.parametrize("requirement,shown_by", [
    ("Hands-on experience with lab equipment.", "Oscilloscope"), ("Familiarity with communication protocols.", "I2C"),
    ("Experience with an HDL.", "VHDL"), ("Experience with microcontrollers.", "ESP32"), ("Circuit simulation experience.", "LTspice"),
])
def test_umbrella_requirements_are_met_by_specific_evidence(requirement, shown_by):
    req = next(r for r in extract_requirements("Requirements:\n" + requirement + "\nThis Summer 2027 internship is located in Atlanta and supports hardware development.") if r.kind == "skill")
    match = match_requirement(req, RESUME)
    assert match.status in {"supported", "partial"} and shown_by in match.explanation


def test_missing_vs_listed_vs_demonstrated_labels():
    reqs = {r.terms: r for r in extract_requirements("Requirements:\nExperience with Rust.\nExperience with Ethernet.\nExperience with Python and KiCad layout tools.\nThis internship supports hardware development teams.") if r.kind == "skill"}
    listed = match_requirement(reqs[("Rust",)], RESUME)
    absent = match_requirement(reqs[("Ethernet",)], RESUME)
    assert listed.status == "partial" and listed.explanation.startswith("Listed, not demonstrated")
    assert absent.status == "missing" and absent.explanation.startswith("Not on your resume") and "does not mean you lack it" in absent.explanation


def test_duties_match_on_word_stems_and_say_they_are_duties():
    req = Requirement("R1", "Bring up and debug new hardware", "responsibility", "context")
    match = match_requirement(req, RESUME)
    assert match.status in {"supported", "partial"} and "duty of the job, not a missing qualification" in match.explanation


def test_verifier_flags_new_activities_but_rejects_new_tools():
    master = parse_resume("Alex Example\nPROJECTS\nRadar Board | 2026\n• Assisted with the layout of a radar PCB in Altium and probed it with an oscilloscope.")
    source = next(x for x in master.items if x.kind == "bullet")
    activity = verify_rewrite(Change((source.id,), source.text, "Supported PCB layout of a radar board in Altium and probed it with an oscilloscope.", "x"), master)
    assert activity.status == "review"
    umbrella = verify_rewrite(Change((source.id,), source.text, "Assisted with radar PCB layout in Altium and checked it with lab equipment.", "x"), master)
    assert umbrella.status == "review"  # oscilloscope evidence supports "lab equipment"
    tool = verify_rewrite(Change((source.id,), source.text, "Assisted with the layout of a radar PCB in Altium and probed it with a spectrum analyzer.", "x"), master)
    assert tool.status == "rejected" and "Spectrum analyzer" in " ".join(tool.reasons)


def test_score_explains_components_and_splits_gaps(tmp_path):
    from hunter.models import Job
    from hunter.service import score_job
    job = Job("manual", "x", "1", "Acme", "Hardware Intern", "Atlanta, GA", "https://example.com",
              "Requirements:\nExperience with Rust.\nExperience with Ethernet.\nExperience with lab equipment.\nResponsibilities:\nDesign RF front ends for satellites.\n"
              "This internship supports our hardware engineering team in Atlanta during Summer 2027.")
    result = score_job(job, RESUME)
    assert "Ethernet" in result["not_on_resume"] and "Rust" in result["listed_not_demonstrated"]
    assert set(result["component_notes"]) == set(result["components"]) and all(result["component_notes"].values())


def test_cached_text_functions_return_independent_sets():
    first = tokens("Designed a PCB")
    first.add("mutated")
    assert "mutated" not in tokens("Designed a PCB")
