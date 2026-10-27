@echo off
setlocal
pushd "%~dp0"
set "RESUME_PYTHON=.venv\Scripts\python.exe"
if exist "%RESUME_PYTHON%" goto run
echo Double-click setup.cmd first to install this app's environment.
popd
pause
exit /b 1
:run
"%RESUME_PYTHON%" -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
set "RESUME_EXIT=%ERRORLEVEL%"
popd
if not "%RESUME_EXIT%"=="0" (
  echo The app stopped with an error. Run setup.cmd to install or repair dependencies.
  pause
)
exit /b %RESUME_EXIT%
