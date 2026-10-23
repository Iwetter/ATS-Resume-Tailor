"""Two-stage local discovery/scoring; tailoring delegates to Phase 1."""
from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re

from ats.matching import extract_requirements, match_requirement, rank_bullets, has_term, make_resume
from ats.models import Change, Extracted, digest
from ats.scoring import analyze

from .filtering import decide, us_location
from .http import PublicClient, RateLimited, SourceError
from .models import Employer, Filters, Job, Profile, utcnow
from .sources import fetch_board

ROOT = Path(__file__).resolve().parents[1]
# HUNTER_DB lets a container keep the database on a persistent volume.
DEFAULT_DB = Path(os.environ.get("HUNTER_DB") or ROOT / "data" / "hunter.sqlite3")
WATCHLIST = ROOT / "hunter" / "employers.json"
SCORING_VERSION = "hunter-1"


def profile_from_master(profile, master):
    """Use explicit education facts in the verified master when detectable."""
    if master is None:
        return profile
    education = "\n".join(x.text for x in master.items if x.section == "Education")
    updates = {}
    gpa = re.search(r"GPA\s*[:=]?\s*(\d\.\d+)", education, re.I)
    if gpa:
        updates["gpa"] = float(gpa[1])
    degree = next((x.text for x in master.items if x.section == "Education" and re.search(r"bachelor|master|ph\.?d|\bB\.S\b|\bM\.S\b", x.text, re.I)), None)
    if degree:
        updates["degree"] = degree
    months = "January February March April May June July August September October November December".split()
    graduation = re.search(r"(?:Expected|Graduation|Graduating)\s*[:=]?\s*(" + "|".join(months) + r")\s+(20\d{2})", education, re.I)
    if graduation:
        updates["graduation"] = f"{graduation[2]}-{next(i for i, month in enumerate(months, 1) if month.casefold() == graduation[1].casefold()):02}"
    return replace(profile, **updates)


def order_skills(resume, requirements):
    """Reorder existing comma/semicolon skill entries without adding content."""
    terms = [term for r in requirements if r.kind == "skill" for term in r.terms]
    rows = []
    for item in resume.items:
        if item.kind != "skill":
            rows.append(item)
            continue
        prefix, body = "", item.text
        if ":" in body:
            label, body = body.split(":", 1)
            prefix = label + ": "
        values = [s.strip() for s in re.split(r"[,;]", body) if s.strip()]
        ordered = sorted(enumerate(values), key=lambda pair: (-sum(has_term(pair[1], t) for t in terms), pair[0]))
        rows.append(replace(item, text=prefix + ", ".join(value for _, value in ordered)) if len(values) > 1 else item)
    return make_resume(tuple(rows), resume.source_hash)


def score_job(job: Job, master, store=None):
    if master is None or len(job.description.split()) < 15:
        return None
    key = digest([SCORING_VERSION, [asdict(x) for x in master.items], job.description])
    cached = store.match_get(key) if store else None
    if cached is not None:
        return cached
    requirements = extract_requirements(job.description)
    core = tuple(r for r in requirements if r.priority != "preferred" and r.kind != "eligibility")
    preferred = tuple(r for r in requirements if r.priority == "preferred" and r.kind != "eligibility")
    # Reuse Phase 1 evidence matching, binary TF-IDF and deterministic grading.
    analysis = analyze(master, Extracted(master.text, "verified-ledger"), core)
    preferred_matches = tuple(match_requirement(r, master) for r in preferred)
    preferred_score = 100 * sum({"supported": 1, "partial": .5}.get(m.status, 0) for m in preferred_matches) / len(preferred_matches) if preferred_matches else 0
    components = {"Technical skills (35%)": analysis.components["skill_coverage"], "Project/experience evidence (35%)": analysis.components["experience_evidence"], "Responsibilities (20%)": analysis.components["responsibilities"], "Preferred qualifications (10%)": round(preferred_score, 1)}
    fit = round(.35*analysis.components["skill_coverage"] + .35*analysis.components["experience_evidence"] + .20*analysis.components["responsibilities"] + .10*preferred_score, 1)
    matches = analysis.matches + preferred_matches
    by_id = {r.id: r for r in core+preferred}
    details = []
    for match in matches:
        r = by_id[match.requirement_id]
        details.append({**asdict(match), "requirement": asdict(r), "evidence": [{"id": eid, "text": master.by_id[eid].text} for eid in match.evidence_ids]})
    skills = [d for d in details if d["requirement"]["kind"] == "skill"]
    mentioned = [d for d in skills if d["status"] in {"supported", "partial"}]
    result = {"fit": fit, "components": components, "details": details,
              "matched_skills": sorted({t for d in skills if d["status"] == "supported" for t in d["requirement"]["terms"]}),
              "missing": sorted({" / ".join(d["requirement"]["terms"]) or d["requirement"]["text"] for d in details if d["status"] == "missing"}),
              "representation": round(100*sum(d["status"] == "supported" for d in mentioned)/len(mentioned), 1) if mentioned else 0,
              "strongest_evidence": [{"id": eid, "text": master.by_id[eid].text, "relevance": value} for eid, value in rank_bullets(master, requirements)[:6]],
              "notes": list(analysis.warnings) + ([] if preferred else ["No preferred qualifications measured; preferred component is 0."]), "source_hash": master.source_hash, "cache_key": key}
    result = json.loads(json.dumps(result))  # identical types before/after SQLite JSON cache
    if store:
        store.match_set(key, result)
    return result


