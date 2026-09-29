from __future__ import annotations

import json

import pytest

from aioffice.llm.schemas import SchemaError
from aioffice.slides import prompts, spec
from aioffice.slides.server import DEFAULT_DECK_PATH, DEFAULT_TOPIC_PATH


def _table_slide(title="T", kicker="01 · 예시"):
    return {
        "layout": "table", "kicker": kicker, "title": title,
        "table": {"type": "table", "title": "표", "columns": ["a"], "rows": [["값"]]},
    }


def _summary_slide():
    return {
        "layout": "summary", "kicker": "01 · 예시", "title": "제목",
        "judgment": {"headline": "헤드라인", "highlight": "헤드", "detail": "설명"},
        "evidence": [{"keyword": "근거", "text": "근거 내용"}],
        "implication": {"text": "시사점", "action": "다음 액션"},
        "panels": [{"type": "bar", "title": "차트", "caption": "설명", "unit": "건",
                    "categories": ["A"], "values": [1]}],
    }


def _chart_slide():
    return {
        "layout": "chart", "kicker": "01 · 예시", "title": "제목",
        "chart": {"type": "dots", "title": "차트", "caption": "설명", "unit": "건",
                  "items": [{"label": "A", "value": 1}]},
        "notes": [{"keyword": "쟁점", "text": "노트 내용"}],
    }


# --- structural validation -------------------------------------------------------------------
def test_validate_slide_accepts_each_layout():
    assert spec.validate_slide(_table_slide())["layout"] == "table"
    assert spec.validate_slide(_summary_slide())["layout"] == "summary"
    assert spec.validate_slide(_chart_slide())["layout"] == "chart"


def test_validate_slide_rejects_unknown_layout():
    with pytest.raises(SchemaError, match="레이아웃"):
        spec.validate_slide({"layout": "unknown", "kicker": "", "title": ""})


def test_validate_slide_rejects_missing_required_field():
    slide = _table_slide()
    del slide["title"]
    with pytest.raises(SchemaError):
        spec.validate_slide(slide)


def test_validate_panel_rejects_unknown_panel_type():
    with pytest.raises(SchemaError, match="패널"):
        spec.validate_panel({"type": "pie", "title": "x"})


def test_validate_slide_rejects_a_table_layout_panel_of_the_wrong_type():
    slide = _table_slide()
    slide["table"] = {"type": "bar", "title": "x", "caption": "c", "unit": "u",
                       "categories": [], "values": []}
    with pytest.raises(SchemaError):
        spec.validate_slide(slide)


def test_validate_slide_rejects_a_chart_layout_panel_that_is_a_table():
    slide = _chart_slide()
    slide["chart"] = {"type": "table", "title": "x", "columns": [], "rows": []}
    with pytest.raises(SchemaError):
        spec.validate_slide(slide)


def test_coerce_and_validate_op_rejects_unknown_op():
    with pytest.raises(SchemaError, match="op"):
        spec.coerce_and_validate_op({"op": "frobnicate", "index": 0})


def test_coerce_and_validate_op_rejects_non_integer_index():
    with pytest.raises(SchemaError, match="index"):
        spec.coerce_and_validate_op({"op": "delete", "index": "not a number"})


def test_coerce_and_validate_op_wraps_slide_errors_in_korean():
    with pytest.raises(SchemaError, match="슬라이드 검증 실패"):
        spec.coerce_and_validate_op({"op": "insert", "index": 0, "slide": {"layout": "nope"}})


def test_coerce_and_validate_op_delete_needs_no_slide():
    op = spec.coerce_and_validate_op({"op": "delete", "index": 1})
    assert op == {"op": "delete", "index": 1}


# --- length warnings ---------------------------------------------------------------------
def test_check_flags_title_length_overflow():
    slide = spec.validate_slide(_table_slide(title="가" * 40))
    warnings = spec.check(slide, {"reports": []})
    assert any("제목 40자 > 34자" in w for w in warnings)


def test_check_flags_kicker_length_overflow():
    slide = spec.validate_slide(_table_slide(kicker="가" * 30))
    warnings = spec.check(slide, {"reports": []})
    assert any("꼭지 30자 > 28자" in w for w in warnings)


def test_check_flags_evidence_field_length_overflow():
    slide = _summary_slide()
    slide["evidence"][0]["keyword"] = "너무너무너무긴키워드"
    slide = spec.validate_slide(slide)
    warnings = spec.check(slide, {"reports": []})
    assert any("근거1 키워드" in w for w in warnings)


def test_check_flags_out_of_range_evidence_count():
    slide = _summary_slide()
    slide["evidence"] = []
    slide = spec.validate_slide(slide)
    warnings = spec.check(slide, {"reports": []})
    assert any("근거 0개" in w for w in warnings)


