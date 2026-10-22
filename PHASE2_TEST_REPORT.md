# Phase 2 validation

Tested October 20, 2026 on Windows with Python 3.13.3 and the existing pinned Phase 1 dependencies. No agents, cloud deployment, paid discovery service, embedding model or new dependency was introduced.

## Automated tests

**128 passed, 0 failed** in 43.18 seconds, including all **53 original Phase 1 tests** and **75 Hunter tests** (34 added for the expanded boards). Five upstream PyMuPDF/SWIG deprecation warnings remain. `pip check` found no broken requirements.

Executed from the app folder with the already-installed workspace interpreter:

```powershell
..\..\work\.venv\Scripts\python.exe -m pytest -q --basetemp=..\..\work\phase2\pytest-expanded-final2
```

An explicit workspace temp directory was used because this machine denied access to pytest's default existing temp directory. No permissions were changed. Use a new disposable workspace temp path for a repeat run if needed.

| Area | Verified |
| --- | --- |
| Adapters | Saved synthetic API-shaped fixtures for Greenhouse, Lever, Ashby and SmartRecruiters; HTML/entity normalization; full pagination; unlisted Ashby records excluded; invalid sources rejected |
| Expanded sources | Workday sitemap indexes, partial coverage, detail limits, JSON-LD `@graph`, missing/invalid descriptions, foreign-host links skipped; Personio sections and Recruitee published-only XML; malformed/entity/error feeds rejected; text caching, content negotiation, robots and internal Workday API blocking |
| Board imports | Encoded searches for nine boards; pasted import with zero HTTP/model calls; LinkedIn/Indeed tracking-URL normalization; immutable discovery and retained status/notes; user-reported dates and availability excluded from automatic confirmed scope; employer-URL validation; canonical URL updates; promotion to official-source metadata with import provenance preserved |
| Dates | Publication versus update timestamps; Lever creation dates remain unknown; 1/3/7-day windows; older/future/unknown dates separated |
| Filtering | Closed/expired roles, wrong terms/years/countries, unrelated/full-time jobs, GPA and graduate/completed-degree constraints; citizenship/work authorization/sponsorship facts; enrollment/standing/clearance confirmation flags |
| Matching | Reused Phase 1 grading/evidence, deterministic weighted Hunter scores, stronger technical evidence ranks above keyword-only lists, repeated keywords do not improve scores, persistent match cache avoids recomputation |
| Persistence | Deduplication, distinct requisitions, immutable first-discovery date, status/notes across new Store instances, cross-source closure observations, failed scans preserve availability, newly discovered jobs detected between scans |
| Tailoring | Job-specific project selection, original source/metric preservation, unsupported claims rejected, explicit approval, one-page PDF/DOCX validation, saved PDF/DOCX/audit references, unchanged verified master |
| API cost | No paid calls during scans/local flows/reruns; persistent reuse of Phase 1 rewrite results across new cache instances; API keys absent from SQLite |
| Streamlit | Hunter → details → Applied status → Phase 1 job handoff → analyze → local tailor → validated export → save copy → Hunter downloads; no fabricated rewrite or forced score gain; demo resume not saved as a verified master |
| Imported listing UI | Paste listing → Imported jobs → local score → disabled unconfirmed tailoring → confirm employer application page → Phase 1 handoff; no request or paid call; saved watchlist choices survive the new-provider migration |
| Phase 1 regression | Original extraction, scoring, conservative rewriting, immutable facts, PDF/DOCX and stale-download checks continue passing |

Saved fixtures under `tests/fixtures/hunter/` use explicitly fictional employers and identifiers. They are not loaded into the runtime job database or presented as live opportunities.

## Live observations

The expanded production scan completed **23/23 enabled boards**, retaining **103 real engineering internship/co-op records** with **10 confirmed recent U.S. Spring/Summer 2027 postings in the seven-day window**. Workday added 38 records before eligibility/scope filtering (NVIDIA 2, RTX 29, GE Vernova 7). The scan reused 132 actual public-response cache entries, including the immediately preceding Workday adapter checks, without changing their Last Checked timestamps. It made no new network or LLM calls. See `MORE_BOARDS_PRODUCTION_SCAN.json` and `MORE_BOARDS_LIVE_REPORT.json`.

Personio's Wingcopter XML feed returned eight public jobs with zero matching internship titles. AME Recruitee was skipped because robots returned HTTP 302; Recruitee's implementation is verified with synthetic fixtures, without claiming live success for AME. Those employers were not added to the U.S. watchlist. Workday public sitemaps are deliberately treated as partial; absent records are never closed. NVIDIA exposed only 100 URLs, illustrating why complete coverage cannot be assumed.

The earlier Phase 2 production CLI completed 20 enabled boards with 65 records, three confirmed recent target-scope listings and three unknown dates. Its repeat run detected zero newly discovered jobs, made one network request and reused 89 cached responses. `LIVE_SCAN_REPORT.json` retains this historical result separately from the extension and fixtures.

The source investigation also tested robots handling: three Ashby employers were skipped after robots returned 401; two SmartRecruiters employers were skipped after robots disallowed the posting paths. These five remain disabled in the default watchlist. No restrictions were bypassed. `SOURCES.md` explains the exact coverage and date semantics.

The updated live Streamlit page was inspected in the browser: it displayed all nine board-search buttons, keyword/location fields, the universal import form, the Imported jobs view and source filter, and real Workday records alongside existing sources. It preserved the user's loaded Phase 1 resume and sidebar configuration. The actual app health endpoint returned HTTP 200. `more-job-boards-preview.png` beside the source ZIP captures the new panel. No fictional listing was inserted into the user's runtime database for UI testing.

## Remaining manual checks

- A Phase 1 resume may already be loaded; verify it and save it as Hunter's master to enable personalized live-job scores and tailoring. None was saved for Hunter at the expanded scan. Profile declarations alone never generate technical resume evidence.
- No live paid rewrite request was made. Optional OpenAI HTTP and cache behavior is mocked/tested; an intentional configured-model call still requires your key.
- Open DOCX exports in Word to verify pagination. PDF one-page count, selectable text, selected-content preservation and margins are checked automatically using the unchanged Phase 1 exporter.
- The daily scheduled command works, but no Windows scheduled task was registered automatically. Follow `SCHEDULING.md`; the computer must be powered on and able to run the task.
- Rule-based eligibility and lexical fit remain internal estimates. Review unknown flags, source descriptions, term/location, evidence meaning, and the official application page before submitting manually.
