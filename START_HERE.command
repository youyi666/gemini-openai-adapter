#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if ! bash "$ROOT/scripts/launch_adapter.sh"; then
  echo
  echo "Gemini Adapter failed to start. Press Enter to close this window."
  read -r _
  exit 1
fi
