from __future__ import annotations

import copy
import json
import sqlite3

from aioffice.analyst import steps, store


def _state(vault, topics=None, claims=None, relations=None, reports=None):
    return store.AnalystState(vault=vault, reports=reports or [], claims=claims or [],
                               topics=topics or {}, relations=relations or [])


def _report(rid="r1", date="2026-07-01", broker="A증권", title="제목"):
    return {"id": rid, "path": f"reports/{date}_a_{rid}.md", "broker": broker, "title": title,
            "date": date, "date_source": "front_matter", "status": "learned", "review": None,
            "fictional": True}


def _processed(local_id, text="주장", value=None, entities=None):
    return {"local_id": local_id, "sids": ["S1"], "text": text, "type": "fact", "direction": "up",
            "entities": entities or [], "metric": "가격", "value": value, "unit": "%",
            "period": "2026Q3", "quote": "원문 문장", "number_ok": True}


# --- number check ------------------------------------------------------------------------


def test_process_extract_claims_drops_claims_with_no_valid_sids():
    sid_map = {"S1": "메모리 가격이 10% 상승했다."}
    data = {"claims": [
        {"sids": ["S1"], "text": "유효", "type": "fact", "direction": "up", "value": None},
        {"sids": ["S9"], "text": "무효 인용", "type": "fact", "direction": "up", "value": None},
    ]}
    out = steps.process_extract_claims(data, sid_map)
    assert len(out) == 1
    assert out[0]["text"] == "유효"


def test_process_extract_claims_flags_number_mismatch_but_keeps_the_claim():
    sid_map = {"S1": "매출은 1,999억원을 기록했다."}
    data = {"claims": [
        {"sids": ["S1"], "text": "매출 발표", "value": "1,999", "type": "fact", "direction": "up"},
        {"sids": ["S1"], "text": "틀린 숫자", "value": "2,000", "type": "fact", "direction": "up"},
    ]}
    out = steps.process_extract_claims(data, sid_map)
    assert out[0]["number_ok"] is True
    assert out[1]["number_ok"] is False
    assert out[1]["text"] == "틀린 숫자"  # kept, just flagged


def test_process_extract_claims_null_value_counts_as_number_ok():
    sid_map = {"S1": "정성적 코멘트다."}
    data = {"claims": [{"sids": ["S1"], "text": "의견", "value": None, "type": "opinion", "direction": "none"}]}
    out = steps.process_extract_claims(data, sid_map)
    assert out[0]["number_ok"] is True


# --- integrate application --------------------------------------------------------------------


def test_apply_integration_creates_a_new_topic(tmp_path):
    state = _state(tmp_path)
    report = _report()
    processed = [_processed("c1")]
    result = {"claims": [{"cid": "c1", "topic": {"new": "새 주제"}, "relation": "new", "target": None}],
               "links": []}
    summary = store.apply_integration(state, report, processed, result)

    assert summary["new"] == 1
    assert len(state.topics) == 1
    tid = summary["topics_created"][0]["id"]
    assert state.topics[tid]["name"] == "새 주제"
    assert state.claims[0]["topic_id"] == tid
    assert state.claims[0]["valid"] is True


def test_apply_integration_supports_an_existing_topic():
    state = _state(None, topics={"t001": {"id": "t001", "name": "기존 주제", "summary": "",
                                            "trend": "flat", "summary_history": [],
                                            "created_report_id": "r0", "created_date": "2026-06-01"}})
    processed = [_processed("c1")]
    result = {"claims": [{"cid": "c1", "topic": "t001", "relation": "supports", "target": None}],
               "links": []}
    summary = store.apply_integration(state, _report(), processed, result)

    assert summary["supports"] == 1
    assert summary["topics_created"] == []
    assert summary["topics_updated"] == [{"id": "t001", "name": "기존 주제"}]
    assert len(state.claims) == 1


