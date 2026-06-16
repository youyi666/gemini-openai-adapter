#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME="$ROOT/runtime"
PYTHON_BIN="${PYTHON:-python3}"

mkdir -p "$RUNTIME"
cd "$ROOT"

if [ ! -f "$ROOT/adapter_env.local.sh" ]; then
  echo "Missing adapter_env.local.sh" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$ROOT/adapter_env.local.sh"

if [ -z "${OPENAI_ADAPTER_SERVER_LOG_PATH:-}" ]; then
  export OPENAI_ADAPTER_SERVER_LOG_PATH="$RUNTIME/server.log"
fi

mkdir -p "$(dirname "$OPENAI_ADAPTER_SERVER_LOG_PATH")"
printf '\n=== Adapter server started at %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$OPENAI_ADAPTER_SERVER_LOG_PATH"

exec "$PYTHON_BIN" "$ROOT/openai_adapter_server.py" >> "$OPENAI_ADAPTER_SERVER_LOG_PATH" 2>&1
