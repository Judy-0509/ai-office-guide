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
    result = chat.answer(ctx_obj, "모델A 출하량 분기별로 보여줘", explain_mode="llm")
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
    result = chat.answer(ctx_obj, "모델A 출하량 보여줘", explain_mode="llm")
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
    result = chat.answer(ctx_obj, "출하량 전체 다 보여줘", explain_mode="llm")
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
    result = chat.answer(ctx_obj, "모델A 출하량 보여줘", explain_mode="llm")
    assert len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("explain_number_mismatch_used_template")
    assert "9999" in result["warnings"][0]  # debug: which number got rejected
    assert "9999" not in result["answer"]
    assert "100" in result["answer"] or "120" in result["answer"]  # template cites real values


def test_explain_backend_error_falls_back_to_template(ctx):
    from aioffice.llm import BackendError

    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "clarify": None},
        BackendError("연결 실패"),
        BackendError("연결 실패"),  # MAX_ATTEMPTS retries once more before LLMError
    ])
    result = chat.answer(ctx_obj, "모델A 출하량 보여줘", explain_mode="llm")
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
                "period_sort": "20260700", "source": "", "value": 200.0, "unit": ""},
               {"metric": "출하량전망", "entity": "모델C", "region": "", "period": "2026Q3",
                "period_sort": "20260700", "source": "", "value": 300.0, "unit": ""}]
    rows_08 = [{"metric": "출하량전망", "entity": "모델A", "region": "", "period": "2026Q3",
                "period_sort": "20260700", "source": "", "value": 120.0, "unit": ""},
               {"metric": "출하량전망", "entity": "모델B", "region": "", "period": "2026Q3",
                "period_sort": "20260700", "source": "", "value": 250.0, "unit": ""},
               {"metric": "출하량전망", "entity": "모델C", "region": "", "period": "2026Q3",
                "period_sort": "20260700", "source": "", "value": 200.0, "unit": ""}]
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


# --- version_diff routing (spec "version": "diff") -----------------------------------------------


def test_spec_version_diff_compares_all_entities_ranked_by_abs_diff(versioned_ctx):
    # A: 100->120 (+20), B: 200->250 (+50), C: 300->200 (-100) -- the LLM enumerated all three
    # (it doesn't know in advance which one changed most); diff mode must compare all of them,
    # not silently filter down to entities[0] like history mode does.
    ctx_obj, backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "entities": ["모델A", "모델B", "모델C"],
         "version": "diff", "rows": ["entity"], "clarify": None},
        {"sentence": "모델C가 300에서 200으로 가장 많이 줄었습니다."},
    ])
    result = chat.answer(ctx_obj, "지난 버전 대비 가장 많이 바뀐 모델은?", explain_mode="llm")
    assert result["table"]["columns"] == ["entity", "old", "new", "diff", "pct"]
    assert [r[0] for r in result["table"]["rows"]] == ["모델C", "모델B", "모델A"]
    assert result["answer"] == "모델C가 300에서 200으로 가장 많이 줄었습니다."
    assert result["warnings"] == []
    assert result["chart"]["type"] == "bar"


def test_spec_version_diff_defaults_to_entity_when_no_rows_given(versioned_ctx):
    ctx_obj, _backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "version": "diff", "clarify": None},
        {"sentence": "요약입니다."},
    ])
    result = chat.answer(ctx_obj, "지난 버전 대비 뭐가 바뀌었어?")
    assert result["table"]["columns"][0] == "entity"
    assert len(result["table"]["rows"]) == 3


def test_spec_version_diff_single_named_entity_filters_to_just_it(versioned_ctx):
    ctx_obj, _backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "entities": ["모델A"],
         "version": "diff", "clarify": None},
        {"sentence": "모델A는 100에서 120으로 늘었습니다."},
    ])
    result = chat.answer(ctx_obj, "모델A 지난 버전 대비 얼마나 바뀌었어?")
    assert result["table"]["rows"] == [["모델A", 100.0, 120.0, 20.0, 20.0]]


