from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import re

from ats.models import digest


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp(value) -> str | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value / (1000 if value > 10**11 else 1), timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("A job needs a public HTTP(S) application URL.")
    keep = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_") and k.lower() not in {"source", "gh_src", "lever-source", "ref"}]
    return urlunsplit(("https", p.netloc.lower(), p.path.rstrip("/"), urlencode(sorted(keep)), ""))


@dataclass(frozen=True)
class Employer:
    name: str
    source: str
    board: str
    career_url: str
    enabled: bool = True
    verified_at: str | None = None

    @property
    def key(self):
        return f"{self.source}:{self.board}"


@dataclass(frozen=True)
class Job:
    source: str
    board: str
    source_id: str
    company: str
    title: str
    location: str
    url: str
    description: str = ""
    country: str = ""
    employment_type: str = ""
    published_at: str | None = None
    publication_basis: str = "Posting date unknown"
    updated_at: str | None = None
    expires_at: str | None = None
    checked_at: str | None = None
    source_status: str = "open"
    compensation: str = ""
    imported_at: str | None = None
    origin_url: str = ""
    availability_basis: str = "Official public source"

    @property
    def key(self):
        return digest([self.source, self.board, self.source_id])[:24]

    @property
    def content_hash(self):
        return digest({k: v for k, v in asdict(self).items() if k not in {"checked_at"}})

    @property
    def secondary_key(self):
        return digest([re.sub(r"\W+", "", s.casefold()) for s in (self.company, self.title, self.location)])


@dataclass(frozen=True)
class Snapshot:
    jobs: tuple[Job, ...]
    all_ids: frozenset[str]
    checked_at: str
    complete: bool = True
    cached: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Profile:
    university: str = "Example University"
    degree: str = "B.S. Electrical Engineering"
    graduation: str = "2028-05"
    gpa: float = 3.50
    us_citizen: bool | None = None
    us_authorized: bool | None = None
    sponsorship_needed: bool | None = None


@dataclass(frozen=True)
class Filters:
    days: int = 7
    terms: tuple[str, ...] = ("Spring 2027", "Summer 2027")


@dataclass(frozen=True)
class Flag:
    requirement: str
    status: str
    explanation: str


@dataclass(frozen=True)
class Decision:
    eligible: bool
    discipline: str
    terms: tuple[str, ...]
    recency: str
    reasons: tuple[str, ...] = ()
    flags: tuple[Flag, ...] = ()
