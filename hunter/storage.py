"""SQLite persistence. Scan membership and application status are independent."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3

from ats.matching import make_resume
from ats.models import Evidence, digest
from .models import Employer, Job, Profile, canonical_url, utcnow

STATUSES = ("New", "Saved", "Applied", "Interview", "Rejected", "Withdrawn")


# Only these bases state when a posting was first published. Ashby's
# publishedAt can be a republication; Lever/Personio expose no publication
# date; imports are user-reported.
VERIFIED_PUBLICATION = ("Greenhouse first_published", "SmartRecruiters releasedDate", "Workday public JobPosting datePosted", "Recruitee explicit published_at")
TRACKED_FIELDS = ("title", "location", "description", "updated_at", "expires_at", "compensation", "employment_type")


def verified_publication(job: Job) -> bool:
    return not job.imported_at and job.publication_basis in VERIFIED_PUBLICATION


def _change_events(old: Job, new: Job, availability) -> list[tuple[str, dict]]:
    """Classify a re-observed job. An update is never a new publication."""
    if (old.source, old.board, old.source_id) != (new.source, new.board, new.source_id):
        return []
    events = []
    changed = [f for f in TRACKED_FIELDS if getattr(old, f) != getattr(new, f) and not (f == "description" and not new.description)]
    if changed:
        events.append(("updated", {"fields": changed, "updated_at": new.updated_at}))
    if old.published_at and new.published_at and old.published_at != new.published_at:
        events.append(("republished", {"previous_published_at": old.published_at, "published_at": new.published_at, "publication_basis": new.publication_basis}))
    if availability == "closed" and new.source_status == "open":
        events.append(("reopened", {}))
    return events


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
              key TEXT PRIMARY KEY, source TEXT, board TEXT, source_id TEXT,
              canonical_url TEXT, secondary_key TEXT, payload TEXT NOT NULL,
              discovered_at TEXT NOT NULL, checked_at TEXT, availability TEXT,
              status TEXT NOT NULL DEFAULT 'New', notes TEXT NOT NULL DEFAULT '',
              UNIQUE(source,board,source_id));
            CREATE INDEX IF NOT EXISTS jobs_url ON jobs(canonical_url);
            CREATE INDEX IF NOT EXISTS jobs_secondary ON jobs(secondary_key);
            CREATE TABLE IF NOT EXISTS aliases (source TEXT,board TEXT,source_id TEXT,job_key TEXT,PRIMARY KEY(source,board,source_id));
            CREATE TABLE IF NOT EXISTS observations (source TEXT,board TEXT,source_id TEXT,status TEXT,checked_at TEXT,PRIMARY KEY(source,board,source_id));
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, fetched_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS matches (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, started TEXT, finished TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY,job_key TEXT,at TEXT,action TEXT,details TEXT);
            CREATE TABLE IF NOT EXISTS artifacts (key TEXT PRIMARY KEY,job_key TEXT,created TEXT,pdf TEXT,docx TEXT,audit TEXT,metadata TEXT);
            CREATE TABLE IF NOT EXISTS imports (job_key TEXT,origin_url TEXT,imported_at TEXT,metadata TEXT,PRIMARY KEY(job_key,origin_url));
            CREATE TABLE IF NOT EXISTS job_events (id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, at TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS job_events_key ON job_events(job_key, id);
            ''')
            # Additive, idempotent migration: existing databases keep every
            # row; new columns start NULL and are filled as jobs are observed.
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for name in ("first_published_at", "publication_verified", "last_changed_at"):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                # The dashboard and the always-on worker share this database.
                db.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def employers(self, defaults_path):
        rows = self.get("employers")
        defaults = json.loads(Path(defaults_path).read_text(encoding="utf-8"))
        # Introduce newly shipped providers without resetting saved names/toggles.
        if rows is not None and not self.get("expanded_boards_v2"):
            keys = {(r["source"], r["board"]) for r in rows}
            rows += [r for r in defaults if r["source"] in {"workday", "recruitee", "personio"} and (r["source"], r["board"]) not in keys]
            self.set("employers", rows)
            self.set("expanded_boards_v2", True)
        # October 2026: verified EE/hardware/robotics/defense/semiconductor boards. Added once;
        # boards the user removed later are not re-added, saved names and toggles are kept.
        if rows is not None and not self.get("expanded_boards_v3"):
            keys = {(r["source"], r["board"]) for r in rows}
            rows += [r for r in defaults if (r["source"], r["board"]) not in keys]
            self.set("employers", rows)
            self.set("expanded_boards_v3", True)
        return [Employer(**row) for row in (rows if rows is not None else defaults)]

    def save_master(self, resume, original: bytes | None = None, filename: str = ""):
        # Content-addressed immutable snapshots; moving the active pointer never
        # overwrites a prior master or a job-specific copy.
        key = digest([resume.source_hash, [asdict(x) for x in resume.items]])
        value = {"source_hash": resume.source_hash, "items": [asdict(x) for x in resume.items], "filename": Path(filename).name, "verified_at": utcnow()}
        if self.get("master:" + key) is None:
            self.set("master:" + key, value)
            if original:
                folder = self.path.parent / "masters"
                folder.mkdir(exist_ok=True)
                path = folder / (key + (Path(filename).suffix if Path(filename).suffix.lower() in {".pdf", ".docx"} else ".bin"))
                if not path.exists():
                    path.write_bytes(original)
        self.set("active_master", key)
        return key

    def master_original(self):
        """(bytes, filename) of the uploaded file behind the active master, or None."""
        key = self.get("active_master")
        value = self.get("master:" + key) if key else None
        if not value:
            return None
        for suffix in (".docx", ".pdf"):
            path = self.path.parent / "masters" / (key + suffix)
            if path.is_file():
                return path.read_bytes(), value.get("filename") or ("master" + suffix)
        return None

    def master(self):
        key = self.get("active_master")
        value = self.get("master:" + key) if key else None
        return make_resume(tuple(Evidence(**x) for x in value["items"]), value["source_hash"]) if value else None

    def cache_get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value,fetched_at FROM cache WHERE key=?", (key,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def cache_set(self, key, value, fetched_at):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value, ensure_ascii=False), fetched_at))

    def match_get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM matches WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def match_set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO matches VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def upsert(self, job: Job, discovered_at: str) -> tuple[str, bool]:
        url = canonical_url(job.url)
        with self.connect() as db:
            alias = db.execute("SELECT job_key FROM aliases WHERE source=? AND board=? AND source_id=?", (job.source, job.board, job.source_id)).fetchone()
            existing = db.execute("SELECT * FROM jobs WHERE key=? OR canonical_url=? ORDER BY discovered_at LIMIT 1", (alias[0] if alias else job.key, url)).fetchone()
            if existing is None:
                secondary = db.execute("SELECT * FROM jobs WHERE secondary_key=? ORDER BY discovered_at LIMIT 1", (job.secondary_key,)).fetchone()
                if secondary and (secondary["source"], secondary["board"]) != (job.source, job.board):
                    existing = secondary
            key = existing["key"] if existing else job.key
            events = []
            if existing:
                old = Job(**json.loads(existing["payload"]))
                events = _change_events(old, job, existing["availability"])
                if not job.description and old.description:
                    from dataclasses import replace
                    job = replace(job, description=old.description, published_at=job.published_at or old.published_at, publication_basis=job.publication_basis if job.published_at else old.publication_basis)
                # Prefer the established provider's canonical record. Cross-
                # posted duplicates remain aliases, so one source cannot close
                # a job still returned by a different source.
                if old.imported_at and not job.imported_at:
                    db.execute("UPDATE jobs SET source=?,board=?,source_id=?,canonical_url=?,secondary_key=?,payload=?,checked_at=?,availability=? WHERE key=?", (job.source, job.board, job.source_id, url, job.secondary_key, json.dumps(asdict(job)), job.checked_at, job.source_status, key))
                elif (old.source, old.board, old.source_id) == (job.source, job.board, job.source_id):
                    db.execute("UPDATE jobs SET canonical_url=?,secondary_key=?,payload=?,checked_at=?,availability=? WHERE key=?", (url, job.secondary_key, json.dumps(asdict(job)), job.checked_at, job.source_status, key))
                elif job.source_status == "open" and not job.imported_at:
                    db.execute("UPDATE jobs SET availability='open',checked_at=? WHERE key=?", (job.checked_at, key))
            else:
                db.execute("INSERT INTO jobs(key,source,board,source_id,canonical_url,secondary_key,payload,discovered_at,checked_at,availability) VALUES (?,?,?,?,?,?,?,?,?,?)", (key, job.source, job.board, job.source_id, url, job.secondary_key, json.dumps(asdict(job)), discovered_at, job.checked_at, job.source_status))
                events = [("discovered", {"published_at": job.published_at, "publication_basis": job.publication_basis, "source": job.source})]
            # Verified first publication is recorded once and never moved by a
            # later update, republication or rediscovery.
            if job.published_at and verified_publication(job):
                db.execute("UPDATE jobs SET first_published_at=?,publication_verified='1' WHERE key=? AND first_published_at IS NULL", (job.published_at, key))
            if any(name in {"updated", "republished"} for name, _ in events):
                db.execute("UPDATE jobs SET last_changed_at=? WHERE key=?", (job.checked_at or discovered_at, key))
            for name, details in events:
                db.execute("INSERT INTO job_events(job_key,at,event,details) VALUES (?,?,?,?)", (key, job.checked_at or discovered_at, name, json.dumps(details, ensure_ascii=False)))
            db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?,?,?)", (job.source, job.board, job.source_id, key))
            db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?)", (job.source, job.board, job.source_id, job.source_status, job.checked_at))
        return key, existing is None

    def save_import(self, key, origin_url, metadata):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO imports VALUES (?,?,?,?)", (key, origin_url, metadata["imported_at"], json.dumps(metadata)))

    def imports(self, key=None):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM imports" + (" WHERE job_key=?" if key else "") + " ORDER BY imported_at DESC", (key,) if key else ()).fetchall()
        return [dict(row) for row in rows]

    def confirm_import(self, key, application_url, confirmed):
        from .boards import public_link, source_for
        record = self.job(key)
        if not record:
            raise ValueError("Job does not exist.")
        job = Job(**json.loads(record["payload"]))
        if not job.imported_at:
            raise ValueError("Availability for an automatic source is updated by scanning its employer board.")
        url = public_link(application_url)
        if confirmed and (url == job.origin_url and source_for(url) not in {"workday", "usajobs", "workable", "manual"} or source_for(url) in {"linkedin", "indeed", "glassdoor", "handshake", "ziprecruiter", "dice", "builtin"}):
            raise ValueError("Provide the employer's own application URL before confirming availability.")
        job = replace(job, url=url, checked_at=utcnow() if confirmed else None,
                      source_status="user-confirmed" if confirmed else "unknown",
                      availability_basis="User confirmed the employer application page is accepting applications" if confirmed else "Pasted listing; availability unverified")
        # The canonical employer URL may now match an automatically scanned job.
        # Keep this record/history stable; a later scan can promote its metadata.
        with self.connect() as db:
            db.execute("UPDATE jobs SET canonical_url=?,payload=?,checked_at=?,availability=? WHERE key=?", (url, json.dumps(asdict(job)), job.checked_at, job.source_status, key))
            db.execute("UPDATE observations SET status=?,checked_at=? WHERE source=? AND board=? AND source_id=?", (job.source_status, job.checked_at, job.source, job.board, job.source_id))
        self.save_import(key, job.origin_url, asdict(job))

    def close_absent(self, employer: Employer, all_ids: frozenset[str], checked_at: str):
        with self.connect() as db:
            rows = db.execute("SELECT job_key AS key,source_id FROM aliases WHERE source=? AND board=?", (employer.source, employer.board)).fetchall()
            for row in rows:
                if row["source_id"] in all_ids:
                    continue
                db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?)", (employer.source, employer.board, row["source_id"], "closed", checked_at))
                states = [x[0] for x in db.execute("SELECT o.status FROM observations o JOIN aliases a USING(source,board,source_id) WHERE a.job_key=?", (row["key"],))]
                availability = "open" if "open" in states else "unknown" if "unknown" in states else "closed"
                if availability != "open":
                    previous = db.execute("SELECT availability FROM jobs WHERE key=?", (row["key"],)).fetchone()
                    db.execute("UPDATE jobs SET availability=?,checked_at=? WHERE key=?", (availability, checked_at, row["key"]))
                    if previous and previous[0] != availability:
                        db.execute("INSERT INTO job_events(job_key,at,event,details) VALUES (?,?,?,?)", (row["key"], checked_at, availability, json.dumps({"source": employer.source, "board": employer.board})))

    def jobs(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute("SELECT * FROM jobs ORDER BY discovered_at DESC,key")]

    def job(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def status(self, key, status, notes=""):
        if status not in STATUSES:
            raise ValueError("Unknown application status.")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone():
                raise ValueError("Job does not exist.")
            db.execute("UPDATE jobs SET status=?,notes=? WHERE key=?", (status, notes, key))
            db.execute("INSERT INTO actions(job_key,at,action,details) VALUES (?,?,?,?)", (key, utcnow(), status, notes))

    def save_artifact(self, job_key, bundle, audit, metadata):
        if not self.job(job_key):
            raise ValueError("Save the job before saving an artifact.")
        key = digest([job_key, audit, metadata])
        folder = self.path.parent / "tailored" / job_key
        folder.mkdir(parents=True, exist_ok=True)
        paths = {"pdf": folder / (key + ".pdf"), "docx": folder / (key + ".docx"), "audit": folder / (key + ".json")}
        for name, value in (("pdf", bundle.pdf), ("docx", bundle.docx), ("audit", json.dumps(audit, ensure_ascii=False, indent=2).encode("utf-8"))):
            if not paths[name].exists():
                paths[name].write_bytes(value)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?,?,?)", (key, job_key, utcnow(), str(paths["pdf"]), str(paths["docx"]), str(paths["audit"]), json.dumps(metadata)))
        return key

    def artifacts(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute("SELECT * FROM artifacts ORDER BY created DESC")]

    def events(self, key=None, limit=500):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM job_events" + (" WHERE job_key=?" if key else "") + " ORDER BY id DESC LIMIT ?", ((key, limit) if key else (limit,))).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]
