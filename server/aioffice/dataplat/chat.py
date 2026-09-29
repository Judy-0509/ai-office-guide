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
from typing import Any, Callable

from ..llm.client import LLMClient, LLMError
from ..numbers import normalize_number
from . import store
from .aliases import AliasConfig
from .aliases import resolve_aliases as _resolve_aliases
from .normalize import normalize_period

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
        "version": {},  # "latest" | "history" | "diff" | a version_label (e.g. "2026-08") --
                        # polymorphic, checked by hand like "clarify" (labels are dataset-specific)
        "from": {}, "to": {},  # version="diff" only: optional version labels to compare
        "rows": {"type": "array", "items": {"type": "string"}},
        "cols": {"type": "array", "items": {"type": "string"}},
        "chart": {"type": "string", "enum": ["line", "bar", "stacked", "table"]},
        "rollup": {},  # null | "year" | "quarter" -- group periods to a coarser level before
                       # pivoting (e.g. "2025년 합계"); only applies outside history/diff
        "agg": {},     # "sum" | "avg", only meaningful together with "rollup"
        "clarify": {},  # string | null -- polymorphic, checked by hand like analyst's "topic"
    },
}

SPEC_SYSTEM_PROMPT = """당신은 사내 데이터 대시보드의 질의 도우미입니다. 사용자의 한국어 질문을
아래 카탈로그(데이터셋/지표/대상/지역/기관/기간/버전)에 있는 이름만 사용해 하나의 JSON 조회 명세로
바꾸세요. 절대 숫자나 계산 결과를 직접 만들지 마세요 -- 오직 무엇을 조회할지만 결정합니다.
질문이 모호해서 데이터셋/지표/대상을 하나로 정할 수 없으면 "clarify"에 한국어로 되물을 질문을
쓰고 나머지 필드는 최선으로 채우세요. 특정 시점 하나의 값을 묻는 질문이면 rows/cols를 비우고
"chart":"table"을 쓰세요.

**질문에 특정 대상(모델 등) 이름이 정확히 하나만 나왔고, 그 대상이 이전과 비교해 어떻게
바뀌었는지 묻는 질문**(예: "모델A는 지난 버전과 비교해 어때?", "모델A 출하량 스냅샷별로 어떻게
바뀌었어?")이면 "version":"history"를 쓰세요 -- entities에도 그 대상 하나만 넣으세요.

일부 데이터셋은 "버전"(예측 시점, 예: "2026-07", "2026-08")이 여러 개 있습니다(카탈로그의
"버전" 목록 참고). "7월 버전", "최신 버전" 등 특정 버전을 콕 집어 물으면 "version"에 그
버전 이름을 그대로 쓰세요(예: "version":"2026-08"). 버전을 언급하지 않았거나 최신을 원하면
"version":"latest"를 쓰세요(버전이 없는 데이터셋에는 이 필드가 아무 영향도 없습니다).

**대상 이름을 콕 집지 않았거나 여러 개(또는 전체)에 걸쳐 두 버전을 비교/순위를 묻는 질문**
(예: "지난 버전 대비 가장 많이 바뀐/증가한/감소한 모델은?", "버전별로 뭐가 달라졌어?")이면
"version":"diff"를 쓰세요 -- "history"는 대상 하나의 시간 흐름만 보여주므로 이런 순위/비교
질문에는 맞지 않습니다. 비교할 두 버전을 특정하지 않았으면 "from"/"to"를 비워두세요(자동으로
최신 버전과 그 이전 버전을 비교합니다). 무엇을 기준으로 비교할지는 "rows"에 담으세요(예:
모델별 비교면 ["entity"], 기본값도 ["entity"]). diff 결과 표는 대상별 old(이전 값)/new(새
값)/diff(차이)/pct(변화율 %)를 코드가 직접 계산해 채웁니다.

**"OO년 합계/총/합"처럼 특정 기간을 더한 값을 물으면** "rollup"에 "year"(연도 단위) 또는
"quarter"(분기 단위)를 쓰고 "agg"는 보통 "sum"(합계, 기본값), 평균을 물으면 "avg"를 쓰세요.
합계를 묻는 게 아니면 "rollup"은 null로 두세요.

JSON 스키마:
{"dataset": "카탈로그의 데이터셋 이름", "metrics": ["..."], "entities": ["..."], "regions": ["..."],
 "sources": ["..."], "period_from": "YYYY..|null", "period_to": "YYYY..|null",
 "version": "latest|history|diff|<버전 이름>", "from": "diff용 이전 버전|null",
 "to": "diff용 새 버전|null", "rows": ["entity"등 표의 행 축(또는 diff의 비교 기준)],
 "cols": ["period"등 표의 열 축], "chart": "line|bar|stacked|table",
 "rollup": "year|quarter|null", "agg": "sum|avg", "clarify": "되물을 질문|null"}
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
    aliases: AliasConfig | None = None  # aliases.yaml, see dataplat/aliases.py


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


# --- step 0: rule-based fast path (no LLM) ------------------------------------------------------
# The shared in-house LLM endpoint queues requests from other company users -- each avoided call
# saves roughly 1-3 minutes (Round 7). Before ever calling the LLM, try to build a spec
# deterministically from catalog name matches + a few Korean intent keywords. Only used when a
# dataset AND an intent were both determined AND at least one entity or period was found;
# anything less confident falls back to the LLM spec call, same as before.


def _norm_for_match(text: str) -> str:
    return "".join(str(text).split()).casefold()


def _find_name_matches(message: str, candidates: list[str]) -> list[str]:
    """Catalog names that appear as a case/space-insensitive substring of `message`, longest
    match first (so a more specific name wins over a shorter one it contains). Also matches the
    part of a candidate before " (" (e.g. "모델A (자사)" matches on "모델A")."""
    norm_message = _norm_for_match(message)
    scored: list[tuple[int, str]] = []
    for c in candidates:
        variants = [c] + ([c.split(" (", 1)[0]] if " (" in c else [])
        for v in variants:
            norm_v = _norm_for_match(v)
            if norm_v and norm_v in norm_message:
                scored.append((len(norm_v), c))
                break
    scored.sort(key=lambda p: -p[0])
    seen: set[str] = set()
    out = []
    for _, c in scored:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _dedup(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for v in seq:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _alias_matches(message: str, alias_map: dict[str, list[str]]) -> list[str]:
    """Like `_find_name_matches`, but keys are alias phrases (aliases.yaml, or a learned alias)
    and each match expands to a list of real catalog values (e.g. "삼성" -> ["모델A", "모델B"]).
    Longest phrase first, so a more specific alias wins."""
    norm_message = _norm_for_match(message)
    out: list[str] = []
    for phrase in sorted(alias_map, key=len, reverse=True):
        norm_phrase = _norm_for_match(phrase)
        if norm_phrase and norm_phrase in norm_message:
            out.extend(alias_map[phrase])
    return _dedup(out)


# AliasMaps: {dataset: {"entities"|"regions"|"metrics": {phrase: [catalog values]}}}
AliasMaps = dict[str, dict[str, dict[str, list[str]]]]


def build_alias_maps(conn: Connection, aliases_cfg: AliasConfig | None,
                      full_catalog: list[dict]) -> AliasMaps:
    """Combines `aliases.yaml` (if configured) with learned aliases (DB, Round 8's learning
    loop) into one lookup the rule path and the clarify-button builder both use. Hand-written
    aliases.yaml entries win over a learned one on the same phrase."""
    learned_by_dataset: dict[str, list[dict]] = {}
    for row in store.list_learned_aliases(conn):
        learned_by_dataset.setdefault(row["dataset"], []).append(row)

    out: AliasMaps = {}
    for ds in full_catalog:
        name = ds["name"]
        per_dim: dict[str, dict[str, list[str]]] = {}
        for dim, catalog_values in (("entities", ds["entities"]), ("regions", ds["regions"]),
                                     ("metrics", ds["metrics"])):
            merged = (dict(_resolve_aliases(aliases_cfg, name, dim, catalog_values))
                      if aliases_cfg is not None else {})
            for row in learned_by_dataset.get(name, []):
                if row["dim"] != dim or row["phrase"] in merged:
                    continue
                filtered = [v for v in row["values"] if v in catalog_values]
                if filtered:
                    merged[row["phrase"]] = filtered
            per_dim[dim] = merged
        out[name] = per_dim
    return out


_YEAR_RANGE_KOREAN_RE = re.compile(r"((?:19|20)\d{2})년?\s*부터\s*((?:19|20)\d{2})년?\s*까지")
_YEAR_RANGE_TILDE_RE = re.compile(r"((?:19|20)\d{2})\s*[~\-]\s*((?:19|20)\d{2})")
# Korean-native "<year>년 <n>분기" phrasing (e.g. "2025년 3분기에") -- not a `normalize_period`
# shape (that module only knows compact codes like "2025Q3"), so it's matched separately, over
# the raw message, before the generic token scan below.
_KOREAN_QUARTER_RE = re.compile(r"((?:19|20)\d{2})년?\s*([1-4])\s*분기")
# Every other period shape (plain years, "2025Q3"/"3Q25"/"FY25"/"2025.09"/... -- anything
# `normalize_period` recognizes) is found by testing each digit/letter run in the message as a
# candidate token. Korean characters (particles, "년"/"월"/"분기"...) fall outside this class, so
# they naturally end a token instead of needing to be stripped -- "25Q3은" still yields "25Q3".
# (named distinctly from the unrelated `_PERIOD_TOKEN_RE` used later by the number-check code)
_PERIOD_SCAN_TOKEN_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.\-/']*")


def _scan_periods(message: str) -> tuple[str | None, str | None]:
    """(period_from, period_to) scanned out of free Korean text -- years, quarters (compact
    codes and alt spellings, Korean "<year>년 <n>분기" phrasing), months, and ranges
    ("2024~2025", "2024년부터 2025년까지"). ponytail: relative periods ("작년", "올해") aren't
    resolved here -- a message using only those has no period signal and, absent an entity too,
    falls back to the LLM."""
    m = _YEAR_RANGE_KOREAN_RE.search(message) or _YEAR_RANGE_TILDE_RE.search(message)
    if m:
        return m.group(1), m.group(2)
    m = _KOREAN_QUARTER_RE.search(message)
    if m:
        p = f"{m.group(1)}Q{m.group(2)}"
        return p, p
    for token in _PERIOD_SCAN_TOKEN_RE.findall(message):
        result = normalize_period(token)
        if result.ok:
            return result.period, result.period
    return None, None


def _rule_based_spec(message: str, full_catalog: list[dict],
                      alias_maps: AliasMaps | None = None) -> dict | None:
    """A deterministic spec for common question shapes, or None when not confident enough --
    the caller then falls back to the LLM spec call. `alias_maps` (aliases.yaml + learned
    aliases, see `build_alias_maps`) are checked alongside literal catalog names for
    entities/regions/metrics."""
    alias_maps = alias_maps or {}

    def matches(ds_name: str, dim: str, catalog_values: list[str]) -> list[str]:
        direct = _find_name_matches(message, catalog_values)
        via_alias = _alias_matches(message, alias_maps.get(ds_name, {}).get(dim, {}))
        return _dedup(direct + via_alias)

    per_dataset_hits: dict[str, dict[str, list[str]]] = {}
    for ds in full_catalog:
        hits = {"entities": matches(ds["name"], "entities", ds["entities"]),
                "regions": matches(ds["name"], "regions", ds["regions"]),
                "sources": _find_name_matches(message, ds["sources"])}
        if any(hits.values()):
            per_dataset_hits[ds["name"]] = hits

    if len(per_dataset_hits) == 1:
        ds_name = next(iter(per_dataset_hits))
        ds = next(d for d in full_catalog if d["name"] == ds_name)
        hits = per_dataset_hits[ds_name]
    elif len(per_dataset_hits) == 0 and len(full_catalog) == 1:
        ds = full_catalog[0]
        hits = {"entities": [], "regions": [], "sources": []}
    else:
        return None  # ambiguous across datasets, or no name signal in a multi-dataset catalog

    if len(ds["metrics"]) == 1:
        metric = ds["metrics"][0]
    else:
        metric_hits = matches(ds["name"], "metrics", ds["metrics"])
        if not metric_hits:
            return None
        metric = metric_hits[0]

    entities, regions, sources = hits["entities"], hits["regions"], hits["sources"]
    period_from, period_to = _scan_periods(message)
    if not entities and not period_from:
        return None  # no entity and no period -- too little signal, let the LLM handle it

    base = {"dataset": ds["name"], "metrics": [metric], "entities": entities,
            "regions": regions, "sources": sources,
            "period_from": period_from, "period_to": period_to, "clarify": None}

    # 추이/흐름/분기별/월별/연도별 mean a TREND across the `period` axis already in the data
    # (entity x period, oldest to newest) -- NOT a version/snapshot history (`store.history`,
    # which needs one period pinned down). Version history is only implied by an actual version
    # word (버전, 지난/이전 버전, 버전 대비, "7월 버전"...), handled by the diff branch below --
    # the rule path doesn't build a `version:"history"` spec at all; that nuance is left to the
    # LLM. With no period named, period_from/period_to stay None -- the full available range.
    if any(kw in message for kw in ("추이", "흐름", "분기별", "월별", "연도별", "연별")) and len(entities) == 1:
        rollup = ("quarter" if "분기별" in message else
                  "year" if ("연도별" in message or "연별" in message) else None)
        return {**base, "version": "latest", "rollup": rollup, "agg": "sum",
                "rows": ["entity"], "cols": ["period"], "chart": "line"}

    if (any(kw in message for kw in
            ("지난 버전", "이전 버전", "버전 대비", "바뀐", "변경", "상향", "하향"))
            or ("가장" in message and "많이" in message
                and ("증가" in message or "감소" in message))):
        return {**base, "version": "diff", "from": None, "to": None,
                "rows": ["entity"], "cols": [], "chart": "bar"}

    if any(kw in message for kw in ("합계", "총", "합")):
        rollup = "year" if period_from and re.fullmatch(r"(19|20)\d{2}", period_from) else None
        return {**base, "version": "latest", "rollup": rollup, "agg": "sum",
                "rows": ["entity"], "cols": ["period"], "chart": "table"}

    if (("비교" in message or "대비" in message or "vs" in message.lower())
            and len(entities) >= 2):
        return {**base, "version": "latest", "rows": ["entity"], "cols": ["period"],
                "chart": "table"}

    if (("얼마" in message or "몇" in message) and len(entities) == 1
            and period_from and period_from == period_to):
        return {**base, "version": "latest", "rows": [], "cols": [], "chart": "table"}

    return None  # no recognized intent -- fall back to the LLM


def _spec_cache_key(message: str) -> str:
    return _norm_for_match(message)


# --- suggested questions + clarify buttons (no LLM) --------------------------------------------

SUGGESTION_LIMIT = 8
MAX_CLARIFY_OPTIONS = 6
ASK_AI_OPTION = {"label": "AI에게 물어보기 (몇 분 걸릴 수 있음)", "action": "ask_ai"}


def _top_entity_by_latest_total(conn: Connection, dataset: str, metric: str | None) -> str | None:
    rows = store.query_observations(conn, dataset=dataset, metrics=[metric] if metric else None,
                                     version="latest")
    totals: dict[str, float] = {}
    for r in rows:
        if r["entity"] and isinstance(r["value"], (int, float)):
            totals[r["entity"]] = totals.get(r["entity"], 0.0) + r["value"]
    return max(totals, key=lambda e: totals[e]) if totals else None


def _latest_spec(dataset: str, metric: str | None, **overrides: Any) -> dict:
    spec = {"dataset": dataset, "metrics": [metric] if metric else [], "entities": [],
            "regions": [], "sources": [], "period_from": None, "period_to": None,
            "version": "latest", "rows": ["entity"], "cols": ["period"], "chart": "table",
            "clarify": None}
    spec.update(overrides)
    return spec


def build_suggestions(conn: Connection, dataset: str) -> list[dict]:
    """Up to `SUGGESTION_LIMIT` ready-to-click questions generated from the catalog + latest
    load, each carrying a precomputed `spec_patch` -- clicking runs it directly
    (`answer_with_spec`, `path="button"`), no LLM and no rule-path re-matching needed. Fewer than
    the limit is fine for a dataset that doesn't have enough distinct signal (e.g. no regions)."""
    ds = next((d for d in store.catalog(conn) if d["name"] == dataset), None)
    if ds is None:
        return []
    metric = ds["metrics"][0] if len(ds["metrics"]) == 1 else None
    top_entity = _top_entity_by_latest_total(conn, dataset, metric)
    out: list[dict] = []

    if ds["version_labels"]:
        out.append({"label": "지난 버전 대비 가장 많이 바뀐 항목",
                     "spec_patch": _latest_spec(dataset, metric, version="diff", **{
                         "from": None, "to": None, "cols": [], "chart": "bar"})})

    if top_entity:
        out.append({"label": f"{top_entity} 분기별 추이",
                     "spec_patch": _latest_spec(dataset, metric, entities=[top_entity],
                                                 rollup=None, agg="sum", chart="line")})

    if ds["period_to"]:
        year = ds["period_to"][:4]
        out.append({"label": f"{year}년 항목별 합계",
                     "spec_patch": _latest_spec(dataset, metric, period_from=year,
                                                 period_to=year, rollup="year", agg="sum")})

    if len(ds["entities"]) >= 2:
        out.append({"label": "전체 항목 비교",
                     "spec_patch": _latest_spec(dataset, metric, entities=ds["entities"][:6])})

    if ds["regions"]:
        top_region = ds["regions"][0]
        out.append({"label": f"{top_region} 지역 표로 보기",
                     "spec_patch": _latest_spec(dataset, metric, regions=[top_region])})

    out.append({"label": f"{ds['title'] or dataset} 전체 표로 보기",
                 "spec_patch": _latest_spec(dataset, metric)})

    return out[:SUGGESTION_LIMIT]


