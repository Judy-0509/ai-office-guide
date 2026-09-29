"""Unit tests for aioffice.analyst.knowledge: a fixture vault built directly (no LLM calls),
covering as_of correctness, the changes() window, claims() filters, metric_history()'s
revision chain, and that citations are always present.
"""
from __future__ import annotations

from aioffice import db
from aioffice.analyst import knowledge, store


def _report(rid, date, broker, title):
    return {"id": rid, "path": f"reports/{date}_{rid}.md", "broker": broker, "title": title,
            "date": date, "date_source": "front_matter", "status": "learned", "review": None,
            "fictional": True}


def _claim(cid, *, report_id, report_date, broker, topic_id, text, entities, metric, value,
           unit=None, period=None, type="fact", direction="up", relation="new", target=None,
           valid=True, invalid_at=None, invalidated_by=None, number_ok=True):
    return {
        "id": cid, "report_id": report_id, "report_date": report_date, "broker": broker,
        "sids": ["S1"], "text": text, "type": type, "direction": direction,
        "entities": entities, "metric": metric, "value": value, "unit": unit, "period": period,
        "quote": f"{text} 원문", "number_ok": number_ok, "topic_id": topic_id,
        "relation": relation, "target_claim_id": target, "valid": valid,
        "invalid_at": invalid_at, "invalidated_by": invalidated_by,
    }


def _build_vault(tmp_path):
    """r1 (A증권, 07-01): c1 메모리가격=72% (later invalidated by c2), c4 메모리공급 (no number).
    r2 (B증권, 07-10): c2 메모리가격=65% updates c1.
    r3 (C증권, 07-20): c3 iPhone 수율=80% forecast, new topic t002 + a relation t001->t002.
    """
    vault = tmp_path / "vault"
    store.state_dir(vault).mkdir(parents=True)
    (vault / "batches").mkdir()
    (vault / "batches" / "001.md").write_text("# 배치 001\n", encoding="utf-8")

    r1 = _report("r1" + "0" * 62, "2026-07-01", "A증권", "리포트1")
    r2 = _report("r2" + "0" * 62, "2026-07-10", "B증권", "리포트2")
    r3 = _report("r3" + "0" * 62, "2026-07-20", "C증권", "리포트3")

    c1 = _claim("c1", report_id=r1["id"], report_date="2026-07-01", broker="A증권",
                topic_id="t001", text="메모리 가격 72%", entities=["memory"], metric="가격",
                value="72", unit="%", period="2026Q3", relation="new",
                valid=False, invalid_at="2026-07-10", invalidated_by="c2")
    c4 = _claim("c4", report_id=r1["id"], report_date="2026-07-01", broker="A증권",
                topic_id="t001", text="메모리 공급 타이트", entities=["memory"], metric="공급",
                value=None, relation="supports", number_ok=True)
    c2 = _claim("c2", report_id=r2["id"], report_date="2026-07-10", broker="B증권",
                topic_id="t001", text="메모리 가격 65%", entities=["memory"], metric="가격",
                value="65", unit="%", period="2026Q3", relation="updates", target="c1")
    c3 = _claim("c3", report_id=r3["id"], report_date="2026-07-20", broker="C증권",
                topic_id="t002", text="iPhone 수율 80%", entities=["iPhone"], metric="수율",
                value="80", unit="%", type="forecast", relation="new")

    topics = {
        "t001": {"id": "t001", "name": "메모리 가격", "summary": "갱신된 요약", "trend": "up",
                 "summary_history": [
                     {"date": "2026-07-01", "summary": "초기 요약", "trend": "up"},
                     {"date": "2026-07-10", "summary": "갱신된 요약", "trend": "up"},
                 ],
                 "created_report_id": r1["id"], "created_date": "2026-07-01"},
        "t002": {"id": "t002", "name": "폴더블 공급", "summary": "신규 요약", "trend": "flat",
                 "summary_history": [{"date": "2026-07-20", "summary": "신규 요약", "trend": "flat"}],
                 "created_report_id": r3["id"], "created_date": "2026-07-20"},
    }
    relations = [{"from": "t001", "to": "t002", "kind": "related", "why": "부품 연관",
                  "first_seen": "2026-07-20", "last_seen": "2026-07-20", "evidence": ["c3"]}]

    state = store.AnalystState(vault=vault, reports=[r1, r2, r3], claims=[c1, c4, c2, c3],
                                topics=topics, relations=relations)
    state.save_all()

    conn = db.connect(store.db_path(vault))
    db.init_schema(conn)
    store.rebuild_mirror_db(state, conn)
    conn.close()
    return vault


