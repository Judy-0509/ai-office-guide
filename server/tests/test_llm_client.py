from __future__ import annotations

import json
import math
import threading

import httpx
import pytest

from aioffice import db
from aioffice.llm import BackendError, LLMError, schemas
from aioffice.llm.client import LLMClient, MAX_ATTEMPTS
from aioffice.llm.direct import DirectBackend

MSG = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
TINY = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


# --- schema validator -------------------------------------------------------
def test_validate_accepts_and_rejects():
    schemas.validate({"ok": True}, TINY)
    with pytest.raises(schemas.SchemaError, match="missing required"):
        schemas.validate({}, TINY)
    with pytest.raises(schemas.SchemaError, match="expected boolean"):
        schemas.validate({"ok": "yes"}, TINY)
    with pytest.raises(schemas.SchemaError, match="not in"):
        schemas.validate({"direction": "up"}, {"type": "object", "properties": {
            "direction": {"type": "string", "enum": schemas.DIRECTION_ENUM}}})


def test_coerce_cleans_common_model_values_and_drops_only_invalid_array_items(caplog):
    item = {
        "type": "object", "required": ["direction", "confidence", "value", "page", "enabled", "label"],
        "properties": {
            "direction": {"type": "string", "enum": schemas.DIRECTION_ENUM},
            "confidence": {"type": "string", "enum": schemas.CONFIDENCE_ENUM},
            "value": {"type": "number"}, "page": {"type": "integer"},
            "enabled": {"type": "boolean"}, "label": {"type": "string"},
        },
    }
    schema = {"type": "object", "required": ["rows"],
              "properties": {"rows": {"type": "array", "items": item}}}
    raw = {"rows": [
        {"direction": " ↑ ", "confidence": "moderate", "value": "$3.2",
         "page": "p.3", "enabled": "true", "label": 245},
        {"direction": "△", "confidence": "low", "value": "bad",
         "page": "3페이지", "enabled": "false", "label": "bad"},
    ]}

    cleaned = schemas.coerce(raw, schema)

    assert cleaned == {"rows": [{"direction": "▲", "confidence": "med", "value": 3.2,
                                  "page": 3, "enabled": True, "label": "245"}]}
    assert "dropped 1 invalid items" in caplog.text
    assert raw["rows"][0]["direction"] == " ↑ "
    assert schemas.coerce("12.5%", {"type": "number"}) == 12.5
    assert schemas.coerce("3", {"type": "integer"}) == 3
    assert schemas.coerce(3.0, {"type": "integer"}) == 3
    assert schemas.coerce("HIGH", {"type": "string", "enum": schemas.CONFIDENCE_ENUM}) == "high"
    assert schemas.coerce("neutral", {"type": "string", "enum": schemas.DIRECTION_ENUM}) == "–"
    assert schemas.coerce("△", {"type": "string", "enum": schemas.DIRECTION_ENUM}) == "△"


# --- extract_json_object (no-tools JSON-in-content parsing) -----------------
def test_extract_json_object_strips_think_blocks_and_code_fences():
    raw = '<think>내부 추론이 길게 이어지는 중이다...</think>```json\n{"a": 1, "b": [1, 2, 3]}\n```'
    assert schemas.extract_json_object(raw) == {"a": 1, "b": [1, 2, 3]}


def test_extract_json_object_finds_the_first_balanced_object_amid_prose():
    raw = 'here is my answer: {"ok": true, "nested": {"x": 1}} -- done'
    assert schemas.extract_json_object(raw) == {"ok": True, "nested": {"x": 1}}


def test_extract_json_object_returns_none_when_nothing_parses():
    assert schemas.extract_json_object("no json here at all") is None
    assert schemas.extract_json_object(None) is None
    assert schemas.extract_json_object(123) is None


