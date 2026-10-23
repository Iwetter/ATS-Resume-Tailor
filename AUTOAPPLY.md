# Auto Apply — always-on discovery, tailoring and guarded applications

Auto Apply extends Internship Hunter. It reuses Hunter's watchlist, public ATS adapters, robots-aware HTTP client, deduplication, eligibility rules and scoring, plus Phase 1's evidence matching, validated rewriting, approval audit and one-page PDF/DOCX export. Nothing in the existing workflow was removed.

**Real submission is off by default.** Until you set `AUTOAPPLY_ALLOW_REAL_SUBMISSION=true`, the system discovers jobs, tailors resumes and pre-fills forms for review, and never clicks Submit.

## How it works

```
worker (every 10–15 min) ──► Hunter scan ──► job persisted + event log
                                   │
                                   └─► immediate evaluation (hard gates + fit) ──► decision persisted
                                                         │ prepare_ok, switch on, mode ≠ OFF
                                                         ▼
                                                     Queued ──► tailor resume ──► open candidate form (Playwright)
                                                                                     │
          blockers (CAPTCHA, login, MFA, attestation, unknown required question) ──► Needs Human Action
                                       REVIEW, or any gate not fully verified ──► Ready for Review ──► you approve ──► Approved
                                       AUTO and every check passes  ─────────────────────────────────────────────────┐
                                                                                                                      ▼
                         final gate: stop switch · real-submission flag · caps · approval hash · idempotent claim ──► click
                                                                                                                      ▼
                                                         confirmation observed ──► Applied        otherwise ──► Unknown
```

### Discovery (Phase 1)
- `python -m autoapply.worker` runs continuously. Each enabled board has its own schedule: `SCAN_INTERVAL_SECONDS` (default 12 min, floor 5 min) and `WORKDAY_SCAN_INTERVAL_SECONDS` (default hourly, because Workday sitemaps are large).
- Listing responses are cached for `LISTING_CACHE_SECONDS` (default 9 min, below the scan interval) so each scan sees fresh membership. Job detail pages are cached for `DETAIL_CACHE_SECONDS`, except that a Greenhouse detail is re-fetched as soon as its listing `updated_at` changes.
- robots.txt, crawl delays, per-host pacing and bounded retries come from Hunter unchanged. A board that fails backs off exponentially; a rate-limited board backs off for at least 30 minutes or its `Retry-After`.
- **First discovery and verified publication are separate.** `jobs.discovered_at` is immutable; `jobs.first_published_at` is set once and only from a verified first-publication field (Greenhouse `first_published`, SmartRecruiters `releasedDate`, Workday `datePosted`, Recruitee `published_at`). Ashby's `publishedAt` can be a republication, and Lever/Personio have no publication date, so they never set it. The `job_events` table records `discovered`, `updated`, `republished`, `closed` and `reopened`; an update is never a new posting.
- Every scanned job is persisted, and every change in its gate decision is stored in `decisions`.

### Eligibility (Phase 2)
`autoapply/gates.py` adds hard gates on top of Hunter's `decide()`:

| Gate | Fails (blocks preparation) | Unknown (blocks AUTO only) |
| --- | --- | --- |
| Posting | Closed, imported without confirmation, partial description | User-confirmed import |
| Discipline | Unrelated titles (mechanical, software-only, business…) or non-priority field | — |
| Term | Outside Spring/Summer 2027 | No term stated |
| Your availability | Term not in your verified `available_terms` | — |
| Start date | Posting starts before your verified earliest start | Posting states a start but you have none |
| Graduation | Clearly incompatible window | Ambiguous wording |
| Location | Outside the U.S. | Unclear |
| Hard requirements | Any Hunter flag `unmet` | Any flag `unknown` (enrollment, clearance…) |
| Fit | Below `AUTOAPPLY_MIN_FIT_REVIEW` | Below `AUTOAPPLY_MIN_FIT_AUTO` |
| Profile | Not verified against the active master | — |
| Employer | Form preparation not permitted | Auto-submit not permitted |

January 2027 availability is never assumed: Spring 2027 requires that term in your profile **and** an explicit earliest start date.