def test_apply_integration_updates_invalidates_the_target_claim():
    old_claim = {"id": "old-c1", "report_id": "r0", "report_date": "2026-06-01", "broker": "A증권",
                 "sids": ["S1"], "text": "이전 주장", "type": "fact", "direction": "up",
                 "entities": [], "metric": "가격", "value": "5", "unit": "%", "period": None,
                 "quote": "", "number_ok": True, "topic_id": "t001", "relation": "new",
                 "target_claim_id": None, "valid": True, "invalid_at": None, "invalidated_by": None}
    state = _state(None, topics={"t001": {"id": "t001", "name": "주제", "summary": "", "trend": "flat",
                                            "summary_history": [], "created_report_id": "r0",
                                            "created_date": "2026-06-01"}},
                    claims=[old_claim])
    processed = [_processed("c1", text="새 주장")]
    result = {"claims": [{"cid": "c1", "topic": "t001", "relation": "updates", "target": "old-c1"}],
               "links": []}
    summary = store.apply_integration(state, _report(rid="r1", date="2026-07-01"), processed, result)

    assert summary["updates"] == 1
    assert len(summary["invalidated"]) == 1
    assert old_claim["valid"] is False
    assert old_claim["invalid_at"] == "2026-07-01"
    new_claim = next(c for c in state.claims if c["id"] != "old-c1")
    assert old_claim["invalidated_by"] == new_claim["id"]
    assert new_claim["target_claim_id"] == "old-c1"


def test_apply_integration_contradicts_invalidates_the_opposing_claim():
    old_claim = {"id": "old-c1", "report_id": "r0", "report_date": "2026-06-01", "broker": "B증권",
                 "sids": ["S1"], "text": "낙관적 전망", "type": "forecast", "direction": "up",
                 "entities": [], "metric": None, "value": None, "unit": None, "period": None,
                 "quote": "", "number_ok": True, "topic_id": "t001", "relation": "new",
                 "target_claim_id": None, "valid": True, "invalid_at": None, "invalidated_by": None}
    state = _state(None, topics={"t001": {"id": "t001", "name": "주제", "summary": "", "trend": "flat",
                                            "summary_history": [], "created_report_id": "r0",
                                            "created_date": "2026-06-01"}},
                    claims=[old_claim])
    processed = [_processed("c1", text="비관적 전망")]
    result = {"claims": [{"cid": "c1", "topic": "t001", "relation": "contradicts", "target": "old-c1"}],
               "links": []}
    summary = store.apply_integration(state, _report(rid="r1", date="2026-07-05", broker="C투자증권"),
                                       processed, result)

    assert summary["contradicts"] == 1
    assert old_claim["valid"] is False
    assert summary["invalidated"][0]["topic_name"] == "주제"


def test_apply_integration_links_create_topic_relations():
    state = _state(None, topics={
        "t001": {"id": "t001", "name": "주제A", "summary": "", "trend": "flat", "summary_history": [],
                 "created_report_id": "r0", "created_date": "2026-06-01"},
        "t002": {"id": "t002", "name": "주제B", "summary": "", "trend": "flat", "summary_history": [],
                 "created_report_id": "r0", "created_date": "2026-06-01"},
    })
    processed = [_processed("c1")]
    result = {"claims": [{"cid": "c1", "topic": "t001", "relation": "supports", "target": None}],
               "links": [{"from": "t001", "to": "t002", "kind": "causes", "cid": "c1"}]}
    summary = store.apply_integration(state, _report(), processed, result)

    assert len(state.relations) == 1
    rel = state.relations[0]
    assert rel["from"] == "t001" and rel["to"] == "t002" and rel["kind"] == "causes"
    assert rel["evidence"] == [state.claims[0]["id"]]
    assert summary["relations_created"] == [rel]


def test_apply_integration_reuses_an_existing_topic_by_name_instead_of_duplicating():
    state = _state(None, topics={"t001": {"id": "t001", "name": "미분류", "summary": "", "trend": "flat",
                                            "summary_history": [], "created_report_id": "r0",
                                            "created_date": "2026-06-01"}})
    processed = [_processed("c1")]
    result = {"claims": [{"cid": "c1", "topic": {"new": "미분류"}, "relation": "new", "target": None}],
               "links": []}
    summary = store.apply_integration(state, _report(), processed, result)
    assert len(state.topics) == 1  # no duplicate "미분류" topic created
    assert summary["topics_created"] == []
    assert summary["topics_updated"] == [{"id": "t001", "name": "미분류"}]


def test_fallback_integrate_result_routes_every_claim_to_미분류():
    processed = [_processed("c1"), _processed("c2")]
    result = store.fallback_integrate_result(processed)
    assert all(c["topic"] == {"new": "미분류"} and c["relation"] == "new" for c in result["claims"])


# --- rollback exactness -----------------------------------------------------------------------


