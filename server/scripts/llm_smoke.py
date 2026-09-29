#!/usr/bin/env python3
"""Smoke-check the AI Office LLM, embedding, and reranker endpoints."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


TINY_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}

# Used when aioffice.llm.schemas is unavailable (for example, from repo root).
INLINE_ISSUES = {
    "type": "object",
    "required": ["issues"],
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "issue_label", "claim", "entities", "directions",
                    "supporting_points", "key_numbers", "page_refs", "confidence",
                ],
                "properties": {
                    "issue_label": {"type": "string"},
                    "claim": {"type": "string"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "directions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["target", "direction"],
                            "properties": {
                                "target": {"type": "string"},
                                "direction": {"type": "string", "enum": ["▲", "▼", "–"]},
                                "horizon": {"type": "string"},
                            },
                        },
                    },
                    "supporting_points": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["text", "kind"],
                            "properties": {
                                "text": {"type": "string"},
                                "kind": {
                                    "type": "string",
                                    "enum": ["data", "company_guidance", "channel_check", "supply_chain", "assumption", "estimate", "comparison"],
                                },
                                "page_refs": {"type": "array", "items": {"type": "integer"}},
                                "key_numbers": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["value", "what"],
                                        "properties": {
                                            "value": {"type": "number"},
                                            "unit": {"type": "string"},
                                            "what": {"type": "string"},
                                            "page": {"type": "integer"},
                                        },
                                    },
                                },
                                "asset_refs": {"type": "array", "items": {"type": "integer"}},
                            },
                        },
                    },
                    "key_numbers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["value", "what"],
                            "properties": {
                                "value": {"type": "number"},
                                "unit": {"type": "string"},
                                "what": {"type": "string"},
                                "page": {"type": "integer"},
                            },
                        },
                    },
                    "page_refs": {"type": "array", "items": {"type": "integer"}},
                    "asset_refs": {"type": "array", "items": {"type": "integer"}},
                    "confidence": {"type": "string", "enum": ["high", "med", "low"]},
                },
            },
        }
    },
}

try:
    server_root = str(Path(__file__).resolve().parents[1])
    if server_root not in sys.path:
        sys.path.insert(0, server_root)
    from aioffice.llm.schemas import ISSUES as ISSUES_SCHEMA
    from aioffice.llm.schemas import validate as schema_validate

    SCHEMA_SOURCE = "aioffice.llm.schemas.ISSUES"
except Exception:
    ISSUES_SCHEMA = INLINE_ISSUES
    SCHEMA_SOURCE = "inline fallback"

    def schema_validate(data: Any, schema: dict[str, Any], path: str = "$") -> None:
        """Validate the schema subset used by the in-house ISSUES schema."""
        types = {
            "object": dict, "array": list, "string": str,
            "number": (int, float), "integer": int, "boolean": bool,
        }
        expected = schema.get("type")
        if expected:
            if expected in ("integer", "number") and isinstance(data, bool):
                raise ValueError(f"{path}: expected {expected}, got bool")
            if not isinstance(data, types[expected]):
                raise ValueError(f"{path}: expected {expected}, got {type(data).__name__}")
        if "enum" in schema and data not in schema["enum"]:
            raise ValueError(f"{path}: {data!r} not in {schema['enum']}")
        if expected == "object":
            required = set(schema.get("required", []))
            for key in required:
                if key not in data:
                    raise ValueError(f"{path}: missing required field {key!r}")
            for key, sub in schema.get("properties", {}).items():
                if key in data and (data[key] is not None or key in required):
                    schema_validate(data[key], sub, f"{path}.{key}")
        elif expected == "array" and "items" in schema:
            for index, item in enumerate(data):
                schema_validate(item, schema["items"], f"{path}[{index}]")


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    except (OSError, UnicodeError):
        pass
    return values


def joined(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def without_v1(base: str) -> str:
    base = base.rstrip("/")
    return base[:-3] if base.lower().endswith("/v1") else base


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def short(value: Any, limit: int = 260) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def response_error(status: int, body: Any) -> str:
    if isinstance(body, dict):
        body = body.get("error", body)
    return short(f"HTTP {status}: {body}")


def message_and_thinking(body: Any) -> tuple[dict[str, Any], bool]:
    message = {}
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        pass
    content = message.get("content") or ""
    thinking = message.get("reasoning_content") is not None or bool(re.search(r"<think(?:ing)?\b", str(content), re.I))
    return message, thinking


def tool_arguments(body: Any) -> tuple[Any, bool, str | None]:
    try:
        calls = body["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            return None, False, "tool_calls absent"
        raw = calls[0]["function"]["arguments"]
        return (json.loads(raw) if isinstance(raw, str) else raw), True, None
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return None, False, short(exc)


def usage_of(body: Any) -> dict[str, Any]:
    return body.get("usage") or {} if isinstance(body, dict) else {}


def finish_of(body: Any) -> Any:
    try:
        return body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None


def extract_model_ids(body: Any) -> list[str]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return []
    return [str(item["id"]) for item in body["data"] if isinstance(item, dict) and item.get("id")]


def find_context_fields(value: Any) -> dict[str, Any]:
    names = {"max_model_len", "context_length", "max_position_embeddings", "max_seq_len", "model_max_length"}
    found: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in names:
                found[key] = item
            elif isinstance(item, (dict, list)):
                found.update(find_context_fields(item))
    elif isinstance(value, list):
        for item in value:
            found.update(find_context_fields(item))
    return found


def extract_token_count(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("count", "token_count", "num_tokens", "input_tokens", "prompt_tokens"):
            count = value.get(key)
            if isinstance(count, int):
                return count
        for key in ("tokens", "token_ids", "input_ids", "ids"):
            if isinstance(value.get(key), list):
                return len(value[key])
        for item in value.values():
            count = extract_token_count(item)
            if count is not None:
                return count
    elif isinstance(value, list):
        return len(value)
    return None


def embeddings_of(body: Any) -> list[list[float]]:
    if isinstance(body, dict):
        rows = body.get("data", body.get("embeddings", []))
    else:
        rows = body
    if not isinstance(rows, list):
        return []
    if rows and isinstance(rows[0], (int, float)):
        rows = [rows]
    rows = sorted(rows, key=lambda row: row.get("index", 0) if isinstance(row, dict) else 0)
    result = []
    for row in rows:
        vector = row.get("embedding") if isinstance(row, dict) else row
        if isinstance(vector, list):
            try:
                result.append([float(value) for value in vector])
            except (TypeError, ValueError):
                return []
    return result


def cosine(left: list[float], right: list[float]) -> float | None:
    if not left or len(left) != len(right):
        return None
    denom = math.sqrt(sum(x * x for x in left) * sum(y * y for y in right))
    return sum(x * y for x, y in zip(left, right)) / denom if denom else None


def rerank_scores(body: Any, count: int) -> list[float] | None:
    rows = body
    if isinstance(body, dict):
        for key in ("results", "data", "scores", "rankings"):
            if key in body:
                rows = body[key]
                break
    if isinstance(rows, list):
        if all(isinstance(row, (int, float)) for row in rows) and len(rows) == count:
            return [float(row) for row in rows]
        scores: list[float | None] = [None] * count
        for order, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            score = next((row[key] for key in ("relevance_score", "score", "similarity") if isinstance(row.get(key), (int, float))), None)
            index = row.get("index", order)
            if score is not None and isinstance(index, int) and 0 <= index < count:
                scores[index] = float(score)
        if all(score is not None for score in scores):
            return [float(score) for score in scores]
    return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    parser.add_argument("--skip-context", action="store_true")
    parser.add_argument("--skip-concurrency", action="store_true")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--out", type=Path, default=Path.cwd() / "llm_smoke_result.json")
    try:
        args = parser.parse_args()
    except SystemExit:
        return 0

    config = parse_env(args.env)
    config.update(os.environ)
    llm_base = config.get("LLM_BASE_URL", "").strip().rstrip("/")
    llm_key = config.get("LLM_API_KEY", "").strip()
    embed_base = config.get("EMBED_BASE_URL", llm_base).strip().rstrip("/")
    embed_key = config.get("EMBED_API_KEY", llm_key).strip()
    rerank_base = config.get("RERANK_BASE_URL", embed_base).strip().rstrip("/")
    rerank_key = config.get("RERANK_API_KEY", embed_key).strip()
    model = config.get("LLM_MODEL_DEFAULT", "").strip()
    embed_model = config.get("EMBED_MODEL", "BAAI-bge-m3").strip()
    rerank_model = config.get("RERANK_MODEL", "BAAI-bge-reranker-v2-m3").strip()
    keys_to_redact = [key for key in (llm_key, embed_key, rerank_key) if key]

    raw_results: dict[str, Any] = {}
    raw_lock = threading.Lock()
    unreachable_origins: dict[str, str] = {}
    origin_lock = threading.Lock()
    results: list[dict[str, Any]] = []
    recommendations: dict[str, Any] = {}
    context_fields: dict[str, Any] = {}
    ko_tokens_per_char: float | None = None
    largest_context: int | None = None

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            for secret in keys_to_redact:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: ("[REDACTED]" if re.search(r"api.?key|authorization|(^token$|access.?token|refresh.?token)|secret|password", str(key), re.I) else clean(item)) for key, item in value.items()}
        return value

    def request(tag: str, method: str, url: str, key: str, payload: Any = None):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with origin_lock:
            previous_error = unreachable_origins.get(origin)
        if previous_error:
            raise RuntimeError(f"origin unavailable after earlier connection failure: {previous_error}")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            response = httpx.request(method, url, json=payload, headers=headers, timeout=args.timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # ponytail: cache a refused origin for this run; route-level retries add no signal while its host is unreachable.
            with origin_lock:
                unreachable_origins[origin] = short(clean(str(exc)), 160)
            raise
        try:
            body = response.json()
        except ValueError:
            body = response.text
        with raw_lock:
            raw_results[tag] = clean({"status_code": response.status_code, "body": body})
        return response, body

    def run_check(name: str, callback):
        started = time.perf_counter()
        try:
            item = clean(callback())
            item.setdefault("status", "OK")
            item["name"] = name
            item["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        except Exception as exc:
            item = {
                "name": name, "status": "FAIL",
                "error": short(clean(str(exc))),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        results.append(item)
        return item

    # Synthetic broker excerpt is deliberately local and contains no real report data.
    excerpt = """Hanbit Securities | Global Smartphone & Apple Supply Chain | 2026-09-28