# --- overview + as_of --------------------------------------------------------------------------


def test_overview_counts_and_date_range(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.overview(vault)
    assert out["reports"] == 3
    assert out["claims_valid"] == 3  # c4, c2, c3 (c1 invalidated)
    assert out["claims_invalid"] == 1
    assert out["topics"] == 2
    assert out["relations"] == 1
    assert out["date_range"] == ["2026-07-01", "2026-07-20"]
    assert out["last_batch"] == {"n": 1, "path": "batches/001.md"}
    top_ids = [t["id"] for t in out["top_topics"]]
    assert "t001" in top_ids and "t002" in top_ids


def test_overview_as_of_reflects_state_at_that_date(tmp_path):
    """As of 07-05 (before c2 updates/invalidates c1), c1 must still count as valid."""
    vault = _build_vault(tmp_path)
    out = knowledge.overview(vault, as_of="2026-07-05")
    assert out["reports"] == 1  # only r1 learned by then
    assert out["claims_valid"] == 2  # c1 (not yet invalidated) + c4
    assert out["claims_invalid"] == 0

    out_later = knowledge.overview(vault, as_of="2026-07-15")
    assert out_later["reports"] == 2
    assert out_later["claims_invalid"] == 1  # c1 is invalidated by then


def test_overview_on_an_empty_vault_returns_zeroed_shape(tmp_path):
    vault = tmp_path / "empty_vault"
    store.state_dir(vault).mkdir(parents=True)
    out = knowledge.overview(vault)
    assert out == {"reports": 0, "claims_valid": 0, "claims_invalid": 0, "topics": 0,
                    "relations": 0, "date_range": [None, None], "top_topics": [], "last_batch": None}


def test_knowledge_functions_raise_a_clear_error_when_the_vault_was_never_learned(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        knowledge.overview(tmp_path / "no_such_vault")


# --- topics_list ---------------------------------------------------------------------------


def test_topics_list_sorted_by_valid_claims_desc(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.topics_list(vault)
    assert [t["id"] for t in out] == ["t001", "t002"]  # t001 has 2 valid claims, t002 has 1
    assert out[0]["valid_claims"] == 2
    assert out[0]["trend"] == "up"


# --- search ----------------------------------------------------------------------------------


def test_search_ranks_topics_and_claims_and_includes_citations(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.search(vault, "메모리 가격", k=5)
    assert out["topics"] and out["topics"][0]["id"] == "t001"
    assert out["claims"]
    for c in out["claims"]:
        assert set(c["citation"]) == {"report_id", "report_path", "date", "broker", "title", "quote"}


def test_search_date_filters_apply_to_claims(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.search(vault, "메모리", k=10, date_from="2026-07-15")
    dates = [c["date"] for c in out["claims"]]
    assert all(d >= "2026-07-15" for d in dates)
    assert "2026-07-01" not in dates


# --- topic -----------------------------------------------------------------------------------


def test_topic_by_id_and_by_name(tmp_path):
    vault = _build_vault(tmp_path)
    by_id = knowledge.topic(vault, "t001")
    by_name = knowledge.topic(vault, "메모리 가격")
    assert by_id["id"] == by_name["id"] == "t001"


def test_topic_valid_and_invalidated_claims_with_citations(tmp_path):
    vault = _build_vault(tmp_path)
    t = knowledge.topic(vault, "t001")
    assert {c["id"] for c in t["valid_claims"]} == {"c2", "c4"}
    assert {c["id"] for c in t["invalidated_claims"]} == {"c1"}
    invalidated = t["invalidated_claims"][0]
    assert invalidated["invalidated_by"] == "c2"
    assert invalidated["citation"]["report_path"].startswith("reports/2026-07-01_")


def test_topic_summary_effective_at_as_of_uses_history(tmp_path):
    vault = _build_vault(tmp_path)
    current = knowledge.topic(vault, "t001")
    assert current["summary"] == "갱신된 요약"

    early = knowledge.topic(vault, "t001", as_of="2026-07-05")
    assert early["summary"] == "초기 요약"


def test_topic_related_topics_include_kind_and_why(tmp_path):
    vault = _build_vault(tmp_path)
    t = knowledge.topic(vault, "t001")
    assert t["related_topics"] == [{"id": "t002", "name": "폴더블 공급", "kind": "related", "why": "부품 연관"}]


def test_topic_returns_none_for_an_unknown_reference(tmp_path):
    vault = _build_vault(tmp_path)
    assert knowledge.topic(vault, "없는주제") is None


# --- changes ---------------------------------------------------------------------------------


def test_changes_window_captures_reports_invalidations_and_new_topics(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.changes(vault, "2026-07-05", "2026-07-25")
    assert {r["id"] for r in out["reports"]} == {"r2" + "0" * 62, "r3" + "0" * 62}
    assert {t["id"] for t in out["new_topics"]} == {"t002"}
    assert len(out["invalidations"]) == 1
    inv = out["invalidations"][0]
    assert inv["old"]["id"] == "c1" and inv["new"]["id"] == "c2"
    assert len(out["relations"]) == 1


def test_changes_judgment_change_shows_before_and_after(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.changes(vault, "2026-07-08", "2026-07-12")
    jc = next(j for j in out["judgment_changes"] if j["topic_id"] == "t001")
    assert jc["before"] == "초기 요약"
    assert jc["after"] == "갱신된 요약"


def test_changes_window_excludes_events_outside_it(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.changes(vault, "2026-01-01", "2026-01-31")
    assert out == {"reports": [], "new_topics": [], "judgment_changes": [], "invalidations": [],
                    "relations": []}


# --- claims filters --------------------------------------------------------------------------


def test_claims_defaults_to_valid_only(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.claims(vault)
    assert out["total"] == 3
    assert all(r["valid"] for r in out["rows"])


def test_claims_entity_and_metric_substring_case_insensitive(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.claims(vault, entity="MEMORY", metric="가격", valid="all")
    assert {r["id"] for r in out["rows"]} == {"c1", "c2"}


def test_claims_broker_type_topic_and_date_filters(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.claims(vault, broker="C증권", type="forecast", topic="폴더블 공급")
    assert [r["id"] for r in out["rows"]] == ["c3"]

    out2 = knowledge.claims(vault, date_from="2026-07-15", valid="all")
    assert {r["id"] for r in out2["rows"]} == {"c3"}  # c2 (07-10) is before date_from


def test_claims_valid_invalid_all(tmp_path):
    vault = _build_vault(tmp_path)
    assert knowledge.claims(vault, valid="invalid")["total"] == 1
    assert knowledge.claims(vault, valid="all")["total"] == 4


def test_claims_numeric_only(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.claims(vault, valid="all", numeric_only=True)
    assert all(r["value"] for r in out["rows"])
    assert "c4" not in {r["id"] for r in out["rows"]}  # c4 has no numeric value


def test_claims_limit_and_offset_paginate(tmp_path):
    vault = _build_vault(tmp_path)
    all_rows = knowledge.claims(vault, valid="all", limit=200)["rows"]
    page1 = knowledge.claims(vault, valid="all", limit=2, offset=0)["rows"]
    page2 = knowledge.claims(vault, valid="all", limit=2, offset=2)["rows"]
    assert page1 + page2 == all_rows
    assert knowledge.claims(vault, valid="all", limit=2)["total"] == 4  # total ignores paging


def test_claims_unknown_topic_returns_empty(tmp_path):
    vault = _build_vault(tmp_path)
    assert knowledge.claims(vault, topic="없는주제") == {"rows": [], "total": 0}


def test_claims_rows_carry_citations(tmp_path):
    vault = _build_vault(tmp_path)
    rows = knowledge.claims(vault, valid="all")["rows"]
    for r in rows:
        assert set(r["citation"]) == {"report_id", "report_path", "date", "broker", "title", "quote"}
        assert r["citation"]["report_path"].startswith("reports/")


# --- metric_history ----------------------------------------------------------------------------


def test_metric_history_groups_by_broker_period_unit_with_a_revision_chain(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.metric_history(vault, "memory", "가격")
    assert len(out["groups"]) == 2  # A증권 and B증권, same period/unit but different broker
    a_group = next(g for g in out["groups"] if g["broker"] == "A증권")
    b_group = next(g for g in out["groups"] if g["broker"] == "B증권")
    assert a_group["latest_value"] is None  # its only claim (c1) is invalidated
    assert b_group["latest_value"] == "65"
    assert [c["value"] for c in a_group["chain"]] == ["72"]


def test_metric_history_period_filter(tmp_path):
    vault = _build_vault(tmp_path)
    out = knowledge.metric_history(vault, "iPhone", "수율", period="2026Q3")
    assert out["groups"] == []  # c3 has no period set
    out_all = knowledge.metric_history(vault, "iPhone", "수율")
    assert len(out_all["groups"]) == 1
    assert out_all["groups"][0]["latest_value"] == "80"
