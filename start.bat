@echo off
REM ---------------------------------------------------------------------------
REM panadhanam — pull, check, start, open the dashboard.
REM
REM Double-click it, or from cmd/PowerShell:   start.bat
REM From Git Bash (MINGW64) use ./start.sh instead — bash does not run a
REM bare `start.bat`, and does not search the current directory either, which
REM is what "bash: start.bat: command not found" means.
REM
REM Everything here uses .venv\Scripts\python.exe explicitly. Plain `python` on
REM Windows finds the Microsoft Store build, which has none of this project's
REM packages installed — that is where "No module named pydantic" comes from.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe

echo.
echo === panadhanam ===
echo.

if not exist "%PY%" (
  echo [X] No virtual environment found.
  echo     Run setup.bat first, then come back here.
  echo.
  pause
  exit /b 1
)

REM --- 1. latest code --------------------------------------------------------
echo - Pulling the latest code...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo [!] The pull did not go through cleanly.
  echo     Usually that means a tracked file was edited locally. To discard
  echo     those edits and take the repository version:
  echo         git checkout config\settings.yaml
  echo     Your .env is NOT tracked and is never touched by a pull.
  echo.
  pause
)

REM --- 2. dependencies, in case requirements changed --------------------------
echo - Checking dependencies...
"%PY%" -m pip install -r requirements.txt --quiet --disable-pip-version-check

REM --- 3. what is actually working -------------------------------------------
echo.
echo - Checking your data feed, broker and LLM...
echo.
"%PY%" run.py --check-data
"%PY%" run.py --check-broker
"%PY%" run.py --check-llm

echo.
echo ============================================================
echo   Starting the dashboard. Leave this window OPEN —
echo   closing it stops the desk.
echo.
echo   Press Ctrl+C here to stop.
echo ============================================================
echo.

REM --- 4. open the browser once the server is actually up ---------------------
start "" /b cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:8000"

"%PY%" run.py