def test_rollback_restores_pre_report_state_except_audit_fields(tmp_path):
    state = _state(tmp_path)
    r0 = _report(rid="r0", date="2026-06-01")
    r1 = _report(rid="r1", date="2026-07-01")
    state.reports = [r0, r1]

    processed0 = [_processed("c1", text="원래 주장")]
    result0 = {"claims": [{"cid": "c1", "topic": {"new": "주제"}, "relation": "new", "target": None}],
               "links": []}
    store.apply_integration(state, r0, processed0, result0)

    # snapshot BEFORE r1 is learned
    before_claims = copy.deepcopy(state.claims)
    before_topics = copy.deepcopy(state.topics)
    before_relations = copy.deepcopy(state.relations)

    topic_id = next(iter(state.topics))
    old_claim_id = state.claims[0]["id"]
    processed1 = [_processed("c1", text="갱신된 주장")]
    result1 = {"claims": [{"cid": "c1", "topic": topic_id, "relation": "updates", "target": old_claim_id}],
               "links": [{"from": topic_id, "to": topic_id, "kind": "related", "cid": "c1"}]}
    store.apply_integration(state, r1, processed1, result1)
    assert len(state.claims) == 2

    result = store.rollback(state, "r1")

    assert state.claims == before_claims
    assert state.relations == before_relations
    # topics equal except the audit-only "needs_summary" flag rollback sets
    after_topics = copy.deepcopy(state.topics)
    for t in after_topics.values():
        t.pop("needs_summary", None)
    for t in before_topics.values():
        t.pop("needs_summary", None)
    assert after_topics == before_topics
    assert r1["status"] == "rolled_back"
    assert r1["review"] == "wrong"
    assert result["touched_topics"] == [topic_id]


def test_rollback_deletes_a_topic_the_report_created_with_no_claims_left(tmp_path):
    state = _state(tmp_path)
    r1 = _report(rid="r1")
    state.reports = [r1]
    processed = [_processed("c1")]
    result = {"claims": [{"cid": "c1", "topic": {"new": "새 주제"}, "relation": "new", "target": None}],
               "links": []}
    store.apply_integration(state, r1, processed, result)
    assert len(state.topics) == 1

    rollback_result = store.rollback(state, "r1")
    assert state.topics == {}
    assert rollback_result["removed_topics"] == list(rollback_result["removed_topics"])
    assert len(rollback_result["removed_topics"]) == 1


def test_rollback_drops_relation_evidence_and_removes_relations_with_none_left(tmp_path):
    state = _state(tmp_path, topics={
        "t001": {"id": "t001", "name": "A", "summary": "", "trend": "flat", "summary_history": [],
                 "created_report_id": "r0", "created_date": "2026-06-01"},
        "t002": {"id": "t002", "name": "B", "summary": "", "trend": "flat", "summary_history": [],
                 "created_report_id": "r0", "created_date": "2026-06-01"},
    })
    r1 = _report(rid="r1")
    state.reports = [r1]
    processed = [_processed("c1")]
    result = {"claims": [{"cid": "c1", "topic": "t001", "relation": "supports", "target": None}],
               "links": [{"from": "t001", "to": "t002", "kind": "related", "cid": "c1"}]}
    store.apply_integration(state, r1, processed, result)
    assert len(state.relations) == 1

    store.rollback(state, "r1")
    assert state.relations == []


# --- report_changes ----------------------------------------------------------------------------


def test_report_changes_reports_claims_invalidated_and_topics(tmp_path):
    state = _state(tmp_path)
    r0 = _report(rid="r0", date="2026-06-01")
    r1 = _report(rid="r1", date="2026-07-01")
    state.reports = [r0, r1]
    store.apply_integration(state, r0, [_processed("c1", text="원래 주장")],
                             {"claims": [{"cid": "c1", "topic": {"new": "주제"}, "relation": "new",
                                          "target": None}], "links": []})
    topic_id = next(iter(state.topics))
    old_id = state.claims[0]["id"]
    store.apply_integration(state, r1, [_processed("c1", text="갱신")],
                             {"claims": [{"cid": "c1", "topic": topic_id, "relation": "updates",
                                          "target": old_id}], "links": []})

    changes = store.report_changes(state, "r1")
    assert len(changes["claims"]) == 1
    assert changes["claims"][0]["relation"] == "updates"
    assert changes["claims"][0]["target"]["id"] == old_id
    assert changes["invalidated"][0]["id"] == old_id
    assert changes["topics_updated"] == [{"id": topic_id, "name": "주제"}]
    assert changes["topics_created"] == []


