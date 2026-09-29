"""Streaming chat call + incremental JSON-Lines op parser.

Research basis (see the work order): JSON Lines is the most robust streaming format for a
27B model -- one bad token spoils one slide line, not the whole deck. `OpParser` parses ONLY
the content channel, never reasoning.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

import httpx

from ..config import Settings
from ..llm import BackendError
from ..llm.direct import BACKOFF_SECONDS, RETRYABLE_STATUS
from ..llm.schemas import SchemaError
from . import spec

Event = tuple[str, Any]


class _ThinkRouter:
    """Routes <think>...</think> spans found INSIDE the content channel to reasoning events,
    for servers with no separate reasoning parser. Handles a tag split across chunks."""

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    @staticmethod
    def _partial_suffix_len(buf: str, tag: str) -> int:
        for length in range(min(len(buf), len(tag) - 1), 0, -1):
            if tag.startswith(buf[-length:]):
                return length
        return 0

    def feed(self, text: str) -> list[Event]:
        self._buf += text
        events: list[Event] = []
        while True:
            tag = "</think>" if self._in_think else "<think>"
            idx = self._buf.find(tag)
            if idx == -1:
                partial = self._partial_suffix_len(self._buf, tag)
                cut = len(self._buf) - partial
                if cut > 0:
                    events.append(("reasoning" if self._in_think else "content", self._buf[:cut]))
                    self._buf = self._buf[cut:]
                break
            before, self._buf = self._buf[:idx], self._buf[idx + len(tag):]
            if before:
                events.append(("reasoning" if self._in_think else "content", before))
            self._in_think = not self._in_think
        return events

    def close(self) -> list[Event]:
        if not self._buf:
            return []
        events = [("reasoning" if self._in_think else "content", self._buf)]
        self._buf = ""
        return events


def _payload(settings: Settings, messages: list[dict], max_tokens: int, include_usage: bool) -> dict:
    payload: dict[str, Any] = {
        "model": settings.llm_model_default,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": settings.llm_temperature,
        "stream": True,
    }
    if include_usage:
        payload["stream_options"] = {"include_usage": True}
    if settings.llm_disable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def stream_chat(
    settings: Settings,
    messages: list[dict[str, str]],
    max_tokens: int,
    *,
    client: httpx.Client | None = None,
    cancel: Any = None,
) -> Iterator[Event]:
    """Stream a chat completion. Yields ("reasoning", str) | ("content", str) | ("usage", dict)
    | ("error", str). Retries 429/502/503/504 before the first byte (reuses direct.py's
    backoff). If the server 400s on `stream_options`, retries once without it. Always decodes
    UTF-8. `cancel`, if given, is a threading.Event: set it to stop reading at the next chunk."""
    own_client = client is None
    if own_client:
        if not settings.llm_base_url:
            raise BackendError("LLM_BASE_URL is not configured")
        http_client = httpx.Client(base_url=settings.llm_base_url, timeout=settings.llm_timeout_sec)
    else:
        http_client = client
    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"
    try:
        include_usage = True
        for attempt in range(len(BACKOFF_SECONDS) + 1):
            payload = _payload(settings, messages, max_tokens, include_usage)
            try:
                with http_client.stream(
                    "POST", "/chat/completions", json=payload, headers=headers,
                    timeout=settings.llm_timeout_sec,
                ) as response:
                    if response.status_code == 400 and include_usage:
                        response.read()
                        body = response.text
                        if "stream_options" in body:
                            include_usage = False
                            continue
                        raise BackendError(f"direct HTTP 400: {body[:200]}")
                    if response.status_code in RETRYABLE_STATUS:
                        if attempt == len(BACKOFF_SECONDS):
                            raise BackendError(f"direct HTTP {response.status_code}")
                        time.sleep(BACKOFF_SECONDS[attempt])
                        continue
                    if response.status_code >= 400:
                        response.read()
                        raise BackendError(
                            f"direct HTTP {response.status_code}: {response.text[:200]}")
                    response.encoding = "utf-8"
                    yield from _consume(response, cancel)
                    return
            except httpx.HTTPError as exc:
                raise BackendError(f"direct transport error: {exc}") from exc
    finally:
        if own_client:
            http_client.close()


def _consume(response: httpx.Response, cancel: Any) -> Iterator[Event]:
    router = _ThinkRouter()
    finish_reason: str | None = None
    for raw_line in response.iter_lines():
        if cancel is not None and cancel.is_set():
            break
        if not raw_line:
            continue
        line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="replace")
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning:
                yield ("reasoning", reasoning)
            content = delta.get("content")
            if content:
                yield from router.feed(content)
            if choice.get("finish_reason") == "length":
                finish_reason = "length"
        usage = chunk.get("usage")
        if usage:
            yield ("usage", usage)
    yield from router.close()
    if finish_reason == "length":
        yield ("error", "출력이 max_tokens에서 잘렸습니다")


class OpParser:
    """Incremental JSON-Lines op parser. `feed(text)` buffers partial lines across chunks,
    skips blank lines and ``` fences, tolerates a stray prose line (reported as a warning),
    and validates each op + slide against spec.py. Invalid lines never raise -- they land in
    `self.warnings`."""

    _FENCE_RE = re.compile(r"^```\w*$")

    def __init__(self) -> None:
        self._buf = ""
        self.warnings: list[str] = []

    def feed(self, text: str) -> list[dict]:
        self._buf += text
        lines = self._buf.split("\n")
        self._buf = lines.pop()  # last element may be a partial line
        return self._parse_lines(lines)

    def close(self) -> list[dict]:
        remaining, self._buf = self._buf, ""
        if not remaining.strip():
            return []
        return self._parse_lines([remaining])

    def _parse_lines(self, lines: list[str]) -> list[dict]:
        ops: list[dict] = []
        for raw in lines:
            line = raw.strip()
            if not line or self._FENCE_RE.match(line):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                self.warnings.append(f"모델 출력 중 무시된 줄: {line[:80]}")
                continue
            try:
                ops.append(spec.coerce_and_validate_op(data))
            except SchemaError as exc:
                self.warnings.append(str(exc))
        return ops
