"""Bounded in-memory document and job extraction; never runs an LLM."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlparse
from zipfile import ZipFile, BadZipFile

from bs4 import BeautifulSoup
from docx import Document
from docx.oxml.ns import qn
import pymupdf
import requests

from .models import Extracted

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 15


class InputError(ValueError):
    pass


def validate_text(text: str) -> str:
    if len(re.findall(r"[A-Za-zÀ-ž0-9]", text)) < 40:
        raise InputError("No usable resume text. Upload a text-based PDF or DOCX; scanned PDFs need OCR outside this app.")
    if text.count("\ufffd") > max(2, len(text) * .01):
        raise InputError("Text is garbled. Export a fresh text-based PDF or use DOCX.")
    return text


def extract_document(data: bytes, filename: str) -> Extracted:
    if not data:
        raise InputError("The document is empty or missing.")
    if len(data) > MAX_BYTES:
        raise InputError("Resume exceeds the 10 MB limit.")
    ext = Path(filename).suffix.lower()
    try:
        if ext == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise InputError("This file is not a valid PDF.")
            return _pdf(data)
        if ext == ".docx":
            return _docx(data)
    except InputError:
        raise
    except (Exception,) as exc:
        raise InputError("Cannot read this document. It may be damaged, encrypted, or incorrectly named.") from exc
    raise InputError("Only PDF and DOCX resumes are supported.")


def _pdf(data: bytes) -> Extracted:
    warnings, texts, sizes = [], [], []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise InputError("Password-protected PDFs are unsupported. Upload an unlocked copy.")
        if not 1 <= len(doc) <= MAX_PAGES:
            raise InputError(f"Upload a resume of 1–{MAX_PAGES} pages.")
        pages = len(doc)
        for n, page in enumerate(doc, 1):
            text = page.get_text("text", sort=True)
            if not text.strip():
                raise InputError(f"Page {n} has no selectable text. Verify the source or supply DOCX.")
            texts.append(text)
            if page.get_images():
                warnings.append("Images detected; essential facts must also appear as selectable text.")
            blocks = page.get_text("dict")["blocks"]
            boxes = []
            for block in blocks:
                if block["type"] != 0:
                    continue
                boxes.append(block["bbox"])
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        if span["text"].strip():
                            sizes.append(span["size"])
                            if span.get("alpha", 255) == 0 or span.get("color") == 16777215:
                                warnings.append("Invisible or white text detected. Verify that no facts are hidden.")
            mid = page.rect.width / 2
            if any(a[2] < mid and b[0] > mid and min(a[3], b[3]) - max(a[1], b[1]) > 18 for a in boxes for b in boxes):
                warnings.append("Possible multiple columns; review the extraction reading order.")
        if pages > 1:
            warnings.append(f"Source is {pages} pages; the default student export must fit one page.")
    minimum = min(sizes) if sizes else None
    if minimum and minimum < 9.5:
        warnings.append("Small source text detected (below 9.5 pt).")
    text = validate_text("\n".join(texts))
    return Extracted(text, "pdf", pages, tuple(dict.fromkeys(warnings)), minimum)


def _docx(data: bytes) -> Extracted:
    with ZipFile(BytesIO(data)) as z:
        if sum(x.file_size for x in z.infolist()) > 30 * 1024 * 1024:
            raise InputError("DOCX expands beyond the 30 MB safety limit.")
        if "word/document.xml" not in z.namelist():
            raise InputError("Invalid DOCX container.")
    doc = Document(BytesIO(data))
    warnings, lines, sizes = [], [], []
    # Iterate body children to preserve table/paragraph order. Hyperlink text is
    # included by python-docx 1.2 paragraph.text and explicit XML for tables.
    for part in doc.iter_inner_content():
        if hasattr(part, "rows"):
            warnings.append("Tables detected; review cell reading order and use single-column export.")
            for row in part.rows:
                lines.append(" | ".join(dict.fromkeys(c.text for c in row.cells)))
        else:
            text = part.text.strip()
            if not text:
                continue
            if part.style and "list" in part.style.name.lower() or part._p.xpath("./w:pPr/w:numPr"):
                text = "• " + text
            lines.append(text)
            for run in part.runs:
                if run.font.size:
                    sizes.append(run.font.size.pt)
                if run.font.hidden:
                    warnings.append("Hidden text detected; verify all factual content.")
    for sec in doc.sections:
        for num in sec._sectPr.xpath("./w:cols/@w:num"):
            if int(num) > 1:
                warnings.append("Multiple DOCX columns detected; review reading order.")
        for variant in (sec.header, sec.footer, sec.first_page_header, sec.first_page_footer, sec.even_page_header, sec.even_page_footer):
            value = "\n".join(t.text for t in variant.paragraphs if t.text.strip())
            if value:
                warnings.append("Header/footer text included; check placement in the evidence ledger.")
                if value not in "\n".join(lines):
                    lines.append(value)
    for box in doc.element.xpath(".//w:txbxContent"):
        value = " ".join(box.xpath(".//w:t/text()"))
        if value and value not in "\n".join(lines):
            lines.append(value)
        warnings.append("Text box detected; review text placement in the evidence ledger.")
    if doc.element.xpath(".//w:drawing | .//w:pict"):
        warnings.append("Graphics detected; exported resumes use text only.")
    if sizes and min(sizes) < 9.5:
        warnings.append("Small source text detected (below 9.5 pt).")
    return Extracted(validate_text("\n".join(lines)), "docx", None, tuple(dict.fromkeys(warnings)), min(sizes) if sizes else None)


def _public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise InputError("Use a public HTTP(S) job URL without credentials or custom ports.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
            raise InputError("Job URLs must resolve to public Internet addresses.")
    except OSError as exc:
        raise InputError("Job URL lookup failed. Paste the job description instead.") from exc


def job_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            obj = json.loads(tag.string or tag.get_text())
            queue = obj if isinstance(obj, list) else [obj]
            while queue:
                node = queue.pop(0)
                if not isinstance(node, dict):
                    continue
                queue.extend(node.get("@graph", []))
                types = node.get("@type", "")
                if "JobPosting" in types and node.get("description"):
                    desc = BeautifulSoup(node["description"], "html.parser").get_text("\n", strip=True)
                    return str(node.get("title", "")) + "\n" + desc
        except (ValueError, TypeError):
            continue
    # SmartRecruiters publishes semantic sections; omit EEO, compensation,
    # application widgets and navigation rather than mistaking them for skills.
    parts = soup.select('[itemprop="responsibilities"], [itemprop="qualifications"], .job-description, .job-qualifications')
    if parts:
        title = soup.find("h1")
        unique = list(dict.fromkeys(p.get_text("\n", strip=True) for p in parts))
        return (title.get_text(" ", strip=True) + "\n" if title else "") + "\n".join(unique)
    main = soup.find("main") or soup.find("article")
    if not main:
        raise InputError("No reliable job description found. Paste the job text instead.")
    for tag in main.select("script, style, nav, footer, form, button"):
        tag.decompose()
    return main.get_text("\n", strip=True)


def fetch_job(url: str) -> str:
    """Only called by an explicit Fetch button. No JS execution or login bypass."""
    try:
        for _ in range(4):
            _public_url(url)
            with requests.get(url, timeout=(5, 15), allow_redirects=False, stream=True,
                              headers={"User-Agent": "LocalResumeTailor/1.0"}) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                if "html" not in response.headers.get("Content-Type", "").lower():
                    raise InputError("The job URL did not return an HTML posting. Paste the text.")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 2 * 1024 * 1024:
                        raise InputError("Job page is too large. Paste its description instead.")
                    chunks.append(chunk)
                text = job_text_from_html(b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"))
                if len(text.split()) < 40 or any(x in text.lower() for x in ("verify you are human", "enable javascript to continue", "access denied")):
                    raise InputError("The posting is blocked or incomplete. Paste the job description instead.")
                if len(text) > 45000:
                    raise InputError("Extracted job text is too long. Paste only the relevant posting.")
                return text
        raise InputError("Too many job page redirects. Paste the description instead.")
    except (requests.RequestException, ValueError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("Could not retrieve this posting. Paste its job description; no requirements were fabricated.") from exc
