"""Offline protocol-shaped fixtures, never represented as actual openings."""
from dataclasses import asdict, replace
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from streamlit.testing.v1 import AppTest

from hunter.boards import import_listing, public_link, search_links
from hunter.http import PublicClient, SourceError
from hunter.models import Employer, Job, Filters, Profile
from hunter.service import evaluate
from hunter.sources import employer_from_url, fetch_board
from hunter.storage import Store

NOW = "2026-10-20T16:00:00+00:00"
DESCRIPTION = "Required: PCB design and electrical engineering experience. Design hardware and test embedded systems with oscilloscopes. Summer 2027 internship for undergraduate engineering students."
WD = "https://fixture.wd5.myworkdayjobs.com/External"
PAGE = WD + "/job/US-CA/Hardware-Engineering-Intern---Summer-2027_JR123"


class Client:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def text(self, url):
        self.calls.append(url)
        return self.responses[url], NOW, False


def html_posting(**updates):
    row = {"@type": "JobPosting", "title": "Hardware Engineering Intern - Summer 2027", "description": DESCRIPTION,
           "jobLocation": {"address": {"addressLocality": "San Jose", "addressRegion": "CA", "addressCountry": "US"}},
           "datePosted": "2026-10-19", "employmentType": ["INTERN", "FULL_TIME"]}
    row.update(updates)
    return '<script type="application/ld+json">' + json.dumps({"@graph": [row]}) + '</script>'


def listing(store, **updates):
    args = dict(origin_url="https://www.linkedin.com/jobs/view/hardware-intern-12345?trackingId=abc", company="Fixture Engineering",
                title="Hardware Engineering Intern - Summer 2027", location="San Jose, CA, United States", description=DESCRIPTION)
    args.update(updates)
    return import_listing(store, **args)


@pytest.mark.parametrize("url,source", [
    (WD, "workday"), ("https://fixture.wd5.myworkdayjobs.com/en-US/External/job/location/title", "workday"),
    ("https://fixture.recruitee.com/o/title", "recruitee"),
    ("https://fixture.jobs.personio.de", "personio"), ("https://fixture.jobs.personio.com/job/123", "personio"),
])
def test_new_employer_url_detection(url, source):
    board = employer_from_url("Fixture", url)
    assert board.source == source and "/job/" not in board.career_url and "/o/" not in board.career_url


@pytest.mark.parametrize("url", ["https://fixture.wd5.myworkdayjobs.com", "https://fixture.wd5.myworkdayjobs.com/en-US", "https://fixture.jobs.personio.de.evil.test", "https://fixture.recruitee.com:444"])
def test_invalid_or_lookalike_boards_rejected(url):
    with pytest.raises(ValueError):
        employer_from_url("Fixture", url)


def test_workday_public_sitemap_and_structured_page_partial_coverage():
    evil = "https://evil.test/job/Hardware-Intern"
    xml = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{PAGE}</loc><lastmod>2026-10-20</lastmod></url><url><loc>{evil}</loc></url></urlset>'
    client = Client({WD + "/siteMap.xml": xml, PAGE: html_posting()})
    snapshot = fetch_board(employer_from_url("Fixture", WD), client)
    assert len(snapshot.jobs) == 1 and not snapshot.complete
    job = snapshot.jobs[0]
    assert job.country == "US" and job.source_status == "open" and job.description == DESCRIPTION
    assert job.published_at.startswith("2026-10-19") and "datePosted" in job.publication_basis
    assert evil not in client.calls and any("outside" in w for w in snapshot.warnings)


def test_workday_lastmod_is_never_a_posted_date_and_detail_failure_is_unknown():
    xml = f'<urlset><url><loc>{PAGE}</loc><lastmod>2026-10-20</lastmod></url></urlset>'
    client = Client({WD + "/siteMap.xml": xml, PAGE: html_posting(datePosted=None)})
    snapshot = fetch_board(employer_from_url("Fixture", WD), client)
    assert snapshot.jobs[0].published_at is None
    client.responses[PAGE] = '<html>Sign in</html>'
    snapshot = fetch_board(employer_from_url("Fixture", WD), client)
    assert not snapshot.jobs and snapshot.warnings and not snapshot.complete


