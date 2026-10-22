@echo off
setlocal
pushd "%~dp0"
set "RESUME_PYTHON=.venv\Scripts\python.exe"
if exist "%RESUME_PYTHON%" goto run
set "RESUME_PYTHON=..\..\work\.venv\Scripts\python.exe"
if exist "%RESUME_PYTHON%" goto run
echo Python environment missing. Follow README.md before scheduling scans.
exit /b 1
:run
"%RESUME_PYTHON%" -m hunter.cli --days 7
set "RESUME_SCAN_EXIT=%ERRORLEVEL%"
popd
exit /b %RESUME_SCAN_EXIT%
