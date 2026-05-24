# Pi and Open WebUI Bridge

A bridge between Pi and Open WebUI.

## Why

- Start a session in Pi and continue your session in Open WebUI.
- Start a chat in Open WebUI and run it with bi on the backend.
- Pi sessions synced to Open WebUI and back as you would expect.
- Access Pi loop, extensions, skills, providers, loops.
- Use OpenWebUi Functions and UI. Example: mermaid diagrams.
- Use Pi tooling from Open WebUI.
- Delete a session in Pi and it gets deleted from Open WebUI.
- Delete a Pi-backed chat from Open WebUI and it's session file gets deleted on your computer.

## What

- This is a independent bridge server that presents Pi and its scoped models to Open WebUi in the model list.
- The bridge syncs Pi sessions to Open WebUI.
- The bridge uses Pi RPC mode to act as a provider to Open WebUi. `pi --mode rpc`.
- Use a Pi profile to limit access, tools, etc.

## Install

1. Clone the repo to your computer
2. Copy `pi_agent_bridge_function.py` to a new Open WebUI function with slug `pi_agent_bridge_function.py`.
3. Edit function settings to point to your bridge URL (for example `http://host.docker.internal:8765`)
3. Create an API key to Open WebUI for your user. 
4. Edit `run_pi_bridge.sh` convenience launcher with your Open WebUI server URL, API key.
5. Launch the bridge server `run_pi_bridge.sh`

## Usage

Run the bridge server:

```bash
./run_pi_bridge.sh
```
By default it listens on `127.0.0.1:8765`.

For Open WebUI, point the function at the bridge URL (for example `http://host.docker.internal:8765`).

PI_BRIDGE_PROJECTION_RECENT_DAYS=5

## Pi session continuity

Pi sessions remain the canonical source of truth for Pi-backed chats. Other Open WebUI chats/providers continue to work normally and are not synced to Pi.

The bridge exposes Pi session discovery/projection APIs:

- `GET /pi/sessions` — list indexed Pi sessions
- `GET /pi/sessions?q=<query>` — filter sessions by id/path/cwd/title
- `GET /pi/sessions/{sessionId}` — get one session summary
- `GET /pi/sessions/{sessionId}/transcript` — get parsed transcript messages
- `POST /pi/sessions/{sessionId}/fork` — fork a Pi session
- `POST /pi/sync/openwebui` — run a projection/deletion sync into Open WebUI (limit 50 sessions)
- `POST /pi/sync/openwebui/reset` — "force" sync

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

Conflict policy is "good luck!". There's no way to determine writers to a session outside the bridge, so play with a session from two computers and what will happen are up to luck.

The projected Open WebUI records should be treated as a rebuildable cache; Pi JSONL sessions remain canonical for content. Deletions are bidirectional for mapped Pi-backed chats: deleting the Open WebUI projection deletes the backing Pi JSONL session, and deleting the Pi JSONL session deletes the projected Open WebUI chat.