# --- graph as_of filtering -----------------------------------------------------------------


def test_graph_as_of_filters_nodes_and_edges_by_date(tmp_path):
    state = _state(tmp_path)
    r0 = _report(rid="r0", date="2026-06-01")
    r1 = _report(rid="r1", date="2026-08-01")
    state.reports = [r0, r1]
    store.apply_integration(state, r0, [_processed("c1", entities=["Apple"])],
                             {"claims": [{"cid": "c1", "topic": {"new": "주제A"}, "relation": "new",
                                          "target": None}], "links": []})
    store.apply_integration(state, r1, [_processed("c1", entities=["Apple"])],
                             {"claims": [{"cid": "c1", "topic": {"new": "주제B"}, "relation": "new",
                                          "target": None}], "links": []})

    full = store.graph(state)
    assert {n["date"] for n in full["nodes"]} == {"2026-06-01", "2026-08-01"}

    filtered = store.graph(state, as_of="2026-07-01")
    dates = {n["date"] for n in filtered["nodes"]}
    assert "2026-08-01" not in dates
    assert "2026-06-01" in dates
    assert all(e["date"] <= "2026-07-01" for e in filtered["edges"])


def test_graph_topic_status_is_invalid_when_all_its_claims_are_invalidated(tmp_path):
    state = _state(tmp_path)
    r0 = _report(rid="r0", date="2026-06-01")
    r1 = _report(rid="r1", date="2026-07-01")
    state.reports = [r0, r1]
    store.apply_integration(state, r0, [_processed("c1")],
                             {"claims": [{"cid": "c1", "topic": {"new": "주제"}, "relation": "new",
                                          "target": None}], "links": []})
    topic_id = next(iter(state.topics))
    old_id = state.claims[0]["id"]
    store.apply_integration(state, r1, [_processed("c1", text="갱신")],
                             {"claims": [{"cid": "c1", "topic": topic_id, "relation": "updates",
                                          "target": old_id}], "links": []})

    g = store.graph(state)
    topic_node = next(n for n in g["nodes"] if n["id"] == f"topics/{topic_id}")
    assert topic_node["status"] == "active"  # the new claim is still valid

    g_old_only = store.graph(state, as_of="2026-06-01")
    topic_node_old = next(n for n in g_old_only["nodes"] if n["id"] == f"topics/{topic_id}")
    assert topic_node_old["status"] == "invalid"  # only the invalidated claim existed by then


def test_graph_entity_nodes_require_at_least_two_mentions(tmp_path):
    state = _state(tmp_path)
    r0 = _report(rid="r0", date="2026-06-01")
    state.reports = [r0]
    store.apply_integration(state, r0, [_processed("c1", entities=["OnlyOnce"])],
                             {"claims": [{"cid": "c1", "topic": {"new": "주제"}, "relation": "new",
                                          "target": None}], "links": []})
    g = store.graph(state)
    assert not any(n["type"] == "entity" for n in g["nodes"])


# --- vault path helpers ------------------------------------------------------------------------


def test_report_filename_is_stable_for_korean_only_broker_names():
    name1 = store.report_filename("2026-07-01", "A증권", "abcd1234" + "0" * 56)
    name2 = store.report_filename("2026-07-01", "A증권", "abcd1234" + "0" * 56)
    assert name1 == name2
    assert name1.endswith("abcd1234.md")


def test_next_topic_id_increments_from_max_existing():
    state = _state(None, topics={"t001": {}, "t003": {}})
    assert state.next_topic_id() == "t004"


def test_next_topic_id_starts_at_t001_when_empty():
    state = _state(None)
    assert state.next_topic_id() == "t001"


# --- jsonl round trip (git-diffable text state) ------------------------------------------------


def test_state_round_trips_through_disk(tmp_path):
    state = _state(tmp_path)
    r1 = _report()
    state.reports = [r1]
    store.apply_integration(state, r1, [_processed("c1")],
                             {"claims": [{"cid": "c1", "topic": {"new": "주제"}, "relation": "new",
                                          "target": None}], "links": []})
    state.save_all()

    reloaded = store.AnalystState.load(tmp_path)
    assert reloaded.reports == state.reports
    assert reloaded.claims == state.claims
    assert reloaded.topics == state.topics
    assert reloaded.relations == state.relations
    # jsonl files are plain UTF-8 text, one JSON object per line -- diffable by git
    assert json.loads(store.claims_path(tmp_path).read_text(encoding="utf-8").splitlines()[0])