def _clarify_button_options(conn: Connection, message: str, full_catalog: list[dict],
                             alias_maps: AliasMaps) -> list[dict]:
    """Up to `MAX_CLARIFY_OPTIONS` clickable options for when the rule path/cache couldn't
    resolve a full spec on their own: one option per catalog entity the message (partially)
    matched (via a literal name or an alias), each with a ready-to-run `spec_patch`. When
    NOTHING at all matched, falls back to dataset-level `build_suggestions()`. Always ends with
    an "AI에게 물어보기" entry -- that's the only way the LLM gets reached from here on."""
    candidates: list[tuple[str, dict, str]] = []
    for ds in full_catalog:
        alias_map = alias_maps.get(ds["name"], {}).get("entities", {})
        for entity in _dedup(_find_name_matches(message, ds["entities"]) + _alias_matches(message, alias_map)):
            candidates.append((ds["name"], ds, entity))

    options: list[dict] = []
    if candidates:
        for ds_name, ds, entity in candidates[:MAX_CLARIFY_OPTIONS]:
            metric = ds["metrics"][0] if len(ds["metrics"]) == 1 else None
            spec_patch = _latest_spec(ds_name, metric, entities=[entity], rollup=None,
                                       agg="sum", chart="line")
            options.append({"label": f"{entity} 보기 ({ds['title'] or ds_name})",
                             "spec_patch": spec_patch})
    else:
        for ds in full_catalog:
            options.extend(build_suggestions(conn, ds["name"]))
            if len(options) >= MAX_CLARIFY_OPTIONS:
                break
        options = options[:MAX_CLARIFY_OPTIONS]

    return options + [ASK_AI_OPTION]


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


