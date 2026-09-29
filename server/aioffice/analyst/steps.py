"""LLM steps: extract / integrate / consolidate prompts + calls, number check, retrieval,
feedback examples. Named `tool_choice` is unverified against the in-house backend, so every
call here goes through `LLMClient.complete_json` (plain JSON in `content`), never `complete()`.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from ..llm.client import LLMClient

CLAIM_TYPES = ["fact", "forecast", "opinion"]
CLAIM_DIRECTIONS = ["up", "down", "flat", "none"]
RELATION_KINDS = ["new", "supports", "updates", "contradicts"]
LINK_KINDS = ["causes", "affects", "contradicts", "related"]

# --- extract ---------------------------------------------------------------------------------

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["claims"],
    "properties": {
        "claims": {"type": "array", "items": {
            "type": "object", "required": ["sids", "text"],
            "properties": {
                "sids": {"type": "array", "items": {"type": "string"}},
                "text": {"type": "string"},
                "type": {"type": "string", "enum": CLAIM_TYPES},
                "direction": {"type": "string", "enum": CLAIM_DIRECTIONS},
                "entities": {"type": "array", "items": {"type": "string"}},
                "metric": {"type": "string"}, "value": {"type": "string"},
                "unit": {"type": "string"}, "period": {"type": "string"},
            },
        }},
        "topics": {"type": "array", "items": {"type": "string"}},
    },
}

EXTRACT_SYSTEM_PROMPT = """You are a market-intelligence analyst's assistant.
Read the numbered sentences of ONE broker report below and extract 3 to 7 key claims.
Rules:
- Each claim cites only sentence ids that appear in the input, in field "sids" (e.g. ["S3"]).
- Do not invent numbers or facts that are not present in the cited sentences.
- Write claim text and topic labels in Korean.
- Answer with ONE JSON object and nothing else -- no explanation, no markdown fence.

JSON schema:
{"claims": [{"sids": ["S3"], "text": "한국어 한 문장 요약 (<=60자)", "type": "fact|forecast|opinion",
             "direction": "up|down|flat|none", "entities": ["Apple", "iPhone 18"],
             "metric": "string|null", "value": "string|null", "unit": "string|null", "period": "string|null"}],
 "topics": ["짧은 주제 라벨(한국어)", "..."]}
