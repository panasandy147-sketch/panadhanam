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
"$PY" run.py --check-config || {
  echo
  echo "[!] The configuration has a blocker. The desk would scan and take"
  echo "    nothing, so it is not being started. Fix the above, then re-run."
  exit 1
}

"$PY" run.py --check || exit 1

echo
echo "Starting the desk. Leave this window open — Ctrl+C stops it."
echo
exec "$PY" run.py "$@"