# --- direct backend ---------------------------------------------------------
def test_direct_backend_parses_submit_tool_arguments(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={
            "choices": [{"message": {"tool_calls": [
                {"function": {"name": "submit", "arguments": json.dumps({"ok": True})}}]}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        })

    settings.llm_api_key = "secret"
    settings.llm_timeout_sec = 37
    settings.llm_max_tokens = 1234
    settings.llm_temperature = 0.15
    settings.llm_disable_thinking = True
    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://llm.test/v1"))
    data, usage = backend.run(MSG, TINY, "qwen-default")

    assert data == {"ok": True}
    assert usage == {"prompt_tokens": 11, "completion_tokens": 7}
    assert seen["body"]["model"] == "qwen-default"
    assert seen["body"]["tools"][0]["function"]["name"] == "submit"
    assert seen["body"]["tools"][0]["function"]["parameters"] == TINY
    assert seen["body"]["tool_choice"]["function"]["name"] == "submit"
    assert seen["body"]["max_tokens"] == 1234 and seen["body"]["temperature"] == 0.15
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["auth"] == "Bearer secret"
    assert seen["timeout"]["read"] == 37


def test_direct_backend_accepts_a_per_call_output_limit(settings):
    seen = {}
    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(lambda request: (
            seen.update(json.loads(request.content)) or httpx.Response(200, json={
                "choices": [{"message": {"tool_calls": [
                    {"function": {"arguments": '{"ok": true}'}}]}}],
            })
        )), base_url="http://llm.test/v1"))
    backend.run(MSG, TINY, "qwen-default", max_tokens=321)
    assert seen["max_tokens"] == 321


def test_direct_backend_retries_retry_after_and_backoff(settings):
    statuses = [
        httpx.Response(429, headers={"Retry-After": "35"}, text="slow"),
        httpx.Response(502, text="gateway"),
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"function": {"arguments": '{"ok": true}'}}]}}]}),
    ]
    calls, delays = [], []

    def handler(request):
        calls.append(request)
        return statuses.pop(0)

    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://llm.test/v1"))
    backend._sleep = delays.append

    assert backend.run(MSG, TINY, "qwen")[0] == {"ok": True}
    assert len(calls) == 4 and delays == [30.0, 4, 8]


def test_direct_backend_stops_after_three_retries(settings):
    calls, delays = [], []
    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(lambda request: (calls.append(request), httpx.Response(429))[1]),
        base_url="http://llm.test/v1"))
    backend._sleep = delays.append
    with pytest.raises(BackendError):
        backend.run(MSG, TINY, "qwen")
    assert len(calls) == 4 and delays == [2, 4, 8]


def test_direct_backend_length_finish_reason_is_schema_error(settings):
    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {}}],
        })), base_url="http://llm.test/v1"))
    with pytest.raises(schemas.SchemaError, match="finish_reason=length"):
        backend.run(MSG, TINY, "qwen")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(429, text="slow down"),
        httpx.Response(200, json={"choices": [{"message": {"content": "no tool call"}}]}),
    ],
)
def test_direct_backend_raises_backend_error(settings, response):
    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(lambda r: response), base_url="http://llm.test/v1"))
    with pytest.raises(BackendError):
        backend.run(MSG, TINY, "qwen-default")


def test_direct_backend_treats_invalid_model_json_as_schema_error(settings):
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"tool_calls": [
                {"function": {"arguments": "{not json"}}
            ]}}],
        })

    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://llm.test/v1"))
    with pytest.raises(schemas.SchemaError, match="invalid JSON"):
        backend.run(MSG, TINY, "qwen-default")


def test_direct_backend_omits_thinking_option_by_default(settings):
    seen = {}
    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(lambda request: (
            seen.update(json.loads(request.content)) or httpx.Response(200, json={
                "choices": [{"message": {"tool_calls": [
                    {"function": {"arguments": '{"ok": true}'}}]}}],
            })
        )), base_url="http://llm.test/v1"))
    backend.run(MSG, TINY, "qwen-default")
    assert "chat_template_kwargs" not in seen


def test_direct_backend_run_content_returns_raw_message_content_without_tools(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"claims": []}'}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        })

    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://llm.test/v1"))
    content, usage = backend.run_content(MSG, "qwen-default", max_tokens=555)

    assert content == '{"claims": []}'
    assert usage == {"prompt_tokens": 5, "completion_tokens": 3}
    assert "tools" not in seen["body"] and "tool_choice" not in seen["body"]
    assert seen["body"]["max_tokens"] == 555


def test_direct_backend_run_content_truncated_is_schema_error(settings):
    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {"content": "cut off"}}],
        })), base_url="http://llm.test/v1"))
    with pytest.raises(schemas.SchemaError, match="finish_reason=length"):
        backend.run_content(MSG, "qwen")