### Claude (Phase 3)
- Official `anthropic` SDK with structured outputs (`output_config` JSON schema) plus local `jsonschema` validation.
- Claude **selects or rewords verified facts only**: project choice by ID, bullet rewrites through the existing `tailor_once` → `validate_change` pipeline (invented tools, numbers or verbs are rejected), cover-letter drafts whose every paragraph cites evidence IDs and is checked for unsupported skills/numbers, and question→field *mapping* using field names only (never your values).
- Rewrites and cover letters are held for your approval. Model mappings need review unless `AUTOAPPLY_TRUST_MODEL_MAPPINGS=true`.
- Every unique request is cached in SQLite (`llm_cache`). Transient errors retry at most `CLAUDE_MAX_RETRIES` times; schema violations and truncations are not retried. Spend is reserved before sending against `CLAUDE_DAILY_USD_LIMIT` and `CLAUDE_MONTHLY_USD_LIMIT`, using the prices you set in `CLAUDE_INPUT_USD_PER_MTOK` / `CLAUDE_OUTPUT_USD_PER_MTOK`. Claude stays disabled until those prices are set.
- Contact details, the original resume file and profile values are never sent. Job and form text is wrapped as untrusted data.

### Application automation (Phases 4–5)
- `autoapply/browser.py`: Greenhouse (`job-boards.greenhouse.io`) and Lever (`jobs.lever.co`) candidate-facing forms only, in a fresh cookie-less Chromium context. No private APIs or credentials.
- Answers come only from the verified applicant profile, the verified answer bank, the tailored PDF and approved cover letters. Sponsorship/authorization/citizenship questions are answered only when their polarity is unambiguous. Voluntary demographic questions and optional consents are left blank. Required legal attestations need your one-time approval per employer and exact wording.
- CAPTCHA (including invisible reCAPTCHA scripts), password or one-time-code fields, MFA text, access restrictions, closed postings, unexpected redirects, unknown required questions or fill errors → **Needs Human Action**, with a screenshot.
- **Modes:** `OFF`, `REVIEW` (default), `AUTO`. AUTO is not selectable directly: run the dry-run validation, then type `ENABLE AUTO`. Changing the profile or master resume deactivates AUTO.
- **Caps:** `AUTOAPPLY_MAX_PER_DAY`, `AUTOAPPLY_MAX_PER_COMPANY_PER_DAY`, `AUTOAPPLY_MAX_PER_COMPANY_TOTAL`. Clicks with uncertain outcomes count toward caps.
- **Emergency stop:** dashboard button, `python -m autoapply.cli stop`, `touch data/STOP`, or `AUTOAPPLY_EMERGENCY_STOP=true`. Checked before every step and again immediately before the click.
- **No duplicates:** one application row per job; a unique `submission_intents` row is written atomically *before* the click; cross-posted jobs with the same employer and title are skipped. After a crash, an interrupted submission becomes **Unknown** and is never retried automatically.
- **Applied requires evidence:** a confirmation message or confirmation URL, saved with a screenshot and the page HTML. Anything else is **Unknown** until you record what you verified.

### Permission modes and SMS (Phase 6)

The OFF / REVIEW / AUTO state machine is unchanged. The dashboard (**Control** and **SMS & phone** tabs) presents it as three permission modes:

| Mode | State | What happens |
| --- | --- | --- |
| **Full Autopilot** | `AUTO` | Tailors and submits a qualifying application without asking, **only** when the profile is verified, the employer *and* its ATS site are approved for auto-submit, every eligibility gate passes, every required question has a verified answer, and the caps allow it. Anything else becomes an approval request. Entered only through the dry run + typed `ENABLE AUTO`. |
| **Approval Required** | `REVIEW` | Prepares the application and texts you a summary with a one-time approval ID. Nothing is submitted until you approve that exact version. |
| **Paused** | `OFF` | Discovery, scoring and alerts continue; nothing is prepared or submitted. |

