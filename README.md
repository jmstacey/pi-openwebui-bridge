# Pi Open WebUI Bridge

A small macOS bridge for routing Open WebUI chat requests to `pi-life-web --mode rpc`.

## Files
- `pi_bridge_server.py` — host bridge server
- `pi_agent_bridge_function.py` — Open WebUI function
- `run_pi_bridge.sh` — convenience launcher for the server

## Usage

Run the bridge server:

```bash
./run_pi_bridge.sh
```

By default it listens on `127.0.0.1:8765`.

For Open WebUI, point the function at the bridge URL (for example `http://host.docker.internal:8765`).

## Pi session continuity

Pi sessions remain the canonical source of truth for Pi-backed chats only. Other Open WebUI chats/providers continue to work normally.

The bridge exposes Pi session discovery/projection APIs:

- `GET /pi/sessions` — list indexed Pi sessions
- `GET /pi/sessions?q=<query>` — filter sessions by id/path/cwd/title
- `GET /pi/sessions/{sessionId}` — get one session summary
- `GET /pi/sessions/{sessionId}/transcript` — get parsed transcript messages
- `POST /pi/sessions/{sessionId}/fork` — fork a Pi session
- `POST /pi/sync/openwebui` — run a projection/deletion sync into Open WebUI

Indexed roots default to:

- `~/.pi/agent/sessions/owui-sessions` for OpenWebUI-started Pi sessions
- `~/.pi/agent/sessions`

Override with `PI_BRIDGE_INDEXED_SESSION_DIRS` or `--indexed-session-dirs` using a comma-separated list.

To import Pi sessions into Open WebUI chat history/search, configure the bridge with an Open WebUI API token:

```bash
OPENWEBUI_URL="https://127.0.0.1:3000" \
OPENWEBUI_API_KEY=". . ." \
./run_pi_bridge.sh
```

When configured, the bridge projects Pi sessions into Open WebUI:

- once on bridge start
- whenever indexed Pi session files are created, modified, or deleted, via a polling watcher
- when manually triggered with `POST /pi/sync/openwebui`

The polling interval defaults to 5 seconds and can be changed with `PI_BRIDGE_PROJECTION_SYNC_INTERVAL` or `--projection-sync-interval`.

To continue a canonical Pi session from Open WebUI, pass either `pi_session_id` or `pi_session_file` in the Open WebUI chat metadata/body. The function forwards that metadata to the bridge, which starts Pi with `--session <file>`. Projected Open WebUI chat IDs are also mapped back to their Pi session files so normal Open WebUI chat requests can resume the projected Pi session.

Conflict policy is fork-on-contention for bridge-owned active writers: if another active Open WebUI bridge process is already writing to the same Pi session, the new request forks the target session and continues in the fork. External Pi TUI activity is not currently lock-detectable by Pi RPC, so a future UI should present Pi sessions as explicit attach/fork choices.

The projected Open WebUI records should be treated as a rebuildable cache; Pi JSONL sessions remain canonical for content. Deletions are bidirectional for mapped Pi-backed chats: deleting the Open WebUI projection deletes the backing Pi JSONL session, and deleting the Pi JSONL session deletes the projected Open WebUI chat.
