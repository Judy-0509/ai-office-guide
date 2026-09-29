from __future__ import annotations

import json

import httpx
import pytest

from aioffice.llm import BackendError
from aioffice.slides.stream import OpParser, stream_chat

MSG = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def _table_op(index=0, title="T"):
    slide = {"layout": "table", "kicker": "01", "title": title,
              "table": {"type": "table", "title": "표", "columns": ["a"], "rows": [["1"]]}}
    return json.dumps({"op": "insert", "index": index, "slide": slide}, ensure_ascii=False)


# --- OpParser ----------------------------------------------------------------------------
def test_op_parser_parses_complete_lines_in_one_feed():
    parser = OpParser()
    ops = parser.feed(_table_op(0, "A") + "\n" + _table_op(1, "B") + "\n")
    assert len(ops) == 2
    assert parser.warnings == []


def test_op_parser_buffers_a_line_split_across_chunks():
    parser = OpParser()
    line = _table_op(0, "A")
    mid = len(line) // 2
    assert parser.feed(line[:mid]) == []
    ops = parser.feed(line[mid:] + "\n")
    assert len(ops) == 1
    assert ops[0]["slide"]["title"] == "A"


def test_op_parser_skips_blank_lines_and_fences():
    parser = OpParser()
    text = "```jsonl\n\n" + _table_op(0, "A") + "\n```\n"
    ops = parser.feed(text)
    assert len(ops) == 1
    assert parser.warnings == []


def test_op_parser_reports_a_stray_prose_line_as_a_warning_but_keeps_going():
    parser = OpParser()
    text = "네, 슬라이드를 생성하겠습니다.\n" + _table_op(0, "A") + "\n"
    ops = parser.feed(text)
    assert len(ops) == 1
    assert len(parser.warnings) == 1
    assert "무시된 줄" in parser.warnings[0]


def test_op_parser_reports_invalid_json_as_a_warning():
    parser = OpParser()
    ops = parser.feed('{"op": "insert", "index": 0, "slide": {bad json\n')
    assert ops == []
    assert len(parser.warnings) == 1


def test_op_parser_reports_a_schema_validation_failure_as_a_warning():
    parser = OpParser()
    ops = parser.feed(json.dumps({"op": "insert", "index": 0, "slide": {"layout": "nope"}}) + "\n")
    assert ops == []
    assert len(parser.warnings) == 1


def test_op_parser_close_flushes_the_last_unterminated_line():
    parser = OpParser()
    parser.feed(_table_op(0, "A"))  # no trailing newline yet
    ops = parser.close()
    assert len(ops) == 1


def test_op_parser_close_on_empty_buffer_is_a_no_op():
    parser = OpParser()
    parser.feed(_table_op(0, "A") + "\n")
    assert parser.close() == []


# --- stream_chat ---------------------------------------------------------------------------
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

    monkeypatch.setattr("aioffice.slides.stream.time.sleep", lambda s: None)
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
