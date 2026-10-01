#!/usr/bin/env bash
# Keep both desks on the latest code without anyone at the machine.
#
#     ./auto_update.sh                  check every 30 minutes, forever
#     ./auto_update.sh --interval 900   every 15 minutes
#     ./auto_update.sh --once           one check, then exit (for a test)
#
# Leave it running in its own Git Bash window. It starts Ollama if it is
# installed but not running, starts panadhanam (:8000) and panaoptions (:8100)
# in the background if they are not already up, opens both dashboards in the
# browser (this first time only), then every interval:
#
#   1. git fetch — only this branch, read-only; nothing is pushed, nothing on
#      this machine is opened to the internet.
#   2. No new commits: nothing happens. The desks keep running.
#   3. New commits: git pull --ff-only, then stop both desks and start them
#      again on the new code, WITHOUT opening new browser tabs (NO_BROWSER=1):
#      the dashboards already open reconnect by themselves. A restart
#      mid-session is safe — each desk restores the day's trade count, its
#      open positions and any daily lockout.
#   4. A pull that fails (a locally edited file, no network) changes nothing:
#      the desks keep running the code they have, and it tries again next time.
#
# Everything it does goes to logs/auto_update.log; each desk's own output to
# logs/panadhanam.log and logs/panaoptions.log. Paper trading only, as always.
set -uo pipefail
cd "$(dirname "$0")"

ARGS=("$@")
INTERVAL=1800
ONCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --interval) INTERVAL="$2"; shift 2 ;;
    --once)     ONCE=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

mkdir -p logs
LOG="logs/auto_update.log"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
PORT_PD=8000
PORT_PO="${PANAOPTIONS_PORT:-8100}"

NOHUP="$(command -v nohup || true)"      # Git for Windows ships it; if not, plain &

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

# The process listening on a port: Windows (Git Bash) via netstat's PID
# column, macOS/Linux via lsof.
pid_on_port() {
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      netstat -ano 2>/dev/null | tr -d '\r' \
        | awk -v p=":$1" '$2 ~ p"$" && $4 == "LISTENING" {print $5; exit}' ;;
    *) command -v lsof >/dev/null && lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -1 ;;
  esac
}

up() { [ -n "$(pid_on_port "$1")" ]; }

stop_port() {
  local pid
  pid="$(pid_on_port "$1")"
  [ -z "$pid" ] && return 0
  say "stopping the desk on :$1 (pid $pid)"
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) taskkill //PID "$pid" //T //F >/dev/null 2>&1 || : ;;
    *) kill "$pid" 2>/dev/null || : ;;
  esac
  for _ in $(seq 1 20); do up "$1" || return 0; sleep 1; done
  say "[!] :$1 still busy after 20s"
}

start_panadhanam() {
  say "starting panadhanam on :$PORT_PD"
  NO_BROWSER=1 $NOHUP ./start.sh >> logs/panadhanam.log 2>&1 &
}

start_panaoptions() {
  say "starting panaoptions on :$PORT_PO"
  # exec: the subshell BECOMES the desk rather than lingering to wait for it.
  ( cd panaoptions && exec env NO_BROWSER=1 $NOHUP ./start.sh >> ../logs/panaoptions.log 2>&1 ) &
}

# The local model (panadhanam's AI reasoning, the panaoptions agents): the
# desks only CALL Ollama, they never start it. If it is installed but not
# answering, start `ollama serve`; pull the model only if it is missing.
# Without it both desks still trade on their rule engines.
env_value() {  # KEY from .env, else the default
  local v
  v="$(grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r"')"
  echo "${v:-$2}"
}

ollama_bin() {
  command -v ollama 2>/dev/null && return
  for p in "${LOCALAPPDATA:-}/Programs/Ollama/ollama.exe" "/c/Program Files/Ollama/ollama.exe"; do
    [ -x "$p" ] && { echo "$p"; return; }
  done
}

