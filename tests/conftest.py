from io import BytesIO
from pathlib import Path

from docx import Document
import pytest

from ats.matching import parse_resume

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def resume_text():
    return (FIXTURES / "fictional_resume.txt").read_text(encoding="utf-8")


@pytest.fixture
def master(resume_text):
    return parse_resume(resume_text)


@pytest.fixture
def job_text():
    return (FIXTURES / "western_digital_job.txt").read_text(encoding="utf-8")


@pytest.fixture
def docx_bytes(resume_text):
    doc = Document()
    for line in resume_text.splitlines():
        doc.add_paragraph(line)
    output = BytesIO()
    doc.save(output)
    return output.getvalue()
