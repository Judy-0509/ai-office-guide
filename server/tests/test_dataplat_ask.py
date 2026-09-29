"""Tests for aioffice.dataplat.ask: the async "AI에게 물어보기" job queue -- FIFO worker,
persistence, cancel (queued and running), stale-job reaping at startup."""
from __future__ import annotations

import json
import time

from aioffice.config import Settings
from aioffice.dataplat import ask, chat, store
from aioffice.llm.client import LLMClient


class FakeContentBackend:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.model = "fake-model"
        self.name = "direct"
        self.calls: list[dict] = []

    def model_for(self):
        return self.model

    def run_content(self, messages, model, timeout=None, max_tokens=None):
        self.calls.append({"messages": messages, "model": model})
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return content, {"prompt_tokens": 10, "completion_tokens": 20}


def _ctx(tmp_path, responses):
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    conn = store.connect(tmp_path / "dataplat.sqlite")
    store.upsert_dataset(conn, "shipments", "출하", store.now_iso())
    rows = [{"metric": "출하량", "entity": "모델A", "region": "", "period": "2024Q1",
             "period_sort": "20240100", "source": "", "value": 100.0, "unit": ""}]
    store.write_load(conn, dataset="shipments", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    backend = FakeContentBackend(responses)
    llm = LLMClient(settings, conn, backend=backend)
    return chat.ChatContext(conn=conn, llm=llm), backend


def test_run_job_directly_marks_done_with_result(tmp_path):
    ctx_obj, _backend = _ctx(tmp_path, [
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "clarify": None},
    ])
    job_id = store.create_ask_job(ctx_obj.conn, message="모델A 얼마야", history=[])
    worker = ask.AskWorker(ctx_obj)
    job = store.get_ask_job(ctx_obj.conn, job_id)
    worker._run_job(job)

    status = ask.job_status(ctx_obj.conn, job_id)
    assert status["status"] == "done"
    assert status["result"]["path"] == "llm"
    assert status["result"]["table"]["rows"] == [["모델A", 100.0]]


def test_run_job_error_recorded_when_llm_fails(tmp_path):
    from aioffice.llm import BackendError

    ctx_obj, _backend = _ctx(tmp_path, [BackendError("연결 실패"), BackendError("연결 실패")])
    job_id = store.create_ask_job(ctx_obj.conn, message="아무 질문", history=[])
    worker = ask.AskWorker(ctx_obj)
    worker._run_job(store.get_ask_job(ctx_obj.conn, job_id))

    status = ask.job_status(ctx_obj.conn, job_id)
    assert status["status"] == "done"  # a spec-call failure is still a normal clarify result
    assert status["result"]["warnings"] == ["clarify"]


def test_job_status_reports_queue_position(tmp_path):
    ctx_obj, _backend = _ctx(tmp_path, [])
    first = store.create_ask_job(ctx_obj.conn, message="첫번째", history=[])
    second = store.create_ask_job(ctx_obj.conn, message="두번째", history=[])
    assert ask.job_status(ctx_obj.conn, first)["position"] == 1
    assert ask.job_status(ctx_obj.conn, second)["position"] == 2


def test_job_status_unknown_id_is_none(tmp_path):
    ctx_obj, _backend = _ctx(tmp_path, [])
    assert ask.job_status(ctx_obj.conn, 999) is None


def test_cancel_queued_job_removes_it_from_the_queue(tmp_path):
    ctx_obj, _backend = _ctx(tmp_path, [])
    job_id = store.create_ask_job(ctx_obj.conn, message="질문", history=[])
    assert ask.cancel_job(ctx_obj.conn, job_id) is True
    assert store.next_queued_ask_job(ctx_obj.conn) is None
    assert ask.job_status(ctx_obj.conn, job_id)["status"] == "cancelled"


def test_queued_job_cancelled_before_run_never_starts(tmp_path):
    # guards the queued->running transition against a cancel landing in the race window between
    # next_queued_ask_job() selecting a job and _run_job() starting it.
    ctx_obj, backend = _ctx(tmp_path, [
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "clarify": None},
    ])
    job_id = store.create_ask_job(ctx_obj.conn, message="모델A 얼마야", history=[])
    job = store.get_ask_job(ctx_obj.conn, job_id)  # as the worker loop would have fetched it
    assert ask.cancel_job(ctx_obj.conn, job_id) is True  # cancelled before _run_job runs

    ask.AskWorker(ctx_obj)._run_job(job)

    assert ask.job_status(ctx_obj.conn, job_id)["status"] == "cancelled"
    assert len(backend.calls) == 0  # never even called the LLM


def test_cancel_mid_flight_job_discards_the_late_result(tmp_path):
    # the OTHER race: a job already running (mid-LLM-call) gets cancelled; when the call finally
    # returns, the result must be discarded, not overwrite "cancelled" with "done".
    ctx_obj, _backend = _ctx(tmp_path, [])
    job_id = store.create_ask_job(ctx_obj.conn, message="모델A 얼마야", history=[])
    store.update_ask_job(ctx_obj.conn, job_id, status="running", started_at=store.now_iso())
    assert ask.cancel_job(ctx_obj.conn, job_id) is True

    worker = ask.AskWorker(ctx_obj)
    worker._finish_unless_cancelled(job_id, status="done", result_json="{}")  # late arrival

    status = ask.job_status(ctx_obj.conn, job_id)
    assert status["status"] == "cancelled"  # never overwritten to "done"
    assert status["result"] is None


def test_cancel_finished_job_is_a_no_op(tmp_path):
    ctx_obj, _backend = _ctx(tmp_path, [])
    job_id = store.create_ask_job(ctx_obj.conn, message="질문", history=[])
    store.update_ask_job(ctx_obj.conn, job_id, status="done", result_json="{}",
                          finished_at=store.now_iso())
    assert ask.cancel_job(ctx_obj.conn, job_id) is False
    assert ask.job_status(ctx_obj.conn, job_id)["status"] == "done"


def test_worker_thread_processes_queued_job_end_to_end(tmp_path):
    ctx_obj, _backend = _ctx(tmp_path, [
        {"dataset": "shipments", "metrics": ["출하량"], "entities": ["모델A"], "clarify": None},
    ])
    job_id = store.create_ask_job(ctx_obj.conn, message="모델A 얼마야", history=[])
    worker = ask.AskWorker(ctx_obj)
    worker.start()
    try:
        status = None
        for _ in range(50):
            status = ask.job_status(ctx_obj.conn, job_id)
            if status["status"] != "queued" and status["status"] != "running":
                break
            time.sleep(0.05)
        assert status["status"] == "done"
        assert status["result"]["table"]["rows"] == [["모델A", 100.0]]
    finally:
        worker.stop()


def test_stale_running_job_reaped_at_startup(tmp_path):
    ctx_obj, _backend = _ctx(tmp_path, [])
    job_id = store.create_ask_job(ctx_obj.conn, message="질문", history=[])
    store.update_ask_job(ctx_obj.conn, job_id, status="running", started_at=store.now_iso())
    reaped = store.reap_stale_ask_jobs(ctx_obj.conn)
    assert reaped == 1
    status = ask.job_status(ctx_obj.conn, job_id)
    assert status["status"] == "error"
    assert "중단됨" in status["error"]


def test_list_ask_jobs_recent_order(tmp_path):
    ctx_obj, _backend = _ctx(tmp_path, [])
    ids = [store.create_ask_job(ctx_obj.conn, message=f"q{i}", history=[]) for i in range(3)]
    jobs = store.list_ask_jobs(ctx_obj.conn, limit=20)
    assert [j["id"] for j in jobs] == list(reversed(ids))  # newest first