def test_spec_version_diff_explicit_from_to_labels(versioned_ctx):
    ctx_obj, _backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "version": "diff",
         "from": "2026-07", "to": "2026-08", "rows": ["entity"], "clarify": None},
        {"sentence": "요약입니다."},
    ])
    result = chat.answer(ctx_obj, "7월 버전과 8월 버전 비교해줘")
    assert result["spec"]["from"] == "2026-07" and result["spec"]["to"] == "2026-08"
    assert len(result["table"]["rows"]) == 3


def test_spec_version_diff_unknown_from_label_clarifies(versioned_ctx):
    ctx_obj, backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "version": "diff",
         "from": "2020-01", "clarify": None},
    ])
    result = chat.answer(ctx_obj, "2020년 버전이랑 비교해줘")
    assert result["warnings"] == ["clarify"]
    assert "2026-07" in result["answer"] and "2026-08" in result["answer"]
    assert len(backend.calls) == 1  # explain never ran


def test_spec_version_diff_on_unversioned_dataset_clarifies(ctx):
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "version": "diff", "clarify": None},
    ])
    result = chat.answer(ctx_obj, "지난 버전 대비 뭐가 바뀌었어?")
    assert result["warnings"] == ["clarify"]
    assert len(backend.calls) == 1


def test_spec_version_diff_same_from_and_to_is_a_degenerate_zero_diff(versioned_ctx):
    # explicitly comparing a label against itself is not an error -- version_diff just diffs
    # it against itself, so every diff/pct comes out 0 rather than blocking with a clarify.
    ctx_obj, _backend = versioned_ctx([
        {"dataset": "forecast", "metrics": ["출하량전망"], "version": "diff",
         "to": "2026-07", "from": "2026-07", "rows": ["entity"], "clarify": None},
        {"sentence": "변화가 없습니다."},
    ])
    result = chat.answer(ctx_obj, "7월 버전을 7월 버전과 비교해줘")
    assert result["table"] is not None
    assert all(r[3] == 0.0 for r in result["table"]["rows"])


def test_spec_version_diff_no_previous_version_clarifies(tmp_path):
    # only one label has EVER existed for this dataset -- nothing to diff against.
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    conn = store.connect(tmp_path / "dataplat.sqlite")
    store.upsert_dataset(conn, "forecast", "출하 전망", store.now_iso())
    rows = [{"metric": "출하량전망", "entity": "모델A", "region": "", "period": "2026Q3",
             "period_sort": "20260700", "source": "", "value": 100.0, "unit": ""}]
    vs = store.compute_version_sort(conn, "forecast", "2026-07", store.now_iso())
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(),
                      finished_at=store.now_iso(), version_label="2026-07", version_sort=vs)

    backend = FakeContentBackend([
        {"dataset": "forecast", "metrics": ["출하량전망"], "version": "diff", "clarify": None},
    ])
    llm = LLMClient(settings, conn, backend=backend)
    ctx_obj = chat.ChatContext(conn=conn, llm=llm)
    result = chat.answer(ctx_obj, "지난 버전 대비 뭐가 바뀌었어?")
    assert result["warnings"] == ["clarify"]
    assert len(backend.calls) == 1


# --- number-check false negatives (trailing .0, percent-vs-pct, thousands separators) ----------
# Confirmed live (3 real OpenRouter runs): every rejected number was a model faithfully writing
# a table cell's Python str() form ("100.0", "25.0%") against the table's own no-decimal
# rendering ("100", "25") -- never a genuinely invented number. These tests pin that fix.


def test_trailing_zero_decimal_is_accepted_against_whole_number_cell():
    table = {"columns": ["v"], "rows": [[100.0]]}
    assert chat._first_rejected_number("값은 100.0입니다.", table) is None


def test_trailing_zero_decimal_with_sign_is_accepted():
    table = {"columns": ["v"], "rows": [[-30.0]]}
    assert chat._first_rejected_number("차이는 -30.0입니다.", table) is None


def test_percent_with_trailing_zero_is_accepted_against_pct_column():
    table = {"columns": ["pct"], "rows": [[25.0]]}
    assert chat._first_rejected_number("25.0% 증가했습니다.", table) is None


