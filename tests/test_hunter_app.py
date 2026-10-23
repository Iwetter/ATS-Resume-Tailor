from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock
import json

import requests
from streamlit.testing.v1 import AppTest

from hunter.models import Employer
from hunter.sources import normalize
from hunter.storage import Store

APP = Path(__file__).resolve().parents[1] / "app.py"


def button(app, label):
    return next(x for x in app.button if x.label == label)


def test_hunter_dashboard_tailoring_and_persistent_status(tmp_path, master, monkeypatch):
    store = Store(tmp_path / "hunter.sqlite3")
    store.save_master(master)  # Clearly labeled fictional source for this test only.
    payload = json.loads((APP.parent / "tests/fixtures/hunter/greenhouse.json").read_text())["detail"]
    employer = Employer("Fixture Engineering", "greenhouse", "fixture", "https://job-boards.greenhouse.io/fixture")
    job = normalize("greenhouse", employer, payload, "2026-10-20T16:00:00+00:00")
    key, _ = store.upsert(job, "2026-10-20T16:00:00+00:00")
    paid = Mock(side_effect=AssertionError("Unintended model call"))
    monkeypatch.setattr(requests, "post", paid)
    app = AppTest.from_file(str(APP), default_timeout=40)
    app.session_state["hunter_db_path"] = str(store.path)
    app.run()
    app.radio(key="workspace_page").set_value("Find Internships").run()
    assert not app.exception
    assert any("Original resume fit" in m.label for m in app.metric)
    next(s for s in app.selectbox if s.label == "Application status").set_value("Applied").run()
    button(app, "Save application status").click().run()
    assert store.job(key)["status"] == "Applied"
    button(app, "Tailor Resume for this job").click().run()
    assert not app.exception
    assert app.radio(key="workspace_page").value == "Resume Grader & Tailor"
    assert app.session_state["job_input"] == job.description
    button(app, "Analyze Resume").click().run()
    button(app, "Tailor Resume").click().run()
    button(app, "Generate validated PDF & DOCX").click().run()
    assert not app.exception
    button(app, "Save validated resume to Internship Hunter").click().run()
    assert not app.exception
    assert len(store.artifacts()) == 1 and paid.call_count == 0
    metadata = json.loads(store.artifacts()[0]["metadata"])
    assert metadata["original_fit_estimate"] is not None and metadata["tailored_fit_estimate"] is not None
    assert not metadata["approved_change_ids"]  # No fabricated rewrite or gain.
    app.radio(key="workspace_page").set_value("Find Internships").run()
    next(r for r in app.radio if r.label == "Opportunity view").set_value("Tailored Resumes").run()
    assert not app.exception
    assert any(x.proto.label == "Download PDF" for x in app.get("download_button"))
    assert store.master() == master


def test_hunter_without_master_does_not_score_demo_or_call_network(tmp_path, monkeypatch):
    paid = Mock(side_effect=AssertionError("Unintended model call"))
    get = Mock(side_effect=AssertionError("No auto scan on refresh"))
    monkeypatch.setattr(requests, "post", paid)
    monkeypatch.setattr(requests.sessions.Session, "get", get)
    app = AppTest.from_file(str(APP), default_timeout=30)
    app.session_state["hunter_db_path"] = str(tmp_path / "hunter.sqlite3")
    app.run()
    app.radio(key="workspace_page").set_value("Resume Grader & Tailor").run()  # Dashboard is now the landing page
    button(app, "Use fictional test resume").click().run()
    app.radio(key="workspace_page").set_value("Find Internships").run()
    assert not app.exception
    assert any("No verified master" in w.value for w in app.warning)
    assert not any(b.label == "Save verified master for Hunter" for b in app.button)
    app.run()
    assert get.call_count == 0 and paid.call_count == 0
