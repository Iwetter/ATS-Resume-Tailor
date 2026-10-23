"""Discovery tracking, eligibility gates, profile validation and answer rules.

All applicant data is fictional (tests/fixtures/fictional_resume.txt).
"""
from dataclasses import replace
import json
import logging
import sqlite3

import pytest

from conftest import AA_DESCRIPTION, aa_job, aa_profile
from autoapply.forms import FormField, attestation_hash, classify, missing_required, resolve
from autoapply.gates import evaluate_gates, graduation_check, start_check
from autoapply.profile import load_profile, save_profile, validate_profile, verify_profile
from autoapply.redaction import JsonFormatter, redact, register_secret
from hunter.filtering import decide
from hunter.http import PublicClient
from hunter.models import Filters, Profile
from hunter.storage import Store

NOW = "2026-10-23T12:00:00+00:00"


# ---- Phase 1: storage, events, freshness -----------------------------------------

def test_existing_database_is_migrated_without_data_loss(tmp_path):
    path = tmp_path / "hunter.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE jobs (key TEXT PRIMARY KEY, source TEXT, board TEXT, source_id TEXT, canonical_url TEXT, secondary_key TEXT,
        payload TEXT NOT NULL, discovered_at TEXT NOT NULL, checked_at TEXT, availability TEXT, status TEXT NOT NULL DEFAULT 'New',
        notes TEXT NOT NULL DEFAULT '', UNIQUE(source,board,source_id));""")
    job = aa_job()
    from dataclasses import asdict
    db.execute("INSERT INTO jobs(key,source,board,source_id,canonical_url,secondary_key,payload,discovered_at,checked_at,availability,status,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (job.key, job.source, job.board, job.source_id, job.url, job.secondary_key, json.dumps(asdict(job)), "2026-10-01T00:00:00+00:00", NOW, "open", "Saved", "my note"))
    db.commit()
    db.close()
    store = Store(path)
    row = store.job(job.key)
    assert row["status"] == "Saved" and row["notes"] == "my note" and row["discovered_at"] == "2026-10-01T00:00:00+00:00"
    assert "first_published_at" in row and "last_changed_at" in row
    Store(path)  # idempotent
    from autoapply.db import AutoStore
    AutoStore(store)
    AutoStore(store)
    assert store.job(job.key)["notes"] == "my note"


def test_updated_job_is_never_treated_as_newly_published(tmp_path):
    store = Store(tmp_path / "h.sqlite3")
    job = aa_job()
    key, fresh = store.upsert(job, NOW)
    assert fresh
    assert store.job(key)["first_published_at"] == job.published_at
    changed = replace(job, description=job.description + "\nNew detail.", updated_at="2026-10-24T09:00:00+00:00", checked_at="2026-10-24T09:05:00+00:00")
    key2, fresh2 = store.upsert(changed, "2026-10-24T09:05:00+00:00")
    assert key2 == key and not fresh2
    republished = replace(changed, published_at="2026-10-24T09:00:00+00:00")
    store.upsert(republished, "2026-10-24T10:00:00+00:00")
    row = store.job(key)
    assert row["discovered_at"] == NOW
    assert row["first_published_at"] == job.published_at  # immutable first publication
    events = [e["event"] for e in reversed(store.events(key))]
    assert events[0] == "discovered" and "updated" in events and "republished" in events
    assert events.count("discovered") == 1


def test_unverified_publication_bases_never_set_first_publication(tmp_path):
    store = Store(tmp_path / "h.sqlite3")
    lever = aa_job(job_id="lev-1", source="lever", published_at=None, publication_basis="Posting date unknown")
    ashby = aa_job(job_id="ash-1", source="ashby", board="fixture", title="Firmware Intern Summer 2027", published_at=NOW, publication_basis="Ashby publishedAt (last published)")
    assert store.job(store.upsert(lever, NOW)[0])["first_published_at"] is None
    assert store.job(store.upsert(ashby, NOW)[0])["first_published_at"] is None


def test_closure_and_reopen_events(tmp_path):
    from hunter.models import Employer
    store = Store(tmp_path / "h.sqlite3")
    job = aa_job()
    key, _ = store.upsert(job, NOW)
    store.close_absent(Employer("Fixture Co", "greenhouse", "fixtureco", job.url), frozenset(), NOW)
    store.upsert(job, NOW)
    events = [e["event"] for e in reversed(store.events(key))]
    assert events == ["discovered", "closed", "reopened"]


def test_listing_and_detail_cache_freshness_are_separate(tmp_path):
    client = PublicClient(Store(tmp_path / "h.sqlite3"), ttl=540, detail_ttl=21600, source_ttls={"myworkdayjobs.com": 3540})
    assert client.ttl_for("https://boards-api.greenhouse.io/v1/boards/acme/jobs") == 540
    assert client.ttl_for("https://boards-api.greenhouse.io/v1/boards/acme/jobs/123") == 21600
    assert client.ttl_for("https://api.smartrecruiters.com/v1/companies/Acme/postings/9") == 21600
    assert client.ttl_for("https://acme.wd5.myworkdayjobs.com/Site/siteMap.xml") == 3540
    assert client.ttl_for("https://acme.wd5.myworkdayjobs.com/Site/job/Atlanta/Intern_R1") == 21600
    assert PublicClient(Store(tmp_path / "h.sqlite3")).ttl_for("https://api.lever.co/v0/postings/x") == 3600  # legacy default preserved


def test_greenhouse_detail_refreshes_when_listing_updated(tmp_path):
    from hunter.models import Employer
    from hunter.sources import fetch_board
    store = Store(tmp_path / "h.sqlite3")
    listing = {"jobs": [{"id": 7, "title": "Hardware Engineering Intern - Summer 2027", "updated_at": "2026-10-23T10:00:00Z",
                         "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/7", "location": {"name": "Atlanta, GA"}}], "meta": {"total": 1}}
    detail_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/7"
    store.cache_set(detail_url, {**listing["jobs"][0], "updated_at": "2026-10-20T10:00:00Z", "content": "old"}, NOW)
    calls = []

    class Client:
        def __init__(self):
            self.store = store

        def json(self, url, force=False):
            calls.append((url, force))
            if url.endswith("/jobs"):
                return listing, NOW, False
            return {**listing["jobs"][0], "content": "&lt;p&gt;" + AA_DESCRIPTION + "&lt;/p&gt;"}, NOW, False

    fetch_board(Employer("Acme", "greenhouse", "acme", "https://job-boards.greenhouse.io/acme"), Client())
    assert (detail_url, True) in calls


# ---- Phase 2: eligibility gates ------------------------------------------------------

def gate_for(job, profile, master, settings, permission=None, hp=None):
    hp = hp or Profile(degree="Bachelor of Science Electrical Engineering", graduation=profile.graduation, gpa=3.68, us_authorized=profile.work_authorized, sponsorship_needed=profile.sponsorship_needed)
    decision = decide(job, hp, Filters(7, ("Spring 2027", "Summer 2027")), NOW)
    from hunter.service import score_job
    match = score_job(job, master) if decision.eligible else None
    return evaluate_gates(job, decision, match, profile, master_hash=master.source_hash,
                          permission=permission or {"prefill": True, "auto_submit": True, "terms_reviewed_at": NOW}, settings=settings)


@pytest.fixture
def verified_profile(master):
    p = aa_profile()
    return replace(p, verified_at=NOW, verified_hash=p.content_hash, verified_master=master.source_hash)


@pytest.fixture
def settings(tmp_path):
    from autoapply import config
    return config.load(db_path=tmp_path / "h.sqlite3", min_fit_review=20.0, min_fit_auto=30.0)


def test_priority_role_passes_every_gate(master, verified_profile, settings):
    gate = gate_for(aa_job(), verified_profile, master, settings)
    assert gate.prepare_ok and gate.auto_ok, gate.summary


@pytest.mark.parametrize("title", ["Mechanical Engineering Intern - Summer 2027", "Software Engineer Intern - Summer 2027",
                                   "Marketing Intern - Summer 2027", "Supply Chain Hardware Intern - Summer 2027", "Data Science Intern - Summer 2027"])
def test_unrelated_disciplines_are_excluded(title, master, verified_profile, settings):
    gate = gate_for(aa_job(title=title), verified_profile, master, settings)
    assert not gate.prepare_ok and not gate.auto_ok


def test_embedded_software_title_is_not_excluded(master, verified_profile, settings):
    gate = gate_for(aa_job(title="Embedded Firmware Software Engineering Intern - Summer 2027"), verified_profile, master, settings)
    assert gate.prepare_ok, gate.summary


@pytest.mark.parametrize("sentence,status", [
    ("Candidates must be graduating between December 2027 and June 2028.", "pass"),
    ("Candidates must be graduating between December 2028 and June 2029.", "fail"),
    ("Must have an expected graduation date of December 2028 or later.", "fail"),
    ("Expected graduation by May 2027.", "fail"),
    ("Open to the class of 2028.", "pass"),
    ("Graduation timing around 2028 will be considered case by case.", "unknown"),
    ("Graduating in 2029 preferred.", "pass"),
])
def test_graduation_compatibility(sentence, status):
    assert graduation_check(AA_DESCRIPTION + "\n" + sentence, "2028-05")[0] == status


def test_incompatible_graduation_blocks_preparation(master, verified_profile, settings):
    gate = gate_for(aa_job(description=AA_DESCRIPTION + "\nCandidates must be graduating between December 2026 and June 2027."), verified_profile, master, settings)
    assert not gate.prepare_ok


def test_spring_availability_is_never_assumed(master, verified_profile, settings):
    spring = aa_job(title="Hardware Engineering Intern - Spring 2027", description=AA_DESCRIPTION.replace("Summer 2027", "Spring 2027").replace("May to August", "January to April"))
    gate = gate_for(spring, verified_profile, master, settings)  # profile is Summer-only
    assert not gate.prepare_ok and any(c.name == "Applicant availability" and c.status == "fail" for c in gate.checks)
    issues = validate_profile(aa_profile(available_terms=("Spring 2027",)), master)
    assert any(i["field"] == "earliest_start" and i["level"] == "error" for i in issues)


def test_explicit_january_start_compared_with_verified_start(master):
    text = AA_DESCRIPTION + "\nThe internship starts January 2027."
    assert start_check(text, (), aa_profile(available_terms=("Spring 2027",), earliest_start=""))[0] == "unknown"
    assert start_check(text, (), aa_profile(earliest_start="2027-02-01"))[0] == "fail"
    assert start_check(text, (), aa_profile(earliest_start="2027-01-05"))[0] == "pass"


def test_unverified_conditions_block_auto_but_allow_review(master, verified_profile, settings):
    job = aa_job(description=AA_DESCRIPTION + "\nMust be enrolled as a full-time student and return to school after the internship.")
    gate = gate_for(job, verified_profile, master, settings)
    assert gate.prepare_ok and not gate.auto_ok
    assert any(c.name == "Hard requirements" and c.status == "unknown" for c in gate.checks)


def test_fit_thresholds_and_permissions(master, verified_profile, settings):
    from autoapply import config
    strict = config.load(db_path=settings.db_path, min_fit_review=99.5, min_fit_auto=99.8)  # above the fixture's fit
    assert not gate_for(aa_job(), verified_profile, master, strict).prepare_ok
    middle = config.load(db_path=settings.db_path, min_fit_review=20.0, min_fit_auto=99.8)
    g = gate_for(aa_job(), verified_profile, master, middle)
    assert g.prepare_ok and not g.auto_ok
    g = gate_for(aa_job(), verified_profile, master, settings, permission={"prefill": True, "auto_submit": False})
    assert g.prepare_ok and not g.auto_ok
    assert not gate_for(aa_job(), verified_profile, master, settings, permission={"prefill": False}).prepare_ok


def test_unverified_profile_blocks_everything(master, settings):
    gate = gate_for(aa_job(), aa_profile(), master, settings)
    assert not gate.prepare_ok


def test_imported_listing_never_auto(master, verified_profile, settings):
    job = aa_job(imported_at=NOW, source_status="user-confirmed", origin_url="https://www.linkedin.com/jobs/view/1")
    gate = gate_for(job, verified_profile, master, settings)
    assert not gate.auto_ok


# ---- applicant profile ----------------------------------------------------------------

def test_profile_must_match_verified_resume(master):
    assert not [i for i in validate_profile(aa_profile(), master) if i["level"] == "error"]
    bad = validate_profile(aa_profile(email="other@example.com", gpa="3.9", graduation="2027-12"), master)
    fields = {i["field"] for i in bad if i["level"] == "error"}
    assert {"email", "gpa", "graduation"} <= fields
    unknown_auth = validate_profile(aa_profile(work_authorized=None), master)
    assert any(i["field"] == "work_authorized" for i in unknown_auth)


def test_editing_profile_clears_verification(tmp_path, master):
    store = Store(tmp_path / "h.sqlite3")
    save_profile(store, aa_profile())
    assert verify_profile(store, master).is_verified(master.source_hash)
    save_profile(store, replace(load_profile(store), phone="(404) 555-0199"))
    assert not load_profile(store).is_verified(master.source_hash)


def test_master_change_invalidates_profile_verification(tmp_path, master):
    store = Store(tmp_path / "h.sqlite3")
    save_profile(store, aa_profile())
    profile = verify_profile(store, master)
    assert not profile.is_verified("a-different-master-hash")


# ---- form answers ------------------------------------------------------------------------

YES_NO = ["Yes", "No"]


@pytest.mark.parametrize("label,auth,needs,expected", [
    ("Will you now or in the future require visa sponsorship?", True, False, "No"),
    ("Will you now or in the future require visa sponsorship?", False, True, "Yes"),
    ("Are you authorized to work in the U.S. without sponsorship?", True, False, "Yes"),
    ("Are you authorized to work in the U.S. without sponsorship?", True, True, "No"),
    ("Are you legally authorized to work in the United States?", True, False, "Yes"),
    ("Do you not require sponsorship?", True, False, None),
])
def test_sponsorship_and_authorization_polarity(label, auth, needs, expected):
    r = resolve([FormField("#q", label, "select", True, YES_NO)], aa_profile(work_authorized=auth, sponsorship_needed=needs), [])[0]
    assert r.value == expected


def test_compound_citizenship_question_not_guessed():
    q = FormField("#c", "Are you a U.S. citizen or permanent resident?", "radio", True, YES_NO)
    assert resolve([q], aa_profile(us_citizen=False), [])[0].value is None
    assert resolve([q], aa_profile(us_citizen=True), [])[0].value == "Yes"


def test_demographics_consents_and_attestations():
    fields = [FormField("#g", "Gender", "select", False, ["Male", "Female", "Decline to self-identify"]),
              FormField("#v", "Veteran status *", "select", True, ["Yes", "No", "Decline"]),
              FormField("#m", "I consent to receive marketing emails", "checkbox", False),
              FormField("#a", "I certify that the information provided is true and complete. *", "checkbox", True)]
    out = {r.fid: r for r in resolve(fields, aa_profile(), [])}
    assert out["#g"].source == "blank" and not out["#g"].filled
    assert out["#v"].source == "unresolved" and out["#v"] in missing_required(out.values())
    assert not out["#m"].filled
    assert out["#a"].source == "unresolved"
    approved = resolve(fields[3:], aa_profile(), [], approved_attestations=[{"hash": attestation_hash(fields[3].label)}])[0]
    assert approved.value is True and approved.source == "attestation"


def test_unknown_question_needs_verified_answer_bank():
    q = FormField("#h", "How did you hear about this job? *", "text", True)
    assert resolve([q], aa_profile(), [])[0].source == "unresolved"
    unverified = [{"id": "a1", "question": "How did you hear about this job?", "answer": "Company website", "verified_at": None}]
    assert resolve([q], aa_profile(), unverified)[0].source == "unresolved"
    verified = [{**unverified[0], "verified_at": NOW}]
    r = resolve([q], aa_profile(), verified)[0]
    assert r.source == "answer_bank" and r.value == "Company website"


def test_model_mapping_can_only_point_at_verified_fields():
    q = FormField("#x", "Which university are you attending? *", "text", True)
    mapped = resolve([q], aa_profile(), [], model_mappings=[{"question_id": "#x", "source": "profile", "key": "school"}])[0]
    assert mapped.value == "Example Institute of Technology"
    q2 = FormField("#y", "Tell us about your favourite project *", "textarea", True)
    bogus = resolve([q2], aa_profile(), [], model_mappings=[{"question_id": "#y", "source": "profile", "key": "nonexistent"}])[0]
    assert not bogus.filled


def test_select_values_must_match_an_allowed_option():
    q = FormField("#d", "Degree *", "select", True, ["High School", "Associate"])
    r = resolve([q], aa_profile(), [])[0]
    assert not r.filled and r.source == "unresolved"
    assert classify(FormField("#f", "Resume/CV", "file")) == "resume"


# ---- logging redaction --------------------------------------------------------------------

def test_structured_logs_redact_secrets_and_contact_details():
    register_secret("Alex Example")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "Applying as Alex Example alex@example.com (404) 555-0100 key sk-ant-api03-abcdefghijklmnop", None, None)
    record.fields = {"email": "alex@example.com", "api_key": "sk-ant-xyz12345678", "state": "Ready"}
    line = JsonFormatter().format(record)
    for secret in ("alex@example.com", "555-0100", "sk-ant", "Alex Example"):
        assert secret not in line
    assert '"state": "Ready"' in line
    assert redact({"answer": "secret", "nested": [{"phone": "1"}]}) == {"answer": "[redacted]", "nested": [{"phone": "[redacted]"}]}
