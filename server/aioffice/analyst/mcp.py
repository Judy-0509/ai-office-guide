"""stdio MCP server exposing the knowledge functions as tools for the in-house OpenCode
agent (see aioffice.analyst.knowledge) -- the same functions the HTTP API uses. Run with
`python -m aioffice.analyst.mcp --vault <dir> [--env .env]`.

Outputs are kept compact (capped list sizes, quotes truncated to 300 chars in
`knowledge._claim_dict`) so a 27B model's context isn't flooded.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from ..config import Settings
from . import knowledge, store

MAX_LIST = 20  # per-list cap on top of whatever limit/k the agent asked for

_vault: Path | None = None
_llm: Any = None  # LLMClient | None -- only used for search()'s optional embed+rerank


def set_vault(vault: Path, llm: Any = None) -> None:
    global _vault, _llm
    _vault = Path(vault)
    _llm = llm


def get_vault() -> Path:
    if _vault is None:
        raise RuntimeError("--vault 가 설정되지 않았습니다")
    return _vault


# --- tools (one per knowledge function) -------------------------------------------------------


def knowledge_overview(as_of: str | None = None) -> dict:
    """분석가가 학습한 내용의 전체 현황을 요약합니다: 리포트/주장/주제/관계 수, 학습 기간, 상위
    주제, 최근 배치. 대화를 시작하거나 "지금까지 뭘 알고 있어?" 같은 질문에 가장 먼저 호출하세요.
    Call this first for a snapshot of what the analyst vault currently knows."""
    return knowledge.overview(get_vault(), as_of=as_of)


def search_knowledge(query: str, k: int = 5, date_from: str | None = None,
                      date_to: str | None = None) -> dict:
    """자연어 질문으로 관련 주제와 주장을 찾습니다. 구체적인 주제 id를 모를 때 먼저 이 도구로
    찾은 뒤 get_topic으로 상세를 확인하세요. 답변에는 반드시 citation의 report_path를 인용하고,
    숫자는 이 도구가 반환한 값만 사용하세요 (지어내지 마세요).
    Search topics/claims by natural-language query. Always cite report_path; never invent numbers."""
    return knowledge.search(get_vault(), query, k=min(k, MAX_LIST), date_from=date_from,
                             date_to=date_to, llm=_llm)


def get_topic(topic_id_or_name: str, as_of: str | None = None) -> dict:
    """주제 하나의 현재 판단(요약·추세), 근거 주장 타임라인(출처 포함), 무효화된 과거 주장,
    관련 주제를 반환합니다. search_knowledge로 주제를 찾은 다음 호출하세요.
    Full detail for one topic: current judgement, cited claim timeline, invalidated
    history, related topics. Call after search_knowledge identifies a topic."""
    result = knowledge.topic(get_vault(), topic_id_or_name, as_of=as_of)
    if result is None:
        return {"error": f"주제를 찾을 수 없습니다: {topic_id_or_name}"}
    result = dict(result)
    result["valid_claims"] = result["valid_claims"][:MAX_LIST]
    result["invalidated_claims"] = result["invalidated_claims"][:MAX_LIST]
    return result


def what_changed(date_from: str, date_to: str) -> dict:
    """지정한 기간에 학습된 리포트, 새로 생긴 주제, 판단이 바뀐 주제(이전→이후), 무효화된 주장,
    새로 생기거나 갱신된 주제 간 관계를 반환합니다. "이번 주/이번 달 뭐가 바뀌었어?" 질문에
    사용하세요. Use for "what changed this week/month" questions (date_from/date_to
    required, format YYYY-MM-DD)."""
    result = knowledge.changes(get_vault(), date_from, date_to)
    return {key: value[:MAX_LIST] for key, value in result.items()}


def find_claims(entity: str | None = None, metric: str | None = None, broker: str | None = None,
                 type: str | None = None, topic: str | None = None, date_from: str | None = None,
                 date_to: str | None = None, valid: str = "valid", numeric_only: bool = False,
                 limit: int = 20) -> dict:
    """조건(기업/부품명, 지표명, 증권사, 유형, 주제, 기간, 유효 여부, 숫자 포함 여부)으로 주장을
    걸러 평탄한 목록으로 반환합니다. 표에 넣을 원자료가 필요할 때 사용하세요.
    Flat, filterable claim rows with citations -- use when you need raw data for a table,
    not prose."""
    return knowledge.claims(get_vault(), entity=entity, metric=metric, broker=broker, type=type,
                             topic=topic, date_from=date_from, date_to=date_to, valid=valid,
                             numeric_only=numeric_only, limit=min(limit, MAX_LIST), offset=0)


def metric_history(entity: str, metric: str, period: str | None = None) -> dict:
    """증권사별로 특정 지표의 수정 이력(체인)을 반환합니다 (예: "A증권 힌지 수율 72%→65%→70%").
    수치가 시간에 따라 어떻게 바뀌었는지 물을 때 사용하세요.
    Per-broker revision chain for one metric -- use for "how did this number change over
    time" questions."""
    return knowledge.metric_history(get_vault(), entity, metric, period=period)


TOOLS = (knowledge_overview, search_knowledge, get_topic, what_changed, find_claims, metric_history)


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("ai-analyst")
    for tool in TOOLS:
        server.add_tool(tool, name=tool.__name__, description=(tool.__doc__ or "").strip())
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="분석가 지식 MCP 서버 (stdio)")
    parser.add_argument("--vault", required=True, help="vault 디렉터리")
    parser.add_argument("--env", default=None, help=".env 파일 경로 (EMBED_*/RERANK_* 설정)")
    args = parser.parse_args(argv)

    from .. import db
    from ..llm.client import LLMClient

    vault = Path(args.vault)
    environ = dict(os.environ)
    environ["DATA_DIR"] = str(store.state_dir(vault))
    settings = Settings.load(Path(args.env) if args.env else None, environ=environ)
    conn = db.connect(store.db_path(vault))
    db.init_schema(conn)
    llm = LLMClient(settings, conn)
    store.rebuild_mirror_db(store.AnalystState.load(vault), conn)  # cheap, keeps a stale mirror fresh

    set_vault(vault, llm)
    build_server().run()


if __name__ == "__main__":
    main()
