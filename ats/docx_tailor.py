"""Tailor the user's own master DOCX in place, preserving its formatting.

The master DOCX is the source of truth. Instead of rebuilding the resume from
a template (``ats.exporting``), this module edits the original document:

* each resume item is mapped to its own paragraph by its text;
* reworded bullets and reordered skill lines change only the text of the
  existing runs: a differently formatted lead-in such as a bold
  "Languages:" is kept, and the body keeps the original run's font, size
  and style;
* omitted projects are removed paragraph by paragraph, and reordered
  projects are moved as whole blocks;
* margins, page size, styles, numbering, spacing and alignment are never
  touched, and every edited paragraph is compared before and after.

PDF output comes from Microsoft Word (Windows, through COM) or LibreOffice
(``soffice``, e.g. in Docker), so the page count is the real layout. If no
converter exists, or anything could not be applied exactly, the result says
so and must be reviewed rather than silently exported.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from .models import Resume


def key(text: str) -> str:
    """Formatting-insensitive identity of a line: letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").casefold())


def _paragraphs(doc):
    return [Paragraph(p, doc) for p in doc.element.body.iter(qn("w:p"))]


def map_items(doc, resume: Resume) -> dict[str, Paragraph]:
    """Item ID → its paragraph, matched in document order."""
    paras = [(p, key(p.text)) for p in _paragraphs(doc)]
    paras = [(p, k) for p, k in paras if k]
    mapping, start = {}, 0
    for item in resume.items:
        wanted = key(item.text)
        if not wanted:
            continue
        for index in range(start, len(paras)):
            if paras[index][1] == wanted:
                mapping[item.id] = paras[index][0]
                start = index + 1
                break
    return mapping


def _signature(paragraph):
    pf = paragraph.paragraph_format
    runs = [(r.font.name, r.font.size, r.bold, r.italic, r.underline, r.style.name if r.style else None) for r in paragraph.runs if r.text.strip()]
    return {"style": paragraph.style.name if paragraph.style else None, "alignment": paragraph.alignment, "space_before": pf.space_before,
            "space_after": pf.space_after, "line_spacing": pf.line_spacing, "left_indent": pf.left_indent, "first_line_indent": pf.first_line_indent,
            "numbered": paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None, "run_formats": sorted(set(map(str, runs)))}


def set_text(paragraph, new_text: str) -> None:
    """Replace a paragraph's text but keep its runs' formatting.

    Leading runs that are formatted differently from the body (bold or italic
    labels) and that the new text still starts with are left untouched; the
    remaining text goes into the first body run, later runs are emptied.
    """
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(new_text)
        return
    lead_text, lead = "", 0
    body_format = (runs[-1].bold, runs[-1].italic)
    target = new_text.lstrip()
    # Compare whitespace-insensitively: real resumes often start a bold label run with a space
    # (" Tools: "), while the parsed line text is trimmed.
    for run in runs[:-1]:
        candidate = (lead_text + run.text).strip()
        if run.text.strip() and target.startswith(candidate) and (run.bold, run.italic) != body_format:
            lead_text += run.text
            lead += 1
        else:
            break
    body = runs[lead:]
    rest = target[len(lead_text.strip()):]
    body[0].text = rest.lstrip() if lead_text[-1:].isspace() else rest
    for run in body[1:]:
        run._r.getparent().remove(run._r)


def _blocks(resume: Resume, mapping):
    """Contiguous paragraph blocks per Projects entry, in master order."""
    blocks = {}
    for item in resume.items:
        if item.section == "Projects" and item.group and item.id in mapping:
            blocks.setdefault(item.group, []).append(mapping[item.id]._p)
    return blocks


