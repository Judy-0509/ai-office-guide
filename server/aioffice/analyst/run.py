"""One-command launcher: initializes the vault, starts the viewer server, opens it as an app
window, and immediately kicks off learning the next batch in the background -- so the common
case ("just run it") is a single command instead of `learn` then `viewer` in two terminals.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

from ..config import Settings
from . import ingest, learn, store, viewer
from .viewer import find_open_port  # shared with `viewer` itself -- avoids a circular import

DEFAULT_PORT = 8780

# Standard Edge install locations, checked before falling back to PATH/webbrowser.
_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
)


def find_msedge() -> str | None:
    for path in _EDGE_CANDIDATES:
        if path and Path(path).exists():
            return path
    return shutil.which("msedge")


def open_browser(url: str) -> None:
    """Edge as an app window (no tabs/address bar) when found, else the OS default browser."""
    edge = find_msedge()
    if edge:
        try:
            subprocess.Popen([edge, f"--app={url}"])
            return
        except OSError:
            pass  # fall through to webbrowser
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AI Office 분석가 -- 한 번에 실행")
    parser.add_argument("--inbox", required=True, help="리포트 원본(.md/.txt/.pdf) 폴더")
    parser.add_argument("--vault", required=True, help="vault 디렉터리")
    parser.add_argument("--env", default=None, help=".env 파일 경로")
    parser.add_argument("--batch", type=int, default=10, help="배치당 리포트 수")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="브라우저 창을 열지 않음")
    parser.add_argument("--no-learn", action="store_true", help="시작 시 자동 학습을 건너뜀")
    parser.add_argument("--max-tokens", type=int, default=learn.DEFAULT_MAX_TOKENS)
    args = parser.parse_args(argv)

    vault = Path(args.vault)
    inbox = Path(args.inbox)

    port = find_open_port(args.host, args.port)
    if port != args.port:
        print(f"포트 {args.port}이(가) 사용 중이라 {port}번으로 실행합니다", flush=True)

    environ = dict(os.environ)
    environ["DATA_DIR"] = str(store.state_dir(vault))
    settings = Settings.load(Path(args.env) if args.env else None, environ=environ)

    server = viewer.build_server(vault, inbox, settings, settings.llm_model_default,
                                  args.max_tokens, args.host, port)

    lock = learn.LearnLock(vault)
    lock.acquire()

    url = f"http://{args.host}:{port}/"
    print(f"분석가 뷰어 실행 중: {url}  (모델: {settings.llm_model_default})", flush=True)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    if not args.no_browser:
        open_browser(url)

    if not args.no_learn:
        state = store.AnalystState.load(vault)
        pending, _warnings = ingest.scan_inbox(inbox, state.learned_ids())
        if pending:
            print(f"다음 배치 학습을 백그라운드에서 시작합니다 ({min(args.batch, len(pending))}건)...", flush=True)
            viewer.start_background_learn(server, args.batch)
        else:
            print("학습할 리포트가 없습니다.", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n종료 중입니다...", flush=True)
    finally:
        server.stop_event.set()  # let any open SSE connections' poll loops exit
        with server.run_lock:
            stop_event = server.run_state.get("stop")
        if stop_event is not None:
            stop_event.set()  # ask an in-flight learn to stop after the current report
        server.shutdown()
        server.server_close()
        lock.release()
        print("종료되었습니다.", flush=True)


if __name__ == "__main__":
    main()
