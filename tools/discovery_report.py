"""Summarise a real discovery scan and compare it with the pre-repair filter.

    python tools/discovery_report.py data/demo/discovery.sqlite3 data/demo/scan-report.json > report.md

Lists the employers checked and failed, listings fetched, jobs kept and judged
relevant, and the relevant openings the old title-only filter would have
discarded before they were ever stored.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunter.filtering import decide  # noqa: E402
from hunter.models import Filters, Job, Profile  # noqa: E402
from hunter.storage import Store  # noqa: E402

# The pre-repair retrieval filter, verbatim: an internship word AND a field word in the title.
LEGACY_INTERNSHIP = re.compile(r"\bintern(?:ship)?s?\b|\bco[ -]?op\b|cooperative education", re.I)
LEGACY_FIELDS = (r"embedded|firmware", r"\bpcb\b|hardware|electronics", r"electrical|\bee\b", r"\bfpga\b|digital design|\basic\b|\bsoc\b|semiconductor|silicon",
                 r"validation|verification|test engineer|hardware test", r"robotics|robotic|control systems|controls engineer|systems integration",
                 r"power electronics|power systems|\brf\b|radio frequency")


def legacy_kept(title: str) -> bool:
    return bool(LEGACY_INTERNSHIP.search(title)) and any(re.search(p, title, re.I) for p in LEGACY_FIELDS)


def main(db_path, report_path):
    store = Store(db_path)
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    now = report["finished"]
    ok = [b for b in report["boards"] if b["success"]]
    failed = [b for b in report["boards"] if not b["success"]]
    rows = []
    for record in store.jobs():
        job = replace(Job(**json.loads(record["payload"])), source_status=record["availability"], checked_at=record["checked_at"])
        rows.append((job, decide(job, Profile(), Filters(30), now)))
    relevant = [(j, d) for j, d in rows if d.eligible]
    rescued = [(j, d) for j, d in relevant if not legacy_kept(j.title)]
    d = report["diagnostics"]
    out = [f"# Live discovery report ({now[:16]} UTC)", "",
           f"- Employer boards checked successfully: **{len(ok)} / {len(report['boards'])}**",
           f"- Public listings fetched: **{d['listed']}**",
           f"- Internship/co-op titles kept and stored: **{d['retained']}** (rejected: {d['rejected_not_internship']} not internships, {d['rejected_non_technical']} non-technical internships)",
           f"- Relevant to electrical/embedded/hardware/FPGA/robotics/power (term, U.S. location, hard requirements): **{len(relevant)}**",
           f"- Relevant openings the old title filter would have discarded: **{len(rescued)}**",
           f"- HTTP requests: {report.get('http_requests')}, cache hits: {report.get('http_cache_hits')}", "",
           "Profile used: blank (no personal data), Spring/Summer 2027, any publication date. Judged relevant does not mean you are eligible; that needs your verified profile.", "",
           "## Not relevant, by reason", ""]
    out += [f"- {reason}: {count}" for reason, count in Counter(r.split(":")[0] for _, dec in rows for r in dec.reasons).most_common()]
    out += ["", "## Relevant openings the previous code would have rejected", "", "| Company | Title | Specialization (basis) | Location | Published |", "| --- | --- | --- | --- | --- |"]
    from hunter.filtering import classify_discipline
    for job, dec in sorted(rescued, key=lambda x: (x[0].company, x[0].title)):
        basis = classify_discipline(job.title, job.description)[1]
        out.append(f"| {job.company} | {job.title} | {dec.discipline} ({basis[:70]}) | {job.location[:40]} | {(job.published_at or 'unknown')[:10]} |")
    out += ["", "## All relevant openings by employer", ""]
    for company, count in Counter(j.company for j, _ in relevant).most_common():
        out.append(f"- {company}: {count}")
    out += ["", "## Boards that failed", ""] + [f"- {b['employer']} ({b['source']}:{b['board']}): {b['error'][:160]}" for b in failed] or ["- none"]
    print("\n".join(out))


if __name__ == "__main__":
    main(*sys.argv[1:3])
