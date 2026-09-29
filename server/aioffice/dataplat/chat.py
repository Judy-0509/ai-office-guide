"""dataplat chatbot. The LLM never writes numbers or SQL -- it only fills a query form from a
narrowed catalog (`spec_messages`); our own code runs the query against `dataplat.sqlite`; the
explanation sentence is checked number-by-number against the query result (reusing
`aioffice.numbers.normalize_number`, the same guard the analyst pipeline's claim checker uses)
before it's trusted, falling back to a code-generated template sentence otherwise.

Pipeline: narrow_catalog (no LLM, trigram similarity) -> spec call (LLMClient.complete_json)
-> validate/auto-correct/clarify -> run the query -> explain call -> answer().
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from sqlite3 import Connection
from typing import Any

from ..llm.client import LLMClient, LLMError
from ..numbers import normalize_number
from . import store

MAX_METRICS = 40
MAX_ENTITIES = 40
HISTORY_TURNS = 4
AUTOCORRECT_THRESHOLD = 0.85
MAX_CLARIFY_CANDIDATES = 5

SPEC_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["dataset"],
    "properties": {
        "dataset": {"type": "string"},
        "metrics": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
        "regions": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
        "period_from": {}, "period_to": {},
        "version": {},  # "latest" | "history" | a version_label (e.g. "2026-08") -- polymorphic,
                        # checked by hand like "clarify" since labels are dataset-specific
        "rows": {"type": "array", "items": {"type": "string"}},
        "cols": {"type": "array", "items": {"type": "string"}},
        "chart": {"type": "string", "enum": ["line", "bar", "stacked", "table"]},
        "clarify": {},  # string | null -- polymorphic, checked by hand like analyst's "topic"
    },
}

SPEC_SYSTEM_PROMPT = """당신은 사내 데이터 대시보드의 질의 도우미입니다. 사용자의 한국어 질문을
아래 카탈로그(데이터셋/지표/대상/지역/기관/기간/버전)에 있는 이름만 사용해 하나의 JSON 조회 명세로
바꾸세요. 절대 숫자나 계산 결과를 직접 만들지 마세요 -- 오직 무엇을 조회할지만 결정합니다.
질문이 모호해서 데이터셋/지표/대상을 하나로 정할 수 없으면 "clarify"에 한국어로 되물을 질문을
쓰고 나머지 필드는 최선으로 채우세요. 특정 시점 하나의 값을 묻는 질문이면 rows/cols를 비우고
"chart":"table"을 쓰세요. 값의 변화 이력을 묻는 질문이면 "version":"history"를 쓰세요.

일부 데이터셋은 "버전"(예측 시점, 예: "2026-07", "2026-08")이 여러 개 있습니다(카탈로그의
"버전" 목록 참고). "7월 버전", "최신 버전" 등 특정 버전을 콕 집어 물으면 "version"에 그
버전 이름을 그대로 쓰세요(예: "version":"2026-08"). 버전을 언급하지 않았거나 최신을 원하면
"version":"latest"를 쓰세요(버전이 없는 데이터셋에는 이 필드가 아무 영향도 없습니다).

JSON 스키마:
{"dataset": "카탈로그의 데이터셋 이름", "metrics": ["..."], "entities": ["..."], "regions": ["..."],
 "sources": ["..."], "period_from": "YYYY..|null", "period_to": "YYYY..|null",
 "version": "latest|history|<버전 이름>", "rows": ["entity"등 표의 행 축],
 "cols": ["period"등 표의 열 축], "chart": "line|bar|stacked|table", "clarify": "되물을 질문|null"}
"""

EXPLAIN_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["sentence"],
    "properties": {"sentence": {"type": "string"}},
}

EXPLAIN_SYSTEM_PROMPT = """다음은 조회 결과 표입니다. 이 표에 있는 숫자만 사용해 한국어 한 문장으로
핵심을 설명하세요. 표에 없는 숫자를 만들거나 계산하지 마세요. JSON 객체 하나만 답하세요.