def test_thousands_separator_with_trailing_zero_is_accepted():
    table = {"columns": ["v"], "rows": [[1234.0]]}
    assert chat._first_rejected_number("값은 1,234.0입니다.", table) is None


def test_genuinely_absent_number_is_still_rejected():
    # e.g. a computed sum the model invented, never present in the table -- must keep failing.
    table = {"columns": ["v"], "rows": [[100.0], [120.0]]}
    assert chat._first_rejected_number("합계는 220입니다.", table) == "220"


def test_first_rejected_number_reports_the_exact_offending_text():
    table = {"columns": ["v"], "rows": [[100.0]]}
    assert chat._first_rejected_number("9999가 답입니다.", table) == "9999"


def test_strip_trailing_zero_decimal_helper():
    assert chat._strip_trailing_zero_decimal("100.0") == "100"
    assert chat._strip_trailing_zero_decimal("25.50") == "25.5"
    assert chat._strip_trailing_zero_decimal("0.0") == "0"
    assert chat._strip_trailing_zero_decimal("100") == "100"  # no decimal point -- untouched
    assert chat._strip_trailing_zero_decimal("10.10") == "10.1"


def test_live_bug_repro_llm_sentence_with_trailing_zeros_now_passes(ctx):
    # the exact shape of sentence the live diagnostic captured -- must NOT fall back anymore.
    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "rows": ["entity"],
         "cols": ["period"], "clarify": None},
        {"sentence": "모델A는 2024Q1에 100.0, 2024Q2에 120.0을 기록했습니다."},
    ])
    result = chat.answer(ctx_obj, "모델A 출하량 보여줘", explain_mode="llm")
    assert result["answer"] == "모델A는 2024Q1에 100.0, 2024Q2에 120.0을 기록했습니다."
    assert result["warnings"] == []


# --- type-aware fallback templates ---------------------------------------------------------------


def test_template_diff_top_two_with_from_to_and_no_unit():
    rows = [["모델B", 200.0, 250.0, 50.0, 25.0], ["모델C", 150.0, 120.0, -30.0, -20.0],
            ["모델A", 100.0, 100.0, 0.0, 0.0]]
    sentence = chat._template_diff(rows, ["entity"], "2026-07", "2026-08", "")
    assert sentence == "가장 많이 바뀐 것은 모델B(+50, +25%)이고, 다음은 모델C(-30, -20%)입니다. (2026-07 → 2026-08)"


def test_template_diff_includes_unit():
    rows = [["모델B", 200.0, 250.0, 50.0, 25.0]]
    sentence = chat._template_diff(rows, ["entity"], None, None, "대")
    assert sentence == "가장 많이 바뀐 것은 모델B(+50대, +25%)입니다."


def test_template_diff_single_row_has_no_second_clause():
    rows = [["모델A", 100.0, 100.0, 0.0, 0.0]]
    sentence = chat._template_diff(rows, ["entity"], None, None, "")
    assert "다음은" not in sentence
    assert sentence == "가장 많이 바뀐 것은 모델A(+0, +0%)입니다."


def test_template_diff_null_pct_shown_as_na():
    rows = [["모델A", None, 50.0, 50.0, None]]  # added key: no baseline -> pct is null
    sentence = chat._template_diff(rows, ["entity"], None, None, "")
    assert "N/A" in sentence


def test_template_diff_empty_rows():
    assert chat._template_diff([], ["entity"], None, None, "") == "버전 사이에 변화가 없습니다."


def test_template_history_with_change_and_unit():
    rows = [[1, "2026-07", 100.0], [2, "2026-08", 120.0]]
    sentence = chat._template_history(rows, "모델A", "대")
    assert sentence == "모델A: 2026-07 100대 → 2026-08 120대 (+20대, +20%)."


def test_template_history_single_point_has_no_diff_clause():
    rows = [[1, "2026-07", 100.0]]
    sentence = chat._template_history(rows, "모델A", "")
    assert sentence == "모델A: 2026-07 100 → 2026-07 100."


