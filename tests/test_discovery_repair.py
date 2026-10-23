"""Milestone A: broad retrieval, description-based classification, diagnostics
and result views. Each case below was silently dropped by the previous code."""
from dataclasses import replace
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from hunter.filtering import classify_discipline, decide, title_screen
from hunter.models import Employer, Filters, Job, Profile, Snapshot
from hunter.service import scan
from hunter.storage import Store

NOW = "2026-10-23T12:00:00+00:00"
EE_DESCRIPTION = ("Summer 2027 internship. You will capture schematics in Altium, bring up prototype PCBs, and write embedded C firmware for STM32 "
                  "microcontrollers over I2C and SPI. Requirements: pursuing a BS in Electrical Engineering or Computer Engineering; lab experience with an oscilloscope. "
                  "This position is located in Atlanta, GA.")
VAGUE_DESCRIPTION = "Summer 2027 internship supporting our team with research, presentations and spreadsheets. Located in Austin, TX."

PREVIOUSLY_DROPPED = ["Engineering Intern", "Systems Engineering Intern", "Summer 2027 Engineering Co-op", "Engineering Development Intern",
                      "Technical Intern", "R&D Intern", "Engineering Student Intern", "Student Engineer - Summer 2027", "Test Engineering Intern"]
ALWAYS_KEPT = ["Electrical Engineering Co-op", "Hardware Engineering Intern", "Embedded Systems Intern", "Firmware Intern", "Digital Design Intern"]


def job(title, description=EE_DESCRIPTION, **changes):
    base = Job("greenhouse", "acme", "1", "Acme Hardware", title, "Atlanta, GA", "https://job-boards.greenhouse.io/acme/jobs/1", description,
               published_at=None, publication_basis="Posting date unknown", checked_at=NOW)
    job = replace(base, **changes)
    return replace(job, url=f"https://job-boards.greenhouse.io/acme/jobs/{job.source_id}")


@pytest.mark.parametrize("title", PREVIOUSLY_DROPPED + ALWAYS_KEPT)
def test_engineering_internship_titles_are_retrieved(title):
    assert title_screen(title) == "keep"


@pytest.mark.parametrize("title", PREVIOUSLY_DROPPED)
def test_generic_titles_are_classified_from_the_description(title):
    field, basis = classify_discipline(title, EE_DESCRIPTION)
    assert field in {"Embedded / Firmware", "PCB / Hardware", "Electrical", "Validation / Test"} and basis
    assert decide(job(title), Profile(), Filters(), NOW).eligible


@pytest.mark.parametrize("title,verdict", [("Senior Hardware Engineer", "not_internship"), ("Marketing Intern", "non_technical"), ("Finance Co-op", "non_technical"),
                                           ("Recruiting Intern", "non_technical")])
def test_clearly_irrelevant_titles_are_rejected_with_a_reason(title, verdict):
    assert title_screen(title) == verdict


@pytest.mark.parametrize("title", ["Software Engineering Intern", "Mechanical Engineering Intern", "Data Science Intern", "Marketing Intern Summer 2027",
                                   # false positives seen in the live scan: descriptions mention electrical work, the role is not EE
                                   "2027 Technical Program Manager Intern", "Thermal Intern (Summer 2027)", "Engineering Intern - Aero Design - Summer 2027",
                                   "Military DoD SkillBridge Internship - Maintenance Technician"])
def test_other_fields_explain_themselves(title):
    decision = decide(job(title), Profile(), Filters(), NOW)
    assert not decision.eligible and any(r.startswith("Unrelated discipline: Title names another field") for r in decision.reasons)


def test_generic_title_without_ee_evidence_is_explained_not_hidden():
    decision = decide(job("Engineering Intern", VAGUE_DESCRIPTION), Profile(), Filters(), NOW)
    assert not decision.eligible and any("Neither the title nor the description" in r for r in decision.reasons)


def test_unknown_publication_date_is_not_a_rejection():
    decision = decide(job("Engineering Intern"), Profile(), Filters(), NOW)
    assert decision.eligible and decision.recency == "unknown"


def test_co_op_listed_only_in_employment_type():
    assert decide(job("Hardware Engineer, Summer 2027", employment_type="Co-op"), Profile(), Filters(), NOW).eligible