Pausing remembers the mode it replaced. **RESUME** restores it after re-running `enable_auto`'s checks: a dry run that passed within 7 days, and an unchanged, verified profile and master resume. If any check fails, it resumes in Approval Required and says why. RESUME never clears an emergency stop.

**Texts you receive.** Each one carries only the employer, position, estimated fit, action taken, current status and an expiring review link. Profile values, answers and credentials are never included.

| Category | When |
| --- | --- |
| New high-match internship | A newly discovered job has fit ≥ the Full Autopilot minimum and no failing eligibility gate. Four or more in one scan arrive as a single batched text. |
| Approval request | An application is Ready for Review. `Reply YES K7Q4M2 or NO K7Q4M2`. Always sent immediately when SMS is on, even in daily-summary mode. |
| Submitted | Confirmation evidence was captured (or you verified an Unknown). |
| Manual action | CAPTCHA, login, unanswered question, attestation, unsupported site… |
| Error / uncertain | Submit clicked without confirmation, interrupted by a restart, or preparation failed after all retries. |
| Daily summary | Once a day at your chosen local hour: counts, then up to six applications with link (and YES/NO ID if awaiting approval), plus a link to the full list. |

**Commands** (accepted only from your verified number):

| Text | Effect |
| --- | --- |
| `YES <ID>` | Approve exactly the version that ID was issued for. Refused if the form, resume or answers changed, the ID expired (`SMS_APPROVAL_TTL_HOURS`, default 24), or it was already used. |
| `NO <ID>` | Reject (cancel) that application. A worker that is mid-preparation cannot bring it back. |
| `PAUSE` | Stop new preparation and submission immediately; an approved application is not clicked while paused. |
| `RESUME` | Restore the previous mode, subject to its checks. |
| `STATUS` | Worker health, mode, real-submission flag, last-24-hour activity and caps. |

Twilio's own keywords still work: `STOP` opts your number out (Auto Apply also pauses and stops texting), and `START` opts back in. A bare `YES` is Twilio's opt-in keyword, so always include the ID.

**Security of the SMS channel:** every webhook must carry a valid `X-Twilio-Signature` computed over `PUBLIC_BASE_URL` + path. The `AccountSid` must be yours and the sender must be the number you verified with a 6-digit code. Each `MessageSid` is recorded before execution, so a Twilio retry is answered but never executed twice. Unknown senders get no reply. Outgoing texts strip links copied from postings, redact contact details and your name, and use plain GSM-7 characters (Unicode punctuation would triple the per-message cost).

**Reliability:** texts go through an outbox (`sms_outbox`) with a unique key per event, so restarts and replays never duplicate a text. The notifier reads the `decisions` and `audit` logs through persisted cursors, so nothing is lost across restarts and history is never replayed when SMS is first enabled. Twilio outages retry with exponential backoff (5 attempts). Permanent errors such as an invalid number or an opt-out fail immediately. A send interrupted by a crash is marked failed rather than resent, because Twilio may already have delivered it. SMS failures never stop the worker.

**Control panel** (Auto Apply → SMS & phone, stored in the database): verified phone number, SMS on/off, immediate alerts vs daily summary only, notification categories, summary hour and time zone, permission mode, minimum fit scores, daily and per-company limits, a test-SMS button and the recent message log. Saved fit/limit values override the environment defaults within the same bounds, and take effect on the worker's next cycle without a restart. `AUTOAPPLY_ALLOW_REAL_SUBMISSION` and the emergency stop can never be changed from the dashboard or by SMS.

## First-time setup

1. **Install** (Python 3.11+). On Windows, keep the virtual environment in a short path (e.g. `C:\aa\.venv`): lxml fails to load from paths over 260 characters.
   ```bash
   python -m venv .venv
   .venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip
   .venv/bin/python -m playwright install chromium
   cp .env.example .env                               # edit; never commit
   ```