# --- batch_progress (events.jsonl -> per-report state machine for the UI) --------------------


def _append(vault, event_type, data):
    store.append_event(vault, event_type, data, ts="2026-07-01T00:00:00")


def test_batch_progress_is_none_when_no_batch_has_ever_started(tmp_path):
    assert store.batch_progress(tmp_path) is None


def test_batch_progress_tracks_the_in_flight_report_through_every_state(tmp_path):
    _append(tmp_path, "batch_start", {"batch": 1, "n": 2})
    _append(tmp_path, "report_start", {"i": 1, "n": 2, "report_id": "r1", "broker": "A증권",
                                        "title": "제목1", "date": "2026-07-01"})

    progress = store.batch_progress(tmp_path)
    assert progress["batch"] == 1 and progress["n"] == 2 and progress["i"] == 1
    assert progress["current"] == "A증권 · 제목1"
    assert len(progress["items"]) == 1
    assert progress["items"][0]["state"] == "waiting"

    _append(tmp_path, "extract_start", {"report_id": "r1"})
    assert store.batch_progress(tmp_path)["items"][0]["state"] == "extracting"

    _append(tmp_path, "extracted", {"report_id": "r1", "claims": 5, "number_fail": 1, "seconds": 3.2})
    item = store.batch_progress(tmp_path)["items"][0]
    assert item["state"] == "extracting"  # unchanged until integrate_start
    assert item["claims"] == 5 and item["number_fail"] == 1 and item["extract_seconds"] == 3.2

    _append(tmp_path, "integrate_start", {"report_id": "r1"})
    assert store.batch_progress(tmp_path)["items"][0]["state"] == "integrating"

    _append(tmp_path, "integrated", {"report_id": "r1", "new": 1, "supports": 2, "updates": 0,
                                      "contradicts": 0, "topics_created": [], "topics_updated": [],
                                      "seconds": 4.5})
    item = store.batch_progress(tmp_path)["items"][0]
    assert item["new"] == 1 and item["supports"] == 2 and item["integrate_seconds"] == 4.5
    assert item["state"] == "integrating"  # unchanged until report_done

    _append(tmp_path, "report_done", {"report_id": "r1", "path": "reports/x.md"})
    assert store.batch_progress(tmp_path)["items"][0]["state"] == "done"


def test_batch_progress_second_report_start_advances_i_and_current(tmp_path):
    _append(tmp_path, "batch_start", {"batch": 1, "n": 2})
    _append(tmp_path, "report_start", {"i": 1, "n": 2, "report_id": "r1", "broker": "A증권",
                                        "title": "제목1", "date": "2026-07-01"})
    _append(tmp_path, "report_done", {"report_id": "r1", "path": "reports/r1.md"})
    _append(tmp_path, "report_start", {"i": 2, "n": 2, "report_id": "r2", "broker": "B증권",
                                        "title": "제목2", "date": "2026-07-02"})

    progress = store.batch_progress(tmp_path)
    assert progress["i"] == 2 and progress["current"] == "B증권 · 제목2"
    assert [item["report_id"] for item in progress["items"]] == ["r1", "r2"]
    assert progress["items"][0]["state"] == "done"
    assert progress["items"][1]["state"] == "waiting"


def test_batch_progress_marks_the_in_flight_report_as_error(tmp_path):
    _append(tmp_path, "batch_start", {"batch": 1, "n": 1})
    _append(tmp_path, "report_start", {"i": 1, "n": 1, "report_id": "r1", "broker": "A증권",
                                        "title": "제목1", "date": "2026-07-01"})
    _append(tmp_path, "extract_start", {"report_id": "r1"})
    _append(tmp_path, "error", {"text": "네트워크 오류"})

    item = store.batch_progress(tmp_path)["items"][0]
    assert item["state"] == "error"
    assert item["error"] == "네트워크 오류"


