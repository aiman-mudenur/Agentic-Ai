@echo off
setlocal

rem ===========================================================================
rem  Course Organizer - one-click launcher for Windows Command Prompt (cmd.exe)
rem
rem  What it does, and why the usual problems cannot come back:
rem    1. Kills stale python listeners on ports 8000 / 8501  -> no port fights,
rem       no half-dead server answering with a forever-spinner.
rem    2. Starts the API WITHOUT --reload                    -> Google sign-in
rem       codes are never wiped mid-run, so links no longer expire.
rem    3. Starts Streamlit and lets IT open a fresh browser tab after it is
rem       listening -> you never race the server, and never reuse an old tab.
rem
rem  Usage:
rem    double-click this file ......... starts API + Dashboard (two windows)
rem    run_project.bat check ......... validates paths + syntax, starts nothing
rem    run_project.bat stop .......... stops the python services on 8000/8501
rem ===========================================================================

set "REPO=C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer"
set "PY=C:\Users\ummea\.virtualenvs\course-organizer\Scripts\python.exe"

if /i "%~1"=="check" goto :check
if /i "%~1"=="stop"  goto :stop

echo.
echo Cleaning stale python listeners on ports 8000 and 8501 ...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr LISTENING ^| findstr :8000') do (
    taskkill /F /FI "IMAGENAME eq python.exe" /FI "PID eq %%a" >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr LISTENING ^| findstr :8501') do (
    taskkill /F /FI "IMAGENAME eq python.exe" /FI "PID eq %%a" >nul 2>&1
)

echo Starting API:  uvicorn on 127.0.0.1:8000 ...
start "API -- course organizer" cmd /k ""%PY%" -m uvicorn api.main:app --host 127.0.0.1 --port 8000"

echo Starting Dashboard:  Streamlit on localhost:8501 ...
start "Dashboard -- course organizer" /D "%REPO%\ui_roles" cmd /k ""%PY%" -m streamlit run Home.py --server.port 8501"

echo.
echo Done. Two windows are open: API and Dashboard.
echo Streamlit will open its own FRESH browser tab when it is ready - use that one.
echo Golden rule: close every OLD window/tab that still shows :8501 before you
echo double-click this file again, or the abandoned tab spins forever.
echo.
echo Quick check once both start:  curl http://127.0.0.1:8000/health
exit /b 0

:stop
for /f "tokens=5" %%a in ('netstat -aon ^| findstr LISTENING ^| findstr :8000') do taskkill /F /FI "IMAGENAME eq python.exe" /FI "PID eq %%a" >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr LISTENING ^| findstr :8501') do taskkill /F /FI "IMAGENAME eq python.exe" /FI "PID eq %%a" >nul 2>&1
echo Stopped course-organizer python services on ports 8000 / 8501.
exit /b 0

:check
if not exist "%PY%" (echo [FAIL] python not at "%PY%" & exit /b 2) else echo [ok] python found:  "%PY%"
if not exist "%REPO%\ui_roles\Home.py" (echo [FAIL] Home.py missing & exit /b 2) else echo [ok] Home.py found
"%PY%" -m scripts.provision_faculty --help >nul 2>&1
if errorlevel 1 (echo [FAIL] provision_faculty not runnable) else echo [ok] provision_faculty runnable
for /f "tokens=5" %%a in ('netstat -aon ^| findstr LISTENING ^| findstr :8000') do echo [loop ok] port 8000 listener PID %%a
for /f "tokens=5" %%a in ('netstat -aon ^| findstr LISTENING ^| findstr :8501') do echo [loop ok] port 8501 listener PID %%a
echo [ok] check finished - no services were started or stopped
exit /b 0