def test_workday_sitemap_index_and_detail_limit():
    sub = WD + "/part.xml"
    client = Client({WD + "/siteMap.xml": f'<sitemapindex><sitemap><loc>{sub}</loc></sitemap></sitemapindex>', sub: f'<urlset><url><loc>{PAGE}</loc></url></urlset>'})
    snapshot = fetch_board(employer_from_url("Fixture", WD), client, max_details=0)
    assert PAGE in snapshot.all_ids and not snapshot.jobs and not snapshot.complete
    assert any("detail limit" in w for w in snapshot.warnings)


def test_personio_xml_preserves_sections_and_does_not_use_creation_date():
    url = "https://fixture.jobs.personio.de"
    xml = f'<workzag-jobs><position><id>123</id><name>Electrical Engineering Intern Summer 2027</name><office>Atlanta, GA, United States</office><createdAt>2026-10-19</createdAt><jobDescriptions><jobDescription><name>Requirements</name><value><![CDATA[<p>{DESCRIPTION}</p>]]></value></jobDescription></jobDescriptions></position><position><id>124</id><name>Marketing Manager</name></position></workzag-jobs>'
    snapshot = fetch_board(employer_from_url("Fixture", url), Client({url + "/xml?language=en": xml}))
    assert snapshot.complete and snapshot.all_ids == {"123", "124"}
    assert snapshot.jobs[0].description == "Requirements\n" + DESCRIPTION
    assert snapshot.jobs[0].published_at is None and snapshot.jobs[0].url == url + "/job/123"


def test_recruitee_xml_only_published_jobs_and_no_invented_dates():
    url = "https://fixture.recruitee.com"
    xml = f'<hash><offers><offer><id>123</id><status>published</status><slug>pcb-intern</slug><title>PCB Hardware Intern Summer 2027</title><description>{DESCRIPTION}</description><requirements>Python required.</requirements><city>Atlanta</city><country-code>US</country-code><created-at>2026-10-19</created-at></offer><offer><id>124</id><title>Hardware Intern</title><status>closed</status></offer></offers></hash>'
    snapshot = fetch_board(employer_from_url("Fixture", url), Client({url + "/api/offers.xml": xml}))
    assert snapshot.complete and snapshot.all_ids == {"123"}
    assert snapshot.jobs[0].country == "US" and snapshot.jobs[0].published_at is None
    assert "Python required" in snapshot.jobs[0].description


@pytest.mark.parametrize("xml", ['<!DOCTYPE a [<!ENTITY x "foo">]><workzag-jobs/>', '<html>Access denied</html>', '<workzag-jobs><position>'])
def test_bad_xml_is_failure_not_empty_complete_success(xml):
    url = "https://fixture.jobs.personio.com"
    with pytest.raises(SourceError):
        fetch_board(employer_from_url("Fixture", url), Client({url + "/xml?language=en": xml}))


def test_plain_text_cache_and_new_hosts_keep_robots_and_block_hidden_apis(tmp_path):
    class Response:
        status_code, headers = 200, {}
        def __init__(self, raw): self.raw = raw
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, n): yield self.raw
    class Session:
        def __init__(self): self.calls = []
        def get(self, url, **kwargs):
            self.calls.append(url)
            assert not kwargs["allow_redirects"]
            if not url.endswith("robots.txt"):
                assert kwargs["headers"]["Accept"].startswith("text/html")
            return Response(b"User-agent: *\nDisallow: /External/private/\n" if url.endswith("robots.txt") else b"<urlset/>")
    session = Session()
    client = PublicClient(Store(tmp_path / "db"), session=session, interval=0)
    a, b = client.text(WD + "/siteMap.xml"), client.text(WD + "/siteMap.xml")
    assert a[1] == b[1] and b[2] and len(session.calls) == 2
    for url in (WD + "/private/a", "https://fixture.wd5.myworkdayjobs.com/wday/cxs/jobs", "https://fixture.recruitee.com.evil.test/api/offers.xml", "https://fixture.recruitee.com:444/api/offers.xml"):
        with pytest.raises(SourceError): client.text(url)
    assert len(session.calls) == 2