def _resolve_optional_label(raw: Any, candidates: list[str]) -> tuple[str | None, str | None]:
    """(resolved label or None, clarification question or None) -- for version="diff"'s
    optional "from"/"to" spec fields. `raw` absent/blank means "let version_diff pick the
    default" (None, None); a name that doesn't auto-correct blocks with a clarification, same
    threshold as every other name in this module."""
    if raw in (None, ""):
        return None, None
    match, score = _resolve_one(str(raw), candidates)
    if match is None or (match != raw and score < AUTOCORRECT_THRESHOLD):
        options = ", ".join(candidates) or "(없음)"
        return None, f"어떤 버전을 말씀하신 건가요? 후보: {options}"
    return match, None


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


def _strip_trailing_zero_decimal(text: str) -> str:
    """"100.0" -> "100", "25.50" -> "25.5" -- a model faithfully echoing a table cell's Python
    `str()` form ("100.0", since the table itself renders floats that way in the prompt) must
    compare equal to that same value's canonical no-decimal form (`_num_text(100.0) == "100"`).
    Confirmed live: every rejected number in 3 real runs (wide/history/diff tables) was this
    exact mismatch -- "100.0"/"200.0"/"-30.0"/"25.0%" vs table forms "100"/"200"/"-30"/"25"."""
    if "." not in text:
        return text
    stripped = text.rstrip("0").rstrip(".")
    return stripped or "0"


