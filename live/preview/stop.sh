#!/usr/bin/env bash
# Stops the preview and removes its containers. Keeps nothing that matters.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out="${PREVIEW_DIR:-$here/.preview}"
if [ -f "$out/service.pid" ]; then kill "$(cat "$out/service.pid")" 2>/dev/null || true; rm -f "$out/service.pid"; fi
if [ -f "$out/studio.pid" ]; then kill "$(cat "$out/studio.pid")" 2>/dev/null || true; rm -f "$out/studio.pid"; fi
cd "$here/../tests/e2e" && docker compose down -v
