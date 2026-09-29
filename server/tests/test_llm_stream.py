"""Tests for aioffice.llm.stream.stream_chat (SSE streaming + <think> tag routing). Moved out
of test_slides_stream.py so this doesn't depend on the slides package -- stream_chat is
slides-agnostic (also used directly by LLMClient.stream_complete)."""
from __future__ import annotations

import json

import httpx
import pytest

from aioffice.llm import BackendError
from aioffice.llm.stream import stream_chat

MSG = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def _sse(chunks: list[dict]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode("utf-8"),
                           headers={"content-type": "text/event-stream"})


def test_stream_chat_routes_openrouter_reasoning_field(settings):
    chunks = [
        {"choices": [{"delta": {"reasoning": "생각 중"}}]},
        {"choices": [{"delta": {"content": "안녕"}}]},
        {"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: _sse(chunks)),
                           base_url="http://llm.test/v1")
    events = list(stream_chat(settings, MSG, 100, client=client))
    assert ("reasoning", "생각 중") in events
    assert ("content", "안녕") in events
    assert ("usage", {"prompt_tokens": 5, "completion_tokens": 2}) in events


def test_stream_chat_routes_vllm_reasoning_content_field(settings):
    chunks = [{"choices": [{"delta": {"reasoning_content": "생각"}}]}]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: _sse(chunks)),
                           base_url="http://llm.test/v1")
    events = list(stream_chat(settings, MSG, 100, client=client))
    assert ("reasoning", "생각") in events


def test_stream_chat_routes_think_tags_inside_content_split_across_chunks(settings):
    chunks = [
        {"choices": [{"delta": {"content": "<thi"}}]},
        {"choices": [{"delta": {"content": "nk>생각중</th"}}]},
        {"choices": [{"delta": {"content": "ink>본문"}}]},
    ]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: _sse(chunks)),
                           base_url="http://llm.test/v1")
    events = list(stream_chat(settings, MSG, 100, client=client))
    reasoning_text = "".join(p for e, p in events if e == "reasoning")
    content_text = "".join(p for e, p in events if e == "content")
    assert reasoning_text == "생각중"
    assert content_text == "본문"


def test_stream_chat_retries_once_without_stream_options_on_400(settings):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(400, text="Unrecognized request argument: stream_options")
        return _sse([{"choices": [{"delta": {"content": "ok"}}]}])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://llm.test/v1")
    events = list(stream_chat(settings, MSG, 100, client=client))
    assert len(calls) == 2
    assert "stream_options" in calls[0] and "stream_options" not in calls[1]
    assert ("content", "ok") in events


def test_stream_chat_raises_on_400_unrelated_to_stream_options(settings):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad request")),
        base_url="http://llm.test/v1")
    with pytest.raises(BackendError):
        list(stream_chat(settings, MSG, 100, client=client))


def test_stream_chat_retries_5xx_before_the_first_byte(settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503)
        return _sse([{"choices": [{"delta": {"content": "ok"}}]}])

    monkeypatch.setattr("aioffice.llm.stream.time.sleep", lambda s: None)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://llm.test/v1")
    events = list(stream_chat(settings, MSG, 100, client=client))
    assert len(calls) == 3
    assert ("content", "ok") in events


def test_stream_chat_emits_error_on_finish_reason_length(settings):
    chunks = [{"choices": [{"delta": {"content": "일부"}, "finish_reason": "length"}]}]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: _sse(chunks)),
                           base_url="http://llm.test/v1")
    events = list(stream_chat(settings, MSG, 100, client=client))
    assert ("error", "출력이 max_tokens에서 잘렸습니다") in events


def test_stream_chat_cancel_event_stops_reading(settings):
    import threading

    chunks = [
        {"choices": [{"delta": {"content": "one"}}]},
        {"choices": [{"delta": {"content": "two"}}]},
    ]
    cancel = threading.Event()
    cancel.set()  # already cancelled before we start reading
    client = httpx.Client(transport=httpx.MockTransport(lambda r: _sse(chunks)),
                           base_url="http://llm.test/v1")
    events = list(stream_chat(settings, MSG, 100, client=client, cancel=cancel))
    assert events == []