2. **Start the dashboard:** `.venv/bin/python -m streamlit run app.py`.
3. **Internship Hunter → Verified master resume:** upload your real resume, check the extraction, save it.
4. **Auto Apply → Applicant profile:** fill every field, fix any error (the profile is checked against the resume header, education, GPA and graduation date), tick the confirmation and click **Verify profile**. Add verified answers for recurring questions (for example "How did you hear about this job?").
5. **Auto Apply → Employers:** for each Greenhouse/Lever employer whose site and ATS terms you have reviewed, tick *prepare forms* and *terms reviewed*. Leave *auto-submit* off for now.
6. **Control:** turn the master switch on (mode REVIEW).
7. **Start the worker:** `.venv/bin/python -m autoapply.worker`. Watch **Discovery** for board freshness and **Queue** for prepared applications.
8. Review each **Ready for Review** item: gates, resume preview, every form answer and the screenshot. Approving does nothing until real submission is enabled on the server.
9. When you have reviewed the profile, the employer flows and the test results, set `AUTOAPPLY_ALLOW_REAL_SUBMISSION=true` and restart the worker. Approved items are then submitted on the next cycle.
10. *(Optional)* For AUTO: **Run dry-run validation** (sandboxed, fictional forms), type `ENABLE AUTO`, and enable *auto-submit* only for specific employers.

## Cloud deployment (persistent VM)

```bash
git clone https://github.com/Iwetter/ATS-Resume-Tailor.git && cd ATS-Resume-Tailor
cp .env.example .env && chmod 600 .env     # add ANTHROPIC_API_KEY, prices, limits
docker compose up -d --build
docker compose ps                          # both services should become healthy
ssh -L 8501:127.0.0.1:8501 you@your-vm     # then open http://localhost:8501
```

- `worker` and `dashboard` share the `appdata` volume (`/app/data`: SQLite database, masters, tailored files, application evidence). The database uses WAL mode for concurrent access.
- The dashboard has no login and is published on the VM's loopback only. Use an SSH tunnel, or put it behind an authenticating reverse proxy; never expose port 8501 publicly.
- Secrets: `.env` (git-ignored) or `ANTHROPIC_API_KEY_FILE=/run/secrets/...` for Docker/Kubernetes secrets or a secrets-manager agent.
- Health: `worker` uses `python -m autoapply.health` (fresh heartbeat); `dashboard` uses Streamlit's `/_stcore/health`. Set `HEALTH_PORT` to also serve `/healthz`.
- Logs are JSON lines on stdout with emails, phone numbers, API keys and your name redacted.
- Restarts are safe: SIGTERM finishes the current step; on start the worker requeues interrupted preparations, marks interrupted submissions Unknown and clears stale scan leases.
- Backups: `docker compose exec worker python -c "import sqlite3; s=sqlite3.connect('/app/data/hunter.sqlite3'); d=sqlite3.connect('/app/data/backup.sqlite3'); s.backup(d)"` then copy `/app/data` off the VM.

## Phone access (HTTPS gateway)

```
phone ──HTTPS──► Caddy :443 ──► gateway :8080 (Flask + waitress) ──► same SQLite volume
Twilio ─HTTPS──►   │  only /sms/*, /login, /logout, /r/*, /j/*, /s/*, /healthz
                   └─ everything else → 404        dashboard :8501 stays on 127.0.0.1 (SSH tunnel)
```

- `autoapply/gateway.py` is the only internet-facing component. Caddy terminates TLS with an automatic Let's Encrypt certificate and forwards only the gateway's paths. The Streamlit dashboard has no route through it.
- **Twilio webhooks** (`/sms/inbound`, `/sms/status`) are authenticated by signature, not by login. Missing or invalid signatures get 403.
- **Review links** (`/r/<code>` application, `/j/<code>` job, `/s/<code>` summary) are random 17-letter codes stored server-side. They are application-specific, expire after `REVIEW_LINK_TTL_HOURS` (default 72) and can be revoked. Opening one also requires signing in: a password checked with scrypt, throttled to 5 failures per client and 20 overall per 15 minutes. The session cookie is `Secure`, `HttpOnly` and `SameSite=Lax`, and every POST carries a CSRF token. Pages send `no-store`, `no-referrer`, a strict CSP and `frame-ancestors 'none'`.
- The application page shows employer, position, state, fit, gates, the reasons a human is needed, the question list with answer *sources* (not values), the tailored PDF and the prepared-form screenshot. **Approve this exact version** binds to the approval hash on the page. Files are served only from inside the data directory.
- `python -m autoapply.cli revoke-sessions` signs out every phone session and revokes all review links.

