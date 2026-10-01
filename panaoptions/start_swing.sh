#!/usr/bin/env bash
# panaoptions — the swing desk: 1-4 day options, US and India, its own
# book and journal (data/swing, journal/swing). Runs beside the 0DTE desk.
#
#     ./start_swing.sh    <- in its own Git Bash window, then open
#                            http://127.0.0.1:8102
set -uo pipefail
cd "$(dirname "$0")"
exec env PANAOPTIONS_PROFILE=swing PANAOPTIONS_DESK_DIR=swing \
  PANAOPTIONS_PORT="${PANAOPTIONS_SWING_PORT:-8102}" PANAOPTIONS_MARKET=AUTO \
  ./start.sh "$@"
