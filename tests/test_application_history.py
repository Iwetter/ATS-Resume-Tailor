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
    assert history_matches(path, "SwRI", "Embedded Systems Intern") == (2, True)
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


def queued(aa, job):
    key = aa.add_job(job)
    app = aa.auto.enqueue(key, job, "fixture:" + job.board, dedupe_key(job), "REVIEW", {})
    token = aa.auto.acquire(app, (S.QUEUED,))
    aa.auto.update(app, state=S.APPROVED)
    return app, token


def test_company_claim_is_atomic_across_connections(aa):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from autoapply.db import AutoStore
    from hunter.storage import Store
    apps = [queued(aa, aa_job(job_id=str(i), company=c, title=f"Hardware Intern {i}"))
            for i, c in enumerate(("Marvell", "Marvell Technology, Inc."))]
    workers = [AutoStore(Store(aa.store.path)) for _ in apps]
    barrier = Barrier(2)
    def claim(pair):
        store, (app, token) = pair
        barrier.wait()
        return store.begin_submission(app, token, aa.settings)
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(claim, zip(workers, apps))) == [False, True]
    assert len(aa.auto.applications([S.SUBMITTING])) == 1


def test_active_lease_survives_other_worker_recovery(aa):
    app, token = queued(aa, aa_job())
    assert aa.auto.begin_submission(app, token, aa.settings)
    assert aa.auto.recover() == []
    assert aa.auto.application(app)['lock_token'] == token
    aa.auto.update(app, lock_expires='2000-01-01T00:00:00+00:00')
    aa.auto.recover()
    assert aa.auto.application(app)['state'] == S.UNKNOWN
    assert not aa.auto.begin_submission(app, token, aa.settings)


def test_expired_lease_cannot_claim(aa):
    app, token = queued(aa, aa_job())
    aa.auto.update(app, lock_expires='2000-01-01T00:00:00+00:00')
    assert not aa.auto.begin_submission(app, token, aa.settings)


def test_manual_history_survives_status_changes(aa):
    key = aa.add_job(aa_job(job_id='old', company='SwRI'))
    aa.store.status(key, 'Applied')
    aa.store.status(key, 'Saved')
    assert history_matches(aa.store.path, 'Southwest Research Institute')[0] == 1
    app, token = queued(aa, aa_job(job_id='new', company='Southwest Research Institute', title='Firmware Intern'))
    assert not aa.auto.begin_submission(app, token, aa.settings)


def test_import_survives_csv_removal_and_matches_requisition(aa):
    path = aa.store.path.parent / 'application_history.csv'
    path.write_text('company,title,status,source_id\nSwRI,Old title,unknown,R123\n', encoding='utf-8')
    assert history_matches(aa.store.path, 'SwRI', 'New title', 'R123') == (1, True)
    path.unlink()
    assert history_matches(aa.store.path, 'SwRI', 'New title', 'R123') == (1, True)


def test_legacy_applied_without_submit_timestamp_counts(aa):
    from datetime import datetime, timezone
    app, _ = queued(aa, aa_job())
    aa.auto.update(app, state=S.APPLIED)
    assert aa.auto.submissions_since(datetime(1970, 1, 1, tzinfo=timezone.utc), company='Fixture Co') == 1


def test_distinct_requisitions_with_same_title_count_separately(tmp_path):
    (tmp_path / 'application_history.csv').write_text('company,title,status,source_id\nAcme,Hardware Intern,applied,1\nAcme,Hardware Intern,applied,2\n')
    assert history_matches(tmp_path / 'h.sqlite3', 'Acme')[0] == 2


def test_same_requisition_with_changed_title_and_board_is_blocked(aa):
    settings = replace(aa.settings, max_per_company_total=5, max_per_company_per_day=5)
    first, token = queued(aa, aa_job(company='SwRI'))
    assert aa.auto.begin_submission(first, token, settings)
    second, token = queued(aa, aa_job(company='Southwest Research Institute', source='lever', board='other', title='Renamed Firmware Intern'))
    assert not aa.auto.begin_submission(second, token, settings)


def test_prior_history_blocks_before_tailoring(aa, monkeypatch):
    aa.permit()
    aa.switch_on()
    (aa.store.path.parent / 'application_history.csv').write_text('company,title,status\nFixture Co,Other role,applied\n')
    key = aa.add_job()
    aa.engine.evaluate(key)
    monkeypatch.setattr(aa.engine, '_plan', lambda *a: pytest.fail('Prior employer must not consume tailoring work'))
    aa.engine.process()
    assert aa.auto.application(job_key=key)['state'] == S.BLOCKED
