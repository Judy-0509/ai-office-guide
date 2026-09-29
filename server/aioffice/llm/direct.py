from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..config import Settings
from . import BackendError
from .schemas import SchemaError

RETRYABLE_STATUS = {429, 502, 503, 504}
BACKOFF_SECONDS = (2, 4, 8)


class DirectBackend:
    """Intranet OpenAI-compatible chat completions with a single `submit` tool."""

    name = "direct"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self._clients: dict[str, httpx.Client] = {}
        self._sleep = time.sleep

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            if not self.settings.llm_base_url:
                raise BackendError("LLM_BASE_URL is not configured")
            self._client = httpx.Client(
                base_url=self.settings.llm_base_url,
                timeout=self.settings.llm_timeout_sec,
            )
        return self._client

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        base_url: str | None = None,
        api_key: str = "",
        timeout: float | None = None,
    ) -> Any:
        """Share transport retries between chat and embedding requests."""
        if base_url is None or base_url.rstrip("/") == self.settings.llm_base_url:
            client = self.client
        else:
            url = base_url.rstrip("/")
            client = self._clients.get(url)
            if client is None:
                client = self._clients[url] = httpx.Client(
                    base_url=url, timeout=self.settings.llm_timeout_sec
                )
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request_timeout = self.settings.llm_timeout_sec if timeout is None else timeout

        for attempt in range(len(BACKOFF_SECONDS) + 1):
            try:
                response = client.post(
                    path, json=payload, headers=headers, timeout=request_timeout
                )
            except httpx.TimeoutException as exc:
                # a distinct message prefix (not just "transport error") lets a caller like
                # dataplat.chat detect "the shared endpoint was too slow" and show a friendlier
                # message than a generic failure, without depending on httpx's own wording.
                raise BackendError(f"direct timeout: {exc}") from exc
            except httpx.HTTPError as exc:
                raise BackendError(f"direct transport error: {exc}") from exc
            if response.status_code in RETRYABLE_STATUS:
                if attempt == len(BACKOFF_SECONDS):
                    raise BackendError(
                        f"direct HTTP {response.status_code}: {response.text[:200]}"
                    )
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(30.0, max(0.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = BACKOFF_SECONDS[attempt]
                self._sleep(delay)
                continue
            if response.status_code >= 400:
                raise BackendError(
                    f"direct HTTP {response.status_code}: {response.text[:200]}"
                )
            try:
                return response.json()
            except ValueError as exc:
                raise BackendError(f"direct response was not JSON: {exc}") from exc
        raise AssertionError("retry loop must return or raise")

    def model_for(self) -> str:
        return self.settings.llm_model_default

    def run_content(
        self, messages, model, timeout: float | None = None, max_tokens: int | None = None,
    ) -> tuple[str, dict]:
        """Like `run()`, but for callers that parse a bare JSON object out of the message
        content themselves instead of relying on a `tool_choice` tool call (named
        `tool_choice` is unverified against some in-house backends)."""
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
            "temperature": self.settings.llm_temperature,
        }
        if self.settings.llm_disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        response = self.post_json(
            "/chat/completions", payload, api_key=self.settings.llm_api_key, timeout=timeout,
        )
        try:
            choice = response["choices"][0]
            if choice.get("finish_reason") == "length":
                raise SchemaError("output truncated (finish_reason=length)")
            content = choice["message"]["content"]
        except SchemaError:
            raise
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(f"direct response had no usable content: {exc}") from exc
        usage = response.get("usage") or {}
        return content, {
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
        }

    def run(
        self, messages, schema, model, timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[dict, dict]:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
            "temperature": self.settings.llm_temperature,
            "tools": [{
                "type": "function",
                "function": {
                    "name": "submit",
                    "description": "Submit the answer as a single JSON object.",
                    "parameters": schema,
                },
            }],
            "tool_choice": {"type": "function", "function": {"name": "submit"}},
        }
        if self.settings.llm_disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        response = self.post_json(
            "/chat/completions", payload, api_key=self.settings.llm_api_key,
            timeout=timeout,
        )
        try:
            choice = response["choices"][0]
            if choice.get("finish_reason") == "length":
                raise SchemaError("output truncated (finish_reason=length)")
            message = choice["message"]
            arguments = message["tool_calls"][0]["function"]["arguments"]
            try:
                data = json.loads(arguments) if isinstance(arguments, str) else arguments
            except (TypeError, ValueError) as exc:
                raise SchemaError(f"model returned invalid JSON: {exc}") from exc
        except SchemaError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise BackendError(f"direct response had no usable submit call: {exc}") from exc
        usage = response.get("usage") or {}
        return data, {
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
        }
