"""Deck JSON spec: the single source of truth for the web preview, edits, and PPTX export.

Two-tier validation:
  1. Structural: `validate_slide` / `validate_panel` / `coerce_and_validate_op` reuse the
     `aioffice.llm.schemas` JSON-schema subset. A structural failure means the op is not a
     usable slide at all -- the caller (stream.OpParser) drops the line.
  2. Content: `check(slide, sources)` returns Korean warnings (length overflow, unverified
     numbers). These never block rendering -- they are shown as badges/lists in the UI.
"""

from __future__ import annotations

import re
from typing import Any

from ..llm import schemas
from ..llm.schemas import SchemaError

# --- design tokens (the ONE place both the web preview and the PPTX exporter read) ---------
INK = "#151515"
BODY = "#30343b"
MUTED = "#676b72"
BORDER = "#d4d4d4"
PANEL_BG = "#f4f4f4"
GRAY_BARS = ["#30343b", "#676b72", "#a8abb0", "#e1e2e4"]
ACCENT = "#1a56db"
ACCENT_TINT = "#eaf1fd"
FONT = "Malgun Gothic"

TOKENS: dict[str, Any] = {
    "ink": INK, "body": BODY, "muted": MUTED, "border": BORDER, "panel": PANEL_BG,
    "gray_bars": GRAY_BARS, "accent": ACCENT, "accent_tint": ACCENT_TINT, "font": FONT,
}

# --- panel schemas (a Panel is a chart or a table; the "type" field discriminates) ----------
_STR = {"type": "string"}
_NUM = {"type": "number"}

BAR_PANEL = {
    "type": "object",
    "required": ["type", "title", "caption", "unit", "categories", "values"],
    "properties": {
        "type": {"type": "string", "enum": ["bar"]},
        "title": _STR, "caption": _STR, "unit": _STR,
        "categories": {"type": "array", "items": _STR},
        "values": {"type": "array", "items": _NUM},
        "forecast": {"type": "array", "items": {"type": "boolean"}},
        "highlight": {"type": "integer"},
    },
}

STACKED_PART = {
    "type": "object", "required": ["name", "value"],
    "properties": {"name": _STR, "value": _NUM},
}
STACKED_ROW = {
    "type": "object", "required": ["label", "parts"],
    "properties": {"label": _STR, "parts": {"type": "array", "items": STACKED_PART}},
}
STACKED_PANEL = {
    "type": "object",
    "required": ["type", "title", "caption", "rows"],
    "properties": {
        "type": {"type": "string", "enum": ["stacked"]},
        "title": _STR, "caption": _STR,
        "rows": {"type": "array", "items": STACKED_ROW},
        "highlight": _STR,
    },
}

DOTS_ITEM = {
    "type": "object", "required": ["label", "value"],
    "properties": {"label": _STR, "value": _NUM},
}
DOTS_PANEL = {
    "type": "object",
    "required": ["type", "title", "caption", "unit", "items"],
    "properties": {
        "type": {"type": "string", "enum": ["dots"]},
        "title": _STR, "caption": _STR, "unit": _STR,
        "items": {"type": "array", "items": DOTS_ITEM},
        "highlight": _STR,
    },
}

TABLE_PANEL = {
    "type": "object",
    "required": ["type", "title", "columns", "rows"],
    "properties": {
        "type": {"type": "string", "enum": ["table"]},
        "title": _STR,
        "columns": {"type": "array", "items": _STR},
        "rows": {"type": "array"},  # rows of cells; cells may be str or number, unchecked
        "highlight_rows": {"type": "array", "items": {"type": "integer"}},
    },
}

PANEL_SCHEMAS: dict[str, dict] = {
    "bar": BAR_PANEL, "stacked": STACKED_PANEL, "dots": DOTS_PANEL, "table": TABLE_PANEL,
}
CHART_PANEL_TYPES = {"bar", "stacked", "dots"}

# --- slide schemas per layout ----------------------------------------------------------------
COMMON_PROPS = {
    "layout": {"type": "string", "enum": ["summary", "table", "chart"]},
    "kicker": _STR, "title": _STR, "subtitle": _STR, "source": _STR,
}

EVIDENCE_ITEM = {
    "type": "object", "required": ["keyword", "text"],
    "properties": {"keyword": _STR, "text": _STR},
}

SUMMARY_SCHEMA = {
    "type": "object",
    "required": ["layout", "kicker", "title", "judgment", "evidence", "implication", "panels"],
    "properties": {
        **COMMON_PROPS,
        "judgment": {
            "type": "object", "required": ["headline", "highlight", "detail"],
            "properties": {"headline": _STR, "highlight": _STR, "detail": _STR},
        },
        "evidence": {"type": "array", "items": EVIDENCE_ITEM},
        "implication": {
            "type": "object", "required": ["text", "action"],
            "properties": {"text": _STR, "action": _STR},
        },
        "panels": {"type": "array"},  # items are a Panel union, validated by validate_panel
    },
}

TABLE_SCHEMA = {
    "type": "object",
    "required": ["layout", "kicker", "title", "table"],
    "properties": {**COMMON_PROPS, "table": {"type": "object"}, "note": _STR},
}

