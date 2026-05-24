#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
ENV_FILE="${PI_BRIDGE_ENV_FILE:-$HOME/.config/pi-openwebui-bridge/bridge.env}"

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

if [ ! -f "$SCRIPT_DIR/pi_bridge_server.py" ]; then
  fail "Error: cannot find pi_bridge_server.py next to the launcher: $SCRIPT_DIR/pi_bridge_server.py"
fi

if [ ! -f "$ENV_FILE" ]; then
  fail "Error: missing bridge config file: $ENV_FILE

Fix:
  1. mkdir -p \"$(dirname "$ENV_FILE")\"
  2. cp \"$SCRIPT_DIR/bridge.env.example\" \"$ENV_FILE\"
  3. edit $ENV_FILE and set OPENWEBUI_URL and OPENWEBUI_API_KEY"
fi

set -a
. "$ENV_FILE"
set +a

BRIDGE_HOST="${PI_BRIDGE_HOST:-127.0.0.1}"
BRIDGE_PORT="${PI_BRIDGE_PORT:-8765}"
PI_BINARY="${PI_BINARY:-pi}"

require_var() {
  eval "value=\${$1:-}"
  [ -n "$value" ] || fail "Error: $1 is not set in $ENV_FILE"
}

require_var OPENWEBUI_URL
require_var OPENWEBUI_API_KEY

case "$OPENWEBUI_URL" in
  http://*|https://*) ;;
  *) fail "Error: OPENWEBUI_URL must start with http:// or https:// (current: $OPENWEBUI_URL)" ;;
esac

case "$BRIDGE_PORT" in
  ''|*[!0-9]*) fail "Error: PI_BRIDGE_PORT must be a number (current: $BRIDGE_PORT)" ;;
esac

case "$PI_BINARY" in
  */*)
    [ -x "$PI_BINARY" ] || fail "Error: PI_BINARY is not executable or not found: $PI_BINARY

Fix:
  Set PI_BINARY to the absolute path to Pi in $ENV_FILE (for example /usr/local/bin/pi)."
    ;;
  *)
    RESOLVED_PI_BINARY=$(command -v "$PI_BINARY" 2>/dev/null || true)
    [ -n "$RESOLVED_PI_BINARY" ] || fail "Error: PI_BINARY '$PI_BINARY' was not found on PATH.

Fix:
  Set PI_BINARY to the absolute path to Pi in $ENV_FILE (for example /usr/local/bin/pi)."
    PI_BINARY="$RESOLVED_PI_BINARY"
    ;;
esac

# Avoid macOS/Xcode Python launcher leakage from parent environments.
unset __PYVENV_LAUNCHER__

if [ -n "${PYTHON3:-}" ]; then
  [ -x "$PYTHON3" ] || fail "Error: PYTHON3 is set but not executable: $PYTHON3"
elif [ -x /usr/local/bin/python3 ]; then
  PYTHON3=/usr/local/bin/python3
else
  PYTHON3=$(command -v python3 2>/dev/null || true)
  if [ -z "$PYTHON3" ] && [ -x /usr/bin/python3 ]; then
    PYTHON3=/usr/bin/python3
  fi
fi
[ -n "$PYTHON3" ] || fail "Error: python3 was not found. Install Python 3 or set PYTHON3 to its absolute path."

exec "$PYTHON3" "$SCRIPT_DIR/pi_bridge_server.py" --pi-binary "$PI_BINARY" --host "$BRIDGE_HOST" --port "$BRIDGE_PORT" "$@"
