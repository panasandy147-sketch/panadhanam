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
# --check-config prints the command that resolves each blocker.
"$PY" run.py --check-config || {
  echo
  echo "[!] Not starting: the desk would scan all morning and take nothing."
  echo
  # Naming the command was not enough twice over, so offer to run it. It only
  # writes .env, and it prints what it changed.
  printf "    Run the suggested fix now? [y/N] "
  read -r reply
  case "$reply" in
    [yY]*)
      echo
      "$PY" run.py --set PANAOPTIONS_CAPITAL=2000 || exit 1
      echo
      echo "Re-checking..."
      "$PY" run.py --check-config || {
        echo "[!] Still blocked. Read the finding above."
        exit 1
      }
      ;;
    *)
      echo "    Run the command above, then ./start.sh again."
      exit 1
      ;;
  esac
}

"$PY" run.py --check || exit 1

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
