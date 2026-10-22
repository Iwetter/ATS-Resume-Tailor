from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock
import json

import pytest
import requests

from ats.models import Change
from ats.rewriting import ModelConfig, apply_changes, tailor_once, validate_change
from ats.exporting import export_resume
from ats.matching import extract_requirements, parse_resume, rank_projects, select_projects
from hunter.models import Employer, Job, Profile, Filters, Snapshot
from hunter.filtering import decide
from hunter.storage import Store
from hunter.service import RewriteCache, evaluate, scan, score_job, profile_from_master
from hunter.sources import normalize

NOW = "2026-10-20T16:00:00+00:00"
FIXTURES = Path(__file__).parent / "fixtures/hunter"


@pytest.fixture
def job():
    payload = json.loads((FIXTURES / "greenhouse.json").read_text())["detail"]
    return normalize("greenhouse", Employer("Fixture Engineering", "greenhouse", "fixture", "https://example.com"), payload, NOW)


@pytest.mark.parametrize("publication,expected", [("2026-10-19T00:00:00+00:00", "recent"), ("2026-09-20T00:00:00+00:00", "older"), (None, "unknown"), ("2026-11-01T00:00:00+00:00", "future")])
def test_publication_recency_is_independent_of_updated_and_discovery(job, publication, expected):
    d = decide(replace(job, published_at=publication, updated_at=NOW, checked_at=NOW), Profile(), Filters(), NOW)
    assert d.recency == expected


@pytest.mark.parametrize("days,expected", [(1, "older"), (3, "recent"), (7, "recent")])
def test_configurable_publication_windows(job, days, expected):
    d = decide(replace(job, published_at="2026-10-18T12:00:00+00:00"), Profile(), Filters(days), NOW)
    assert d.recency == expected


@pytest.mark.parametrize("changes,reason", [
    ({"source_status": "closed"}, "Closed"),
    ({"expires_at": "2026-10-01T00:00:00+00:00"}, "expired"),
    ({"title": "Senior Hardware Engineer"}, "Not an internship"),
    ({"title": "Marketing Intern Summer 2027"}, "Unrelated"),
    ({"title": "Hardware Intern Summer 2026"}, "term"),
    ({"country": "DE", "location": "Munich, Germany"}, "United States"),
    ({"description": "Required: Minimum GPA 3.9. This Summer 2027 internship requires PCB engineering and hardware testing experience in a technical setting."}, "hard requirement"),
    ({"description": "Required: Must be pursuing a master's degree in Electrical Engineering. This Summer 2027 internship requires PCB engineering and hardware testing experience."}, "hard requirement"),
    ({"description": "Required: Must have completed a bachelor's degree. This Summer 2027 hardware internship involves circuit board design and testing in the laboratory."}, "hard requirement"),
])
def test_ineligible_or_unrelated_jobs_are_filtered(job, changes, reason):
    d = decide(replace(job, **changes), Profile(), Filters(), NOW)
    assert not d.eligible and any(reason in x for x in d.reasons)


def test_profile_facts_vs_conditions_requiring_confirmation(job):
    description = """Required:
U.S. citizenship is required.
Must be authorized to work in the United States.
We cannot provide visa sponsorship.
Must return to school after Summer 2027.
Must have active security clearance.
Junior academic standing is required.
Available for a six month co-op rotation.
Minimum GPA 3.0.
Benefits package available at no cost.
"""
    d = decide(replace(job, description=description), Profile(us_citizen=True, us_authorized=True, sponsorship_needed=False), Filters(), NOW)
    assert d.eligible
    assert any(f.status == "met" and "citizenship" in f.requirement for f in d.flags)
    assert any(f.status == "met" and "sponsorship" in f.requirement for f in d.flags)
    assert all(f.status == "unknown" for f in d.flags if "clearance" in f.requirement or "Junior" in f.requirement or "school" in f.requirement)
    assert not any("Benefits package" in f.requirement for f in d.flags)


def test_unknown_term_and_location_are_flags_not_invented(job):
    d = decide(replace(job, title="Hardware Intern", description="Required: PCB design and hardware testing experience in an engineering lab using oscilloscopes and other tools.", location="Remote", country=""), Profile(), Filters(), NOW)
    assert d.eligible and not d.terms
    assert any(f.requirement == "Internship term" for f in d.flags)
    assert any(f.requirement == "U.S. location" for f in d.flags)


def test_real_master_education_overrides_default_profile():
    r = parse_resume("Someone Else\nEDUCATION\nBachelor of Science in Computer Engineering | Expected December 2029 | GPA: 3.45")
    p = profile_from_master(Profile(), r)
    assert p.graduation == "2029-12" and p.gpa == 3.45 and "Computer Engineering" in p.degree


