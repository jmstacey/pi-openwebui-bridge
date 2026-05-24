#!/usr/bin/env python3
"""
Pi Bridge Server

A small macOS host bridge for OpenWebUI Functions running in Docker.
It owns pi-life-web --mode rpc subprocesses and exposes a narrow HTTP/SSE API.

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
import calendar
import ssl
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Generator, Optional
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


class OpenWebUIHTTPError(RuntimeError):
    def __init__(self, code: int, url: str, body: str):
        super().__init__(f"HTTP {code} from {url}: {body}")
        self.code = code
        self.owui_url = url
        self.body = body

_UNVERIFIED_SSL = ssl.create_default_context()
_UNVERIFIED_SSL.check_hostname = False
_UNVERIFIED_SSL.verify_mode = ssl.CERT_NONE

DEFAULT_OWUI_SESSION_DIR = Path.home() / ".pi" / "agent" / "sessions" / "owui-sessions"


def _ts_to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = int(value)
        # Pi RPC timestamps are milliseconds; convert to seconds.
        return ts // 1000 if ts > 9_999_999_999 else ts
    if isinstance(value, str):
        try:
            import datetime
            dt = datetime.datetime.strptime(value.rstrip("Z").split(".")[0], "%Y-%m-%dT%H:%M:%S")
            return calendar.timegm(dt.timetuple())
        except Exception:
            pass
    return None


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


def content_text(content: Any) -> str:
    """Plain-text extraction used for session titles and search."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"text", "input_text"}:
                parts.append(str(part.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return str(content or "")


# Tools whose output is best shown in a language-fenced code block.
_SHELL_TOOLS = {"bash", "sh", "zsh"}
_CODE_TOOLS = {"bash", "sh", "zsh", "python", "node", "ruby", "perl"}
_FILE_TOOLS = {"read", "write", "edit", "target_edit", "quick_edit"}
_SEARCH_TOOLS = {"grep", "find", "ls"}
_FETCH_TOOLS = {"web_fetch", "batch_web_fetch"}
_QUIET_SUCCESS_TOOLS = {
    "tool_search", "ralph_done", "ralph_start",
    "hindsight_sync_retain", "hindsight_retain",
}


def _tool_icon(tool_name: str) -> str:
    if tool_name in _SHELL_TOOLS:
        return "💻"
    if tool_name in _FILE_TOOLS:
        return "📄"
    if tool_name in _SEARCH_TOOLS:
        return "🔍"
    if tool_name in _FETCH_TOOLS:
        return "🌐"
    if tool_name in {"hindsight_recall", "hindsight_reflect"}:
        return "🧠"
    if tool_name.startswith("browser"):
        return "🌐"
    return "🔧"


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
    return str(content or "").strip()


def _render_tool_call(name: str, args: dict, result: Optional[str], is_error: bool) -> str:
    """Render a paired tool call + result as a pure-markdown block."""
    icon = _tool_icon(name)
    status = " ❌" if is_error else (" ✅" if result is not None else "")
    lines: list[str] = []

    # Header line
    if name in _QUIET_SUCCESS_TOOLS and result is not None and not is_error:
        # Single compact line for noisy housekeeping tools
        short = result[:120].replace("\n", " ")
        return f"{icon} **{name}**{status} — `{short}`"

    lines.append(f"#### {icon} {name}{status}")

    # Key args shown as inline code pairs; skip implementation-detail args.
    _INLINE_ARGS = ("command", "path", "url", "pattern", "query", "names", "content", "tag")
    _NOISE_ARGS = frozenset(("timeout", "offset", "limit", "context", "ignoreCase",
                              "literal", "glob", "includeReplies", "removeImages"))
    if args and name not in _QUIET_SUCCESS_TOOLS:
        display = {k: v for k, v in args.items() if k not in _NOISE_ARGS}
        inline = {k: v for k, v in display.items() if k in _INLINE_ARGS}
        complex_rest = {k: v for k, v in display.items() if k not in _INLINE_ARGS}
        for k, v in inline.items():
            if isinstance(v, list):
                lines.append(f"**{k}:** " + ", ".join(f"`{x}`" for x in v))
            elif isinstance(v, (str, int, float, bool)):
                lines.append(f"**{k}:** `{v}`")
        if complex_rest:
            lines.append(f"```json\n{json.dumps(complex_rest, indent=2, ensure_ascii=False)}\n```")

    # Result
    if result is None:
        pass  # call without paired result — show args only
    elif not result:
        lines.append("*No output.*")
    elif is_error:
        lines.append("**Error:**")
        lines.append(f"```\n{result}\n```")
    elif name in _SHELL_TOOLS or name in _SEARCH_TOOLS:
        lines.append(f"```\n{result}\n```")
    elif name in _FILE_TOOLS:
        if len(result) < 120 and "\n" not in result:
            lines.append(f"*{result}*")
        else:
            lines.append(f"```\n{result}\n```")
    elif name in _FETCH_TOOLS:
        if len(result) > 2000:
            result = result[:2000] + "\n\n*… truncated*"
        lines.append(result)
    else:
        lines.append(f"```\n{result}\n```")

    return "\n".join(lines)


def content_to_owui_markdown(content: Any) -> str:
    """Render Pi assistant message content (content block list) as OpenWebUI markdown.

    Handles: thinking, text, toolCall blocks.
    toolResult blocks are paired in _project_chat; this covers the fallback case
    where a standalone toolCall block appears without a matching result.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    thinking_parts: list[str] = []
    body_parts: list[str] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "thinking":
            text = (block.get("thinking") or "").strip()
            if text:
                thinking_parts.append(text)

        elif btype in ("text", "input_text"):
            text = (block.get("text") or "").strip()
            if text:
                body_parts.append(text)

        elif btype == "toolCall":
            name = block.get("name") or "tool"
            args = block.get("arguments") or {}
            body_parts.append(_render_tool_call(name, args, result=None, is_error=False))

        elif btype == "image":
            body_parts.append("*\ud83d\uddbc\ufe0f Image attachment (not shown in projected view)*")

    out = "\n\n".join(body_parts)
    return out.strip()


class PiBridgeState:
    def __init__(
        self,
        pi_binary: str = "pi-life-web",
        session_dir: str = str(DEFAULT_OWUI_SESSION_DIR),
        idle_timeout_seconds: int = 300,
        fallback_model: str = "openai-codex/gpt-5.2",
        token: str = "",
        workspace_dir: str = str(Path.home() / ".pi" / "owui-bridge-workspace"),
        exclude_loadout_extension: bool = True,
        indexed_session_dirs: Optional[list[str]] = None,
        openwebui_url: str = "",
        openwebui_api_key: str = "",
        projection_sync_interval: int = 30,
        projection_sync_batch_size: int = 50,
        projection_sync_request_delay: float = 0.15,
        projection_sync_recent_days: int = 0,
        projection_max_messages: int = 100,
        projection_max_message_chars: int = 4000,
        owui_function_id: str = "pi_agent_bridge_function",
    ):
        self.pi_binary = pi_binary
        self.session_dir = Path(session_dir).expanduser()
        self.idle_timeout_seconds = idle_timeout_seconds
        self.fallback_model = fallback_model
        self.token = token
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.exclude_loadout_extension = exclude_loadout_extension
        default_index_dirs = [self.session_dir, Path.home() / ".pi" / "agent" / "sessions"]
        configured_dirs = indexed_session_dirs or [str(p) for p in default_index_dirs]
        self.indexed_session_dirs = [Path(p).expanduser() for p in configured_dirs]
        self.openwebui_url = openwebui_url.rstrip("/")
        self.openwebui_api_key = openwebui_api_key
        self.projection_sync_interval = max(5, projection_sync_interval)
        self.projection_sync_batch_size = max(1, projection_sync_batch_size)
        self.projection_sync_request_delay = max(0.0, projection_sync_request_delay)
        self.projection_sync_recent_days = projection_sync_recent_days
        self.projection_max_messages = max(1, projection_max_messages)
        self.projection_max_message_chars = max(100, projection_max_message_chars)
        self.owui_function_id = owui_function_id
        self.projection_file = self.session_dir / "openwebui_projection.json"
        self.projection: dict[str, dict[str, Any]] = self._load_projection()
        self._sync_stop = threading.Event()
        self._sync_thread: Optional[threading.Thread] = None
        self._session_signature: dict[str, tuple[float, int]] = {}

        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._write_workspace_settings()
        self.mapping_file = self.session_dir / "chat_sessions.json"
        self.mapping: dict[str, str] = self._load_mapping()

        self.procs: dict[str, subprocess.Popen] = {}
        self.last_used: dict[str, float] = {}
        self.chat_locks: dict[str, threading.RLock] = {}
        self.session_writers: dict[str, str] = {}
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
            data = json.loads(self.mapping_file.read_text())
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_mapping(self):
        try:
            self.mapping_file.write_text(json.dumps(self.mapping, indent=2))
        except Exception:
            pass

    def _load_projection(self) -> dict[str, dict[str, Any]]:
        if not self.projection_file.exists():
            return {}
        try:
            data = json.loads(self.projection_file.read_text())
            return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_projection(self):
        try:
            self.projection_file.write_text(json.dumps(self.projection, indent=2))
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

    def _spawn_rpc(
        self,
        no_session: bool = False,
        session_path: Optional[str] = None,
        fork_path: Optional[str] = None,
    ) -> subprocess.Popen:
        args = [self.pi_binary, "--mode", "rpc"]
        if no_session:
            args.append("--no-session")
        elif fork_path:
            args.extend(["--fork", fork_path])
        elif session_path:
            args.extend(["--session", session_path])
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

    # ── Pi session index / transcript projection ─────────────────

    def _iter_session_files(self) -> list[Path]:
        files: dict[str, Path] = {}
        for root in self.indexed_session_dirs:
            if not root.exists():
                continue
            for path in root.rglob("*.jsonl"):
                if path.is_file():
                    files[str(path.resolve())] = path
        return list(files.values())

    def _session_id_from_path(self, path: str) -> Optional[str]:
        try:
            stem = Path(path).stem
            parts = stem.rsplit("_", 1)
            if len(parts) == 2:
                candidate = parts[1]
                # Validate it looks like a UUID (contains hyphens)
                if "-" in candidate:
                    return candidate
        except Exception:
            pass
        return None

    def _read_session_record(self, path: Path, include_messages: bool = False) -> Optional[dict]:
        try:
            stat = path.stat()
        except Exception:
            return None

        header: dict[str, Any] = {}
        messages: list[dict[str, Any]] = []
        title = ""
        message_count = 0
        model_provider = ""
        model_id_val = ""
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if event.get("type") == "session" and not header:
                        header = event
                        continue
                    if event.get("type") == "model_change" and not model_provider:
                        model_provider = event.get("provider") or ""
                        model_id_val = event.get("modelId") or ""
                        continue
                    if event.get("type") != "message":
                        continue
                    msg = event.get("message") or {}
                    role = msg.get("role") or "unknown"
                    raw_content = msg.get("content")
                    plain = content_text(raw_content)
                    if role not in {"toolResult"}:
                        message_count += 1
                    if not title and role == "user" and plain:
                        first_line = plain.splitlines()[0].strip()
                        # Skip system/skill injection messages that start with XML-like tags.
                        if not first_line.startswith("<"):
                            title = first_line[:80]
                    if include_messages:
                        entry: dict[str, Any] = {
                            "id": event.get("id"),
                            "parentId": event.get("parentId"),
                            "timestamp": event.get("timestamp"),
                            "role": role,
                            "text": plain,
                            "rendered": content_to_owui_markdown(raw_content),
                            "raw_content": raw_content if isinstance(raw_content, list) else [],
                        }
                        if role == "toolResult":
                            entry["toolCallId"] = msg.get("toolCallId") or ""
                            entry["toolName"] = msg.get("toolName") or ""
                            entry["isError"] = bool(msg.get("isError"))
                            entry["content"] = raw_content
                        else:
                            # Capture model from first assistant message if model_change missed
                            if role == "assistant" and not model_provider:
                                model_provider = msg.get("provider") or ""
                                model_id_val = msg.get("model") or ""
                        messages.append(entry)
        except Exception:
            return None

        session_id = header.get("id") or path.stem.rsplit("_", 1)[-1]
        bridge_model_id = safe_model_id(f"{model_provider}/{model_id_val}") if model_provider and model_id_val else ""
        record = {
            "id": session_id,
            "sessionId": session_id,
            "sessionFile": str(path),
            "cwd": header.get("cwd"),
            "createdAt": header.get("timestamp"),
            "updatedAt": stat.st_mtime,
            "updatedAtIso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            "title": title or path.stem,
            "messageCount": message_count,
            "source": "pi",
            "bridgeModelId": bridge_model_id,
        }
        if include_messages:
            record["messages"] = messages
        return record

    def list_pi_sessions(self, query: str = "") -> list[dict]:
        q = query.lower().strip()
        sessions = []
        for path in self._iter_session_files():
            record = self._read_session_record(path, include_messages=False)
            if not record:
                continue
            if q:
                haystack = " ".join(
                    str(record.get(key) or "")
                    for key in ("id", "sessionFile", "cwd", "title")
                ).lower()
                if q not in haystack:
                    continue
            sessions.append(record)
        sessions.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
        return sessions

    def _resolve_session(self, session_ref: str) -> Optional[dict]:
        ref = unquote(str(session_ref or "")).strip()
        if not ref:
            return None
        candidate = Path(ref).expanduser()
        if candidate.exists() and candidate.is_file():
            return self._read_session_record(candidate, include_messages=False)
        matches = []
        for record in self.list_pi_sessions():
            sid = str(record.get("sessionId") or "")
            path = str(record.get("sessionFile") or "")
            if sid == ref or sid.startswith(ref) or path == ref:
                matches.append(record)
        if len(matches) == 1:
            return matches[0]
        return None

    def get_pi_session(self, session_ref: str, include_messages: bool = False) -> Optional[dict]:
        record = self._resolve_session(session_ref)
        if not record:
            return None
        if include_messages:
            return self._read_session_record(Path(record["sessionFile"]), include_messages=True)
        return record

    def fork_pi_session(self, session_ref: str) -> dict:
        record = self._resolve_session(session_ref)
        if not record:
            raise FileNotFoundError("unknown Pi session")
        proc = self._spawn_rpc(fork_path=record["sessionFile"])
        try:
            self._send(proc, {"type": "get_state"})
            resp = self._read_response(proc)
            data = resp.get("data") or {}
            return {
                "sourceSessionId": record.get("sessionId"),
                "sourceSessionFile": record.get("sessionFile"),
                "sessionId": data.get("sessionId"),
                "sessionFile": data.get("sessionFile"),
                "forked": True,
            }
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ── OpenWebUI projection sync ─────────────────────────────────

    def _openwebui_enabled(self) -> bool:
        return bool(self.openwebui_url and self.openwebui_api_key)

    def _openwebui_request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        if not self._openwebui_enabled():
            raise RuntimeError("OpenWebUI sync is not configured")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        url = self.openwebui_url + path
        req = Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.openwebui_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        ssl_ctx = _UNVERIFIED_SSL if self.openwebui_url.startswith("https") else None
        try:
            with urlopen(req, timeout=30, context=ssl_ctx) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
                if not raw:
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {"raw": raw}
        except HTTPError as exc:
            body_preview = ""
            try:
                body_preview = exc.read(500).decode("utf-8", errors="ignore")
            except Exception:
                pass
            raise OpenWebUIHTTPError(exc.code, url, body_preview[:200]) from exc

    def _probe_openwebui(self) -> bool:
        try:
            result = self._openwebui_request("GET", "/api/v1/auths/")
            return True
        except Exception as exc:
            print(f"[sync] OpenWebUI connectivity probe failed: {exc}", file=sys.stderr, flush=True)
            print(f"  URL: {self.openwebui_url}", file=sys.stderr, flush=True)
            print(f"  Hint: check OPENWEBUI_URL (http vs https, port, hostname)", file=sys.stderr, flush=True)
            return False

    def _status_desc(self, name: str, args: dict) -> str:
        desc = f"Using {name}"
        for key in ("url", "command", "path", "description", "query"):
            if key in args:
                desc += f": {str(args[key])[:120]}"
                break
        return desc

    def _render_assistant_group(self, msgs: list[dict], result_by_call_id: dict) -> dict:
        """Combine all consecutive assistant rounds into a single projected message
        matching OpenWebUI's native streaming-produced format exactly."""
        first_ts = None
        rep_id = str(msgs[0].get("id") or uuid.uuid4()) if msgs else str(uuid.uuid4())

        content_parts: list[str] = []
        output_blocks: list[dict] = []
        status_history: list[dict] = []
        counter = 0

        for msg in msgs:
            if first_ts is None:
                first_ts = _ts_to_int(msg.get("timestamp"))

            round_thinking: list[str] = []
            round_text: list[str] = []

            for block in (msg.get("raw_content") or []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "thinking":
                    t = (block.get("thinking") or "").strip()
                    if t:
                        round_thinking.append(t)

                elif btype in ("text", "input_text"):
                    t = (block.get("text") or "").strip()
                    if t:
                        round_text.append(t)

                elif btype == "toolCall":
                    call_id = block.get("id") or ""
                    name = block.get("name") or "tool"
                    args = block.get("arguments") or {}
                    res_msg = result_by_call_id.get(call_id)
                    is_error = bool(res_msg.get("isError")) if res_msg else False
                    status_history.append({"description": self._status_desc(name, args), "done": True})
                    if is_error:
                        status_history.append({"description": f"✗ {name}", "done": True})

                elif btype == "image":
                    pass  # images not representable as text in projection

            # Emit thinking block for this round
            if round_thinking:
                text = "\n\n".join(round_thinking)
                quoted = "\n".join(f"&gt; {line}" for line in text.splitlines())
                content_parts.append(
                    f'<details type="reasoning" done="true" duration="0">\n'
                    f'<summary>Thought for 0 seconds</summary>\n'
                    f'{quoted}\n'
                    f'</details>'
                )
                counter += 1
                output_blocks.append({
                    "type": "reasoning",
                    "id": f"r_pi_{rep_id}_{counter}",
                    "status": "completed",
                    "start_tag": "<think>",
                    "end_tag": "</think>",
                    "attributes": {},
                    "content": [{"type": "output_text", "text": text}],
                    "summary": None,
                    "duration": 0,
                })

            # Emit text block for this round
            if round_text:
                text = "\n\n".join(round_text)
                content_parts.append(text)
                counter += 1
                output_blocks.append({
                    "type": "message",
                    "id": f"msg_pi_{rep_id}_{counter}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })

        rendered = "\n".join(content_parts).strip()
        if not rendered:
            rendered = msgs[-1].get("text") or "" if msgs else ""

        entry: dict[str, Any] = {
            "id": rep_id,
            "childrenIds": [],
            "role": "assistant",
            "content": rendered,
            "done": True,
            "timestamp": first_ts,
        }
        if output_blocks:
            entry["output"] = output_blocks
        if status_history:
            entry["statusHistory"] = status_history
        return entry

    def _project_chat(self, record: dict) -> dict:
        raw = record.get("messages") or []

        # Index toolResult messages by toolCallId so they can be paired with toolCall blocks.
        result_by_call_id: dict[str, dict] = {}
        for msg in raw:
            if msg.get("role") == "toolResult":
                cid = msg.get("toolCallId") or ""
                if cid:
                    result_by_call_id[cid] = msg

        # Group messages into alternating user / [assistant...] runs.
        # All consecutive assistant rounds between two user turns are collapsed into
        # one projected message, matching how the bridge streams a single response.
        groups: list[tuple[str, list[dict]]] = []
        assistant_buf: list[dict] = []
        for msg in raw:
            role = msg.get("role") or "unknown"
            if role == "toolResult":
                continue
            if role == "user":
                if assistant_buf:
                    groups.append(("assistant", assistant_buf))
                    assistant_buf = []
                groups.append(("user", [msg]))
            else:
                assistant_buf.append(msg)
        if assistant_buf:
            groups.append(("assistant", assistant_buf))

        # Build bridge model ID for avatar / model name display.
        bridge_model_id = record.get("bridgeModelId") or ""
        owui_model_id = f"{self.owui_function_id}.{bridge_model_id}" if bridge_model_id else "pi"

        messages: list[dict[str, Any]] = []
        minimal_messages: list[dict[str, Any]] = []
        prev_mid: Optional[str] = None

        for gtype, group_msgs in groups:
            if gtype == "user":
                msg = group_msgs[0]
                mid = str(msg.get("id") or uuid.uuid4())
                text = msg.get("text") or ""
                entry: dict[str, Any] = {
                    "id": mid,
                    "parentId": prev_mid,
                    "childrenIds": [],
                    "role": "user",
                    "content": text,
                    "timestamp": _ts_to_int(msg.get("timestamp")),
                    "models": [owui_model_id],
                }
                messages.append(entry)
                minimal_messages.append({"role": "user", "content": text})
                prev_mid = mid
            else:
                entry = self._render_assistant_group(group_msgs, result_by_call_id)
                entry["parentId"] = prev_mid
                entry["model"] = owui_model_id
                messages.append(entry)
                prev_mid = entry["id"]

        by_id = {m["id"]: m for m in messages}
        for msg in messages:
            pid = msg.get("parentId")
            if pid in by_id:
                by_id[pid].setdefault("childrenIds", []).append(msg["id"])
        current_id = messages[-1]["id"] if messages else None
        timestamp = int(record.get("updatedAt") or time.time())
        return {
            "title": record.get("title") or "Pi session",
            "models": [owui_model_id],
            "messages": minimal_messages,
            "history": {"messages": by_id, "currentId": current_id},
            "params": {},
            "files": [],
            "tags": ["pi"],
            "timestamp": timestamp,
            "metadata": {
                "source": "pi",
                "pi_session_id": record.get("sessionId"),
                "pi_session_file": record.get("sessionFile"),
                "pi_cwd": record.get("cwd"),
            },
        }

    def _extract_openwebui_chat_id(self, response: dict, fallback: Optional[str] = None) -> Optional[str]:
        if not isinstance(response, dict):
            return fallback
        for candidate in (response, response.get("chat") or {}, response.get("data") or {}):
            if isinstance(candidate, dict) and candidate.get("id"):
                return str(candidate["id"])
        return fallback

    def _forget_projection(self, session_id: str, chat_id: Optional[str] = None) -> None:
        entry = self.projection.pop(str(session_id), None) or {}
        cid = chat_id or entry.get("chat_id")
        if cid:
            self.mapping.pop(str(cid), None)

    def _delete_openwebui_chat(self, chat_id: str) -> bool:
        try:
            self._openwebui_request("DELETE", f"/api/v1/chats/{chat_id}")
            return True
        except OpenWebUIHTTPError as exc:
            if exc.code in {401, 403, 404, 410}:
                return False
            raise

    def _delete_pi_session_file(self, session_file: Optional[str]) -> bool:
        if not session_file:
            return False
        try:
            path = Path(session_file).expanduser()
            if path.exists() and path.is_file():
                path.unlink()
                return True
        except Exception:
            raise
        return False

    def _reconcile_deleted_pi_sessions(self, changed_paths: Optional[set] = None) -> dict[str, int]:
        """If a mapped Pi JSONL is gone, delete its projected OpenWebUI chat."""
        stats = {"deleted_remote": 0, "forgotten": 0}
        changed = {str(p) for p in changed_paths} if changed_paths is not None else None
        for session_id, entry in list(self.projection.items()):
            session_file = entry.get("session_file")
            if not session_file:
                continue
            if changed is not None and str(session_file) not in changed:
                continue
            if Path(session_file).exists():
                continue
            chat_id = entry.get("chat_id")
            if chat_id:
                if self._delete_openwebui_chat(str(chat_id)):
                    stats["deleted_remote"] += 1
            self._forget_projection(str(session_id), str(chat_id) if chat_id else None)
            stats["forgotten"] += 1
        return stats

    def _reconcile_deleted_openwebui_chats(self) -> dict[str, int]:
        """Check a batch of mapped OpenWebUI chats; if missing, delete local Pi session."""
        stats = {"deletion_checked": 0, "deleted_local": 0, "forgotten": 0}
        now = time.time()
        entries = sorted(
            list(self.projection.items()),
            key=lambda item: float((item[1] or {}).get("deletion_checked_at") or 0),
        )[: self.projection_sync_batch_size]
        for session_id, entry in entries:
            chat_id = entry.get("chat_id")
            if not chat_id:
                continue
            try:
                self._openwebui_request("GET", f"/api/v1/chats/{chat_id}")
                entry["deletion_checked_at"] = now
                stats["deletion_checked"] += 1
            except OpenWebUIHTTPError as exc:
                if exc.code not in {401, 403, 404, 410}:
                    raise
                if self._delete_pi_session_file(entry.get("session_file")):
                    stats["deleted_local"] += 1
                self._forget_projection(str(session_id), str(chat_id))
                stats["forgotten"] += 1
        return stats

    def reset_projection_fingerprints(self) -> dict:
        count = 0
        with self.state_lock:
            for session_id, entry in self.projection.items():
                if "fingerprint" in entry:
                    del entry["fingerprint"]
                    count += 1
        self._save_projection()
        self._session_signature = {}
        return {"reset": count}

    def sync_openwebui_sessions(self, force: bool = False, changed_paths: Optional[set] = None) -> dict:
        if not self._openwebui_enabled():
            return {"enabled": False, "synced": 0, "created": 0, "updated": 0, "errors": []}
        result: dict[str, Any] = {
            "enabled": True,
            "synced": 0,
            "created": 0,
            "updated": 0,
            "deleted_local": 0,
            "deleted_remote": 0,
            "deletion_checked": 0,
            "errors": [],
        }

        try:
            owui_deletes = self._reconcile_deleted_openwebui_chats()
            result["deleted_local"] += owui_deletes.get("deleted_local", 0)
            result["deletion_checked"] += owui_deletes.get("deletion_checked", 0)
            local_deletes = self._reconcile_deleted_pi_sessions(changed_paths=changed_paths)
            result["deleted_remote"] += local_deletes.get("deleted_remote", 0)
        except Exception as exc:
            result["errors"].append({"phase": "deletion_reconcile", "error": str(exc)})

        cutoff: Optional[float] = None
        if self.projection_sync_recent_days > 0:
            cutoff = time.time() - self.projection_sync_recent_days * 86400

        candidates = []
        for summary in self.list_pi_sessions():
            if not summary.get("sessionId"):
                continue
            if changed_paths is not None and summary.get("sessionFile") not in changed_paths:
                continue
            if cutoff and (summary.get("updatedAt") or 0) < cutoff:
                continue
            previous = self.projection.get(str(summary["sessionId"])) or {}
            fingerprint = f"{summary.get('sessionFile')}:{summary.get('updatedAt')}:{summary.get('messageCount')}"
            if not force and previous.get("fingerprint") == fingerprint:
                continue
            candidates.append((summary, previous, fingerprint))

        # Respect batch limit to avoid overwhelming OpenWebUI.
        candidates = candidates[: self.projection_sync_batch_size]
        max_consecutive_errors = 5
        consecutive_errors = 0

        for summary, previous, fingerprint in candidates:
            if self._sync_stop.is_set():
                break
            session_id = str(summary["sessionId"])
            detail = self.get_pi_session(session_id, include_messages=True)
            if not detail:
                continue
            chat = self._project_chat(detail)
            payload_bytes = len(json.dumps({"chat": chat}).encode("utf-8"))
            chat_id = previous.get("chat_id")
            try:
                if chat_id:
                    payload = {"chat": chat}
                    try:
                        response = self._openwebui_request("POST", f"/api/v1/chats/{chat_id}", payload)
                        result["updated"] += 1
                    except OpenWebUIHTTPError as exc:
                        if exc.code not in {401, 403, 404, 410}:
                            raise
                        # Mapped OpenWebUI chat was deleted or is inaccessible.
                        # Deletion policy: OpenWebUI deletion deletes the backing Pi session.
                        if self._delete_pi_session_file(detail.get("sessionFile")):
                            result["deleted_local"] += 1
                        self._forget_projection(session_id, str(chat_id))
                        continue
                else:
                    response = self._openwebui_request("POST", "/api/v1/chats/new", {"chat": chat})
                    chat_id = self._extract_openwebui_chat_id(response)
                    result["created"] += 1
                chat_id = self._extract_openwebui_chat_id(response, chat_id)
                if chat_id:
                    self.projection[session_id] = {
                        "chat_id": chat_id,
                        "session_file": detail.get("sessionFile"),
                        "fingerprint": fingerprint,
                        "synced_at": time.time(),
                    }
                    self.mapping[str(chat_id)] = str(detail.get("sessionFile"))
                    result["synced"] += 1
                consecutive_errors = 0
            except Exception as exc:
                result["errors"].append({"sessionId": session_id, "error": str(exc), "payload_kb": round(payload_bytes / 1024, 1)})
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    result["errors"].append({"note": f"Stopping after {max_consecutive_errors} consecutive errors"})
                    break

            if self.projection_sync_request_delay > 0:
                time.sleep(self.projection_sync_request_delay)

        self._save_projection()
        self._save_mapping()
        return result

    def _scan_session_signature(self) -> dict[str, tuple[float, int]]:
        # Stat-only scan including subdirectories (TUI sessions live in per-cwd subdirs).
        signature: dict[str, tuple[float, int]] = {}
        for root in self.indexed_session_dirs:
            if not root.exists():
                continue
            for dirpath, _dirs, files in os.walk(root):
                for fname in files:
                    if not fname.endswith(".jsonl"):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    try:
                        st = os.stat(fpath)
                        signature[fpath] = (st.st_mtime, st.st_size)
                    except Exception:
                        pass
        return signature

    def start_projection_sync(self):
        if not self._openwebui_enabled():
            print("OpenWebUI projection sync disabled; set OPENWEBUI_URL and OPENWEBUI_API_KEY", file=sys.stderr, flush=True)
            return
        self._session_signature = self._scan_session_signature()
        threading.Thread(target=self._initial_sync_worker, name="pi-openwebui-sync-init", daemon=True).start()
        self._sync_thread = threading.Thread(target=self._projection_sync_loop, name="pi-openwebui-sync", daemon=True)
        self._sync_thread.start()

    def _initial_sync_worker(self):
        print(f"OpenWebUI projection initial sync starting (url={self.openwebui_url})...", file=sys.stderr, flush=True)
        if not self._probe_openwebui():
            print("[sync] Skipping initial sync due to connectivity failure.", file=sys.stderr, flush=True)
            return
        initial = self.sync_openwebui_sessions(force=False)
        errors = initial.get("errors") or []
        print(f"OpenWebUI projection initial sync done: created={initial.get('created')} updated={initial.get('updated')} deleted_local={initial.get('deleted_local')} deleted_remote={initial.get('deleted_remote')} errors={len(errors)}", file=sys.stderr, flush=True)
        for err in errors:
            print(f"  [sync error] {err}", file=sys.stderr, flush=True)

    def _projection_sync_loop(self):
        while not self._sync_stop.wait(self.projection_sync_interval):
            signature = self._scan_session_signature()
            if signature == self._session_signature:
                result = self.sync_openwebui_sessions(force=False, changed_paths=set())
                if result.get("deleted_local") or result.get("errors"):
                    print(f"OpenWebUI deletion sync: deleted_local={result.get('deleted_local')} errors={len(result.get('errors') or [])}", file=sys.stderr, flush=True)
                continue
            changed = {
                path for path, sig in signature.items()
                if self._session_signature.get(path) != sig
            } | (
                self._session_signature.keys() - signature.keys()
            )
            self._session_signature = signature
            result = self.sync_openwebui_sessions(force=False, changed_paths=changed)
            if result.get("synced") or result.get("deleted_local") or result.get("deleted_remote") or result.get("errors"):
                print(f"OpenWebUI projection sync: created={result.get('created')} updated={result.get('updated')} deleted_local={result.get('deleted_local')} deleted_remote={result.get('deleted_remote')} errors={len(result.get('errors') or [])}", file=sys.stderr, flush=True)

    # ── Process lifecycle ────────────────────────────────────────

    def _get_chat_lock(self, chat_id: str) -> threading.RLock:
        with self.state_lock:
            lock = self.chat_locks.get(chat_id)
            if lock is None:
                lock = threading.RLock()
                self.chat_locks[chat_id] = lock
            return lock

    def _session_key(self, session_file: str) -> str:
        try:
            return str(Path(session_file).expanduser().resolve())
        except Exception:
            return str(session_file)

    def _release_writer(self, chat_id: str):
        with self.state_lock:
            for key, owner in list(self.session_writers.items()):
                if owner == chat_id:
                    self.session_writers.pop(key, None)

    def _active_writer_for(self, session_file: str, chat_id: str) -> Optional[str]:
        key = self._session_key(session_file)
        with self.state_lock:
            owner = self.session_writers.get(key)
            if not owner or owner == chat_id:
                return None
            proc = self.procs.get(owner)
            if proc is None or proc.poll() is not None:
                self.session_writers.pop(key, None)
                return None
            return owner

    def cleanup_stale(self):
        cutoff = time.monotonic() - self.idle_timeout_seconds
        with self.state_lock:
            stale = [cid for cid, ts in list(self.last_used.items()) if ts < cutoff]
        for chat_id in stale:
            with self._get_chat_lock(chat_id):
                with self.state_lock:
                    proc = self.procs.pop(chat_id, None)
                    self.last_used.pop(chat_id, None)
                self._release_writer(chat_id)
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

    def _target_session_for_payload(self, chat_id: str, payload: dict) -> Optional[str]:
        explicit = payload.get("pi_session_id") or payload.get("pi_session_file")
        if explicit:
            record = self._resolve_session(str(explicit))
            if not record:
                raise FileNotFoundError(f"Unknown Pi session: {explicit}")
            return str(record["sessionFile"])
        existing = self.mapping.get(chat_id)
        if existing and Path(existing).exists():
            return existing
        return None

    def _spawn_for_chat(self, chat_id: str, payload: dict) -> subprocess.Popen:
        target_session = self._target_session_for_payload(chat_id, payload)
        forked = False
        if target_session:
            owner = self._active_writer_for(target_session, chat_id)
            if owner:
                proc = self._spawn_rpc(fork_path=target_session)
                forked = True
            else:
                proc = self._spawn_rpc(session_path=target_session)
        else:
            proc = self._spawn_rpc(no_session=False)

        self._send(proc, {"type": "get_state"})
        resp = self._read_response(proc)
        session_file = (resp.get("data") or {}).get("sessionFile")
        if session_file:
            self.mapping[chat_id] = session_file
            self._save_mapping()
            with self.state_lock:
                self.session_writers[self._session_key(session_file)] = chat_id
            # Register in projection so the background sync doesn't create a duplicate
            # OpenWebUI chat for a session that was initiated through the bridge.
            pi_session_id = self._session_id_from_path(session_file)
            if pi_session_id and self._openwebui_enabled():
                with self.state_lock:
                    if pi_session_id not in self.projection:
                        self.projection[pi_session_id] = {
                            "chat_id": chat_id,
                            "session_file": session_file,
                            "fingerprint": None,
                            "synced_at": time.time(),
                        }
                self._save_projection()
        if forked:
            payload["_pi_forked_from"] = target_session
            payload["_pi_forked_to"] = session_file
        return proc

    def get_proc(self, chat_id: str, payload: dict) -> subprocess.Popen:
        requested_session = self._target_session_for_payload(chat_id, payload)
        with self.state_lock:
            proc = self.procs.get(chat_id)
            current_session = self.mapping.get(chat_id)
        requested_changed = bool(
            requested_session
            and current_session
            and self._session_key(requested_session) != self._session_key(current_session)
        )
        if requested_changed and proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._release_writer(chat_id)
            with self.state_lock:
                self.procs.pop(chat_id, None)
            proc = None
        if proc is None or proc.poll() is not None:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
                self._release_writer(chat_id)
            proc = self._spawn_for_chat(chat_id, payload)
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
            "indexed_session_dirs": [str(p) for p in self.indexed_session_dirs],
            "exclude_loadout_extension": self.exclude_loadout_extension,
            "active_writers": dict(self.session_writers),
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
                proc = self.get_proc(chat_id, payload)
            except Exception as exc:
                yield {"type": "error", "message": f"Failed to start Pi: {exc}"}
                yield {"type": "done"}
                return

            if payload.get("_pi_forked_from"):
                yield {
                    "type": "status",
                    "description": "Pi session was active elsewhere; forked a new session for OpenWebUI.",
                    "done": True,
                }

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
        self._sync_stop.set()
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5)
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

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

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

        if path == "/pi/sessions":
            q = (query.get("q") or [""])[0]
            return self._send_json(HTTPStatus.OK, {"sessions": self.state.list_pi_sessions(q)})

        if path.startswith("/pi/sessions/"):
            suffix = path[len("/pi/sessions/"):].strip("/")
            include_messages = False
            if suffix.endswith("/transcript"):
                suffix = suffix[: -len("/transcript")].strip("/")
                include_messages = True
            record = self.state.get_pi_session(suffix, include_messages=include_messages)
            if not record:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown Pi session"})
            return self._send_json(HTTPStatus.OK, {"session": record})

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if not self._auth_ok():
            return self._send_unauthorized()

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/pi/sync/openwebui":
            try:
                # force=False so fingerprints are respected; each call advances
                # through un-synced sessions. Run reset first to re-process all.
                return self._send_json(HTTPStatus.OK, self.state.sync_openwebui_sessions(force=False))
            except Exception as exc:
                return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if path == "/pi/sync/openwebui/reset":
            try:
                return self._send_json(HTTPStatus.OK, self.state.reset_projection_fingerprints())
            except Exception as exc:
                return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if path.startswith("/pi/sessions/") and path.endswith("/fork"):
            session_ref = path[len("/pi/sessions/"): -len("/fork")].strip("/")
            try:
                return self._send_json(HTTPStatus.OK, {"session": self.state.fork_pi_session(session_ref)})
            except FileNotFoundError as exc:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except Exception as exc:
                return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        if path != "/chat/stream":
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
    parser.add_argument("--pi-binary", default=os.environ.get("PI_BINARY", "pi-life-web"))
    parser.add_argument("--session-dir", default=os.environ.get("PI_BRIDGE_SESSION_DIR", str(DEFAULT_OWUI_SESSION_DIR)))
    parser.add_argument("--workspace-dir", default=os.environ.get("PI_BRIDGE_WORKSPACE_DIR", str(Path.home() / ".pi" / "owui-bridge-workspace")))
    parser.add_argument("--idle-timeout", type=int, default=int(os.environ.get("PI_BRIDGE_IDLE_TIMEOUT", "300")))
    parser.add_argument("--fallback-model", default=os.environ.get("PI_BRIDGE_FALLBACK_MODEL", "openai-codex/gpt-5.2"))
    parser.add_argument("--token", default=os.environ.get("PI_BRIDGE_TOKEN", ""))
    parser.add_argument("--openwebui-url", default=os.environ.get("OPENWEBUI_URL", os.environ.get("OPENWEBUI_BASE_URL", "")))
    parser.add_argument("--openwebui-api-key", default=os.environ.get("OPENWEBUI_API_KEY", ""))
    parser.add_argument("--projection-sync-interval", type=int, default=int(os.environ.get("PI_BRIDGE_PROJECTION_SYNC_INTERVAL", "30")), help="Seconds between file-change polls (default 30).")
    parser.add_argument("--projection-sync-batch-size", type=int, default=int(os.environ.get("PI_BRIDGE_PROJECTION_SYNC_BATCH", "50")), help="Max sessions to project per sync run (default 50).")
    parser.add_argument("--projection-sync-request-delay", type=float, default=float(os.environ.get("PI_BRIDGE_PROJECTION_REQUEST_DELAY", "0.15")), help="Seconds between OpenWebUI API requests (default 0.15).")
    parser.add_argument("--projection-sync-recent-days", type=int, default=int(os.environ.get("PI_BRIDGE_PROJECTION_RECENT_DAYS", "0")), help="Only project sessions updated in the last N days; 0 = all.")
    parser.add_argument("--projection-max-messages", type=int, default=int(os.environ.get("PI_BRIDGE_PROJECTION_MAX_MESSAGES", "100")), help="Max messages per projected chat (default 100).")
    parser.add_argument("--projection-max-message-chars", type=int, default=int(os.environ.get("PI_BRIDGE_PROJECTION_MAX_MSG_CHARS", "4000")), help="Max characters per message in projected chats (default 4000).")
    parser.add_argument("--owui-function-id", default=os.environ.get("PI_BRIDGE_OWUI_FUNCTION_ID", "pi_agent_bridge_function"), help="OpenWebUI function ID for model avatar/name in projected chats.")
    parser.add_argument(
        "--indexed-session-dirs",
        default=os.environ.get("PI_BRIDGE_INDEXED_SESSION_DIRS", ""),
        help="Comma-separated Pi session roots to expose through /pi/sessions.",
    )
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
    indexed_session_dirs = [p for p in args.indexed_session_dirs.split(",") if p]
    STATE = PiBridgeState(
        pi_binary=args.pi_binary,
        session_dir=args.session_dir,
        idle_timeout_seconds=args.idle_timeout,
        fallback_model=args.fallback_model,
        token=args.token,
        workspace_dir=args.workspace_dir,
        exclude_loadout_extension=not args.include_loadout_extension,
        indexed_session_dirs=indexed_session_dirs or None,
        openwebui_url=args.openwebui_url,
        openwebui_api_key=args.openwebui_api_key,
        projection_sync_interval=args.projection_sync_interval,
        projection_sync_batch_size=args.projection_sync_batch_size,
        projection_sync_request_delay=args.projection_sync_request_delay,
        projection_sync_recent_days=args.projection_sync_recent_days,
        projection_max_messages=args.projection_max_messages,
        projection_max_message_chars=args.projection_max_message_chars,
        owui_function_id=args.owui_function_id,
    )
    server = ThreadingHTTPServer((args.host, args.port), PiBridgeHandler)
    print(f"Pi bridge listening on http://{args.host}:{args.port}", flush=True)
    print(f"Pi bridge workspace: {STATE.workspace_dir}", flush=True)
    if STATE.exclude_loadout_extension:
        print("Pi bridge excludes pi-loadout extension via project settings override", flush=True)
    if args.token:
        print("Pi bridge token auth is enabled", flush=True)
    STATE.start_projection_sync()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Pi bridge...", flush=True)
    finally:
        server.server_close()
        STATE.shutdown()


if __name__ == "__main__":
    main()
