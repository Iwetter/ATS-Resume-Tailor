# ATS Resume Grader and Tailor with Internship Hunter

A local Python 3.11+ / Streamlit app for evidence-based engineering internship applications. No database, hosting, embedding model, or API key is needed for grading, project selection, manual editing, PDF export, or DOCX export.

Phase 2 adds **Internship Hunter** inside this same application. It reuses Phase 1 parsing, evidence, matching, rewriting, approval and export functions. Hunter uses Python's built-in SQLite for persistent jobs, verified master snapshots, statuses, deterministic match caches and job-specific exports. No new dependencies were added.

## Internship Hunter quick start

1. Launch the app, then choose **Internship Hunter** in the Workspace sidebar.
2. Upload your actual resume or use the current non-demo Phase 1 resume. Preview it, verify its facts/grouping, and click **Save verified master for Hunter**. An uploaded master remains unchanged. No real resume was attached to either build brief; the fictional test resume is never saved as your verified master by the UI.
3. Review the declared eligibility profile. The public example starts with a fictional engineering-student profile; work authorization and sponsorship begin unknown and must be entered explicitly. Detectable education facts in your verified master take precedence; future enrollment, academic standing, co-op availability and clearance require confirmation.
4. Click **Search Now**. Default scope is a verified publication within 7 days, Spring/Summer 2027, U.S. engineering internships/co-ops. The publication window also supports 24 hours, 3, 14 and 30 days.
5. Browse **Best Matches**, **Recently Posted**, **New Postings**, **Imported jobs**, **Posting date unknown**, **Needs confirmation**, **Older postings**, **Saved Jobs**, **Applied**, **Rejected / Closed**, and **Tailored Resumes**. Sort by fit/recency/employer/location and filter by job source, discipline, term and minimum fit.
6. Open a job's complete available description, component scores, supporting evidence, missing requirements and eligibility flags. Source publication, update, first-discovered and last-checked timestamps are separate. New discovery never means newly published.
7. Click **Tailor Resume for this job** to return to the existing Phase 1 workspace with that description and verified master. Hunter proposes up to four projects by local relevance and reorders existing skill entries; review all omissions. Analyze, select bullets, optionally request AI rewriting, and approve/reject proposed changes as before. Local selection/manual editing works without a key.
8. Generate validated PDF/DOCX files, then click **Save validated resume to Internship Hunter**. The saved copy includes the source hash, approved changes, omitted evidence and separate original/tailored fit estimates. Find the files again under Tailored Resumes after restarting the app.
9. Open the official application link yourself and set the application status/notes. The app never submits applications or marks them Applied automatically.

The current workspace has real public job records from the documented live scan. Personalized scores require a verified master saved for Hunter, even if a resume is already loaded in the Phase 1 workspace. The source ZIP excludes runtime data; an extracted installation starts with an empty job database until its first scan. See [SOURCES.md](SOURCES.md) for verified coverage and limitations, [PHASE2_TEST_REPORT.md](PHASE2_TEST_REPORT.md) for tests, and [SCHEDULING.md](SCHEDULING.md) for daily local scanning.

## More job boards

Open **More job boards & import a listing** in Internship Hunter. Edit the keywords/location, then search **LinkedIn, Indeed, Workday employers, Handshake, Glassdoor, USAJOBS, Built In, ZipRecruiter or Dice** in your browser. Workday employers uses Google site search; some boards require an account, and filters may need adjustment on the destination site. These links do not scrape the boards or synchronize an account.

To bring a listing into Hunter, paste its URL, employer, exact title, location and full description, then click **Import listing into Hunter**. An optional employer application URL helps deduplicate cross-postings. Leave the posted date blank when unknown. Dates entered here are labeled user-reported. The listing appears under **Imported jobs**, where it can be scored against your verified master, saved and tracked. Check the employer application page and save an availability confirmation to enable tailoring. Imports are never counted as automatically verified recent results. Duplicate imports retain first discovery, status and notes; an official source can later replace imported metadata while keeping its original-link history.

**Search Now** also scans enabled employer boards on **Workday public sitemaps**, plus **Recruitee** and **Personio XML feeds** where accessible, alongside the original four ATS adapters. NVIDIA, RTX and GE Vernova Workday boards are included by default. Add other employer boards in **Local daily scanning & source settings → Add employer** using their hosted career URL. Workday needs the employer site name, for example `https://company.wd5.myworkdayjobs.com/External`; Personio uses `https://company.jobs.personio.com` or `.de`; Recruitee uses `https://company.recruitee.com`. Public access and an enabled feed are required. Unsupported boards, including Workable without employer credentials, support pasted imports or manual bookmarks.