def scan(store, employers=None, master=None, profile=None, filters=None, client=None, progress=None, lease=timedelta(hours=2), on_job=None):
    """``lease`` bounds how long a crashed scan can block the next one.

    ``on_job(key, job, fresh)`` is called after each job is persisted so the
    always-on worker can evaluate new/changed relevant jobs immediately.
    """
    profile, filters = profile_from_master(profile or Profile(), master), filters or Filters()
    employers = employers if employers is not None else store.employers(WATCHLIST)
    started = utcnow()
    # Atomic lease prevents UI and scheduled CLI from scanning simultaneously.
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM kv WHERE key='scan_lock'").fetchone()
        lock = json.loads(row[0]) if row else None
        if lock and datetime.fromisoformat(lock) > datetime.now(timezone.utc)-lease:
            raise ValueError(f"Another scan is already running. Its lease expires after {int(lease.total_seconds()//60)} minutes if interrupted.")
        db.execute("INSERT OR REPLACE INTO kv VALUES ('scan_lock',?)", (json.dumps(started),))
        scan_id = db.execute("INSERT INTO scans(started) VALUES (?)", (started,)).lastrowid
    client = client or PublicClient(store)
    result = {"scan_id": scan_id, "started": started, "new_ids": [], "boards": [], "jobs_checked": 0, "scored": 0}
    try:
        active = [e for e in employers if e.enabled and e.source != "manual"]
        for n, employer in enumerate(active):
            if progress:
                progress(n, len(active), employer.name)
            try:
                snapshot = fetch_board(employer, client)
                new = 0
                for job in snapshot.jobs:
                    key, fresh = store.upsert(job, started)
                    if fresh:
                        result["new_ids"].append(key)
                        new += 1
                    result["jobs_checked"] += 1
                    decision = decide(job, profile, filters, started)
                    if decision.eligible and master is not None and job.description:
                        if score_job(job, master, store):
                            result["scored"] += 1
                    if on_job:
                        on_job(key, job, fresh)
                if snapshot.complete:
                    store.close_absent(employer, snapshot.all_ids, snapshot.checked_at)
                result["boards"].append({"employer": employer.name, "source": employer.source, "board": employer.board, "success": True, "total_public_jobs": len(snapshot.all_ids), "engineering_internships": len(snapshot.jobs), "new": new, "checked_at": snapshot.checked_at, "cached": snapshot.cached, "complete": snapshot.complete, "warnings": list(snapshot.warnings)})
            except (SourceError, KeyError, TypeError, ValueError) as exc:
                result["boards"].append({"employer": employer.name, "source": employer.source, "board": employer.board, "success": False, "error": str(exc), "rate_limited": isinstance(exc, RateLimited), "retry_after": getattr(exc, "retry_after", None)})
        result["finished"] = utcnow()
        result["http_requests"] = getattr(client, "requests", None)
        result["http_cache_hits"] = getattr(client, "cache_hits", None)
        store.set("last_scan", result)
        with store.connect() as db:
            db.execute("UPDATE scans SET finished=?,result=? WHERE id=?", (result["finished"], json.dumps(result), scan_id))
        return result
    finally:
        store.set("scan_lock", None)


def evaluate(store, master, profile, filters, now=None):
    profile = profile_from_master(profile, master)
    values = []
    for record in store.jobs():
        job = replace(Job(**json.loads(record["payload"])), source_status=record["availability"], checked_at=record["checked_at"])
        decision = decide(job, profile, filters, now or utcnow())
        fit = score_job(job, master, store) if decision.eligible else None
        values.append({"record": record, "job": job, "decision": decision, "match": fit,
                       "confirmed_scope": bool(set(decision.terms) & set(filters.terms)) and us_location(job) == "met" and job.source_status == "open" and not job.imported_at})
    return values


class RewriteCache(MutableMapping):
    """Persist Phase 1's reserved attempts and results, without credentials."""
    def __init__(self, store):
        self.store = store

    def __getitem__(self, key):
        value = self.store.match_get("rewrite:" + key)
        if value is None:
            raise KeyError(key)
        if "changes" in value:
            value["changes"] = tuple(Change(tuple(c["source_ids"]), c["original_text"], c["proposed_text"], c["justification"]) for c in value["changes"])
        return value

    def __setitem__(self, key, value):
        serialized = {**value}
        if "changes" in serialized:
            serialized["changes"] = [asdict(c) for c in serialized["changes"]]
        self.store.match_set("rewrite:" + key, serialized)

    def __delitem__(self, key):
        raise ValueError("Reserved paid attempts cannot be silently deleted.")

    def __iter__(self):
        with self.store.connect() as db:
            return iter([row[0][8:] for row in db.execute("SELECT key FROM matches WHERE key LIKE 'rewrite:%'")])

    def __len__(self):
        with self.store.connect() as db:
            return db.execute("SELECT COUNT(*) FROM matches WHERE key LIKE 'rewrite:%'").fetchone()[0]
