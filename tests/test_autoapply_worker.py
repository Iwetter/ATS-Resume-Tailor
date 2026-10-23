"""Worker scheduling, immediate evaluation, restart recovery and retries."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from conftest import aa_job
from autoapply import db as S
from autoapply.worker import Worker, health
from hunter.http import RateLimited, SourceError
from hunter.models import Employer, Snapshot, utcnow


def employers(store, *rows):
    store.set("expanded_boards_v2", True)  # skip Hunter's one-time shipped-board merge
    store.set("employers", [dict(name=n, source=s, board=b, career_url=u, enabled=True, verified_at=None) for n, s, b, u in rows])


@pytest.fixture
def worker(aa, monkeypatch):
    employers(aa.store, ("Fixture Co", "greenhouse", "fixtureco", "https://job-boards.greenhouse.io/fixtureco"),
              ("Slow Workday", "workday", "slow-wd5-Site", "https://slow.wd5.myworkdayjobs.com/Site"))
    w = Worker(aa.settings, aa.store, aa.engine, client_factory=lambda: Mock(requests=0, cache_hits=0))
    return w


def snapshot(*jobs):
    return Snapshot(tuple(jobs), frozenset(j.source_id for j in jobs), utcnow())


def test_new_job_is_evaluated_immediately_and_queued(aa, worker, monkeypatch):
    aa.permit()
    aa.switch_on()
    monkeypatch.setattr("hunter.service.fetch_board", lambda e, c: snapshot(aa_job()) if e.source == "greenhouse" else snapshot())
    monkeypatch.setattr(aa.engine, "process", lambda: [])
    result, _ = worker.run_once()
    key = result["new_ids"][0]
    assert aa.auto.decisions(key)[0]["trigger"] == "new_job"
    assert aa.auto.application(job_key=key)["state"] == S.QUEUED
    # A rescan of an unchanged job records no duplicate decision row.
    for status in aa.auto.board_status():
        aa.auto.board_update(status["employer_key"], status["name"], next_due=None)
    worker.run_once()
    assert len(aa.auto.decisions(key)) == 1
    assert aa.store.get("worker_heartbeat")["state"] == "idle"
    assert health(aa.store, aa.settings)[0]


def test_every_scanned_job_decision_persisted_even_when_off(aa, worker, monkeypatch):
    unrelated = aa_job(job_id="102", title="Marketing Intern - Summer 2027")
    monkeypatch.setattr("hunter.service.fetch_board", lambda e, c: snapshot(aa_job(), unrelated) if e.source == "greenhouse" else snapshot())
    result, _ = worker.run_once()
    assert len(result["new_ids"]) == 2
    for key in result["new_ids"]:
        assert aa.auto.decisions(key)
        assert aa.auto.application(job_key=key) is None  # master switch off


def test_per_board_cadence_and_backoff(aa, worker, monkeypatch):
    calls = []

    def fetch(employer, client):
        calls.append(employer.source)
        if employer.source == "workday":
            raise RateLimited("HTTP 429", 120)
        return snapshot()
    monkeypatch.setattr("hunter.service.fetch_board", fetch)
    monkeypatch.setattr(aa.engine, "process", lambda: [])
    worker.run_once()
    status = {s["employer_key"]: s for s in aa.auto.board_status()}
    now = datetime.now(timezone.utc)
    gh = datetime.fromisoformat(status["greenhouse:fixtureco"]["next_due"]) - now
    wd = datetime.fromisoformat(status["workday:slow-wd5-Site"]["next_due"]) - now
    assert timedelta(minutes=10) < gh < timedelta(minutes=14)
    assert wd >= timedelta(minutes=29) and status["workday:slow-wd5-Site"]["consecutive_failures"] == 1
    worker.run_once()  # nothing due yet: no new requests
    assert calls == ["greenhouse", "workday"], calls


def test_scan_interval_has_a_safe_floor(monkeypatch):
    from autoapply import config
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "30")
    assert config.load().scan_interval == 300
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "900")
    assert config.load().scan_interval == 900


def test_restart_recovery(aa, worker):
    aa.switch_on()
    jobs = [aa.add_job(aa_job(job_id=str(n), title=f"Hardware Engineering Intern {n} - Summer 2027")) for n in (1, 2)]
    ids = [aa.auto.enqueue(k, aa_job(job_id=str(n), title=f"Hardware Engineering Intern {n} - Summer 2027"), "greenhouse:fixtureco", f"d{n}", "REVIEW", {}) for n, k in zip((1, 2), jobs)]
    aa.auto.update(ids[0], state=S.SUBMITTING, submit_started_at=utcnow(), lock_token="dead", lock_expires=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    aa.auto.update(ids[1], state=S.PREPARING, lock_token="dead", lock_expires=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    aa.store.set("scan_lock", (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    changed = worker.recover()
    assert aa.auto.application(ids[0])["state"] == S.UNKNOWN  # never re-clicked
    assert aa.auto.application(ids[1])["state"] == S.QUEUED   # safe to redo
    assert aa.auto.application(ids[1])["lock_token"] is None
    assert aa.store.get("scan_lock") is None and changed
    assert any(e["action"] == "recovered_after_restart" for e in aa.auto.audit_log())


def test_preparation_failures_retry_with_bounded_backoff(aa, monkeypatch):
    aa.permit()
    aa.switch_on()
    key = aa.add_job()
    aa.engine.evaluate(key)
    app_id = aa.auto.application(job_key=key)["id"]

    class Broken:
        router = None

        def page(self):
            raise ConnectionError("browser unavailable")
    aa.engine.browser = Broken()
    aa.engine.process()
    app = aa.auto.application(app_id)
    assert app["state"] == S.FAILED and app["attempts"] == 1 and "browser unavailable" in app["last_error"]
    aa.engine.process()
    assert aa.auto.application(app_id)["state"] == S.FAILED  # backoff not elapsed
    for attempt in range(2, 5):
        with aa.auto.connect() as db:
            db.execute("UPDATE applications SET updated_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), app_id))
        aa.engine.process()
    app = aa.auto.application(app_id)
    assert app["state"] == S.FAILED and app["attempts"] == 3  # bounded


def test_error_after_submission_claim_is_unknown(aa, monkeypatch):
    aa.permit()
    aa.switch_on()
    key = aa.add_job()
    aa.engine.evaluate(key)
    app_id = aa.auto.application(job_key=key)["id"]
    token = aa.auto.acquire(app_id, (S.QUEUED,))
    aa.auto.update(app_id, state=S.PREPARING)
    assert aa.auto.begin_submission(app_id, token)
    assert aa.engine._failure(app_id, RuntimeError("crash mid-click"), claimed=True) == S.UNKNOWN
    assert not aa.auto.begin_submission(app_id, token)


def test_health_check_detects_stale_or_stopped_worker(aa):
    assert not health(aa.store, aa.settings)[0]
    aa.store.set("worker_heartbeat", {"at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), "state": "idle"})
    assert not health(aa.store, aa.settings)[0]
    aa.store.set("worker_heartbeat", {"at": utcnow(), "state": "stopped"})
    assert not health(aa.store, aa.settings)[0]
    aa.store.set("worker_heartbeat", {"at": utcnow(), "state": "idle"})
    assert health(aa.store, aa.settings)[0]


def test_emergency_stop_cli_and_mode_off(aa, monkeypatch):
    from autoapply import cli
    monkeypatch.setenv("HUNTER_DB", str(aa.store.path))
    assert cli.main(["stop", "--reason", "test"]) == 0
    assert aa.auto.emergency_stopped()
    assert cli.main(["resume"]) == 0
    assert not aa.auto.emergency_stopped()
    aa.permit()
    aa.switch_on("OFF")
    key = aa.add_job()
    aa.engine.evaluate(key)
    assert aa.auto.application(job_key=key) is None and aa.engine.process() == []
