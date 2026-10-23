"""Container health check: exit 0 when the worker heartbeat is fresh."""
from __future__ import annotations

import json
import sys

from hunter.storage import Store

from . import config
from .worker import health


def main():
    settings = config.load()
    try:
        ok, body = health(Store(settings.db_path), settings)
    except Exception as exc:  # database not reachable
        ok, body = False, {"ok": False, "reason": type(exc).__name__}
    print(json.dumps(body))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