아이폰 생산 조정과 스마트폰 수요: 9월 채널 점검 (가상 분석 자료)

투자의견: 중립. 2026년 하반기 iPhone 출하량은 초기 계획보다 낮아질 수 있으나, 고가 모델 비중과 부품 단가가 손익 하락을 일부 상쇄할 전망이다. 중국과 북미의 교체 수요는 엇갈리며, 10월 신제품 출시 이후 판매 데이터를 확인해야 한다. 아래 수치는 모두 smoke test 전용 가상 추정치다.

| 지표 | 2025A | 2026E | 변화 | 방향 |
|---|---:|---:|---:|:---:|
| 글로벌 스마트폰 출하량 (백만 대) | 1,245 | 1,218 | -2.2% | ▼ |
| iPhone 조립 주문 (백만 대) | 82.0 | 76.5 | -6.7% | ▼ |
| 프리미엄 기기 평균판매가격 (달러) | 812 | 846 | +4.2% | ▲ |
| 메모리 부품 원가 지수 (2025=100) | 100 | 108 | +8.0% | ▲ |
| 채널 재고 (주) | 5.8 | 6.4 | +0.6주 | ▲ |

The survey covers 24 distributors and 11 component suppliers through September 25. North American sell-through was flat year over year, while Greater China smartphone demand declined 5% in August. Suppliers report that the latest iPhone mix is shifting toward Pro models, but total build plans remain below the June forecast. These are channel checks, not company guidance.

