"""Exercise Streamlit reruns, manual review, approval and stale-export gates."""
from pathlib import Path
from unittest.mock import Mock

import requests
from streamlit.testing.v1 import AppTest


APP = Path(__file__).resolve().parents[1] / "app.py"


def button(app, label):
    return next(x for x in app.button if x.label == label)


def area(app, label):
    return next(x for x in app.text_area if x.label == label)


def loaded_app():
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.radio(key="workspace_page").set_value("Resume Grader & Tailor").run()
    button(app, "Use fictional test resume").click().run()
    button(app, "Use saved Western Digital example").click().run()
    assert not app.exception
    return app


def test_streamlit_local_workflow_and_stale_export(monkeypatch):
    network = Mock(side_effect=AssertionError("Unexpected paid API request"))
    monkeypatch.setattr(requests, "post", network)
    app = loaded_app()
    button(app, "Analyze Resume").click().run()
    assert not app.exception
    assert any("Job alignment" in x.label for x in app.metric)
    original_text = app.session_state["master"].text
    button(app, "Tailor Resume").click().run()
    assert not app.exception
    assert any("no AI rewriting happened" in x.value for x in app.info)
    edits = [x for x in app.text_area if x.label == "Proposed bullet"]
    pcb = next(x for x in edits if "four-layer" in x.value)
    revised = "Designed and built a four-layer wearable PCB with 3.3 V power rails in KiCad."
    pcb.set_value(revised).run()
    # Match a decision to the known evidence ID, not UI ordering.
    source_id = next(x.id for x in app.session_state["master"].items if x.kind == "bullet" and "four-layer" in x.text)
    decision = next(x for x in app.radio if source_id in x.key)
    decision.set_value("Approve change").run()
    button(app, "Generate validated PDF & DOCX").click().run()
    assert not app.exception
    assert app.session_state["bundle"].pages == 1
    assert revised in app.session_state["bundle"].text
    assert app.session_state["master"].text == original_text
    assert network.call_count == 0
    # Changing approval immediately makes old downloads unavailable.
    next(x for x in app.radio if source_id in x.key).set_value("Reject change").run()
    assert any("Generate fresh" in x.value for x in app.info)
    downloads = [x.proto.label for x in app.get("download_button")]
    assert "Download tailored PDF" not in downloads
    button(app, "Generate validated PDF & DOCX").click().run()
    assert next(x.text for x in app.session_state["master"].items if x.id == source_id) in app.session_state["bundle"].text
    app.run()
    assert not app.exception and network.call_count == 0


def test_streamlit_requirements_change_requires_reanalysis():
    app = loaded_app()
    button(app, "Analyze Resume").click().run()
    area(app, "Paste or review job description").set_value("Required: Experience developing STM32 firmware and using C++ for embedded systems testing and validation of electronic hardware.").run()
    assert not app.exception
    assert not any(x.label == "Tailor Resume" for x in app.button)
