from __future__ import annotations

import subprocess

from aioffice.llm import BackendError
from aioffice.analyst import learn, store


def _write_report(inbox, name, broker, title, date, body):
    (inbox / name).write_text(
        f"---\nbroker: {broker}\ntitle: {title}\ndate: {date}\nfictional: true\n---\n\n{body}\n",
        encoding="utf-8",
    )


REPORT_BODY = ("메모리 반도체 계약 가격이 이번 분기 10% 상승했다고 밝혔다. "
               "이는 스마트폰 부품 원가 부담을 키우는 요인으로 지목된다. "
               "폴더블 아이폰용 힌지 부품의 수율도 이번 달 크게 개선됐다고 전했다.")


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


def _integrate_new_topics_response():
    return {
        "claims": [
            {"cid": "c1", "topic": {"new": "메모리 가격 상승"}, "relation": "new", "target": None},
            {"cid": "c2", "topic": {"new": "폴더블 아이폰 공급"}, "relation": "new", "target": None},
        ],
        "links": [],
    }


def _consolidate_response(topic_ids):
    return {"topics": [{"id": tid, "summary": f"{tid} 요약", "trend": "up"} for tid in topic_ids],
            "relations": []}


def _empty_consolidate_response():
    return {"topics": [], "relations": []}


def _events(vault):
    return [(e["event"], e["data"]) for e in store.read_events(vault)]


# --- one report end to end + git commit per report/batch --------------------------------------


def test_learn_batch_commits_per_report_and_per_batch(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01", REPORT_BODY)

    git_ok = learn.init_vault(vault)
    assert git_ok is True

    analyst_llm.backend.responses.extend([
        _extract_response(), _integrate_new_topics_response(),
        _consolidate_response(["t001", "t002"]),
    ])
    result = learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)

    assert result["reports"] == 1
    log = subprocess.run(["git", "log", "--format=%s"], cwd=vault, capture_output=True).stdout
    lines = [line for line in log.decode("utf-8").splitlines() if line.strip()]
    assert any(line.startswith("학습: 2026-07-01 A증권 리포트1") for line in lines)
    assert any(line.startswith("배치 001 정리") for line in lines)


def test_learn_batch_writes_report_and_topic_pages(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01", REPORT_BODY)
    git_ok = learn.init_vault(vault)

    analyst_llm.backend.responses.extend([_extract_response(), _integrate_new_topics_response(),
                                           _consolidate_response(["t001", "t002"])])
    learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)

    state = store.AnalystState.load(vault)
    assert len(state.reports) == 1
    report = state.reports[0]
    assert (vault / report["path"]).exists()
    assert len(state.topics) == 2
    for tid in state.topics:
        assert (vault / "topics" / f"{tid}.md").exists()
    assert (vault / "batches" / "001.md").exists()
    assert "학습" in (vault / "log.md").read_text(encoding="utf-8")


def test_index_reflects_each_report_immediately_not_only_at_batch_end(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01", REPORT_BODY)
    _write_report(inbox, "r2.md", "B Securities", "리포트2", "2026-07-02", REPORT_BODY)
    git_ok = learn.init_vault(vault)

    analyst_llm.backend.responses.extend([
        _extract_response(), _integrate_new_topics_response(),
        _extract_response(), _integrate_new_topics_response(),
        _empty_consolidate_response(),
    ])

    snapshots = []

    def on_event(event_type, data):
        if event_type == "report_done":
            snapshots.append((vault / "index.md").read_text(encoding="utf-8"))

    learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok, on_event=on_event)

    assert len(snapshots) == 2
    assert "학습한 리포트 수: 1" in snapshots[0]
    assert "학습한 리포트 수: 2" in snapshots[1]


# --- events emitted in contract order -----------------------------------------------------


def test_events_are_emitted_in_contract_order(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01", REPORT_BODY)
    git_ok = learn.init_vault(vault)

    analyst_llm.backend.responses.extend([_extract_response(), _integrate_new_topics_response(),
                                           _consolidate_response(["t001", "t002"])])
    learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)

    events = [e for e, _ in _events(vault)]
    assert events == ["batch_start", "report_start", "extract_start", "extracted",
                       "integrate_start", "integrated", "report_done", "consolidated", "batch_done"]


def test_extracted_event_carries_claim_and_number_fail_counts(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01", REPORT_BODY)
    git_ok = learn.init_vault(vault)
    analyst_llm.backend.responses.extend([_extract_response(), _integrate_new_topics_response(),
                                           _consolidate_response(["t001", "t002"])])
    learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)

    data = dict(_events(vault))["extracted"]
    assert data["claims"] == 2
    assert data["number_fail"] == 0
    assert "report_id" in data and "seconds" in data


# --- two reports: new topic then updates -> invalidation --------------------------------------


