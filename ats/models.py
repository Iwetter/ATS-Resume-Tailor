from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json


def digest(value) -> str:
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return sha256(value).hexdigest()


@dataclass(frozen=True)
class Extracted:
    text: str
    kind: str
    pages: int | None = None
    warnings: tuple[str, ...] = ()
    min_font: float | None = None


@dataclass(frozen=True)
class Evidence:
    id: str
    text: str
    kind: str  # header, section, entry, bullet, skill, text
    section: str
    group: str = ""


@dataclass(frozen=True)
class Fact:
    id: str
    source_id: str
    kind: str
    value: str


@dataclass(frozen=True)
class Resume:
    items: tuple[Evidence, ...]
    facts: tuple[Fact, ...]
    source_hash: str

    @property
    def text(self) -> str:
        return "\n".join(x.text for x in self.items)

    @property
    def by_id(self) -> dict[str, Evidence]:
        return {x.id: x for x in self.items}


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    kind: str = "skill"
    priority: str = "required"
    terms: tuple[str, ...] = ()
    mode: str = "all"
    assertion: str = "unknown"
    note: str = ""


@dataclass(frozen=True)
class Match:
    requirement_id: str
    status: str
    evidence_ids: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class Analysis:
    readability: float
    alignment: float
    components: dict[str, float]
    checks: dict[str, float]
    matches: tuple[Match, ...]
    warnings: tuple[str, ...]
    improvements: tuple[str, ...]
    deductions: tuple[tuple[str, float], ...] = ()   # (requirement id, alignment points lost), largest first
    unmeasured: tuple[str, ...] = ()                 # components with no requirements in this posting
    breakdown: dict = field(default_factory=dict)    # category -> {status: count}


@dataclass(frozen=True)
class Change:
    source_ids: tuple[str, ...]
    original_text: str
    proposed_text: str
    justification: str


@dataclass(frozen=True)
class Validation:
    status: str  # unchanged, review, rejected
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportBundle:
    pdf: bytes
    docx: bytes
    pages: int
    text: str
    font_size: float