def test_template_history_zero_baseline_omits_pct():
    rows = [[1, "2026-07", 0.0], [2, "2026-08", 50.0]]
    sentence = chat._template_history(rows, "모델A", "")
    assert "+50" in sentence and "%" not in sentence


def test_template_history_no_entity_name_omits_prefix():
    rows = [[1, "2026-07", 100.0], [2, "2026-08", 120.0]]
    sentence = chat._template_history(rows, "", "")
    assert not sentence.startswith(":")
    assert sentence.startswith("2026-07")


def test_template_wide_names_max_and_min_cells():
    table = {"columns": ["entity", "2024Q1", "2024Q2"],
             "rows": [["모델A", 80.0, 85.0], ["모델B", 200.0, 250.0]]}
    sentence = chat._template_wide(table, ["entity"], "")
    assert sentence == "최댓값: 모델B 2024Q2 250, 최솟값: 모델A 2024Q1 80."


def test_template_wide_includes_unit():
    table = {"columns": ["entity", "2024Q1"], "rows": [["모델A", 80.0]]}
    sentence = chat._template_wide(table, ["entity"], "대")
    assert "80대" in sentence


def test_template_wide_no_numeric_cells_falls_back_to_count():
    table = {"columns": ["entity"], "rows": [["모델A"], ["모델B"]]}
    assert chat._template_wide(table, ["entity"], "") == "조건에 맞는 데이터가 2건 있습니다."


# --- diff/history routing on an unversioned dataset (code-level safety net, not just prompt) ---


# --- rollup/agg spec fields, missing-data warning, timings/path, LLM timeout -------------------


def test_rollup_and_agg_spec_fields_group_periods_before_pivoting(ctx):
    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"],
         "rollup": "year", "agg": "sum", "rows": ["entity"], "cols": ["period"], "clarify": None},
    ])
    result = chat.answer(ctx_obj, "연간 합계 좀")
    assert result["table"]["columns"] == ["entity", "2024"]
    assert result["table"]["rows"] == [["모델A", 220.0]]  # 100 (Q1) + 120 (Q2)


def test_rollup_omitted_leaves_periods_untouched(ctx):
    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"],
         "rows": ["entity"], "cols": ["period"], "clarify": None},
    ])
    result = chat.answer(ctx_obj, "표로 보여줘")
    assert result["table"]["columns"] == ["entity", "2024Q1", "2024Q2"]


def test_missing_entity_warning_when_one_of_several_has_no_rows_in_period(ctx):
    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A", "모델B"],
         "period_from": "2024Q2", "period_to": "2024Q2", "rows": ["entity"], "cols": ["period"],
         "clarify": None},
    ])
    result = chat.answer(ctx_obj, "이 기간 표로 보여줘")
    # 모델B has no 2024Q2 row at all (only 모델A does) -- must warn, not silently show a smaller table
    assert "'모델B'은(는) 해당 기간 데이터가 없습니다" in result["warnings"]


def test_missing_entity_warning_absent_when_all_requested_entities_have_rows(ctx):
    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"],
         "rows": ["entity"], "cols": ["period"], "clarify": None},
    ])
    result = chat.answer(ctx_obj, "표로 보여줘")
    assert result["warnings"] == []


def test_default_explain_mode_is_template_skips_llm_explain_call(ctx):
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "rows": ["entity"],
         "cols": ["period"], "clarify": None},
    ])
    result = chat.answer(ctx_obj, "모델A 출하량 보여줘")  # explain_mode not given -> "template"
    assert len(backend.calls) == 1  # spec only -- no LLM explain call
    assert result["timings"]["explain_seconds"] == 0.0
    assert result["answer"]  # the template sentence, not empty


def test_response_carries_path_and_timings_on_llm_spec_path(ctx):
    ctx_obj, _backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "rows": ["entity"],
         "cols": ["period"], "clarify": None},
    ])
    result = chat.answer(ctx_obj, "이 데이터 좀 봐줘 아무거나")
    assert result["path"] == "llm"
    assert set(result["timings"]) == {"spec_seconds", "explain_seconds", "query_seconds"}
    assert result["timings"]["spec_seconds"] >= 0.0