def test_second_report_updates_and_invalidates_first(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01", REPORT_BODY)
    _write_report(inbox, "r2.md", "B Securities", "리포트2", "2026-07-10", REPORT_BODY)
    git_ok = learn.init_vault(vault)

    analyst_llm.backend.responses.extend([_extract_response(), _integrate_new_topics_response(),
                                           _empty_consolidate_response()])
    learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 1, git_ok)  # batch of 1: r1 only

    state = store.AnalystState.load(vault)
    topic_id = next(tid for tid, t in state.topics.items() if t["name"] == "메모리 가격 상승")
    old_claim_id = next(c["id"] for c in state.claims if c["topic_id"] == topic_id)

    def integrate_update_response(data):
        return {"claims": [
            {"cid": "c1", "topic": topic_id, "relation": "updates", "target": old_claim_id},
            {"cid": "c2", "topic": {"new": "폴더블 아이폰 공급"}, "relation": "new", "target": None},
        ], "links": []}

    analyst_llm.backend.responses.extend([_extract_response(), integrate_update_response(None),
                                           _empty_consolidate_response()])
    learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 1, git_ok)

    state = store.AnalystState.load(vault)
    old_claim = state.claim_by_id(old_claim_id)
    assert old_claim["valid"] is False
    assert old_claim["invalid_at"] == "2026-07-10"


# --- invalid integrate output retries once then falls back to 미분류 --------------------------


def test_integrate_falls_back_to_미분류_after_two_invalid_attempts(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01", REPORT_BODY)
    git_ok = learn.init_vault(vault)

    bad_response = {"claims": [{"cid": "wrong-id", "topic": "nope", "relation": "new"}], "links": []}
    analyst_llm.backend.responses.extend([_extract_response(), bad_response, bad_response,
                                           _empty_consolidate_response()])
    result = learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)

    state = store.AnalystState.load(vault)
    assert result["reports"] == 1
    assert any(t["name"] == "미분류" for t in state.topics.values())
    events = [e for e, _ in _events(vault)]
    assert "error" in events


# --- learn/stop cooperative cancel --------------------------------------------------------------


def test_should_stop_halts_before_the_next_report(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "리포트1", "2026-07-01", REPORT_BODY)
    _write_report(inbox, "r2.md", "B Securities", "리포트2", "2026-07-10", REPORT_BODY)
    git_ok = learn.init_vault(vault)

    analyst_llm.backend.responses.extend([_extract_response(), _integrate_new_topics_response(),
                                           _empty_consolidate_response()])
    result = learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok,
                                should_stop=lambda: len(store.AnalystState.load(vault).reports) >= 1)
    assert result["reports"] == 1


# --- git missing: warn once, continue without commits -------------------------------------------


def test_init_vault_continues_without_commits_when_git_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(learn, "_git_available", lambda: False)
    git_ok = learn.init_vault(tmp_path / "vault2")
    assert git_ok is False
    assert "git" in capsys.readouterr().out
    # a subsequent commit call must be a silent no-op, not an error
    learn.git_commit(tmp_path / "vault2", "학습: 테스트", git_ok)


# --- a hard extract failure must not stop the batch ---------------------------------------------


def _events(vault):
    return [(e["event"], e["data"]) for e in store.read_events(vault)]


def test_extract_failure_marks_the_report_error_and_continues_the_batch(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "실패할 리포트", "2026-07-01", REPORT_BODY)
    _write_report(inbox, "r2.md", "B Securities", "정상 리포트", "2026-07-02", REPORT_BODY)
    git_ok = learn.init_vault(vault)

    # r1's extract exhausts complete_json's own 2 attempts (both BackendError) -> LLMError,
    # which learn_one_report must catch; r2 proceeds normally afterward.
    analyst_llm.backend.responses.extend([
        BackendError("연결 끊김"), BackendError("연결 끊김"),
        _extract_response(), _integrate_new_topics_response(),
        _empty_consolidate_response(),
    ])

    result = learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)

    assert result["reports"] == 2
    state = store.AnalystState.load(vault)
    r1 = next(r for r in state.reports if r["title"] == "실패할 리포트")
    r2 = next(r for r in state.reports if r["title"] == "정상 리포트")
    assert r1["status"] == "error"
    assert r2["status"] == "learned"
    assert state.claims_for_report(r1["id"]) == []  # nothing was integrated for the failure

    events = dict(_events(vault))
    assert events["error"]["report_id"] == r1["id"]
    assert "추출 실패" in events["error"]["text"]

    # no commit for the failed report; the good one still got its own commit
    log = subprocess.run(["git", "log", "--format=%s"], cwd=vault, capture_output=True).stdout.decode("utf-8")
    lines = [line for line in log.splitlines() if line.strip()]
    assert not any("실패할 리포트" in line for line in lines)
    assert any("정상 리포트" in line for line in lines)


def test_extract_failure_report_is_retried_in_a_later_batch(tmp_path, analyst_llm):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox, "r1.md", "A증권", "실패할 리포트", "2026-07-01", REPORT_BODY)
    git_ok = learn.init_vault(vault)

    analyst_llm.backend.responses.extend([BackendError("연결 끊김"), BackendError("연결 끊김")])
    result = learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)
    assert result["reports"] == 1
    state = store.AnalystState.load(vault)
    assert len(state.reports) == 1 and state.reports[0]["status"] == "error"

    # a fresh scan_inbox call must still see it as pending
    from aioffice.analyst import ingest
    pending, _warnings = ingest.scan_inbox(inbox, state.learned_ids())
    assert len(pending) == 1

    # retrying it replaces the "error" record rather than duplicating it
    analyst_llm.backend.responses.extend([_extract_response(), _integrate_new_topics_response(),
                                           _empty_consolidate_response()])
    learn.learn_batch(vault, inbox, analyst_llm, "fake-model", 8000, 10, git_ok)
    state = store.AnalystState.load(vault)
    assert len(state.reports) == 1
    assert state.reports[0]["status"] == "learned"