def _first_rejected_number(sentence: str, table: dict) -> str | None:
    """The first number in `sentence` that appears nowhere in `table` (in either its exact or
    trailing-.0-stripped form) -- or None if every number checks out. Also used to report
    (in `warnings`, on fallback) exactly which number triggered the fallback.

    ponytail: a float's 2-decimal rounded form rarely matches what a model chooses to round
    to beyond that -- a mismatch just falls back to the template sentence (never a
    wrong-but-confident answer), so erring conservative there is fine."""
    table_numbers = _table_number_texts(table)
    stripped = _PERIOD_TOKEN_RE.sub(" ", sentence)
    for match in _SENTENCE_NUMBER_RE.finditer(stripped):
        norm = normalize_number(match.group())
        if not norm:
            continue
        if norm in table_numbers or _strip_trailing_zero_decimal(norm) in table_numbers:
            continue
        return match.group()
    return None


def _signed_num_text(value: float) -> str:
    return f"{'-' if value < 0 else '+'}{_num_text(abs(value))}"


# --- type-aware fallback templates --------------------------------------------------------------
# Used only when the LLM's own sentence fails the number check (or the call itself fails). Each
# is built for exactly the table shape its `answer()` branch produces, so it can name real
# row/column labels instead of a generic "first/last/max/min" -- which for a ranking question
# (diff) is meaningless without knowing WHICH row was first/last.


