# ATS Resume Tailor & Internship Hunter

**A local application for evidence-based resume review and engineering internship tracking.**

A Python/Streamlit tool that parses resumes, compares existing evidence with job requirements, proposes reviewable edits, exports PDF/DOCX documents, and tracks job listings in SQLite.

## Features

- Resume extraction, section detection, and transparent readability heuristics.
- Requirement matching that distinguishes supported facts from unknown qualifications.
- Project selection, manual edits, and approval of proposed wording changes.
- PDF/DOCX export with extraction/layout checks.
- Public job-source adapters, duplicate handling, recency tracking, and application notes.
- Deterministic matching caches and tests built around fictional fixtures.
- **Auto Apply:** an always-on worker (10–15 minute scans), hard eligibility gates, Claude-assisted evidence-grounded tailoring with spending limits, and guarded Playwright completion of Greenhouse/Lever candidate forms in OFF / REVIEW / AUTO modes. Real submission is disabled by default. See [AUTOAPPLY.md](AUTOAPPLY.md).
- **SMS & phone control:** Twilio texts for new high-match internships, approvals, submissions, manual-action hand-offs, errors and a daily summary; reply YES/NO with a one-time approval ID, or PAUSE/RESUME/STATUS. Permission modes Full Autopilot / Approval Required / Paused. An authenticated HTTPS gateway serves Twilio webhooks and expiring phone review links while the dashboard stays private.

## Run locally

Use Python 3.11 or later:

```powershell
git clone https://github.com/Iwetter/ATS-Resume-Tailor.git
cd ATS-Resume-Tailor
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m streamlit run app.py
```

Upload a resume or choose the clearly labeled fictional demo, then supply a job description. Review the extracted evidence before approving edits or exporting a document. The public example profile is fictional; work authorization and sponsorship start unknown and require explicit input.

```powershell
.venv/Scripts/python -m pytest -q
```

## Architecture and scope

`ats/` contains parsing, matching, scoring, rewriting, and export logic. `hunter/` adds public-source adapters, filters, SQLite storage, and the Streamlit workspace. Core grading and manual editing run locally without an API key; optional AI rewriting is a separate user-configured feature.

`autoapply/` adds the worker, gates, Claude integration, browser automation, dashboard workspace, SMS notifications and commands (`sms.py`, `notify.py`, `commands.py`), permission modes (`controls.py`), the public phone gateway (`gateway.py`) and container deployment.

Scores are documented heuristics, not employer ATS results or hiring predictions. Job-source coverage and dates vary. Applications are submitted only through Auto Apply's guarded flow, which is off until you enable real submission on your server. This repository excludes resumes, application records, the populated local database, and scan history.

**Windows:** see [WINDOWS_SETUP.md](WINDOWS_SETUP.md) for exact setup and run commands. See the [user guide](USER_GUIDE.md), [Auto Apply guide](AUTOAPPLY.md), [source coverage](SOURCES.md), and [local scheduling notes](SCHEDULING.md).

Run everything in containers on a persistent VM with `cp .env.example .env && docker compose up -d --build` (details in AUTOAPPLY.md).

**Tools:** Python · Streamlit · SQLite · scikit-learn · PyMuPDF · python-docx · ReportLab · Anthropic SDK · Playwright · Twilio SDK · Flask · Caddy · Docker · pytest

[Ian Hall's engineering portfolio](https://github.com/Iwetter)