def test_scan_reports_every_stage(tmp_path, monkeypatch):
    store = Store(tmp_path / "hunter.sqlite3")
    rows = [job("Engineering Intern", source_id="1"), job("Summer 2027 Engineering Co-op", source_id="2"),
            job("Mechanical Engineering Intern", source_id="3"), job("Engineering Intern", VAGUE_DESCRIPTION, source_id="4")]
    rejected = (("Senior Hardware Engineer", "not_internship"), ("Marketing Intern", "non_technical"))
    monkeypatch.setattr("hunter.service.fetch_board", lambda e, c: Snapshot(tuple(rows), frozenset({"1", "2", "3", "4", "5", "6"}), NOW, listed=6, rejected=rejected))
    result = scan(store, [Employer("Acme Hardware", "greenhouse", "acme", "https://job-boards.greenhouse.io/acme")], None, Profile(), Filters(), client=object())
    d = result["diagnostics"]
    assert (d["listed"], d["rejected_not_internship"], d["rejected_non_technical"], d["retained"], d["eligible"]) == (6, 1, 1, 4, 2)
    assert d["decision_reasons"] == {"Unrelated discipline": 2}
    assert result["title_rejections"] == [{"employer": "Acme Hardware", "title": "Marketing Intern", "reason": "Internship in a non-technical field (title)"}]
    assert {r["title"] for r in result["decision_rejections"]} == {"Mechanical Engineering Intern", "Engineering Intern"}
    assert len(store.jobs()) == 4  # kept jobs are stored even when judged not relevant


def test_greenhouse_adapter_keeps_generic_titles(tmp_path):
    from hunter.sources import fetch_board

    class Client:
        def json(self, url, force=False):
            if url.endswith("/jobs"):
                return {"jobs": [{"id": 1, "title": "Engineering Intern"}, {"id": 2, "title": "Senior Hardware Engineer"},
                                 {"id": 3, "title": "Marketing Intern"}], "meta": {"total": 3}}, NOW, False
            return {"id": 1, "title": "Engineering Intern", "content": EE_DESCRIPTION, "location": {"name": "Atlanta, GA"},
                    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1", "first_published": "2026-10-20T00:00:00Z"}, NOW, False

    snap = fetch_board(Employer("Acme", "greenhouse", "acme", "https://job-boards.greenhouse.io/acme"), Client())
    assert [j.title for j in snap.jobs] == ["Engineering Intern"] and snap.listed == 3
    assert set(snap.rejected) == {("Senior Hardware Engineer", "not_internship"), ("Marketing Intern", "non_technical")}


def test_hunter_views_show_unknown_date_jobs_and_diagnostics(tmp_path):
    store = Store(tmp_path / "hunter.sqlite3")
    store.upsert(job("Engineering Intern"), NOW)  # Lever-style: no publication date
    store.upsert(job("Mechanical Engineering Intern", source_id="9"), NOW)
    store.set("last_scan", {"started": NOW, "finished": NOW, "boards": [], "new_ids": [],
                            "diagnostics": {"listed": 10, "rejected_not_internship": 7, "rejected_non_technical": 1, "retained": 2, "eligible": 1, "scored": 0,
                                            "decision_reasons": {"Unrelated discipline": 1}},
                            "title_rejections": [{"employer": "Acme", "title": "Marketing Intern", "reason": "x"}], "decision_rejections": []})
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=60)
    app.session_state["hunter_db_path"] = str(store.path)
    app.run()
    app.radio(key="workspace_page").set_value("Find Internships").run()
    assert not app.exception, app.exception
    best = app.dataframe[-1].value
    assert list(best["Title"]) == ["Engineering Intern"] and best["Published"][0] == "Unknown"
    assert any("Discovery diagnostics: 10 listings fetched" in e.label for e in app.expander)
    app.radio(key="hunter_view").set_value("All Discovered Jobs").run()
    everything = app.dataframe[-1].value
    assert set(everything["Title"]) == {"Engineering Intern", "Mechanical Engineering Intern"}
    assert everything.set_index("Title").loc["Mechanical Engineering Intern", "Eligibility"].startswith("No: Unrelated discipline")


# ---- bugs found by the live scan (October 23, 2026) ------------------------------