def test_dedup_status_first_discovery_and_closure_persistence(job, tmp_path):
    store = Store(tmp_path / "hunter.sqlite3")
    key, new = store.upsert(job, "2026-10-19T10:00:00+00:00")
    store.status(key, "Applied", "Submitted on official site manually")
    key2, new2 = store.upsert(replace(job, url=job.url+"?utm_source=test"), NOW)
    assert key == key2 and not new2
    assert store.job(key)["discovered_at"] == "2026-10-19T10:00:00+00:00"
    assert Store(store.path).job(key)["status"] == "Applied"
    # Cross-provider title/company/location duplicates collapse; distinct IDs
    # on the same provider remain distinct requisitions.
    alias_key, fresh = store.upsert(replace(job, source="lever", board="other", source_id="other", url="https://jobs.lever.co/other/other"), NOW)
    assert alias_key == key and not fresh
    _, separate = store.upsert(replace(job, source_id="different-requisition", url=job.url.replace("101", "102")), NOW)
    assert separate
    store.close_absent(Employer(job.company, job.source, job.board, job.url), frozenset(), NOW)
    assert store.job(key)["availability"] == "open"  # alias still present
    store.close_absent(Employer(job.company, "lever", "other", job.url), frozenset(), NOW)
    assert store.job(key)["availability"] == "closed"
    assert store.job(key)["status"] == "Applied"


def test_matching_ranking_cache_and_zero_paid_calls(job, master, tmp_path, monkeypatch):
    store = Store(tmp_path / "hunter.sqlite3")
    paid = Mock(side_effect=AssertionError("No LLM permitted"))
    monkeypatch.setattr(requests, "post", paid)
    first = score_job(job, master, store)
    assert first == score_job(job, master, store)
    assert 0 < first["fit"] <= 100 and "PCB" in first["matched_skills"]
    assert first["strongest_evidence"] and paid.call_count == 0
    weaker = parse_resume("Alex Example\nEDUCATION\nBachelor of Science\nSKILLS\nPCB, KiCad\nPROJECTS\nExample Project\nâ€¢ Built an unrelated metal box.")
    assert score_job(job, weaker)["fit"] < first["fit"]
    repeated = replace(job, description=job.description + "\nPCB PCB PCB PCB KiCad KiCad")
    assert score_job(repeated, master)["fit"] == first["fit"]
    # Cache eliminates repeated invocation of Phase 1 grading.
    monkeypatch.setattr("hunter.service.analyze", Mock(side_effect=AssertionError("Should reuse cache")))
    assert score_job(job, master, store) == first


def test_scan_new_discovery_and_failures_do_not_close(job, master, tmp_path, monkeypatch):
    store = Store(tmp_path / "hunter.sqlite3")
    employer = Employer(job.company, job.source, job.board, job.url)
    snapshot = Snapshot((job,), frozenset({job.source_id}), NOW)
    monkeypatch.setattr("hunter.service.fetch_board", lambda e, c: snapshot)
    first = scan(store, [employer], master, client=Mock(requests=0, cache_hits=0))
    second = scan(store, [employer], master, client=Mock(requests=0, cache_hits=0))
    assert len(first["new_ids"]) == 1 and not second["new_ids"]
    def failure(e, c):
        from hunter.http import SourceError
        raise SourceError("Unavailable")
    monkeypatch.setattr("hunter.service.fetch_board", failure)
    third = scan(store, [employer], master, client=Mock(requests=0, cache_hits=0))
    assert third["boards"][0]["success"] is False
    assert store.job(job.key)["availability"] == "open"


def test_job_specific_approved_tailoring_and_artifact_persistence(job, master, tmp_path):
    store = Store(tmp_path / "hunter.sqlite3")
    key, _ = store.upsert(job, NOW)
    store.save_master(master)
    requirements = extract_requirements(job.description)
    chosen = [g for g, _, _ in rank_projects(master, requirements)[:3]]
    draft = select_projects(master, chosen)
    source = next(x for x in draft.items if "four-layer" in x.text and x.kind == "bullet")
    change = Change((source.id,), source.text, "Designed and built a four-layer wearable PCB with 3.3 V power rails in KiCad.", "Clarity")
    assert validate_change(change, master).status == "review"
    reviewed = apply_changes(master, draft, (change,), {source.id})
    bundle = export_resume(reviewed)
    assert bundle.pages == 1 and b"PK" == bundle.docx[:2]
    before, after = score_job(job, master), score_job(job, reviewed)
    artifact = store.save_artifact(key, bundle, {"approved": [asdict(change)]}, {"before": before["fit"], "after": after["fit"]})
    assert store.save_artifact(key, bundle, {"approved": [asdict(change)]}, {"before": before["fit"], "after": after["fit"]}) == artifact
    files = Store(store.path).artifacts()
    assert len(files) == 1 and Path(files[0]["pdf"]).read_bytes() == bundle.pdf
    assert store.master() == master
    unsupported = replace(change, proposed_text=change.proposed_text+" using STM32.")
    assert validate_change(unsupported, master).status == "rejected"


def test_persistent_phase1_rewrite_cache_survives_new_instance(job, master, tmp_path):
    store = Store(tmp_path / "hunter.sqlite3")
    eid = next(x.id for x in master.items if x.kind == "bullet")
    transport = Mock(return_value={"payload": {"changes": []}, "usage": {"total_tokens": 20}})
    args = (master, job.description, (), [eid], ModelConfig("configured-model"), "fake-key")
    a = tailor_once(*args, intentional=True, enabled=True, cache=RewriteCache(store), transport=transport)
    b = tailor_once(*args, intentional=True, enabled=True, cache=RewriteCache(Store(store.path)), transport=transport)
    assert not a["cache_hit"] and b["cache_hit"] and transport.call_count == 1
    assert "fake-key" not in store.path.read_bytes().decode("latin1")


def test_public_example_does_not_assume_work_authorization():
    profile = Profile()
    assert profile.us_citizen is None
    assert profile.us_authorized is None
    assert profile.sponsorship_needed is None
