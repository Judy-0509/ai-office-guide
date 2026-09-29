"""Stdlib HTTP server (SSE over `http.server.ThreadingHTTPServer`) implementing the analyst
vault contract's viewer API exactly. Serves `analyst/static/viewer.html` when present, else a
one-line Korean placeholder (the viewer frontend is a separate, parallel work item).
"""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..config import Settings
from . import ingest, learn, render, store

STATIC_DIR = Path(__file__).resolve().parent / "static"
PLACEHOLDER_HTML = "<!doctype html><html><body>분석가 뷰어 (viewer.html 준비 중)</body></html>"

PORT_FALLBACK_TRIES = 10


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
        return True


def find_open_port(host: str, start_port: int, tries: int = PORT_FALLBACK_TRIES) -> int:
    """The requested port if free, else the next `tries` ports in order. Shared by
    `analyst.viewer` and `analyst.run` (which starts a viewer server in the same process)."""
    for port in range(start_port, start_port + tries + 1):
        if _port_is_free(host, port):
            return port
    raise RuntimeError(f"포트 {start_port}부터 {tries}개를 모두 사용할 수 없습니다")


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def start_background_learn(server: ThreadingHTTPServer, batch_size: int) -> bool:
    """Starts learn_batch in a background thread against `server`'s vault/inbox/llm, guarded
    by server.run_state["running"]. Shared by `POST /api/learn` and the `analyst.run`
    launcher so both go through identical start/stop bookkeeping. Returns False (does
    nothing) if a batch is already running."""
    with server.run_lock:  # type: ignore[attr-defined]
        if server.run_state["running"]:  # type: ignore[attr-defined]
            return False
        server.run_state["running"] = True  # type: ignore[attr-defined]
        server.run_state["progress"] = None  # type: ignore[attr-defined]
        stop_event = threading.Event()
        server.run_state["stop"] = stop_event  # type: ignore[attr-defined]

    def worker() -> None:
        try:
            learn.learn_batch(
                server.vault, server.inbox, server.llm, server.model, server.max_tokens,  # type: ignore[attr-defined]
                batch_size, server.git_ok, should_stop=stop_event.is_set,  # type: ignore[attr-defined]
            )
        except Exception as exc:  # noqa: BLE001 -- surface as an events.jsonl error, keep the server up
            store.append_event(server.vault, "error", {"text": f"학습 실패: {exc}"[:500]}, ts=_now_iso())  # type: ignore[attr-defined]
        finally:
            with server.run_lock:  # type: ignore[attr-defined]
                server.run_state["running"] = False  # type: ignore[attr-defined]
                server.run_state["progress"] = None  # type: ignore[attr-defined]

    threading.Thread(target=worker, daemon=True).start()
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean
        pass

    # --- server-level state, proxied per request -----------------------------------------
    @property
    def vault(self) -> Path:
        return self.server.vault  # type: ignore[attr-defined]

    @property
    def inbox(self) -> Path:
        return self.server.inbox  # type: ignore[attr-defined]

    @property
    def llm(self):
        return self.server.llm  # type: ignore[attr-defined]

    @property
    def model(self) -> str | None:
        return self.server.model  # type: ignore[attr-defined]

    @property
    def max_tokens(self) -> int:
        return self.server.max_tokens  # type: ignore[attr-defined]

    @property
    def git_ok(self) -> bool:
        return self.server.git_ok  # type: ignore[attr-defined]

    @property
    def run_state(self) -> dict:
        return self.server.run_state  # type: ignore[attr-defined]

    # --- small helpers ----------------------------------------------------------------------
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def _sse(self, event: str, payload: Any) -> bool:
        data = json.dumps(payload, ensure_ascii=False)
        chunk = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return False

    # --- routing -------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/":
            self._serve_index()
        elif path == "/api/status":
            self._send_json(self._status_payload())
        elif path == "/api/tree":
            self._send_json(self._tree_payload())
        elif path == "/api/page":
            self._handle_page(query.get("path", [""])[0])
        elif path == "/api/graph":
            state = store.AnalystState.load(self.vault)
            self._send_json(store.graph(state, query.get("as_of", [None])[0]))
        elif path == "/api/report_changes":
            self._handle_report_changes(query.get("id", [""])[0])
        elif path == "/api/events":
            self._handle_events()
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/learn":
            self._handle_learn()
        elif path == "/api/learn/stop":
            self._handle_learn_stop()
        elif path == "/api/review":
            self._handle_review()
        elif path == "/api/claim":
            self._handle_claim()
        else:
            self._send_json({"error": "not found"}, status=404)

    # --- GET handlers ----------------------------------------------------------------------
    def _serve_index(self) -> None:
        html_path = STATIC_DIR / "viewer.html"
        body = html_path.read_bytes() if html_path.exists() else PLACEHOLDER_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _status_payload(self) -> dict:
        state = store.AnalystState.load(self.vault)
        pending, _warnings = ingest.scan_inbox(self.inbox, state.learned_ids())
        with self.server.run_lock:  # type: ignore[attr-defined]
            running = self.run_state["running"]
        # progress is reconstructed from events.jsonl (store.batch_progress), not tracked
        # in-memory, so a batch started from the `analyst.learn`/`analyst.run` CLI shows up
        # here exactly like one started through POST /api/learn. `running` also tells it
        # whether a stuck extracting/integrating item means "still working" or "killed".
        return {
            "running": running, "progress": store.batch_progress(self.vault, running=running),
            "learned": len([r for r in state.reports if r["status"] == "learned"]),
            "pending": len(pending), "topics": len(state.topics), "claims": len(state.claims),
            "model": self.model, "git": self.git_ok,
        }

    def _tree_payload(self) -> dict:
        state = store.AnalystState.load(self.vault)

        topics = []
        for tid, t in state.topics.items():
            claims = state.claims_for_topic(tid)
            updated = max((c["report_date"] for c in state.claims if c.get("topic_id") == tid), default=None)
            topics.append({"id": tid, "name": t["name"], "path": f"topics/{tid}.md",
                            "claims": len(claims), "updated": updated})
        topics.sort(key=lambda t: t["claims"], reverse=True)

        reports = sorted(
            [{"id": r["id"], "path": r["path"], "date": r["date"], "broker": r["broker"],
              "title": r["title"], "review": r.get("review"), "status": r["status"]}
             for r in state.reports],
            key=lambda r: r["date"], reverse=True,
        )

        entity_counts: dict[str, int] = {}
        for c in state.claims:
            for name in c.get("entities") or []:
                entity_counts[name] = entity_counts.get(name, 0) + 1
        entities = sorted(
            [{"name": name, "path": f"entities/{store.safe_entity_name(name)}.md", "count": n}
             for name, n in entity_counts.items()],
            key=lambda e: e["count"], reverse=True,
        )

        batches_dir = Path(self.vault) / "batches"
        batch_files = sorted(batches_dir.glob("*.md")) if batches_dir.exists() else []
        batches = [{"n": int(p.stem), "path": f"batches/{p.name}"} for p in batch_files]

        return {"index": "index.md", "log": "log.md", "topics": topics, "reports": reports,
                "entities": entities, "batches": batches}

    def _handle_page(self, rel_path: str) -> None:
        if not rel_path or Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
            self._send_json({"error": "잘못된 경로"}, status=400)
            return
        full = Path(self.vault) / rel_path
        try:
            full.resolve().relative_to(Path(self.vault).resolve())
        except ValueError:
            self._send_json({"error": "잘못된 경로"}, status=400)
            return
        if not full.exists() or not full.is_file():
            self._send_json({"error": "찾을 수 없습니다"}, status=404)
            return
        self._send_json({"path": rel_path, "markdown": full.read_text(encoding="utf-8")})

    def _handle_report_changes(self, report_id: str) -> None:
        if not report_id:
            self._send_json({"error": "id 필요"}, status=400)
            return
        state = store.AnalystState.load(self.vault)
        if state.report_by_id(report_id) is None:
            self._send_json({"error": "찾을 수 없습니다"}, status=404)
            return
        self._send_json(store.report_changes(state, report_id))

    def _handle_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        for ev in store.read_events(self.vault, limit=50):
            if not self._sse(ev["event"], ev["data"]):
                return

        events_file = store.events_path(self.vault)
        last_size = events_file.stat().st_size if events_file.exists() else 0
        stop_event = self.server.stop_event  # type: ignore[attr-defined]
        while not stop_event.is_set():
            time.sleep(0.3)
            if not events_file.exists():
                continue
            size = events_file.stat().st_size
            if size <= last_size:
                continue
            with events_file.open("r", encoding="utf-8") as f:
                f.seek(last_size)
                new_text = f.read()
            last_size = size
            for line in new_text.splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if not self._sse(record["event"], record["data"]):
                    return

    # --- POST handlers ---------------------------------------------------------------------
    def _handle_learn(self) -> None:
        body = self._body_or_400()
        if body is None:
            return
        batch = int(body.get("batch") or 10)
        if not start_background_learn(self.server, batch):
            self._send_json({"error": "학습 중입니다"}, status=409)
            return
        self._send_json({"ok": True}, status=202)

    def _handle_learn_stop(self) -> None:
        with self.server.run_lock:  # type: ignore[attr-defined]
            stop_event = self.run_state.get("stop")
        if stop_event is not None:
            stop_event.set()
        self._send_json({"ok": True})

    def _handle_review(self) -> None:
        body = self._body_or_400()
        if body is None:
            return
        report_id, verdict = body.get("report_id"), body.get("verdict")
        state = store.AnalystState.load(self.vault)
        report = state.report_by_id(report_id)
        if report is None or verdict not in ("ok", "wrong"):
            self._send_json({"error": "잘못된 요청"}, status=400)
            return

        if verdict == "wrong":
            # entities mentioned only by this report's claims must lose that mention once
            # rollback removes them -- capture the list before rollback deletes the claims.
            affected_entities = {n for c in state.claims_for_report(report_id)
                                  for n in (c.get("entities") or [])}
            result = store.rollback(state, report_id)
            if result["touched_topics"]:
                learn.consolidate_topics(state, self.llm, self.model, self.max_tokens,
                                          result["touched_topics"], _now_iso()[:10])
            state.save_all()
            store.rebuild_mirror_db(state, self.llm.conn)
            render.write_report_page(self.vault, state, report_id)
            for tid in result["touched_topics"]:
                if tid in state.topics:
                    render.write_topic_page(self.vault, state, tid)
            render.write_entity_pages(self.vault, state, affected_entities)
            render.write_index_and_log(self.vault, state)
            learn.git_commit(self.vault, f"되돌림: {report['date']} {report['broker']} {report['title']}",
                              self.git_ok)
            changes: dict = {"claims": [], "invalidated": [], "topics_created": [], "topics_updated": []}
        else:
            report["review"] = "ok"
            state.save_reports()
            store.rebuild_mirror_db(state, self.llm.conn)
            changes = store.report_changes(state, report_id)

        store.append_event(self.vault, "review_done", {"report_id": report_id, "verdict": verdict},
                            ts=_now_iso())
        self._send_json({"ok": True, "changes": changes})

    def _handle_claim(self) -> None:
        body = self._body_or_400()
        if body is None:
            return
        claim_id = body.get("claim_id")
        state = store.AnalystState.load(self.vault)
        claim = state.claim_by_id(claim_id)
        if claim is None:
            self._send_json({"error": "찾을 수 없습니다"}, status=404)
            return

        original_text, original_topic = claim["text"], claim.get("topic_id")
        if "text" in body and body["text"]:
            claim["text"] = body["text"]
        if body.get("topic_id") and body["topic_id"] in state.topics:
            claim["topic_id"] = body["topic_id"]

        report = state.report_by_id(claim["report_id"])
        if report is not None:
            report["review"] = "fixed"

        store.append_feedback(self.vault, {
            "report_id": claim["report_id"], "claim_id": claim_id, "text": original_text,
            "corrected_text": claim["text"], "corrected_topic_id": claim.get("topic_id"),
        }, )
        state.save_claims()
        state.save_reports()
        store.rebuild_mirror_db(state, self.llm.conn)
        render.write_report_page(self.vault, state, claim["report_id"])
        for tid in {original_topic, claim.get("topic_id")}:
            if tid and tid in state.topics:
                render.write_topic_page(self.vault, state, tid)
        # the entity page quotes each mention's claim text -- must refresh after an edit
        render.write_entity_pages(self.vault, state, set(claim.get("entities") or []))
        learn.git_commit(self.vault, f"검토: {str(claim['report_id'])[:8]} 교정", self.git_ok)
        self._send_json({"ok": True})


