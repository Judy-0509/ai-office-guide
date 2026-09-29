"""Stdlib HTTP server (SSE over `http.server.ThreadingHTTPServer`) + CLI for the slide studio.

No FastAPI/Node -- the runtime target is an offline intranet PC. One process, one deck at a
time (`DeckStore.busy` gate), one browser tab.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import db
from ..config import Settings, read_env_file
from ..llm.client import LLMClient
from ..llm.schemas import SchemaError
from . import pptx_export, prompts, spec
from .stream import OpParser

STATIC_DIR = Path(__file__).resolve().parent / "static"
SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
DEFAULT_TOPIC_PATH = SAMPLES_DIR / "topic_memory.json"
DEFAULT_DECK_PATH = SAMPLES_DIR / "deck_memory.json"
DEFAULT_SLIDES_MAX_TOKENS = 16000
ZERO_OPS_RETRY_WARNING = "모델이 슬라이드를 내놓지 않아 한 번 더 시도합니다"
ZERO_OPS_GIVE_UP_ERROR = "모델이 슬라이드를 생성하지 못했습니다"


def resolve_slides_max_tokens(env_path: Path | None) -> int:
    """SLIDES_MAX_TOKENS: process environment wins, then the --env file, then the default --
    matches Settings.load's own precedence so the team can set it in .env like everything else."""
    if os.environ.get("SLIDES_MAX_TOKENS"):
        return int(os.environ["SLIDES_MAX_TOKENS"])
    if env_path is not None:
        value = read_env_file(Path(env_path)).get("SLIDES_MAX_TOKENS")
        if value:
            return int(value)
    return DEFAULT_SLIDES_MAX_TOKENS


