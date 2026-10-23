"""End-to-end tailoring of a real master DOCX for one job, with every check written to a report.

    python tools/tailoring_demo.py MASTER.docx JOB.txt OUT_DIR [PROPOSALS.json]

Without PROPOSALS it lists the master's bullets and IDs. PROPOSALS is the
same JSON the Claude request returns ({"changes": [...]}); each change goes
through the evidence verifier, and only "review" (supported) changes are
applied, as if approved. Output: tailored DOCX/PDF, the original's PDF, and
report.md (scores, verifier verdicts, word diffs, layout and format checks).
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ats.docx_tailor import docx_to_pdf, export_tailored  # noqa: E402
from ats.extraction import extract_document  # noqa: E402
from ats.matching import extract_requirements, parse_resume, rank_projects, select_projects  # noqa: E402
from ats.rewriting import apply_changes, parse_changes, validate_change  # noqa: E402
from hunter.models import Job  # noqa: E402
from hunter.service import order_skills, score_job  # noqa: E402


def main(master_path, job_path, out_dir, proposals_path=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    original = Path(master_path).read_bytes()
    master = parse_resume(extract_document(original, Path(master_path).name).text)
    job_text = Path(job_path).read_text(encoding="utf-8")
    if not proposals_path:
        for item in master.items:
            if item.kind in ("bullet", "entry", "skill"):
                print(item.id, item.kind, item.section, "|", item.text)
        return
    job = Job("manual", "demo", "1", "Demo", job_text.splitlines()[0], "", "https://example.com", job_text)
    requirements = extract_requirements(job_text)
    ranked = [g for g, _, _ in rank_projects(master, requirements)]
    changes = parse_changes(json.loads(Path(proposals_path).read_text(encoding="utf-8")))
    verdicts = [(c, validate_change(c, master)) for c in changes]
    approved = {c.source_ids[0] for c, v in verdicts if v.status == "review"}
    chosen = ranked[:3]
    needed = {master.by_id[i].group for i in approved if master.by_id[i].section == "Projects"}
    chosen = list(dict.fromkeys([g for g in chosen if g in needed] + [g for g in chosen if g not in needed]))[:max(3, len(needed))]
    while True:
        final = apply_changes(master, order_skills(select_projects(master, chosen), requirements), changes, approved)
        result = export_tailored(original, master, final)
        if result["pdf"] and result["pages"] > 1 and len(chosen) > 1:
            chosen = chosen[:-1]
            continue
        break
    before, after = score_job(job, master), score_job(job, final)
    (out / "tailored.docx").write_bytes(result["docx"])
    if result["pdf"]:
        (out / "tailored.pdf").write_bytes(result["pdf"])
    original_pdf, _ = docx_to_pdf(original)
    if original_pdf:
        (out / "original.pdf").write_bytes(original_pdf)
    from difflib import SequenceMatcher

    def diff(a, b):
        x, y = a.split(), b.split()
        parts = []
        for op, i, j, k, l in SequenceMatcher(a=x, b=y).get_opcodes():
            if op == "equal":
                parts.append(" ".join(x[i:j]))
            else:
                if i != j:
                    parts.append("~~" + " ".join(x[i:j]) + "~~")
                if k != l:
                    parts.append("**" + " ".join(y[k:l]) + "**")
        return " ".join(parts)

    r = result["report"]
    lines = [f"# Tailoring demonstration: {job.title}", "",
             f"- Fit estimate: **{before['fit']} → {after['fit']}** (internal heuristic, not an employer ATS score)",
             f"- Components before: {before['components']}", f"- Components after: {after['components']}",
             f"- Output: your DOCX edited in place, rendered by **{result['converter']}**, **{result['pages']} page(s)**",
             f"- Lines matched to DOCX paragraphs: {r['mapped']}/{r['items']} · reworded: {len(r['edited'])} · removed: {len(r['removed'])} · projects reordered: {bool(r['moved'])}",
             f"- Formatting changes in edited paragraphs: {len(r['format_changes'])} · margins/page size unchanged: {'Page size or margins changed.' not in result['issues']}",
             f"- Issues: {result['issues'] or 'none'}", "",
             f"Projects kept (job order): {', '.join(master.by_id[next(x.id for x in master.items if x.group == g and x.kind == 'entry')].text.split('|')[0].strip() for g in chosen)}", "",
             "## Proposed rewrites and the evidence verifier's verdicts", ""]
    for change, verdict in verdicts:
        lines += [f"### {verdict.status.upper()}: {change.source_ids[0]}", "", diff(change.original_text, change.proposed_text), "",
                  *[f"- {reason}" for reason in verdict.reasons], ""]
    lines += ["## Every line that changed in the document", ""]
    for item in final.items:
        if item.id in master.by_id and master.by_id[item.id].text != item.text:
            lines += ["- " + diff(master.by_id[item.id].text, item.text)]
    lines += ["", "## Removed for space or relevance", ""] + [f"- {master.by_id[i].text}" for i in r["removed"]]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:12]))


if __name__ == "__main__":
    main(*sys.argv[1:5])
