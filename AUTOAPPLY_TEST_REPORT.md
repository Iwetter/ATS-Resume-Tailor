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


---

# Phase 6 validation: SMS, permission modes, phone gateway (October 23, 2026)

Tested on Windows 11 with CPython 3.13.3, Playwright 1.63.0 (headless Chromium), twilio 9.12.0, Flask 3.1.3 and waitress 3.0.2. `pip check` reports no broken requirements.

## Audit of the first implementation

- **Test report verified.** The unmodified ZIP (commit `961cb613`, identical to `autoapply.patch`) gives **209 passed, 0 failed, 0 skipped**, matching the report above. (On Windows the virtual environment must live in a short path; lxml's DLL fails to load from paths over 260 characters.)
- **Live discovery, not exercised before:** `python -m autoapply.worker --once` on a scratch database scanned **all 23 boards successfully** and recorded 108 engineering internships, each with a persisted decision, in 2 min 39 s. A second run on the same database (a restart) made no requests because nothing was due, found 0 new postings, and `autoapply.health` reported healthy.
- **Read-only live adapter validation.** One Greenhouse (`job-boards.greenhouse.io`) and one Lever (`jobs.lever.co`) internship page were opened in headless Chromium after checking robots.txt. Nothing was typed, uploaded, selected or submitted. Findings:
  1. Both pages load an invisible CAPTCHA (reCAPTCHA Enterprise / hCaptcha), so real applications on these boards stop at Needs Human Action. The guide had claimed the form was "pre-filled in the screenshot"; the code returned before filling. *Fixed:* it now lists every question with its answer source for the human, still without typing anything, and the guide is corrected.
  2. **Greenhouse résumé upload was never filled**: the file input is labelled "Attach", so it was classified as an optional unknown file. *Fixed:* upload groups use their `aria-labelledby` question ("Resume/CV*", required).
  3. **13 phantom required questions** on Greenhouse: React-Select's hidden `aria-hidden` proxy inputs. *Fixed:* skipped.
  4. **Every Greenhouse dropdown had no options**, so degree, discipline, month, GPA and similar fields could never be answered. *Fixed:* options are read by opening the menu; filling types the verified value and clicks an exactly matching option, or records a fill error.
  5. **Lever custom questions lost their text** (labels like `cards[…][field0]`). *Fixed:* the question container's label is used.
  6. Education "End date month/year" → graduation month/year; "Start date month/year" is no longer mistaken for internship availability; when a profile value is not in an employer's list, a verified answer-bank entry for that question is used.
  7. A verification code requested **after** the submit click is now reported explicitly (state stays Unknown and is never retried).
- **Race found in the engine:** a cancel from a human (dashboard, or SMS `NO`) during preparation was overwritten by the worker's next transition (back to Ready), and a cancel could overwrite Submitting. *Fixed:* every worker transition is now compare-and-set against the states it may legitimately find, and `cancel`/`requeue` are compare-and-set too.
- **Bug found by the new tests:** outgoing-SMS redaction removed the applicant's surname even inside the review-link hostname. *Fixed:* gateway links are exempt; all other text is still redacted.

## Automated result

**279 passed, 0 failed, 0 skipped** (`python -m pytest -q -rs`, 3 min 39 s): all **209 existing tests** pass unchanged, plus **70 new tests**. No test was skipped; Chromium was installed, so every browser test ran.

| File | Tests | Covers |
| --- | --- | --- |
| `test_sms.py` | 13 | Official Twilio SDK over a fake transport: phone normalisation; posting text and outgoing bodies sanitised (links, contact details, name, API keys); verification code flow, one-time use, wrong-code and rate limits; prefs validation and persistence; unconfigured Twilio; outbox dedupe; transient retry/backoff then give-up; permanent errors and opt-out; crash mid-send not resent; delivery-status callback; test SMS; API-key auth; one-time version-bound approval IDs |
| `test_sms_commands.py` | 14 | Through the Flask webhook: missing/forged/wrong-URL signatures → 403; gateway unconfigured; unverified sender and foreign AccountSid ignored, sender stored masked; YES approves that exact version once; Twilio retry of the same MessageSid answered, not re-executed; reused, stale (version changed), expired, unknown and missing IDs refused; NO rejects and the worker cannot resurrect it; cancel during preparation wins; cancel cannot overwrite Submitting; PAUSE keeps monitoring but queues nothing; RESUME restores Approval Required; RESUME restores Full Autopilot only while still authorised, otherwise falls back with the reason; RESUME refused during emergency stop; STATUS contains no personal data; STOP pauses and opts out, START opts in; status callback signature |
| `test_gateway.py` | 10 | scrypt hashes; secret key required; review links require login, are kind-specific and expire; login throttling and CSRF; phone approval binds to the shown hash; reject; files confined to the data directory; security headers, no dashboard routes; Secure/HttpOnly/SameSite cookie, HSTS and session revocation; summary page pause/resume |
| `test_notify.py` | 13 | New high-match alert has employer, position, fit, action, status and link, sent once; ineligible jobs silent; unapproved employer gets a job link; batching; no backlog after enabling; approval request once per version (also in digest mode); manual-action/error/submitted texts and category toggles; preparation failure only after retries; digest holds events for the summary; daily summary once per day with every field and YES/NO IDs; local-hour scheduling; restart neither loses nor repeats; SMS outage never breaks the worker. Every text is checked for personal data and GSM-7 (ASCII) characters |
| `test_permissions.py` | 9 | Mode names map onto OFF/REVIEW/AUTO; AUTO still needs dry run + phrase; double pause; resume default; preferences validated, persisted and applied by the worker without restart; real submission never overridable; review ≤ auto; saved cap enforced at the final gate; Autopilot readiness checklist; activity counts; Streamlit SMS panel: buttons, mode set elsewhere not undone by a rerun, limits saved |
| `test_adapter_fixes.py` | 6 | Real Chromium against fixtures copied from the live DOM: Greenhouse upload group, React-Select options and exact-match filling, answer-bank fallback, no-match → fill error; Lever cards; CAPTCHA reference listing types nothing; code requested after submit → Unknown, never retried |
| `test_sms_e2e.py` | 5 | Worker scan → Chromium preparation → new-match and approval texts → signed `YES` → submission against the mocked form → Submitted text → daily summary → restart without resend/resubmit; real submission disabled keeps everything prepared-only with fresh approval IDs; `NO` then rescans never resurrect; `PAUSE` stops an approved submission and `RESUME` releases it; crash mid-click → one "uncertain" text, never re-clicked |

Real submission stayed disabled throughout development: the autouse fixture removes `AUTOAPPLY_ALLOW_REAL_SUBMISSION`, and the few tests that set it use the fixture router, which answers every request and aborts anything else. No real SMS was sent and no Twilio account was contacted.

## Manual checks performed

- `python -m autoapply.gateway` under waitress: `/healthz` 200; unsigned webhook 403; review link without a session → redirect to sign-in; `/` 404; CSP and other headers present; no `Server` header.
- `docker-compose.yml` parsed and checked structurally: dashboard published only on `127.0.0.1:8501`; worker, dashboard and gateway share the `appdata` volume; the gateway has no published port; Caddy (ports 80/443, `phone` profile) persists certificates in `caddy_data`.

## Not verified here

- **Docker image not built and compose not run:** this machine has neither Docker nor WSL. Run `docker compose --profile phone up -d --build` on the VM and check `docker compose ps` (all services healthy), then `docker compose restart worker` to confirm data survives.
- **No real Twilio traffic:** set up the number (A2P 10DLC or toll-free verification), then use **Send test SMS** and text `STATUS`.
- **Caddy's certificate issuance** needs a real DNS name pointed at the VM.
- The live adapter check was read-only on one page per ATS; option lists and filling were verified against fixtures that copy the live DOM, not by typing into real employer forms.
