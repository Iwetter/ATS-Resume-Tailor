"""Tests for local, zero-paid-API cover letter generation and attachment."""
from pathlib import Path
import pytest

from ats.matching import make_resume
from ats.models import Evidence, Resume
from hunter.models import Job
from autoapply.cover_letter import (
    build_deterministic_paragraphs,
    generate_cover_letter,
    try_ollama_paragraphs,
)
from autoapply.engine import write_cover_letter
from autoapply.forms import FormField, resolve
from autoapply.llm import validate_cover_letter
from autoapply.profile import ApplicantProfile


@pytest.fixture
def sample_master():
    items = (
        Evidence("E1", "header", "Ian Hall", "Header", ""),
        Evidence("E2", "header", "Atlanta, GA | ihall32@gatech.edu", "Header", ""),
        Evidence("E3", "text", "Georgia Institute of Technology, Atlanta, GA May 2028", "Education", ""),
        Evidence("E4", "text", "Bachelor of Science in Electrical Engineering", "Education", ""),
        Evidence("E5", "text", "Cumulative GPA: 3.77 / 4.00", "Education", ""),
        Evidence("E6", "entry", "Embedded MP3 Player Prototype | May 2026", "Projects", "P1"),
        Evidence("E7", "bullet", "Designed, soldered, and assembled a portable ESP32-based MP3 player using an SSD1306 OLED, MAX98357A I2S amplifier, microSD storage, physical controls, and perfboard.", "Projects", "P1"),
        Evidence("E8", "bullet", "Developed Arduino/C++ firmware for display menus, audio playback, song navigation, and button input; debugged I2C/I2S communication, microSD access, display freezes, and power issues.", "Projects", "P1"),
        Evidence("E9", "skill", "Programming: C, C++, Python, VHDL", "Skills", ""),
        Evidence("E10", "skill", "Embedded: ESP32, I2C, SPI, UART, PWM", "Skills", ""),
    )
    return make_resume(items, "test-master-hash")


@pytest.fixture
def applicant_profile():
    return ApplicantProfile(
        first_name="Ian",
        last_name="Hall",
        email="ihall32@gatech.edu",
        school="Georgia Institute of Technology",
        degree="Bachelor of Science",
        major="Electrical Engineering",
        graduation="2028-05",
        gpa="3.77",
    )


@pytest.fixture
def sample_job():
    return Job(
        source="greenhouse",
        board="anduril",
        source_id="REQ-101",
        company="Anduril Industries",
        title="Firmware Engineer Intern",
        location="Atlanta, GA",
        url="https://job-boards.greenhouse.io/anduril/jobs/101",
        description="We are looking for a Firmware Engineer Intern with experience in C, C++, and microcontroller systems.",
    )


def test_deterministic_cover_letter_validation(sample_master, sample_job, applicant_profile):
    """Deterministic cover letters must cite real evidence and pass validate_cover_letter with 0 problems."""
    paragraphs = build_deterministic_paragraphs(sample_master, sample_job, applicant_profile, chosen_projects=("P1",))
    assert len(paragraphs) == 3
    for p in paragraphs:
        assert p["text"].strip()
        assert len(p["evidence_ids"]) >= 1
        for eid in p["evidence_ids"]:
            assert eid in sample_master.by_id

    problems = validate_cover_letter(paragraphs, sample_master, sample_job)
    assert problems == [], f"Validation failed with: {problems}"


def test_cover_letter_generation_pipeline(sample_master, sample_job, applicant_profile, tmp_path):
    """Pipeline generates dictionary, approved flag, and outputs valid PDF."""
    result = generate_cover_letter(sample_master, sample_job, applicant_profile, chosen_projects=("P1",), use_ollama=False)
    assert result["approved"] is True
    assert result["method"] == "deterministic_template"
    assert result["problems"] == []
    assert len(result["paragraphs"]) == 3
    assert "Anduril Industries" in result["text"]

    pdf_path = tmp_path / "Cover_Letter.pdf"
    write_cover_letter(result["text"], applicant_profile, pdf_path)
    assert pdf_path.is_file()
    assert pdf_path.stat().st_size > 500


def test_cover_letter_form_resolution_required(sample_master, sample_job, applicant_profile, tmp_path):
    """When a form has a required cover letter file or textarea, resolve() fills it from the generated letter."""
    result = generate_cover_letter(sample_master, sample_job, applicant_profile, chosen_projects=("P1",), use_ollama=False)
    pdf_path = tmp_path / "Cover_Letter.pdf"
    write_cover_letter(result["text"], applicant_profile, pdf_path)

    cover = {"text": result["text"], "path": str(pdf_path)}

    # Test file upload field
    file_field = FormField(fid="cl_file", name="cover_letter", label="Cover Letter *", kind="file", required=True)
    # Test textarea field
    text_field = FormField(fid="cl_text", name="cover_letter_text", label="Paste Cover Letter *", kind="textarea", required=True)

    resolutions = resolve([file_field, text_field], applicant_profile, answers=[], resume_path="fake_resume.pdf", cover_letter=cover)

    res_file = next(r for r in resolutions if r.fid == "cl_file")
    assert res_file.filled is True
    assert res_file.source == "cover_letter"
    assert res_file.value == str(pdf_path)

    res_text = next(r for r in resolutions if r.fid == "cl_text")
    assert res_text.filled is True
    assert res_text.source == "cover_letter"
    assert res_text.value == result["text"]


def test_ollama_fallback_on_unreachable_endpoint(sample_master, sample_job, applicant_profile):
    """When Ollama endpoint is down or times out, generate_cover_letter falls back to deterministic template without error."""
    result = generate_cover_letter(
        sample_master, sample_job, applicant_profile, chosen_projects=("P1",),
        use_ollama=True, ollama_endpoint="http://127.0.0.1:59999"  # invalid port
    )
    assert result["method"] == "deterministic_template"
    assert result["approved"] is True
    assert len(result["paragraphs"]) == 3
    assert validate_cover_letter(result["paragraphs"], sample_master, sample_job) == []