def test_direct_backend_model_for(settings):
    settings.llm_model_default = "qwen"
    backend = DirectBackend(settings)
    assert backend.model_for() == "qwen"


# --- client: model resolution, budgets, logging ------------------------------
def make_client(settings, conn, backend, clock=None):
    return LLMClient(settings, conn, backend=backend, now=clock or (lambda: 1000.0))


class _RankerBackend:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def post_json(self, path, payload, *, base_url, api_key):
        self.requests.append((path, payload, base_url, api_key))
        return self.handler(path, payload)


def _ranker_client(settings, conn, handler):
    backend = _RankerBackend(handler)
    settings.rerank_base_url = "http://rank.test/v1"
    settings.rerank_api_key = "rank-key"
    return LLMClient(settings, conn, backend=backend), backend


@pytest.mark.parametrize(
    "style,response,path,expected_payload",
    [
        ("cohere", {"results": [{"index": 1, "relevance_score": 0.2},
                                  {"index": 0, "relevance_score": 2.0}]},
         "/rerank", {"model", "query", "documents"}),
        ("tei", [{"index": 1, "score": 0.2}, {"index": 0, "score": 2.0}],
         "/rerank", {"query", "texts"}),
        ("score", {"data": [{"index": 1, "score": 0.2}, {"index": 0, "score": 2.0}]},
         "/score", {"model", "text_1", "text_2"}),
    ],
)
def test_reranker_supports_all_api_shapes_and_restores_input_order(
    settings, conn, style, response, path, expected_payload
):
    client, backend = _ranker_client(settings, conn, lambda _path, _payload: response)
    settings.rerank_api_style = style

    scores = client.rerank("query", ["first", "second"], task_id=17)

    assert scores == pytest.approx([1 / (1 + math.exp(-2)), 0.2])
    request_path, payload, base_url, api_key = backend.requests[0]
    assert request_path == f"{base_url}{path}" and api_key == "rank-key"
    assert set(payload) == expected_payload
    assert tuple(db.fetchone(conn, "SELECT step, backend, status FROM llm_calls")) == (
        "rerank", "rerank", "ok"
    )


def test_reranker_batches_requests_and_truncates_each_document(settings, conn):
    def respond(_path, payload):
        return {"results": [{"index": i, "relevance_score": (i + 1) / 10}
                            for i in reversed(range(len(payload["documents"]))) ]}

    client, backend = _ranker_client(settings, conn, respond)
    settings.rerank_batch_size = 2
    docs = ["A" * 2501, "B", "C"]

    assert client.rerank("query", docs) == [0.1, 0.2, 0.1]
    assert len(backend.requests) == 2
    assert len(backend.requests[0][1]["documents"]) == 2
    assert len(backend.requests[0][1]["documents"][0]) == 2000
    assert backend.requests[1][1]["documents"] == ["C"]
    assert db.fetchone(conn, "SELECT COUNT(*) c FROM llm_calls WHERE step='rerank' AND status='ok'")["c"] == 2


def test_reranker_failure_is_logged_and_raises_llm_error(settings, conn):
    client, _ = _ranker_client(
        settings, conn, lambda _path, _payload: {"results": [{"index": 0, "relevance_score": 0.5}]},
    )
    with pytest.raises(LLMError, match="reranker batch failed"):
        client.rerank("query", ["one", "two"])
    row = db.fetchone(conn, "SELECT step, backend, status FROM llm_calls")
    assert tuple(row) == ("rerank", "rerank", "error")


def test_max_tokens_override_reaches_the_backend(settings, conn, fake_backend):
    backend = fake_backend(result={"ok": True})
    client = make_client(settings, conn, backend)

    client.complete(MSG, TINY, step="card", agent="a", max_tokens=321)

    assert backend.seen[0]["max_tokens"] == 321


def test_model_override_bypasses_model_for(settings, conn, fake_backend):
    backend = fake_backend(result={"ok": True})
    client = make_client(settings, conn, backend)

    client.complete(MSG, TINY, step="card", agent="a", model="vision-1")

    assert backend.seen[0]["model"] == "vision-1"


