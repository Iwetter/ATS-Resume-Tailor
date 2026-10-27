@echo off
setlocal
pushd "%~dp0" || exit /b 1
if exist ".venv\Scripts\python.exe" goto dependencies
where py >nul 2>&1
if errorlevel 1 (
  python -m venv .venv
) else (
  py -3 -m venv .venv
)
if errorlevel 1 goto failed
:dependencies
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
if not exist ".env" copy /y ".env.example" ".env" >nul
echo Setup finished. Double-click launch.cmd to open Resume Bench.
popd
pause
exit /b 0
:failed
echo Setup failed. Check the error above and WINDOWS_SETUP.md. Python 3.11 or newer is required.
popd
pause
exit /b 1
