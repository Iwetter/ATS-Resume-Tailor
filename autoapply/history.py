"""Privacy-preserving, local-only history of applications made outside Auto Apply.

The CSV is never included in Git. An absent file means no imported records,
not evidence that a company has never received an application.
"""
from __future__ import annotations

import csv
from pathlib import Path
import re
import json
import sqlite3
from contextlib import closing

# Common brand and legal-name variants. Extend only for confirmed equivalents.
ALIASES = {
    "swri": "southwest research institute",
    "southwest research institute": "southwest research institute",
    "google": "google",
    "alphabet": "google",
    "google deepmind": "google",
    "marvell technology": "marvell",
    "marvell": "marvell",
    "western digital": "western digital",
    "wd": "western digital",
    "lockheed": "lockheed martin",
    "lockheed martin": "lockheed martin",
    "lockheed martin space": "lockheed martin",
    "lockheed martin aeronautics": "lockheed martin",
    "anduril": "anduril",
    "anduril industries": "anduril",
    "spacex": "spacex",
    "space exploration technologies": "spacex",
    "l3harris": "l3harris",
    "l3 harris": "l3harris",
    "l3harris technologies": "l3harris",
    "northrop": "northrop grumman",
    "northrop grumman": "northrop grumman",
    "rtx": "rtx",
    "raytheon": "rtx",
    "raytheon technologies": "rtx",
    "texas instruments": "texas instruments",
    "ti": "texas instruments",
    "analog devices": "analog devices",
    "adi": "analog devices",
    "intel": "intel",
    "nvidia": "nvidia",
    "apple": "apple",
    "amd": "amd",
    "advanced micro devices": "amd",
    "qualcomm": "qualcomm",
}
SUBMITTED = {"applied", "submitted", "interview", "rejected", "withdrawn", "offer"}
UNCERTAIN = {"unknown", "pending confirmation"}
SUFFIX = {"llc", "inc", "incorporated", "corporation", "corp", "limited", "ltd", "co", "company"}


def company_key(name: str) -> str:
    """Normalize punctuation, legal suffixes and confirmed employer aliases."""
    words = re.findall(r"[a-z0-9]+", (name or "").casefold().replace("&", " and "))
    if words and words[0] == "the":
        words.pop(0)
    while len(words) > 1 and words[-1] in SUFFIX:
        words.pop()
    result = " ".join(words)
    return ALIASES.get(result, result)


def title_key(title: str) -> str:
    """Normalize common season/year suffixes without conflating different roles."""
    title = re.sub(r"\([^)]*\)|[-–]\s*(spring|summer|fall)\s*20\d{2}\b", "", title or "", flags=re.I)
    return " ".join(re.findall(r"[a-z0-9]+", title.casefold()))


def history_path(db_path) -> Path:
    return Path(db_path).parent / "application_history.csv"


def read_history(db_path) -> list[dict[str, str]]:
    """Only explicit application states count. Malformed files refuse submission."""
    path = history_path(db_path)
    if not path.exists():
        return []
    if path.stat().st_size > 2_000_000:
        raise ValueError("Application history CSV is too large to validate.")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not {"company", "status"}.issubset(set(reader.fieldnames or ())):
            raise ValueError("Application history CSV requires company and status columns.")
        records = []
        for number, row in enumerate(reader, 2):
            company = (row.get("company") or "").strip()
            status = (row.get("status") or "").strip().casefold()
            if not company or status not in SUBMITTED | UNCERTAIN:
                raise ValueError(f"Invalid application history row {number}: supply a company and a known application status.")
            source_id = (row.get("source_id") or row.get("requisition_id") or row.get("req_id") or "").strip()
            date = (row.get("date") or row.get("applied_at") or row.get("submission_date") or "").strip()
            records.append({
                "company": company_key(company),
                "title": title_key(row.get("title") or ""),
                "status": status,
                "source_id": source_id,
                "applied_at": date,
            })
        return records


def history_matches(db_path, company: str, title: str = "", source_id: str = "", *, connection=None) -> tuple[int, bool]:
    """Count unique prior/uncertain roles and detect the same title.

    Multiple acknowledgments for a single role must never consume the quota twice.
    An unspecified title counts as one prior application at that employer.
    """
    if connection is None and Path(db_path).exists():
        with closing(sqlite3.connect(db_path, timeout=30)) as db, db:
            return history_matches(db_path, company, title, source_id, connection=db)
    records = read_history(db_path)
    if connection is not None:
        # Append-only snapshots survive CSV removal and dashboard status edits.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS imported_application_history ("
            "company TEXT, title TEXT, status TEXT, source_id TEXT, applied_at TEXT, "
            "first_seen TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(company,title,status,source_id))"
        )
        cols = {r[1] for r in connection.execute("PRAGMA table_info(imported_application_history)")}
        if "applied_at" not in cols:
            connection.execute("ALTER TABLE imported_application_history ADD COLUMN applied_at TEXT")
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'jobs', 'actions'} <= tables:
            for payload, status in connection.execute("SELECT payload,status FROM jobs WHERE lower(status) IN ('applied','interview','rejected','withdrawn') OR key IN (SELECT job_key FROM actions WHERE lower(action) IN ('applied','interview','rejected','withdrawn'))"):
                job = json.loads(payload)
                records.append(dict(company=company_key(job['company']), title=title_key(job['title']), status='submitted', source_id=job['source_id'], applied_at=''))
        connection.executemany(
            "INSERT OR IGNORE INTO imported_application_history(company,title,status,source_id,applied_at) VALUES (?,?,?,?,?)",
            [(r['company'], r['title'], r['status'], r.get('source_id', ''), r.get('applied_at', '')) for r in records]
        )
        records = [dict(zip(('company', 'title', 'status', 'source_id', 'applied_at'), r)) for r in connection.execute("SELECT company,title,status,source_id,applied_at FROM imported_application_history")]
    key, title_norm = company_key(company), title_key(title)
    submitted_roles, all_roles = set(), set()
    same_id = False
    for record in records:
        if record["company"] != key:
            continue
        role = record['source_id'] or record["title"] or "__unspecified__"
        all_roles.add(record['title'])
        same_id |= bool(source_id and source_id == record['source_id'])
        if record["status"] in SUBMITTED | UNCERTAIN:
            submitted_roles.add(role)
    return len(submitted_roles), same_id or bool(title_norm and title_norm in all_roles)
