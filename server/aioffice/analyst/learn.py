"""Batch orchestration: extract -> retrieve -> integrate per report, consolidate per batch,
git commits, events.jsonl, and the `python -m aioffice.analyst.learn` CLI. LLM calls are
strictly sequential (the in-house backend serves one request at a time; parallel calls only
queue -- see the work order), so this module never runs more than one call concurrently.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import db
from ..config import Settings
from ..llm.client import LLMClient
from . import ingest, render, steps, store

LOCK_NAME = "learn.lock"
DEFAULT_MAX_TOKENS = 20000
CONSOLIDATE_CHUNK_SIZE = 12


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().date().isoformat()


# --- lock ------------------------------------------------------------------------------------


class LearnLock:
    """A module-level lock file prevents two learners from touching one vault at once."""

    def __init__(self, vault: Path):
        self.path = store.state_dir(vault) / LOCK_NAME
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError(f"다른 학습이 이미 실행 중입니다 (lock: {self.path})") from None
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        self._acquired = True

    def release(self) -> None:
        if self._acquired and self.path.exists():
            self.path.unlink()
        self._acquired = False

    def __enter__(self) -> "LearnLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# --- git -------------------------------------------------------------------------------------


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def init_vault(vault: Path) -> bool:
    """Creates the vault skeleton (dirs, index.md, log.md); git init if missing. Returns
    whether git is usable (warns once and continues without commits if not)."""
    vault = Path(vault)
    vault.mkdir(parents=True, exist_ok=True)
    store.state_dir(vault).mkdir(parents=True, exist_ok=True)
    for name in ("reports", "topics", "entities", "batches"):
        (vault / name).mkdir(exist_ok=True)
    if not (vault / "index.md").exists():
        (vault / "index.md").write_text("# 개요\n\n아직 학습한 리포트가 없습니다.\n", encoding="utf-8")
    if not (vault / "log.md").exists():
        (vault / "log.md").write_text("# 학습 기록\n\n", encoding="utf-8")
    if not (vault / ".gitignore").exists():
        # analyst.sqlite is a rebuilt-from-JSON mirror (+ the LLM call log) -- not source of
        # truth, and its WAL/SHM side files churn on every write, so keep it out of git.
        (vault / ".gitignore").write_text("analyst.sqlite*\nlearn.lock\n", encoding="utf-8")

    git_ok = _git_available()
    if git_ok and not (vault / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
        subprocess.run(["git", "config", "user.email", "analyst@local"], cwd=vault, check=True)
        subprocess.run(["git", "config", "user.name", "AI Office Analyst"], cwd=vault, check=True)
    elif not git_ok:
        print("경고: git을 찾을 수 없어 커밋 없이 진행합니다", flush=True)
    return git_ok


def git_commit(vault: Path, message: str, git_ok: bool) -> None:
    if not git_ok:
        return
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=vault, capture_output=True)
    # a nonzero exit here (e.g. nothing to commit on a no-op review) isn't worth surfacing


# --- events (append + forward to an optional on_event callback + log.md) --------------------


# extract_start/integrate_start are transient "in-flight step" markers for the UI (see
# store.batch_progress) -- worth keeping in events.jsonl for replay, not worth a permanent
# log.md line.
_SILENT_LOG_EVENTS = {"extract_start", "integrate_start"}


def _emit(vault: Path, event_type: str, data: dict, on_event: Callable[[str, dict], None] | None) -> dict:
    ev = store.append_event(vault, event_type, data, ts=_now_iso())
    if event_type not in _SILENT_LOG_EVENTS:
        render.append_log_line(vault, render.log_line(event_type, data))
    if on_event:
        on_event(event_type, data)
    return ev


# --- one report --------------------------------------------------------------------------------


def learn_one_report(state: store.AnalystState, pending: ingest.PendingReport, llm: LLMClient,
                      model: str | None, max_tokens: int, *, task_id: int | None = None,
                      on_event: Callable[[str, dict], None] | None = None) -> dict:
    """Runs extract -> retrieve -> integrate for one report and applies the result to
    `state` in place. Returns a summary used for events/rendering/the live-check report."""
    numbered_text, sid_map = ingest.number_sentences(pending.text)

    _emit(state.vault, "extract_start", {"report_id": pending.id}, on_event)
    started = time.perf_counter()
    try:
        extract_data = steps.run_extract(llm, numbered_text, model=model, max_tokens=max_tokens, task_id=task_id)
    except Exception as exc:  # noqa: BLE001 -- a hard extract failure must not stop the batch
        report = {
            "id": pending.id,
            "path": f"reports/{store.report_filename(pending.date, pending.broker, pending.id)}",
            "broker": pending.broker, "title": pending.title, "date": pending.date,
            "date_source": pending.date_source, "status": "error", "review": None,
            "fictional": pending.fictional,
        }
        state.reports.append(report)
        _emit(state.vault, "error", {"report_id": pending.id, "text": f"추출 실패: {exc}"[:500]}, on_event)
        return {
            "report": report, "processed_claims": [],
            "change_summary": {"new": 0, "supports": 0, "updates": 0, "contradicts": 0,
                                "topics_created": [], "topics_updated": [], "invalidated": [],
                                "relations_created": []},
            "extract_seconds": time.perf_counter() - started, "integrate_seconds": 0.0,
            "error": str(exc), "number_fail": 0,
        }
    processed = steps.process_extract_claims(extract_data, sid_map)
    for i, c in enumerate(processed, start=1):
        c["local_id"] = f"c{i}"
    extract_seconds = time.perf_counter() - started
    number_fail = sum(1 for c in processed if not c["number_ok"])
    _emit(state.vault, "extracted", {
        "report_id": pending.id, "claims": len(processed), "number_fail": number_fail,
        "seconds": round(extract_seconds, 1),
    }, on_event)

    claims_by_topic = {tid: state.claims_for_topic(tid) for tid in state.topics}
    candidates = steps.candidate_topics(processed, state.topics, claims_by_topic, llm)
    feedback = steps.feedback_examples(processed, store.read_feedback(state.vault), llm)

    _emit(state.vault, "integrate_start", {"report_id": pending.id}, on_event)
    started = time.perf_counter()
    integrate_data, error = None, None
    for _attempt in range(2):
        try:
            candidate = steps.run_integrate(llm, processed, candidates, feedback, pending.date,
                                             model=model, max_tokens=max_tokens, task_id=task_id)
            steps.validate_integrate_shape(processed, candidate)
            integrate_data = candidate
            error = None
            break
        except Exception as exc:  # noqa: BLE001 -- any failure here retries once, then falls back
            error = str(exc)
    if integrate_data is None:
        integrate_data = store.fallback_integrate_result(processed)
    integrate_seconds = time.perf_counter() - started

    report = {
        "id": pending.id,
        "path": f"reports/{store.report_filename(pending.date, pending.broker, pending.id)}",
        "broker": pending.broker, "title": pending.title, "date": pending.date,
        "date_source": pending.date_source, "status": "learned", "review": None,
        "fictional": pending.fictional,
    }
    state.reports.append(report)
    change_summary = store.apply_integration(state, report, processed, integrate_data)

    _emit(state.vault, "integrated", {
        "report_id": pending.id, "new": change_summary["new"], "supports": change_summary["supports"],
        "updates": change_summary["updates"], "contradicts": change_summary["contradicts"],
        "topics_created": [t["name"] for t in change_summary["topics_created"]],
        "topics_updated": [t["name"] for t in change_summary["topics_updated"]],
        "seconds": round(integrate_seconds, 1),
    }, on_event)
    if error:
        _emit(state.vault, "error", {"text": f"{report['id'][:8]}: {error}"[:500]}, on_event)

    return {
        "report": report, "processed_claims": processed, "change_summary": change_summary,
        "extract_seconds": extract_seconds, "integrate_seconds": integrate_seconds,
        "error": error, "number_fail": number_fail,
    }


# --- consolidate ---------------------------------------------------------------------------------


def consolidate_topics(state: store.AnalystState, llm: LLMClient, model: str | None,
                        max_tokens: int, topic_ids: list[str], as_of_date: str,
                        *, on_event: Callable[[str, dict], None] | None = None,
                        task_id: int | None = None) -> dict:
    """Refreshes summaries/trends for the given topics (up to CONSOLIDATE_CHUNK_SIZE per LLM
    call). A chunk that fails is logged as an error event and skipped, not fatal.

    ponytail: a failed chunk's topics keep their stale summary until next touched by a
    report (not retried on a timer) -- fine since backend failures are rare after the
    per-call retries already inside complete_json; add a scheduled re-consolidate pass if
    stale summaries turn out to linger in practice."""
    relations_out: list[dict] = []
    for start in range(0, len(topic_ids), CONSOLIDATE_CHUNK_SIZE):
        chunk_ids = topic_ids[start:start + CONSOLIDATE_CHUNK_SIZE]
        topics_info = []
        for tid in chunk_ids:
            topic = state.topics.get(tid)
            if not topic:
                continue
            claims = state.claims_for_topic(tid, valid_only=False)[:12]
            topics_info.append({
                "id": tid, "name": topic["name"], "previous_summary": topic.get("summary"),
                "claims": [{"date": c["report_date"], "broker": c["broker"], "text": c["text"],
                            "valid": c.get("valid", True)} for c in claims],
            })
        if not topics_info:
            continue
        try:
            data = steps.run_consolidate(llm, topics_info, model=model, max_tokens=max_tokens,
                                          task_id=task_id)
        except Exception as exc:  # noqa: BLE001 -- one bad chunk shouldn't fail the whole batch
            _emit(state.vault, "error", {"text": f"통합 정리 실패: {exc}"[:500]}, on_event)
            continue
        for t in data.get("topics", []):
            tid = t.get("id")
            if tid not in state.topics:
                continue
            state.topics[tid]["summary"] = t.get("summary") or state.topics[tid].get("summary", "")
            state.topics[tid]["trend"] = t.get("trend") or state.topics[tid].get("trend", "flat")
            state.topics[tid].setdefault("summary_history", []).append({
                "date": as_of_date, "summary": state.topics[tid]["summary"],
                "trend": state.topics[tid]["trend"],
            })
            state.topics[tid]["needs_summary"] = False
        for rel_info in data.get("relations", []):
            src, dst, kind = rel_info.get("from"), rel_info.get("to"), rel_info.get("kind")
            if src not in state.topics or dst not in state.topics or not kind:
                continue
            rel = next((r for r in state.relations
                        if r["from"] == src and r["to"] == dst and r["kind"] == kind), None)
            if rel is None:
                rel = {"from": src, "to": dst, "kind": kind, "why": rel_info.get("why", ""),
                       "first_seen": as_of_date, "last_seen": as_of_date, "evidence": []}
                state.relations.append(rel)
            else:
                rel["last_seen"] = as_of_date
                rel["why"] = rel_info.get("why") or rel["why"]
            relations_out.append(rel)
    return {"relations": relations_out}


# --- batch -----------------------------------------------------------------------------------


def _next_batch_number(vault: Path) -> int:
    batches_dir = Path(vault) / "batches"
    existing = sorted(batches_dir.glob("*.md")) if batches_dir.exists() else []
    return len(existing) + 1


def learn_batch(vault: Path, inbox: Path, llm: LLMClient, model: str | None, max_tokens: int,
                 batch_size: int, git_ok: bool, *,
                 on_event: Callable[[str, dict], None] | None = None,
                 should_stop: Callable[[], bool] | None = None) -> dict:
    vault = Path(vault)
    state = store.AnalystState.load(vault)
    pending, warnings = ingest.scan_inbox(inbox, state.learned_ids())
    for w in warnings:
        _emit(vault, "error", {"text": w}, on_event)

    batch_reports = ingest.next_batch(pending, batch_size)
    if not batch_reports:
        return {"reports": 0, "results": []}

    batch_n = _next_batch_number(vault)
    _emit(vault, "batch_start", {"batch": batch_n, "n": len(batch_reports)}, on_event)

    touched_topics: set[str] = set()
    results: list[dict] = []
    for i, pending_report in enumerate(batch_reports, start=1):
        if should_stop and should_stop():
            break
        _emit(vault, "report_start", {
            "i": i, "n": len(batch_reports), "report_id": pending_report.id,
            "title": pending_report.title, "broker": pending_report.broker,
            "date": pending_report.date,
        }, on_event)

        # a report retried after an earlier "error" replaces that stale record, not duplicates it
        state.reports = [r for r in state.reports if r["id"] != pending_report.id]

        result = learn_one_report(state, pending_report, llm, model, max_tokens, on_event=on_event)
        report, cs = result["report"], result["change_summary"]

        for t in cs["topics_created"] + cs["topics_updated"]:
            touched_topics.add(t["id"])

        state.save_all()
        store.rebuild_mirror_db(state, llm.conn)
        render.write_report_page(vault, state, report["id"])
        render.write_entity_pages(vault, state, {n for c in result["processed_claims"]
                                                  for n in (c.get("entities") or [])})
        render.write_index_and_log(vault, state)  # keep index.md's counts live, not just at batch end
        if report["status"] != "error":  # a failed extract commits nothing -- retried later, not lost
            git_commit(vault, f"학습: {report['date']} {report['broker']} {report['title']}", git_ok)

        _emit(vault, "report_done", {"report_id": report["id"], "path": report["path"]}, on_event)
        results.append(result)

    if not results:
        return {"reports": 0, "results": []}

    if touched_topics:
        started = time.perf_counter()
        as_of_date = results[-1]["report"]["date"]
        consolidate_summary = consolidate_topics(state, llm, model, max_tokens, sorted(touched_topics),
                                                  as_of_date, on_event=on_event)
        seconds = time.perf_counter() - started
        state.save_topics()
        state.save_relations()
        store.rebuild_mirror_db(state, llm.conn)
        for tid in touched_topics:
            if tid in state.topics:
                render.write_topic_page(vault, state, tid)
        _emit(vault, "consolidated", {
            "batch": batch_n, "topics": len(touched_topics),
            "relations": len(consolidate_summary["relations"]), "seconds": round(seconds, 1),
        }, on_event)

    render.write_index_and_log(vault, state)
    batch_path = render.write_batch_page(vault, state, batch_n, results, touched_topics)
    git_commit(vault, f"배치 {batch_n:03d} 정리", git_ok)
    _emit(vault, "batch_done", {"batch": batch_n, "path": batch_path}, on_event)

    return {"reports": len(results), "results": results, "topics_touched": len(touched_topics)}


# --- CLI ---------------------------------------------------------------------------------------


def build_llm_client(vault: Path, env: Path | None) -> LLMClient:
    environ = dict(os.environ)
    environ["DATA_DIR"] = str(store.state_dir(vault))
    settings = Settings.load(env, environ=environ)
    conn = db.connect(store.db_path(vault))
    db.init_schema(conn)
    return LLMClient(settings, conn)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AI Office 분석가 학습 파이프라인")
    parser.add_argument("--vault", required=True, help="학습 결과를 쌓을 vault 디렉터리")
    parser.add_argument("--inbox", required=True, help="리포트 원본(.md/.txt/.pdf) 폴더")
    parser.add_argument("--batch", type=int, default=10, help="배치당 리포트 수")
    parser.add_argument("--env", default=None, help=".env 파일 경로")
    parser.add_argument("--batches", type=int, default=1, help="실행할 배치 수")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    args = parser.parse_args(argv)

    vault = Path(args.vault)
    git_ok = init_vault(vault)
    llm = build_llm_client(vault, Path(args.env) if args.env else None)
    model = llm.settings.llm_model_default

    with LearnLock(vault):
        for _ in range(args.batches):
            result = learn_batch(vault, Path(args.inbox), llm, model, args.max_tokens,
                                  args.batch, git_ok,
                                  on_event=lambda t, d: print(render.log_line(t, d), flush=True))
            if result["reports"] == 0:
                print("학습할 리포트가 더 없습니다", flush=True)
                break


if __name__ == "__main__":
    main()
