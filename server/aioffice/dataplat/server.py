"""`python -m aioffice.dataplat.server --db <path> --source <source.yaml> [--site <dashboard
site/ dir>] [--pre "<their existing aggregation command>"] [--env .env] [--host 127.0.0.1]
[--port 8000] [--admin-token T] [--cors ORIGIN]`

Serves the dashboard build from `--site` (SPA fallback to index.html) AND the API on the same
origin -- no CORS needed in production; `--cors` is only for the Vite dev server. Also serves
the reference chatbot page at `/chat`.

GET endpoints (catalog/query/history/loads/refresh-status) are always open (intranet
dashboard). `POST /api/refresh` requires `--admin-token` (header `Authorization: Bearer` or
`X-API-Key`) whenever given, and the server refuses to start bound off-loopback without one.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..config import Settings
from . import chat as chat_module
from . import snapshot, store
from .source import SourceConfig, load_source

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
STATIC_DIR = Path(__file__).resolve().parent / "static"
CHAT_HTML = STATIC_DIR / "chat.html"


def _parse_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean
        pass

    # --- helpers --------------------------------------------------------------------------
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._maybe_cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._maybe_cors()
        self.end_headers()
        self.wfile.write(body)

    def _maybe_cors(self) -> None:
        cors = self.server.cors_origin  # type: ignore[attr-defined]
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)

    def _admin_authorized(self) -> bool:
        token = self.server.admin_token  # type: ignore[attr-defined]
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        api_key = self.headers.get("X-API-Key", "")
        return auth == f"Bearer {token}" or api_key == token

    def _read_json_body(self) -> Any:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _body_or_400(self) -> dict | None:
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            self._send_json({"error": "잘못된 JSON 요청"}, status=400)
            return None
        return body

    def do_OPTIONS(self) -> None:  # noqa: N802 -- CORS preflight
        self.send_response(204)
        cors = self.server.cors_origin  # type: ignore[attr-defined]
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, X-API-Key, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- routing ----------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/chat":
            self._serve_chat_html()
        elif path.startswith("/api/"):
            self._dispatch_api(path, query)
        else:
            self._serve_site(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/refresh":
            self._handle_refresh()
        elif path == "/api/chat":
            self._handle_chat()
        else:
            self._send_json({"error": "not found"}, status=404)

    # --- API ----------------------------------------------------------------------------------
    def _dispatch_api(self, path: str, query: dict[str, list[str]]) -> None:
        def q(name: str) -> str | None:
            values = query.get(name)
            return values[0] if values else None

        conn = self.server.conn  # type: ignore[attr-defined]
        if path == "/api/catalog":
            self._send_json({"datasets": store.catalog(conn)})
        elif path == "/api/query":
            self._handle_query(conn, q)
        elif path == "/api/history":
            self._handle_history(conn, q)
        elif path == "/api/loads":
            self._send_json({"loads": store.list_loads(conn, q("dataset"))})
        elif path.startswith("/api/loads/"):
            self._handle_load_detail(conn, path)
        elif path == "/api/refresh/status":
            with self.server.refresh_lock:  # type: ignore[attr-defined]
                self._send_json(dict(self.server.refresh_state))  # type: ignore[attr-defined]
        else:
            self._send_json({"error": "not found"}, status=404)

    def _handle_query(self, conn, q) -> None:
        dataset = q("dataset")
        if not dataset:
            self._send_json({"error": "dataset 파라미터가 필요합니다"}, status=400)
            return
        try:
            rows = store.query_observations(
                conn, dataset=dataset, metrics=_parse_list(q("metric")),
                entities=_parse_list(q("entity")), regions=_parse_list(q("region")),
                sources=_parse_list(q("source")), period_from=q("period_from"),
                period_to=q("period_to"), version=q("version") or "latest",
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        if len(rows) > store.QUERY_ROW_CAP:
            self._send_json(
                {"error": f"결과가 너무 많습니다 ({len(rows)}행 > {store.QUERY_ROW_CAP}행). "
                          f"조건을 좁혀주세요."}, status=400)
            return

        fmt = q("format") or "long"
        if fmt == "long":
            self._send_json({"rows": rows})
            return
        if fmt != "wide":
            self._send_json({"error": f"알 수 없는 format: {fmt}"}, status=400)
            return
        row_dims = _parse_list(q("rows")) or ["entity"]
        col_dims = _parse_list(q("cols")) or ["period"]
        wide = store.to_wide(rows, row_dims, col_dims)
        cell_count = len(wide["rows"]) * max(1, len(wide["columns"]))
        if cell_count > store.QUERY_CELL_CAP:
            self._send_json(
                {"error": f"셀 수가 너무 많습니다 ({cell_count} > {store.QUERY_CELL_CAP}). "
                          f"조건을 좁혀주세요."}, status=400)
            return
        self._send_json(wide)

    def _handle_history(self, conn, q) -> None:
        dataset, metric, entity, period = q("dataset"), q("metric"), q("entity"), q("period")
        if not (dataset and metric and entity and period):
            self._send_json({"error": "dataset, metric, entity, period 파라미터가 필요합니다"}, status=400)
            return
        self._send_json({"history": store.history(conn, dataset=dataset, metric=metric,
                                                    entity=entity, period=period, source=q("source"))})

    def _handle_load_detail(self, conn, path: str) -> None:
        raw_id = path.rsplit("/", 1)[-1]
        try:
            load_id = int(raw_id)
        except ValueError:
            self._send_json({"error": f"잘못된 load id: {raw_id}"}, status=400)
            return
        result = store.get_load(conn, load_id)
        if result is None:
            self._send_json({"error": "찾을 수 없습니다"}, status=404)
            return
        self._send_json(result)

    def _handle_refresh(self) -> None:
        if not self._admin_authorized():
            self._send_json({"error": "인증이 필요합니다"}, status=401)
            return
        body = self._body_or_400()
        if body is None:
            return
        dataset = body.get("dataset")

        server = self.server
        with server.refresh_lock:  # type: ignore[attr-defined]
            if server.refresh_state["running"]:  # type: ignore[attr-defined]
                self._send_json({"error": "이미 갱신 중입니다"}, status=409)
                return
            server.refresh_state["running"] = True  # type: ignore[attr-defined]

        def worker() -> None:
            try:
                result = snapshot.run_refresh_once(
                    server.source, server.db_path, pre_command=server.pre_command,  # type: ignore[attr-defined]
                    dataset=dataset, log_path=server.log_path,  # type: ignore[attr-defined]
                )
            except Exception as exc:  # noqa: BLE001 -- surface as last_result, keep server up
                result = {"ok": False, "stage": "unexpected", "error": str(exc), "results": []}
            with server.refresh_lock:  # type: ignore[attr-defined]
                server.refresh_state["running"] = False  # type: ignore[attr-defined]
                server.refresh_state["last_result"] = result  # type: ignore[attr-defined]

        threading.Thread(target=worker, daemon=True).start()
        self._send_json({"ok": True}, status=202)

    def _handle_chat(self) -> None:
        body = self._body_or_400()
        if body is None:
            return
        message = body.get("message")
        if not message or not isinstance(message, str):
            self._send_json({"error": "message가 필요합니다"}, status=400)
            return
        history = body.get("history") or []

        acquired = self.server.chat_lock.acquire(blocking=False)  # type: ignore[attr-defined]
        if not acquired:
            self._send_json({"error": "다른 질문을 처리 중입니다. 잠시 후 다시 시도해주세요."}, status=409)
            return
        try:
            result = chat_module.answer(self.server.chat_ctx, message, history)  # type: ignore[attr-defined]
            self._send_json(result)
        finally:
            self.server.chat_lock.release()  # type: ignore[attr-defined]

    # --- static: dashboard SPA + reference chat page ------------------------------------------
    def _serve_chat_html(self) -> None:
        if not CHAT_HTML.exists():
            self._send_json({"error": "chat.html이 없습니다"}, status=404)
            return
        self._send_bytes(CHAT_HTML.read_bytes(), "text/html; charset=utf-8")

    def _serve_site(self, path: str) -> None:
        site_dir = self.server.site_dir  # type: ignore[attr-defined]
        if site_dir is None:
            self._send_json({"error": "--site가 설정되지 않았습니다"}, status=404)
            return
        rel = path.lstrip("/") or "index.html"
        candidate = site_dir / rel
        try:
            resolved = candidate.resolve()
            resolved.relative_to(site_dir.resolve())
        except ValueError:
            self._send_json({"error": "잘못된 경로"}, status=400)
            return
        if not resolved.exists() or resolved.is_dir():
            resolved = (site_dir / "index.html").resolve()  # SPA fallback
        if not resolved.exists():
            self._send_json({"error": "not found"}, status=404)
            return
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        self._send_bytes(resolved.read_bytes(), content_type)


def build_server(db_path: Path, source: SourceConfig, settings: Settings, host: str, port: int, *,
                  site: Path | None = None, pre_command: str | None = None,
                  admin_token: str | None = None, cors: str | None = None,
                  llm: Any = None) -> ThreadingHTTPServer:
    """Raises SystemExit if `host` isn't loopback and no `admin_token` was given."""
    if host not in LOOPBACK_HOSTS and not admin_token:
        raise SystemExit("--host가 127.0.0.1/localhost가 아니면 --admin-token이 반드시 필요합니다")

    from ..llm.client import LLMClient

    db_path = Path(db_path)
    conn = store.connect(db_path)
    llm_client = llm or LLMClient(settings, conn)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.conn = conn  # type: ignore[attr-defined]
    server.db_path = db_path  # type: ignore[attr-defined]
    server.source = source  # type: ignore[attr-defined]
    server.site_dir = Path(site) if site else None  # type: ignore[attr-defined]
    server.pre_command = pre_command  # type: ignore[attr-defined]
    server.log_path = db_path.parent / "dataplat_run.log"  # type: ignore[attr-defined]
    server.admin_token = admin_token  # type: ignore[attr-defined]
    server.cors_origin = cors  # type: ignore[attr-defined]
    server.refresh_lock = threading.RLock()  # type: ignore[attr-defined]
    server.refresh_state = {"running": False, "last_result": None}  # type: ignore[attr-defined]
    server.chat_lock = threading.Lock()  # type: ignore[attr-defined]
    server.chat_ctx = chat_module.ChatContext(conn=conn, llm=llm_client)  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="AI Office 데이터 플랫폼 서버 (API + 대시보드)")
    parser.add_argument("--db", required=True, help="dataplat.sqlite 경로")
    parser.add_argument("--source", required=True, help="source.yaml 경로")
    parser.add_argument("--site", default=None, help="대시보드 정적 빌드(site/) 디렉터리")
    parser.add_argument("--pre", default=None, dest="pre_command", help="갱신 전 실행할 기존 집계 명령")
    parser.add_argument("--env", default=None, help=".env 파일 경로")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--admin-token", default=None, help="POST /api/refresh에 필요")
    parser.add_argument("--cors", default=None, help="Vite 개발 서버용 허용 Origin")
    args = parser.parse_args(argv)

    settings = Settings.load(Path(args.env) if args.env else None)
    source = load_source(Path(args.source))

    try:
        server = build_server(
            Path(args.db), source, settings, args.host, args.port,
            site=Path(args.site) if args.site else None, pre_command=args.pre_command,
            admin_token=args.admin_token, cors=args.cors,
        )
    except SystemExit as exc:
        print(f"오류: {exc}", flush=True)
        raise

    print(f"데이터 플랫폼 서버 실행 중: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
