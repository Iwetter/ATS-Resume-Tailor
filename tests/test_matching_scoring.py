from dataclasses import replace

import pytest

from ats.matching import (extract_requirements, has_term, match_requirement, parse_resume, rank_projects, select_projects, similarity, skills_in, validate_requirements)
from ats.models import Extracted, Requirement
from ats.scoring import analyze


@pytest.mark.parametrize("text,term", [("I²C", "I2C"), ("printed circuit board", "PCB"), ("pulse-width modulation", "PWM"), ("field-programmable gate array", "FPGA")])
def test_meaningful_aliases(text, term):
    assert has_term(text, term)


def test_unrelated_technologies_remain_distinct():
    assert skills_in("C++ and VHDL on ESP32") >= {"C++", "VHDL", "ESP32"}
    assert not skills_in("C++ and VHDL on ESP32") & {"C", "Verilog", "SystemVerilog", "STM32"}
    assert not has_term("electrical circuits and spindles", "C")
    assert "Verilog" not in skills_in("SystemVerilog")


def test_requirement_priority_and_or_grouping():
    text = "Required:\nExperience with C or C++ in embedded systems is required.\nPreferred:\nKiCad experience is a plus for PCB design and testing.\nResponsibilities:\nDesign and validate electronic circuit boards in a laboratory."
    reqs = extract_requirements(text)
    alternatives = next(r for r in reqs if "C++" in r.terms)
    assert alternatives.mode == "any" and alternatives.priority == "required"
    assert next(r for r in reqs if "KiCad" in r.terms).priority == "preferred"
    assert any(r.kind == "responsibility" for r in reqs)


def test_skill_list_is_only_partial_evidence(master):
    req = Requirement("r", "KiCad", terms=("KiCad",))
    full = match_requirement(req, master)
    assert full.status == "supported" and full.evidence_ids
    listed = parse_resume("Alex Example\nSKILLS\nKiCad, Altium\nEDUCATION\nBachelor of Science")
    assert match_requirement(req, listed).status == "partial"
    assert match_requirement(replace(req, terms=("STM32",)), master).status == "missing"


def test_alternative_requirement_does_not_require_every_language(master):
    req = Requirement("r", "C or C++", terms=("C", "C++"), mode="any")
    assert match_requirement(req, master).status == "supported"
    assert match_requirement(replace(req, mode="all"), master).status == "partial"


def test_future_and_negative_claims_are_not_demonstrated():
    r = parse_resume("Alex Example\nPROJECTS\nPrototype\n• Planned to build a PCB in Altium.\n• Designed a circuit without experience in KiCad.")
    assert match_requirement(Requirement("r", "Altium", terms=("Altium",)), r).status != "supported"
    assert match_requirement(Requirement("r", "KiCad", terms=("KiCad",)), r).status == "missing"


def test_hard_eligibility_is_conservative(master):
    assert match_requirement(Requirement("r", "Minimum GPA 3.0", "eligibility"), master).status == "met"
    assert match_requirement(Requirement("r", "Minimum GPA 3.8", "eligibility"), master).status == "unmet"
    assert match_requirement(Requirement("r", "Must be authorized to work in the United States", "eligibility"), master).status == "unknown"
    assert match_requirement(Requirement("r", "Graduating in 2027", "eligibility"), master).status == "unknown"
    assert match_requirement(Requirement("r", "Currently pursuing a Bachelor's degree in Electrical Engineering", "eligibility"), master).status == "met"
    assert match_requirement(Requirement("r", "GPA 3.0 and authorized to work in the US", "eligibility"), master).status == "unknown"
    masters_only = parse_resume("Alex Example\nEDUCATION\nMaster of Science in Electrical Engineering, Expected 2028")
    assert match_requirement(Requirement("r", "Bachelor's degree in Electrical Engineering required", "eligibility"), masters_only).status == "unknown"


def test_eligibility_override_requires_note():
    with pytest.raises(ValueError, match="note"):
        validate_requirements([{"text": "Work authorization", "kind": "eligibility", "assertion": "met"}])


def test_no_default_profile_facts_are_added():
    r = parse_resume("Someone Else\nEDUCATION\nOther College, Computer Science, 2030\nPROJECTS\n• Built a robot.")
    assert "Georgia" not in r.text and "3.68" not in r.text and "2028" not in r.text


def test_deterministic_ledger_and_scoring(master, resume_text, job_text):
    assert master == parse_resume(resume_text)
    assert len({x.id for x in master.items}) == len(master.items)
    assert len({f.id for f in master.facts}) == len(master.facts)
    reqs = extract_requirements(job_text)
    a = analyze(master, Extracted(resume_text, "demo"), reqs)
    assert a == analyze(master, Extracted(resume_text, "demo"), reqs)
    assert 0 <= a.readability <= 100 and 0 <= a.alignment <= 100
    assert any(r.kind == "eligibility" for r in reqs)
    assert any("PCB" in r.terms for r in reqs)
    assert any(m.status == "missing" for m in a.matches)


def test_keyword_repetition_cannot_improve_coverage_or_tfidf(master):
    reqs = (Requirement("r", "PCB", terms=("PCB",)),)
    r = parse_resume(master.text + "\n" + "PCB " * 80)
    a = analyze(master, Extracted(master.text, "demo"), reqs)
    b = analyze(r, Extracted(r.text, "demo"), reqs)
    assert a.components["skill_coverage"] == b.components["skill_coverage"]
    assert similarity("PCB design", "PCB design test") == pytest.approx(similarity("PCB design", "PCB " * 50 + "design test"))


def test_project_selection_retains_experience_and_whole_projects(master, job_text):
    ranked = rank_projects(master, extract_requirements(job_text))
    selected = select_projects(master, [ranked[0][0]])
    assert [x for x in master.items if x.section == "Experience"] == [x for x in selected.items if x.section == "Experience"]
    assert {x.group for x in selected.items if x.section == "Projects" and x.group} == {ranked[0][0]}
    assert len(rank_projects(master, ())) == 4
    assert master.text.count("Four-Layer") == 1


def test_all_zero_weights_fail(master):
    with pytest.raises(ValueError, match="positive total"):
        analyze(master, Extracted(master.text, "demo"), (), {"skill_coverage": 0, "experience_evidence": 0, "responsibilities": 0, "clarity": 0})