def build_server(vault: Path, inbox: Path, settings: Settings, model: str | None, max_tokens: int,
                  host: str, port: int) -> ThreadingHTTPServer:
    from .. import db
    from ..llm.client import LLMClient

    vault = Path(vault)
    git_ok = learn.init_vault(vault)
    conn = db.connect(store.db_path(vault))
    db.init_schema(conn)
    llm = LLMClient(settings, conn)
    # On an existing vault, analyst.sqlite otherwise only gains the mirror tables on the next
    # report -- rebuild it now (cheap: it's just the JSON/JSONL state re-inserted) so a fresh
    # `viewer`/`run` process can be queried immediately.
    store.rebuild_mirror_db(store.AnalystState.load(vault), conn)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.vault = vault  # type: ignore[attr-defined]
    server.inbox = Path(inbox)  # type: ignore[attr-defined]
    server.llm = llm  # type: ignore[attr-defined]
    server.model = model  # type: ignore[attr-defined]
    server.max_tokens = max_tokens  # type: ignore[attr-defined]
    server.git_ok = git_ok  # type: ignore[attr-defined]
    server.run_lock = threading.RLock()  # type: ignore[attr-defined]
    server.run_state = {"running": False, "progress": None, "stop": None}  # type: ignore[attr-defined]
    server.stop_event = threading.Event()  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AI Office 분석가 뷰어")
    parser.add_argument("--vault", required=True, help="vault 디렉터리")
    parser.add_argument("--inbox", required=True, help="리포트 원본(.md/.txt/.pdf) 폴더")
    parser.add_argument("--env", default=None, help=".env 파일 경로")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--max-tokens", type=int, default=learn.DEFAULT_MAX_TOKENS)
    args = parser.parse_args(argv)

    vault = Path(args.vault)
    import os
    environ = dict(os.environ)
    environ["DATA_DIR"] = str(store.state_dir(vault))
    settings = Settings.load(Path(args.env) if args.env else None, environ=environ)

    port = find_open_port(args.host, args.port)
    if port != args.port:
        print(f"포트 {args.port}이(가) 사용 중이라 {port}번으로 실행합니다", flush=True)

    server = build_server(vault, Path(args.inbox), settings, settings.llm_model_default,
                           args.max_tokens, args.host, port)
    print(f"분석가 뷰어 실행 중: http://{args.host}:{port}/  (모델: {settings.llm_model_default})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
