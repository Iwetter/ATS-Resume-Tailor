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

## First-time setup

1. **Install** (Python 3.11+):
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

## Operator commands

```bash
python -m autoapply.cli status      # mode, switches, queue counts, heartbeat
python -m autoapply.cli stop --reason "checking something"
python -m autoapply.cli resume
python -m autoapply.cli dry-run     # sandboxed validation (needed for AUTO)
python -m autoapply.cli health
python -m autoapply.worker --once   # one scan + queue cycle
```

## Limits to know

- Many Greenhouse boards load reCAPTCHA. Those applications will always stop at Needs Human Action with the form pre-filled in the screenshot for reference; they are never bypassed.
- Greenhouse and Lever DOMs change. The adapters discover fields by labels rather than fixed IDs, and the approval hash detects a changed form, but a redesign can still cause fill errors (→ Needs Human Action). Real-world flows have not been exercised against live employer pages from this build; the dry run and the tests use fictional mocked forms.
- Workday, Ashby, SmartRecruiters and other ATSs are discovered but not automated; they go to Needs Human Action with your tailored resume ready.
- Fit scores are local heuristics, not employer ATS results.