JSON 스키마: {"sentence": "한국어 설명 한 문장"}
"""


@dataclass
class ChatContext:
    conn: Connection
    llm: LLMClient


# --- trigram similarity (no LLM, no embeddings -- same technique as analyst.steps, kept local
# since that module's version is private) ------------------------------------------------------


def _trigrams(text: str) -> Counter:
    text = "".join(str(text).split()).casefold()
    if len(text) < 3:
        return Counter([text]) if text else Counter()
    return Counter(text[i:i + 3] for i in range(len(text) - 2))


def _similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    common = set(ta) & set(tb)
    dot = sum(ta[t] * tb[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in ta.values()))
    norm_b = math.sqrt(sum(v * v for v in tb.values()))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _top_candidates(name: str, pool: list[str], k: int) -> list[str]:
    scored = sorted(((c, _similarity(name, c)) for c in pool), key=lambda p: p[1], reverse=True)
    return [c for c, _ in scored[:k]]


# --- step 1: candidate narrowing (no LLM) -----------------------------------------------------


def narrow_catalog(conn: Connection, message: str, history: list[dict]) -> dict:
    datasets = store.catalog(conn)
    all_metrics = sorted({m for d in datasets for m in d["metrics"]})
    all_entities = sorted({e for d in datasets for e in d["entities"]})
    query_text = message + " " + " ".join(h.get("content", "") for h in (history or [])[-HISTORY_TURNS:])

    metrics = (all_metrics if len(all_metrics) <= MAX_METRICS
               else _top_candidates(query_text, all_metrics, MAX_METRICS))
    entities = (all_entities if len(all_entities) <= MAX_ENTITIES
                else _top_candidates(query_text, all_entities, MAX_ENTITIES))

    return {
        "datasets": [{"name": d["name"], "title": d["title"], "versions": d["version_labels"]}
                     for d in datasets],
        "metrics": sorted(metrics), "entities": sorted(entities),
        "regions": sorted({r for d in datasets for r in d["regions"]}),
        "sources": sorted({s for d in datasets for s in d["sources"]}),
        "period_from": min((d["period_from"] for d in datasets if d["period_from"]), default=None),
        "period_to": max((d["period_to"] for d in datasets if d["period_to"]), default=None),
    }


def spec_messages(message: str, history: list[dict], narrowed: dict) -> list[dict]:
    catalog_text = (
        f"데이터셋: {narrowed['datasets']}\n지표 후보: {narrowed['metrics']}\n"
        f"대상 후보: {narrowed['entities']}\n지역: {narrowed['regions']}\n기관: {narrowed['sources']}\n"
        f"전체 기간: {narrowed['period_from']} ~ {narrowed['period_to']}"
    )
    turns = "\n".join(f"{h.get('role', 'user')}: {h.get('content', '')}"
                       for h in (history or [])[-HISTORY_TURNS:])
    user = f"카탈로그:\n{catalog_text}\n\n이전 대화:\n{turns or '(없음)'}\n\n질문: {message}"
    return [{"role": "system", "content": SPEC_SYSTEM_PROMPT}, {"role": "user", "content": user}]


# --- step 2/3: validate + auto-correct/clarify -------------------------------------------------


def _resolve_one(name: str, candidates: list[str]) -> tuple[str | None, float]:
    if not candidates:
        return None, 0.0
    if name in candidates:
        return name, 1.0
    scored = sorted(((c, _similarity(name, c)) for c in candidates), key=lambda p: p[1], reverse=True)
    return scored[0]


def resolve_names(values: list[str] | None, candidates: list[str], *,
                   required: bool = True) -> tuple[list[str], list[str], list[str]]:
    """(resolved names, clarification questions, drop warnings).

    `required=True` (dataset/metric, or an entity the user explicitly named): a name that
    doesn't auto-correct produces a clarification question and stops the query.
    `required=False` (optional filters -- source/region, and an entity the LLM added on its own
    that the user never typed, e.g. expanding "제품별" into every catalog entity): a name that
    doesn't auto-correct is DROPPED instead, with a Korean warning -- it never blocks the query.
    """
    resolved: list[str] = []
    questions: list[str] = []
    warnings: list[str] = []
    for name in values or []:
        match, score = _resolve_one(name, candidates)
        if match is not None and (match == name or score >= AUTOCORRECT_THRESHOLD):
            resolved.append(match)
        elif required:
            top = _top_candidates(name, candidates, MAX_CLARIFY_CANDIDATES) if candidates else []
            options = ", ".join(top) if top else "(후보 없음)"
            questions.append(f'"{name}"을(를) 찾을 수 없습니다. 다음 중 하나인가요? {options}')
        else:
            warnings.append(f"존재하지 않는 조건 '{name}'는 제외했습니다")
    return resolved, questions, warnings


def _mentioned_in_message(name: str, message: str) -> bool:
    """Did the user's raw text actually contain this name, or did the LLM add it on its own
    (e.g. expanding "제품별" into every catalog entity)? A plain, whitespace-insensitive
    substring check -- good enough to tell "the user typed this" from "the model inferred
    this", without a second LLM call."""
    norm_name = "".join(str(name).split())
    norm_message = "".join(message.split())
    return bool(norm_name) and norm_name in norm_message


def resolve_entities(values: list[str] | None, candidates: list[str],
                      message: str) -> tuple[list[str], list[str], list[str]]:
    """Like `resolve_names`, but per-entity required-ness: an entity the user explicitly named
    in `message` is required (clarify on failure); one the model added on its own is optional
    (dropped with a warning on failure)."""
    explicit = [v for v in (values or []) if _mentioned_in_message(v, message)]
    implicit = [v for v in (values or []) if not _mentioned_in_message(v, message)]
    resolved_e, questions, _ = resolve_names(explicit, candidates, required=True)
    resolved_i, _, warnings = resolve_names(implicit, candidates, required=False)
    return resolved_e + resolved_i, questions, warnings


