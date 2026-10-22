"""User-operated board searches and local pasted-listing import. No scraping."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import ipaddress
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from ats.models import digest
from .models import Job, canonical_url, timestamp, utcnow
from .sources import plain


def search_links(keywords, location="United States", days=7):
    keywords, location = keywords.strip(), location.strip() or "United States"
    query = lambda **kw: urlencode(kw)
    return {
        "LinkedIn": "https://www.linkedin.com/jobs/search/?" + query(keywords=keywords, location=location, f_TPR=f"r{days*86400}"),
        "Indeed": "https://www.indeed.com/jobs?" + query(q=keywords, l=location, fromage=days),
        "Workday employers": "https://www.google.com/search?" + query(q=f"site:myworkdayjobs.com {keywords} {location}"),
        "Handshake": "https://app.joinhandshake.com/stu/postings?" + query(text=keywords),
        "Glassdoor": "https://www.glassdoor.com/Job/jobs.htm?" + query(sc_keyword=keywords),
        "USAJOBS": "https://www.usajobs.gov/Search/Results?" + query(k=keywords, l=location),
        "Built In": "https://builtin.com/jobs?" + query(search=keywords),
        "ZipRecruiter": "https://www.ziprecruiter.com/jobs-search?" + query(search=keywords, location=location),
        "Dice": "https://www.dice.com/jobs?" + query(q=keywords, location=location, countryCode="US"),
    }


def public_link(url):
    clean = canonical_url(url.strip())
    parsed = urlsplit(clean)
    host = parsed.hostname or ""
    if parsed.port not in {None, 443} or "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("Use a public job-board or employer HTTPS URL.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return clean
    raise ValueError("Use the public job-board domain, not an IP address.")


def source_for(url):
    host = urlsplit(url).hostname or ""
    for domain, label in (("linkedin.com", "linkedin"), ("indeed.com", "indeed"), ("myworkdayjobs.com", "workday"),
                           ("joinhandshake.com", "handshake"), ("glassdoor.com", "glassdoor"), ("usajobs.gov", "usajobs"),
                           ("builtin.com", "builtin"), ("ziprecruiter.com", "ziprecruiter"), ("dice.com", "dice"), ("workable.com", "workable")):
        if host == domain or host.endswith("." + domain):
            return label
    return "manual"


def import_listing(store, *, origin_url, company, title, location, description, application_url="", published_date="", confirmed=False):
    """Never fetch the URL, infer a publication date, or claim API verification."""
    origin = public_link(origin_url)
    if confirmed and (not application_url.strip() or source_for(public_link(application_url)) in {"linkedin", "indeed", "glassdoor", "handshake", "ziprecruiter", "dice", "builtin"}):
        raise ValueError("Provide the employer's application URL before confirming availability.")
    parsed, source = urlsplit(origin), source_for(origin)
    # Ignore common board tracking parameters for repeat-import identity.
    if source == "linkedin":
        match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)", parsed.path)
        if match:
            origin = "https://www.linkedin.com/jobs/view/" + match[1]
    if source == "indeed":
        job_id = parse_qs(parsed.query).get("jk", [None])[0]
        if job_id:
            origin = "https://www.indeed.com/viewjob?" + urlencode({"jk": job_id})
    company, title, location, description = map(plain, (company, title, location, description))
    if not company or not title or not location:
        raise ValueError("Enter the employer, exact job title and location from the listing.")
    if len(description.split()) < 15 or len(description) > 45000:
        raise ValueError("Paste the job description (at least 15 words, at most 45,000 characters).")
    if len(company) > 300 or len(title) > 500 or len(location) > 500:
        raise ValueError("Employer, title or location is too long.")
    published = None
    if published_date.strip():
        try:
            day = datetime.strptime(published_date.strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("Use YYYY-MM-DD for the posted date, or leave it blank if unknown.") from exc
        if day > datetime.now(timezone.utc).date():
            raise ValueError("A posted date cannot be in the future. Leave it blank if uncertain.")
        published = timestamp(day.isoformat())
    at = utcnow()
    job = Job(source, "imported", digest(origin)[:24], company, title, location,
              public_link(application_url) if application_url.strip() else origin, description,
              published_at=published, publication_basis="User-reported posted date; not independently verified" if published else "Posting date unknown",
              checked_at=at if confirmed else None, source_status="user-confirmed" if confirmed else "unknown",
              imported_at=at, origin_url=origin,
              availability_basis="User confirmed the employer application page is accepting applications" if confirmed else "Pasted listing; availability unverified")
    key, fresh = store.upsert(job, at)
    store.save_import(key, origin, asdict(job))
    return key, fresh
