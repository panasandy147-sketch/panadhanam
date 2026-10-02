#!/usr/bin/env bash
# Start Ollama (the local AI model the desk's agents ask) if it is installed
# but not answering, and pull the model once if it is missing. Called by
# ./start.sh; safe to run any time, and does nothing when Ollama is already
# up. Without Ollama the desk still trades on its rule engines.
#
#     ./ollama_up.sh
#
# Reads OLLAMA_HOST and OLLAMA_MODEL from .env (defaults
# http://127.0.0.1:11434 and qwen2.5:7b). Its output goes to logs/ollama.log.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs

env_value() {  # KEY from .env, else the default
  local v
  v="$(grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r"')"
  echo "${v:-$2}"
}

ollama_bin() {
  command -v ollama 2>/dev/null && return
  # The Windows installer puts it here, and Git Bash may not have it on PATH.
  for p in "${LOCALAPPDATA:-}/Programs/Ollama/ollama.exe" "/c/Program Files/Ollama/ollama.exe"; do
    [ -x "$p" ] && { echo "$p"; return; }
  done
}

HOST="$(env_value OLLAMA_HOST http://127.0.0.1:11434)"
MODEL="$(env_value OLLAMA_MODEL qwen2.5:7b)"
NOHUP="$(command -v nohup || true)"
answering() { curl -s -m "${1:-5}" "$HOST/api/tags" >/dev/null 2>&1; }

if ! answering; then
  BIN="$(ollama_bin)"
  if [ -z "$BIN" ]; then
    echo "- Ollama is not installed (optional): the desk runs on its rule engines."
    echo "  To add the local AI: install it from https://ollama.com/download, then run ./start.sh again."
    exit 0
  fi
  echo "- Starting Ollama ($HOST)..."
  $NOHUP "$BIN" serve >> logs/ollama.log 2>&1 &
  for _ in $(seq 1 30); do answering 2 && break; sleep 1; done
  if ! answering; then
    echo "  [!] Ollama did not start — see logs/ollama.log. The desk runs on its rule engines."
    exit 0
  fi
fi
echo "- Ollama is up at $HOST"

if ! curl -s -m 5 "$HOST/api/tags" | grep -q "\"$MODEL\""; then
  BIN="$(ollama_bin)"
  [ -n "$BIN" ] || exit 0
  # One download at a time, whichever desk's start.sh asks first: a marker in
  # the shared temp folder, good for two hours.
  MARK="${TMPDIR:-/tmp}/ollama-pull-${MODEL//[^A-Za-z0-9._-]/_}.started"
  if [ -n "$(find "$MARK" -mmin -120 2>/dev/null)" ]; then
    echo "- The model $MODEL is still downloading (started earlier); see logs/ollama.log."
    exit 0
  fi
  touch "$MARK"
  echo "- The model $MODEL is missing: downloading it in the background (a few GB, once)."
  echo "  The desk uses its rule engines until it finishes; see logs/ollama.log."
  $NOHUP "$BIN" pull "$MODEL" >> logs/ollama.log 2>&1 &
fi
exit 0
