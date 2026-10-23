"""Always-on worker: discovery every 10–15 minutes + application queue.

Run: ``python -m autoapply.worker`` (``--once`` for a single cycle).

* Each enabled board has its own ``next_due``; Workday sitemaps default to an
  hourly cadence. Failures back off exponentially; rate-limited boards back
  off for at least the requested Retry-After (minimum 30 minutes).
* Hunter's robots.txt checks, host pacing and bounded retries are reused
  unchanged (``hunter.http.PublicClient``).
* Every persisted job is evaluated immediately (``Engine.on_scanned_job``).
* Startup recovery converts interrupted submissions to Unknown and requeues
  interrupted preparations. SIGTERM/SIGINT finish the current step and exit.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import random
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from hunter.http import PublicClient
from hunter.models import Filters, Profile, utcnow
from hunter.service import WATCHLIST, scan
from hunter.storage import Store

from . import config
from .engine import Engine
from .gates import TARGET_TERMS
from .profile import hunter_profile, load_profile
from .redaction import configure_logging, log

logger = logging.getLogger("autoapply.worker")
VERSION = "autoapply-1"


class Worker:
    def __init__(self, settings=None, store=None, engine=None, client_factory=None, sleep=time.sleep, notifier=None):
        self.settings = settings or config.load()
        self.store = store or Store(self.settings.db_path)
        self.engine = engine or Engine(self.store, self.settings)
        self.auto = self.engine.auto
        if notifier is None:
            from .notify import Notifier
            from .sms import SmsService
            notifier = Notifier(self.engine, SmsService(self.store, self.settings))
        self.notifier = notifier
        self.client_factory = client_factory or self._client
        self.sleep = sleep
        self.stopping = threading.Event()
        self.started = utcnow()
        self.last_cycle = {}

    def _client(self):
        s = self.settings
        # Listing TTL below the scan interval keeps each scan fresh; details
        # use a long TTL and are refreshed early when the listing changes.
        return PublicClient(self.store, ttl=s.listing_ttl, detail_ttl=s.detail_ttl,
                            source_ttls={"myworkdayjobs.com": max(s.listing_ttl, s.workday_interval - 60)})

    # ---- lifecycle -------------------------------------------------------------
    def recover(self):
        changed = self.auto.recover()  # audit rows here become "uncertain outcome" alerts on the first notify()
        lock = self.store.get("scan_lock")
        if lock and datetime.fromisoformat(lock) < datetime.now(timezone.utc) - timedelta(minutes=self.settings.scan_lease_minutes):
            self.store.set("scan_lock", None)
            changed.append(("scan_lock", None, "cleared"))
        for item in changed:
            log(logger, logging.WARNING, "recovered after restart", item=list(item))
        return changed

    def heartbeat(self, state, **extra):
        value = {"at": utcnow(), "state": state, "pid": os.getpid(), "host": socket.gethostname(), "started": self.started,
                 "version": VERSION, "scan_interval": self.settings.scan_interval, "mode": self.auto.mode(),
                 "enabled": self.auto.enabled(), "emergency_stop": self.engine.stopped(), **self.last_cycle, **extra}
        self.store.set("worker_heartbeat", value)
        return value

    # ---- discovery -------------------------------------------------------------
    def interval_for(self, employer):
        return self.settings.workday_interval if employer.source == "workday" else self.settings.scan_interval

    def due_employers(self, now=None):
        now = now or datetime.now(timezone.utc)
        due = []
        for employer in self.store.employers(WATCHLIST):
            if not employer.enabled or employer.source == "manual":
                continue
            status = self.auto.board_get(employer.key)
            if status and status.get("next_due") and datetime.fromisoformat(status["next_due"]) > now:
                continue
            due.append(employer)
        return due

    def scan_once(self):
        due = self.due_employers()
        if not due:
            return None
        master = self.store.master()
        applicant = load_profile(self.store)
        profile = hunter_profile(applicant, Profile(**self.store.get("profile", asdict(Profile()))))
        for employer in due:
            self.auto.board_update(employer.key, employer.name, last_attempt=utcnow())
        result = scan(self.store, due, master, profile, Filters(self.settings.days, TARGET_TERMS), client=self.client_factory(),
                      lease=timedelta(minutes=self.settings.scan_lease_minutes), on_job=self.engine.on_scanned_job)
        now = datetime.now(timezone.utc)
        by_key = {f"{e.source}:{e.board}": e for e in due}
        for board in result["boards"]:
            key = f"{board['source']}:{board['board']}"
            employer = by_key.get(key)
            interval = self.interval_for(employer) if employer else self.settings.scan_interval
            jitter = random.randint(0, self.settings.scan_jitter) if self.settings.scan_jitter else 0
            if board["success"]:
                self.auto.board_update(key, board["employer"], last_success=utcnow(), last_checked=board.get("checked_at"), last_error="",
                                       consecutive_failures=0, jobs=board.get("engineering_internships", 0), new_jobs=board.get("new", 0),
                                       next_due=(now + timedelta(seconds=interval + jitter)).isoformat())
            else:
                previous = (self.auto.board_get(key) or {}).get("consecutive_failures", 0) + 1
                delay = min(6 * 3600, interval * 2 ** min(previous, 5))
                if board.get("rate_limited"):
                    delay = max(delay, 1800, int(board.get("retry_after") or 0))
                self.auto.board_update(key, board["employer"], last_error=board.get("error", "")[:500], consecutive_failures=previous,
                                       next_due=(now + timedelta(seconds=delay + jitter)).isoformat())
        log(logger, logging.INFO, "scan finished", boards=len(result["boards"]), ok=sum(b["success"] for b in result["boards"]),
            new=len(result["new_ids"]), requests=result.get("http_requests"), cache_hits=result.get("http_cache_hits"))
        return result

    # ---- loop --------------------------------------------------------------------
    def run_once(self):
        self.heartbeat("scanning")
        scan_result, processed = None, []
        try:
            scan_result = self.scan_once()
        except ValueError as exc:  # e.g. another scan holds the lease
            log(logger, logging.WARNING, "scan skipped", reason=str(exc))
        except Exception as exc:
            log(logger, logging.ERROR, "scan failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        if not self.stopping.is_set():
            self.heartbeat("applying")
            try:
                processed = self.engine.process()
            except Exception as exc:
                log(logger, logging.ERROR, "queue processing failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        sms = self.notify()
        self.last_cycle = {"last_cycle": utcnow(), "last_scan_new": len(scan_result["new_ids"]) if scan_result else None,
                           "last_scan_at": scan_result["finished"] if scan_result else (self.last_cycle.get("last_scan_at")),
                           "processed": [list(x) for x in processed], "sms": sms}
        self.heartbeat("idle")
        return scan_result, processed

    def notify(self):
        """Queue and send SMS for this cycle's events. A failure here never stops the worker."""
        try:
            return self.notifier.run()
        except Exception as exc:
            log(logger, logging.ERROR, "notifications failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
            return {"error": type(exc).__name__}

    def run_forever(self, tick=60):
        self.recover()
        self._signals()
        server = self._health_server()
        log(logger, logging.INFO, "worker started", interval=self.settings.scan_interval, mode=self.auto.mode(),
            real_submission=self.settings.allow_real_submission)
        try:
            while not self.stopping.is_set():
                self.run_once()
                self.stopping.wait(tick)
        finally:
            self.heartbeat("stopped")
            if server:
                server.shutdown()
            log(logger, logging.INFO, "worker stopped")

    def _signals(self):
        def handler(signum, frame):
            log(logger, logging.INFO, "shutdown requested", signal=signum)
            self.stopping.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except ValueError:  # not main thread (tests)
                pass

    def _health_server(self):
        if not self.settings.health_port:
            return None
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                ok, body = health(worker.store, worker.settings)
                data = json.dumps(body).encode()
                self.send_response(200 if ok else 503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = HTTPServer(("0.0.0.0", self.settings.health_port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


def health(store, settings, max_age=None):
    beat = store.get("worker_heartbeat")
    if not beat:
        return False, {"ok": False, "reason": "no heartbeat"}
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(beat["at"])).total_seconds()
    limit = max_age or max(600, 3 * 60 + 2 * settings.scan_interval)
    ok = age < limit and beat.get("state") != "stopped"
    return ok, {"ok": ok, "age_seconds": round(age), "state": beat.get("state"), "mode": beat.get("mode"), "emergency_stop": beat.get("emergency_stop")}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Always-on internship discovery and application worker.")
    parser.add_argument("--once", action="store_true", help="Run a single scan + queue cycle, then exit.")
    args = parser.parse_args(argv)
    settings = config.load()
    configure_logging(settings.log_level)
    worker = Worker(settings)
    if args.once:
        worker.recover()
        scan_result, processed = worker.run_once()
        print(json.dumps({"new": len(scan_result["new_ids"]) if scan_result else 0, "processed": processed}, default=str))
        return 0
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
