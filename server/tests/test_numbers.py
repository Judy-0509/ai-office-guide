"""Tests for aioffice.numbers.normalize_number -- shared by analyst.steps (claim number
checking) and dataplat.chat (explanation number checking)."""
from __future__ import annotations

from aioffice.numbers import normalize_number


def test_strips_commas():
    assert normalize_number("1,234") == "1234"


def test_strips_percent_and_dollar():
    assert normalize_number("12.5%") == "12.5"
    assert normalize_number("$1,234") == "1234"


def test_strips_whitespace():
    assert normalize_number(" 1 234 ") == "1234"


def test_none_is_empty_string():
    assert normalize_number(None) == ""


def test_numeric_input_is_stringified():
    assert normalize_number(1234) == "1234"
    assert normalize_number(12.5) == "12.5"


def test_plain_text_passes_through_unchanged():
    assert normalize_number("모델A") == "모델A"
