@echo off
REM ---------------------------------------------------------------------------
REM panaoptions — check the config, then start the desk.
REM
REM Double-click it, or from cmd/PowerShell:   start.bat
REM From Git Bash (MINGW64) use ./start.sh instead.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM panadhanam's virtual environment, one level up, already carries every
REM package panaoptions needs. A bare `python` on Windows finds the Microsoft
REM Store build, which has none of them.
set PY=
if exist ".venv\Scripts\python.exe"    set PY=.venv\Scripts\python.exe
if "%PY%"=="" if exist "..\.venv\Scripts\python.exe" set PY=..\.venv\Scripts\python.exe

if "%PY%"=="" (
  echo No virtual environment found here or in the parent directory.
  echo Run ..\setup.bat first.
  echo.
  pause
  exit /b 1
)

echo - Using %PY%
echo.

"%PY%" run.py --check-config
if errorlevel 1 (
  echo.
  echo [!] The configuration has a blocker. The desk would scan and take
  echo     nothing, so it is not being started. Fix the above, then re-run.
  echo.
  pause
  exit /b 1
)

"%PY%" run.py --check
if errorlevel 1 (
  pause
  exit /b 1
)

echo.
echo Starting the desk. Leave this window OPEN — closing it stops the desk.
echo Press Ctrl+C to stop.
echo.

"%PY%" run.py %*
