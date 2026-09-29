"""stdio MCP server for the report markdown library (see aioffice.library.mdlibrary).

Run with `python -m aioffice.tools.library_mcp --library <path> [--env ../.env]`. Gives
in-house OpenCode agents a search tool over the stacked reports instead of reading many
files: hybrid FTS+vector search with reciprocal rank fusion and an optional reranker.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ..config import read_env_file
from ..library import mdlibrary

_library: Path | None = None
_env: dict[str, str] = {}


def set_library(path: Path, env: dict[str, str] | None = None) -> None:
    global _library, _env
    _library = Path(path)
    _env = env or {}


def get_library() -> Path:
    if _library is None:
        raise RuntimeError("--library 가 설정되지 않았습니다")
    return _library


def search_reports(query: str, week: str | None = None, broker: str | None = None,
                    k: int = 8) -> Any:
    """리포트 청크를 검색합니다 (FTS + 벡터 검색을 RRF로 합치고, 설정 시 재랭킹).

    한 리포트에서 최대 2개까지만 반환해 한 리포트가 결과를 독점하지 않습니다."""
    return mdlibrary.search_chunks(get_library(), query, week=week, broker=broker, k=k, env=_env)


def list_reports(week: str | None = None, broker: str | None = None,
                  company: str | None = None) -> Any:
    """카탈로그에서 날짜/증권사/제목/기업/유형/파일/요약 목록을 반환합니다."""
    return mdlibrary.list_reports(get_library(), week=week, broker=broker, company=company)


TOOLS = (search_reports, list_reports)


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("ai-office-library")
    for tool in TOOLS:
        server.add_tool(tool, name=tool.__name__, description=(tool.__doc__ or "").strip())
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="리포트 라이브러리 검색 MCP 서버")
    parser.add_argument("--library", required=True, help="라이브러리 폴더 경로")
    parser.add_argument("--env", default=None, help=".env 파일 경로 (EMBED_*/RERANK_* 설정)")
    args = parser.parse_args(argv)

    env = read_env_file(Path(args.env)) if args.env else {}
    set_library(Path(args.library), env)
    build_server().run()


if __name__ == "__main__":
    main()
