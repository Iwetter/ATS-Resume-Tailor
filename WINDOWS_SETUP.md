# Running on Windows

Tested on Windows 11 with Python 3.13 and Microsoft 365 Word. Commands are for PowerShell, run from the project folder.

## 1. One-time setup

Keep the project (and its `.venv`) in a short path such as `C:\Users\<you>\ATS-Resume-Tailor`: lxml cannot load its DLL from paths longer than 260 characters.

```powershell
cd C:\Users\ianha\ATS-Resume-Tailor
git switch feature/ats-hunter-complete
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
copy .env.example .env    # optional: edit settings; never commit .env
```

`requirements.txt` installs `pywin32` on Windows, which drives Microsoft Word to turn your tailored DOCX into a PDF with your exact layout. Without Word, install LibreOffice and make sure `soffice` is on PATH.

## 2. Start the app

```powershell
.venv\Scripts\python -m streamlit run app.py --server.address 127.0.0.1
```

or double-click `launch.cmd`. Your browser opens http://localhost:8501. The dashboard listens on your PC only.

## 3. Keep discovering in the background (optional)

```powershell
.venv\Scripts\python -m autoapply.worker
```

or double-click `worker.cmd`. It scans every 12 minutes while the window is open and the PC is awake; close the window (or Ctrl+C) to stop. For one cycle only: `.venv\Scripts\python -m autoapply.worker --once`. Real applications are never submitted unless `.env` sets `AUTOAPPLY_ALLOW_REAL_SUBMISSION=true`.

## 4. Data and safety

- Everything lives in `data\` (git-ignored): `hunter.sqlite3`, your saved master (`data\masters\`), tailored files (`data\tailored\`), application evidence. Back it up by copying the folder while the app and worker are closed.
- A backup taken before this branch was installed: `data\backups\hunter-before-feature-2026-10-23.sqlite3`.
- Restarting is safe: interrupted preparations are requeued, an interrupted submit becomes "Unknown" and is never clicked again.
- Tests: `.venv\Scripts\python -m pytest -q --basetemp=$env:TEMP\aapt` (the `--basetemp` avoids a permission problem with the default pytest temp folder on some Windows setups).

## 5. Claude (optional, paid API)

A Claude Pro subscription does not include API access. To let Claude reword bullets, create an API key at console.anthropic.com, then in the sidebar: turn on **Enable paid AI rewriting**, paste the key, enter the model ID and its input/output prices per million tokens. The sidebar says exactly what is missing until it is ready. For the worker, set `CLAUDE_ENABLED=true`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `CLAUDE_INPUT_USD_PER_MTOK` and `CLAUDE_OUTPUT_USD_PER_MTOK` in `.env`.