ensure_ollama() {
  local host model bin
  host="$(env_value OLLAMA_HOST http://127.0.0.1:11434)"
  model="$(env_value OLLAMA_MODEL qwen2.5:7b)"
  if curl -s -m 5 "$host/api/tags" >/dev/null 2>&1; then
    : # already answering
  else
    bin="$(ollama_bin)"
    if [ -z "$bin" ]; then
      say "Ollama is not installed — the desks run on their rule engines (docs/OLLAMA.md)"
      return
    fi
    say "Ollama is not answering at $host — starting ollama serve"
    $NOHUP "$bin" serve >> logs/ollama.log 2>&1 &
    for _ in $(seq 1 30); do curl -s -m 2 "$host/api/tags" >/dev/null 2>&1 && break; sleep 1; done
    curl -s -m 5 "$host/api/tags" >/dev/null 2>&1 \
      || { say "[!] Ollama did not start — see logs/ollama.log"; return; }
    say "Ollama is up"
  fi
  if ! curl -s -m 5 "$host/api/tags" | grep -q "\"$model\""; then
    # One pull per run of this script (a reload keeps the flag): a large
    # download still going when the next check comes is not started twice.
    [ -n "${OLLAMA_PULL_STARTED:-}" ] && return
    bin="$(ollama_bin)"
    [ -n "$bin" ] || return
    say "the model $model is missing — pulling it in the background (a few GB, once)"
    $NOHUP "$bin" pull "$model" >> logs/ollama.log 2>&1 &
    export OLLAMA_PULL_STARTED=1
  fi
}

# Both dashboards in the default browser: only when you start this script,
# never on its own restarts or reloads.
open_tabs() {
  [ -n "${AUTO_UPDATE_RELOADED:-}" ] && return
  local url
  for url in "http://127.0.0.1:$PORT_PD" "http://127.0.0.1:$PORT_PO"; do
    case "$(uname -s)" in
      MINGW*|MSYS*|CYGWIN*) explorer.exe "$url" >/dev/null 2>&1 || : ;;
      Darwin)               open "$url" ;;
      *)                    command -v xdg-open >/dev/null && xdg-open "$url" >/dev/null 2>&1 ;;
    esac
  done
  say "opened both dashboards in the browser"
}

wait_up() {
  for _ in $(seq 1 90); do up "$1" && { say "$2 is up on :$1"; return 0; }; sleep 2; done
  say "[!] $2 did not come up on :$1 within 3 minutes — see logs/$2.log"
}

restart_all() {
  stop_port "$PORT_PD"
  stop_port "$PORT_PO"
  start_panadhanam
  start_panaoptions
  wait_up "$PORT_PD" panadhanam
  wait_up "$PORT_PO" panaoptions
}

check() {
  if ! git fetch --quiet origin "$BRANCH" 2>>"$LOG"; then
    say "fetch failed (network?) — keeping the current code, trying again later"
    return
  fi
  local here there
  here="$(git rev-parse HEAD)"
  there="$(git rev-parse "origin/$BRANCH")"
  if [ "$here" = "$there" ]; then
    say "no new commits on $BRANCH ($(git rev-parse --short HEAD)) — nothing to do"
    return
  fi
  if ! git merge-base --is-ancestor HEAD "origin/$BRANCH"; then
    say "[!] this copy has commits origin does not — not pulling; check by hand"
    return
  fi
  say "new commits on $BRANCH: $(git rev-parse --short HEAD) -> $(git rev-parse --short "origin/$BRANCH")"
  git log --oneline "HEAD..origin/$BRANCH" | sed 's/^/    /' | tee -a "$LOG"
  if ! git pull --ff-only --quiet origin "$BRANCH" >>"$LOG" 2>&1; then
    say "[!] git pull failed (a locally edited file?) — the desks keep the old code. " \
        "git status shows what is in the way."
    return
  fi
  say "pulled $(git rev-parse --short HEAD) — restarting both desks"
  ensure_ollama
  restart_all
  # The pull changed this script: carry on as the NEW version (the desks are
  # already up, so it does not restart them again).
  if git diff --name-only "$here" HEAD | grep -qx "auto_update.sh"; then
    say "auto_update.sh itself changed — reloading it"
    AUTO_UPDATE_RELOADED=1 exec bash ./auto_update.sh "${ARGS[@]}"
  fi
}

main() {
  say "auto-update on $BRANCH, every $((INTERVAL / 60)) min (log: $LOG)"
  ensure_ollama
  up "$PORT_PD" || { start_panadhanam; wait_up "$PORT_PD" panadhanam; }
  up "$PORT_PO" || { start_panaoptions; wait_up "$PORT_PO" panaoptions; }
  open_tabs
  while :; do
    check
    [ -n "$ONCE" ] && exit 0
    sleep "$INTERVAL"
    ensure_ollama          # started again if it stopped in the meantime
  done
}

# Read to the end before running: a pull that rewrites this file must not
# change what the running copy executes half-way through.
main; exit