def test_batch_progress_reports_a_stuck_extracting_item_as_error_when_not_running(tmp_path):
    """A killed process leaves a report frozen mid-extract/integrate with no error event --
    once nothing is running, that must surface as an error, not spin forever in the UI."""
    _append(tmp_path, "batch_start", {"batch": 1, "n": 1})
    _append(tmp_path, "report_start", {"i": 1, "n": 1, "report_id": "r1", "broker": "A증권",
                                        "title": "제목1", "date": "2026-07-01"})
    _append(tmp_path, "extract_start", {"report_id": "r1"})

    still_running = store.batch_progress(tmp_path, running=True)["items"][0]
    assert still_running["state"] == "extracting"
    assert still_running["error"] is None

    stopped = store.batch_progress(tmp_path, running=False)["items"][0]
    assert stopped["state"] == "error"
    assert stopped["error"] == "중단됨"


def test_batch_progress_stuck_integrating_item_also_reports_error_when_not_running(tmp_path):
    _append(tmp_path, "batch_start", {"batch": 1, "n": 1})
    _append(tmp_path, "report_start", {"i": 1, "n": 1, "report_id": "r1", "broker": "A증권",
                                        "title": "제목1", "date": "2026-07-01"})
    _append(tmp_path, "extract_start", {"report_id": "r1"})
    _append(tmp_path, "extracted", {"report_id": "r1", "claims": 3, "number_fail": 0, "seconds": 1.0})
    _append(tmp_path, "integrate_start", {"report_id": "r1"})

    stopped = store.batch_progress(tmp_path, running=False)["items"][0]
    assert stopped["state"] == "error"
    assert stopped["error"] == "중단됨"


def test_batch_progress_does_not_reopen_a_done_item_when_not_running(tmp_path):
    _append(tmp_path, "batch_start", {"batch": 1, "n": 1})
    _append(tmp_path, "report_start", {"i": 1, "n": 1, "report_id": "r1", "broker": "A증권",
                                        "title": "제목1", "date": "2026-07-01"})
    _append(tmp_path, "report_done", {"report_id": "r1", "path": "reports/r1.md"})

    stopped = store.batch_progress(tmp_path, running=False)["items"][0]
    assert stopped["state"] == "done"
    assert stopped["error"] is None


def test_batch_progress_an_error_after_report_done_does_not_reopen_it(tmp_path):
    """A batch/consolidate-scoped error (no report_id) must not mark the already-finished
    report as failed -- it fires positionally after report_done, with current_id stale."""
    _append(tmp_path, "batch_start", {"batch": 1, "n": 1})
    _append(tmp_path, "report_start", {"i": 1, "n": 1, "report_id": "r1", "broker": "A증권",
                                        "title": "제목1", "date": "2026-07-01"})
    _append(tmp_path, "report_done", {"report_id": "r1", "path": "reports/r1.md"})
    _append(tmp_path, "error", {"text": "통합 정리 실패: 어쩌구"})

    item = store.batch_progress(tmp_path)["items"][0]
    assert item["state"] == "done"
    assert item["error"] is None


def test_batch_progress_shows_only_the_most_recent_batch(tmp_path):
    _append(tmp_path, "batch_start", {"batch": 1, "n": 1})
    _append(tmp_path, "report_start", {"i": 1, "n": 1, "report_id": "r1", "broker": "A증권",
                                        "title": "제목1", "date": "2026-07-01"})
    _append(tmp_path, "report_done", {"report_id": "r1", "path": "reports/r1.md"})
    _append(tmp_path, "batch_done", {"batch": 1, "path": "batches/001.md"})
    _append(tmp_path, "batch_start", {"batch": 2, "n": 1})
    _append(tmp_path, "report_start", {"i": 1, "n": 1, "report_id": "r2", "broker": "B증권",
                                        "title": "제목2", "date": "2026-08-01"})

    progress = store.batch_progress(tmp_path)
    assert progress["batch"] == 2
    assert [item["report_id"] for item in progress["items"]] == ["r2"]


def test_batch_progress_is_a_pure_function_of_the_event_log(tmp_path):
    """No in-memory state involved -- a batch driven entirely by `analyst.learn`'s CLI (never
    touching /api/learn) still reconstructs correctly, since batch_progress only reads
    events.jsonl."""
    _append(tmp_path, "batch_start", {"batch": 1, "n": 1})
    _append(tmp_path, "report_start", {"i": 1, "n": 1, "report_id": "r1", "broker": "A증권",
                                        "title": "제목1", "date": "2026-07-01"})
    _append(tmp_path, "report_done", {"report_id": "r1", "path": "reports/r1.md"})

    assert store.batch_progress(tmp_path) == store.batch_progress(tmp_path)  # deterministic replay


