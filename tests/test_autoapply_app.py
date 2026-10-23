"""Streamlit Auto Apply workspace (AppTest), with fictional data only."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from autoapply import db as S
from test_autoapply_browser import prepared

APP = Path(__file__).resolve().parents[1] / "app.py"


def open_workspace(store):
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.session_state["hunter_db_path"] = str(store.path)
    app.run()
    app.radio(key="workspace_page").set_value("Auto Apply").run()
    assert not app.exception, app.exception
    return app


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_workspace_renders_without_data(aa):
    app = open_workspace(aa.store)
    labels = [b.label for b in app.button]
    assert "STOP ALL APPLICATIONS" in labels and "Run dry-run validation" in labels
    assert any("Real submission is disabled" in i.value for i in app.info)
    assert any(m.label == "Mode" and "REVIEW" in m.value for m in app.metric)


def test_emergency_stop_master_switch_and_permissions(aa):
    app = open_workspace(aa.store)
    button(app, "STOP ALL APPLICATIONS").click().run()
    assert aa.auto.emergency_stopped() and any("EMERGENCY STOP ACTIVE" in e.value for e in app.error)
    button(app, "Clear emergency stop").click().run()
    assert not aa.auto.emergency_stopped()
    app.toggle(key="aa_master").set_value(True).run()
    assert aa.auto.enabled()
    assert any(r["action"] == "master_switch" for r in aa.auto.audit_log())


@pytest.mark.usefixtures("chromium_available")
def test_review_queue_approval_from_dashboard(aa):
    app_row = prepared(aa)
    assert app_row["state"] == S.READY
    app = open_workspace(aa.store)
    assert not app.exception
    button(app, "Approve submission").click().run()
    assert not app.exception
    assert aa.auto.application(app_row["id"])["state"] == S.APPROVED
    assert aa.router.posts == []  # approval alone never submits from the UI