**Setup**

1. DNS: point `apply.example.com` (A/AAAA) at the VM and open ports 80 and 443.
2. `.env`: set `PUBLIC_HOSTNAME=apply.example.com`, `PUBLIC_BASE_URL=https://apply.example.com`, `GATEWAY_SECRET_KEY` (`python -c "import secrets;print(secrets.token_urlsafe(48))"`), `GATEWAY_PASSWORD_HASH` (`python -m autoapply.cli hash-password`) and the `TWILIO_*` values.
3. `docker compose --profile phone up -d --build`.
4. Twilio console → your number → Messaging → *A message comes in*: `https://apply.example.com/sms/inbound`, HTTP POST. U.S. long-code numbers need an approved A2P 10DLC campaign (or use a verified toll-free number) before carriers deliver texts.
5. Dashboard → Auto Apply → **SMS & phone**: enter your mobile number, enter the 6-digit code, turn SMS on, choose categories, press **Send test SMS**, then text `STATUS`.

Without the `phone` profile nothing is published except the loopback dashboard. Texts are still sent, but they carry no links and inbound commands are refused.

## Operator commands

```bash
python -m autoapply.cli status      # mode, switches, queue counts, heartbeat
python -m autoapply.cli stop --reason "checking something"
python -m autoapply.cli resume
python -m autoapply.cli dry-run     # sandboxed validation (needed for AUTO)
python -m autoapply.cli health
python -m autoapply.worker --once   # one scan + queue cycle
python -m autoapply.cli pause       # permission mode -> Paused
python -m autoapply.cli unpause     # restore the mode in force before the pause
python -m autoapply.cli sms-test    # send a test text to the verified number
python -m autoapply.cli notify      # queue and send pending notifications now
python -m autoapply.cli hash-password
python -m autoapply.cli revoke-sessions
python -m autoapply.cli inspect https://job-boards.greenhouse.io/<board>/jobs/<id>   # read-only adapter check
python -m autoapply.gateway         # phone gateway (normally via docker compose --profile phone)
```

`cli resume` clears the **emergency stop**; `cli unpause` (or the SMS `RESUME`) leaves **Paused** mode.

## Limits to know

- **Live check (October 23, 2026):** the Greenhouse (job-boards.greenhouse.io) and Lever (jobs.lever.co) pages inspected both load an invisible CAPTCHA on the application form: reCAPTCHA Enterprise and hCaptcha respectively. Those applications stop at **Needs Human Action** and are never bypassed. In practice this means **Full Autopilot will rarely be able to submit to these boards**: it prepares the tailored resume, lists every question with whether a verified answer exists, and texts you to finish. Nothing is typed or uploaded into a page that has a CAPTCHA.
- Greenhouse and Lever DOMs change. The adapters discover fields by labels rather than fixed IDs, and the approval hash detects a changed form, but a redesign can still cause fill errors (→ Needs Human Action). Real-world flows have not been exercised against live employer pages from this build; the dry run and the tests use fictional mocked forms.
- **Adapter fixes from the live check:** Greenhouse's résumé upload is labelled by its group ("Resume/CV"), not the "Attach" button, so it is uploaded again. React-Select dropdowns (degree, discipline, month, GPA…) have their options read and are filled with an exact match only. Their invisible required proxy inputs no longer appear as phantom unanswered questions. Lever custom-question cards keep their question text. Education "End date month/year" maps to your graduation month/year. When your profile value is not in an employer's list ("Bachelor of Science" vs "Bachelor's Degree"), a verified answer-bank entry for that exact question is used. Use `python -m autoapply.cli inspect <url>` to re-check a page read-only.
- Workday, Ashby, SmartRecruiters and other ATSs are discovered but not automated; they go to Needs Human Action with your tailored resume ready.
- Fit scores are local heuristics, not employer ATS results.