def test_complete_logs_an_llm_call_row(settings, conn, fake_backend):
    settings.llm_model_default = "qwen"
    backend = fake_backend(result={"ok": True})
    client = make_client(settings, conn, backend)
    out = client.complete(MSG, TINY, step="metadata", agent="애널리스트 A", task_id=None)
    assert out == {"ok": True}
    row = db.fetchone(conn, "SELECT * FROM llm_calls ORDER BY id DESC")
    assert row["backend"] == "direct" and row["model"] == "qwen"
    assert row["status"] == "ok" and row["step"] == "metadata" and row["agent"] == "애널리스트 A"
    assert row["prompt_tokens"] == 10 and row["completion_tokens"] == 5


def test_complete_coerces_simple_boolean_strings(settings, conn, fake_backend):
    backend = fake_backend(result={"ok": "true"})
    client = make_client(settings, conn, backend)
    assert client.complete(MSG, TINY, step="s", agent="a") == {"ok": True}


def test_schema_error_exhausts_retries_then_raises(settings, conn, fake_backend):
    backend = fake_backend(result={"wrong": 1})
    client = make_client(settings, conn, backend)
    with pytest.raises(LLMError):
        client.complete(MSG, TINY, step="s", agent="a")
    assert backend.calls == MAX_ATTEMPTS
    errors = db.fetchall(conn, "SELECT backend, status FROM llm_calls WHERE status='error'")
    assert len(errors) == MAX_ATTEMPTS


def test_backend_error_exhausts_retries_then_raises(settings, conn, fake_backend):
    backend = fake_backend(error="down")
    client = make_client(settings, conn, backend)
    with pytest.raises(LLMError):
        client.complete(MSG, TINY, step="s", agent="a")
    assert backend.calls == MAX_ATTEMPTS


# --- complete_json (no-tools path used by the analyst pipeline) -------------
def test_complete_json_parses_and_validates_bare_json_content(analyst_llm, conn):
    analyst_llm.backend.responses.append('<think>...</think>{"ok": true}')
    out = analyst_llm.complete_json(MSG, TINY, step="analyst.extract", agent="analyst")
    assert out == {"ok": True}
    row = db.fetchone(conn, "SELECT step, backend, status FROM llm_calls")
    assert tuple(row) == ("analyst.extract", "direct", "ok")


def test_complete_json_retries_once_then_succeeds(analyst_llm):
    analyst_llm.backend.responses.extend(["no json here", '{"ok": true}'])
    out = analyst_llm.complete_json(MSG, TINY, step="s", agent="a")
    assert out == {"ok": True}
    assert len(analyst_llm.backend.calls) == 2


def test_complete_json_exhausts_retries_then_raises(analyst_llm, conn):
    analyst_llm.backend.responses.extend(["not json", "still not json"])
    with pytest.raises(LLMError):
        analyst_llm.complete_json(MSG, TINY, step="s", agent="a")
    errors = db.fetchall(conn, "SELECT status FROM llm_calls WHERE status='error'")
    assert len(errors) == MAX_ATTEMPTS


def test_complete_json_never_sends_tools_or_tool_choice(settings, conn):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    settings.llm_api_key = "sk-should-never-leak"
    real_backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://llm.test/v1"))
    client = LLMClient(settings, conn, backend=real_backend)
    client.complete_json(MSG, TINY, step="s", agent="a")

    assert "tools" not in seen["body"] and "tool_choice" not in seen["body"]


def test_embed_batches_normalizes_orders_and_caches_vectors(settings, conn):
    requests = []
    urls = []

    def handler(request):
        urls.append(str(request.url))
        payload = json.loads(request.content)
        requests.append(payload)
        count = len(payload["input"])
        records = [{"index": i, "embedding": [1.0 if i == 0 else 0.0, 1.0 if i == 1 else 0.0]}
                   for i in reversed(range(count))]
        return httpx.Response(200, json={"data": records})

    settings.embed_base_url = settings.llm_base_url = "http://embed.test/v1"
    settings.embed_batch_size = 2
    backend = DirectBackend(settings, client=httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.embed_base_url))
    client = LLMClient(settings, conn, backend=backend)

    first = client.embed(["alpha", "beta"], task_id=7)
    second = client.embed(["alpha", "beta"], task_id=7)

    assert len(requests) == 1 and requests[0]["model"] == settings.embed_model
    assert urls == ["http://embed.test/v1/embeddings"]
    assert first == second == [[1.0, 0.0], [0.0, 1.0]]
    call = db.fetchone(conn, "SELECT step, backend, status FROM llm_calls WHERE step='embed'")
    assert tuple(call) == ("embed", "embed", "ok")
    assert db.fetchone(conn, "SELECT COUNT(*) c FROM embeddings")["c"] == 2