# --- number-checked explanation ----------------------------------------------------------------


# Period tokens ("2024Q1", "2024-03", "2024W05") look like numbers to a naive scan -- strip
# them whole before hunting for numbers, so e.g. the "2024" in "2024Q1" isn't checked against
# the table's values (a bare 4-digit VALUE, without a period suffix, is still checked).
_PERIOD_TOKEN_RE = re.compile(r"\d{4}(?:Q[1-4]|-\d{1,2}|W\d{1,2})")
_SENTENCE_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*%?")


def _num_text(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _table_number_texts(table: dict) -> set[str]:
    out: set[str] = set()
    for row in table.get("rows") or []:
        for v in row:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            out.add(normalize_number(_num_text(float(v))))
            out.add(normalize_number(str(round(float(v)))))
    return out


def _sentence_numbers_in_table(sentence: str, table: dict) -> bool:
    # ponytail: a float's decimal form rarely matches what a model chooses to round to --
    # int() and 2-decimal forms cover the common cases; a mismatch just falls back to the
    # template sentence (never a wrong-but-confident answer), so erring conservative is fine.
    table_numbers = _table_number_texts(table)
    stripped = _PERIOD_TOKEN_RE.sub(" ", sentence)
    for match in _SENTENCE_NUMBER_RE.finditer(stripped):
        norm = normalize_number(match.group())
        if norm and norm not in table_numbers:
            return False
    return True


def _template_sentence(table: dict, value_cols: list[int] | None = None) -> str:
    """`value_cols`: which columns hold actual data values, not id/label columns (e.g. a
    history table's "load_id" is numeric but not a value -- see the two `explain()` callers)."""
    rows = table.get("rows") or []
    cols = value_cols if value_cols is not None else list(range(len(table.get("columns") or [])))
    values = [row[i] for row in rows for i in cols
              if i < len(row) and not isinstance(row[i], bool) and isinstance(row[i], (int, float))]
    if not values:
        return f"조건에 맞는 데이터가 {len(rows)}건 있습니다."
    first, last, vmax, vmin = values[0], values[-1], max(values), min(values)
    return (f"첫 값은 {_num_text(first)}, 마지막 값은 {_num_text(last)}이고, "
            f"최댓값은 {_num_text(vmax)}, 최솟값은 {_num_text(vmin)}입니다.")


def explain_messages(table: dict) -> list[dict]:
    columns = table.get("columns") or []
    rows = (table.get("rows") or [])[:30]
    lines = [", ".join(str(c) for c in columns)] + [", ".join(str(v) for v in r) for r in rows]
    return [{"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
            {"role": "user", "content": "표:\n" + "\n".join(lines)}]


def explain(llm: LLMClient, table: dict, value_cols: list[int] | None = None) -> tuple[str, list[str]]:
    if not (table.get("rows")):
        return "조건에 맞는 데이터가 없습니다.", []
    try:
        data = llm.complete_json(explain_messages(table), EXPLAIN_SCHEMA,
                                  step="dataplat.chat.explain", agent="dataplat")
        sentence = data.get("sentence", "")
    except LLMError:
        return _template_sentence(table, value_cols), ["explain_failed_used_template"]
    if sentence and _sentence_numbers_in_table(sentence, table):
        return sentence, []
    return _template_sentence(table, value_cols), ["explain_number_mismatch_used_template"]


# --- chart shaping ------------------------------------------------------------------------------


def _chart_from_wide(table: dict, chart_type: str, row_dims_count: int) -> dict | None:
    columns, rows = table.get("columns") or [], table.get("rows") or []
    if chart_type == "table" or not rows or len(columns) <= row_dims_count:
        return None
    x = columns[row_dims_count:]
    series = [{"name": " / ".join(str(v) for v in r[:row_dims_count]) or "값",
               "values": r[row_dims_count:]} for r in rows]
    return {"type": chart_type, "x": x, "series": series, "unit": ""}


def _chart_from_history(rows: list[dict], series_name: str, x_labels: list[str]) -> dict | None:
    if not rows:
        return None
    return {"type": "line", "x": x_labels,
            "series": [{"name": series_name, "values": [r["value"] for r in rows]}], "unit": ""}


# --- top-level entry point -----------------------------------------------------------------------


def _clarify(question: str, spec: dict | None, started: float) -> dict:
    return {"answer": question, "spec": spec, "table": None, "chart": None,
            "warnings": ["clarify"], "seconds": round(time.perf_counter() - started, 2)}


def answer(ctx: ChatContext, message: str, history: list[dict] | None = None) -> dict:
    started = time.perf_counter()
    history = history or []

    narrowed = narrow_catalog(ctx.conn, message, history)
    full_catalog = store.catalog(ctx.conn)
    dataset_names = [d["name"] for d in full_catalog]

    try:
        spec = ctx.llm.complete_json(spec_messages(message, history, narrowed), SPEC_SCHEMA,
                                      step="dataplat.chat.spec", agent="dataplat")
    except LLMError as exc:
        return _clarify(f"질문을 이해하지 못했습니다. 다시 말씀해 주시겠어요? ({exc})", None, started)

    if spec.get("clarify"):
        return _clarify(str(spec["clarify"]), spec, started)

    ds_match, ds_score = _resolve_one(spec.get("dataset", ""), dataset_names)
    if ds_match is None or (ds_match != spec.get("dataset") and ds_score < AUTOCORRECT_THRESHOLD):
        options = ", ".join(_top_candidates(spec.get("dataset", ""), dataset_names, MAX_CLARIFY_CANDIDATES))
        return _clarify(f"어떤 데이터셋을 말씀하신 건가요? 후보: {options or '(없음)'}", spec, started)

    ds = next(d for d in full_catalog if d["name"] == ds_match)
    metrics, metric_q, _ = resolve_names(spec.get("metrics"), ds["metrics"], required=True)
    entities, entity_q, entity_drop = resolve_entities(spec.get("entities"), ds["entities"], message)
    regions, _, region_drop = resolve_names(spec.get("regions"), ds["regions"], required=False)
    sources, _, source_drop = resolve_names(spec.get("sources"), ds["sources"], required=False)
    questions = metric_q + entity_q
    if questions:
        return _clarify(" / ".join(questions[:3]), spec, started)
    drop_warnings = entity_drop + region_drop + source_drop

    raw_version = spec.get("version")
    if raw_version in (None, "", "latest", "history"):
        query_version = raw_version or "latest"
    else:
        match, score = _resolve_one(str(raw_version), ds["version_labels"])
        if match is None or (match != raw_version and score < AUTOCORRECT_THRESHOLD):
            options = ", ".join(ds["version_labels"]) or "(없음)"
            return _clarify(f"어떤 버전을 말씀하신 건가요? 후보: {options}", spec, started)
        query_version = match

    if query_version == "history":
        if not metrics or not entities:
            return _clarify("이력을 보려면 지표와 대상을 하나씩 알려주세요.", spec, started)
        period = spec.get("period_to") or spec.get("period_from") or ""
        if not period:
            return _clarify("이력을 보려면 조회할 기간(period)을 알려주세요.", spec, started)
        # history() only tracks ONE series (no cross-entity ranking/diff query exists yet) --
        # if the model asked for several entities, say so instead of silently answering for
        # just the first one (e.g. "which model changed the most" can't be answered this way).
        if len(entities) > 1:
            drop_warnings.append(
                f"여러 대상 중 '{entities[0]}'의 이력만 보여드립니다 (한 번에 하나의 대상만 가능)")
        rows = store.history(ctx.conn, dataset=ds_match, metric=metrics[0], entity=entities[0],
                              period=period, source=sources[0] if sources else None)
        labels = [r.get("version_label") or (r["finished_at"] or r["started_at"]) for r in rows]
        table = {"columns": ["load_id", "label", "value"],
                 "rows": [[r["load_id"], lbl, r["value"]] for r, lbl in zip(rows, labels)]}
        chart = _chart_from_history(rows, f"{metrics[0]}/{entities[0]}", labels)
        value_cols = [2]  # only "value" -- "load_id" is an id, not a data value
    else:
        long_rows = store.query_observations(
            ctx.conn, dataset=ds_match, metrics=metrics or None, entities=entities or None,
            regions=regions or None, sources=sources or None,
            period_from=spec.get("period_from") or None, period_to=spec.get("period_to") or None,
            version=query_version,
        )
        if len(long_rows) > store.QUERY_ROW_CAP:
            return _clarify("조건에 맞는 데이터가 너무 많습니다. 질문을 더 구체적으로 해주세요.", spec, started)
        row_dims = [d for d in (spec.get("rows") or ["entity"]) if d]
        col_dims = [d for d in (spec.get("cols") or ["period"]) if d]
        table = store.to_wide(long_rows, row_dims, col_dims)
        chart = _chart_from_wide(table, spec.get("chart") or "table", len(row_dims))
        value_cols = list(range(len(row_dims), len(table["columns"])))  # id/dim columns excluded

    sentence, explain_warnings = explain(ctx.llm, table, value_cols)
    return {"answer": sentence, "spec": spec, "table": table, "chart": chart,
            "warnings": drop_warnings + explain_warnings,
            "seconds": round(time.perf_counter() - started, 2)}
