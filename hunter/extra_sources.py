"""Public XML feeds and Workday sitemap/JobPosting structured data.

No Workday internal search endpoint, authentication or browser automation.
Sitemaps are discovery hints, so their omissions never close saved jobs.
"""
from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from .filtering import potential_title
from .http import SourceError
from .models import Job, Snapshot, canonical_url, timestamp
from .sources import plain, compensation, employer_from_url


def xml_root(text):
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
        raise SourceError("XML declarations with document types/entities are unsupported.")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise SourceError("Source did not return a valid XML feed.") from exc


def tag(element):
    return element.tag.rsplit("}", 1)[-1]


def value(element, name):
    return (element.findtext(name) or "").strip()


def postings(data):
    if isinstance(data, list):
        for item in data:
            yield from postings(item)
    elif isinstance(data, dict):
        kind = data.get("@type", [])
        if "JobPosting" in ([kind] if isinstance(kind, str) else kind):
            yield data
        yield from postings(data.get("@graph", []))


def workday_job(employer, url, html, checked):
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            found.extend(postings(json.loads(script.string or script.get_text())))
        except (TypeError, ValueError):
            continue
    if len(found) != 1:
        raise SourceError("Public job page has no unique JobPosting structured record; paste the description instead.")
    row = found[0]
    title, description = plain(row.get("title")), plain(row.get("description"))
    if not title or not description:
        raise SourceError("Public job page is missing its title or description.")
    places = row.get("jobLocation") or []
    places = [places] if isinstance(places, dict) else places
    locations, countries = [], []
    for place in places:
        address = place.get("address") or {}
        if isinstance(address, str):
            locations.append(address)
            continue
        country = address.get("addressCountry", "")
        country = country.get("name", "") if isinstance(country, dict) else country
        locations.append(", ".join(str(x) for x in (address.get("addressLocality"), address.get("addressRegion"), country) if x))
        if country:
            countries.append(country)
    employment = row.get("employmentType", "")
    return Job("workday", employer.board, canonical_url(url), employer.name, title,
               "; ".join(dict.fromkeys(locations)) or plain(row.get("jobLocationType")), canonical_url(url), description,
               country=";".join(dict.fromkeys(countries)), employment_type=", ".join(employment) if isinstance(employment, list) else employment,
               published_at=timestamp(row.get("datePosted")), publication_basis="Workday public JobPosting datePosted" if timestamp(row.get("datePosted")) else "Posting date unknown",
               expires_at=timestamp(row.get("validThrough")), checked_at=checked, compensation=compensation(row, description),
               availability_basis="Public Workday job page contains JobPosting structured data")


