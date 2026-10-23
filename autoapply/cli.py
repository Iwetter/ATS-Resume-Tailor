"""Operator commands. ``python -m autoapply.cli status|stop|resume|health|dry-run|recover|...``."""
from __future__ import annotations

import argparse
import json

from hunter.storage import Store

from . import config
from .engine import Engine
from .worker import health


def main(argv=None):
    parser = argparse.ArgumentParser(description="Auto Apply operator controls")
    parser.add_argument("command", choices=["status", "stop", "resume", "health", "dry-run", "recover", "disable", "pause", "unpause",
                                            "sms-test", "notify", "hash-password", "revoke-sessions", "inspect"])
    parser.add_argument("target", nargs="?", default="", help="inspect: a Greenhouse or Lever candidate page URL")
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    if args.command == "hash-password":
        import getpass
        from .webauth import hash_password
        password = getpass.getpass("Gateway password (12+ characters): ")
        if password != getpass.getpass("Repeat: "):
            print("Passwords differ.")
            return 1
        print("GATEWAY_PASSWORD_HASH=" + hash_password(password))
        return 0
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
    elif args.command == "pause":
        from .controls import pause
        print(pause(engine, actor="cli", reason=args.reason))
    elif args.command == "unpause":
        from .controls import resume
        print(resume(engine, actor="cli")[1])
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
    elif args.command in ("sms-test", "notify"):
        from .notify import Notifier
        from .sms import SmsService
        sms = SmsService(store, settings)
        try:
            print(json.dumps(sms.send_test(actor="cli") if args.command == "sms-test" else Notifier(engine, sms).run(), default=str))
        except ValueError as exc:
            print(exc)
            return 1
    elif args.command == "revoke-sessions":
        from . import links
        store.set("gateway_session_epoch", (store.get("gateway_session_epoch") or 0) + 1)
        print(f"Signed out every phone session and revoked {links.revoke_all(auto)} review link(s).")
        auto.audit("cli", "gateway_sessions_revoked")
    elif args.command == "inspect":
        return _inspect(settings, args.target)
    else:
        from . import db as S
        from .controls import describe_mode
        print(json.dumps({"mode": auto.mode(), "permission_mode": describe_mode(engine), "enabled": auto.enabled(), "emergency_stop": engine.stopped(),
                          "real_submission_allowed": settings.allow_real_submission, "auto_active": engine.auto_active(),
                          "worker": store.get("worker_heartbeat"),
                          "applications": {state: len(auto.applications([state])) for state in S.STATES}}, indent=2, default=str))
    return 0


def _inspect(settings, url):
    """Read-only adapter check against a live candidate page: types, uploads and submits nothing."""
    from urllib import robotparser
    from urllib.parse import urlsplit
    from .browser import ADAPTERS, BrowserFactory, inspect_form
    adapter = next((a for a in ADAPTERS if a.handles(url)), None)
    if adapter is None:
        print("Give a job-boards.greenhouse.io or jobs.lever.co candidate page URL.")
        return 2
    robots = robotparser.RobotFileParser(f"https://{urlsplit(url).hostname}/robots.txt")
    try:
        robots.read()
        if not robots.can_fetch("*", url):
            print("robots.txt disallows this page; not inspected.")
            return 2
    except OSError:
        pass
    with BrowserFactory(settings).page() as page:
        print(json.dumps(inspect_form(page, adapter, url), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
