@echo off
REM ---------------------------------------------------------------------------
REM panaoptions — check the config, then start the desk.
REM
REM Double-click it, or from cmd/PowerShell:   start.bat
REM From Git Bash (MINGW64) use ./start.sh instead.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM This folder's own virtual environment (setup.bat makes it); an older
REM checkout inside panadhanam used panadhanam's, one level up. A bare
REM `python` on Windows finds the Microsoft Store build, with no packages.
set PY=
if exist ".venv\Scripts\python.exe"    set PY=.venv\Scripts\python.exe
if "%PY%"=="" if exist "..\.venv\Scripts\python.exe" set PY=..\.venv\Scripts\python.exe

if "%PY%"=="" (
  echo No virtual environment found here or in the parent directory.
  echo Run setup.bat first.
  echo.
  pause
  exit /b 1
)

echo - Using %PY%
echo.

REM A per-machine .env setting can hold an old value after an upgrade meant
REM to change it. Ask preflight what it would suggest rather than hardcoding
REM a figure here, which goes stale the moment the shipped one moves.
REM The per-trade budget is 20%% of capital (25%% on SPY/QQQ/DIA). A value
REM this script wrote before is brought back to the shipped $4,000; a figure
REM you chose yourself is only ever raised.
"%PY%" run.py --ensure-capital
"%PY%" run.py --ensure-profile

for /f "usebackq delims=" %%F in (`"%PY%" run.py --suggest-fix 2^>nul`) do (
  echo - Applying: %%F
  call %%F
  echo.
)

"%PY%" run.py --check-config
if errorlevel 1 (
  echo.
  echo [!] Not starting: the desk would scan all morning and take nothing.
  echo     Read the finding above - it names what to change.
  pause
  exit /b 1
)

REM --check exit codes: 0 fine, 1 no data feed at all, 2 charts work but
REM option chains do not. Only the first is fatal — a desk that can screen,
REM chart and show WHY nothing is being bought is worth having on screen.
REM `errorlevel N` is true for N OR HIGHER, so test 2 before 1.
"%PY%" run.py --check
if errorlevel 2 (
  echo.
  echo [!] Starting anyway - the desk will screen, chart and fire setups,
  echo     but it cannot buy anything until option chains work.
  echo     The dashboard shows this in red at the top.
  echo.
) else if errorlevel 1 (
  echo.
  echo [!] Not starting: no market data at all.
  pause
  exit /b 1
)

if "%PANAOPTIONS_PORT%"=="" set PANAOPTIONS_PORT=8100

echo.
echo Dashboard  ^<-  http://127.0.0.1:%PANAOPTIONS_PORT%
echo Leave this window OPEN — closing it stops the desk.
echo Press Ctrl+C to stop.
echo.

start "" /b cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:%PANAOPTIONS_PORT%"

"%PY%" run.py --port %PANAOPTIONS_PORT% %*
