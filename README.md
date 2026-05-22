# Pi Open WebUI Bridge

A small macOS bridge for routing Open WebUI chat requests to `pi --mode rpc`.

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
