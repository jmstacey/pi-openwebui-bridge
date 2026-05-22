"""
title: Pi Agent Bridge Function
author: jon
description: |
  OpenWebUI Function that connects Dockerized OpenWebUI to a macOS Pi bridge.
  The bridge owns pi --mode rpc subprocesses on the host. This Function owns
  OpenWebUI integration: model listing, streaming response text, thinking blocks,
  and real WebSocket status events via __event_emitter__.

  Required host bridge:
    python3 /Users/jon/Downloads/pi_bridge_server.py --host 127.0.0.1 --port 8765

version: 0.1.0
requirements: aiohttp, pydantic
"""

from __future__ import annotations

import inspect
import json
import time
import uuid
from typing import AsyncGenerator, Callable, List, Literal, Optional, Union

import aiohttp
from pydantic import BaseModel


class Pipe:
    class Valves(BaseModel):
        bridge_url: str = "http://host.docker.internal:8765"
        bridge_token: str = ""
        fallback_model: str = "openai-codex/gpt-5.2"
        default_thinking_level: str = "high"
        guardrail_default: Literal["allow", "deny"] = "deny"
        request_timeout_seconds: int = 600
        model_cache_seconds: int = 30

    def __init__(self):
        # OpenWebUI Functions detect a manifold by the presence of pipes().
        self.type = "pipe"
        self.name = "pi/"
        self.valves = self.Valves()
        self._model_list: list[dict] = []
        self._model_cache_at: float = 0.0

    # ── Helpers ──────────────────────────────────────────────────

    def _bridge_url(self, path: str) -> str:
        base = self.valves.bridge_url.rstrip("/")
        return f"{base}{path}"

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.valves.bridge_token:
            headers["X-Pi-Bridge-Token"] = self.valves.bridge_token
        return headers

    async def _emit(self, emitter: Optional[Callable], event: dict):
        if emitter is None:
            return
        try:
            result = emitter(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    async def _status(self, emitter: Optional[Callable], description: str, done: bool = False):
        await self._emit(
            emitter,
            {"type": "status", "data": {"description": description, "done": done}},
        )

    def _done_chunk(self, model: str) -> str:
        msg = {
            "id": f"{model}-{uuid.uuid4()}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "delta": {}, "logprobs": None, "finish_reason": "stop"}
            ],
        }
        return f"data: {json.dumps(msg)}"

    def _fallback_models(self) -> list[dict]:
        fallback = self.valves.fallback_model
        return [{"id": fallback.replace("/", "--"), "name": fallback}]

    def _get_last_user_message(self, messages: List[dict]) -> str:
        for msg in reversed(messages or []):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text", ""))
                return "\n".join(p for p in parts if p)
            return str(content)
        return ""

    def _extract_images(self, messages: List[dict]) -> list[dict]:
        images = []
        for msg in reversed(messages or []):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "image_url":
                        continue
                    url = (part.get("image_url") or {}).get("url", "")
                    if not url.startswith("data:"):
                        continue
                    try:
                        header, data = url.split(",", 1)
                        mime = header.split(":", 1)[1].split(";", 1)[0]
                        images.append({"type": "image", "data": data, "mimeType": mime})
                    except Exception:
                        pass
            break
        return images

    def _sub_model_id(self, model_value: str) -> str:
        # Function manifold model IDs arrive as "function_id.sub_model_id".
        return model_value.split(".", 1)[1] if "." in model_value else model_value

    def _chat_id_from(self, body: dict, metadata: dict, __chat_id__: Optional[str]) -> str:
        return (
            __chat_id__
            or metadata.get("chat_id")
            or body.get("chat_id")
            or body.get("metadata", {}).get("chat_id")
            or "default"
        )

    # ── Model manifold ───────────────────────────────────────────

    async def pipes(self) -> list[dict]:
        # Keep this cache short. OpenWebUI may call pipes() while the bridge is
        # still starting; a permanent fallback cache would hide the real Pi models.
        now = time.monotonic()
        if self._model_list and (now - self._model_cache_at) < self.valves.model_cache_seconds:
            return self._model_list

        headers = self._headers()
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._bridge_url("/models"), headers=headers) as resp:
                    if resp.status != 200:
                        return self._model_list or self._fallback_models()
                    data = await resp.json()
                    models = data.get("models") or []
                    if models:
                        self._model_list = models
                        self._model_cache_at = now
                        return models
        except Exception:
            pass
        return self._model_list or self._fallback_models()

    # ── Bridge SSE handling ──────────────────────────────────────

    async def _handle_bridge_event(
        self,
        event: dict,
        __event_emitter__: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        etype = event.get("type")

        if etype == "text_delta":
            delta = event.get("delta") or ""
            if delta:
                yield delta

        elif etype == "thinking_start":
            yield "<think>\n"

        elif etype == "thinking_delta":
            delta = event.get("delta") or ""
            if delta:
                yield delta

        elif etype == "thinking_end":
            yield "\n</think>\n\n"

        elif etype == "status":
            await self._status(
                __event_emitter__,
                event.get("description") or "Pi is working",
                bool(event.get("done", False)),
            )

        elif etype == "error":
            message = event.get("message") or "Unknown Pi bridge error"
            await self._status(__event_emitter__, message, True)
            yield f"\n\n[Pi Bridge] {message}\n"

    async def _stream_bridge(
        self,
        payload: dict,
        model_value: str,
        __event_emitter__: Optional[Callable],
    ) -> AsyncGenerator[str, None]:
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        timeout = aiohttp.ClientTimeout(total=self.valves.request_timeout_seconds, sock_read=None)
        done = False

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self._bridge_url("/chat/stream"), headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        try:
                            text = await resp.text()
                        except Exception:
                            text = ""
                        message = f"Bridge returned HTTP {resp.status}: {text[:500]}"
                        await self._status(__event_emitter__, message, True)
                        yield f"[Pi Bridge] {message}"
                        done = True
                        return

                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8", errors="ignore").strip()
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            done = True
                            break
                        try:
                            event = json.loads(data)
                        except Exception:
                            continue

                        if event.get("type") == "done":
                            done = True
                            break

                        async for chunk in self._handle_bridge_event(event, __event_emitter__):
                            yield chunk

        except Exception as exc:
            message = f"Could not reach Pi bridge at {self.valves.bridge_url}: {exc}"
            await self._status(__event_emitter__, message, True)
            yield f"[Pi Bridge] {message}"

        finally:
            # OpenWebUI Functions currently add finish chunks for strings/sync generators,
            # but not consistently for AsyncGenerator responses. Emit them explicitly.
            if not done:
                await self._status(__event_emitter__, "Pi bridge stream ended", True)
            yield self._done_chunk(model_value)
            yield "data: [DONE]"

    # ── Main OpenWebUI handler ───────────────────────────────────

    async def pipe(
        self,
        body: dict,
        __event_emitter__: Optional[Callable] = None,
        __event_call__: Optional[Callable] = None,
        __chat_id__: Optional[str] = None,
        __session_id__: Optional[str] = None,
        __message_id__: Optional[str] = None,
        __files__: Optional[list] = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __request__=None,
    ) -> Union[str, AsyncGenerator[str, None]]:
        metadata = __metadata__ or body.get("metadata") or {}
        messages = body.get("messages", []) or []
        model_value = body.get("model") or self.valves.fallback_model.replace("/", "--")
        model_id = self._sub_model_id(model_value)
        chat_id = self._chat_id_from(body, metadata, __chat_id__)

        payload = {
            "chat_id": chat_id,
            "model_id": model_id,
            "message": self._get_last_user_message(messages),
            "messages": messages,
            "images": self._extract_images(messages),
            "reasoning_effort": body.get("reasoning_effort"),
            "default_thinking_level": self.valves.default_thinking_level,
            "guardrail_default": self.valves.guardrail_default,
        }

        return self._stream_bridge(payload, model_value, __event_emitter__)
