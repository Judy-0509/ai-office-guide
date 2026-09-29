"""Tests for aioffice.dataplat.normalize: value coercion and period canonicalization."""
from __future__ import annotations

import pytest

from aioffice.dataplat.normalize import (
    clean_text,
    coarsen_period,
    normalize_period,
    normalize_value,
    period_range_end_sort,
)


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


# --- broker/tracker period spellings (real in-house data: "25Q3") ------------------------------


@pytest.mark.parametrize("text", ["25Q3", "'25Q3", "3Q25", "3Q2025", "2025 Q3", "2025-Q3",
                                    "Q3 2025", "Q3-2025", "Q3'25"])
def test_quarter_alt_spellings_all_normalize_to_2025q3(text):
    r = normalize_period(text)
    assert r.period == "2025Q3" and r.ok is True
    assert r.period_sort == normalize_period("2025Q3").period_sort


@pytest.mark.parametrize("text", ["FY25", "fy25", "FY2025"])
def test_fiscal_year_normalizes_to_plain_year(text):
    r = normalize_period(text)
    assert r.period == "2025" and r.ok is True


@pytest.mark.parametrize("text", ["2025.09", "2025/09"])
def test_month_alt_separators(text):
    r = normalize_period(text)
    assert r.period == "2025-09" and r.ok is True


def test_two_digit_year_is_2000_plus():
    assert normalize_period("25Q3").period == "2025Q3"
    assert normalize_period("99Q1").period == "2099Q1"  # still 2000+yy, no rollover guessing


@pytest.mark.parametrize("text", [
    "12345", "20255", "Q5 2025", "Q0 2025", "XY25", "25", "garbage", "2025Q5",
])
def test_new_quarter_patterns_have_no_false_positives_on_plain_numbers(text):
    # "1234" is a pre-existing, unrelated case (any bare 4-digit string is already a valid
    # year) -- not exercised here since it's not one of the new patterns added this round.
    r = normalize_period(text)
    assert r.ok is False, f"{text!r} should not parse as a period"


# --- range bounds: a coarser bound must cover every finer period within it ---------------------


def test_range_end_year_covers_every_quarter_month_week():
    end = period_range_end_sort("2025")
    assert end >= normalize_period("2025Q4").period_sort
    assert end >= normalize_period("2025-12").period_sort
    assert end >= normalize_period("2025W53").period_sort


def test_range_start_year_is_already_correct_lower_bound():
    # period_from doesn't need widening -- a coarser period's own sort already sits <= every
    # finer period within it (month/week always start >= 1, year's own sort uses 0).
    start = normalize_period("2025").period_sort
    assert start <= normalize_period("2025Q1").period_sort
    assert start <= normalize_period("2025-01").period_sort
    assert start <= normalize_period("2025W01").period_sort


def test_range_end_quarter_covers_its_months_but_not_the_next_quarter():
    end = period_range_end_sort("2025Q2")
    assert end >= normalize_period("2025-04").period_sort
    assert end >= normalize_period("2025-06").period_sort
    assert end < normalize_period("2025-07").period_sort  # next quarter must NOT be included


def test_range_end_month_and_week_are_unchanged_exact_bounds():
    assert period_range_end_sort("2025-09") == normalize_period("2025-09").period_sort
    assert period_range_end_sort("2025W20") == normalize_period("2025W20").period_sort


def test_range_end_unparseable_falls_back_to_its_own_text():
    assert period_range_end_sort("garbage") == "garbage"


# --- rollup: coarsening a period to year/quarter for `store.query_observations`'s rollup= -----


def test_coarsen_quarter_to_year():
    assert coarsen_period("2025Q3", "year") == ("2025", normalize_period("2025").period_sort)


def test_coarsen_month_to_year():
    assert coarsen_period("2025-09", "year") == ("2025", normalize_period("2025").period_sort)


def test_coarsen_month_to_quarter():
    assert coarsen_period("2025-09", "quarter") == ("2025Q3", normalize_period("2025Q3").period_sort)
    assert coarsen_period("2025-04", "quarter") == ("2025Q2", normalize_period("2025Q2").period_sort)


def test_coarsen_week_to_quarter_uses_iso_week_month():
    period, _sort = coarsen_period("2025W20", "quarter")
    assert period == "2025Q2"  # ISO week 20 of 2025 falls in May


def test_coarsen_week_to_year():
    period, _sort = coarsen_period("2025W20", "year")
    assert period == "2025"


def test_coarsen_already_coarsest_level_returns_none():
    assert coarsen_period("2025", "year") is None
    assert coarsen_period("2025Q3", "quarter") is None


def test_coarsen_unparseable_returns_none():
    assert coarsen_period("garbage", "year") is None


def test_coarsen_invalid_rollup_raises():
    with pytest.raises(ValueError):
        coarsen_period("2025-09", "month")
