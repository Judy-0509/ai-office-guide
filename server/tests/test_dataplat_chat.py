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


def test_hallucinated_optional_source_is_dropped_with_warning_not_clarified(ctx):
    # Regression for the live-check Q1 bug: the LLM added a source filter that doesn't exist
    # for this dataset/message ("기관Z" is never mentioned) -- must be dropped, not block the
    # whole query with a clarification.
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "sources": ["기관Z"],
         "rows": ["entity"], "cols": ["period"], "clarify": None},
        {"sentence": "요약입니다."},
    ])
    result = chat.answer(ctx_obj, "모델A 출하량 보여줘")
    assert result["warnings"] == ["존재하지 않는 조건 '기관Z'는 제외했습니다"]
    assert result["table"] is not None and result["table"]["rows"]
    assert len(backend.calls) == 2  # spec + explain both ran -- query was not blocked


def test_hallucinated_optional_region_is_dropped_with_warning(ctx):
    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "regions": ["없는지역"],
         "clarify": None},
        {"sentence": "요약입니다."},
    ])
    result = chat.answer(ctx_obj, "모델A 출하량 보여줘")
    assert "존재하지 않는 조건 '없는지역'는 제외했습니다" in result["warnings"]
    assert result["table"] is not None


def test_entity_the_model_added_on_its_own_is_dropped_not_clarified(ctx):
    # The user asked for "everything", not a specific model -- the LLM enumerating a
    # non-existent entity here must be dropped (optional), not turn into a clarification.
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A", "없는모델"],
         "rows": ["entity"], "cols": ["period"], "clarify": None},
        {"sentence": "요약입니다."},
    ])
    result = chat.answer(ctx_obj, "출하량 전체 다 보여줘")
    assert result["warnings"] == ["존재하지 않는 조건 '없는모델'는 제외했습니다"]
    assert result["table"] is not None
    assert len(backend.calls) == 2


def test_entity_the_user_explicitly_named_still_clarifies_even_mixed_with_optional_one(ctx):
    # "없는모델" is explicitly in the user's message -> required -> clarify wins overall, even
    # though a valid entity was also present.
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A", "없는모델"],
         "clarify": None},
    ])
    result = chat.answer(ctx_obj, "모델A랑 없는모델 출하량 비교해줘")
    assert result["warnings"] == ["clarify"]
    assert result["table"] is None
    assert len(backend.calls) == 1  # explain never ran -- blocked before the query


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


# --- version_column (spec "version": a version_label) -------------------------------------------


@pytest.fixture()
def versioned_ctx(tmp_path):
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    conn = store.connect(tmp_path / "dataplat.sqlite")
    store.upsert_dataset(conn, "forecast", "출하 전망", store.now_iso())
    rows_07 = [{"metric": "출하량전망", "entity": "모델A", "region": "", "period": "2026Q3",
                "period_sort": "20260700", "source": "", "value": 100.0, "unit": ""},
               {"metric": "출하량전망", "entity": "모델B", "region": "", "period": "2026Q3",
                "period_sort": "20260700", "source": "", "value": 200.0, "unit": ""}]
    rows_08 = [{"metric": "출하량전망", "entity": "모델A", "region": "", "period": "2026Q3",
                "period_sort": "20260700", "source": "", "value": 120.0, "unit": ""},
               {"metric": "출하량전망", "entity": "모델B", "region": "", "period": "2026Q3",
                "period_sort": "20260700", "source": "", "value": 250.0, "unit": ""}]
    vs07 = store.compute_version_sort(conn, "forecast", "2026-07", store.now_iso())
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows_07),
                      observations=rows_07, report={}, started_at=store.now_iso(),
                      finished_at=store.now_iso(), version_label="2026-07", version_sort=vs07)
    vs08 = store.compute_version_sort(conn, "forecast", "2026-08", store.now_iso())
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows_08),
                      observations=rows_08, report={}, started_at=store.now_iso(),
                      finished_at=store.now_iso(), version_label="2026-08", version_sort=vs08)

    def make(responses):
        backend = FakeContentBackend(responses)
        llm = LLMClient(settings, conn, backend=backend)
        return chat.ChatContext(conn=conn, llm=llm), backend

    return make


def test_spec_version_label_resolves_to_that_versions_data(versioned_ctx):
    ctx_obj, _backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "entities": ["모델A"],
         "version": "2026-07", "clarify": None},
        {"sentence": "모델A 출하량전망은 100입니다."},
    ])
    result = chat.answer(ctx_obj, "모델A 7월 버전 출하량전망 보여줘")
    assert result["table"]["rows"] == [["모델A", 100.0]]


def test_spec_version_defaults_to_latest_when_omitted(versioned_ctx):
    ctx_obj, _backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "entities": ["모델A"], "clarify": None},
        {"sentence": "모델A 출하량전망은 120입니다."},
    ])
    result = chat.answer(ctx_obj, "모델A 출하량전망 보여줘")
    assert result["table"]["rows"] == [["모델A", 120.0]]


def test_spec_unknown_version_label_clarifies_with_candidates(versioned_ctx):
    ctx_obj, backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "entities": ["모델A"],
         "version": "2099-01", "clarify": None},
    ])
    result = chat.answer(ctx_obj, "모델A 2099년 버전 출하량전망")
    assert result["warnings"] == ["clarify"]
    assert "2026-07" in result["answer"] and "2026-08" in result["answer"]
    assert len(backend.calls) == 1  # explain never ran


def test_spec_version_label_auto_corrects_near_miss(versioned_ctx):
    ctx_obj, _backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "entities": ["모델A"],
         "version": "2026-07 ", "clarify": None},  # trailing space -- not an exact match
        {"sentence": "요약입니다."},
    ])
    result = chat.answer(ctx_obj, "모델A 7월 버전 출하량전망")
    assert result["warnings"] != ["clarify"]
    assert result["table"]["rows"] == [["모델A", 100.0]]


def test_spec_version_history_shows_one_point_per_label(versioned_ctx):
    ctx_obj, backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "entities": ["모델A"],
         "version": "history", "period_to": "2026Q3", "clarify": None},
        {"sentence": "모델A 출하량전망은 100에서 120으로 늘었습니다."},
    ])
    result = chat.answer(ctx_obj, "모델A 출하량전망 버전별로 어떻게 바뀌었어?")
    assert result["table"]["columns"] == ["load_id", "label", "value"]
    assert [r[1] for r in result["table"]["rows"]] == ["2026-07", "2026-08"]
    assert [r[2] for r in result["table"]["rows"]] == [100.0, 120.0]


def test_spec_version_history_with_multiple_entities_warns_and_uses_first(versioned_ctx):
    # history() tracks one series -- no cross-entity ranking/diff query exists yet ("which
    # model changed the most" can't be answered this way). Asking for several entities must
    # warn and fall back to the first, never silently answer for only one of them.
    ctx_obj, backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "entities": ["모델A", "모델B"],
         "version": "history", "period_to": "2026Q3", "clarify": None},
        {"sentence": "모델A 출하량전망은 100에서 120으로 늘었습니다."},
    ])
    result = chat.answer(ctx_obj, "지난 버전 대비 가장 많이 바뀐 모델은?")
    assert any("모델A" in w and "하나의 대상만" in w for w in result["warnings"])
    assert [r[1] for r in result["table"]["rows"]] == ["2026-07", "2026-08"]
    assert [r[2] for r in result["table"]["rows"]] == [100.0, 120.0]  # 모델A only, not 모델B
