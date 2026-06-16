#!/usr/bin/env bash
set -euo pipefail

NO_OPEN=0
for arg in "$@"; do
  case "$arg" in
    --no-open|-n)
      NO_OPEN=1
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME="$ROOT/runtime"
PYTHON_BIN="${PYTHON:-python3}"

mkdir -p "$RUNTIME"
cd "$ROOT"

say() {
  printf '[Gemini Adapter] %s\n' "$1"
}

ensure_local_file() {
  local source="$1"
  local target="$2"
  if [ ! -f "$ROOT/$target" ]; then
    cp "$ROOT/$source" "$ROOT/$target"
    say "Created $target"
  fi
}

test_dependencies() {
  "$PYTHON_BIN" -c 'import fastapi, sse_starlette, uvicorn, curl_cffi' >/dev/null 2>&1
}

get_health() {
  local port="$1"
  curl --silent --show-error --fail --noproxy '*' --max-time 3 "http://127.0.0.1:$port/health" 2>/dev/null || true
}

port_listening() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

ensure_local_file "examples/adapter_env.example.sh" "adapter_env.local.sh"
ensure_local_file "examples/gemini_cookies.example.json" "gemini_cookies.local.json"

if ! test_dependencies; then
  say "Missing dependencies; installing..."
  bash "$SCRIPT_DIR/install_adapter_dependencies.sh"
fi

# shellcheck disable=SC1091
source "$ROOT/adapter_env.local.sh"

PORT="${OPENAI_ADAPTER_PORT:-8000}"
export OPENAI_ADAPTER_SERVER_LOG_PATH="${OPENAI_ADAPTER_SERVER_LOG_PATH:-$RUNTIME/server.log}"
mkdir -p "$(dirname "$OPENAI_ADAPTER_SERVER_LOG_PATH")"

HEALTH="$(get_health "$PORT")"
if [ -n "$HEALTH" ]; then
  say "Server already running."
else
  if port_listening "$PORT"; then
    say "Port $PORT is already in use, but /health is not available."
  else
    say "Starting server in background..."
    nohup bash "$SCRIPT_DIR/run_server.sh" >/dev/null 2>&1 &

    deadline=$((SECONDS + 75))
    while [ "$SECONDS" -lt "$deadline" ]; do
      sleep 2
      HEALTH="$(get_health "$PORT")"
      if [ -n "$HEALTH" ]; then
        break
      fi
    done

    if [ -n "$HEALTH" ]; then
      say "Server started."
    else
      say "Server is still starting or failed. Check Terminal Output in the panel."
    fi
  fi
fi

URL="http://127.0.0.1:$PORT/"
if [ "$NO_OPEN" -eq 0 ]; then
  if command -v open >/dev/null 2>&1; then
    open "$URL"
    say "Opened panel: $URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
    say "Opened panel: $URL"
  else
    say "Panel: $URL"
  fi
else
  say "Panel: $URL"
fi
