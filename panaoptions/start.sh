#!/usr/bin/env bash
# panaoptions — check the config, then start the desk.
#
# Works in Git Bash (MINGW64) on Windows, and on macOS/Linux.
#
#     ./start.sh          <- the ./ is required; bash does not search the
#                            current directory
set -uo pipefail
cd "$(dirname "$0")"

# panadhanam's virtual environment, one level up, already carries every
# package panaoptions needs. Prefer a local .venv if one exists, fall back to
# the parent, and only then to whatever `python` means — which on Windows is
# the Microsoft Store build and has none of the packages.
PY=""
for candidate in \
  ".venv/bin/python" ".venv/Scripts/python.exe" \
  "../.venv/bin/python" "../.venv/Scripts/python.exe"; do
  [ -x "$candidate" ] && { PY="$candidate"; break; }
done
if [ -z "$PY" ]; then
  echo "No virtual environment found here or in the parent directory." >&2
  echo "Run ../setup.sh (or ../setup.bat) first." >&2
  exit 1
fi

echo "- Using $PY"
echo
# A per-machine .env setting can hold an old value after an upgrade meant to
# change it — it wins over the repo by design, and nobody goes looking. Ask
# preflight what it would suggest rather than hardcoding a figure here, which
# goes stale the moment the shipped one moves.
#
# Only ever writes .env, and prints what it changed.
# The per-trade budget is 20% of capital; a stale .env below the shipped
# $4,000 would quietly cut it. Raises only, never lowers, prints what it did.
"$PY" run.py --ensure-capital || true

FIX="$("$PY" run.py --suggest-fix 2>/dev/null)" || FIX=""
if [ -n "$FIX" ]; then
  echo "- Applying: $FIX"
  eval "$FIX" || echo "  (could not apply — carrying on with the current settings)"
  echo
fi

# --check-config prints the command that resolves each blocker.
"$PY" run.py --check-config || {
  echo
  echo "[!] Not starting: the desk would scan all morning and take nothing."
  echo "    Read the finding above — it names what to change."
  exit 1
}

# --check exit codes: 0 fine, 1 no data feed at all, 2 charts work but option
# chains do not. Only the first is fatal. A desk that can screen, chart and
# show WHY nothing is being bought is worth having on screen — refusing to
# start would hide the very banner that explains it.
"$PY" run.py --check
case $? in
  0) ;;
  2)
    echo
    echo "[!] Starting anyway — the desk will screen, chart and fire setups,"
    echo "    but it cannot buy anything until option chains work."
    echo "    The dashboard shows this in red at the top."
    echo
    ;;
  *)
    echo
    echo "[!] Not starting: no market data at all."
    exit 1
    ;;
esac

PORT="${PANAOPTIONS_PORT:-8100}"
URL="http://127.0.0.1:$PORT"
echo
echo "Dashboard → $URL"
echo "Leave this window open — Ctrl+C stops the desk."
echo

# Give uvicorn a moment to bind before pointing a browser at it.
( sleep 4
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) explorer.exe "$URL" || : ;;
    Darwin)               open "$URL" ;;
    *)                    command -v xdg-open >/dev/null && xdg-open "$URL" ;;
  esac ) >/dev/null 2>&1 &

exec "$PY" run.py --port "$PORT" "$@"