# --- rebuild_mirror_db (SQLite mirror of the JSON state) --------------------------------------


def _mirror_conn(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _row_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def test_rebuild_mirror_db_populates_every_table_matching_json_state(tmp_path):
    state = _state(tmp_path)
    r1 = _report(rid="r1", date="2026-07-01")
    state.reports = [r1]
    store.apply_integration(state, r1, [_processed("c1", entities=["Apple"])],
                             {"claims": [{"cid": "c1", "topic": {"new": "주제"}, "relation": "new",
                                          "target": None}],
                              "links": []})
    tid = next(iter(state.topics))
    state.topics[tid]["summary_history"] = [{"date": "2026-07-01", "summary": "요약", "trend": "up"}]
    store.append_feedback(tmp_path, {"report_id": "r1", "claim_id": state.claims[0]["id"],
                                      "text": "원문", "corrected_text": "교정", "corrected_topic_id": tid})

    conn = _mirror_conn(tmp_path)
    store.rebuild_mirror_db(state, conn)

    assert _row_count(conn, "reports") == len(state.reports) == 1
    assert _row_count(conn, "claims") == len(state.claims) == 1
    assert _row_count(conn, "topics") == len(state.topics) == 1
    assert _row_count(conn, "topic_summaries") == 1
    assert _row_count(conn, "feedback") == 1

    claim_row = conn.execute("SELECT * FROM claims").fetchone()
    assert claim_row["id"] == state.claims[0]["id"]
    assert claim_row["valid"] == 1
    assert claim_row["topic_id"] == tid
    assert claim_row["number_ok"] == 1


def test_rebuild_mirror_db_reflects_invalidation_and_relations(tmp_path):
    state = _state(tmp_path, topics={"t001": {"id": "t001", "name": "주제", "summary": "", "trend": "flat",
                                                "summary_history": [], "created_report_id": "r0",
                                                "created_date": "2026-06-01"}})
    old_claim = _claim_dict("old-c1", topic_id="t001")
    state.claims = [old_claim]
    r1 = _report(rid="r1", date="2026-07-01")
    state.reports = [r1]
    store.apply_integration(state, r1, [_processed("c1", text="갱신")],
                             {"claims": [{"cid": "c1", "topic": "t001", "relation": "updates",
                                          "target": "old-c1"}],
                              "links": [{"from": "t001", "to": "t001", "kind": "related", "cid": "c1"}]})

    conn = _mirror_conn(tmp_path)
    store.rebuild_mirror_db(state, conn)

    invalid_row = conn.execute("SELECT valid, invalidated_by FROM claims WHERE id='old-c1'").fetchone()
    assert invalid_row["valid"] == 0
    assert invalid_row["invalidated_by"] is not None
    assert _row_count(conn, "relations") == 1


def test_rebuild_mirror_db_drops_and_recreates_rather_than_appending(tmp_path):
    state = _state(tmp_path)
    r1 = _report(rid="r1")
    state.reports = [r1]
    store.apply_integration(state, r1, [_processed("c1")],
                             {"claims": [{"cid": "c1", "topic": {"new": "주제"}, "relation": "new",
                                          "target": None}], "links": []})
    conn = _mirror_conn(tmp_path)
    store.rebuild_mirror_db(state, conn)
    store.rebuild_mirror_db(state, conn)  # calling it twice must not duplicate rows
    assert _row_count(conn, "reports") == 1
    assert _row_count(conn, "claims") == 1


def test_rebuild_mirror_db_never_touches_llm_calls_table(tmp_path):
    conn = _mirror_conn(tmp_path)
    conn.execute("CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, step TEXT)")
    conn.execute("INSERT INTO llm_calls (step) VALUES ('analyst.extract')")
    conn.commit()

    state = _state(tmp_path)
    store.rebuild_mirror_db(state, conn)

    assert _row_count(conn, "llm_calls") == 1


def _claim_dict(claim_id, *, topic_id):
    return {"id": claim_id, "report_id": "r0", "report_date": "2026-06-01", "broker": "A증권",
            "sids": ["S1"], "text": "이전 주장", "type": "fact", "direction": "up", "entities": [],
            "metric": None, "value": None, "unit": None, "period": None, "quote": "", "number_ok": True,
            "topic_id": topic_id, "relation": "new", "target_claim_id": None, "valid": True,
            "invalid_at": None, "invalidated_by": None}
