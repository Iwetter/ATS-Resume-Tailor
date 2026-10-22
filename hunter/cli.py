from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .models import Filters, Profile
from .service import DEFAULT_DB, WATCHLIST, scan, evaluate
from .storage import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Internship Hunter. No LLM calls and no application submissions.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--watchlist", type=Path, help="Optional JSON watchlist override.")
    parser.add_argument("--days", type=int, choices=(1, 3, 7, 14, 30), default=7)
    parser.add_argument("--report", type=Path, help="Optional JSON scan report path.")
    args = parser.parse_args(argv)
    store = Store(args.db)
    employers = store.employers(args.watchlist or WATCHLIST)
    if args.watchlist:
        from .models import Employer
        employers = [Employer(**r) for r in json.loads(args.watchlist.read_text(encoding="utf-8"))]
    profile = Profile(**store.get("profile", asdict(Profile())))
    result = scan(store, employers, store.master(), profile, Filters(days=args.days), progress=lambda n, total, name: print(f"[{n+1}/{total}] {name}", flush=True))
    result["verified_master_configured"] = store.master() is not None
    evaluated = evaluate(store, store.master(), profile, Filters(days=args.days), result["finished"])
    result["confirmed_recent"] = sum(x["decision"].eligible and x["confirmed_scope"] and x["decision"].recency == "recent" for x in evaluated)
    result["publication_unknown"] = sum(x["decision"].eligible and x["decision"].recency == "unknown" for x in evaluated)
    print(json.dumps(result, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if any(x["success"] for x in result["boards"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
