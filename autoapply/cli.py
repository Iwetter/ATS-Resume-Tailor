"""Operator commands. ``python -m autoapply.cli status|stop|resume|health|dry-run|recover``."""
from __future__ import annotations

import argparse
import json

from hunter.storage import Store

from . import config
from .engine import Engine
from .worker import health


def main(argv=None):
    parser = argparse.ArgumentParser(description="Auto Apply operator controls")
    parser.add_argument("command", choices=["status", "stop", "resume", "health", "dry-run", "recover", "disable"])
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    settings = config.load()
    store = Store(settings.db_path)
    engine = Engine(store, settings)
    auto = engine.auto
    if args.command == "stop":
        auto.set_emergency_stop(True, actor="cli", reason=args.reason or "CLI emergency stop")
        print("Emergency stop set. No application step will run until 'resume'.")
    elif args.command == "resume":
        auto.set_emergency_stop(False, actor="cli", reason=args.reason)
        if settings.stop_file.exists():
            print(f"Note: {settings.stop_file} still exists and keeps the stop active; delete it to resume.")
        print("Emergency stop cleared. Mode:", auto.mode(), "| master switch:", auto.enabled())
    elif args.command == "disable":
        auto.set_enabled(False, actor="cli")
        print("Auto Apply master switch turned off.")
    elif args.command == "health":
        ok, body = health(store, settings)
        print(json.dumps(body))
        return 0 if ok else 1
    elif args.command == "dry-run":
        from .modes import run_dry_run
        result = run_dry_run(engine)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    elif args.command == "recover":
        print(json.dumps(auto.recover(actor="cli")))
    else:
        from . import db as S
        print(json.dumps({"mode": auto.mode(), "enabled": auto.enabled(), "emergency_stop": engine.stopped(),
                          "real_submission_allowed": settings.allow_real_submission, "auto_active": engine.auto_active(),
                          "worker": store.get("worker_heartbeat"),
                          "applications": {state: len(auto.applications([state])) for state in S.STATES}}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