def fetch_public_board(employer, client, max_details=60):
    parsed = employer_from_url(employer.name, employer.career_url)
    if parsed.source != employer.source or parsed.board != employer.board:
        raise SourceError("Employer board and its official career URL do not agree.")
    checks, warnings, jobs, ids = [], [], [], set()
    any_cached = False

    def get(url):
        nonlocal any_cached
        text, checked, cached = client.text(url)
        checks.append(checked)
        any_cached |= cached
        return text, checked

    if employer.source == "workday":
        base = parsed.career_url
        host = urlsplit(base).hostname
        pending, visited, pages = [base + "/siteMap.xml"], set(), []
        # Public sitemaps can be capped by the provider, even without an index.
        warnings.append("Workday public sitemap coverage may be partial. Missing jobs are never marked closed; use the employer site to search all openings.")
        while pending and len(visited) < 5:
            sitemap = pending.pop(0)
            if sitemap in visited:
                continue
            visited.add(sitemap)
            text, checked = get(sitemap)
            root = xml_root(text)
            if tag(root) not in {"sitemapindex", "urlset"}:
                raise SourceError("Workday did not return a public sitemap.")
            for node in root:
                loc = next((c.text for c in node if tag(c) == "loc"), None)
                if not loc:
                    continue
                loc = canonical_url(loc)
                if urlsplit(loc).hostname != host or not urlsplit(loc).path.startswith(urlsplit(base).path + "/"):
                    warnings.append("A sitemap link outside the configured employer site was skipped.")
                    continue
                if tag(root) == "sitemapindex":
                    if loc not in visited and loc not in pending:
                        pending.append(loc)
                elif "/job/" in urlsplit(loc).path:
                    ids.add(loc)
                    if potential_title(re.sub(r"[-_]", " ", unquote(urlsplit(loc).path.rsplit("/", 1)[-1]))):
                        pages.append(loc)
        if pending:
            warnings.append("Sitemap request limit reached.")
        pages = list(dict.fromkeys(pages))
        if hasattr(client, "store"):
            pages.sort(key=lambda url: client.store.cache_get("text:" + url) is not None)
        if len(pages) > max_details:
            warnings.append("Workday detail limit reached; unvisited pages take priority on a later scan.")
        for url in pages[:max_details]:
            try:
                html, checked = get(url)
                job = workday_job(employer, url, html, checked)
                if potential_title(job.title):
                    jobs.append(job)
            except (SourceError, ValueError, TypeError) as exc:
                warnings.append(f"Public job detail unavailable for {url}: {exc}")
        return Snapshot(tuple(jobs), frozenset(ids), min(checks), False, any_cached, tuple(dict.fromkeys(warnings)))

    personio = employer.source == "personio"
    base = parsed.career_url
    xml, checked = get(base + ("/xml?language=en" if personio else "/api/offers.xml"))
    root = xml_root(xml)
    if tag(root) not in ({"workzag-jobs"} if personio else {"hash", "offers", "jobs"}):
        raise SourceError("Unexpected public XML feed schema; no closures inferred.")
    if not personio and tag(root) == "hash" and root.find("offers") is None:
        raise SourceError("Recruitee response is missing its offers collection; no closures inferred.")
    rows = root.findall(".//position") if personio else root.findall(".//offer") or root.findall(".//job")
    for row in rows:
        if not personio and value(row, "status") not in {"", "published"}:
            continue
        source_id = value(row, "id") or value(row, "reference")
        title = value(row, "name" if personio else "title")
        if not source_id or not title:
            raise SourceError("Public feed has an incomplete job identity; no closures inferred.")
        ids.add(source_id)
        if not potential_title(title):
            continue
        if personio:
            description = "\n".join(plain(value(section, "name")) + "\n" + plain(value(section, "value")) for section in row.findall("jobDescriptions/jobDescription"))
            location = "; ".join(filter(None, [value(row, "office")] + [x.text for x in row.findall("additionalOffices/office") if x.text]))
            url = base + "/job/" + source_id
            country, published, basis = "", None, "Posting date unknown"
        else:
            description = "\n".join(filter(None, (plain(value(row, "description_requirements")), plain(value(row, "description")), plain(value(row, "requirements")))))
            country = value(row, "country-code") or value(row, "country")
            location = ", ".join(filter(None, (value(row, "city"), value(row, "state-name") or value(row, "state"), country)))
            url = value(row, "careers-url") or value(row, "url") or (base + "/o/" + value(row, "slug") if value(row, "slug") else "")
            # A feed-add date is not necessarily the job's first publication.
            published = timestamp(value(row, "published_at") or value(row, "published-at"))
            basis = "Recruitee explicit published_at" if published else "Posting date unknown"
        if not url:
            raise SourceError("Public feed did not supply a usable job URL.")
        jobs.append(Job(employer.source, employer.board, source_id, employer.name, plain(title), location,
                        canonical_url(url), description, country=country, employment_type=value(row, "employmentType") or value(row, "employment-type"),
                        published_at=published, publication_basis=basis, checked_at=checked,
                        compensation=compensation({}, description), availability_basis="Published job in official public XML feed"))
    return Snapshot(tuple(jobs), frozenset(ids), min(checks), True, any_cached, tuple(warnings))
