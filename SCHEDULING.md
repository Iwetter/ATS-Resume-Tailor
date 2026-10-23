# Daily local Internship Hunter scan

> For continuous 10–15 minute scanning on a persistent machine or cloud VM, use the Auto Apply worker (`python -m autoapply.worker` or `docker compose up -d`) described in [AUTOAPPLY.md](AUTOAPPLY.md). The daily Windows task below still works and shares the same database.

The app has no cloud scheduler. A scheduled scan runs only when this Windows computer is powered on and Windows can execute the task. It cannot run while the computer is shut down. No scheduled task was installed automatically during this build.

## Manual CLI

Double-click `scan.cmd`, or run it from a terminal. It selects an app-local `.venv` when present, otherwise the already-installed workspace environment, changes to the app directory and runs:

```powershell
python -m hunter.cli --days 7
```

For the current workspace, this command uses the installed interpreter directly:

```powershell
Set-Location 'C:\projects\ATS-Resume-Tailor'
..\..\work\.venv\Scripts\python.exe -m hunter.cli --days 7
```

Optional flags:

```powershell
python -m hunter.cli --days 14 --report C:\path\scan-report.json
python -m hunter.cli --db C:\path\hunter.sqlite3 --watchlist C:\path\employers.json
```

Without overrides, the CLI and dashboard share `data/hunter.sqlite3`, the active verified master, declared profile and saved employer settings. The Streamlit server does not need to be running. If no master is saved, discovery still runs and reports `verified_master_configured: false`; personalized scores wait for a verified upload.

All discovery/scoring is local computation and free public GET requests. The CLI never calls an LLM, rewrites a resume, transmits your master, or submits an application. A one-hour HTTP cache, 24-hour robots cache, per-host request pacing and one bounded retry reduce traffic. An interrupted scan lease expires after two hours. Failed boards keep existing application statuses and do not silently close jobs.

## Windows Task Scheduler setup

1. Open **Task Scheduler → Create Basic Task**. Name it **Resume Bench Internship Hunter**.
2. Select **Daily**, every 1 day, and choose a local time such as 7:30 AM.
3. Select **Start a program**.
4. Program/script: `C:\Windows\System32\cmd.exe`.
5. Add arguments, including the quotes:

   ```text
   /c "C:\projects\ATS-Resume-Tailor\scan.cmd"
   ```

6. Set **Start in** to the app folder, without quotes:

   ```text
   C:\projects\ATS-Resume-Tailor
   ```

7. In task properties, use your normal user account. In **Settings**, enable **Run task as soon as possible after a scheduled start is missed** and choose **Do not start a new instance** if the task is already running. Keep a reasonable execution limit such as 30 minutes. Sleep/power policy can still prevent execution; test it under your intended conditions.
8. Click **Run** once. Confirm **Last Run Result = 0x0** and check **Last Scanned** in Hunter. A source-specific error is visible in scan coverage. CLI exit code 1 means no configured source returned successfully.

If you move the project or interpreter, update the action paths. Fresh installations require the README dependency-installation commands first. Use Task Scheduler's Disable/Delete actions to stop or remove the daily task; neither removes saved jobs or resumes.