def test_check_does_not_flag_a_slide_within_all_limits():
    slide = spec.validate_slide(_table_slide())
    assert spec.check(slide, {"reports": []}) == []


# --- unverified-number warnings --------------------------------------------------------------
def test_check_no_false_positive_on_formatted_numbers_and_dates():
    sources = {"reports": [{"date": "2026-09-21", "claims": ["가격이 1,999달러로 상승", "18% 증가"]}]}
    slide = spec.validate_slide({
        "layout": "table", "kicker": "01", "title": "제목",
        "table": {"type": "table", "title": "표", "columns": ["a", "b", "c"],
                  "rows": [["1,999", "18%", "9/21"]]},
    })
    assert spec.check(slide, sources) == []


def test_check_flags_a_genuinely_unverified_number():
    sources = {"reports": [{"date": "2026-09-21", "claims": ["가격이 1,999달러로 상승"]}]}
    slide = spec.validate_slide({
        "layout": "table", "kicker": "01", "title": "제목",
        "table": {"type": "table", "title": "표", "columns": ["a"], "rows": [["500"]]},
    })
    warnings = spec.check(slide, sources)
    assert any("확인 필요 숫자: 500" in w for w in warnings)


def test_check_ignores_numbers_in_the_kicker_running_index():
    sources = {"reports": []}
    slide = spec.validate_slide(_table_slide(kicker="99 · 예시"))
    warnings = spec.check(slide, sources)
    assert not any("99" in w for w in warnings)


def test_check_treats_stats_as_verified_sources():
    sources = {"reports": [], "stats": {"report_count": 42}}
    slide = spec.validate_slide(_table_slide(title="총 42건"))
    assert spec.check(slide, sources) == []


def test_check_flags_unverified_chart_values():
    slide = spec.validate_slide(_summary_slide())
    warnings = spec.check(slide, {"reports": []})
    assert any("확인 필요 숫자: 1" in w for w in warnings)


# --- apply_ops ------------------------------------------------------------------------------
def _deck(n=0):
    return {"title": "t", "footer": "가상 데이터 · 견본",
            "slides": [spec.validate_slide(_table_slide(f"S{i}")) for i in range(n)]}


def test_apply_ops_insert_replace_delete():
    deck = _deck(2)
    ops = [
        {"op": "insert", "index": 1, "slide": spec.validate_slide(_table_slide("NEW"))},
        {"op": "replace", "index": 0, "slide": spec.validate_slide(_table_slide("REPLACED"))},
        {"op": "delete", "index": 2},
    ]
    new_deck, warnings = spec.apply_ops(deck, ops)
    assert [s["title"] for s in new_deck["slides"]] == ["REPLACED", "NEW"]
    assert warnings == []


def test_apply_ops_bad_index_insert_clamps_replace_and_delete_are_ignored():
    deck = _deck(1)
    ops = [
        {"op": "insert", "index": 99, "slide": spec.validate_slide(_table_slide("END"))},
        {"op": "replace", "index": 50, "slide": spec.validate_slide(_table_slide("X"))},
        {"op": "delete", "index": -1},
    ]
    new_deck, warnings = spec.apply_ops(deck, ops)
    assert len(new_deck["slides"]) == 2  # replace/delete were no-ops
    assert new_deck["slides"][-1]["title"] == "END"
    assert len(warnings) == 3


def test_apply_op_insert_at_negative_index_clamps_to_zero():
    deck = _deck(1)
    new_deck, warnings = spec.apply_op(deck, {"op": "insert", "index": -5,
                                               "slide": spec.validate_slide(_table_slide("FIRST"))})
    assert new_deck["slides"][0]["title"] == "FIRST"
    assert warnings


# --- sample data consistency (deck_memory.json must match topic_memory.json's real stats) ---
def test_sample_stats_match_topic_file():
    topic = json.loads(DEFAULT_TOPIC_PATH.read_text(encoding="utf-8"))
    stats = prompts.compute_stats(topic)
    assert stats["report_count"] == 11
    assert stats["institution_count"] == 8
    assert stats["stance_counts"] == {"부정": 6, "중립": 3, "긍정": 2}


def test_sample_deck_validates_and_has_no_content_warnings():
    topic = json.loads(DEFAULT_TOPIC_PATH.read_text(encoding="utf-8"))
    deck = json.loads(DEFAULT_DECK_PATH.read_text(encoding="utf-8"))
    stats = prompts.compute_stats(topic)
    sources = prompts.sources_for_check(topic, stats)
    for slide in deck["slides"]:
        validated = spec.validate_slide(slide)
        assert spec.check(validated, sources) == []
