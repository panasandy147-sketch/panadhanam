#!/usr/bin/env bash
# panadhanam — pull, check, start, open the dashboard.
#
# Works in Git Bash (MINGW64) on Windows, and on macOS/Linux. The Windows
# double-click equivalent is start.bat.
#
#     ./start.sh          <- note the ./ — bash does not search the current
#                            directory, so a bare `start.sh` is "command not found"
set -uo pipefail
cd "$(dirname "$0")"

# .venv/bin on Unix, .venv/Scripts on Windows. Plain `python` on Windows finds
# the Microsoft Store build, which has none of this project's packages — that
# is where "No module named pydantic" comes from.
PY=".venv/bin/python"
[ -x "$PY" ] || PY=".venv/Scripts/python.exe"
if [ ! -x "$PY" ]; then
  echo "No virtual environment found at .venv — run ./setup.sh first." >&2
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
# Paper trading is the point of a paper account. A .env created from an older
# template can hold AUTO_PLACE_ORDERS=false, which quietly turns the desk into
# alerts-only — this keeps it on for a simulator and never touches real money.
"$PY" run.py --ensure-paper-orders
"$PY" run.py --check-data
"$PY" run.py --check-broker
"$PY" run.py --check-llm

URL="http://127.0.0.1:8000"
echo
echo "Starting the dashboard at $URL — Ctrl+C to stop."
echo "Leave this window open; closing it stops the desk."
echo

open_browser() {
  sleep 4
  case "$(uname -s)" in
    # Git Bash has no xdg-open, and `start` is a cmd builtin rather than a
    # program, so neither is on PATH. explorer.exe is, and it opens a URL in
    # the default browser. It exits non-zero even on success, hence the `|| :`.
    MINGW*|MSYS*|CYGWIN*) explorer.exe "$URL" || : ;;
    Darwin)               open "$URL" ;;
    *)                    command -v xdg-open >/dev/null && xdg-open "$URL" ;;
  esac
}
open_browser >/dev/null 2>&1 &

exec "$PY" run.py
