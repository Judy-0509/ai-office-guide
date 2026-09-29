"""Value and period normalization shared by `dataplat.snapshot`.

Standard columns: dataset, metric, entity, region, period, period_sort, source, value, unit.
This module owns the two messy conversions a source row's text/number cells still need before
they're a clean observation: text -> float, and text -> one of the canonical period shapes.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, NamedTuple

STANDARD_COLUMNS = ("metric", "entity", "region", "period", "source", "value", "unit")

_NUM_STRIP_RE = re.compile(r"[,\s]")
_BLANK_TEXTS = {"", "-", "–", "—", "n.a.", "na", "n/a", "null", "nan", "none"}

_YEAR_RE = re.compile(r"^(\d{4})$")
_QUARTER_RE = re.compile(r"^(\d{4})[\-\s]?[Qq]([1-4])$")
_MONTH_RE = re.compile(r"^(\d{4})[\-./](\d{1,2})$")
_WEEK_RE = re.compile(r"^(\d{4})[\-\s]?[Ww](\d{1,2})$")


def clean_text(raw: Any) -> str:
    """Best-effort cell -> display text: strips, and drops a trailing ".0" sqlite/pandas
    sometimes attaches to a whole-number float that was really meant as text (an id column)."""
    if raw is None:
        return ""
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw).strip()


class ValueResult(NamedTuple):
    value: float | None
    unit: str | None  # "%" when the text carried a percent sign, else None
    status: str  # "ok" | "blank" | "non_numeric"


def normalize_value(raw: Any) -> ValueResult:
    """Cell text/number -> ValueResult. "1,234" / "12.5%" / "(3.2)" all parse; blanks and
    "n.a."-style markers are reported as "blank"; anything else unparseable is "non_numeric"."""
    if raw is None:
        return ValueResult(None, None, "blank")
    if isinstance(raw, bool):
        return ValueResult(None, None, "non_numeric")
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and raw != raw:  # NaN
            return ValueResult(None, None, "blank")
        return ValueResult(float(raw), None, "ok")

    text = str(raw).strip()
    if text.casefold() in _BLANK_TEXTS:
        return ValueResult(None, None, "blank")

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1].strip()
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    cleaned = _NUM_STRIP_RE.sub("", text)
    if not cleaned:
        return ValueResult(None, None, "blank")
    try:
        value = float(cleaned)
    except ValueError:
        return ValueResult(None, None, "non_numeric")
    if negative:
        value = -value
    return ValueResult(value, "%" if percent else None, "ok")


class PeriodResult(NamedTuple):
    period: str
    period_sort: str
    ok: bool


def _sort_key(year: int, month: int = 0, week: int = 0) -> str:
    # ponytail: cross-granularity ordering (year vs quarter vs month vs week) is a rough
    # chronological approximation, not exact -- fine for chart x-axis sorting within one
    # dataset, which sticks to one granularity in practice. Upgrade to real date ranges if
    # a dataset ever mixes granularities and the ordering matters.
    return f"{year:04d}{month:02d}{week:02d}"


def normalize_period(raw: Any) -> PeriodResult:
    """header/cell text -> one of YYYY, YYYYQn, YYYY-MM, YYYYWnn, plus a sortable
    `period_sort`. Unparseable input is kept as text with `ok=False` (caller counts it as a
    warning)."""
    if isinstance(raw, _dt.datetime):
        raw = raw.date()
    if isinstance(raw, _dt.date):
        return PeriodResult(f"{raw.year:04d}-{raw.month:02d}", _sort_key(raw.year, raw.month), True)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if float(raw).is_integer() and 1900 <= int(raw) <= 2200:
            year = int(raw)
            return PeriodResult(f"{year:04d}", _sort_key(year), True)
        text = str(raw)
    else:
        text = str(raw).strip() if raw is not None else ""

    text = text.strip()
    m = _YEAR_RE.match(text)
    if m:
        year = int(m.group(1))
        return PeriodResult(f"{year:04d}", _sort_key(year), True)
    m = _QUARTER_RE.match(text)
    if m:
        year, q = int(m.group(1)), int(m.group(2))
        return PeriodResult(f"{year:04d}Q{q}", _sort_key(year, (q - 1) * 3 + 1), True)
    m = _MONTH_RE.match(text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return PeriodResult(f"{year:04d}-{month:02d}", _sort_key(year, month), True)
    m = _WEEK_RE.match(text)
    if m:
        year, week = int(m.group(1)), int(m.group(2))
        if 1 <= week <= 53:
            return PeriodResult(f"{year:04d}W{week:02d}", _sort_key(year, 0, week), True)

    return PeriodResult(text, text, False)