Workday discovery reads robots-permitted public sitemaps and JobPosting structured data. Sitemaps can be partial (NVIDIA returned only 100 URLs), so omissions never mark a Workday job closed. No internal Workday search API is used. See [SOURCES.md](SOURCES.md) for the actual live results and access limits.

Hunter fit estimates use technical skills 35%, demonstrated experience/projects 35%, responsibilities 20%, and preferred qualifications 10%. Required/contextual evidence uses Phase 1's deterministic matching; preferred qualifications are measured separately. An unmeasured component contributes zero with an explanation. Eligibility is a separate exclusion/warning and cannot be offset by a high fit score. Neither employer fame nor repeated keywords earns bonus points. Estimates are not employer ATS results or interview probabilities.

Hunter's local `data/` folder holds `hunter.sqlite3`, immutable master copies, and job-specific PDF/DOCX/audit files. Back it up to retain your work. **Clear current session** clears browser-session state; it does not delete Hunter's persistent records. Scan and scoring caches are content-addressed. Optional Hunter rewriting persists Phase 1's reserved attempts/results in SQLite across restarts, without API keys.

## Windows launch

In this delivered workspace, double-click **launch.cmd**. Dependencies are already installed in the workspace's `work\.venv`; the launcher also supports an app-local `.venv`. Close the terminal or press Ctrl+C to stop the server. The app binds to `127.0.0.1`.

For a fresh installation, open PowerShell in this folder:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

No PowerShell activation or execution-policy change is required. Then open the local URL printed in the terminal, normally http://localhost:8501.

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The already-installed workspace environment can instead run tests with `..\..\work\.venv\Scripts\python.exe -m pytest -q`.

## Workflow

1. Upload a text-based PDF or DOCX master and preview **all extracted text**. No default profile information is inserted. Download the untouched master or JSON evidence ledger if desired.
2. Paste a job description, fetch a public posting URL, or load the saved Western Digital example. Failed/blocked URL extraction asks for pasted text. Review the extracted description.
3. Review section/group assignments in the evidence ledger. Standard headings and marked bullets parse best. Every evidence item, project group, technical skill, and metric has a stable source-linked ID. Correct grouping if a PDF line wraps unexpectedly.
4. Edit requirements, types, priorities and alternatives. Delete unrelated engineering tracks. Terms are separated by semicolons; `any` means alternatives and `all` means all terms. Eligibility assertions need a verification note. Click **Analyze Resume**.
5. Inspect matches, missing/partial evidence, eligibility, formatting warnings and component scores. Select/reorder projects and optionally select accomplishment bullets by local relevance. All projects and bullets are kept by default; omissions are explicit and recorded in the downloadable audit. Names, roles and dates remain unchanged.
6. Select up to eight bullets and click **Tailor Resume**. In local mode this opens manual review and does not rewrite automatically. Optionally export the compact JSON request for manual use with Codex, then import the returned JSON `changes` object.
7. Compare highlighted before/after text. Every modification starts at **Keep original**. Explicitly approve or reject each proposal. A changed proposal loses its previous approval. Unsupported edits are blocked, including manual edits. Correct/add new verified facts in a separate updated master and re-upload it.
8. Click **Generate validated PDF & DOCX**. Downloads appear only after the selected text survives PDF re-extraction, PDF page count is one, safe margins pass, and DOCX text is reopened successfully. Changed inputs invalidate old downloads.

The included fictional resume is a software test fixture, **not your resume**. Its wearable PCB is described as designed and built. Your actual uploaded wording, project status, dates and metrics remain authoritative.

## Transparent scoring

ATS readability is a 100-point heuristic: extraction 30, recognizable sections 25, contact 20, structure 15, and formatting 10. Source layout warnings reduce formatting points; unknown DOCX pagination is not treated as a verified page count. The post-export score uses the generated PDF's extraction and layout checks.

Job alignment defaults to skill coverage 40%, demonstrated experience/project evidence 30%, responsibility similarity 20%, and clarity/relevance 10%. Weights are editable and normalized to their total. Required/preferred/contextual requirements have relative weights 2/1/0.5. A supported skill requires a positive action bullet in experience/projects; a skills-list mention is partial. Partial coverage earns half credit, while demonstrated-evidence credit requires supported status. Missing measured dimensions score zero with a warning. Eligibility is reported separately, never hidden in a numerical average.

