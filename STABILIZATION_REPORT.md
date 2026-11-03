# Stabilization and deployment

Real submissions remain disabled. No employer communication or paid API call was used for validation. Existing working checkout and production database were left untouched; changes were developed in a separate worktree on the existing PR branch.

## Changes

- Corrected the stale test that excluded an uncertain historical application from the company count.
- Serialized daily/company quotas, history checks, and submission-intent creation in one SQLite transaction. One submitted application per normalized company remains the default.
- Retained submission intents when counting prior attempts, including legacy Applied rows without submit timestamps. Expired worker tokens cannot submit; recovery preserves active leases and handles expired leases each cycle.
- Added durable, append-only imported history and manual dashboard status/action recognition. Requisition IDs catch renamed cross-postings; known company aliases and title normalization remain deterministic.
- Blocked prior-company applications before resume tailoring. Required resume evidence gaps block AUTO even with a high aggregate score. General software testing, graduate-level titles, and ambiguous international locations cannot pass AUTO.
- Kept the existing scoring/description caches and resume pipeline. A multi-page tailored resume now explicitly blocks AUTO. Tests block external Python socket connections; browser fixtures abort non-fixture requests.

## Validation

Previous GitHub run 41369690274: 441 passed, one stale history assertion failed. Targeted integration run: 114 passed. Final gate tests: 47 passed. The single local full-suite run finished with 454 passed and two rejection-explanation wording failures; the wording was corrected, then all 118 affected filtering/eligibility tests passed. No failures remain known. GitHub CI will validate the pushed revision separately. Five third-party SWIG deprecation warnings remain.

A SQLite backup of the existing local database passed integrity_check after initialization: all 25 original tables and row counts were preserved. No destructive schema change was introduced. Dependency check and git diff whitespace check passed.

## Remaining limits

- Duplicate detection cannot prove identity when company aliases, requisition IDs, URLs, and titles all differ. The default one-company limit is an additional conservative defense; review unknown aliases manually.
- An application present both in the bot database and external history can conservatively consume extra quota. Imported snapshots intentionally survive CSV deletion; resolve mistaken records through a reviewed database correction, never by blindly raising limits.
- Rule-based eligibility extraction is conservative but cannot understand every employer sentence. Unrecognized or ambiguous requirements, enrollment/standing, clearance, and form attestations still require personal verification. Fixtures do not certify live employer forms.
- Workers must share one durable SQLite database. Separate database copies cannot coordinate. Start deployment with one worker.
- Existing adapters automate Greenhouse/Lever candidate forms; unsupported systems, CAPTCHA, login, and MFA require human action. Unknown outcomes must be reconciled in the employer portal and must not be retried automatically.

## Personal configuration and deployment steps

1. Wait for the updated PR checks to pass; review PR #1. Do not merge failed checks. Stop any currently running worker before updating the deployment checkout, and back up the database, application_history.csv, original resume, and local settings together.
2. Deploy the reviewed branch with the existing checkout/environment. After stopping the worker and confirming `git status --short` is clean in `C:\Users\ianha\ATS-Resume-Tailor`, run `git fetch origin` then `git switch --detach origin/fix/application-history-company-guards`. This avoids the branch being held by the development worktree. Never overwrite local changes. After a later approved merge, deploy main instead.
3. In that checkout's `.env`, keep `AUTOAPPLY_ALLOW_REAL_SUBMISSION=false`, `CLAUDE_ENABLED=false`, `AUTOAPPLY_MAX_PER_COMPANY_TOTAL=1`, and the default AUTO fit threshold of 80. Point `HUNTER_DB` at the intended persistent database. Leave optional SMS/paid integrations disabled during deployment validation.
4. Use the dashboard to verify the real one-page DOCX master and profile: Georgia Tech electrical engineering undergraduate; graduation May 2028; GPA 3.68; U.S. citizen; work authorized without sponsorship; nationwide U.S. search. Verify Summer 2027 dates and explicitly confirm Spring 2027 availability before adding Spring. Confirm enrollment/standing, contact details, and any employer-specific answer or attestation personally.
5. Populate the private CSV beside HUNTER_DB with all prior and uncertain applications, preferably including requisition IDs. Review aliases and history completeness. Follow APPLICATION_HISTORY.md; never commit personal history or credentials.
6. With the existing virtual environment, run `.venv\Scripts\python.exe -m autoapply.cli status` and `.venv\Scripts\python.exe -m autoapply.cli dry-run`. Launch the dashboard with `launch.cmd`, review employer permissions and source settings, then start a single worker with `worker.cmd` in REVIEW mode. Check `.venv\Scripts\python.exe -m autoapply.cli health` and review queue/audit/error output.
7. Verify emergency stop with `.venv\Scripts\python.exe -m autoapply.cli stop --reason "deployment verification"`. Confirm no queue action proceeds. Clear it with `resume` only when ready to continue non-submitting review. Use a persistent local disk, regular SQLite backups, and the existing scheduling/service documentation for restarts.
8. Review real prepared artifacts and official forms personally, including one-page formatting and every hard qualification. Keep real submissions false. Enabling real submissions is a separate future decision after history completeness, live adapter suitability, permissions, and deployment health have been confirmed.
