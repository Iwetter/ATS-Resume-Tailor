# Progress — ATS Resume Tailor & Autonomous Internship Hunter

Branch: `feature/ats-hunter-complete` (from GitHub `main` 36117fb). Never merged to `main` by automation.
Real submission stays **off** (`AUTOAPPLY_ALLOW_REAL_SUBMISSION` unset). Nothing is deployed publicly.

## Commit history on this branch

| Commit | What |
| --- | --- |
| Auto Apply platform | `autoapply.patch` applied as its own commit (tree identical to `ATS-Resume-Tailor-autoapply.zip`) |
| SMS / permission modes / phone gateway | Earlier session's work (Twilio SMS, YES/NO/PAUSE/RESUME/STATUS, Full Autopilot / Approval Required / Paused, HTTPS gateway, Greenhouse/Lever adapter fixes). Applied before Milestones A–C to avoid conflicts; it is Milestone D. |
| Milestone A | Discovery repair: broad retrieval + description classification, diagnostics, views/filters, 113 verified boards |
| Milestone B | Evidence-verified rewriting, formatting-preserving DOCX tailoring, Claude status, fit threshold 80 |
| Live-scan fixes | Ashby robots 401, null addresses, bulk fallback, precision, bold lead-in fix |
| Milestone E | Workspaces, dashboard, Windows setup, profile checklist, reports |

## Milestone status

- [x] **Phase 0** — audit, branch, patch commit, local DB backed up to `data/backups/` (git-ignored)
- [x] **A. Discovery** — see `data/demo/discovery-report.md` (local) and the summary in `AUTOAPPLY_TEST_REPORT.md`
- [x] **B. ATS + tailoring** — DOCX master edited in place (Word/LibreOffice render, one-page + text checks); verifier replaces lexical gate
- [~] **C. Auto Apply hardening** — compare-and-set transitions, CAPTCHA/adapter fixes, AUTO refuses unreviewed export issues. Not done: additional ATS adapters beyond Greenhouse/Lever.
- [x] **D. SMS** — complete and tested with mocks (no real texts sent)
- [x] **E. Integration** — 7 workspaces (Dashboard, Find Internships, Resume Grader & Tailor, Auto Apply, Applications, Notifications, Settings), WINDOWS_SETUP.md, worker.cmd, 347 tests passing

## Next actions (resume here)

1. Verify your real profile: Auto Apply → Applicant profile (checklist in AUTOAPPLY.md → "Before real submissions").
2. Set an Anthropic API key, model and prices (sidebar) and run one real Claude tailoring; compare with `data/demo/tailoring/report.md`.
3. Grader (Phase 2): done for terminology and labels (see AUTOAPPLY_TEST_REPORT.md). Remaining: 30 real postings still have no recognisable requirement section (mostly prose-only GE Vernova, Etched, Rockwell); re-run `tools/grader_audit.py` after new scans to find new terms.
4. Streamlit: ATS Grader and Resume Tailor are still one combined five-step page (grade → tailor → export); splitting them is optional.
5. Cloud: `docker compose build` has never been run (no Docker on the development PC).
6. More adapters (Ashby, SmartRecruiters, Workday) for form preparation, one at a time after REVIEW-mode validation.

## How to resume in a new session

```powershell
cd C:\Users\ianha\ATS-Resume-Tailor
git switch feature/ats-hunter-complete
.venv\Scripts\python -m pytest -q --basetemp=$env:TEMP\aapt
```
