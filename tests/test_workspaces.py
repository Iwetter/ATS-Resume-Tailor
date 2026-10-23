"""Navigation: every workspace renders from real data with no exceptions or placeholder pages."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"
WORKSPACES = ["Dashboard", "Find Internships", "Resume Grader & Tailor", "Auto Apply", "Applications", "Notifications", "Settings"]


@pytest.mark.parametrize("page", WORKSPACES)
def test_each_workspace_renders(aa, page):
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.session_state["hunter_db_path"] = str(aa.store.path)
    app.run()
    assert app.radio(key="workspace_page").options == WORKSPACES
    app.radio(key="workspace_page").set_value(page).run()
    assert not app.exception, app.exception


def test_dashboard_reports_real_state_and_navigates(aa):
    aa.ready = None
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.session_state["hunter_db_path"] = str(aa.store.path)
    app.run()
    labels = {m.label: m.value for m in app.metric}
    assert labels["Awaiting approval"] == "0" and "Relevant now" in labels
    assert any("Off (forms are prepared only)" in str(v) for v in app.dataframe[0].value["Status"])
    next(b for b in app.button if b.label == "Notifications").click().run()
    assert app.radio(key="workspace_page").value == "Notifications" and not app.exception
