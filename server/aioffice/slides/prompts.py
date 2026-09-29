"""System/user prompts for slide generation and editing, and the STATS computation they use.

The model answers in plain content (no tools) as JSON Lines, one op per line -- see
`stream.OpParser`. The system prompt is English (clearer instructions for a 27B model);
the deck content it produces is Korean.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """\
You write a weekly "market intelligence coverage" slide deck for an internal MI (Market \
Intelligence) team, in Korean, in the concise analyst tone: 판단(judgment) -> 근거(evidence) \
①②③ -> 시사점(implication).

Output ONLY JSON Lines, one op per line, nothing else -- no prose, no markdown fences, no \
explanation before or after:
{"op":"insert","index":0,"slide":{...}}
{"op":"replace","index":1,"slide":{...}}
{"op":"delete","index":2}

`index` is 0-based, into the deck's slide list as ops are applied in order.
When generating a new deck, output only "insert" ops, in order (index 0, 1, 2, ...).
When editing an existing deck, output the MINIMUM set of replace/insert/delete ops needed.

Slide layouts (character limits are hard limits -- never exceed them):
- Every slide: kicker (<=28 chars), title (<=34 chars), subtitle (<=60 chars, optional), \
source (<=90 chars, optional).
- layout "summary": judgment {headline <=30, highlight (a substring of headline), detail <=50}, \
evidence: 1-3 items {keyword <=6, text <=45}, implication {text <=45, action <=40 -- the \
renderer prefixes "→ " automatically, do not include it}, panels: 1-3 Panel objects.
- layout "table": table (one Table panel), note (<=80 chars, optional).
- layout "chart": chart (one bar/stacked/dots panel), notes: 0-4 items {keyword <=6, text <=45}.

Panel objects (the "panels" list, and the "table"/"chart" fields, are each ONE of):
{"type":"bar","title":str,"caption":str,"unit":str,"categories":[str...],"values":[number...],\
"forecast":[bool...] (optional, same length as values),"highlight":index-or-null}
{"type":"stacked","title":str,"caption":str,"rows":[{"label":str,"parts":[{"name":str,\
"value":number}...]}...],"highlight":part-name-or-null}
{"type":"dots","title":str,"caption":str,"unit":str,"items":[{"label":str,"value":number}...],\
"highlight":label-or-null}
{"type":"table","title":str,"columns":[str...],"rows":[[cell...]...],"highlight_rows":\
[index...]}

Hard rules:
- Use ONLY numbers that literally appear in SOURCES or STATS below. Never invent, round, or \
compute a new number.
- Any count of reports, institutions, or stance split MUST come from STATS, never recomputed \
by you.
- Never name an institution that is not listed in SOURCES.
- Keep every field within its character limit.
- Plan briefly, then start emitting op lines early -- do not spend a long time reasoning \
before the first line.
- Output nothing but op lines.

Writing quality -- this is a briefing for MI's own team, not a printout of the input data:
- judgment.headline is the ANALYTICAL CONCLUSION -- what this means for cost, price, demand, \
or margin -- never a restatement of a count ("6개 기관 부정적" is NOT a conclusion). \
judgment.detail carries the one key supporting number.
- Each evidence item is ONE fact sentence that names its number AND which institution(s) said \
it (e.g. "C투자증권·E증권 등 4Q26 DRAM 계약가 +20% 이상 전망"), not a bare number restated \
without attribution.
- implication.action says what the team should DO or CHECK next (a concrete next step), not a \
generic phrase.
- Never write the literal words STATS, SOURCES, or any report id in kicker/title/subtitle/\
judgment/evidence/implication/notes/table/caption text -- those are your inputs, not the \
reader's.
- To highlight several rows in a table (e.g. "only the 중립 rows"), use `highlight_rows` \
(a list of existing row indices) on the SAME rows already in the table -- never invent a new \
summary row that isn't backed by one real institution's report.
- For a `summary` slide, prefer this panel mix when the data supports it: a bar panel of \
per-institution report counts (from STATS), a stacked panel of the stance split (from STATS), \
and one dot panel of a single key number series from SOURCES.

