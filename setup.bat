@echo off
REM ---------------------------------------------------------------------------
REM panadhanam setup for Windows (cmd.exe / PowerShell / double-click).
REM No `make` required.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo.
echo === panadhanam setup ===
echo.

REM --- 1. find Python --------------------------------------------------------
set PYCMD=
py -3 -c "import sys" >nul 2>&1 && set PYCMD=py -3
if "%PYCMD%"=="" (
  python -c "import sys" >nul 2>&1 && set PYCMD=python
)

if "%PYCMD%"=="" (
  echo [X] Python 3.11 or newer was not found on your PATH.
  echo.
  echo     Install it from https://www.python.org/downloads/
  echo     and TICK "Add python.exe to PATH" in the installer,
  echo     then close and reopen this window.
  echo.
  pause
  exit /b 1
)

%PYCMD% --version

REM --- 2. virtual environment ------------------------------------------------
if not exist .venv (
  echo - Creating virtual environment ^(.venv^)...
  %PYCMD% -m venv .venv
)

REM --- 3. dependencies --------------------------------------------------------
echo - Installing dependencies ^(this takes a minute or two^)...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if errorlevel 1 (
  echo [X] Dependency install failed. Scroll up for the error.
  pause
  exit /b 1
)

REM --- 4. first-run config ----------------------------------------------------
if not exist .env (
  copy .env.example .env >nul
  echo - Created .env ^(safe defaults: paper trading, no API keys needed^)
)

REM --- 5. verify --------------------------------------------------------------
.venv\Scripts\python.exe -c "import fastapi, pandas, langgraph; print('- core packages import cleanly')"

echo.
echo ============================================================
echo   Setup complete.
echo.
echo   Start the dashboard:
echo       .venv\Scripts\python.exe run.py
echo.
echo   Then open  http://127.0.0.1:8000
echo.
echo   Other commands:
echo       .venv\Scripts\python.exe run.py --cycle
echo       .venv\Scripts\python.exe -m pytest -q
echo.
echo   Runs in PAPER mode with no API keys.
echo ============================================================
echo.
pause
