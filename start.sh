#!/usr/bin/env bash
# panadhanam — pull, check, start, open the dashboard.
# The Windows equivalent is start.bat.
set -uo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
[ -x "$PY" ] || PY=".venv/Scripts/python.exe"      # Git Bash on Windows
if [ ! -x "$PY" ]; then
  echo "No virtual environment found. Run ./setup.sh first." >&2
  exit 1
fi

echo "- Pulling the latest code..."
git pull --ff-only || cat <<'MSG'

[!] The pull did not go through cleanly — usually a locally edited tracked
    file. To discard those edits and take the repository version:
        git checkout config/settings.yaml
    Your .env is NOT tracked and is never touched by a pull.

MSG

echo "- Checking dependencies..."
"$PY" -m pip install -r requirements.txt --quiet --disable-pip-version-check

echo
"$PY" run.py --check-data
"$PY" run.py --check-broker
"$PY" run.py --check-llm

URL="http://127.0.0.1:8000"
echo
echo "Starting the dashboard at $URL — Ctrl+C to stop."

( sleep 4
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  elif command -v open     >/dev/null 2>&1; then open "$URL"
  elif command -v start    >/dev/null 2>&1; then start "$URL"
  fi ) >/dev/null 2>&1 &

exec "$PY" run.py