"""


def extract_messages(numbered_text: str) -> list[dict]:
    user = ("다음은 번호가 매겨진 리포트 문장입니다. 각 주장에 근거 문장 번호(sids)를 반드시 "
            "포함하세요.\n\n" + numbered_text)
    return [{"role": "system", "content": EXTRACT_SYSTEM_PROMPT}, {"role": "user", "content": user}]


def run_extract(llm: LLMClient, numbered_text: str, *, model: str | None = None,
                 max_tokens: int | None = None, task_id: int | None = None) -> dict:
    return llm.complete_json(extract_messages(numbered_text), EXTRACT_SCHEMA,
                              step="analyst.extract", agent="analyst", model=model,
                              max_tokens=max_tokens, task_id=task_id)


# --- number check + claim post-processing -----------------------------------------------------

_NUM_STRIP_RE = re.compile(r"[,%$\s]")


def normalize_number(value: Any) -> str:
    if value is None:
        return ""
    return _NUM_STRIP_RE.sub("", str(value))


def value_in_sentences(value: Any, sentences: list[str]) -> bool:
    norm = normalize_number(value)
    if not norm:
        return False
    return any(norm in normalize_number(s) for s in sentences)


def process_extract_claims(data: dict, sid_map: dict[str, str]) -> list[dict]:
    """Drops claims whose sids are all invalid; attaches cited-sentence quote text and the
    number_ok flag (kept, just flagged, per the work order)."""
    out = []
    for claim in data.get("claims", []):
        sids = [s for s in (claim.get("sids") or []) if s in sid_map]
        if not sids:
            continue
        cited = [sid_map[s] for s in sids]
        value = claim.get("value")
        number_ok = True if not value else value_in_sentences(value, cited)
        out.append({
            "sids": sids, "text": claim.get("text", ""), "type": claim.get("type"),
            "direction": claim.get("direction"), "entities": claim.get("entities") or [],
            "metric": claim.get("metric"), "value": value, "unit": claim.get("unit"),
            "period": claim.get("period"), "quote": " / ".join(cited), "number_ok": number_ok,
        })
    return out


# --- retrieval (candidate topics + feedback examples) -----------------------------------------


def _trigrams(text: str) -> Counter:
    text = "".join(text.split())
    if len(text) < 3:
        return Counter([text]) if text else Counter()
    return Counter(text[i:i + 3] for i in range(len(text) - 2))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _similarities(query: str, candidates: list[str], llm: LLMClient | None) -> list[float]:
    """Embedding + rerank when EMBED_BASE_URL is configured, else a character-trigram
    cosine fallback.

    ponytail: the trigram fallback is a cheap shortlist heuristic, not real semantic
    search -- fine for picking among a few dozen topics; drop it once an embedding
    endpoint is always available (EMBED_BASE_URL configured everywhere).
    """
    if not candidates:
        return []
    if llm is not None and llm.embeddings_enabled:
        if llm.rerank_enabled:
            return llm.rerank(query, candidates)
        vectors = llm.embed([query] + candidates)
        query_vec, cand_vecs = vectors[0], vectors[1:]
        return [sum(a * b for a, b in zip(query_vec, v)) for v in cand_vecs]
    query_tri = _trigrams(query)
    return [_cosine(query_tri, _trigrams(c)) for c in candidates]


def candidate_topics(report_claims: list[dict], topics: dict[str, dict],
                      claims_by_topic: dict[str, list[dict]], llm: LLMClient | None = None,
                      k: int = 8) -> list[dict]:
    if not topics:
        return []
    query = " ".join(c["text"] for c in report_claims)
    topic_ids = list(topics)
    texts = []
    for tid in topic_ids:
        topic = topics[tid]
        latest = claims_by_topic.get(tid, [])[:5]
        texts.append(" ".join([topic["name"], topic.get("summary") or ""] + [c["text"] for c in latest]))
    scores = _similarities(query, texts, llm)
    ranked = sorted(zip(topic_ids, scores), key=lambda p: p[1], reverse=True)[:k]
    out = []
    for tid, _score in ranked:
        topic = topics[tid]
        latest = claims_by_topic.get(tid, [])[:5]
        out.append({
            "id": tid, "name": topic["name"], "summary": topic.get("summary") or "",
            "latest_claims": [{"id": c["id"], "text": c["text"], "date": c["report_date"],
                                "broker": c["broker"]} for c in latest],
        })
    return out


def feedback_examples(report_claims: list[dict], feedback: list[dict], llm: LLMClient | None = None,
                       k: int = 3) -> list[dict]:
    if not feedback:
        return []
    query = " ".join(c["text"] for c in report_claims)
    texts = [f.get("text", "") for f in feedback]
    scores = _similarities(query, texts, llm)
    ranked = sorted(zip(feedback, scores), key=lambda p: p[1], reverse=True)[:k]
    return [f for f, _score in ranked]


# --- integrate ---------------------------------------------------------------------------------

INTEGRATE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["claims"],
    "properties": {
        "claims": {"type": "array", "items": {
            "type": "object", "required": ["cid", "topic", "relation"],
            "properties": {
                "cid": {"type": "string"},
                "topic": {},  # string (existing topic id) or {"new": "<name>"} -- checked by hand
                "relation": {"type": "string", "enum": RELATION_KINDS},
                "target": {"type": "string"},
            },
        }},
        "links": {"type": "array", "items": {
            "type": "object", "required": ["from", "to", "kind", "cid"],
            "properties": {
                "from": {"type": "string"}, "to": {"type": "string"},
                "kind": {"type": "string", "enum": LINK_KINDS}, "cid": {"type": "string"},
            },
        }},
    },
}

INTEGRATE_SYSTEM_PROMPT = """You are a market-intelligence analyst's assistant integrating new
claims into a persistent knowledge base of topics.
For each new claim (already given local ids c1, c2, ...), decide:
- "topic": the id of an existing topic that best fits, or {"new": "<Korean topic name, <=20 chars>"}
  if none of the given topics fit. Prefer an existing topic; create a new one only when necessary.
