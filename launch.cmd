@echo off
setlocal
pushd "%~dp0"
set "RESUME_PYTHON=.venv\Scripts\python.exe"
if exist "%RESUME_PYTHON%" goto run
set "RESUME_PYTHON=..\..\work\.venv\Scripts\python.exe"
if exist "%RESUME_PYTHON%" goto run
echo Create the environment first using the three installation commands in README.md.
pause
exit /b 1
:run
"%RESUME_PYTHON%" -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
popd
endlocal
