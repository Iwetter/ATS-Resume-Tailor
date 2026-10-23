@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create the environment first: see WINDOWS_SETUP.md
  pause
  exit /b 1
)
echo Auto Apply worker: scanning every 12 minutes. Close this window or press Ctrl+C to stop.
".venv\Scripts\python.exe" -m autoapply.worker
popd
endlocal