def test_clarify_responses_also_carry_path_and_timings(ctx):
    ctx_obj, _backend = ctx([{"dataset": "존재하지않는데이터셋", "clarify": None}])
    result = chat.answer(ctx_obj, "아무 질문")
    assert result["path"] == "llm"
    assert result["timings"]["explain_seconds"] == 0.0
    assert result["timings"]["query_seconds"] == 0.0
    assert result["timings"]["spec_seconds"] >= 0.0


def test_spec_call_timeout_returns_friendly_busy_message(ctx):
    from aioffice.llm import BackendError

    ctx_obj, backend = ctx([
        BackendError("direct timeout: ReadTimeout"),
        BackendError("direct timeout: ReadTimeout"),  # MAX_ATTEMPTS retries once more
    ])
    result = chat.answer(ctx_obj, "이 데이터 좀 보여줘 아무거나")
    assert "붐벼서" in result["answer"]
    assert result["spec"] is None
    assert result["path"] == "llm"
    assert len(backend.calls) == 2


# --- spec cache (chat_spec_cache): repeated single-turn question skips the LLM entirely --------


def test_repeated_message_hits_cache_and_skips_the_llm_spec_call(ctx):
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "rows": ["entity"],
         "cols": ["period"], "clarify": None},
    ])
    first = chat.answer(ctx_obj, "이 데이터 좀 봐줘 아무거나")
    assert first["path"] == "llm" and len(backend.calls) == 1

    second = chat.answer(ctx_obj, "이 데이터 좀 봐줘 아무거나")  # same message, same catalog
    assert second["path"] == "cache"
    assert len(backend.calls) == 1  # no new LLM call
    assert second["table"] == first["table"]


def test_cache_misses_after_catalog_version_changes(ctx):
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "rows": ["entity"],
         "cols": ["period"], "clarify": None},
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "rows": ["entity"],
         "cols": ["period"], "clarify": None},
    ])
    first = chat.answer(ctx_obj, "이 데이터 좀 봐줘 아무거나")
    assert first["path"] == "llm"

    new_rows = [{"metric": "출하량", "entity": "모델A", "region": "", "period": "2024Q3",
                 "period_sort": "20240300", "source": "", "value": 130.0, "unit": ""}]
    store.write_load(ctx_obj.conn, dataset="shipments", content_hash_value=store.content_hash(new_rows),
                      observations=new_rows, report={}, started_at=store.now_iso(),
                      finished_at=store.now_iso())  # a new snapshot changes catalog_version

    second = chat.answer(ctx_obj, "이 데이터 좀 봐줘 아무거나")
    assert second["path"] == "llm"  # cache scoped to the old catalog_version -> miss
    assert len(backend.calls) == 2


def test_multiturn_history_bypasses_rule_path_and_cache(ctx):
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "rows": ["entity"],
         "cols": ["period"], "clarify": None},
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "rows": ["entity"],
         "cols": ["period"], "clarify": None},
    ])
    turn = [{"role": "user", "content": "이전 대화"}]
    first = chat.answer(ctx_obj, "모델A 지난 버전 대비 얼마나 바뀌었어", history=turn)
    second = chat.answer(ctx_obj, "모델A 지난 버전 대비 얼마나 바뀌었어", history=turn)
    assert first["path"] == "llm" and second["path"] == "llm"  # never "rule" or "cache" with history
    assert len(backend.calls) == 2


# --- rule-based fast path (no LLM): ~25 Korean question table, incl. fallback cases -------------

_FAST_PATH_CATALOG = [
    {"name": "shipments", "metrics": ["출하량"], "entities": ["모델A", "모델B", "모델C"],
     "regions": ["한국", "미국"], "sources": ["기관A", "기관B"]},
    {"name": "prices", "metrics": ["가격지수"], "entities": ["제품X", "제품Y"],
     "regions": [], "sources": []},
]

