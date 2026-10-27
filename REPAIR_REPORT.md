# Complete application repair — October 27, 2026

The supplied `ATS-Resume-Tailor-complete.zip` matched GitHub's `feature/ats-hunter-complete` at `e220a1e05c93e9685004c3a148d21a1575d86c58`. The default `main` branch was still at `36117fb5e3e86f61af97a2216b0602ce191e137a`, without the newer workspaces and Auto Apply implementation. This repair promotes the complete version and its existing history to main and fixes the integration problems below. No runtime database, private resume, credentials or application history is included.

## Fixed behavior

- **Local configuration was silently ignored.** The Windows guide told users to edit `.env`, but settings read only exported environment variables. `runtime.py` now loads the installation's explicit `.env` before the default database path is chosen and when settings are read. Exported environment variables retain precedence, UTF-8 BOMs and inline comments are handled, and an unrelated working-directory or parent `.env` is not searched. Restart processes after changing configuration.
- **Malformed prices could crash the dashboard or defeat budget comparisons.** The sidebar previously called `float()` on raw environment text. Settings now reject non-finite values; the sidebar uses parsed settings and mounted key files. Claude refuses zero/negative/non-finite prices and invalid daily/monthly limits before sending a request. The example configuration requires a model ID from the user's own API account.
- **Word rendering could hang or fault inside Streamlit.** The old converter called COM inside the app process and ignored its timeout for Word. The baseline run reproduced an RPC/native Word error. Conversion now runs in a hidden, bounded helper process with COM cleanup, macros disabled and link updates disabled. It uses Word's PDF export rather than saving the open DOCX as a different file type. Partial failed output is discarded before fallback. Concurrent LibreOffice conversions receive separate writable profiles and share the overall timeout budget.
- **A missing environment could launch an obsolete installation.** `launch.cmd` no longer falls back to an unrelated workspace environment. `setup.cmd` installs this checkout's requirements and Chromium, preserves an existing `.env`, and provides a one-time Windows setup. Launcher errors remain visible.
- **Docker builds could include files that Git ignored.** Build context now excludes credential folders, private keys, Streamlit secrets, browser cookies/state and applicant/answer-bank files.
- **Regression checks were local only.** GitHub Actions now installs Chromium, LibreOffice and the pinned dependencies and runs the suite on pushes and pull requests, with read-only repository permissions and no API credentials.

## Validation

Tested on Windows with Python 3.13 and the project's pinned dependencies plus `python-dotenv==1.2.4`.

- Baseline: **386 passed, 30 skipped** in 57.47 seconds. Chromium was missing, and the Word test reported a native/RPC error in the parent test process.
- Repaired: **438 passed, zero skipped, zero failed** in **209.21 seconds**. This includes 22 new runtime/configuration/export-failure checks and all existing grader, discovery, browser, SMS, permissions, gateway, review, application-state and workspace tests.
- Installed Playwright's official headless Chromium for the previously skipped browser tests. Candidate forms and SMS/API transports were synthetic fixtures or mocked; no real application, SMS or paid model request was sent.
- The real Word rendering test passed: the tailored original DOCX produced a one-page PDF with all selected text extractable. Timeout/crash fallback and parallel LibreOffice profile isolation also pass deterministic tests; the real LibreOffice renderer is covered by the Linux GitHub workflow rather than this Windows installation.
- `pip check` reported no broken requirements, and `git diff --check` reported no whitespace errors.
- Five upstream PyMuPDF/SWIG deprecation warnings remain.

Command, from the repaired checkout:

```powershell
C:\Users\ianha\ATS-Resume-Tailor\.venv\Scripts\python.exe -m pytest -q --basetemp=C:\Users\ianha\Documents\Codex\2026-10-20\files-pasted-by-the-user-build\work\oct27\pytest-repair-all1
```

## Scope

Existing verified-master, application, approval, eligibility and export controls are retained. No worker, public gateway or real submission was enabled for the user. Live paid Claude/Twilio behavior and a production Docker deployment were not exercised. GitHub workflow execution is reported separately from the local results above.
