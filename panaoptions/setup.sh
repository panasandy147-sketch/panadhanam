#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# panaoptions setup — its own virtual environment (.venv) in this folder.
# Works on Windows (Git Bash), macOS and Linux. Run once, then ./start.sh.
#
#   ./setup.sh
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
  echo "Python 3.11 or newer was not found on your PATH."
  echo "  Windows: install from https://www.python.org/downloads/ and tick"
  echo "           'Add python.exe to PATH' in the installer."
  exit 1
fi
echo "- Using: $($PY --version)"

# --- 2. the virtual environment ---------------------------------------------
if [ ! -d .venv ]; then
  echo "- Creating the virtual environment (.venv)..."
  $PY -m venv .venv
fi
if [ -d .venv/Scripts ]; then VPY=".venv/Scripts/python.exe"; else VPY=".venv/bin/python"; fi

# --- 3. packages --------------------------------------------------------------
echo "- Installing packages (a minute or two)..."
"$VPY" -m pip install --upgrade pip --quiet
"$VPY" -m pip install -r requirements.txt -r requirements-dev.txt --quiet

# --- 4. first-run config ------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  echo "- Created .env (paper trading, no API keys needed)"
fi

"$VPY" -c "import fastapi, pandas, httpx, yaml; print('- packages import cleanly')"

cat <<BANNER

============================================================
  Setup complete.

  Start the desk:   ./start.sh      (then http://127.0.0.1:8100)
  Run the tests:    $VPY -m pytest -q

  PAPER ONLY — no broker connection exists.
============================================================
BANNER