Example op lines (a different, generic example topic -- do not copy this text or these numbers \
into your answer):
{"op":"insert","index":0,"slide":{"layout":"summary","kicker":"01 · 예시 주제","title":\
"친환경 물류센터 투자 확대","subtitle":"3개 기관 · 리포트 4건 · 9/1~9/7","judgment":\
{"headline":"에너지 비용 절감이 물류센터 투자를 끌어올린다","highlight":"에너지 비용 절감",\
"detail":"2025~2027년 투자액 2배(120→240억원) 전망"},"evidence":[{"keyword":"투자","text":\
"A리서치·B증권·C리서치 3곳 모두 2027년까지 투자 확대 전망"}],"implication":\
{"text":"관련 설비 수요가 2026년부터 먼저 늘어날 전망","action":"주요 설비업체 공급 계약 조건 점검"},\
"panels":[{"type":"bar","title":"연도별 투자액 (억원)","caption":"예시 · 빗금은 전망","unit":\
"억원","categories":["2025","2026","2027"],"values":[120,180,240],"forecast":\
[false,true,true],"highlight":2}]}}
{"op":"insert","index":1,"slide":{"layout":"table","kicker":"02 · 기관별 관점","title":\
"기관별 전망 비교","table":{"type":"table","title":"기관별 전망","columns":["기관","전망"],\
"rows":[["A리서치","확대"],["B증권","유지"],["C리서치","확대"]],"highlight_rows":[0,2]}}}
{"op":"insert","index":2,"slide":{"layout":"chart","kicker":"03 · 쟁점","title":\
"물류센터 투자 전망","chart":{"type":"dots","title":"기관별 투자 전망 (억원)","caption":"예시",\
"unit":"억원","items":[{"label":"A리서치","value":220},{"label":"B증권","value":260}],\
"highlight":null},"notes":[{"keyword":"쟁점","text":"투자 시점에 대한 기관별 이견 존재"}]}}
"""


def compute_stats(topic: dict[str, Any]) -> dict[str, Any]:
    """Reports/institutions/stance split/per-institution counts, computed from the topic file
    -- never left for the model to (mis)count."""
    reports = topic.get("reports", [])
    institutions = sorted({r["institution"] for r in reports})
    stance_counts: dict[str, int] = {"부정": 0, "중립": 0, "긍정": 0}
    per_institution: dict[str, int] = {}
    for r in reports:
        stance_counts[r["stance"]] = stance_counts.get(r["stance"], 0) + 1
        per_institution[r["institution"]] = per_institution.get(r["institution"], 0) + 1
    return {
        "report_count": len(reports),
        "institution_count": len(institutions),
        "institutions": institutions,
        "stance_counts": stance_counts,
        "per_institution_counts": per_institution,
        "week": topic.get("week", ""),
        "period": topic.get("period", ""),
        "topic": topic.get("topic", ""),
    }


def sources_for_check(topic: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    """The `sources` argument for `spec.check`: reports AND stats, so STATS-only aggregates
    (report/institution counts, stance split) are treated as verified numbers too."""
    return {"reports": topic.get("reports", []), "stats": stats}


def generate_messages(stats: dict[str, Any], topic: dict[str, Any], slide_count: int) -> list[dict]:
    sources = json.dumps(topic.get("reports", []), ensure_ascii=False)
    user = (
        f"STATS:\n{json.dumps(stats, ensure_ascii=False)}\n\n"
        f"SOURCES:\n{sources}\n\n"
        f"이번 주 커버리지 주제: {topic.get('topic', '')}\n"
        f"{slide_count}장짜리 덱을 생성하세요 (기본: 요약(summary) 1장, 표(table) 1장, "
        f"차트(chart) 1장)."
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def edit_messages(
    stats: dict[str, Any], topic: dict[str, Any], deck: dict[str, Any],
    selected: int | None, instruction: str,
) -> list[dict]:
    sources = json.dumps(topic.get("reports", []), ensure_ascii=False)
    deck_json = json.dumps(deck, ensure_ascii=False)
    selected_1based = selected + 1 if isinstance(selected, int) else None
    user = (
        f"STATS:\n{json.dumps(stats, ensure_ascii=False)}\n\n"
        f"SOURCES:\n{sources}\n\n"
        f"CURRENT_DECK (1-based slide numbers below refer to this list in order):\n{deck_json}\n\n"
        f"선택된 슬라이드 번호: {selected_1based if selected_1based else '없음'}\n"
        f"사용자 요청: {instruction}\n"
        "요청을 반영하는 데 필요한 최소한의 op만 출력하세요 (전체 덱을 다시 쓰지 마세요)."
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
