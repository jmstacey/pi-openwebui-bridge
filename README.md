# [Pi](https://pi.dev) and [Open WebUI](https://openwebui.com) Bridge

A bridge between [Pi](https://pi.dev) and [Open WebUI](https://openwebui.com).

## Why

- Start a session in Pi and continue your session in Open WebUI.
- Start a chat in Open WebUI and run it with Pi on the backend.
- Pi sessions synced to Open WebUI and back as you would expect.
- Access Pi's extensions, skills, providers, loop... all from Open WebUI.
- Use Open WebUI Functions and Tools with your session backed in Pi.
- Delete a session in Pi and it gets deleted from Open WebUI.
- Delete a Pi-backed chat from Open WebUI and its session file gets deleted on your computer.

## What

- This is an independent bridge server that presents Pi and its scoped models to Open WebUI in the model list.
- The bridge syncs Pi sessions to Open WebUI.
- The bridge uses Pi RPC mode to act as a provider to Open WebUI. `pi --mode rpc`.
- Use a Pi profile to limit access, tools, etc.

## Demo 

https://github.com/user-attachments/assets/8beccfbe-c393-4f5b-9c38-16a2aa0bb7bd


## Install

1. Clone the repo to your computer.
2. Copy `pi_agent_bridge_function.py` to a new Open WebUI function with slug `pi_agent_bridge_function.py`.
3. Edit function settings to point to your bridge URL (for example `http://host.docker.internal:8765`).
4. Copy `bridge.env.example` to your local config file:

   ```bash
   mkdir -p ~/.config/pi-openwebui-bridge
   cp bridge.env.example ~/.config/pi-openwebui-bridge/bridge.env
   ```

5. Edit `~/.config/pi-openwebui-bridge/bridge.env` and set `OPENWEBUI_URL`, `OPENWEBUI_API_KEY`, and `PI_BINARY` if `pi` is not already on your PATH.
6. Launch the bridge with `./run_pi_bridge.sh`.

## Usage

Run the bridge server manually:

```bash
./run_pi_bridge.sh
```

By default it listens on `127.0.0.1:8765`.
For Open WebUI, point the function at the bridge URL (for example `http://host.docker.internal:8765`).

To configure an automatic macOS launch:

1. Copy `com.example.pi-openwebui-bridge.plist` to `~/Library/LaunchAgents/com.example.pi-openwebui-bridge.plist`, or rename it to your own reverse-DNS label.
2. Replace `/ABSOLUTE/PATH/TO/REPO/run_pi_bridge.sh` with the actual path to your clone.
3. If you rename the plist, update the `Label` value inside the plist to match.
4. Make sure your local config file exists at `~/.config/pi-openwebui-bridge/bridge.env` and set `PI_BINARY=pi` (or your own absolute path) if needed.
5. Load it with:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.pi-openwebui-bridge.plist
   ```

To unload it later:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.example.pi-openwebui-bridge.plist
```

The checked-in plist is an example template only.

### Restart continuity troubleshooting

The bridge persists OpenWebUI chat continuity in `chat_sessions.json` under the configured Pi bridge session directory. If an existing OpenWebUI chat appears to forget earlier context after a bridge restart:

1. Check bridge diagnostics:

   ```bash
   curl http://127.0.0.1:8765/sessions
   ```

2. Confirm `mapping_file` is the expected path and `mapping_file_writeable` is `true`.
3. Check the bridge stderr log for `[persistence]` or `[sessions]` messages.

When a mapping is missing, the bridge attempts to recover by matching prior OpenWebUI user messages to existing Pi session JSONL files before creating a new session.

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

The polling interval defaults to 30 seconds and can be changed with `PI_BRIDGE_PROJECTION_SYNC_INTERVAL` or `--projection-sync-interval`.

To continue a Pi session from Open WebUI, pass either `pi_session_id` or `pi_session_file` in the Open WebUI chat metadata/body. The function forwards that metadata to the bridge, which starts Pi with `--session <file>`. Projected Open WebUI chat IDs are also mapped back to their Pi session files so normal Open WebUI chat requests can resume the projected Pi session.

## Watch Out!

The Pi JSONL session files are the source of truth. The chats synced to Open WebUI are treated as rebuildable cache.

Deletions are bidirectional for mapped Pi-backed chats. Deleting the Open WebUI synced chat deletes the Pi JSONL session file, and deleting the Pi JSONL session deletes the previously synced Open WebUI chat. Open WebUi chats that use providers other than Pi are not synced back to Pi, so your OpenRouter/Grok/whoever chat's won't suddenly flood into Pi session history.

Don't play with the same session in Pi TUI and Open WebUI at the same time. Session conflict policy from simultaneous use is "good luck!". There's no locking in Pi to protect from stomping on sessions.

This is intended to run on a trusted local network, and dangerously on the server. There's no interactive user element for guardrails to protect from wiping your hard drive, for example. This bridge was also created with a single-user Open WebUi instance in mind.