def _template_diff(rows: list[list], group_by: list[str], from_label: str | None,
                    to_label: str | None, unit: str) -> str:
    """rows: version_diff's [*group_vals, old, new, diff, pct] rows, already ranked."""
    if not rows:
        return "버전 사이에 변화가 없습니다."
    gb_count = len(group_by)

    def fmt(row: list) -> str:
        label = " / ".join(str(v) for v in row[:gb_count])
        diff_v, pct_v = row[gb_count + 2], row[gb_count + 3]
        diff_text = _signed_num_text(diff_v) + unit
        pct_text = f"{_signed_num_text(pct_v)}%" if isinstance(pct_v, (int, float)) else "N/A"
        return f"{label}({diff_text}, {pct_text})"

    sentence = f"가장 많이 바뀐 것은 {fmt(rows[0])}"
    sentence += f"이고, 다음은 {fmt(rows[1])}입니다." if len(rows) > 1 else "입니다."
    if from_label and to_label:
        sentence += f" ({from_label} → {to_label})"
    return sentence


def _template_history(rows: list[list], entity: str, unit: str) -> str:
    """rows: [load_id, label, value] rows from the history table, oldest to newest."""
    numeric = [r for r in rows if len(r) > 2 and isinstance(r[2], (int, float))
               and not isinstance(r[2], bool)]
    if not numeric:
        return "이력 데이터가 없습니다."
    first, last = numeric[0], numeric[-1]
    prefix = f"{entity}: " if entity else ""
    sentence = (f"{prefix}{first[1]} {_num_text(first[2])}{unit} → "
                f"{last[1]} {_num_text(last[2])}{unit}")
    if len(numeric) > 1:
        diff = last[2] - first[2]
        pct_text = f", {_signed_num_text(diff / abs(first[2]) * 100)}%" if first[2] != 0 else ""
        sentence += f" ({_signed_num_text(diff)}{unit}{pct_text})"
    return sentence + "."


def _template_wide(table: dict, row_dims: list[str], unit: str) -> str:
    """table: {"columns": row_dims + col_headers, "rows": [...]} from `store.to_wide`. Three
    shapes: exactly one cell -> name it directly; exactly one row (a single entity's trend
    across periods) -> first -> last plus the peak; otherwise -> max/min across the whole
    table (a genuine comparison, e.g. across entities)."""
    columns, rows = table.get("columns") or [], table.get("rows") or []
    row_dims_count = len(row_dims)
    col_headers = columns[row_dims_count:]
    cells: list[tuple[float, str, str]] = []
    for row in rows:
        row_label = " / ".join(str(v) for v in row[:row_dims_count])
        for i, col_label in enumerate(col_headers):
            idx = row_dims_count + i
            if idx < len(row) and isinstance(row[idx], (int, float)) and not isinstance(row[idx], bool):
                cells.append((row[idx], row_label, str(col_label)))
    if not cells:
        return f"조건에 맞는 데이터가 {len(rows)}건 있습니다."
    if len(cells) == 1:
        value, row_label, col_label = cells[0]
        return f"{row_label} {col_label}: {_num_text(value)}{unit}."
    if len(rows) == 1:
        row_label = " / ".join(str(v) for v in rows[0][:row_dims_count])
        first, last = cells[0], cells[-1]
        vmax = max(cells, key=lambda c: c[0])
        return (f"{row_label}: {first[2]} {_num_text(first[0])}{unit} → "
                f"{last[2]} {_num_text(last[0])}{unit} "
                f"(최고 {vmax[2]} {_num_text(vmax[0])}{unit}).")
    vmax, vmin = max(cells, key=lambda c: c[0]), min(cells, key=lambda c: c[0])
    return (f"최댓값: {vmax[1]} {vmax[2]} {_num_text(vmax[0])}{unit}, "
            f"최솟값: {vmin[1]} {vmin[2]} {_num_text(vmin[0])}{unit}.")


