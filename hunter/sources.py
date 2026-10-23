"""Adapters for four documented, unauthenticated public posting APIs."""
from __future__ import annotations

from dataclasses import replace
from html import unescape
import json
import re
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

from .filtering import potential_title
from .http import SourceError, supported_host
from .models import Employer, Job, Snapshot, canonical_url, timestamp


def plain(value) -> str:
    return BeautifulSoup(unescape(str(value or "")), "html.parser").get_text("\n", strip=True)


def compensation(data, description):
    value = data.get("compensation") or data.get("salaryRange") or data.get("pay_input_ranges")
    if value:
        return json.dumps(value, ensure_ascii=False)
    lines = [s.strip() for s in description.splitlines() if re.search(r"\$\s?\d|USD\s?\d|\d\s?USD", s)]
    return "\n".join(lines[:4])


def employer_from_url(name, url) -> Employer:
    p = urlparse(url)
    host, parts = (p.hostname or "").lower(), p.path.strip("/").split("/")
    if p.scheme != "https" or p.username or p.password or p.port not in {None, 443}:
        raise ValueError("Use an official HTTPS career-board URL without credentials.")
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        source = "greenhouse"
        board = parse_qs(p.query).get("for", [parts[0]])[0]
    elif host == "jobs.lever.co":
        source, board = "lever", parts[0]
    elif host == "jobs.ashbyhq.com":
        source, board = "ashby", parts[0]
    elif host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"}:
        source, board = "smartrecruiters", parts[0]
    elif supported_host(host) and host.endswith(".myworkdayjobs.com"):
        source = "workday"
        if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
            parts = parts[1:]
        if not parts or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]):
            raise ValueError("Use the Workday employer career-site URL, including its site name.")
        board = host.split(".")[0] + "-" + host.split(".")[1] + "-" + parts[0]
        url = f"https://{host}/{parts[0]}"
    elif supported_host(host) and host.endswith(".recruitee.com"):
        source, board = "recruitee", host.split(".")[0]
        url = f"https://{host}"
    elif supported_host(host) and ".jobs.personio." in host:
        source, board = "personio", host.replace(".", "-")
        url = f"https://{host}"
    else:
        raise ValueError("Automatic scanning supports Greenhouse, Lever, Ashby, public SmartRecruiters, Workday public sitemaps, Recruitee and Personio feeds. Other career pages can be kept as manual bookmarks or listings imported with pasted text.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", board or "") or not name.strip():
        raise ValueError("An employer name and valid board identifier are required.")
    return Employer(name.strip(), source, board, canonical_url(url))


def normalize(source: str, employer: Employer, row: dict, checked: str) -> Job:
    if source == "greenhouse":
        description = plain(row.get("content"))
        return Job(source, employer.board, str(row["id"]), employer.name, row["title"], row.get("location", {}).get("name", ""), row["absolute_url"], description,
                   published_at=timestamp(row.get("first_published")), publication_basis="Greenhouse first_published" if row.get("first_published") else "Posting date unknown",
                   updated_at=timestamp(row.get("updated_at")), expires_at=timestamp(row.get("application_deadline")), checked_at=checked, compensation=compensation(row, description))
    if source == "lever":
        categories = row.get("categories") or {}
        parts = [row.get("descriptionPlain") or plain(row.get("description"))]
        for entry in row.get("lists", []):
            parts += [entry.get("text", ""), plain(entry.get("content"))]
        parts += [row.get("additionalPlain") or plain(row.get("additional"))]
        description = "\n".join(s for s in parts if s)
        locations = row.get("categories", {}).get("allLocations") or [categories.get("location", "")]
        return Job(source, employer.board, str(row["id"]), employer.name, row["text"], "; ".join(locations), row.get("hostedUrl") or row["applyUrl"], description,
                   country=categories.get("country", ""), employment_type=categories.get("commitment", ""),
                   # createdAt is creation, not a verified publication date.
                   updated_at=timestamp(row.get("updatedAt")), checked_at=checked, compensation=compensation(row, description))
    if source == "ashby":
        description = row.get("descriptionPlain") or plain(row.get("descriptionHtml"))
        primary = row.get("address", {}).get("postalAddress", row.get("address", {})) or {}
        secondary = row.get("secondaryLocations") or []
        countries = [primary.get("addressCountry", "")] + [x.get("address", {}).get("addressCountry", "") for x in secondary]
        return Job(source, employer.board, str(row["id"]), employer.name, row["title"], "; ".join([row.get("location", "")] + [x.get("location", "") for x in secondary]), row.get("jobUrl") or row["applyUrl"], description,
                   country=";".join(dict.fromkeys(x for x in countries if x)), employment_type=row.get("employmentType", ""), published_at=timestamp(row.get("publishedAt")),
                   publication_basis="Ashby publishedAt (last published)" if row.get("publishedAt") else "Posting date unknown", checked_at=checked,
                   source_status="open" if row.get("isListed", True) else "closed", compensation=compensation(row, description))
    if source == "smartrecruiters":
        loc = row.get("location") or {}
        sections = row.get("jobAd", {}).get("sections", {})
        description = "\n".join(plain(sections[k].get("title", "")) + "\n" + plain(sections[k].get("text", "")) for k in ("jobDescription", "qualifications", "additionalInformation") if k in sections)
        return Job(source, employer.board, str(row["id"]), employer.name, row["name"], ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x),
                   row.get("postingUrl") or row.get("applyUrl") or f"https://jobs.smartrecruiters.com/{employer.board}/{row['id']}", description,
                   country=loc.get("country", ""), employment_type=row.get("typeOfEmployment", {}).get("label", ""), published_at=timestamp(row.get("releasedDate")),
                   publication_basis="SmartRecruiters releasedDate" if row.get("releasedDate") else "Posting date unknown", checked_at=checked, compensation=compensation(row, description))
    raise SourceError("Unsupported provider.")


