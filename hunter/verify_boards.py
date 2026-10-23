"""Verify candidate employer boards against their documented public job APIs.

    python -m hunter.verify_boards candidates.json --out verified.json

``candidates.json`` is a list of ``{"name": ..., "slugs": [...]}`` for
Greenhouse/Lever/Ashby, or ``{"name": ..., "workday": "https://x.wd1.myworkdayjobs.com/Site"}``.
A board is accepted only when its public endpoint answers with the expected
schema and lists at least one job; nothing is added on a guess. Requests are
sequential per host with a short pause, and robots.txt is honoured for the
Workday career sites (the JSON board APIs are documented public endpoints).
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
from pathlib import Path
import threading
import time
from urllib import robotparser
from urllib.parse import urlsplit
import requests

from .extra_sources import tag, xml_root
from .filtering import title_screen
from .http import SourceError

UA = "ATS-Resume-Tailor-InternshipHunter/1.0 (personal job search; board verification)"
_locks: dict[str, threading.Lock] = {}


def _get(url, timeout=20, accept="application/json"):
    host = urlsplit(url).hostname
    lock = _locks.setdefault(host, threading.Lock())
    with lock:  # one request at a time per host
        try:
            return requests.get(url, headers={"User-Agent": UA, "Accept": accept}, timeout=timeout)
        finally:
            time.sleep(0.3)


def _titles_greenhouse(slug):
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    data = r.json() if r.status_code == 200 else None
    return [j["title"] for j in data["jobs"]] if isinstance(data, dict) and isinstance(data.get("jobs"), list) else None


def _titles_lever(slug):
    r = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=100")
    data = r.json() if r.status_code == 200 else None
    return [j["text"] for j in data] if isinstance(data, list) else None


def _titles_ashby(slug):
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    data = r.json() if r.status_code == 200 else None
    return [j["title"] for j in data["jobs"]] if isinstance(data, dict) and isinstance(data.get("jobs"), list) else None


def _workday(url):
    parts = urlsplit(url)
    robots = robotparser.RobotFileParser(f"https://{parts.hostname}/robots.txt")
    try:
        robots.read()
        if not robots.can_fetch("*", url + "/siteMap.xml"):
            return None, "robots.txt disallows the sitemap"
    except OSError:
        pass
    pending, locs = [url.rstrip("/") + "/siteMap.xml"], []
    for _ in range(3):  # a sitemap index points at the job sitemaps
        if not pending:
            break
        r = _get(pending.pop(0), timeout=30, accept="*/*")  # Workday answers 406 to a narrow Accept header
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        try:
            root = xml_root(r.text)  # rejects DOCTYPE/ENTITY declarations (no XXE or entity expansion)
        except SourceError:
            return None, "not a plain XML sitemap"
        found = [e.text for e in root.iter() if tag(e) == "loc" and e.text]
        if tag(root) == "sitemapindex":
            pending += found
        locs += [l for l in found if "/job/" in l]
    return [l.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ") for l in locs], ""


PROVIDERS = {"greenhouse": (_titles_greenhouse, "https://job-boards.greenhouse.io/{slug}"),
             "lever": (_titles_lever, "https://jobs.lever.co/{slug}"),
             "ashby": (_titles_ashby, "https://jobs.ashbyhq.com/{slug}")}


def verify(candidate) -> dict:
    name = candidate["name"]
    if candidate.get("workday"):
        titles, why = _workday(candidate["workday"])
        if titles:
            host = urlsplit(candidate["workday"]).hostname
            site = urlsplit(candidate["workday"]).path.strip("/").split("/")[-1]
            board = host.split(".")[0] + "-" + host.split(".")[1] + "-" + site
            return {"name": name, "source": "workday", "board": board, "career_url": candidate["workday"].rstrip("/"), "jobs": len(titles),
                    "internships": [t for t in titles if title_screen(t) == "keep"][:8]}
        return {"name": name, "error": why or "no jobs"}
    for slug in candidate["slugs"]:
        for source, (fetch, url) in PROVIDERS.items():
            try:
                titles = fetch(slug)
            except (requests.RequestException, ValueError):
                titles = None
            if titles:  # a real board with at least one public job
                return {"name": name, "source": source, "board": slug, "career_url": url.format(slug=slug), "jobs": len(titles),
                        "internships": [t for t in titles if title_screen(t) == "keep"][:8]}
    return {"name": name, "error": "no public Greenhouse/Lever/Ashby board for " + ", ".join(candidate["slugs"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(verify, candidates))
    for r in results:
        print(("OK  " if "board" in r else "--  ") + r["name"] + (f"  [{r['source']}:{r['board']}] {r['jobs']} jobs, {len(r['internships'])} internship titles" if "board" in r else "  " + r["error"]), flush=True)
    today = date.today().isoformat()
    verified = [{"name": r["name"], "source": r["source"], "board": r["board"], "career_url": r["career_url"], "enabled": True, "verified_at": today}
                for r in results if "board" in r]
    args.out.write_text(json.dumps({"verified": verified, "results": results}, indent=2), encoding="utf-8")
    print(f"{len(verified)}/{len(candidates)} verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