def tailor_docx(original: bytes, master: Resume, final: Resume) -> tuple[bytes, dict]:
    doc = Document(BytesIO(original))
    mapping = map_items(doc, master)
    final_by_id = final.by_id
    report = {"mapped": len(mapping), "items": len([x for x in master.items if key(x.text)]), "edited": [], "removed": [], "moved": [], "problems": [], "format_changes": []}
    report["unmapped"] = [x.id for x in master.items if key(x.text) and x.id not in mapping]
    # 1. reorder whole project entries to the final order
    blocks = _blocks(master, mapping)
    final_groups = list(dict.fromkeys(x.group for x in final.items if x.section == "Projects" and x.group))
    kept = [g for g in final_groups if g in blocks]
    master_order = [g for g in dict.fromkeys(x.group for x in master.items if x.section == "Projects" and x.group) if g in kept]
    if kept != master_order and kept:
        anchor = blocks[master_order[0]][0]
        parent = anchor.getparent()
        position = list(parent).index(anchor)
        for group in kept:
            for element in blocks[group]:
                parent.remove(element)
        for offset, element in enumerate(e for g in kept for e in blocks[g]):
            parent.insert(position + offset, element)
        report["moved"] = kept
    # 2. edit or remove items
    for item in master.items:
        if not key(item.text):
            continue
        paragraph = mapping.get(item.id)
        if item.id not in final_by_id:
            if paragraph is None:
                report["problems"].append(f"Could not find '{item.text[:60]}' in the DOCX to remove it.")
            else:
                paragraph._p.getparent().remove(paragraph._p)
                report["removed"].append(item.id)
            continue
        new_text = final_by_id[item.id].text
        if key(new_text) == key(item.text):
            continue
        if paragraph is None:
            report["problems"].append(f"Could not find '{item.text[:60]}' in the DOCX to edit it.")
        elif paragraph._p.xpath(".//w:hyperlink") or paragraph._p.xpath(".//w:drawing"):
            report["problems"].append(f"'{item.text[:60]}' contains a link or image; edit it in Word.")
        else:
            before = _signature(paragraph)
            set_text(paragraph, new_text)
            after = _signature(paragraph)
            if before != after:
                report["format_changes"].append({"item": item.id, "before": str(before), "after": str(after)})
            report["edited"].append(item.id)
    out = BytesIO()
    doc.save(out)
    return out.getvalue(), report


def docx_to_pdf(docx_bytes: bytes, timeout=120) -> tuple[bytes | None, str]:
    """(pdf, converter) using Word on Windows or LibreOffice elsewhere; (None, reason) if neither works."""
    with tempfile.TemporaryDirectory() as tmp:
        source, target = Path(tmp) / "resume.docx", Path(tmp) / "resume.pdf"
        source.write_bytes(docx_bytes)
        errors = []
        if sys.platform == "win32":
            try:
                import pythoncom
                import win32com.client
                pythoncom.CoInitialize()  # Streamlit runs scripts on worker threads
                word = win32com.client.DispatchEx("Word.Application")
                try:
                    word.Visible, word.DisplayAlerts = False, 0
                    document = word.Documents.Open(str(source), ReadOnly=True, AddToRecentFiles=False, Visible=False)
                    document.SaveAs2(str(target), FileFormat=17)  # wdFormatPDF
                    document.Close(False)
                    del document  # release the COM proxy while Word is still running
                finally:
                    word.Quit()
                    del word
                if target.is_file():
                    return target.read_bytes(), "Microsoft Word"
            except Exception as exc:  # Word missing, licensing dialog, COM error
                errors.append(f"Word: {type(exc).__name__}")
        office = shutil.which("soffice") or shutil.which("libreoffice")
        if office:
            try:
                subprocess.run([office, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source)], check=True, timeout=timeout, capture_output=True)
                if target.is_file():
                    return target.read_bytes(), "LibreOffice"
            except (subprocess.SubprocessError, OSError) as exc:
                errors.append(f"LibreOffice: {type(exc).__name__}")
        return None, "; ".join(errors) or "No DOCX-to-PDF converter (Microsoft Word or LibreOffice) is available."


def pdf_check(pdf: bytes, resume: Resume) -> tuple[int, list[str]]:
    """(pages, items whose text is not extractable from the PDF)."""
    import pymupdf
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        pages = document.page_count
        text = key("".join(page.get_text() for page in document))
    return pages, [x.text for x in resume.items if key(x.text) and key(x.text) not in text]


def section_layout(docx_bytes: bytes) -> list[tuple]:
    doc = Document(BytesIO(docx_bytes))
    return [(s.page_width, s.page_height, s.top_margin, s.bottom_margin, s.left_margin, s.right_margin) for s in doc.sections]


def export_tailored(original: bytes, master: Resume, final: Resume) -> dict:
    """Tailored DOCX + PDF from the user's own document, with every check the UI shows."""
    docx_bytes, report = tailor_docx(original, master, final)
    pdf, converter = docx_to_pdf(docx_bytes)
    pages, missing = pdf_check(pdf, final) if pdf else (None, [])
    issues = list(report["problems"])
    if report["format_changes"]:
        issues.append(f"{len(report['format_changes'])} edited paragraph(s) changed formatting; review them.")
    if section_layout(docx_bytes) != section_layout(original):
        issues.append("Page size or margins changed.")
    if pdf is None:
        issues.append("PDF not generated (" + converter + "); the page count is unverified.")
    elif pages != 1:
        issues.append(f"The tailored PDF has {pages} pages; it must fit on one.")
    if missing:
        issues.append(f"{len(missing)} line(s) are not extractable from the PDF: " + "; ".join(m[:50] for m in missing[:3]))
    return {"docx": docx_bytes, "pdf": pdf, "pages": pages, "converter": converter, "report": report, "issues": issues, "ok": not issues}
