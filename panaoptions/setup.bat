@echo off
REM ---------------------------------------------------------------------------
REM panaoptions setup for Windows (cmd.exe / PowerShell / double-click):
REM its own virtual environment (.venv) in this folder. Then start.bat.
REM From Git Bash use ./setup.sh instead.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set PYCMD=
py -3 -c "import sys" >nul 2>&1 && set PYCMD=py -3
if "%PYCMD%"=="" (
  python -c "import sys" >nul 2>&1 && set PYCMD=python
)
if "%PYCMD%"=="" (
  echo [X] Python 3.11 or newer was not found on your PATH.
  echo     Install it from https://www.python.org/downloads/
  echo     and TICK "Add python.exe to PATH" in the installer.
  pause
  exit /b 1
)
%PYCMD% --version

if not exist .venv (
  echo - Creating the virtual environment ^(.venv^)...
  %PYCMD% -m venv .venv
)

echo - Installing packages ^(a minute or two^)...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt --quiet
if errorlevel 1 (
  echo [X] Package install failed. Scroll up for the error.
  pause
  exit /b 1
)

if not exist .env (
  copy .env.example .env >nul
  echo - Created .env ^(paper trading, no API keys needed^)
)

.venv\Scripts\python.exe -c "import fastapi, pandas, httpx, yaml; print('- packages import cleanly')"

echo.
echo ============================================================
echo   Setup complete.
echo   Start the desk:  start.bat   ^(then http://127.0.0.1:8100^)
echo   PAPER ONLY - no broker connection exists.
echo ============================================================
pause
