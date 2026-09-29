"""Integration coverage for the SQLite mirror (`<vault>/analyst.sqlite`): its table counts
must always equal the JSON/JSONL state, through learn, a claim edit, and a review rollback.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from aioffice.analyst import learn, store, viewer
from aioffice.config import Settings

REPORT_BODY = ("메모리 반도체 계약 가격이 이번 분기 10% 상승했다고 밝혔다. "
               "이는 스마트폰 부품 원가 부담을 키우는 요인으로 지목된다. "
               "폴더블 아이폰용 힌지 부품의 수율도 이번 달 크게 개선됐다고 전했다.")


def _write_report(inbox, name, broker, title, date):
    (inbox / name).write_text(
        f"---\nbroker: {broker}\ntitle: {title}\ndate: {date}\nfictional: true\n---\n\n{REPORT_BODY}\n",
        encoding="utf-8",
    )


def _extract_response():
    return {
        "claims": [
            {"sids": ["S1"], "text": "메모리 가격 10% 상승", "type": "fact", "direction": "up",
             "entities": ["memory"], "metric": "가격", "value": "10", "unit": "%", "period": "2026Q3"},
            {"sids": ["S3"], "text": "힌지 수율 개선", "type": "fact", "direction": "up",
             "entities": ["iPhone"], "metric": "수율", "value": None, "unit": None, "period": None},
        ],
        "topics": ["메모리 가격", "폴더블 아이폰"],
    }


def _integrate_response():
    return {
        "claims": [
            {"cid": "c1", "topic": {"new": "메모리 가격 상승"}, "relation": "new", "target": None},
            {"cid": "c2", "topic": {"new": "폴더블 아이폰 공급"}, "relation": "new", "target": None},
        ],
        "links": [],
    }


def _consolidate_response():
    return {"topics": [], "relations": []}


def test_build_server_rebuilds_the_mirror_on_an_existing_vault_at_startup(tmp_path):
    """On an existing vault, analyst.sqlite must reflect the JSON state as soon as a
    `viewer`/`run` process starts -- not only after the next report is learned."""
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    learn.init_vault(vault)

    # Seed JSON state directly (as if learned in an earlier session) -- no mirror db yet.
    state = store.AnalystState(vault=vault, reports=[{
        "id": "abc123ef" + "0" * 56, "path": "reports/2026-07-01_a_abc123ef.md",
        "broker": "A증권", "title": "리포트", "date": "2026-07-01", "date_source": "front_matter",
        "status": "learned", "review": None, "fictional": True,
    }], claims=[], topics={}, relations=[])
    state.save_all()
    assert not store.db_path(vault).exists()

    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data"),
                                             "LLM_MODEL_DEFAULT": "qwen-test"})
    srv = viewer.build_server(vault, inbox, settings, "qwen-test", 8000, "127.0.0.1", 0)
    try:
        row = srv.llm.conn.execute("SELECT COUNT(*) AS n FROM reports").fetchone()  # type: ignore[attr-defined]
        assert row["n"] == 1
    finally:
        srv.server_close()


def _table_counts(conn):
    tables = ("reports", "claims", "topics", "topic_summaries", "relations", "feedback")
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}


def _assert_mirror_matches_state(state, conn):
    counts = _table_counts(conn)
    assert counts["reports"] == len(state.reports)
    assert counts["claims"] == len(state.claims)
    assert counts["topics"] == len(state.topics)
    assert counts["relations"] == len(state.relations)
    history_total = sum(len(t.get("summary_history") or []) for t in state.topics.values())
    assert counts["topic_summaries"] == history_total
    assert counts["feedback"] == len(store.read_feedback(state.vault))


def test_mirror_matches_json_state_after_learn(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01")
    git_ok = learn.init_vault(vault)

    analyst_llm.backend.responses.extend([_extract_response(), _integrate_response(),
                                           _consolidate_response()])
    learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)

    state = store.AnalystState.load(vault)
    assert len(state.claims) == 2 and len(state.topics) == 2
    _assert_mirror_matches_state(state, analyst_llm.conn)

    # llm_calls (owned by aioffice.db, not the mirror) shares the same sqlite file
    row = analyst_llm.conn.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()
    assert row["n"] == 3  # extract + integrate + consolidate


def test_mirror_matches_json_state_after_a_claim_edit(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01")
    git_ok = learn.init_vault(vault)
    analyst_llm.backend.responses.extend([_extract_response(), _integrate_response(),
                                           _consolidate_response()])
    learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)

    state = store.AnalystState.load(vault)
    claim = state.claims[0]
    claim["text"] = "교정된 주장"
    store.append_feedback(vault, {"report_id": claim["report_id"], "claim_id": claim["id"],
                                   "text": "원래 주장", "corrected_text": "교정된 주장",
                                   "corrected_topic_id": claim["topic_id"]})
    state.save_claims()
    store.rebuild_mirror_db(state, analyst_llm.conn)

    _assert_mirror_matches_state(state, analyst_llm.conn)
    row = analyst_llm.conn.execute("SELECT text FROM claims WHERE id=?", (claim["id"],)).fetchone()
    assert row["text"] == "교정된 주장"


def test_mirror_matches_json_state_after_a_rollback(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01")
    git_ok = learn.init_vault(vault)
    analyst_llm.backend.responses.extend([_extract_response(), _integrate_response(),
                                           _consolidate_response()])
    learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)

    state = store.AnalystState.load(vault)
    report_id = state.reports[0]["id"]
    store.rollback(state, report_id)
    state.save_all()
    store.rebuild_mirror_db(state, analyst_llm.conn)

    assert state.claims == [] and state.topics == {}
    _assert_mirror_matches_state(state, analyst_llm.conn)


# --- through the real viewer HTTP endpoint (not just the store functions directly) ------------


def _post(port, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_viewer_review_wrong_rebuilds_the_mirror_over_http(tmp_path):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data"),
                                             "LLM_MODEL_DEFAULT": "qwen-test"})
    srv = viewer.build_server(vault, inbox, settings, "qwen-test", 8000, "127.0.0.1", 0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        state = store.AnalystState.load(vault)
        report = {"id": "abc123ef" + "0" * 56, "path": "reports/2026-07-01_a_abc123ef.md",
                  "broker": "A증권", "title": "리포트", "date": "2026-07-01",
                  "date_source": "front_matter", "status": "learned", "review": None,
                  "fictional": True}
        claim = {"id": "abc123ef-c1", "report_id": report["id"], "report_date": "2026-07-01",
                  "broker": "A증권", "sids": ["S1"], "text": "주장", "type": "fact", "direction": "up",
                  "entities": [], "metric": None, "value": None, "unit": None, "period": None,
                  "quote": "인용", "number_ok": True, "topic_id": "t001", "relation": "new",
                  "target_claim_id": None, "valid": True, "invalid_at": None, "invalidated_by": None}
        topic = {"id": "t001", "name": "주제", "summary": "", "trend": "flat", "summary_history": [],
                 "created_report_id": report["id"], "created_date": "2026-07-01"}
        state.reports = [report]
        state.claims = [claim]
        state.topics = {"t001": topic}
        state.save_all()
        store.rebuild_mirror_db(state, srv.llm.conn)  # type: ignore[attr-defined]

        status, data = _post(port, "/api/review", {"report_id": report["id"], "verdict": "wrong"})
        assert status == 200 and data["ok"] is True

        reloaded = store.AnalystState.load(vault)
        assert reloaded.claims == [] and reloaded.topics == {}
        _assert_mirror_matches_state(reloaded, srv.llm.conn)  # type: ignore[attr-defined]
    finally:
        srv.stop_event.set()  # type: ignore[attr-defined]
        srv.shutdown()
        thread.join(5)
        srv.server_close()