_FAST_PATH_CASES = [
    # trend (entity x period, full range unless a period is named, chart=line) -- NOT version
    # history: 추이/흐름/분기별/월별/연도별 mean "across periods", never "across snapshots".
    ("모델A 출하량 추이 보여줘",
     {"dataset": "shipments", "version": "latest", "rollup": None, "entities": ["모델A"],
      "chart": "line"}),
    ("모델B 흐름 좀 보여줘",
     {"dataset": "shipments", "version": "latest", "rollup": None, "entities": ["모델B"],
      "chart": "line"}),
    ("모델A 분기별 추이", {"dataset": "shipments", "version": "latest", "rollup": "quarter",
                       "entities": ["모델A"], "period_from": None, "chart": "line"}),
    ("모델C 연도별 추이 보여줘", {"dataset": "shipments", "version": "latest", "rollup": "year",
                          "entities": ["모델C"], "chart": "line"}),
    ("모델B 월별 흐름 어때", {"dataset": "shipments", "version": "latest", "rollup": None,
                        "entities": ["모델B"], "chart": "line"}),  # no monthly rollup level exists
    ("제품Y 가격지수 추이 알려줘",
     {"dataset": "prices", "version": "latest", "rollup": None, "entities": ["제품Y"],
      "chart": "line"}),
    ("모델A 지난 버전 대비 얼마나 바뀌었어",
     {"dataset": "shipments", "version": "diff", "entities": ["모델A"]}),
    ("모델B 이전 버전과 비교하면?", {"dataset": "shipments", "version": "diff", "entities": ["모델B"]}),
    ("제품X 지난 버전 대비 상향됐나요", {"dataset": "prices", "version": "diff", "entities": ["제품X"]}),
    ("모델A 모델B 출하량 합계 알려줘",
     {"dataset": "shipments", "version": "latest", "rollup": None, "agg": "sum",
      "entities": ["모델A", "모델B"]}),
    ("2024년 모델A 합계는",
     {"dataset": "shipments", "version": "latest", "rollup": "year", "agg": "sum",
      "period_from": "2024", "period_to": "2024", "entities": ["모델A"]}),
    ("모델A 모델B 비교해줘",
     {"dataset": "shipments", "version": "latest", "entities": ["모델A", "모델B"],
      "rows": ["entity"], "cols": ["period"]}),
    ("모델A vs 모델C 출하량", {"dataset": "shipments", "version": "latest", "entities": ["모델A", "모델C"]}),
    ("모델A 2024Q1 출하량 얼마야",
     {"dataset": "shipments", "version": "latest", "rows": [], "cols": [], "entities": ["모델A"],
      "period_from": "2024Q1", "period_to": "2024Q1"}),
    ("모델B 2024년 몇 개야",
     {"dataset": "shipments", "version": "latest", "entities": ["모델B"],
      "period_from": "2024", "period_to": "2024"}),
    ("모델A 2024년부터 2025년까지 합계 알려줘",
     {"dataset": "shipments", "version": "latest", "rollup": "year", "agg": "sum",
      "period_from": "2024", "period_to": "2025", "entities": ["모델A"]}),
    # single-value, two-digit-year/quarter-first/Korean-native period spellings, incl. particles
    # attached directly to the period token (Round 7 bug fix -- these used to fall through).
    ("모델A 25Q3 얼마야?",
     {"dataset": "shipments", "version": "latest", "rows": [], "cols": [], "entities": ["모델A"],
      "period_from": "2025Q3", "period_to": "2025Q3"}),
    ("모델B 3Q25 얼마야",
     {"dataset": "shipments", "version": "latest", "entities": ["모델B"],
      "period_from": "2025Q3", "period_to": "2025Q3"}),
    ("모델A 2025년 3분기에 얼마였어",
     {"dataset": "shipments", "version": "latest", "entities": ["모델A"],
      "period_from": "2025Q3", "period_to": "2025Q3"}),
    ("제품Y 25Q3은 얼마인가요",
     {"dataset": "prices", "version": "latest", "entities": ["제품Y"],
      "period_from": "2025Q3", "period_to": "2025Q3"}),
    ("모델C 25Q3 몇 개야", {"dataset": "shipments", "version": "latest", "entities": ["모델C"],
                        "period_from": "2025Q3", "period_to": "2025Q3"}),
    # fallback cases -- must fall back to the LLM (too little signal, or ambiguous)
    ("아무 질문입니다", None),
    ("출하량 좀 보여줘", None),  # metric only, no entity/dataset signal
    ("지난 버전 대비 뭐가 바뀌었어", None),  # diff keyword but no entity/period/dataset signal
    ("얼마야", None),
    ("모델A 얼마야", None),  # entity found but no period -- single-value needs both
    ("제품Y 예측 알려줘", None),  # entity found but no recognized intent keyword
    ("모델A와 모델C 출하량 최근 몇 년간 추이", None),  # 2 entities -- trend/single-value need exactly 1
    ("모델A 출하량 어떻게 되나요", None),  # entity found but no intent keyword
    ("모델A 모델B 모델C 제품X 다 보여줘", None),  # names span 2 datasets -- ambiguous
    ("최근 몇 개월간 어땠어", None),  # no entity/region/source signal at all -- dataset undetermined
    ("제품X 알려줘", None),  # entity found but no intent keyword
]


