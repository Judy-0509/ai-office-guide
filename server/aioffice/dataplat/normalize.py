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
# Broker/tracker quarter spellings: 2-digit year ("25Q3", "'25Q3"), quarter-first ("3Q25",
# "3Q2025", "Q3 2025", "Q3-2025", "Q3'25"). Each entry is (pattern, year_first) -- year_first
# says whether group(1) is the year or the quarter digit.
_QUARTER_ALT_RES: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"^'?(\d{2})[\-\s]?[Qq]([1-4])$"), True),          # 25Q3, '25Q3, 25-Q3
    (re.compile(r"^[Qq]([1-4])[\-\s']?(\d{4})$"), False),           # Q3 2025, Q3-2025, Q32025
    (re.compile(r"^[Qq]([1-4])'(\d{2})$"), False),                   # Q3'25
    (re.compile(r"^([1-4])[Qq](\d{2}|\d{4})$"), False),              # 3Q25, 3Q2025
)
# Fiscal year: "FY25", "FY2025" -- treated as a plain year.
_FISCAL_YEAR_RE = re.compile(r"^FY'?(\d{2}|\d{4})$", re.IGNORECASE)


def _expand_2digit_year(text: str) -> int:
    year = int(text)
    return year if len(text) == 4 else 2000 + year


def _match_quarter_alt(text: str) -> tuple[int, int] | None:
    """(year, quarter) from one of the non-canonical quarter spellings above, or None."""
    for pattern, year_first in _QUARTER_ALT_RES:
        m = pattern.match(text)
        if not m:
            continue
        first, second = m.group(1), m.group(2)
        year_text, q_text = (first, second) if year_first else (second, first)
        return _expand_2digit_year(year_text), int(q_text)
    return None


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
    alt = _match_quarter_alt(text)
    if alt:
        year, q = alt
        return PeriodResult(f"{year:04d}Q{q}", _sort_key(year, (q - 1) * 3 + 1), True)
    m = _FISCAL_YEAR_RE.match(text)
    if m:
        year = _expand_2digit_year(m.group(1))
        return PeriodResult(f"{year:04d}", _sort_key(year), True)
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


_CANON_YEAR_RE = re.compile(r"^(\d{4})$")
_CANON_QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")
_CANON_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_CANON_WEEK_RE = re.compile(r"^(\d{4})W(\d{2})$")


def period_range_end_sort(raw: Any) -> str:
    """The `period_sort` to use as an upper range bound for `raw` -- unlike the lower bound
    (a period's own `period_sort` already sits at or before every finer period within it,
    since finer units always have month/week >= 1), the upper bound must be widened to cover
    the coarser period's LAST finer unit: year "2025" -> end of December (any week), quarter
    "2025Q2" -> end of its last month (any week). Month/week bounds are already exact (no
    coarser sub-unit within them to cover)."""
    result = normalize_period(raw)
    if not result.ok:
        return result.period_sort
    m = _CANON_YEAR_RE.match(result.period)
    if m:
        return f"{int(m.group(1)):04d}9999"
    m = _CANON_QUARTER_RE.match(result.period)
    if m:
        year, q = int(m.group(1)), int(m.group(2))
        return f"{year:04d}{q * 3:02d}99"
    return result.period_sort


ROLLUP_LEVELS = ("year", "quarter")


def coarsen_period(period: str, rollup: str) -> tuple[str, str] | None:
    """(new canonical period, new period_sort) after rolling `period` up to `rollup`
    ("year"|"quarter") -- or None if `period` doesn't parse, or is already at (or coarser
    than) that level, so there's nothing to roll up. Week -> quarter uses the ISO week's
    first day to pick a month (an approximation, like the cross-granularity sort already is --
    see `_sort_key`)."""
    if rollup not in ROLLUP_LEVELS:
        raise ValueError(f"rollup은 {ROLLUP_LEVELS} 중 하나여야 합니다: {rollup!r}")

    m = _CANON_YEAR_RE.match(period)
    if m:
        return None  # already the coarsest level

    m = _CANON_QUARTER_RE.match(period)
    if m:
        year = int(m.group(1))
        return (f"{year:04d}", _sort_key(year)) if rollup == "year" else None

    m = _CANON_MONTH_RE.match(period)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if rollup == "year":
            return f"{year:04d}", _sort_key(year)
        q = (month - 1) // 3 + 1
        return f"{year:04d}Q{q}", _sort_key(year, (q - 1) * 3 + 1)

    m = _CANON_WEEK_RE.match(period)
    if m:
        year, week = int(m.group(1)), int(m.group(2))
        if rollup == "year":
            return f"{year:04d}", _sort_key(year)
        try:
            month = _dt.date.fromisocalendar(year, week, 1).month
        except ValueError:
            return None
        q = (month - 1) // 3 + 1
        return f"{year:04d}Q{q}", _sort_key(year, (q - 1) * 3 + 1)

    return None  # unparseable -- caller keeps the original period unchanged
