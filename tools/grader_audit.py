"""Measure requirement extraction and matching on real postings.

    python tools/grader_audit.py data/demo/discovery.sqlite3 [master.docx]

For every relevant stored posting: how many requirements were found and with
what priority, whether the qualification headings were recognised, and which
common engineering terms in the posting the alias table does not know. With a
master resume, also the share of requirements demonstrated / listed only /
not on the resume.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ats.matching import extract_requirements, match_requirement, parse_resume, skills_in  # noqa: E402
from hunter.filtering import decide  # noqa: E402
from hunter.models import Filters, Job, Profile  # noqa: E402
from hunter.storage import Store  # noqa: E402

HEADING = re.compile(r"^\s*(?:[A-Z][A-Za-z'’ &/,-]{2,60}|[A-Z' ’&/,-]{4,60}):?\s*$")
# Engineering vocabulary to check coverage against (deliberately independent of ALIASES).
VOCAB = ["schematic capture", "schematics", "pcb layout", "board layout", "bring-up", "bring up", "logic analyzer", "spectrum analyzer", "network analyzer",
         "microcontroller", "mcu", "embedded c", "embedded linux", "hdl", "ecad", "orcad", "cadence", "allegro", "spice", "pspice", "soldering", "rework",
         "bluetooth", "ble", "ethernet", "pcie", "ddr", "wi-fi", "wifi", "arm cortex", "ros", "pid", "control systems", "plc", "ladder logic",
         "motor control", "bldc", "battery management", "bms", "high voltage", "harness", "rf", "antenna", "dsp", "signal processing", "vivado",
         "quartus", "modelsim", "questa", "uvm", "test automation", "emi", "emc", "imu", "lab equipment", "test equipment", "power supplies",
         "serial communication", "communication protocols", "scripting", "version control", "c#", "rust", "bash", "kicad", "altium", "python",
         "matlab", "verilog", "vhdl", "systemverilog", "fpga", "oscilloscope", "multimeter", "i2c", "spi", "uart", "can bus", "rtos", "linux", "git"]


def main(db_path, master_path=None):
    store = Store(db_path)
    master = None
    if master_path:
        from ats.extraction import extract_document
        master = parse_resume(extract_document(Path(master_path).read_bytes(), Path(master_path).name).text)
    stats, unknown, statuses, headings = Counter(), Counter(), Counter(), Counter()
    for record in store.jobs():
        job = replace(Job(**json.loads(record["payload"])), source_status=record["availability"])
        if not decide(job, Profile(), Filters(30), "2026-10-23T21:00:00+00:00").eligible or len(job.description.split()) < 40:
            continue
        stats["postings"] += 1
        reqs = extract_requirements(job.description)
        prio = Counter(r.priority for r in reqs if r.kind != "eligibility")
        stats["requirements"] += sum(prio.values())
        stats["required"] += prio["required"]
        stats["preferred"] += prio["preferred"]
        stats["skill_reqs"] += sum(r.kind == "skill" for r in reqs)
        if prio["required"] == 0:
            stats["postings_with_no_required"] += 1
        for line in job.description.splitlines():
            if HEADING.match(line) and len(line.split()) <= 6 and re.search(r"qualif|requir|need|looking|preferred|bonus|nice|plus|skills|you (?:have|bring)|about you", line, re.I):
                headings[line.strip().rstrip(":").casefold()] += 1
        known = {s.casefold() for s in skills_in(job.description)}
        low = job.description.casefold()
        for term in VOCAB:
            if re.search(r"(?<![\w+#])" + re.escape(term) + r"(?![\w+#])", low) and not skills_in(term):
                unknown[term] += 1
        if master:
            for r in reqs:
                if r.kind in ("skill", "responsibility"):
                    statuses[match_requirement(r, master).status] += 1
    print(json.dumps(stats, indent=1))
    print("requirement headings seen (top 25):", headings.most_common(25))
    print("terms in postings the alias table does not know:", unknown.most_common(40))
    if master:
        total = sum(statuses.values())
        print("match statuses:", {k: f"{v} ({100 * v / total:.0f}%)" for k, v in statuses.items()})


if __name__ == "__main__":
    main(*sys.argv[1:3])
