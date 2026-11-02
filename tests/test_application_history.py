"""Prior application history and employer deduplication use fictional data only."""
from dataclasses import replace

import pytest

from conftest import aa_job
from autoapply.engine import dedupe_key
from autoapply.history import company_key, history_matches, read_history
from autoapply import db as S


def test_company_aliases_and_title_dedupe():
    assert company_key("Southwest Research Institute, Inc.") == company_key("SwRI")
    assert company_key("Marvell Technology, Inc.") == company_key("Marvell")
    assert dedupe_key(aa_job(company="Marvell Technology", title="Hardware Engineering Intern - Summer 2027")) == dedupe_key(
        aa_job(company="Marvell", title="Hardware Engineering Intern"))


def test_history_counts_distinct_roles_and_uncertain_same_role(tmp_path):
    path = tmp_path / "data" / "hunter.sqlite3"
    path.parent.mkdir()
    (path.parent / "application_history.csv").write_text(
        "company,title,status\n"
        "SwRI,Robotics Student Engineer,Applied\n"
        "Southwest Research Institute,Robotics Student Engineer,Submitted\n"
        "Southwest Research Institute,Embedded Systems Intern,unknown\n"
        "Google,Hardware Engineering Intern,applied\n", encoding="utf-8")
    assert history_matches(path, "SwRI", "Robotics Student Engineer") == (2, True)
    assert history_matches(path, "SwRI", "Embedded Systems Intern") == (1, True)
    assert history_matches(path, "SwRI", "Unrelated Role") == (2, False)
    assert history_matches(path, "Marvell", "Robotics Student Engineer") == (0, False)


def test_bad_history_fails_closed(tmp_path):
    path = tmp_path / "hunter.sqlite3"
    (tmp_path / "application_history.csv").write_text("company,status\nGoogle,maybe\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_history(path)


def test_prior_company_alias_counts_actual_submissions(aa):
    job = aa_job()
    key = aa.add_job(job)
    appid = aa.auto.enqueue(key, job, "greenhouse:fixtureco", dedupe_key(job), "REVIEW", {})
    aa.auto.update(appid, state=S.APPLIED, submit_started_at="2026-10-23T12:00:00+00:00")
    from datetime import datetime, timezone
    since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert aa.auto.submissions_since(since, company="Fixture Co, Inc.") == 1
    assert aa.auto.prior_company_application("Fixture Co LLC", "other", "Hardware Engineering Intern - Summer 2027")
