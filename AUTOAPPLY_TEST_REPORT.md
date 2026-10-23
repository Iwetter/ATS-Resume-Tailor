# Auto Apply validation

Tested October 23, 2026 on Linux with CPython 3.13, Playwright 1.63.0 (headless Chromium), anthropic 2.2.0 and the existing pinned dependencies. `pip check` reports no broken requirements.

## Automated result

**209 passed, 0 failed** (`python -m pytest -q`, about 2 minutes): all **129 pre-existing tests** unchanged and passing, plus **80 new tests**.

| File | Tests | Covers |
| --- | --- | --- |
| `test_autoapply_core.py` | 42 | Additive migration of an old-schema database without data loss; immutable discovery vs verified first publication; updates/republications/closures as events, never new postings; listing vs detail cache freshness; detail refresh on listing change; discipline exclusions; graduation-window parsing; Spring availability and January start never assumed; unknown conditions block AUTO only; fit thresholds; employer permissions; imported listings never auto; profile-vs-resume consistency; edits clear verification; sponsorship/authorization polarity; compound citizenship questions; demographics, consents and attestations; verified answer bank; model mappings limited to verified fields; log redaction |
| `test_autoapply_browser.py` | 17 | Real Chromium against mocked Greenhouse/Lever forms: REVIEW prepares without submitting; approval refused while real submission disabled; approved submission with confirmation screenshot/HTML; duplicate-submission and cross-post prevention; form change after approval; CAPTCHA, login, MFA → Needs Human Action; attestation approval; unknown required question reported then answered from the verified bank; missing confirmation → Unknown, never retried; AUTO requires dry run + typed enable; employer auto permission; profile change deactivates AUTO; daily cap; emergency stop before and during a run; unsupported ATS handoff |
| `test_autoapply_worker.py` | 9 | Immediate evaluation of newly discovered jobs; decisions persisted for every scanned job; per-board cadence, Workday cadence, failure and rate-limit backoff; 5-minute interval floor; restart recovery (Submitting → Unknown, Preparing → Queued, stale scan lease); bounded preparation retries; error after submission claim → Unknown; health check; CLI stop/resume and OFF mode |
| `test_autoapply_llm.py` | 9 | Fake SDK client: structured-output request and caching; schema violations not retried and cached; bounded transient retries; truncation; daily/token budgets refuse before sending; prices required; fabricated rewrites rejected by Phase 1 validation; question mapping sends no profile values; cover-letter claim validation |
| `test_autoapply_app.py` | 3 | Streamlit workspace renders; emergency stop and master switch; approval from the dashboard does not submit |

Every browser test runs with a router that answers each request from fixtures and aborts anything else; tests assert that no request tried to leave. The autouse test fixture removes `AUTOAPPLY_ALLOW_REAL_SUBMISSION`, `ANTHROPIC_API_KEY` and related variables, so no real application or paid API call is possible during the suite. All applicant data is the fictional `tests/fixtures/fictional_resume.txt` profile.

## Manual checks performed

- `python -m autoapply.worker --once` on a scratch database: all 23 enabled boards were attempted. This sandbox's network allowlist blocks the employer API hosts, so every board failed with a connection error; the worker recorded each failure with backoff, closed nothing, and `python -m autoapply.health` reported a healthy worker. **Live discovery was therefore not exercised in this build**; adapters remain covered by the saved API-shaped fixtures.
- Streamlit served the app (health endpoint OK). The Auto Apply Control, Queue and Blocked tabs were inspected in a browser with a fictional seeded database: gates, Ready for Review and a CAPTCHA hand-off displayed correctly.
- `docker compose config` validated the compose file. The image was **not built** here (no Docker daemon in the sandbox); build it on the VM with `docker compose build`.

## Not yet verified (review before enabling real submission)

- No live Greenhouse or Lever application page was opened; real DOMs may differ from the fixtures. Run REVIEW mode first and inspect the screenshots for several real postings.
- No live Claude request was made; set prices and limits, then test one rewrite in REVIEW mode.
- Your real profile and resume have not been loaded; complete and verify them in the dashboard.