def test_import_deduplicates_board_tracking_and_keeps_status_and_first_discovery(tmp_path):
    store = Store(tmp_path / "db")
    key, fresh = listing(store)
    discovered = store.job(key)["discovered_at"]
    store.status(key, "Applied", "Test history")
    key2, fresh2 = listing(store, origin_url="https://www.linkedin.com/jobs/view/12345?trackingId=other")
    assert fresh and not fresh2 and key2 == key and len(store.jobs()) == 1
    assert store.job(key)["status"] == "Applied" and store.job(key)["notes"] == "Test history"
    assert store.job(key)["discovered_at"] == discovered and len(store.imports()) == 1
    job = json.loads(store.job(key)["payload"])
    assert job["published_at"] is None and job["checked_at"] is None and job["source_status"] == "unknown"
    assert job["origin_url"] == "https://www.linkedin.com/jobs/view/12345"


def test_indeed_import_user_reported_date_is_not_automatically_confirmed_scope(tmp_path):
    store = Store(tmp_path / "db")
    key, _ = listing(store, origin_url="https://www.indeed.com/viewjob?jk=abc123&from=search", published_date="2026-10-19",
                     confirmed=True, application_url="https://employer.example/jobs/123")
    row = evaluate(store, None, Profile(), Filters(), NOW)[0]
    assert row["job"].source == "indeed" and row["job"].source_status == "user-confirmed"
    assert row["decision"].recency == "recent" and not row["confirmed_scope"]
    assert "not independently verified" in row["job"].publication_basis


def test_automatic_source_promotes_import_without_losing_provenance_or_history(tmp_path):
    store = Store(tmp_path / "db")
    key, _ = listing(store, application_url="https://employer.example/jobs/123")
    store.status(key, "Saved", "Keep my notes")
    official = Job("greenhouse", "fixture", "123", "Fixture Engineering", "Hardware Engineering Intern - Summer 2027", "San Jose, CA, United States", "https://employer.example/jobs/123", DESCRIPTION, checked_at=NOW)
    assert store.upsert(official, NOW) == (key, False)
    assert len(store.jobs()) == 1 and store.imports(key)
    assert store.job(key)["status"] == "Saved" and store.job(key)["notes"] == "Keep my notes"
    assert json.loads(store.job(key)["payload"])["source"] == "greenhouse"
    assert json.loads(store.job(key)["payload"])["imported_at"] is None
    listing(store, application_url="https://employer.example/jobs/123")
    assert store.job(key)["availability"] == "open"


@pytest.mark.parametrize("updates", [{"description": "Too short"}, {"origin_url": "javascript:alert(1)"}, {"origin_url": "http://127.0.0.1/job"}, {"origin_url": "https://localhost.local/job"}, {"published_date": "yesterday"}, {"published_date": "2099-01-01"}, {"confirmed": True}])
def test_invalid_import_does_not_write_partial_records(tmp_path, updates):
    store = Store(tmp_path / "db")
    with pytest.raises(ValueError): listing(store, **updates)
    assert not store.jobs() and not store.imports()


def test_user_confirmation_requires_employer_url_and_never_becomes_automatic(tmp_path):
    store = Store(tmp_path / "db")
    key, _ = listing(store)
    with pytest.raises(ValueError): store.confirm_import(key, "https://www.linkedin.com/jobs/view/12345", True)
    store.confirm_import(key, "https://employer.example/jobs/123", True)
    assert store.job(key)["availability"] == "user-confirmed"
    assert not evaluate(store, None, Profile(), Filters(), NOW)[0]["confirmed_scope"]
    store.confirm_import(key, "https://employer.example/jobs/123", False)
    assert store.job(key)["availability"] == "unknown"


