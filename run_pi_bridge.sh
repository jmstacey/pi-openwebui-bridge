#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

HOST="${PI_BRIDGE_HOST:-127.0.0.1}"
PORT="${PI_BRIDGE_PORT:-8765}"

exec python3 pi_bridge_server.py --host "$HOST" --port "$PORT" "$@"