def test_ashby_posting_with_null_address_is_normalized():
    from hunter.sources import normalize
    row = {"id": "a1", "title": "Hardware Intern", "location": None, "address": None, "secondaryLocations": [{"location": "Austin, TX", "address": None}],
           "jobUrl": "https://jobs.ashbyhq.com/acme/a1", "descriptionPlain": EE_DESCRIPTION, "publishedAt": "2026-10-20T00:00:00Z"}
    job = normalize("ashby", Employer("Acme", "ashby", "acme", "https://jobs.ashbyhq.com/acme"), row, NOW)
    assert job.title == "Hardware Intern" and "Austin, TX" in job.location


def test_one_malformed_board_never_stops_the_scan(tmp_path, monkeypatch):
    store = Store(tmp_path / "hunter.sqlite3")
    good = Employer("Good", "greenhouse", "good", "https://job-boards.greenhouse.io/good")
    bad = Employer("Bad", "greenhouse", "bad", "https://job-boards.greenhouse.io/bad")

    def fetch(employer, client):
        if employer.board == "bad":
            None.get("address")  # the AttributeError a malformed posting used to raise
        return Snapshot((job("Engineering Intern"),), frozenset({"1"}), NOW, listed=1)
    monkeypatch.setattr("hunter.service.fetch_board", fetch)
    result = scan(store, [bad, good], None, Profile(), Filters(), client=object())
    assert [b["success"] for b in result["boards"]] == [False, True] and result["diagnostics"]["eligible"] == 1


def test_documented_public_api_robots_401_is_treated_as_no_policy(tmp_path):
    from hunter.http import PublicClient

    class Session:
        def get(self, url, **kwargs):
            class Response:
                status_code = 401 if url.endswith("robots.txt") else 200
                headers = {"Content-Type": "application/json"}
                def iter_content(self, size): yield b'{"jobs": []}' if not url.endswith("robots.txt") else b"denied"
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return Response()
    client = PublicClient(Store(tmp_path / "h.sqlite3"), session=Session(), sleep=lambda s: None)
    assert client.json("https://api.ashbyhq.com/posting-api/job-board/acme")[0] == {"jobs": []}
    from hunter.http import SourceError
    with pytest.raises(SourceError, match="robots.txt could not be confirmed"):
        client.json("https://api.lever.co/v0/postings/acme?mode=json")  # the same 401 on any other host is still skipped


def test_greenhouse_bulk_download_failure_falls_back_to_details(tmp_path):
    from hunter.http import SourceError
    from hunter.sources import fetch_board
    store = Store(tmp_path / "h.sqlite3")
    rows = [{"id": n, "title": f"Engineering Intern {n}"} for n in range(10)]

    class Client:
        def __init__(self):
            self.store, self.details = store, 0
        def json(self, url, force=False):
            if url.endswith("/jobs"):
                return {"jobs": rows, "meta": {"total": 10}}, NOW, False
            if "content=true" in url:
                raise SourceError("Source response exceeds the bounded download size.")
            self.details += 1
            n = url.rsplit("/", 1)[-1]
            return {"id": int(n), "title": f"Engineering Intern {n}", "content": EE_DESCRIPTION, "location": {"name": "Atlanta, GA"},
                    "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{n}"}, NOW, False
    client = Client()
    snap = fetch_board(Employer("Acme", "greenhouse", "acme", "https://job-boards.greenhouse.io/acme"), client)
    assert len(snap.jobs) == 10 and client.details == 10 and any("Bulk description download skipped" in w for w in snap.warnings)


def test_new_shipped_boards_are_added_once_without_resetting_user_choices(tmp_path):
    from hunter.service import WATCHLIST
    store = Store(tmp_path / "h.sqlite3")
    store.set("expanded_boards_v2", True)
    store.set("employers", [{"name": "My renamed Anduril", "source": "greenhouse", "board": "andurilindustries",
                             "career_url": "https://job-boards.greenhouse.io/andurilindustries", "enabled": False, "verified_at": None}])
    boards = store.employers(WATCHLIST)
    assert len(boards) > 100 and boards[0].name == "My renamed Anduril" and boards[0].enabled is False
    rows = store.get("employers")
    store.set("employers", rows[:3])  # the user later removes boards: they are not re-added
    assert len(store.employers(WATCHLIST)) == 3