@pytest.mark.parametrize("message,expected", _FAST_PATH_CASES)
def test_rule_based_spec_question_table(message, expected):
    spec = chat._rule_based_spec(message, _FAST_PATH_CATALOG)
    if expected is None:
        assert spec is None, f"{message!r} should fall back to the LLM but got {spec!r}"
        return
    assert spec is not None, f"{message!r} should be handled by the fast path"
    for key, value in expected.items():
        assert spec[key] == value, f"{message!r}: spec[{key!r}] = {spec[key]!r}, expected {value!r}"


def test_rule_based_spec_matches_name_before_parenthesis(ctx):
    # a catalog entity like "모델A (자사)" should match on just "모델A" in the message.
    catalog = [{"name": "shipments", "metrics": ["출하량"], "entities": ["모델A (자사)", "모델B (경쟁사)"],
                "regions": [], "sources": []}]
    spec = chat._rule_based_spec("모델A 출하량 추이", catalog)
    assert spec is not None and spec["entities"] == ["모델A (자사)"]


def test_rule_based_spec_used_end_to_end_by_answer(ctx):
    # no responses queued at all -- if the LLM were called, FakeContentBackend.responses.pop(0)
    # would raise IndexError, so this also proves the rule path made zero LLM calls.
    ctx_obj, backend = ctx([])
    result = chat.answer(ctx_obj, "모델A 분기별 추이 보여줘")
    assert result["path"] == "rule"
    assert len(backend.calls) == 0
    # trend over the FULL available period range (no period named) -- both 2024Q1 and 2024Q2,
    # not a version/snapshot history table.
    assert result["table"]["columns"] == ["entity", "2024Q1", "2024Q2"]
    assert result["table"]["rows"] == [["모델A", 100.0, 120.0]]
    assert result["chart"]["type"] == "line"


def test_rule_based_spec_single_value_with_particle_attached_period(ctx):
    # "25Q3은" -- particle attached directly to the compact period code, no space.
    ctx_obj, backend = ctx([])
    result = chat.answer(ctx_obj, "모델A 2024Q1은 얼마야")
    assert result["path"] == "rule"
    assert len(backend.calls) == 0
    assert result["table"]["rows"] == [["모델A", 100.0]]


def test_diff_on_unversioned_dataset_with_single_named_entity_reinterpreted_as_history(ctx):
    # the model picked "diff" for a single-entity "compared to previous" question on a dataset
    # that has no version_column at all -- diff is meaningless there, so this must silently
    # become a history answer instead of an unhelpful clarify (confirmed live: this exact
    # misrouting produced a 202s clarify for "모델A ... 이전 스냅샷 대비 어떻게 바뀌었어?").
    ctx_obj, backend = ctx([
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "version": "diff",
         "period_to": "2024Q1", "clarify": None},
        {"sentence": "모델A 출하량은 100입니다."},
    ])
    result = chat.answer(ctx_obj, "모델A 출하량이 이전 스냅샷 대비 어떻게 바뀌었어?")
    assert result["warnings"] != ["clarify"]
    assert result["table"]["columns"] == ["load_id", "label", "value"]