핵심 근거: ① 9월 1~20일 중국 온라인 스마트폰 판매는 전년 동기 대비 5% 감소했다. ② 주요 조립사의 4분기 iPhone 부품 발주 추정치는 7,650만 대로 기존 8,200만 대에서 하향됐다. ③ Pro 모델 비중은 2025년 4분기 41%에서 2026년 4분기 46%로 상승할 것으로 가정했다. ④ 메모리와 디스플레이 가격 상승은 원가에 부담이지만 프리미엄 믹스가 일부 방어한다.

English summary: We lower our hypothetical 2026 smartphone unit forecast but raise the premium mix assumption. A 6.7% cut to iPhone assembly orders is a negative demand signal (▼); a 4.2% increase in average selling price is a positive mix signal (▲). The net direction for total units remains ▼, while revenue impact is uncertain (–) until launch-week sell-through is observed. Watch points include October preorders, December channel inventory, and the next supplier shipment update.

해석 시 주의: 보고서 발췌에는 실제 기업의 확정 가이던스가 없으며, 표의 숫자는 테스트용으로 생성했다. 주문량과 소비자 판매량은 서로 다른 지표다. 주문 하향만으로 최종 출하량을 단정하지 않고, 재고와 신제품 초기 판매를 함께 확인한다. 관측 기간은 2026년 9월이며, 2025A는 비교 기준, 2026E는 추정치다. 환율과 계절성은 단순화했다."""
    if len(excerpt) < 2300:
        excerpt += "\n" + ("이 자료는 가상의 채널 확인과 추정치를 구분하며, 실제 실적과 회사 가이던스를 의미하지 않는다. " * 18)
    excerpt = excerpt[:2600]

    def headers_for_model() -> dict[str, Any]:
        models_url = joined(llm_base, "/models")
        response, body = request("llm.models", "GET", models_url, llm_key)
        if response.status_code >= 400:
            raise RuntimeError(response_error(response.status_code, body))
        ids = extract_model_ids(body)
        fields = find_context_fields(body)
        context_fields.update(fields)
        nonlocal model
        if not model and ids:
            model = ids[0]
        return {"status": "OK", "models": ids, "context_fields": fields, "using_model": model or None}

    run_check("1 모델 목록", headers_for_model)

    def chat_payload(messages: list[dict[str, str]], max_tokens: int | None = None, thinking: bool = False):
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    def call_chat(tag: str, messages: list[dict[str, str]], max_tokens: int | None = None, thinking: bool = False):
        return request(tag, "POST", joined(llm_base, "/chat/completions"), llm_key, chat_payload(messages, max_tokens, thinking))

    plain_prompt = "한 문장으로 답하세요. 오늘 확인해야 할 스마트폰 수요 지표는 무엇인가요?"

    def plain_chat():
        response, body = call_chat("llm.plain_chat", [{"role": "user", "content": plain_prompt}])
        if response.status_code >= 400:
            raise RuntimeError(response_error(response.status_code, body))
        message, thinking = message_and_thinking(body)
        if not message:
            raise RuntimeError("response had no assistant message")
        return {"status": "OK", "finish_reason": finish_of(body), "usage": usage_of(body), "thinking": thinking, "content_excerpt": short(message.get("content") or "", 160)}

    run_check("2 일반 채팅", plain_chat)

    baseline_latency: float | None = None
    thinking_accepted = False

    def disabled_chat():
        nonlocal baseline_latency, thinking_accepted
        started = time.perf_counter()
        response, body = call_chat("llm.thinking_disabled", [{"role": "user", "content": plain_prompt}], thinking=True)
        latency = (time.perf_counter() - started) * 1000
        baseline_latency = next((item["latency_ms"] for item in results if item["name"] == "2 일반 채팅"), None)
        if response.status_code >= 400:
            return {"status": "WARN", "accepted": False, "error": response_error(response.status_code, body), "latency_ms": round(latency, 1)}
        thinking_accepted = True
        message, thinking = message_and_thinking(body)
        if not message:
            thinking = None
        return {
            "status": "OK" if message and not thinking else "WARN", "accepted": True,
            "thinking": thinking, "finish_reason": finish_of(body),
            "latency_ms": round(latency, 1),
            "latency_delta_ms": round(latency - baseline_latency, 1) if baseline_latency is not None else None,
            "error": None if message else "response had no assistant message",
        }

    check3 = run_check("3 thinking 비활성화", disabled_chat)
    thinking_accepted = bool(check3.get("accepted"))

    tiny_tools = [{"type": "function", "function": {"name": "submit", "description": "Submit JSON", "parameters": TINY_SCHEMA}}]
    tiny_messages = [{"role": "system", "content": "Call the submit function with answer set to ok."}, {"role": "user", "content": "Return answer=ok."}]

    def tiny_tool_call(tag: str, choice: Any, response_format: Any = None):
        payload: dict[str, Any] = {"model": model, "messages": tiny_messages}
        if choice is not None:
            payload.update({"tools": tiny_tools, "tool_choice": choice})
        if response_format is not None:
            payload["response_format"] = response_format
            payload["messages"] = [{"role": "system", "content": "Return one JSON object matching the schema."}, {"role": "user", "content": "Set answer to ok."}]
        response, body = request(tag, "POST", joined(llm_base, "/chat/completions"), llm_key, payload)
        return response, body

    def named_tool():
        response, body = tiny_tool_call("llm.tiny_named_tool", {"type": "function", "function": {"name": "submit"}})
        if response.status_code >= 400:
            raise RuntimeError(response_error(response.status_code, body))
        data, present, error = tool_arguments(body)
        valid = False
        if present:
            try:
                schema_validate(data, TINY_SCHEMA)
                valid = True
            except Exception as exc:
                error = short(exc)
        return {"status": "OK" if present and valid else "FAIL", "tool_call": present, "json_valid": present, "schema_valid": valid, "error": error, "finish_reason": finish_of(body), "usage": usage_of(body)}

    check4 = run_check("4 named tool_choice 소형 스키마", named_tool)

    issues_prompt = "아래 가상 증권사 리포트 발췌에서 투자 이슈를 추출하세요. ISSUES 스키마와 submit 도구를 사용하고, 근거와 숫자·방향을 보존하세요.\n\n" + excerpt
    issues_messages = [{"role": "system", "content": "Extract evidence-grounded investment issues. Use submit exactly once."}, {"role": "user", "content": issues_prompt}]
    issues_tools = [{"type": "function", "function": {"name": "submit", "description": "Submit the answer as a single JSON object.", "parameters": ISSUES_SCHEMA}}]

    def issues_payload() -> dict[str, Any]:
        payload = {"model": model, "messages": issues_messages, "tools": issues_tools, "tool_choice": {"type": "function", "function": {"name": "submit"}}}
        if thinking_accepted:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    def call_issues(tag: str):
        return request(tag, "POST", joined(llm_base, "/chat/completions"), llm_key, issues_payload())

    def full_schema_tool():
        response, body = call_issues("llm.full_schema_tool")
        if response.status_code >= 400:
            raise RuntimeError(response_error(response.status_code, body))
        data, present, error = tool_arguments(body)
        json_valid = present and data is not None
        schema_valid = False
        if json_valid:
            try:
                schema_validate(data, ISSUES_SCHEMA)
                schema_valid = True
            except Exception as exc:
                error = short(exc)
        issue_count = len(data.get("issues", [])) if isinstance(data, dict) and isinstance(data.get("issues"), list) else None
        return {
            "status": "OK" if present and json_valid and schema_valid else "FAIL",
            "tool_call": present, "json_valid": json_valid, "schema_valid": schema_valid,
            "schema_source": SCHEMA_SOURCE, "first_schema_error": error if json_valid and not schema_valid else None,
            "issue_count": issue_count, "finish_reason": finish_of(body),
            "completion_tokens": usage_of(body).get("completion_tokens"),
        }

    check5 = run_check("5 ISSUES 스키마", full_schema_tool)

    def fallback_modes():
        modes = [
            ("required", "required", None),
            ("auto", "auto", None),
            ("json_schema", None, {"type": "json_schema", "json_schema": {"name": "submit", "schema": TINY_SCHEMA}}),
        ]
        findings = {}
        for name, choice, fmt in modes:
            try:
                response, body = tiny_tool_call(f"llm.fallback_{name}", choice, fmt)
                if response.status_code >= 400:
                    findings[name] = {"works": False, "error": response_error(response.status_code, body)}
                    continue
                if name == "json_schema":
                    message, _ = message_and_thinking(body)
                    content = message.get("content")
                    try:
                        data = json.loads(content) if isinstance(content, str) else content
                        schema_validate(data, TINY_SCHEMA)
                        works, error = True, None
                    except Exception as exc:
                        works, error = False, short(exc)
                else:
                    data, present, error = tool_arguments(body)
                    if present:
                        try:
                            schema_validate(data, TINY_SCHEMA)
                            works = True
                        except Exception as exc:
                            works, error = False, short(exc)
                    else:
                        works = False
                findings[name] = {"works": works, "tool_call": bool(body.get("choices", [{}])[0].get("message", {}).get("tool_calls")) if isinstance(body, dict) else False, "error": error, "finish_reason": finish_of(body)}
            except Exception as exc:
                findings[name] = {"works": False, "error": short(clean(exc))}
        works = any(result.get("works") for result in findings.values())
        first_error = next((result.get("error") for result in findings.values() if result.get("error")), None)
        return {"status": "OK" if works else "FAIL", "modes": findings, "error": None if works else first_error}

    check6 = run_check("6 대체 structured output", fallback_modes)

    def output_cap():
        response, body = call_chat("llm.max_tokens_32", [{"role": "user", "content": "Write a detailed 20-point explanation of smartphone demand, each point with several sentences."}], max_tokens=32)
        if response.status_code >= 400:
            raise RuntimeError(response_error(response.status_code, body))
        finish = finish_of(body)
        return {"status": "OK" if finish == "length" else "WARN", "finish_reason": finish, "usage": usage_of(body)}

    run_check("7 출력 토큰 상한", output_cap)

    ko_text = ("아이폰 생산 조정으로 스마트폰 수요 전망을 낮추고 공급망 재고와 프리미엄 모델 판매를 확인한다. ") * 25
    en_text = ("We lower the smartphone demand forecast after iPhone production cuts and monitor channel inventory and premium model sales. ") * 17
    ko_text, en_text = ko_text[:1000], en_text[:1000]
    tokenizer_observations: list[dict[str, Any]] = []

    def tokenizer_check():
        nonlocal ko_tokens_per_char
        root = without_v1(llm_base)
        urls = list(dict.fromkeys((joined(root, "/tokenize"), joined(llm_base, "/tokenize"))))
        counts: dict[str, int | None] = {"ko": None, "en": None}
        for endpoint_index, url in enumerate(urls):
            for language, text_value in (("ko", ko_text), ("en", en_text)):
                try:
                    response, body = request(f"llm.tokenize.{endpoint_index}.{language}", "POST", url, llm_key, {"model": model, "prompt": text_value})
                    count = extract_token_count(body) if response.status_code < 400 else None
                    tokenizer_observations.append({"url": safe_url(url), "language": language, "status_code": response.status_code, "tokens": count, "error": None if response.status_code < 400 else response_error(response.status_code, body)})
                    if counts[language] is None and count:
                        counts[language] = count
                except Exception as exc:
                    tokenizer_observations.append({"url": safe_url(url), "language": language, "error": short(clean(exc))})
        source = "tokenize"
        if counts["ko"] and counts["en"]:
            ko_cpt = len(ko_text) / counts["ko"]
            en_cpt = len(en_text) / counts["en"]
            ko_tokens_per_char = counts["ko"] / len(ko_text)
            facts = {"source": source, "ko_chars_per_token": round(ko_cpt, 4), "en_chars_per_token": round(en_cpt, 4), "ko_tokens_per_char": round(ko_tokens_per_char, 6), "tokenizer": tokenizer_observations}
            return {"status": "OK", **facts}

        estimate_tokens: dict[str, int] = {}
        for language, text_value in (("ko", ko_text), ("en", en_text)):
            try:
                response, body = call_chat(f"llm.token_estimate.{language}", [{"role": "user", "content": text_value}], max_tokens=8)
                usage = usage_of(body)
                if response.status_code < 400 and isinstance(usage.get("prompt_tokens"), int):
                    estimate_tokens[language] = usage["prompt_tokens"]
            except Exception as exc:
                tokenizer_observations.append({"language": language, "estimate_error": short(clean(exc))})
        if estimate_tokens.get("ko") and estimate_tokens.get("en"):
            ko_cpt = len(ko_text) / estimate_tokens["ko"]
            en_cpt = len(en_text) / estimate_tokens["en"]
            ko_tokens_per_char = estimate_tokens["ko"] / len(ko_text)
            return {"status": "WARN", "source": "chat usage estimate", "ko_chars_per_token": round(ko_cpt, 4), "en_chars_per_token": round(en_cpt, 4), "ko_tokens_per_char": round(ko_tokens_per_char, 6), "prompt_tokens": estimate_tokens, "estimate": True, "tokenizer": tokenizer_observations}
        return {"status": "FAIL", "source": "unavailable", "error": "tokenizer and chat usage estimates unavailable", "tokenizer": tokenizer_observations}

    run_check("8 토크나이저 비율", tokenizer_check)

    if args.skip_context:
        results.append({"name": "9 컨텍스트 길이", "status": "WARN", "skipped": True, "latency_ms": 0})
    else:
        context_sizes = (8000, 16000, 32000, 64000, 100000)

        def context_check():
            nonlocal largest_context
            # ponytail: use a rough Hangul ratio only when measurement failed; rerun after check 8 can count tokens.
            ratio = ko_tokens_per_char or 1.5
            paragraph = "아이폰 생산 조정과 스마트폰 수요 전망을 비교하고 공급망 재고, 판매량, 가격 변화를 확인한다. "
            for size in context_sizes:
                chars = max(1, round(size / ratio))
                prompt = (paragraph * ((chars // len(paragraph)) + 1))[:chars]
                try:
                    response, body = call_chat(f"llm.context_{size}", [{"role": "user", "content": prompt}], max_tokens=16)
                    if response.status_code >= 400:
                        return {"status": "WARN" if largest_context else "FAIL", "largest_accepted_tokens": largest_context, "first_rejected_tokens": size, "error": response_error(response.status_code, body), "ratio_tokens_per_char": ratio}
                    largest_context = size
                except Exception as exc:
                    return {"status": "WARN" if largest_context else "FAIL", "largest_accepted_tokens": largest_context, "first_rejected_tokens": size, "error": short(clean(exc)), "ratio_tokens_per_char": ratio}
            return {"status": "OK", "largest_accepted_tokens": largest_context, "first_rejected_tokens": None, "ratio_tokens_per_char": ratio}

        run_check("9 컨텍스트 길이", context_check)

    if args.skip_concurrency:
        results.append({"name": "10 동시성", "status": "WARN", "skipped": True, "latency_ms": 0})
    else:
        def concurrency_check():
            levels = {}
            for level in (1, 4, 8):
                def fire(index: int):
                    started = time.perf_counter()
                    try:
                        response, body = call_issues(f"llm.concurrency_{level}.{index}")
                        data, present, error = tool_arguments(body) if response.status_code < 400 else (None, False, response_error(response.status_code, body))
                        ok = response.status_code < 400 and present and data is not None
                        return {"ok": ok, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "status_code": response.status_code, "error": None if ok else error}
                    except Exception as exc:
                        return {"ok": False, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "error": short(clean(exc))}

                with ThreadPoolExecutor(max_workers=level) as pool:
                    calls = list(pool.map(fire, range(level)))
                latencies = [call["latency_ms"] for call in calls]
                levels[str(level)] = {
                    "p50_ms": round(statistics.median(latencies), 1), "max_ms": max(latencies, default=0),
                    "errors": sum(not call["ok"] for call in calls),
                    "http_429": sum(call.get("status_code") == 429 for call in calls),
                    "http_503": sum(call.get("status_code") == 503 for call in calls),
                    "calls": calls,
                }
            all_failed = bool(levels) and all(facts["errors"] == int(level) for level, facts in levels.items())
            status = "FAIL" if all_failed else ("OK" if all(facts["errors"] == 0 for facts in levels.values()) else "WARN")
            first_error = next((call["error"] for facts in levels.values() for call in facts["calls"] if call.get("error")), None)
            return {"status": status, "levels": levels, "error": first_error}

        run_check("10 동시성", concurrency_check)

    embedding_texts = [
        "아이폰 생산 감축으로 하반기 스마트폰 출하량 전망이 낮아졌다.",
        "iPhone production cuts have lowered the smartphone shipment outlook for the second half.",
        "메모리 DRAM 가격 상승으로 서버 업체의 원가 부담이 커지고 있다.",
        "Rising memory DRAM prices are increasing costs for server manufacturers.",
    ]
    embedding_worked = False

    def embedding_check():
        nonlocal embedding_worked
        try:
            models_response, models_body = request("embed.models", "GET", joined(embed_base, "/models"), embed_key)
            if models_response.status_code >= 400:
                models_error = response_error(models_response.status_code, models_body)
                model_ids = []
            else:
                models_error, model_ids = None, extract_model_ids(models_body)
        except Exception as exc:
            models_error, model_ids = short(clean(exc)), []
        vectors = []
        core_error = None
        try:
            response, body = request("embed.four_texts", "POST", joined(embed_base, "/embeddings"), embed_key, {"model": embed_model, "input": embedding_texts})
            if response.status_code >= 400:
                core_error = response_error(response.status_code, body)
            else:
                vectors = embeddings_of(body)
                if len(vectors) != 4:
                    core_error = "embedding response did not contain four vectors"
                else:
                    embedding_worked = True
        except Exception as exc:
            core_error = short(clean(exc))
        similarities = {"ko_en_paraphrase": cosine(vectors[0], vectors[1]), "ko_unrelated": cosine(vectors[0], vectors[2])} if len(vectors) == 4 else None
        batch_ok = False
        try:
            batch_response, batch_body = request("embed.batch_64", "POST", joined(embed_base, "/embeddings"), embed_key, {"model": embed_model, "input": [f"짧은 테스트 문장 {index}: 스마트폰 수요와 공급망." for index in range(64)]})
            batch_ok = batch_response.status_code < 400 and len(embeddings_of(batch_body)) == 64
            batch_error = None if batch_ok else (response_error(batch_response.status_code, batch_body) if batch_response.status_code >= 400 else "expected 64 embeddings")
            batch_info = {"ok": batch_ok, "status_code": batch_response.status_code, "latency_ms": round(batch_response.elapsed.total_seconds() * 1000, 1) if batch_response.elapsed else None, "error": batch_error}
        except Exception as exc:
            batch_info = {"ok": False, "error": short(clean(exc))}
        try:
            long_text = ("아이폰 생산 조정과 스마트폰 시장 수요, 공급망 재고와 메모리 가격을 확인한다. " * 200)[:6000]
            long_response, long_body = request("embed.long_text", "POST", joined(embed_base, "/embeddings"), embed_key, {"model": embed_model, "input": [long_text]})
            long_ok = long_response.status_code < 400 and bool(embeddings_of(long_body))
            long_error = None if long_ok else (response_error(long_response.status_code, long_body) if long_response.status_code >= 400 else "no embedding vector returned")
            long_info = {"ok": long_ok, "chars": len(long_text), "status_code": long_response.status_code, "error": long_error}
        except Exception as exc:
            long_info = {"ok": False, "chars": 6000, "error": short(clean(exc))}
        partial_fail = not batch_info["ok"] or not long_info["ok"] or bool(models_error)
        return {
            "status": "FAIL" if core_error else ("WARN" if partial_fail else "OK"), "model_ids": model_ids, "models_error": models_error,
            "model": embed_model, "dimension": len(vectors[0]) if vectors else None, "similarities": similarities, "four_texts_error": core_error,
            "batch_64": batch_info, "long_text": long_info,
        }

    check11 = run_check("11 임베딩", embedding_check)
    if embedding_worked:
        recommendations["EMBED_BASE_URL"] = clean(safe_url(embed_base))
        recommendations["EMBED_MODEL"] = embed_model

    rerank_documents = [embedding_texts[1], embedding_texts[2], embedding_texts[3]]
    rerank_worked: dict[str, Any] = {}

    def reranker_check():
        nonlocal rerank_worked
        attempts = [
            ("cohere", joined(rerank_base, "/rerank"), {"model": rerank_model, "query": embedding_texts[0], "documents": rerank_documents}),
            ("tei", joined(rerank_base, "/rerank"), {"query": embedding_texts[0], "texts": rerank_documents}),
            ("cohere", joined(without_v1(rerank_base), "/rerank"), {"model": rerank_model, "query": embedding_texts[0], "documents": rerank_documents}),
        ]
        errors = []
        for index, (style, url, payload) in enumerate(attempts):
            try:
                response, body = request(f"rerank.try_{index}", "POST", url, rerank_key, payload)
                if response.status_code >= 400:
                    errors.append(response_error(response.status_code, body))
                    continue
                scores = rerank_scores(body, len(rerank_documents))
                if scores is not None:
                    rerank_worked = {"style": style, "url": safe_url(url)}
                    return {"status": "OK", "style": style, "url": safe_url(url), "response_shape": type(body).__name__ + (":" + ",".join(body.keys()) if isinstance(body, dict) else ""), "scores": scores, "relevant_doc_highest": scores[0] == max(scores), "attempt_errors": errors}
                errors.append("HTTP success but no ranked scores found")
            except Exception as exc:
                errors.append(short(clean(exc)))

        root_score = joined(without_v1(rerank_base), "/score")
        score_values = []
        for index, document in enumerate(rerank_documents):
            try:
                response, body = request(f"rerank.score_{index}", "POST", root_score, rerank_key, {"model": rerank_model, "text_1": embedding_texts[0], "text_2": document})
                if response.status_code >= 400:
                    errors.append(response_error(response.status_code, body))
                    score_values = []
                    break
                value = None
                if isinstance(body, (int, float)):
                    value = float(body)
                elif isinstance(body, dict):
                    value = next((body[key] for key in ("score", "similarity", "relevance_score") if isinstance(body.get(key), (int, float))), None)
                if value is None:
                    errors.append("score endpoint returned no numeric score")
                    score_values = []
                    break
                score_values.append(float(value))
            except Exception as exc:
                errors.append(short(clean(exc)))
                score_values = []
                break
        if len(score_values) == len(rerank_documents):
            rerank_worked = {"style": "score", "url": safe_url(root_score)}
            return {"status": "OK", "style": "score", "url": safe_url(root_score), "response_shape": "numeric score per text pair", "scores": score_values, "relevant_doc_highest": score_values[0] == max(score_values), "attempt_errors": errors}
        return {"status": "FAIL", "error": short(errors[0]) if errors else "no reranker response", "attempt_errors": errors}

    check12 = run_check("12 리랭커", reranker_check)
    if rerank_worked:
        recommendations["RERANK_API_STYLE"] = rerank_worked["style"]
        recommendations["RERANK_URL"] = clean(rerank_worked["url"])
        recommendations["RERANK_BASE_URL"] = clean(safe_url(rerank_base if rerank_worked["url"].lower().startswith(joined(rerank_base, "").lower()) else without_v1(rerank_base)))

    named_ok = check4.get("status") == "OK"
    named_known = check4.get("tool_call") is not None or str(check4.get("error", "")).startswith("HTTP ")
    working_fallbacks = [name for name, value in check6.get("modes", {}).items() if value.get("works")]
    recommendations["LLM_DISABLE_THINKING"] = str(bool(check3.get("accepted"))).lower()
    context_value = largest_context or next((value for value in context_fields.values() if isinstance(value, int)), None)
    if context_value:
        recommendations["LLM_CONTEXT_TOKENS"] = context_value
        recommendations["LLM_MAX_TOKENS"] = min(8192, context_value // 4)
    concurrency = next((item for item in results if item["name"] == "10 동시성"), {})
    levels = concurrency.get("levels", {})
    baseline = levels.get("1", {}).get("p50_ms")
    viable = [int(level) for level, facts in levels.items() if facts["errors"] == 0 and baseline is not None and facts["p50_ms"] < 2 * baseline]
    if viable:
        recommendations["LLM_MAX_CONCURRENCY"] = max(viable)
    if ko_tokens_per_char is not None:
        recommendations["KO_TOKENS_PER_CHAR"] = round(ko_tokens_per_char, 6)
    recommendations["LLM_NAMED_TOOL_CHOICE"] = named_ok if named_known else None

    summary_lines = []
    for item in results:
        facts = {key: value for key, value in item.items() if key not in {"name", "status", "latency_ms", "calls", "tokenizer", "attempt_errors", "error", "first_schema_error"}}
        if item.get("error"):
            facts["error"] = item["error"]
        elapsed = f"{item['latency_ms']}ms" if item.get("latency_ms") is not None else "시간 미측정"
        if item["name"] == "10 동시성":
            levels = item.get("levels", {})
            fact_text = "; ".join(f"{count}x p50={level['p50_ms']}ms max={level['max_ms']}ms errors={level['errors']} 429={level['http_429']} 503={level['http_503']}" for count, level in levels.items()) or short(item.get("error", "동시성 결과 없음"))
            if item.get("error"):
                fact_text += " error=" + short(item["error"], 90)
        elif item["name"] == "5 ISSUES 스키마":
            fact_text = "tool={tool_call} JSON={json_valid} schema={schema_valid} issues={issue_count} finish={finish_reason} completion_tokens={completion_tokens}".format(**{key: item.get(key) for key in ("tool_call", "json_valid", "schema_valid", "issue_count", "finish_reason", "completion_tokens")})
            if item.get("first_schema_error"):
                fact_text += " error=" + short(item["first_schema_error"], 100)
            elif item.get("error"):
                fact_text += " error=" + short(item["error"], 100)
        elif item["name"] == "11 임베딩":
            similarity = item.get("similarities") or {}
            batch = item.get("batch_64", {})
            long_text = item.get("long_text", {})
            fact_text = f"dim={item.get('dimension')} ko-en={similarity.get('ko_en_paraphrase')} ko-unrelated={similarity.get('ko_unrelated')} batch64={batch.get('ok')} long6000={long_text.get('ok')}"
            if item.get("four_texts_error"):
                fact_text += " error=" + short(item["four_texts_error"], 90)
        else:
            fact_text = short(json.dumps(clean(facts), ensure_ascii=False), 210)
        summary_lines.append(f"[{item['status']}] {item['name']} — {fact_text} ({elapsed})")

    print("\n".join(summary_lines))
    print("\n권장 설정")
    env_names = ("LLM_DISABLE_THINKING", "LLM_CONTEXT_TOKENS", "LLM_MAX_TOKENS", "LLM_MAX_CONCURRENCY", "KO_TOKENS_PER_CHAR", "EMBED_BASE_URL", "EMBED_MODEL", "RERANK_API_STYLE", "RERANK_BASE_URL", "RERANK_URL", "LLM_NAMED_TOOL_CHOICE")
    for name in env_names:
        value = recommendations.get(name)
        if value is None:
            value = "미측정"
        print(f"{name}={value}")
    if named_known and not named_ok:
        fallback_text = ", ".join(working_fallbacks) if working_fallbacks else "작동한 대체 방식 없음"
        print(f"경고: named tool_choice가 작동하지 않았습니다. 파이프라인 structured-output 방식을 변경해야 합니다. 확인된 대체 방식: {fallback_text}.")

    output = {
        "config": {
            "llm_base_url": clean(safe_url(llm_base)), "llm_model": model or None,
            "embed_base_url": clean(safe_url(embed_base)), "embed_model": embed_model,
            "rerank_base_url": clean(safe_url(rerank_base)), "rerank_model": rerank_model,
            "timeout_sec": args.timeout, "schema_source": SCHEMA_SOURCE,
        },
        "results": results,
        "recommendations": recommendations,
        "raw_results": clean(raw_results),
    }
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"결과 JSON: {args.out}")
    except Exception as exc:
        print(f"[WARN] 결과 JSON 저장 실패 — {short(clean(exc))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
