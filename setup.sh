#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# panadhanam setup — works on Windows (Git Bash), macOS and Linux.
# No `make` required.
#
#   bash setup.sh
# ---------------------------------------------------------------------------
set -e

cd "$(dirname "$0")"

# --- 1. find a usable Python -----------------------------------------------
PY=""
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1; then
    # `py` needs the -3 switch; also skip the Windows Store stub that exits 9009
    if [ "$candidate" = "py" ]; then
      if py -3 -c "import sys" >/dev/null 2>&1; then PY="py -3"; break; fi
    elif "$candidate" -c "import sys; assert sys.version_info >= (3, 11)" >/dev/null 2>&1; then
      PY="$candidate"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "❌ Python 3.11 or newer was not found on your PATH."
  echo ""
  echo "   Windows : install from https://www.python.org/downloads/"
  echo "             and TICK 'Add python.exe to PATH' in the installer."
  echo "   macOS   : brew install python@3.12"
  echo "   Linux   : sudo apt install python3 python3-venv"
  exit 1
fi

echo "✔ Using: $($PY --version)"

# --- 2. create the virtual environment -------------------------------------
if [ ! -d .venv ]; then
  echo "→ Creating virtual environment (.venv)…"
  $PY -m venv .venv
fi

# Windows venvs put executables in Scripts/, everyone else uses bin/
if [ -d .venv/Scripts ]; then
  VPY=".venv/Scripts/python.exe"
else
  VPY=".venv/bin/python"
fi

# --- 3. install dependencies ------------------------------------------------
echo "→ Installing dependencies (this takes a minute or two)…"
"$VPY" -m pip install --upgrade pip --quiet
"$VPY" -m pip install -r requirements.txt --quiet

# --- 4. first-run config ----------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  echo "✔ Created .env (safe defaults: paper trading, no API keys needed)"
fi

# --- 5. verify it actually works -------------------------------------------
echo "→ Verifying the install…"
"$VPY" -c "import fastapi, pandas, langgraph; print('✔ core packages import cleanly')"

cat <<BANNER

============================================================
  ✅ Setup complete.

  Start the dashboard:
      $VPY run.py

  Then open  http://127.0.0.1:8000

  Other commands:
      $VPY run.py --cycle       one analysis cycle in the terminal
      $VPY run.py --premarket   pre-market scan
      $VPY -m pytest -q         run the tests

  Runs in PAPER mode with no API keys. See README.md to connect
  a broker or enable Claude reasoning.
============================================================
BANNER