- "relation": one of "new" (first claim on this topic), "supports" (adds evidence to the same
  view), "updates" (same metric/subject with a newer value or a revised forecast/view), or
  "contradicts" (an opposing view on the same point from a DIFFERENT institution). Only for
  "updates"/"contradicts", also set "target" to the id of the existing claim being
  updated/contradicted (an id from the candidate topics' claim lists).
Also emit "links": relations between topics evidenced by this claim, "kind" one of
"causes"|"affects"|"contradicts"|"related", with "from"/"to" as topic ids (or the exact new
topic name you used above) and "cid" of the evidencing claim.
Answer with ONE JSON object and nothing else -- no explanation, no markdown fence.

JSON schema:
{"claims": [{"cid": "c1", "topic": "t003" | {"new": "짧은 주제명"},
             "relation": "new|supports|updates|contradicts", "target": "<existing claim id>|null"}],
 "links": [{"from": "t003", "to": "t007", "kind": "causes|affects|contradicts|related", "cid": "c1"}]}
"""


def integrate_messages(new_claims: list[dict], candidates: list[dict], feedback: list[dict],
                        report_date: str) -> list[dict]:
    claims_lines = [f"[{c['local_id']}] ({c.get('type')}/{c.get('direction')}) {c['text']}"
                     for c in new_claims]
    topic_blocks = []
    for t in candidates:
        header = f"topic {t['id']} \"{t['name']}\": {t['summary']}"
        claim_lines = [f"  - [{lc['id']}] {lc['date']} {lc['broker']}: {lc['text']}"
                       for lc in t["latest_claims"]]
        topic_blocks.append("\n".join([header] + claim_lines))
    topics_block = "\n".join(topic_blocks) or "(기존 주제 없음)"
    feedback_lines = [f"- 이전 교정 예시: \"{f.get('text', '')}\" -> topic {f.get('corrected_topic_id', '')}"
                       for f in feedback]
    feedback_block = "\n".join(feedback_lines) or "(없음)"
    user = (
        f"오늘 리포트 날짜: {report_date}\n\n"
        f"새 주장 목록:\n" + "\n".join(claims_lines) + "\n\n"
        f"기존 주제 후보:\n{topics_block}\n\n"
        f"참고용 이전 교정 예시:\n{feedback_block}\n"
    )
    return [{"role": "system", "content": INTEGRATE_SYSTEM_PROMPT}, {"role": "user", "content": user}]


def run_integrate(llm: LLMClient, new_claims: list[dict], candidates: list[dict],
                   feedback: list[dict], report_date: str, *, model: str | None = None,
                   max_tokens: int | None = None, task_id: int | None = None) -> dict:
    return llm.complete_json(
        integrate_messages(new_claims, candidates, feedback, report_date), INTEGRATE_SCHEMA,
        step="analyst.integrate", agent="analyst", model=model, max_tokens=max_tokens,
        task_id=task_id,
    )


def validate_integrate_shape(processed_claims: list[dict], data: dict) -> None:
    """Semantic checks schema validation alone can't catch: every claim must get a decision
    with a resolvable topic reference. Raises ValueError if not -- the caller retries once,
    then falls back to `store.fallback_integrate_result` (see the work order)."""
    if not isinstance(data, dict):
        raise ValueError("integrate 결과가 JSON 객체가 아닙니다")
    decisions = {d.get("cid"): d for d in data.get("claims", [])}
    for c in processed_claims:
        decision = decisions.get(c["local_id"])
        if decision is None:
            raise ValueError(f"claim {c['local_id']}에 대한 결정이 없습니다")
        topic = decision.get("topic")
        if not (isinstance(topic, str) or (isinstance(topic, dict) and "new" in topic)):
            raise ValueError(f"claim {c['local_id']}의 topic 형식이 올바르지 않습니다")


# --- consolidate ---------------------------------------------------------------------------

CONSOLIDATE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["topics"],
    "properties": {
        "topics": {"type": "array", "items": {
            "type": "object", "required": ["id", "summary", "trend"],
            "properties": {
                "id": {"type": "string"}, "summary": {"type": "string"},
                "trend": {"type": "string", "enum": ["up", "down", "flat", "mixed"]},
            },
        }},
        "relations": {"type": "array", "items": {
            "type": "object", "required": ["from", "to", "kind", "why"],
            "properties": {
                "from": {"type": "string"}, "to": {"type": "string"},
                "kind": {"type": "string", "enum": LINK_KINDS}, "why": {"type": "string"},
            },
        }},
    },
}

CONSOLIDATE_SYSTEM_PROMPT = """You are a market-intelligence analyst's assistant. For each topic
below, write an updated Korean summary (2-3 sentences, the current judgement, citing the source
institution(s) and any key numbers) and a trend ("up"|"down"|"flat"|"mixed", the overall
direction of its valid claims). Ground everything only in the claims given, never invent
numbers. Optionally also emit "relations" you notice between the topics given
("kind": "causes"|"affects"|"contradicts"|"related", "why" <=40 Korean characters).
Answer with ONE JSON object and nothing else -- no explanation, no markdown fence.

JSON schema:
{"topics": [{"id": "t003", "summary": "...", "trend": "up|down|flat|mixed"}],
 "relations": [{"from": "t003", "to": "t007", "kind": "causes|affects|contradicts|related", "why": "..."}]}
"""


def consolidate_messages(topics_info: list[dict]) -> list[dict]:
    blocks = []
    for t in topics_info:
        claim_lines = [
            "  - {date} {broker} ({state}): {text}".format(
                date=c["date"], broker=c["broker"], text=c["text"],
                state="유효" if c["valid"] else "무효")
            for c in t["claims"]
        ]
        header = f"topic {t['id']} \"{t['name']}\" (이전 요약: {t.get('previous_summary') or '(없음)'})"
        blocks.append("\n".join([header] + claim_lines))
    user = "다음 주제들의 최신 판단을 갱신하세요:\n\n" + "\n\n".join(blocks)
    return [{"role": "system", "content": CONSOLIDATE_SYSTEM_PROMPT}, {"role": "user", "content": user}]


def run_consolidate(llm: LLMClient, topics_info: list[dict], *, model: str | None = None,
                     max_tokens: int | None = None, task_id: int | None = None) -> dict:
    return llm.complete_json(
        consolidate_messages(topics_info), CONSOLIDATE_SCHEMA,
        step="analyst.consolidate", agent="analyst", model=model, max_tokens=max_tokens,
        task_id=task_id,
    )