CHART_SCHEMA = {
    "type": "object",
    "required": ["layout", "kicker", "title", "chart"],
    "properties": {
        **COMMON_PROPS, "chart": {"type": "object"},
        "notes": {"type": "array", "items": EVIDENCE_ITEM},
    },
}

SLIDE_SCHEMAS: dict[str, dict] = {
    "summary": SUMMARY_SCHEMA, "table": TABLE_SCHEMA, "chart": CHART_SCHEMA,
}

# --- structural validation --------------------------------------------------------------------
def validate_panel(panel: Any, *, allowed_types: set[str] | None = None) -> dict:
    if not isinstance(panel, dict):
        raise SchemaError("패널이 JSON 객체가 아닙니다")
    ptype = panel.get("type")
    schema = PANEL_SCHEMAS.get(ptype)
    if schema is None or (allowed_types is not None and ptype not in allowed_types):
        raise SchemaError(f"알 수 없는 패널 타입: {ptype!r}")
    panel = schemas.coerce(panel, schema)
    schemas.validate(panel, schema)
    return panel


def validate_slide(slide: Any) -> dict:
    if not isinstance(slide, dict):
        raise SchemaError("슬라이드가 JSON 객체가 아닙니다")
    layout = slide.get("layout")
    schema = SLIDE_SCHEMAS.get(layout)
    if schema is None:
        raise SchemaError(f"알 수 없는 레이아웃: {layout!r}")
    slide = schemas.coerce(slide, schema)
    schemas.validate(slide, schema)
    if layout == "summary":
        slide["panels"] = [validate_panel(p) for p in slide.get("panels") or []]
    elif layout == "table":
        slide["table"] = validate_panel(slide.get("table"), allowed_types={"table"})
    elif layout == "chart":
        slide["chart"] = validate_panel(slide.get("chart"), allowed_types=CHART_PANEL_TYPES)
    return slide


OP_TYPES = {"insert", "replace", "delete"}


def coerce_and_validate_op(raw: Any) -> dict:
    """Validate one parsed JSON-Lines op. Raises SchemaError with a Korean reason."""
    if not isinstance(raw, dict):
        raise SchemaError("op이 JSON 객체가 아닙니다")
    op = raw.get("op")
    if op not in OP_TYPES:
        raise SchemaError(f"알 수 없는 op: {op!r}")
    index = schemas.coerce(raw.get("index"), {"type": "integer"})
    if isinstance(index, bool) or not isinstance(index, int):
        raise SchemaError(f"index가 정수가 아닙니다: {raw.get('index')!r}")
    result: dict = {"op": op, "index": index}
    if op in ("insert", "replace"):
        try:
            result["slide"] = validate_slide(raw.get("slide"))
        except SchemaError as exc:
            raise SchemaError(f"슬라이드 검증 실패: {exc}") from exc
    return result


# --- apply_ops -----------------------------------------------------------------------------
def apply_op(deck: dict, op: dict) -> tuple[dict, list[str]]:
    """Apply one op. Out-of-range insert indices are clamped; replace/delete are ignored."""
    slides = list(deck.get("slides", []))
    warnings: list[str] = []
    kind, index = op["op"], op["index"]
    if kind == "insert":
        clamped = max(0, min(index, len(slides)))
        if clamped != index:
            warnings.append(f"삽입 위치 {index} 범위를 벗어나 {clamped}로 보정됨")
        slides.insert(clamped, op["slide"])
    elif kind == "replace":
        if 0 <= index < len(slides):
            slides[index] = op["slide"]
        else:
            warnings.append(f"교체 위치 {index}가 범위를 벗어나 무시됨")
    elif kind == "delete":
        if 0 <= index < len(slides):
            del slides[index]
        else:
            warnings.append(f"삭제 위치 {index}가 범위를 벗어나 무시됨")
    return {**deck, "slides": slides}, warnings


def apply_ops(deck: dict, ops: list[dict]) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    for op in ops:
        deck, w = apply_op(deck, op)
        warnings.extend(w)
    return deck, warnings