def test_reimport_updated_employer_url_updates_canonical_link(tmp_path):
    store = Store(tmp_path / "db")
    key, _ = listing(store)
    assert listing(store, application_url="https://employer.example/jobs/123") == (key, False)
    assert store.job(key)["canonical_url"] == "https://employer.example/jobs/123"
    assert len(store.imports(key)) == 1


def test_recruitee_generic_error_object_cannot_close_saved_jobs():
    url = "https://fixture.recruitee.com"
    with pytest.raises(SourceError, match="missing its offers collection"):
        fetch_board(employer_from_url("Fixture", url), Client({url + "/api/offers.xml": "<hash><error>Unavailable</error></hash>"}))


def test_saved_watchlist_preferences_survive_new_provider_migration(tmp_path):
    store = Store(tmp_path / "db")
    old = Employer("My renamed employer", "greenhouse", "fixture", "https://job-boards.greenhouse.io/fixture", False)
    store.set("employers", [asdict(old)])
    defaults = tmp_path / "employers.json"
    defaults.write_text(json.dumps([asdict(old), asdict(employer_from_url("Fixture WD", WD))]))
    rows = store.employers(defaults)
    assert len(rows) == 2 and rows[0] == old
    assert store.employers(defaults) == rows


def test_search_links_encode_user_input_and_target_named_boards():
    links = search_links("PCB & firmware internship", "Atlanta, GA", 3)
    assert len(links) == 9 and "f_TPR=r259200" in links["LinkedIn"] and "fromage=3" in links["Indeed"]
    assert "PCB+%26+firmware" in links["LinkedIn"] and "site%3Amyworkdayjobs.com" in links["Workday employers"]


def test_imported_listing_ui_confirmation_scoring_and_phase1_handoff(tmp_path, master, monkeypatch):
    store = Store(tmp_path / "db")
    store.save_master(master)
    network = Mock(side_effect=AssertionError("No automatic request during import or matching"))
    monkeypatch.setattr(requests.sessions.Session, "get", network)
    monkeypatch.setattr(requests, "post", network)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=40)
    app.session_state["hunter_db_path"] = str(store.path)
    app.run().radio(key="workspace_page").set_value("Internship Hunter").run()
    fields = {"Listing URL": "https://www.indeed.com/viewjob?jk=fixture", "Listing employer": "Fixture Engineering",
              "Exact listing title": "Hardware Engineering Intern Summer 2027", "Listing location": "Atlanta, GA, United States"}
    for label, text in fields.items(): next(x for x in app.text_input if x.label == label).set_value(text)
    next(x for x in app.text_area if x.label == "Paste complete job description").set_value(DESCRIPTION)
    next(x for x in app.button if x.label == "Import listing into Hunter").click().run()
    assert not app.exception and app.radio(key="hunter_view").value == "Imported jobs"
    assert len(store.jobs()) == 1 and any(m.label == "Original resume fit estimate" for m in app.metric)
    assert next(b for b in app.button if b.label == "Tailor Resume for this job").disabled
    next(x for x in app.text_input if x.label == "Employer application page for this import").set_value("https://employer.example/jobs/123")
    next(x for x in app.checkbox if x.label == "I verified this employer page is accepting applications").set_value(True)
    next(x for x in app.button if x.label == "Save availability confirmation").click().run()
    assert not app.exception
    next(x for x in app.button if x.label == "Tailor Resume for this job").click().run()
    assert not app.exception and app.radio(key="workspace_page").value == "Resume Grader & Tailor"
    assert app.session_state["job_input"] == DESCRIPTION and network.call_count == 0
