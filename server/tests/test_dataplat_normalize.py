"""Tests for aioffice.dataplat.normalize: value coercion and period canonicalization."""
from __future__ import annotations

from aioffice.dataplat.normalize import clean_text, normalize_period, normalize_value


def test_value_thousands_separator():
    assert normalize_value("1,234") == (1234.0, None, "ok")


def test_value_percent_keeps_unit():
    assert normalize_value("12.5%") == (12.5, "%", "ok")


def test_value_parens_is_negative():
    assert normalize_value("(3.2)") == (-3.2, None, "ok")


def test_value_native_number_passthrough():
    assert normalize_value(42) == (42.0, None, "ok")
    assert normalize_value(3.5) == (3.5, None, "ok")


def test_value_blank_markers_dropped_and_counted():
    for raw in [None, "", "-", "n.a.", "N/A", "null"]:
        result = normalize_value(raw)
        assert result.status == "blank" and result.value is None


def test_value_non_numeric_text():
    result = normalize_value("모델A")
    assert result.status == "non_numeric" and result.value is None


def test_value_nan_float_is_blank():
    result = normalize_value(float("nan"))
    assert result.status == "blank"


def test_period_year():
    assert normalize_period("2024") == ("2024", "20240000", True)


def test_period_quarter():
    r = normalize_period("2024Q1")
    assert r.period == "2024Q1" and r.ok is True
    r4 = normalize_period("2024Q4")
    assert r4.period_sort > r.period_sort


def test_period_month():
    assert normalize_period("2024-03") == ("2024-03", "20240300", True)


def test_period_month_single_digit():
    assert normalize_period("2024-3").period == "2024-03"


def test_period_week():
    r = normalize_period("2024W05")
    assert r.period == "2024W05" and r.ok is True


def test_period_unparseable_kept_as_text():
    r = normalize_period("작년 상반기")
    assert r.ok is False and r.period == "작년 상반기" and r.period_sort == "작년 상반기"


def test_period_sort_orders_within_year():
    q1 = normalize_period("2024Q1").period_sort
    q2 = normalize_period("2024Q2").period_sort
    m6 = normalize_period("2024-06").period_sort
    assert q1 < q2 < m6


def test_period_from_date_object():
    import datetime

    r = normalize_period(datetime.date(2024, 3, 15))
    assert r.period == "2024-03" and r.ok is True


def test_clean_text_strips_whole_number_float_suffix():
    assert clean_text(2024.0) == "2024"
    assert clean_text("  hi  ") == "hi"
    assert clean_text(None) == ""