# --- content check: length overflow + unverified numbers -----------------------------------
# No leading sign: a "-" is almost always a date/range separator here ("2026-09-21", "12~25"),
# not a minus sign, and treating it as one would misparse date digits as negative numbers.
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _iter_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_strings(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield str(obj)


def extract_numbers(obj: Any) -> set[float]:
    """Every number literally present in obj (native values, or substrings of any string),
    normalized ("1,999" -> 1999, "18%" -> 18, "$8" -> 8)."""
    numbers: set[float] = set()
    for text in _iter_strings(obj):
        for match in _NUM_RE.finditer(text):
            token = match.group().replace(",", "")
            try:
                numbers.add(float(token))
            except ValueError:
                continue
    return numbers


def _isclose_in(n: float, allowed: set[float]) -> bool:
    return any(abs(n - a) < 1e-9 for a in allowed)


def _check_numbers_in_text(text: str, allowed: set[float], warnings: list[str], label: str) -> None:
    for n in extract_numbers(text or ""):
        if not _isclose_in(n, allowed):
            token = int(n) if n.is_integer() else n
            warnings.append(f"확인 필요 숫자: {token} ({label})")


def _check_panel_numbers(panel: dict, allowed: set[float], warnings: list[str]) -> None:
    ptype = panel.get("type")
    label = panel.get("title") or "패널"
    if ptype == "bar":
        for v in panel.get("values") or []:
            if not _isclose_in(float(v), allowed):
                token = int(v) if float(v).is_integer() else v
                warnings.append(f"확인 필요 숫자: {token} ({label})")
    elif ptype == "dots":
        for item in panel.get("items") or []:
            v = item.get("value")
            if v is not None and not _isclose_in(float(v), allowed):
                token = int(v) if float(v).is_integer() else v
                warnings.append(f"확인 필요 숫자: {token} ({label})")
    elif ptype == "stacked":
        for row in panel.get("rows") or []:
            for part in row.get("parts") or []:
                v = part.get("value")
                if v is not None and not _isclose_in(float(v), allowed):
                    token = int(v) if float(v).is_integer() else v
                    warnings.append(f"확인 필요 숫자: {token} ({label})")
    elif ptype == "table":
        for row in panel.get("rows") or []:
            for cell in row:
                for n in extract_numbers(cell):
                    if not _isclose_in(n, allowed):
                        token = int(n) if n.is_integer() else n
                        warnings.append(f"확인 필요 숫자: {token} ({label})")
    _check_numbers_in_text(panel.get("caption") or "", allowed, warnings, f"{label} 캡션")


def _len_warn(warnings: list[str], label: str, value: str, limit: int) -> None:
    if value and len(value) > limit:
        warnings.append(f"{label} {len(value)}자 > {limit}자")


def check(slide: dict, sources: Any) -> list[str]:
    """Korean warnings for `slide`: length overflow and unverified numbers. Never raises,
    never blocks rendering. `sources` should carry everything a number may legitimately come
    from -- the topic's SOURCES reports AND the computed STATS (see prompts.compute_stats)."""
    warnings: list[str] = []
    allowed = extract_numbers(sources)
    layout = slide.get("layout")

    _len_warn(warnings, "꼭지", slide.get("kicker") or "", 28)
    _len_warn(warnings, "제목", slide.get("title") or "", 34)
    _len_warn(warnings, "부제목", slide.get("subtitle") or "", 60)
    _len_warn(warnings, "출처", slide.get("source") or "", 90)
    # kicker carries the running slide index ("01 · ...") -- never number-checked.
    for label, text in (("제목", slide.get("title")), ("부제목", slide.get("subtitle")),
                         ("출처", slide.get("source"))):
        _check_numbers_in_text(text or "", allowed, warnings, label)

    if layout == "summary":
        judgment = slide.get("judgment") or {}
        _len_warn(warnings, "판단 헤드라인", judgment.get("headline") or "", 30)
        _len_warn(warnings, "판단 설명", judgment.get("detail") or "", 50)
        _check_numbers_in_text(judgment.get("headline") or "", allowed, warnings, "판단 헤드라인")
        _check_numbers_in_text(judgment.get("detail") or "", allowed, warnings, "판단 설명")

        evidence = slide.get("evidence") or []
        if not (1 <= len(evidence) <= 3):
            warnings.append(f"근거 {len(evidence)}개 (권장 1~3개)")
        for i, item in enumerate(evidence, 1):
            _len_warn(warnings, f"근거{i} 키워드", item.get("keyword") or "", 6)
            _len_warn(warnings, f"근거{i} 본문", item.get("text") or "", 45)
            _check_numbers_in_text(item.get("text") or "", allowed, warnings, f"근거{i}")

        implication = slide.get("implication") or {}
        _len_warn(warnings, "시사점", implication.get("text") or "", 45)
        _len_warn(warnings, "시사점 액션", implication.get("action") or "", 40)
        _check_numbers_in_text(implication.get("text") or "", allowed, warnings, "시사점")
        _check_numbers_in_text(implication.get("action") or "", allowed, warnings, "시사점 액션")

        panels = slide.get("panels") or []
        if not (1 <= len(panels) <= 3):
            warnings.append(f"패널 {len(panels)}개 (권장 1~3개)")
        for panel in panels:
            _check_panel_numbers(panel, allowed, warnings)

    elif layout == "table":
        if slide.get("note"):
            _len_warn(warnings, "노트", slide["note"], 80)
            _check_numbers_in_text(slide["note"], allowed, warnings, "노트")
        table = slide.get("table") or {}
        _check_panel_numbers(table, allowed, warnings)

    elif layout == "chart":
        chart = slide.get("chart") or {}
        _check_panel_numbers(chart, allowed, warnings)
        notes = slide.get("notes") or []
        if len(notes) > 4:
            warnings.append(f"노트 {len(notes)}개 (최대 4개)")
        for i, item in enumerate(notes, 1):
            _len_warn(warnings, f"노트{i} 키워드", item.get("keyword") or "", 6)
            _len_warn(warnings, f"노트{i} 본문", item.get("text") or "", 45)
            _check_numbers_in_text(item.get("text") or "", allowed, warnings, f"노트{i}")

    return warnings
