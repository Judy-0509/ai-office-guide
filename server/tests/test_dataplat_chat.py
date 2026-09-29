"""Tests for aioffice.dataplat.chat: the LLM only fills a query spec (never numbers/SQL) --
covered here with a fake LLM backend (same FakeContentBackend pattern as the analyst tests):
a valid spec, name auto-correction, a clarification round-trip, and a bad explanation falling
back to the code-generated template sentence."""
from __future__ import annotations

import json

import pytest

from aioffice.config import Settings
from aioffice.dataplat import chat, store
from aioffice.llm.client import LLMClient


class FakeContentBackend:
    """Same shape as tests/conftest.py's FakeContentBackend (complete_json callers)."""

    def __init__(self, responses=None, model="fake-model"):
        self.responses = list(responses or [])
        self.model = model
        self.name = "direct"
        self.calls: list[dict] = []

    def model_for(self):
        return self.model

    def run_content(self, messages, model, timeout=None, max_tokens=None):
        self.calls.append({"messages": messages, "model": model})
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return content, {"prompt_tokens": 10, "completion_tokens": 20}


@pytest.fixture()
def ctx(tmp_path):
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    conn = store.connect(tmp_path / "dataplat.sqlite")
    store.upsert_dataset(conn, "shipments", "기관별 출하 전망", store.now_iso())
    rows = [
        {"metric": "출하량", "entity": "모델A", "region": "한국", "period": "2024Q1",
         "period_sort": "20240100", "source": "기관A", "value": 100.0, "unit": ""},
        {"metric": "출하량", "entity": "모델A", "region": "한국", "period": "2024Q2",
         "period_sort": "20240400", "source": "기관A", "value": 120.0, "unit": ""},
        {"metric": "출하량", "entity": "모델B", "region": "한국", "period": "2024Q1",
         "period_sort": "20240100", "source": "기관A", "value": 80.0, "unit": ""},
    ]
    store.write_load(conn, dataset="shipments", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(), finished_at=store.now_iso())

    def make(responses):
        backend = FakeContentBackend(responses)
        llm = LLMClient(settings, conn, backend=backend)
        return chat.ChatContext(conn=conn, llm=llm), backend

    return make


def test_valid_spec_returns_table_and_llm_sentence(ctx):
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "rows": ["entity"],
         "cols": ["period"], "chart": "line", "clarify": None},
        {"sentence": "모델A 출하량은 2024Q1에 100, 2024Q2에 120입니다."},
    ])
    result = chat.answer(ctx_obj, "모델A 출하량 분기별로 보여줘")
    assert result["answer"] == "모델A 출하량은 2024Q1에 100, 2024Q2에 120입니다."
    assert result["warnings"] == []
    assert result["table"]["columns"] == ["entity", "2024Q1", "2024Q2"]
    assert result["chart"]["type"] == "line"
    assert len(backend.calls) == 2


def test_entity_name_auto_corrects_within_threshold(ctx):
    # "모델 A" (with a space) is close enough to "모델A" to auto-correct, not clarify.
    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델 A"], "clarify": None},
        {"sentence": "요약입니다."},
    ])
    result = chat.answer(ctx_obj, "모델 A 얼마야")
    assert result["warnings"] != ["clarify"]
    assert result["spec"]["entities"] == ["모델 A"]  # spec itself unchanged, resolution is internal
    assert result["table"] is not None


def test_unknown_entity_triggers_clarification_with_candidates(ctx):
    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["존재하지않는대상"], "clarify": None},
    ])
    result = chat.answer(ctx_obj, "존재하지않는대상 출하량")
    assert result["warnings"] == ["clarify"]
    assert result["table"] is None
    assert "모델" in result["answer"]  # candidate list mentions a real entity


def test_llm_clarify_field_short_circuits_before_query(ctx):
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "clarify": "어떤 지표를 원하시나요?"},
    ])
    result = chat.answer(ctx_obj, "얼마야")
    assert result["answer"] == "어떤 지표를 원하시나요?"
    assert result["warnings"] == ["clarify"]
    assert len(backend.calls) == 1  # explain step never called


def test_bad_explanation_with_invented_number_falls_back_to_template(ctx):
    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "rows": ["entity"],
         "cols": ["period"], "clarify": None},
        {"sentence": "모델A 출하량은 9999로 급증했습니다."},  # 9999 is not in the table
    ])
    result = chat.answer(ctx_obj, "모델A 출하량 보여줘")
    assert result["warnings"] == ["explain_number_mismatch_used_template"]
    assert "9999" not in result["answer"]
    assert "100" in result["answer"] or "120" in result["answer"]  # template cites real values


def test_explain_backend_error_falls_back_to_template(ctx):
    from aioffice.llm import BackendError

    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "clarify": None},
        BackendError("연결 실패"),
        BackendError("연결 실패"),  # MAX_ATTEMPTS retries once more before LLMError
    ])
    result = chat.answer(ctx_obj, "모델A 출하량 보여줘")
    assert result["warnings"] == ["explain_failed_used_template"]
    assert result["table"] is not None


def test_unknown_dataset_triggers_clarification(ctx):
    ctx_obj, _backend = ctx([
        {"dataset": "존재하지않는데이터셋", "clarify": None},
    ])
    result = chat.answer(ctx_obj, "아무 질문")
    assert result["warnings"] == ["clarify"]
    assert "데이터셋" in result["answer"]


def test_spec_call_failure_returns_clarify_not_500(ctx):
    from aioffice.llm import BackendError

    ctx_obj, _backend = ctx([BackendError("연결 실패"), BackendError("연결 실패")])
    result = chat.answer(ctx_obj, "아무 질문")
    assert result["warnings"] == ["clarify"]
    assert result["spec"] is None


def test_no_rows_returns_no_data_message_without_llm_explain_call(ctx):
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"],
         "period_from": "20990000", "period_to": "20990000", "clarify": None},
    ])
    result = chat.answer(ctx_obj, "2099년 출하량")
    assert result["table"]["rows"] == []
    assert len(backend.calls) == 1  # explain skipped: nothing to explain
