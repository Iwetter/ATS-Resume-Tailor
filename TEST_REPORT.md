# Build validation

Validated October 20, 2026 on Windows using CPython 3.13.3. Direct dependencies are pinned in `requirements.txt`; `pip check` reports no broken requirements.

## Automated result

**53 passed, 0 failed** using `python -m pytest -q`.

Five upstream PyMuPDF/SWIG deprecation warnings were emitted; they did not fail any checks.

| Area | Verified behavior |
| --- | --- |
| Extraction | PDF and DOCX text, metrics/units, DOCX tables in body order, contact headers; empty/corrupt/unsupported/encrypted/blank PDFs rejected |
| Job input | Real saved Western Digital posting; structured JobPosting HTML; failed retrieval asks for pasted text; nonpublic URL inputs rejected |
| Matching | Technical aliases, punctuation-aware boundaries, distinct technologies, required/preferred priorities, alternative groups, missing and partial skills |
| Grading | Reproducible ledger and scores, supported evidence IDs, separate eligibility, conservative composite conditions and degree-level checks, no repeated-keyword bonus, configurable weights |
| Rewriting | Source ID/original text checks; unsupported tools, numbers, units, outcome verbs and status changes rejected; approval required; header/education facts and master preserved |
| Cost | No API calls during grading/request preparation/local workflow/reruns; successful operations reused; uncertain failures not automatically retried; model configuration changes produce a new operation |
| HTTP adapter | Mocked single POST, structured JSON schema, output-token bound, `store: false`, incomplete responses rejected |
| Export | One-page PDF, 10–10.5 pt body, every selected line re-extracted, safe margins, working PDF links, reopened DOCX text, no DOCX tables/graphics, overflow rejected without truncation |
| Streamlit | Load inputs → analyze → local tailor → edit → approve → export → reject → regenerate; source unchanged; stale downloads unavailable; changed job requires reanalysis |

## Launch and visual checks

- Streamlit started successfully on `http://127.0.0.1:8501`; its health endpoint returned HTTP 200.
- Inspected the rendered initial browser interface: upload, job text, local/AI controls and instructions were visible and readable.
- Generated a fictional sample PDF and DOCX in the workspace scratch directory. Rendered the PDF with Poppler and inspected the page image: one clean page with no clipping, overlap or missing text. Poppler printed a Symbol display-font notice; the visible bullets and other text rendered correctly.
- The source ZIP contains code, configuration, tests, fixtures, launcher and documentation; it does not include virtual environments, generated personal resumes, API keys or runtime caches.

## Deliberate verification limits

- No actual user resume was attached. Tests use the clearly labeled fictional engineering resume, never a claim that these are the user's verified metrics or projects.
- The real posting was retrieved by the browsing tool and saved as its title/Job Description/Qualifications sections. Direct Python fetching timed out on this Windows environment; the app's explicit pasted-text fallback is covered.
- No paid API call was made. The OpenAI adapter and failure/cache behavior were tested with mocked transport; a configured live model still requires an intentional user test.
- DOCX content and structure were reopened/checked with python-docx. Microsoft Word pagination and visual rendering were not certified. Open the DOCX in Word before submission. Only the PDF's one-page count is automatically enforced.
- Lexical evidence matching and conservative rewrite validation cannot certify semantic truth. Review extracted requirements, grouping, eligibility and approved wording against the authoritative source before submitting an application.