def run_all_concurrently(targets):
    """Start one thread per target, all released by a barrier, so the race is real."""
    barrier = threading.Barrier(len(targets))
    errors = []

    def wrap(fn):
        def worker():
            try:
                barrier.wait()
                fn()
            except Exception as exc:  # noqa: BLE001 - the test asserts on what was collected
                errors.append(exc)

        return worker

    threads = [threading.Thread(target=wrap(fn)) for fn in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def run_concurrently(target, n):
    return run_all_concurrently([target] * n)


def test_concurrent_completes_all_log_their_row(settings, conn, fake_backend):
    settings.llm_model_default = "qwen"
    backend = fake_backend(result={"ok": True})
    client = make_client(settings, conn, backend)

    errors = run_concurrently(
        lambda: client.complete(MSG, TINY, step="s", agent="a"), 4
    )

    assert errors == []
    rows = db.fetchall(conn, "SELECT status FROM llm_calls")
    assert len(rows) == 4 and all(r["status"] == "ok" for r in rows)


def test_estimate_tokens_is_monotonic(settings, conn, fake_backend):
    client = make_client(settings, conn, fake_backend(result={"ok": True}))
    assert client.estimate_tokens("a" * 400) == 100
    assert client.estimate_tokens("") == 1


def test_estimate_tokens_uses_configured_hangul_cjk_rate(settings, conn, fake_backend):
    settings.ko_tokens_per_char = 2.0
    client = make_client(settings, conn, fake_backend(result={"ok": True}))
    assert client.estimate_tokens("한") == 2
    assert client.estimate_tokens("字") == 2
    assert client.estimate_tokens("ᄀ") == 2
    assert client.estimate_tokens("abcd") == 1


def test_client_writes_wait_on_the_db_write_lock(settings, conn, fake_backend):
    """Another writer holding db.WRITE_LOCK blocks the client's llm_calls insert."""
    backend = fake_backend(result={"ok": True})
    client = make_client(settings, conn, backend)
    done = threading.Event()

    def call():
        client.complete(MSG, TINY, step="s", agent="a")
        done.set()

    worker = threading.Thread(target=call, daemon=True)
    with db.WRITE_LOCK:
        worker.start()
        assert not done.wait(0.3)  # it cannot log while we hold the lock
        assert db.fetchone(conn, "SELECT COUNT(*) AS n FROM llm_calls")["n"] == 0

    worker.join(5)
    assert done.is_set()
    assert db.fetchone(conn, "SELECT COUNT(*) AS n FROM llm_calls")["n"] == 1


def test_stream_complete_forwards_events_and_logs_the_call(settings, conn, fake_backend, monkeypatch):
    settings.llm_model_default = "qwen"

    def fake_stream_chat(settings_arg, messages, max_tokens, *, client=None, cancel=None):
        yield ("reasoning", "생각 중")
        yield ("content", "안녕")
        yield ("usage", {"prompt_tokens": 11, "completion_tokens": 4})

    monkeypatch.setattr("aioffice.llm.stream.stream_chat", fake_stream_chat)
    client = make_client(settings, conn, fake_backend(result={"ok": True}))

    events = list(client.stream_complete(MSG, step="slides.generate", agent="", max_tokens=999))

    assert ("reasoning", "생각 중") in events
    assert ("content", "안녕") in events
    # the client's own usage event (last) reflects the backend's real usage, not an estimate
    assert events[-1] == ("usage", {"prompt_tokens": 11, "completion_tokens": 4,
                                     "estimated": False, "reasoning_tokens": None})
    row = db.fetchone(conn, "SELECT * FROM llm_calls ORDER BY id DESC")
    assert row["step"] == "slides.generate" and row["model"] == "qwen" and row["status"] == "ok"
    assert row["prompt_tokens"] == 11 and row["completion_tokens"] == 4


def test_stream_complete_surfaces_api_reasoning_tokens_when_present(settings, conn, fake_backend, monkeypatch):
    def fake_stream_chat(settings_arg, messages, max_tokens, *, client=None, cancel=None):
        yield ("content", "안녕")
        yield ("usage", {"prompt_tokens": 11, "completion_tokens": 40,
                          "completion_tokens_details": {"reasoning_tokens": 33}})

    monkeypatch.setattr("aioffice.llm.stream.stream_chat", fake_stream_chat)
    client = make_client(settings, conn, fake_backend(result={"ok": True}))

    events = list(client.stream_complete(MSG, step="s", agent="a"))

    assert events[-1] == ("usage", {"prompt_tokens": 11, "completion_tokens": 40,
                                     "estimated": False, "reasoning_tokens": 33})


def test_stream_complete_does_not_yield_from_finally_on_early_close(settings, conn, fake_backend, monkeypatch):
    """A generator's `finally` must not `yield` -- stopping the generator early (.close()) would
    raise RuntimeError("generator ignored GeneratorExit") if it did."""
    def fake_stream_chat(settings_arg, messages, max_tokens, *, client=None, cancel=None):
        yield ("content", "one")
        yield ("content", "two")
        yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr("aioffice.llm.stream.stream_chat", fake_stream_chat)
    client = make_client(settings, conn, fake_backend(result={"ok": True}))

    gen = client.stream_complete(MSG, step="s", agent="a")
    next(gen)  # consume only the first event, then abandon the generator
    gen.close()  # must not raise


def test_stream_complete_estimates_completion_tokens_when_no_usage_arrives(settings, conn, fake_backend, monkeypatch):
    def fake_stream_chat(settings_arg, messages, max_tokens, *, client=None, cancel=None):
        yield ("content", "a" * 400)

    monkeypatch.setattr("aioffice.llm.stream.stream_chat", fake_stream_chat)
    client = make_client(settings, conn, fake_backend(result={"ok": True}))

    events = list(client.stream_complete(MSG, step="s", agent="a"))

    usage = events[-1][1]
    assert usage["estimated"] is True
    assert usage["completion_tokens"] == client.estimate_tokens("a" * 400)
    row = db.fetchone(conn, "SELECT completion_tokens FROM llm_calls ORDER BY id DESC")
    assert row["completion_tokens"] == usage["completion_tokens"]


def test_stream_complete_marks_status_cancelled_when_cancel_event_is_set(settings, conn, fake_backend, monkeypatch):
    def fake_stream_chat(settings_arg, messages, max_tokens, *, client=None, cancel=None):
        yield ("content", "일부만")

    monkeypatch.setattr("aioffice.llm.stream.stream_chat", fake_stream_chat)
    client = make_client(settings, conn, fake_backend(result={"ok": True}))
    cancel = threading.Event()
    cancel.set()

    list(client.stream_complete(MSG, step="s", agent="a", cancel=cancel))

    row = db.fetchone(conn, "SELECT status FROM llm_calls ORDER BY id DESC")
    assert row["status"] == "cancelled"


def test_client_logging_leaves_another_writer_on_the_connection_intact(settings, conn, fake_backend):
    """Both writers hold db.WRITE_LOCK through their commit, so no half-transaction lands."""
    settings.llm_model_default = "qwen"
    client = make_client(settings, conn, fake_backend(result={"ok": True}))

    def log_one():
        client.complete(MSG, TINY, step="s", agent="a")

    def insert_a_pair():
        # two rows that must land together: the client's commit must not split them
        with db.WRITE_LOCK:
            marker = f"T{threading.get_ident()}"
            db.insert(conn, "llm_calls", step="pair", backend="test", model="m",
                      status="ok", agent=marker)
            db.insert(conn, "llm_calls", step="pair", backend="test", model="m",
                      status="ok", agent=marker)
            conn.commit()

    errors = run_all_concurrently([log_one] * 4 + [insert_a_pair] * 4)

    assert errors == []
    assert db.fetchone(conn, "SELECT COUNT(*) AS n FROM llm_calls WHERE step='s'")["n"] == 4
    pairs = db.fetchall(conn, "SELECT agent, COUNT(*) AS n FROM llm_calls WHERE step='pair' GROUP BY agent")
    assert len(pairs) == 4 and all(r["n"] == 2 for r in pairs)