Technical aliases are explicit and boundary-aware (I²C/I2C, PCB/printed circuit board, PWM, etc.). C and C++ remain distinct, as do VHDL/Verilog/SystemVerilog and ESP32/STM32. Binary TF-IDF and set-based coverage do not reward keyword repetition. Responsibilities use transparent lexical thresholds (0.60 supported, 0.25 partial); this is a retrieval heuristic requiring human review, not proof of full responsibility scope. No score predicts an employer's ATS or hiring decision.

## Optional API rewriting and cost controls

Enable the sidebar toggle, enter your API key and a **model ID you have configured that supports Chat Completions structured outputs**, then intentionally click **Tailor Resume**. Keys are never automatically read from your environment. No paid requests were made during development/testing.

- Only selected original bullets and up to 18 relevant requirements are sent. The whole resume, application history, contact header and master-file bytes are not sent.
- One HTTP POST per unique operation; strict JSON schema, at most 8 bullet changes, at most 18,000 input characters and a configurable 256–3,000 output-token cap. There are no tools, agents, embeddings, retries, fallback models, or scoring calls. `store` is false.
- The session cache hashes source bytes, ledger, job text, edited requirements, selected evidence, prompt version and model configuration. It caches successes and uncertain/failed attempts before a rerun can send again. Clicking the same operation again reuses its result. Correct configuration or choose local editing after a failure; no background retries occur.
- Standalone Phase 1 cache and credentials are process/session memory only. Hunter-linked operations persist the rewrite cache in SQLite, including reserved/failed attempts; credentials always remain in memory. Restarting an isolated Phase 1 session can lose its cache and permit a fresh intentional request.
- The exact payload is inspectable and exportable before sending. Usage is shown when returned. No assumed dollar estimate or default paid model is provided.

Proposals must cite exactly one original bullet, copy its original text exactly, and preserve all numeric values/units, technologies, accomplishment verbs and completion status. New or removed factual content is rejected conservatively, even for technologies outside the alias vocabulary. Alias normalization is allowed. Remaining edits require explicit human confirmation of meaning and relationships; lexical checks cannot prove semantic equivalence. This intentionally blocks some legitimate paraphrases.

## Validation and limits

The automated suite covers PDF/DOCX extraction, corrupt/empty/encrypted documents, table order, aliases, alternatives, deterministic scores, missing skills, conservative eligibility, unsupported claims, immutable facts/metrics, one-page PDF text verification, overflow rejection, caches, zero unintended API calls, and Streamlit review/export/rerun behavior. The saved real job text supports deterministic offline regression tests. See `TEST_REPORT.md` for this build's results.

- Your actual resume was not attached to the build brief. It has not been personally graded or tested; upload it and verify the full extraction and grouping before use.
- No OCR or JavaScript/browser login scraping. Network, bot protection or expired postings can require pasted job text. Direct Windows fetching timed out during this build; the saved example came from the retrieved real posting, without invented requirements.
- Rule-based section recognition, technical vocabulary, negation handling and requirement extraction are limited. Correct the editable requirements/grouping and review supporting evidence. Hard eligibility defaults to unknown unless safely supported or explicitly verified.
- PDF exports are always one page, single column, 10–10.5 pt body, selectable text, standard headings and working email/web links where detected. Overflow is reported instead of silently truncating experience or shrinking text below 10 pt.
- DOCX uses the same content, margins and typography, with no tables or graphics. Word/LibreOffice can paginate differently: **open the DOCX in Word and verify its page count and appearance**. Its page count is not automatically certified by the PDF generator.
- Phase 1 uploaded content is processed locally in memory. Hunter persists only a master you explicitly verify and save. The app never overwrites your master. Downloads persist where your browser saves them; job discovery, optional URL fetching and optional AI rewriting use the network. Streamlit usage telemetry is disabled.

## Files

`app.py` provides the UI; `ats/extraction.py`, `matching.py`, `scoring.py`, `rewriting.py` and `exporting.py` contain the independent functions. `models.py` contains simple frozen data classes. Tests and the clearly labeled fictional/real-job fixtures are under `tests/`. Architectural and license review notes are in `REFERENCES.md`; no reference-project code is vendored.

Phase 2 lives in `hunter/`: `sources.py` and `extra_sources.py` implement the adapters; `boards.py` and `boards_ui.py` provide external searches and pasted imports; `http.py` enforces public access, robots, throttling, caching and retries; `filtering.py` handles dates/scope/eligibility; `storage.py` persists records; `service.py` integrates Phase 1 scoring and rewrite caching; `ui.py` is the dashboard; `cli.py` is the scheduled scanner. `hunter/employers.json` is the configurable initial watchlist. Runtime modifications are saved in SQLite rather than overwriting the shipped watchlist. Newly added default Workday boards are merged once into saved watchlists while preserving existing names and enable/disable choices.
