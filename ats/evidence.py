"""Evidence verification for tailored bullets.

Replaces the earlier lexical gate (any new word, removed word or changed verb
was rejected, so almost every real rewrite failed). A rewrite may now reword,
condense, reorder and emphasise freely. It is **rejected** only when it asserts
something the evidence does not support:

* a number, date, metric or unit that is not in the evidence;
* a technology, tool or skill not present in the source bullet, its own
  project/role entry, or an approved evidence-library entry for it;
* a stage of work the evidence does not show (designed → fabricated,
  assembled, tested, deployed or led), so research PCB work keeps its exact
  designed/fabricated/assembled/tested status;
* dropping "assisted/supported/contributed" or other qualifiers (claiming
  sole ownership), or changing whether work is finished or planned/ongoing.

Everything else that deserves a human look becomes a **risk flag** on a
"review" result: omitted details, new domain terms, large length changes.
"""
from __future__ import annotations

import re

from .matching import METRIC, clean, skills_in, tokens
from .models import Change, Resume, Validation

# Stages of engineering work. A rewrite may not claim a stage absent from its evidence.
STAGES = {
    "design": r"\b(?:design(?:ed|ing)?|architect(?:ed)?|model(?:ed|led)|simulat(?:ed|ion)|schematic|laid out|layout|specified|drafted)\b",
    "fabrication": r"\b(?:fabricat(?:ed|ion)|manufactur(?:ed|ing)|machin(?:ed|ing)|printed|etched|ordered boards)\b",
    "assembly": r"\b(?:assembl(?:ed|y)|solder(?:ed|ing)|populated|built|constructed|wired|integrated)\b",
    "testing": r"\b(?:test(?:ed|ing)|validat(?:ed|ion)|verif(?:ied|ication)|characteriz(?:ed|ation)|debugg(?:ed|ing)|measured|troubleshot|brought up|bring-up)\b",
    "deployment": r"\b(?:deploy(?:ed|ment)|shipped|launched|released|in production|delivered to)\b",
    "leadership": r"\b(?:led|lead|managed|supervised|directed|mentored|owned|headed|spearheaded)\b",
    "publication": r"\b(?:published|patent(?:ed)?|awarded|presented at)\b",
}
ASSISTANCE = r"\b(?:assist(?:ed|ing)?|help(?:ed|ing)?|support(?:ed|ing)?|contribut(?:ed|ing)|collaborat(?:ed|ing)|co-?(?:designed|developed|authored)|with (?:a|the) team|as part of)\b"
UNFINISHED = r"\b(?:planned|planning|proposed|ongoing|in progress|currently|future|prototype stage|intend(?:ed)?|not yet|working toward)\b"
COMMON = set("a an the and or to of for in on at by with using through from that which as into via per under over across within while including".split())


def _stages(text: str) -> set[str]:
    return {name for name, pattern in STAGES.items() if re.search(pattern, text, re.I)}


def _numbers(text: str) -> set[str]:
    return {clean(m.group()).replace(" ", "") for m in METRIC.finditer(text)}


def evidence_for(item_id: str, master: Resume, library=()) -> list[str]:
    """The source bullet, its own entry (role/project header and sibling bullets), and approved library facts."""
    item = master.by_id[item_id]
    texts = [item.text] + [x.text for x in master.items if item.group and x.group == item.group and x.id != item.id]
    texts += [e["text"] for e in library if e.get("approved_at") and (not e.get("applies_to") or item_id in e["applies_to"] or item.group in e["applies_to"])]
    return texts


def verify_rewrite(change: Change, master: Resume, library=()) -> Validation:
    if len(change.source_ids) != 1:
        return Validation("rejected", ("Exactly one original bullet must support a change.",))
    source = master.by_id.get(change.source_ids[0])
    if not source or source.kind != "bullet" or source.text != change.original_text:
        return Validation("rejected", ("Source ID or original text does not match the immutable master.",))
    proposed = change.proposed_text.strip()
    if not proposed or "\n" in proposed or len(proposed) > 600:
        return Validation("rejected", ("A proposal must be one nonempty bullet under 600 characters.",))
    if proposed == source.text:
        return Validation("unchanged")
    evidence = evidence_for(source.id, master, library)
    corpus = "\n".join(evidence)
    hard, flags = [], []
    new_numbers = _numbers(proposed) - _numbers(corpus)
    if new_numbers:
        hard.append("Numbers or metrics not in the evidence: " + ", ".join(sorted(new_numbers)) + ".")
    new_skills = skills_in(proposed) - skills_in(corpus)
    if new_skills:
        hard.append("Technologies/skills not shown for this project or role: " + ", ".join(sorted(new_skills)) + ".")
    # A stage must appear somewhere in this entry's evidence; one borrowed from a sibling
    # bullet (condensing two lines of the same project) is allowed but flagged.
    new_stages = _stages(proposed) - _stages(corpus)
    if new_stages:
        hard.append("Claims a stage of work the evidence does not show: " + ", ".join(sorted(new_stages)) + ".")
    borrowed = _stages(proposed) - _stages(source.text)
    if re.search(ASSISTANCE, source.text, re.I) and not re.search(ASSISTANCE, proposed, re.I):
        hard.append("Drops the assisted/contributed qualifier, which would overstate your role.")
    if bool(re.search(UNFINISHED, source.text, re.I)) != bool(re.search(UNFINISHED, proposed, re.I)):
        hard.append("Changes whether the work is finished or still planned/ongoing.")
    if hard:
        return Validation("rejected", tuple(hard))
    if borrowed:
        flags.append("Combines work from another line of this entry (" + ", ".join(sorted(borrowed)) + "); confirm it describes the same work.")
    dropped = _numbers(source.text) - _numbers(proposed)
    if dropped:
        flags.append("Omits these figures (allowed for space, confirm nothing important is lost): " + ", ".join(sorted(dropped)) + ".")
    dropped_skills = skills_in(source.text) - skills_in(proposed)
    if dropped_skills:
        flags.append("No longer mentions: " + ", ".join(sorted(dropped_skills)) + ".")
    novel = {t for t in tokens(proposed) - tokens(corpus) - COMMON if len(t) > 3}
    if novel:
        flags.append("New wording to confirm is accurate: " + ", ".join(sorted(novel)[:12]) + ".")
    if len(proposed) > 1.35 * len(source.text) + 20:
        flags.append("Noticeably longer than the original; check the one-page layout.")
    return Validation("review", tuple(flags) or ("Supported by the stored evidence. Confirm the meaning before approval.",))
