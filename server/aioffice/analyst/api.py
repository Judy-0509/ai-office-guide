"""Knowledge HTTP API: GET-only endpoints over the `knowledge` read functions -- the same
functions the MCP door (`aioffice.analyst.mcp`) uses. Two ways to run it:

- Mounted on the existing viewer server under `/api/v1/...` (see `viewer.py`'s routing) --
  no extra auth, since the viewer already binds 127.0.0.1 by default.
- Standalone and read-only: `python -m aioffice.analyst.api --vault <dir> [--env .env]
  [--host 127.0.0.1] [--port 8790] [--token <key>] [--cors <origin>]`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..config import Settings
from . import knowledge, store

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

ENDPOINTS = [
    {"path": "/api/v1/overview", "method": "GET", "params": ["as_of"],
     "description": "학습 현황 요약: 리포트/주장/주제/관계 수, 학습 기간, 상위 주제, 최근 배치"},
    {"path": "/api/v1/search", "method": "GET", "params": ["query", "k", "date_from", "date_to"],
     "description": "주제·주장 검색 (임베딩+재랭킹 설정 시 사용, 아니면 트라이그램) -- LLM 생성 없음"},
    {"path": "/api/v1/topics", "method": "GET", "params": [],
     "description": "주제 목록: id, 이름, 유효 주장 수, 추세, 최근 갱신일"},
    {"path": "/api/v1/topics/<id_or_name>", "method": "GET", "params": ["as_of"],
     "description": "주제 상세: 요약, 추세, 유효/무효 주장(출처 포함), 관련 주제"},
    {"path": "/api/v1/changes", "method": "GET", "params": ["date_from", "date_to"],
     "description": "기간 내 변화: 학습된 리포트, 새 주제, 판단 변화, 무효화, 신규/갱신 관계"},
    {"path": "/api/v1/claims", "method": "GET",
     "params": ["entity", "metric", "broker", "type", "topic", "date_from", "date_to",
                "valid", "numeric_only", "limit", "offset"],
     "description": "조건별 주장 목록 (대시보드용 평탄화된 행, 출처 포함)"},
    {"path": "/api/v1/metric_history", "method": "GET", "params": ["entity", "metric", "period"],
     "description": "증권사별 수치 변경 이력 (수정 체인)"},
]

_TOPIC_ID_RE = re.compile(r"^/api/v1/topics/(.+)$")


def _parse_bool(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def dispatch(vault: Path, path: str, query: dict[str, list[str]], llm: Any = None) -> tuple[int, Any]:
    """(status, JSON-serializable body) for one `/api/v1/...` GET request. Shared by the
    standalone server below and by `viewer.py`'s mount -- the one place routing lives."""

    def q(name: str) -> str | None:
        values = query.get(name)
        return values[0] if values else None

    try:
        if path == "/api/v1":
            return 200, {"endpoints": ENDPOINTS}

        if path == "/api/v1/overview":
            return 200, knowledge.overview(vault, as_of=q("as_of"))

        if path == "/api/v1/search":
            query_text = q("query")
            if not query_text:
                return 400, {"error": "query 파라미터가 필요합니다"}
            return 200, knowledge.search(vault, query_text, k=_parse_int(q("k"), 10),
                                          date_from=q("date_from"), date_to=q("date_to"), llm=llm)

        if path == "/api/v1/topics":
            return 200, {"topics": knowledge.topics_list(vault)}

        m = _TOPIC_ID_RE.match(path)
        if m:
            ref = urllib.parse.unquote(m.group(1))
            result = knowledge.topic(vault, ref, as_of=q("as_of"))
            if result is None:
                return 404, {"error": f"주제를 찾을 수 없습니다: {ref}"}
            return 200, result

        if path == "/api/v1/changes":
            date_from, date_to = q("date_from"), q("date_to")
            if not date_from or not date_to:
                return 400, {"error": "date_from, date_to 파라미터가 필요합니다"}
            return 200, knowledge.changes(vault, date_from, date_to)

        if path == "/api/v1/claims":
            return 200, knowledge.claims(
                vault, entity=q("entity"), metric=q("metric"), broker=q("broker"),
                type=q("type"), topic=q("topic"), date_from=q("date_from"), date_to=q("date_to"),
                valid=q("valid") or "valid", numeric_only=_parse_bool(q("numeric_only")),
                limit=_parse_int(q("limit"), 200), offset=_parse_int(q("offset"), 0),
            )

        if path == "/api/v1/metric_history":
            entity, metric = q("entity"), q("metric")
            if not entity or not metric:
                return 400, {"error": "entity, metric 파라미터가 필요합니다"}
            return 200, knowledge.metric_history(vault, entity, metric, period=q("period"))

        return 404, {"error": "not found"}
    except FileNotFoundError as exc:
        return 404, {"error": str(exc)}


# --- standalone server -----------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean
        pass

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        cors = self.server.cors_origin  # type: ignore[attr-defined]
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = self.server.token  # type: ignore[attr-defined]
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        api_key = self.headers.get("X-API-Key", "")
        return auth == f"Bearer {token}" or api_key == token

    def do_OPTIONS(self) -> None:  # noqa: N802 -- CORS preflight
        self.send_response(204)
        cors = self.server.cors_origin  # type: ignore[attr-defined]
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, X-API-Key, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send_json(401, {"error": "인증이 필요합니다"})
            return
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        status, body = dispatch(self.server.vault, parsed.path, query, self.server.llm)  # type: ignore[attr-defined]
        self._send_json(status, body)


def build_server(vault: Path, settings: Settings, host: str, port: int, *,
                  token: str | None = None, cors: str | None = None) -> ThreadingHTTPServer:
    """Raises SystemExit if `host` isn't loopback and no `token` was given (contract:
    never expose a token-less API beyond 127.0.0.1/localhost)."""
    if host not in LOOPBACK_HOSTS and not token:
        raise SystemExit("--host가 127.0.0.1/localhost가 아니면 --token이 반드시 필요합니다")

    from .. import db
    from ..llm.client import LLMClient

    vault = Path(vault)
    conn = db.connect(store.db_path(vault))
    db.init_schema(conn)
    llm = LLMClient(settings, conn)
    # cheap and idempotent -- see viewer.build_server for why this runs at every startup
    store.rebuild_mirror_db(store.AnalystState.load(vault), conn)

    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.vault = vault  # type: ignore[attr-defined]
    server.llm = llm  # type: ignore[attr-defined]
    server.token = token  # type: ignore[attr-defined]
    server.cors_origin = cors  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="AI Office 분석가 지식 API (읽기 전용)")
    parser.add_argument("--vault", required=True, help="vault 디렉터리")
    parser.add_argument("--env", default=None, help=".env 파일 경로")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--token", default=None, help="비-loopback 바인딩 시 필수")
    parser.add_argument("--cors", default=None, help="허용할 Origin 하나 (예: http://localhost:3000)")
    args = parser.parse_args(argv)

    import os
    vault = Path(args.vault)
    environ = dict(os.environ)
    environ["DATA_DIR"] = str(store.state_dir(vault))
    settings = Settings.load(Path(args.env) if args.env else None, environ=environ)

    try:
        server = build_server(vault, settings, args.host, args.port, token=args.token, cors=args.cors)
    except SystemExit as exc:
        print(f"오류: {exc}", flush=True)
        raise

    print(f"분석가 지식 API 실행 중: http://{args.host}:{args.port}/api/v1  (읽기 전용)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
