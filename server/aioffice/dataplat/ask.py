"""Async "AI에게 물어보기" queue: `POST /api/ask` enqueues a job and returns immediately; a
single background worker thread processes jobs FIFO (the shared in-house LLM endpoint only
serves one request at a time anyway, so more worker threads wouldn't help) via
`chat.answer_via_llm()`. Jobs persist in `ask_jobs` (dataplat.sqlite) so a server restart never
loses a finished answer; a job still "running" when the process dies is reaped to status="error"
at the next startup (`store.reap_stale_ask_jobs`, called once by `dataplat.server.build_server`).

ponytail: the worker polls the DB every `POLL_INTERVAL_SEC` for the oldest queued job rather
than an in-memory queue synced with the DB -- avoids a dual-write consistency problem for a
handful of jobs a day; switch to a real queue (or LISTEN/NOTIFY-style signaling) if polling
latency or DB write volume ever actually matters.
"""
from __future__ import annotations

import json
import threading
import time

from . import chat, store

POLL_INTERVAL_SEC = 0.5


class AskWorker:
    def __init__(self, ctx: chat.ChatContext, *, explain_mode: str = "template",
                 chat_timeout: float | None = None):
        self.ctx = ctx
        self.explain_mode = explain_mode
        self.chat_timeout = chat_timeout
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = store.next_queued_ask_job(self.ctx.conn)
            if job is None:
                self._stop.wait(POLL_INTERVAL_SEC)
                continue
            self._run_job(job)

    def _run_job(self, job: dict) -> None:
        # guard the queued->running transition against a cancel that landed in the race window
        # between next_queued_ask_job() selecting this job and this call starting.
        current = store.get_ask_job(self.ctx.conn, job["id"])
        if current is None or current["status"] != "queued":
            return
        store.update_ask_job(self.ctx.conn, job["id"], status="running", started_at=store.now_iso())
        try:
            history = json.loads(job["history_json"] or "[]")
            result = chat.answer_via_llm(self.ctx, job["message"], history,
                                          explain_mode=self.explain_mode,
                                          chat_timeout=self.chat_timeout)
        except Exception as exc:  # noqa: BLE001 -- a broken job must never kill the worker thread
            self._finish_unless_cancelled(job["id"], status="error", error=str(exc)[:2000])
            return
        self._finish_unless_cancelled(
            job["id"], status="done", result_json=json.dumps(result, ensure_ascii=False))

    def _finish_unless_cancelled(self, job_id: int, **fields) -> None:
        current = store.get_ask_job(self.ctx.conn, job_id)
        if current is None or current["status"] == "cancelled":
            return  # the user cancelled while this was running -- discard the result
        store.update_ask_job(self.ctx.conn, job_id, finished_at=store.now_iso(), **fields)


def job_status(conn, job_id: int) -> dict | None:
    """`GET /api/ask/<job_id>`'s payload: the job row plus `position` (1-based, among still-
    queued jobs) and `elapsed_seconds` (since started_at, or created_at while still queued)."""
    job = store.get_ask_job(conn, job_id)
    if job is None:
        return None
    out = dict(job)
    out["result"] = json.loads(job["result_json"]) if job.get("result_json") else None
    out.pop("result_json", None)
    out["history"] = json.loads(job.pop("history_json") or "[]")
    out["position"] = store.ask_job_queue_position(conn, job_id)
    since = job.get("started_at") or job["created_at"]
    try:
        elapsed = time.mktime(time.strptime(store.now_iso(), "%Y-%m-%dT%H:%M:%S")) - \
            time.mktime(time.strptime(since, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        elapsed = 0.0
    out["elapsed_seconds"] = max(0.0, round(elapsed, 2))
    return out


def cancel_job(conn, job_id: int) -> bool:
    """Marks a queued OR running job cancelled. A queued job simply never runs (the worker skips
    anything not `status="queued"`); a running job's result is discarded when it finishes (see
    `AskWorker._finish_unless_cancelled`). Returns False if the job doesn't exist or has already
    finished (done/error/cancelled)."""
    job = store.get_ask_job(conn, job_id)
    if job is None or job["status"] not in ("queued", "running"):
        return False
    store.update_ask_job(conn, job_id, status="cancelled", finished_at=store.now_iso())
    return True
