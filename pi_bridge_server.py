#!/usr/bin/env python3
"""
Pi Bridge Server

A small macOS host bridge for OpenWebUI Functions running in Docker.
It owns pi --mode rpc subprocesses and exposes a narrow HTTP/SSE API.

Endpoints:
  GET  /health
  GET  /models
  POST /chat/stream

No third-party dependencies. Run with:
  python3 pi_bridge_server.py --host 127.0.0.1 --port 8765

If Docker cannot reach 127.0.0.1, use:
  PI_BRIDGE_TOKEN="some-secret" python3 pi_bridge_server.py --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Generator, Optional
from urllib.parse import urlparse


PI_SETTINGS = Path.home() / ".pi" / "agent" / "settings.json"
THINKING_LEVEL_MAP = {
    "none": "off",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
}


def safe_model_id(native_id: str) -> str:
    return native_id.replace("/", "--")


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    cleaned = ANSI_RE.sub("", str(text or ""))
    return " ".join(cleaned.replace("\r", " ").replace("\n", " ").split())


def sse_line(event: dict) -> bytes:
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")


class PiBridgeState:
    def __init__(
        self,
        pi_binary: str = "pi",
        session_dir: str = str(Path.home() / ".pi" / "owui-sessions"),
        idle_timeout_seconds: int = 300,
        fallback_model: str = "openai-codex/gpt-5.2",
        token: str = "",
        workspace_dir: str = str(Path.home() / ".pi" / "owui-bridge-workspace"),
        exclude_loadout_extension: bool = True,
    ):
        self.pi_binary = pi_binary
        self.session_dir = Path(session_dir).expanduser()
        self.idle_timeout_seconds = idle_timeout_seconds
        self.fallback_model = fallback_model
        self.token = token
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.exclude_loadout_extension = exclude_loadout_extension

        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._write_workspace_settings()
        self.mapping_file = self.session_dir / "chat_sessions.json"
        self.mapping: dict[str, str] = self._load_mapping()

        self.procs: dict[str, subprocess.Popen] = {}
        self.last_used: dict[str, float] = {}
        self.chat_locks: dict[str, threading.RLock] = {}
        self.state_lock = threading.RLock()

        self.model_list: list[dict] = []
        self.model_map: dict[str, tuple[str, str]] = {}
        self.model_debug: dict[str, Any] = {}
        self.status_cache: dict[str, tuple[str, float]] = {}

    # ── Bridge workspace ─────────────────────────────────────────

    def _write_workspace_settings(self):
        """Create a project-local Pi settings override for bridge-launched Pi.

        Pi has no CLI flag to load all extensions except one package extension.
        Package filtering is supported through project `.pi/settings.json`, and
        project package entries win over global package entries with the same
        identity. This leaves the user's global Pi config untouched while making
        bridge-launched Pi sessions load every global extension except the
        pi-loadout extension.
        """
        project_pi = self.workspace_dir / ".pi"
        project_pi.mkdir(parents=True, exist_ok=True)
        settings_path = project_pi / "settings.json"
        settings: dict[str, Any] = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
            except Exception:
                settings = {}
        if self.exclude_loadout_extension:
            settings["packages"] = [
                {"source": "npm:pi-loadout", "extensions": []}
            ]
        else:
            # Remove our override if explicitly disabled and it is the only thing
            # we manage. Preserve unrelated project settings if the user added any.
            if settings.get("packages") == [{"source": "npm:pi-loadout", "extensions": []}]:
                settings.pop("packages", None)
        settings_path.write_text(json.dumps(settings, indent=2))

    # ── Mapping/session persistence ──────────────────────────────

    def _load_mapping(self) -> dict[str, str]:
        if not self.mapping_file.exists():
            return {}
        try:
            return json.loads(self.mapping_file.read_text())
        except Exception:
            return {}

    def _save_mapping(self):
        try:
            self.mapping_file.write_text(json.dumps(self.mapping, indent=2))
        except Exception:
            pass

    # ── Pi RPC primitives ────────────────────────────────────────

    def _send(self, proc: subprocess.Popen, cmd: dict):
        if proc.stdin is None:
            raise ConnectionError("Pi stdin is closed")
        proc.stdin.write(json.dumps(cmd) + "\n")
        proc.stdin.flush()

    def _read_response(self, proc: subprocess.Popen) -> dict:
        if proc.stdout is None:
            raise ConnectionError("Pi stdout is closed")
        while True:
            line = proc.stdout.readline()
            if not line:
                raise ConnectionError("Pi process exited unexpectedly")
            try:
                event = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if event.get("type") == "response":
                return event

    def _spawn_rpc(self, no_session: bool = False) -> subprocess.Popen:
        args = [self.pi_binary, "--mode", "rpc"]
        if no_session:
            args.append("--no-session")
        else:
            args.extend(["--session-dir", str(self.session_dir)])
        return subprocess.Popen(
            args,
            cwd=str(self.workspace_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    # ── Models ───────────────────────────────────────────────────

    def load_models(self, force: bool = False) -> list[dict]:
        with self.state_lock:
            if self.model_list and not force:
                return self.model_list

        debug: dict[str, Any] = {
            "settings_path": str(PI_SETTINGS),
            "settings_exists": PI_SETTINGS.exists(),
            "pi_binary": self.pi_binary,
            "pi_binary_resolved": shutil.which(self.pi_binary) or "",
            "workspace_dir": str(self.workspace_dir),
            "exclude_loadout_extension": self.exclude_loadout_extension,
            "entries_count": 0,
            "models_count": 0,
            "fallback_used": False,
            "settings_error": "",
            "available_models_error": "",
        }

        try:
            settings = json.loads(PI_SETTINGS.read_text())
        except Exception as exc:
            settings = {}
            debug["settings_error"] = str(exc)

        default_provider = settings.get("defaultProvider", "")
        default_model = settings.get("defaultModel", "")
        enabled = settings.get("enabledModels", []) or []

        entries: list[str] = []
        if default_provider and default_model:
            entries.append(f"{default_provider}/{default_model}")
        for entry in enabled:
            if entry not in entries:
                entries.append(entry)
        debug["entries_count"] = len(entries)

        if not entries and self.fallback_model:
            entries = [self.fallback_model]
            debug["fallback_used"] = True
            print(
                f"[models] No Pi settings entries found at {PI_SETTINGS}; using fallback {self.fallback_model}",
                file=sys.stderr,
                flush=True,
            )

        name_map: dict[str, str] = {}
        proc: Optional[subprocess.Popen] = None
        try:
            proc = self._spawn_rpc(no_session=True)
            self._send(proc, {"type": "get_available_models"})
            resp = self._read_response(proc)
            for model in (resp.get("data") or {}).get("models", []):
                key = f"{model['provider']}/{model['id']}"
                name_map[key] = model.get("name", model["id"])
        except Exception as exc:
            debug["available_models_error"] = str(exc)
            print(f"[models] get_available_models failed: {exc}", file=sys.stderr, flush=True)
        finally:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        models: list[dict] = []
        model_map: dict[str, tuple[str, str]] = {}
        for entry in entries:
            parts = entry.split("/", 1)
            provider = parts[0]
            model_id = parts[1] if len(parts) > 1 else ""
            sid = safe_model_id(entry)
            model_map[sid] = (provider, model_id)
            models.append({"id": sid, "name": name_map.get(entry, entry)})
        debug["models_count"] = len(models)

        with self.state_lock:
            self.model_list = models
            self.model_map = model_map
            self.model_debug = debug
        return models

    def model_diagnostics(self) -> dict:
        with self.state_lock:
            debug = dict(self.model_debug)
        if not debug:
            self.load_models(force=True)
            with self.state_lock:
                debug = dict(self.model_debug)
        return debug

    # ── Process lifecycle ────────────────────────────────────────

    def _get_chat_lock(self, chat_id: str) -> threading.RLock:
        with self.state_lock:
            lock = self.chat_locks.get(chat_id)
            if lock is None:
                lock = threading.RLock()
                self.chat_locks[chat_id] = lock
            return lock

    def cleanup_stale(self):
        cutoff = time.monotonic() - self.idle_timeout_seconds
        with self.state_lock:
            stale = [cid for cid, ts in list(self.last_used.items()) if ts < cutoff]
        for chat_id in stale:
            with self._get_chat_lock(chat_id):
                with self.state_lock:
                    proc = self.procs.pop(chat_id, None)
                    self.last_used.pop(chat_id, None)
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

    def _spawn_for_chat(self, chat_id: str) -> subprocess.Popen:
        proc = self._spawn_rpc(no_session=False)

        existing = self.mapping.get(chat_id)
        if existing and Path(existing).exists():
            self._send(proc, {"type": "switch_session", "sessionPath": existing})
            resp = self._read_response(proc)
            # success is usually true; data.cancelled must also be checked.
            if not resp.get("success") or (resp.get("data") or {}).get("cancelled"):
                self.mapping.pop(chat_id, None)
                self._save_mapping()

        if chat_id not in self.mapping:
            self._send(proc, {"type": "get_state"})
            resp = self._read_response(proc)
            session_file = (resp.get("data") or {}).get("sessionFile")
            if session_file:
                self.mapping[chat_id] = session_file
                self._save_mapping()

        return proc

    def get_proc(self, chat_id: str) -> subprocess.Popen:
        with self.state_lock:
            proc = self.procs.get(chat_id)
        if proc is None or proc.poll() is not None:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            proc = self._spawn_for_chat(chat_id)
            with self.state_lock:
                self.procs[chat_id] = proc
        with self.state_lock:
            self.last_used[chat_id] = time.monotonic()
        return proc

    def sessions_diagnostics(self) -> dict:
        now = time.monotonic()
        with self.state_lock:
            chat_ids = sorted(set(self.mapping) | set(self.procs) | set(self.last_used))
            sessions = []
            for chat_id in chat_ids:
                proc = self.procs.get(chat_id)
                last = self.last_used.get(chat_id)
                sessions.append(
                    {
                        "chat_id": chat_id,
                        "pid": proc.pid if proc else None,
                        "alive": bool(proc and proc.poll() is None),
                        "age_seconds": round(now - last, 1) if last else None,
                        "session_file": self.mapping.get(chat_id),
                    }
                )
        return {
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "mapping_file": str(self.mapping_file),
            "workspace_dir": str(self.workspace_dir),
            "exclude_loadout_extension": self.exclude_loadout_extension,
            "sessions": sessions,
        }

    def _status_event(
        self,
        chat_id: str,
        description: str,
        done: bool = False,
        key: Optional[str] = None,
        min_interval: float = 0.75,
        force: bool = False,
    ) -> Optional[dict]:
        desc = strip_ansi(description)
        if not desc:
            return None

        # Drop common Pi TUI/status noise that is not useful in OpenWebUI.
        noise_markers = ("Proj today", "Total today", "t/s", "⏱", "⏳")
        if any(marker in desc for marker in noise_markers):
            return None

        now = time.monotonic()
        cache_key = f"{chat_id}:{key or desc}"
        with self.state_lock:
            previous = self.status_cache.get(cache_key)
            if previous and not force:
                prev_desc, prev_time = previous
                if prev_desc == desc and (now - prev_time) < max(min_interval, 2.0):
                    return None
                if (now - prev_time) < min_interval:
                    return None
            self.status_cache[cache_key] = (desc, now)
        return {"type": "status", "description": desc, "done": done}

    # ── Chat streaming ───────────────────────────────────────────

    def stream_chat(self, payload: dict) -> Generator[dict, None, None]:
        chat_id = str(payload.get("chat_id") or "default")
        if chat_id == "default":
            print("[sessions] Warning: request used fallback chat_id='default'", file=sys.stderr, flush=True)
        lock = self._get_chat_lock(chat_id)
        with lock:
            self.cleanup_stale()
            try:
                proc = self.get_proc(chat_id)
            except Exception as exc:
                yield {"type": "error", "message": f"Failed to start Pi: {exc}"}
                yield {"type": "done"}
                return

            # Ensure model map is available.
            if not self.model_map:
                self.load_models(force=False)

            model_id = payload.get("model_id") or ""
            if model_id in self.model_map:
                provider, mid = self.model_map[model_id]
                try:
                    self._send(proc, {"type": "set_model", "provider": provider, "modelId": mid})
                    self._read_response(proc)
                except Exception:
                    pass

            thinking_level = None
            reasoning_effort = payload.get("reasoning_effort")
            if reasoning_effort:
                thinking_level = THINKING_LEVEL_MAP.get(str(reasoning_effort))
            if not thinking_level:
                thinking_level = payload.get("default_thinking_level") or "high"
            if thinking_level:
                try:
                    self._send(proc, {"type": "set_thinking_level", "level": thinking_level})
                    self._read_response(proc)
                except Exception:
                    pass

            prompt_cmd: dict[str, Any] = {
                "type": "prompt",
                "message": payload.get("message") or "",
            }
            images = payload.get("images") or []
            if images:
                prompt_cmd["images"] = images

            try:
                self._send(proc, prompt_cmd)
                acceptance = self._read_response(proc)
            except Exception as exc:
                yield {"type": "error", "message": f"Error sending prompt to Pi: {exc}"}
                yield {"type": "done"}
                return

            if not acceptance.get("success"):
                yield {"type": "error", "message": f"Pi prompt rejected: {acceptance.get('error', 'unknown error')}"}
                yield {"type": "done"}
                return

            current_tool: Optional[str] = None
            emitted_tool_updates: set[str] = set()
            while True:
                try:
                    if proc.stdout is None:
                        raise ConnectionError("Pi stdout is closed")
                    raw = proc.stdout.readline()
                    if not raw:
                        raise ConnectionError("Pi process exited unexpectedly")
                    event = json.loads(raw.strip())
                except Exception as exc:
                    yield {"type": "error", "message": str(exc)}
                    yield {"type": "done"}
                    return

                etype = event.get("type")

                if etype == "message_update":
                    ae = event.get("assistantMessageEvent") or {}
                    ae_type = ae.get("type")
                    if ae_type == "text_delta":
                        delta = ae.get("delta") or ""
                        if delta:
                            with self.state_lock:
                                self.last_used[chat_id] = time.monotonic()
                            yield {"type": "text_delta", "delta": delta}
                    elif ae_type == "thinking_start":
                        yield {"type": "thinking_start"}
                    elif ae_type == "thinking_delta":
                        delta = ae.get("thinking") or ae.get("delta") or ""
                        if delta:
                            yield {"type": "thinking_delta", "delta": delta}
                    elif ae_type == "thinking_end":
                        yield {"type": "thinking_end"}

                elif etype == "tool_execution_start":
                    current_tool = event.get("toolName") or "tool"
                    args = event.get("args") or {}
                    desc = f"Using {current_tool}"
                    for key in ("url", "command", "path", "description"):
                        if key in args:
                            desc += f": {str(args[key])[:120]}"
                            break
                    status = self._status_event(
                        chat_id,
                        desc,
                        done=True,
                        key=f"tool-start:{current_tool}:{json.dumps(args, sort_keys=True, default=str)[:200]}",
                        min_interval=2.0,
                    )
                    if status:
                        yield status

                elif etype == "tool_execution_update":
                    # Suppress normal tool output previews in OpenWebUI statuses.
                    # The assistant answer will contain the useful result; the status
                    # area should identify the tool/command, not duplicate stdout like
                    # "bash: 56K ." or repeated fetch progress lines.
                    pass

                elif etype == "tool_execution_end":
                    tool_name = event.get("toolName") or current_tool or "tool"
                    is_error = bool(event.get("isError"))
                    # Generic OpenWebUI status events do not update prior lines like
                    # native tool cards do. Suppress successful completions so a tool
                    # appears once as "Using tool" instead of as start + checkmark.
                    if is_error:
                        status = self._status_event(
                            chat_id,
                            f"✗ {tool_name}",
                            done=True,
                            key=f"tool-end:{tool_name}:{is_error}",
                            min_interval=2.0,
                            force=True,
                        )
                        if status:
                            yield status
                    current_tool = None

                elif etype == "extension_ui_request":
                    yield from self._handle_extension_ui(proc, event, payload.get("guardrail_default") or "deny")

                elif etype == "agent_end":
                    yield {"type": "done"}
                    return

    def _handle_extension_ui(self, proc: subprocess.Popen, event: dict, guardrail_default: str) -> Generator[dict, None, None]:
        method = event.get("method") or ""
        req_id = event.get("id") or ""
        title = event.get("title") or "Pi requested confirmation"
        message = event.get("message") or ""

        noisy_fire_and_forget = {"setStatus", "setWidget", "setTitle", "set_editor_text"}
        dialogs = {"confirm", "select", "input", "editor"}

        if method in noisy_fire_and_forget:
            # These are Pi TUI/internal display updates. Surfacing them in OpenWebUI
            # creates noisy lines like "setWidget" plus ANSI-colored token metrics.
            return

        if method == "notify":
            desc = event.get("message") or event.get("statusText") or event.get("title") or ""
            status = self._status_event("extension", desc, done=True, key=f"notify:{desc[:80]}", min_interval=2.0)
            if status:
                yield status
            return

        if method in dialogs:
            allow = guardrail_default == "allow"
            action = "allowed" if allow else "denied"
            if method == "confirm":
                response = {"type": "extension_ui_response", "id": req_id, "confirmed": allow}
            elif allow:
                options = event.get("options") or []
                value = options[0] if options else ""
                response = {"type": "extension_ui_response", "id": req_id, "value": value}
            else:
                response = {"type": "extension_ui_response", "id": req_id, "cancelled": True}

            try:
                self._send(proc, response)
            except Exception as exc:
                yield {"type": "status", "description": f"Failed to answer Pi {method} request: {exc}", "done": True}
                return

            detail = f": {strip_ansi(message)}" if message else ""
            status = self._status_event(
                "extension",
                (
                    f"Pi requested {method}: {strip_ansi(title)}{detail}. "
                    f"Auto-{action} (guardrail_default = {guardrail_default})."
                ),
                done=True,
                key=f"dialog:{req_id or method}",
                force=True,
            )
            if status:
                yield status

    def shutdown(self):
        with self.state_lock:
            procs = list(self.procs.values())
            self.procs.clear()
        for proc in procs:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


STATE: Optional[PiBridgeState] = None


class PiBridgeHandler(BaseHTTPRequestHandler):
    server_version = "PiBridge/0.1"

    def log_message(self, fmt: str, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    @property
    def state(self) -> PiBridgeState:
        if STATE is None:
            raise RuntimeError("Bridge state is not initialized")
        return STATE

    def _auth_ok(self) -> bool:
        token = self.state.token
        if not token:
            return True
        return self.headers.get("X-Pi-Bridge-Token", "") == token

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_unauthorized(self):
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})

    def do_GET(self):
        if not self._auth_ok():
            return self._send_unauthorized()

        path = urlparse(self.path).path

        if path == "/health":
            return self._send_json(HTTPStatus.OK, {"ok": True})

        if path == "/models":
            try:
                models = self.state.load_models(force=True)
                return self._send_json(HTTPStatus.OK, {"models": models})
            except Exception as exc:
                return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if path == "/debug/models":
            try:
                self.state.load_models(force=True)
                return self._send_json(HTTPStatus.OK, self.state.model_diagnostics())
            except Exception as exc:
                return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if path == "/sessions":
            return self._send_json(HTTPStatus.OK, self.state.sessions_diagnostics())

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if not self._auth_ok():
            return self._send_unauthorized()

        if self.path != "/chat/stream":
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid json: {exc}"})

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            for event in self.state.stream_chat(payload):
                self.wfile.write(sse_line(event))
                self.wfile.flush()
                if event.get("type") == "done":
                    break
        except BrokenPipeError:
            return
        except Exception as exc:
            try:
                self.wfile.write(sse_line({"type": "error", "message": str(exc)}))
                self.wfile.write(sse_line({"type": "done"}))
                self.wfile.flush()
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pi bridge server for OpenWebUI Functions")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--pi-binary", default=os.environ.get("PI_BINARY", "pi"))
    parser.add_argument("--session-dir", default=os.environ.get("PI_BRIDGE_SESSION_DIR", str(Path.home() / ".pi" / "owui-sessions")))
    parser.add_argument("--workspace-dir", default=os.environ.get("PI_BRIDGE_WORKSPACE_DIR", str(Path.home() / ".pi" / "owui-bridge-workspace")))
    parser.add_argument("--idle-timeout", type=int, default=int(os.environ.get("PI_BRIDGE_IDLE_TIMEOUT", "300")))
    parser.add_argument("--fallback-model", default=os.environ.get("PI_BRIDGE_FALLBACK_MODEL", "openai-codex/gpt-5.2"))
    parser.add_argument("--token", default=os.environ.get("PI_BRIDGE_TOKEN", ""))
    parser.add_argument(
        "--include-loadout-extension",
        action="store_true",
        default=os.environ.get("PI_BRIDGE_INCLUDE_LOADOUT", "").lower() in {"1", "true", "yes"},
        help="Do not exclude the pi-loadout extension for bridge-launched Pi processes.",
    )
    return parser.parse_args()


def main():
    global STATE
    args = parse_args()
    STATE = PiBridgeState(
        pi_binary=args.pi_binary,
        session_dir=args.session_dir,
        idle_timeout_seconds=args.idle_timeout,
        fallback_model=args.fallback_model,
        token=args.token,
        workspace_dir=args.workspace_dir,
        exclude_loadout_extension=not args.include_loadout_extension,
    )
    server = ThreadingHTTPServer((args.host, args.port), PiBridgeHandler)
    print(f"Pi bridge listening on http://{args.host}:{args.port}", flush=True)
    print(f"Pi bridge workspace: {STATE.workspace_dir}", flush=True)
    if STATE.exclude_loadout_extension:
        print("Pi bridge excludes pi-loadout extension via project settings override", flush=True)
    if args.token:
        print("Pi bridge token auth is enabled", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Pi bridge...", flush=True)
    finally:
        server.server_close()
        STATE.shutdown()


if __name__ == "__main__":
    main()