class DeckStore:
    """Deck state + on-disk persistence (deck.json + numbered versions/) for one running
    studio session. All mutation goes through this class, under `self.lock`."""

    def __init__(
        self, data_dir: Path, topic: dict, sample_deck: dict, *, deck_override: Path | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.topic = topic
        self.stats = prompts.compute_stats(topic)
        self.lock = threading.RLock()
        self.busy = False
        self.cancel_event: threading.Event | None = None
        self.warnings: dict[int, list[str]] = {}

        if deck_override is not None:
            self.deck = json.loads(Path(deck_override).read_text(encoding="utf-8"))
        else:
            saved = self.data_dir / "deck.json"
            self.deck = (
                json.loads(saved.read_text(encoding="utf-8")) if saved.exists()
                else json.loads(json.dumps(sample_deck))
            )
        self._version = self._next_version_number() - 1
        self._recompute_warnings()

    def _next_version_number(self) -> int:
        versions_dir = self.data_dir / "versions"
        existing = sorted(versions_dir.glob("*.json")) if versions_dir.exists() else []
        return int(existing[-1].stem) + 1 if existing else 1

    def sources(self) -> dict:
        return prompts.sources_for_check(self.topic, self.stats)

    def _recompute_warnings(self) -> None:
        sources = self.sources()
        self.warnings = {
            i: spec.check(slide, sources) for i, slide in enumerate(self.deck.get("slides", []))
        }

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.deck, ensure_ascii=False, indent=2)
        (self.data_dir / "deck.json").write_text(text, encoding="utf-8")
        versions_dir = self.data_dir / "versions"
        versions_dir.mkdir(parents=True, exist_ok=True)
        self._version += 1
        (versions_dir / f"{self._version:04d}.json").write_text(text, encoding="utf-8")

    def reset_deck(self, sample_deck: dict) -> None:
        self.deck = json.loads(json.dumps(sample_deck))
        self._recompute_warnings()
        self.save()

    def apply_and_warn(self, op: dict) -> list[str]:
        """Apply one already-validated op, persist, and return the combined (apply-time +
        content-check) warnings for the SSE `op` event."""
        self.deck, apply_warnings = spec.apply_op(self.deck, op)
        content_warnings: list[str] = []
        if op["op"] == "delete":
            self._recompute_warnings()
        elif 0 <= op["index"] < len(self.deck.get("slides", [])):
            content_warnings = spec.check(self.deck["slides"][op["index"]], self.sources())
            self.warnings[op["index"]] = content_warnings
        self.save()
        return apply_warnings + content_warnings


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # app state lives on the server instance, not the (per-request) handler instance
    @property
    def store(self) -> DeckStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def llm_client(self) -> LLMClient:
        return self.server.llm_client  # type: ignore[attr-defined]

    @property
    def sample_deck(self) -> dict:
        return self.server.sample_deck  # type: ignore[attr-defined]

    @property
    def slides_max_tokens(self) -> int:
        return self.server.slides_max_tokens  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean
        pass

    # --- small response helpers -------------------------------------------------------------
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send_json({"error": "not found"}, status=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_pptx(self) -> None:
        with self.store.lock:
            deck = self.store.deck
        body = pptx_export.export_bytes(deck)
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.send_header("Content-Disposition", 'attachment; filename="weekly-coverage.pptx"')
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
            return False  # client went away (navigated off / cancelled) -- nothing to clean up

    def _state_payload(self) -> dict:
        with self.store.lock:
            slides = self.store.deck.get("slides", [])
            return {
                "deck": self.store.deck,
                "warnings": [self.store.warnings.get(i, []) for i in range(len(slides))],
                "topic_title": self.store.topic.get("topic", ""),
                "model": self.llm_client.settings.llm_model_default,
                "busy": self.store.busy,
            }

    # --- routing -------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send_json(self._state_payload())
        elif path == "/api/export.pptx":
            self._serve_pptx()
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_PUT(self) -> None:  # noqa: N802
        if urllib.parse.urlsplit(self.path).path != "/api/deck":
            self._send_json({"error": "not found"}, status=404)
            return
        body = self._body_or_400()
        if body is None:
            return
        try:
            slides = [spec.validate_slide(s) for s in body.get("slides", [])]
        except SchemaError as exc:
            self._send_json({"error": f"저장 실패: {exc}"}, status=400)
            return
        with self.store.lock:
            self.store.deck = {**body, "slides": slides}
            self.store._recompute_warnings()
            self.store.save()
            state = self._state_payload()
        self._send_json(state)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/generate":
            self._handle_generate()
        elif path == "/api/edit":
            self._handle_edit()
        elif path == "/api/cancel":
            self._handle_cancel()
        elif path == "/api/deck/new":
            self._handle_deck_new()
        else:
            self._send_json({"error": "not found"}, status=404)

    def _handle_deck_new(self) -> None:
        with self.store.lock:
            if self.store.busy:
                self._send_json({"error": "생성 중입니다"}, status=409)
                return
            self.store.reset_deck(self.sample_deck)
            state = self._state_payload()
        self._send_json(state)

    def _handle_cancel(self) -> None:
        with self.store.lock:
            if self.store.cancel_event is not None:
                self.store.cancel_event.set()
        self._send_json({"ok": True})

    def _handle_generate(self) -> None:
        body = self._body_or_400()
        if body is None:
            return
        slide_count = int(body.get("slides") or 3)
        store = self.store
        with store.lock:
            if store.busy:
                self._send_json({"error": "생성 중입니다"}, status=409)
                return
            store.busy = True
            store.cancel_event = threading.Event()
            cancel_event = store.cancel_event
            store.deck = {
                "title": store.deck.get("title") or store.topic.get("topic", ""),
                "footer": "가상 데이터 · 견본", "slides": [],
            }
            store.warnings = {}
            store.save()
        try:
            messages = prompts.generate_messages(store.stats, store.topic, slide_count)
            self._stream_ops(messages, step="slides.generate", cancel_event=cancel_event)
        finally:
            with store.lock:
                store.busy = False
                store.cancel_event = None

    def _handle_edit(self) -> None:
        body = self._body_or_400()
        if body is None:
            return
        instruction = str(body.get("instruction") or "")
        selected = body.get("selected")
        selected = selected if isinstance(selected, int) else None
        store = self.store
        with store.lock:
            if store.busy:
                self._send_json({"error": "생성 중입니다"}, status=409)
                return
            store.busy = True
            store.cancel_event = threading.Event()
            cancel_event = store.cancel_event
            deck_snapshot = store.deck
        try:
            messages = prompts.edit_messages(store.stats, store.topic, deck_snapshot, selected, instruction)
            self._stream_ops(messages, step="slides.edit", cancel_event=cancel_event)
        finally:
            with store.lock:
                store.busy = False
                store.cancel_event = None

    # --- SSE generation/edit loop --------------------------------------------------------
    def _emit_op(self, op: dict) -> None:
        warnings = self.store.apply_and_warn(op)
        self._sse("op", {
            "op": op["op"], "index": op["index"], "slide": op.get("slide"), "warnings": warnings,
        })

    def _run_llm_stream(
        self, messages: list[dict], *, step: str, cancel_event: threading.Event,
        reasoning_chars: list[str],
    ) -> dict:
        """One LLM streaming attempt: emits reasoning/op/warning/error SSE events as they
        arrive, applies ops via the store. Returns token counts and whether it produced
        anything, so the caller can decide whether to retry (see L in the fix order)."""
        prompt_tokens = completion_tokens = 0
        reasoning_tokens_api: int | None = None
        had_error = False
        op_count = 0
        parser = OpParser()
        try:
            for event, payload in self.llm_client.stream_complete(
                messages, step=step, agent="", max_tokens=self.slides_max_tokens, cancel=cancel_event,
            ):
                if event == "reasoning":
                    reasoning_chars.append(payload)
                    self._sse("reasoning", {"text": payload})
                elif event == "content":
                    before = len(parser.warnings)
                    for op in parser.feed(payload):
                        self._emit_op(op)
                        op_count += 1
                    for warning in parser.warnings[before:]:
                        self._sse("warning", {"text": warning})
                elif event == "usage":
                    prompt_tokens = int(payload.get("prompt_tokens", 0))
                    completion_tokens = int(payload.get("completion_tokens", 0))
                    reasoning_tokens_api = payload.get("reasoning_tokens")
                elif event == "error":
                    had_error = True
                    self._sse("error", {"text": str(payload)})
        except Exception as exc:  # noqa: BLE001 - tell the client, keep the server thread alive
            had_error = True
            self._sse("error", {"text": f"생성 실패: {exc}"[:500]})

        before = len(parser.warnings)
        for op in parser.close():
            self._emit_op(op)
            op_count += 1
        for warning in parser.warnings[before:]:
            self._sse("warning", {"text": warning})

        return {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "reasoning_tokens_api": reasoning_tokens_api, "had_error": had_error,
            "op_count": op_count,
        }

    def _stream_ops(self, messages: list[dict], *, step: str, cancel_event: threading.Event) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        reasoning_chars: list[str] = []
        started = time.perf_counter()
        prompt_tokens = completion_tokens = 0
        reasoning_tokens_api: int | None = None

        result = self._run_llm_stream(
            messages, step=step, cancel_event=cancel_event, reasoning_chars=reasoning_chars)
        prompt_tokens += result["prompt_tokens"]
        completion_tokens += result["completion_tokens"]
        if result["reasoning_tokens_api"] is not None:
            reasoning_tokens_api = result["reasoning_tokens_api"]

        # The model sometimes finishes still "thinking" and never emits a single op line (see
        # the work order's live-check finding). One retry with the same messages recovers most
        # of these; if it still produces nothing, surface a clear error instead of silence.
        if result["op_count"] == 0 and not result["had_error"] and not cancel_event.is_set():
            self._sse("warning", {"text": ZERO_OPS_RETRY_WARNING})
            retry = self._run_llm_stream(
                messages, step=step, cancel_event=cancel_event, reasoning_chars=reasoning_chars)
            prompt_tokens += retry["prompt_tokens"]
            completion_tokens += retry["completion_tokens"]
            if retry["reasoning_tokens_api"] is not None:
                reasoning_tokens_api = retry["reasoning_tokens_api"]
            if retry["op_count"] == 0 and not retry["had_error"]:
                self._sse("error", {"text": ZERO_OPS_GIVE_UP_ERROR})

        seconds = time.perf_counter() - started
        reasoning_text = "".join(reasoning_chars)
        reasoning_tokens = (
            reasoning_tokens_api if reasoning_tokens_api is not None
            else (self.llm_client.estimate_tokens(reasoning_text) if reasoning_text else 0)
        )
        self._sse("done", {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens, "seconds": round(seconds, 1),
            "internal_estimate_seconds": round(completion_tokens / 40, 1),
        })


def build_server(
    settings: Settings, topic_path: Path, data_dir: Path, deck_override: Path | None,
    host: str, port: int, *, env_path: Path | None = None,
) -> ThreadingHTTPServer:
    topic = json.loads(Path(topic_path).read_text(encoding="utf-8"))
    sample_deck = json.loads(DEFAULT_DECK_PATH.read_text(encoding="utf-8"))
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    conn = db.connect(data_dir / "slides.sqlite")
    db.init_schema(conn)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.llm_client = LLMClient(settings, conn)  # type: ignore[attr-defined]
    server.sample_deck = sample_deck  # type: ignore[attr-defined]
    server.slides_max_tokens = resolve_slides_max_tokens(env_path)  # type: ignore[attr-defined]
    server.store = DeckStore(  # type: ignore[attr-defined]
        data_dir, topic, sample_deck,
        deck_override=Path(deck_override) if deck_override else None,
    )
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AI Office 슬라이드 스튜디오")
    parser.add_argument("--env", default=None, help=".env 파일 경로")
    parser.add_argument("--topic", default=str(DEFAULT_TOPIC_PATH), help="주제 입력 자료 JSON")
    parser.add_argument("--data", required=True, help="deck.json/versions/slides.sqlite 저장 디렉터리")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--deck", default=None, help="시작 덱 JSON (없으면 저장된 덱, 없으면 견본)")
    args = parser.parse_args(argv)

    environ = dict(os.environ)
    environ["DATA_DIR"] = args.data
    settings = Settings.load(Path(args.env) if args.env else None, environ=environ)
    settings.ensure_dirs()

    server = build_server(
        settings, Path(args.topic), Path(args.data),
        Path(args.deck) if args.deck else None, args.host, args.port,
        env_path=Path(args.env) if args.env else None,
    )
    print(f"슬라이드 스튜디오 실행 중: http://{args.host}:{args.port}/  (모델: {settings.llm_model_default})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