def explain_messages(table: dict) -> list[dict]:
    columns = table.get("columns") or []
    rows = (table.get("rows") or [])[:30]
    lines = [", ".join(str(c) for c in columns)] + [", ".join(str(v) for v in r) for r in rows]
    return [{"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
            {"role": "user", "content": "표:\n" + "\n".join(lines)}]


def explain(llm: LLMClient, table: dict, template_fn: Callable[[], str],
            *, timeout: float | None = None) -> tuple[str, list[str]]:
    """`template_fn`: a zero-arg closure built by the caller (one per table shape -- diff/
    history/wide -- see `answer()`) that produces the type-aware fallback sentence. Only called
    when `answer()`'s `explain_mode="llm"` -- the default `explain_mode="template"` skips this
    LLM call entirely and uses `template_fn()` directly (Round 7: the shared in-house endpoint's
    queue makes every avoided call worth 1-3 minutes)."""
    if not (table.get("rows")):
        return "조건에 맞는 데이터가 없습니다.", []
    try:
        data = llm.complete_json(explain_messages(table), EXPLAIN_SCHEMA,
                                  step="dataplat.explain", agent="dataplat", timeout=timeout)
        sentence = data.get("sentence", "")
    except LLMError:
        return template_fn(), ["explain_failed_used_template"]
    if sentence:
        rejected = _first_rejected_number(sentence, table)
        if rejected is None:
            return sentence, []
        return template_fn(), [f"explain_number_mismatch_used_template: {rejected!r} not in table"]
    return template_fn(), ["explain_number_mismatch_used_template"]


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


def _chart_from_diff(diff_rows: list[dict], group_by: list[str]) -> dict | None:
    """Bar chart of `diff` by the group dimension(s) -- version_diff's rows are already sorted
    by the requested rank (abs/rel), so this is a ranked bar chart, not just alphabetical."""
    if not diff_rows:
        return None
    x = [" / ".join(str(r.get(d, "")) for d in group_by) for r in diff_rows]
    return {"type": "bar", "x": x,
            "series": [{"name": "diff", "values": [r["diff"] for r in diff_rows]}], "unit": ""}


# --- top-level entry point -----------------------------------------------------------------------

DEFAULT_CHAT_TIMEOUT_SEC = 180.0
_ZERO_TIMINGS = {"spec_seconds": 0.0, "explain_seconds": 0.0, "query_seconds": 0.0}
_LLM_BUSY_MESSAGE = ("지금 사내 LLM이 붐벼서 응답이 늦습니다. 모델명·기간을 넣어 더 구체적으로 "
                     "물어보시면 바로 답할 수 있습니다.")


def _clarify(question: str, spec: dict | None, started: float, *, path: str,
             timings: dict, options: list[dict] | None = None) -> dict:
    return {"answer": question, "spec": spec, "table": None, "chart": None,
            "warnings": ["clarify"], "path": path, "timings": timings, "options": options,
            "seconds": round(time.perf_counter() - started, 2)}


def _missing_entities_warning(requested_entities: list[str], rows: list[dict]) -> list[str]:
    """When specific entities were asked for and some come back with NO rows at all (not just
    a blank cell), warn instead of silently showing a smaller table."""
    if not requested_entities:
        return []
    present = {r["entity"] for r in rows}
    return [f"'{e}'은(는) 해당 기간 데이터가 없습니다" for e in requested_entities if e not in present]


def _learn_entities(ctx: ChatContext, message: str, spec: dict, full_catalog: list[dict]) -> None:
    """Round 8 learning loop: if `spec` names entities that the rule path (literal names +
    current aliases) can't find in `message` on its own, remember `message` -> those entities as
    a learned alias, so an identical question skips the LLM/button next time. Called only after
    a query that actually returned rows (never learn from an empty result). ponytail: keys on
    the whole normalized message rather than an extracted sub-phrase ("폴더블 라인업" out of a
    longer sentence) -- good enough for a repeated exact question; upgrade to phrase extraction
    if partial-message reuse turns out to matter."""
    dataset = spec.get("dataset")
    ds = next((d for d in full_catalog if d["name"] == dataset), None)
    if ds is None or not spec.get("entities"):
        return
    known = set(_find_name_matches(message, ds["entities"]))
    new_entities = [e for e in spec["entities"] if e in ds["entities"] and e not in known]
    if not new_entities:
        return
    try:
        store.upsert_learned_alias(ctx.conn, dataset=dataset, dim="entities",
                                    phrase=_norm_for_match(message), values=new_entities)
    except Exception:  # noqa: BLE001 -- the learning loop must never break a real answer
        pass


def _answer_from_spec(ctx: ChatContext, spec: dict, path: str, spec_seconds: float,
                       started: float, message: str, *, explain_mode: str, timeout: float) -> dict:
    """Shared by every spec source (rule path, cache, a clarify-button click, and the LLM):
    given a resolved spec + which path produced it, validates/auto-corrects names, runs the
    query, and builds the final response including the type-aware fallback explanation.
    `message`: the raw user text, if any (used only to tell an entity the user explicitly typed
    from one the model/spec_patch added on its own -- see `resolve_entities`; "" for a
    spec_patch with no free-text message behind it, e.g. a plain suggestion click)."""
    full_catalog = store.catalog(ctx.conn)
    dataset_names = [d["name"] for d in full_catalog]

    def _early(question: str) -> dict:
        return _clarify(question, spec, started, path=path,
                         timings={**_ZERO_TIMINGS, "spec_seconds": spec_seconds})

    if spec.get("clarify"):
        return _early(str(spec["clarify"]))

    ds_match, ds_score = _resolve_one(spec.get("dataset", ""), dataset_names)
    if ds_match is None or (ds_match != spec.get("dataset") and ds_score < AUTOCORRECT_THRESHOLD):
        options = ", ".join(_top_candidates(spec.get("dataset", ""), dataset_names, MAX_CLARIFY_CANDIDATES))
        return _early(f"어떤 데이터셋을 말씀하신 건가요? 후보: {options or '(없음)'}")

    ds = next(d for d in full_catalog if d["name"] == ds_match)
    # only used by the fallback templates -- "" (omitted) when the dataset mixes units, since
    # tacking one metric's unit onto another's number would be misleading.
    unit = ds["units"][0] if len(ds["units"]) == 1 else ""
    metrics, metric_q, _ = resolve_names(spec.get("metrics"), ds["metrics"], required=True)
    entities, entity_q, entity_drop = resolve_entities(spec.get("entities"), ds["entities"], message)
    regions, _, region_drop = resolve_names(spec.get("regions"), ds["regions"], required=False)
    sources, _, source_drop = resolve_names(spec.get("sources"), ds["sources"], required=False)
    questions = metric_q + entity_q
    if questions:
        return _early(" / ".join(questions[:3]))
    drop_warnings = entity_drop + region_drop + source_drop

    raw_version = spec.get("version")
    if raw_version in (None, "", "latest", "history", "diff"):
        query_version = raw_version or "latest"
    else:
        match, score = _resolve_one(str(raw_version), ds["version_labels"])
        if match is None or (match != raw_version and score < AUTOCORRECT_THRESHOLD):
            options = ", ".join(ds["version_labels"]) or "(없음)"
            return _early(f"어떤 버전을 말씀하신 건가요? 후보: {options}")
        query_version = match

    if query_version == "diff" and not ds["version_labels"]:
        # a code-level safety net, not just a prompt hint: "diff" is meaningless without
        # several distinct version labels. A single named entity almost certainly meant
        # "history" (e.g. "모델A는 지난 스냅샷과 비교해 어때?") -- reinterpret instead of
        # bouncing a perfectly answerable question into a clarification.
        if len(entities) == 1:
            query_version = "history"
        else:
            return _early(
                "이 데이터셋은 버전이 하나뿐이라 여러 대상을 버전별로 비교할 수 없습니다. "
                "특정 대상을 하나만 알려주시면 그 값의 변화 이력을 보여드릴게요.")

    query_start = time.perf_counter()
    if query_version == "diff":
        from_label, from_err = _resolve_optional_label(spec.get("from"), ds["version_labels"])
        if from_err:
            return _early(from_err)
        to_label, to_err = _resolve_optional_label(spec.get("to"), ds["version_labels"])
        if to_err:
            return _early(to_err)
        group_by = [d for d in (spec.get("rows") or ["entity"]) if d] or ["entity"]
        # a SINGLE named entity narrows the comparison to just it; several (or none) means
        # "compare across entities" -- the whole point of diff mode -- so don't filter them out.
        diff_entity = entities[0] if len(entities) == 1 else None
        try:
            diff = store.version_diff(
                ctx.conn, ds_match, from_label=from_label, to_label=to_label,
                metric=metrics[0] if metrics else None, entity=diff_entity,
                region=regions[0] if regions else None, source=sources[0] if sources else None,
                period_from=spec.get("period_from") or None, period_to=spec.get("period_to") or None,
                group_by=group_by,
            )
        except ValueError as exc:
            return _early(f"버전을 비교할 수 없습니다: {exc}")
        table = {"columns": group_by + ["old", "new", "diff", "pct"],
                 "rows": [[r.get(d) for d in group_by] + [r["old"], r["new"], r["diff"], r["pct"]]
                          for r in diff["rows"]]}
        chart = _chart_from_diff(diff["rows"], group_by)
        template_fn = lambda: _template_diff(table["rows"], group_by, diff["from_label"],  # noqa: E731
                                              diff["to_label"], unit)
    elif query_version == "history":
        if not metrics or not entities:
            return _early("이력을 보려면 지표와 대상을 하나씩 알려주세요.")
        period = spec.get("period_to") or spec.get("period_from") or ""
        if not period:
            return _early("이력을 보려면 조회할 기간(period)을 알려주세요.")
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
        template_fn = lambda: _template_history(table["rows"], entities[0], unit)  # noqa: E731
    else:
        raw_rollup = spec.get("rollup")
        rollup = raw_rollup if raw_rollup in ("year", "quarter") else None
        agg = spec.get("agg") if spec.get("agg") in ("sum", "avg") else "sum"
        long_rows = store.query_observations(
            ctx.conn, dataset=ds_match, metrics=metrics or None, entities=entities or None,
            regions=regions or None, sources=sources or None,
            period_from=spec.get("period_from") or None, period_to=spec.get("period_to") or None,
            version=query_version, rollup=rollup, agg=agg,
        )
        if len(long_rows) > store.QUERY_ROW_CAP:
            return _early("조건에 맞는 데이터가 너무 많습니다. 질문을 더 구체적으로 해주세요.")
        drop_warnings += _missing_entities_warning(entities, long_rows)
        row_dims = [d for d in (spec.get("rows") or ["entity"]) if d]
        col_dims = [d for d in (spec.get("cols") or ["period"]) if d]
        table = store.to_wide(long_rows, row_dims, col_dims)
        chart = _chart_from_wide(table, spec.get("chart") or "table", len(row_dims))
        template_fn = lambda: _template_wide(table, row_dims, unit)  # noqa: E731
    query_seconds = round(time.perf_counter() - query_start, 2)

    explain_start = time.perf_counter()
    if explain_mode == "llm":
        sentence, explain_warnings = explain(ctx.llm, table, template_fn, timeout=timeout)
    else:
        sentence, explain_warnings = template_fn(), []
    explain_seconds = round(time.perf_counter() - explain_start, 2)

    return {"answer": sentence, "spec": spec, "table": table, "chart": chart,
            "warnings": drop_warnings + explain_warnings, "path": path, "options": None,
            "timings": {"spec_seconds": spec_seconds, "explain_seconds": explain_seconds,
                        "query_seconds": query_seconds},
            "seconds": round(time.perf_counter() - started, 2)}


def answer(ctx: ChatContext, message: str, history: list[dict] | None = None, *,
           explain_mode: str = "template", chat_timeout: float | None = None) -> dict:
    """Synchronous entry point for `POST /api/chat`. Never makes the automatic LLM spec call
    (Round 8: "a user must never block on the LLM") -- spec resolution tries only the
    deterministic rule path (`_rule_based_spec`, aliases included) and the spec cache
    (`chat_spec_cache`). When neither resolves the question, returns a clarify response whose
    `options` are clickable choices built from catalog/alias matches (or dataset-level
    `build_suggestions()` when nothing at all was recognized), always ending with an "AI에게
    물어보기" entry -- that queues a `POST /api/ask` job (`answer_via_llm`, `dataplat.ask`),
    the only way this chat ever reaches the LLM for spec resolution now. Picking one of the
    other options re-runs it directly via `answer_with_spec` (`path="button"`).

    `explain_mode`/`chat_timeout` still control the SEPARATE, OPT-IN explanation-sentence LLM
    call ("llm" instead of the default "template") -- unrelated to spec resolution, and left
    alone by this round's "no automatic LLM" rule since the caller must ask for it explicitly.
    Multi-turn messages (`history` non-empty) skip straight to the clarify/suggestions response
    too -- the rule path and cache are both single-turn only."""
    started = time.perf_counter()
    history = history or []
    timeout = DEFAULT_CHAT_TIMEOUT_SEC if chat_timeout is None else chat_timeout
    full_catalog = store.catalog(ctx.conn)

    spec_start = time.perf_counter()
    alias_maps = build_alias_maps(ctx.conn, ctx.aliases, full_catalog)
    spec = None if history else _rule_based_spec(message, full_catalog, alias_maps)
    if spec is not None:
        spec_seconds = round(time.perf_counter() - spec_start, 2)
        return _answer_from_spec(ctx, spec, "rule", spec_seconds, started, message,
                                  explain_mode=explain_mode, timeout=timeout)

    cached = None
    if not history:
        cache_key = _spec_cache_key(message)
        cat_version = store.catalog_version(ctx.conn)
        cached = store.get_cached_spec(ctx.conn, cache_key, cat_version)
    spec_seconds = round(time.perf_counter() - spec_start, 2)
    if cached is not None:
        return _answer_from_spec(ctx, cached, "cache", spec_seconds, started, message,
                                  explain_mode=explain_mode, timeout=timeout)

    options = _clarify_button_options(ctx.conn, message, full_catalog, alias_maps)
    question = ("질문을 정확히 이해하지 못했습니다. 아래에서 골라 주시거나 AI에게 직접 "
                "물어보세요 (몇 분 걸릴 수 있습니다).")
    return _clarify(question, None, started, path="none",
                     timings={**_ZERO_TIMINGS, "spec_seconds": spec_seconds}, options=options)


def answer_via_llm(ctx: ChatContext, message: str, history: list[dict] | None = None, *,
                    explain_mode: str = "template", chat_timeout: float | None = None) -> dict:
    """The LLM-backed spec resolution path -- used ONLY by the async `dataplat.ask` worker
    (never by the synchronous `answer()`/`POST /api/chat`, which must never block on the shared
    in-house endpoint's queue). Tries the spec cache first (a job can repeat a question nobody's
    asked live yet), then calls the LLM. Runs the Round 8 learning loop on a successful,
    non-empty answer: any entity the LLM resolved that the rule path/aliases couldn't find in
    `message` on their own is remembered as a learned alias."""
    started = time.perf_counter()
    history = history or []
    timeout = DEFAULT_CHAT_TIMEOUT_SEC if chat_timeout is None else chat_timeout
    full_catalog = store.catalog(ctx.conn)

    spec_start = time.perf_counter()
    cache_key = _spec_cache_key(message) if not history else None
    cat_version = store.catalog_version(ctx.conn) if cache_key is not None else None
    spec = store.get_cached_spec(ctx.conn, cache_key, cat_version) if cache_key is not None else None
    path = "cache"
    if spec is None:
        path = "llm"
        narrowed = narrow_catalog(ctx.conn, message, history)
        try:
            spec = ctx.llm.complete_json(spec_messages(message, history, narrowed), SPEC_SCHEMA,
                                          step="dataplat.spec", agent="dataplat", timeout=timeout)
        except LLMError as exc:
            timings = {**_ZERO_TIMINGS, "spec_seconds": round(time.perf_counter() - spec_start, 2)}
            if "timeout" in str(exc).lower():
                return _clarify(_LLM_BUSY_MESSAGE, None, started, path=path, timings=timings)
            return _clarify(f"질문을 이해하지 못했습니다. 다시 말씀해 주시겠어요? ({exc})",
                             None, started, path=path, timings=timings)
        if cache_key is not None:
            store.set_cached_spec(ctx.conn, cache_key, cat_version, spec)
    spec_seconds = round(time.perf_counter() - spec_start, 2)

    result = _answer_from_spec(ctx, spec, path, spec_seconds, started, message,
                                explain_mode=explain_mode, timeout=timeout)
    if path == "llm" and result.get("table") and result["table"].get("rows"):
        _learn_entities(ctx, message, spec, full_catalog)
    return result


def answer_with_spec(ctx: ChatContext, spec: dict, *, original_message: str | None = None,
                      explain_mode: str = "template", chat_timeout: float | None = None) -> dict:
    """Runs `spec` directly -- no rule/cache/LLM resolution -- for a clarify-button click or a
    precomputed suggestion (`path="button"`). `original_message`, when given (a button click,
    not a raw suggestion click), feeds the Round 8 learning loop the same way `answer_via_llm`
    does: the phrase that needed a button is remembered against the entities it resolved to."""
    started = time.perf_counter()
    timeout = DEFAULT_CHAT_TIMEOUT_SEC if chat_timeout is None else chat_timeout
    result = _answer_from_spec(ctx, spec, "button", 0.0, started, original_message or "",
                                explain_mode=explain_mode, timeout=timeout)
    if original_message and result.get("table") and result["table"].get("rows"):
        full_catalog = store.catalog(ctx.conn)
        _learn_entities(ctx, original_message, spec, full_catalog)
    return result