def fetch_board(employer: Employer, client, max_details=60) -> Snapshot:
    if employer.source in {"workday", "recruitee", "personio"}:
        from .extra_sources import fetch_public_board
        return fetch_public_board(employer, client, max_details)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", employer.board):
        raise SourceError("Invalid board identifier.")
    source, board = employer.source, employer.board
    rows, all_ids, checks, warnings = [], set(), [], []
    any_cached, complete = False, True

    def get(url, force=False):
        nonlocal any_cached
        data, checked, cached = client.json(url, force=True) if force else client.json(url)
        checks.append(checked)
        any_cached = any_cached or cached
        return data, checked

    if source == "greenhouse":
        data, checked = get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceError("Greenhouse response schema was unexpected.")
        rows = data["jobs"]
        complete = data.get("meta", {}).get("total", len(rows)) == len(rows)
    elif source in {"lever", "smartrecruiters"}:
        seen_pages = set()
        for page in range(100):
            offset = page * 100
            url = f"https://api.lever.co/v0/postings/{board}?mode=json&limit=100&skip={offset}" if source == "lever" else f"https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100&offset={offset}"
            data, checked = get(url)
            entries = data if source == "lever" else data.get("content") if isinstance(data, dict) else None
            if not isinstance(entries, list):
                raise SourceError("Source pagination response schema was unexpected.")
            signature = tuple(str(x["id"]) for x in entries)
            if signature and signature in seen_pages:
                raise SourceError("Source repeated a page; scan is incomplete and closure detection was skipped.")
            seen_pages.add(signature)
            rows.extend(entries)
            if len(entries) < 100 or (source == "smartrecruiters" and len(rows) >= data.get("totalFound", 10**9)):
                break
        else:
            complete = False
            warnings.append("Pagination limit reached; missing jobs were not marked closed.")
    elif source == "ashby":
        data, checked = get(f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceError("Ashby response schema was unexpected.")
        rows = data["jobs"]
    else:
        raise SourceError("Unsupported source; use a documented public board or manual bookmark.")
    all_ids = {str(x["id"]) for x in rows if x.get("isListed", True)}
    if source in {"greenhouse", "smartrecruiters"} and hasattr(client, "store"):
        def detail_cached(row):
            detail_url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{row['id']}" if source == "greenhouse" else f"https://api.smartrecruiters.com/v1/companies/{board}/postings/{row['id']}"
            return client.store.cache_get(detail_url) is not None
        # Previously unvisited descriptions take priority over repeat details.
        rows.sort(key=detail_cached)
    jobs, details = [], 0
    for row in rows:
        if row.get("isListed") is False:
            continue
        title = row.get("title", row.get("text", row.get("name", "")))
        if not potential_title(title):
            continue
        job_checked = min(checks)
        if source in {"greenhouse", "smartrecruiters"}:
            if details >= max_details:
                warnings.append("Detail request limit reached; some descriptions are pending a later scan.")
                job = normalize(source, employer, row, job_checked)
                jobs.append(replace(job, source_status="unknown"))
                continue
            details += 1
            url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{row['id']}" if source == "greenhouse" else f"https://api.smartrecruiters.com/v1/companies/{board}/postings/{row['id']}"
            # A listing timestamp newer than the cached detail means the
            # posting changed; refresh that one detail instead of waiting
            # for the long detail TTL. This is an update, never a new job.
            stale = False
            if hasattr(client, "store") and row.get("updated_at"):
                cached_detail = client.store.cache_get(url)
                stale = bool(cached_detail and isinstance(cached_detail[0], dict) and cached_detail[0].get("updated_at") != row.get("updated_at"))
            try:
                detail, job_checked = get(url, force=stale)
                row = {**row, **detail}
            except SourceError as exc:
                warnings.append(f"Description unavailable for {row['id']}: {exc}")
                jobs.append(replace(normalize(source, employer, row, job_checked), source_status="unknown"))
                continue
        job = normalize(source, employer, row, job_checked)
        canonical_url(job.url)  # Validate before accepting any official link.
        jobs.append(job)
    return Snapshot(tuple(jobs), frozenset(all_ids), min(checks), complete, any_cached, tuple(dict.fromkeys(warnings)))
