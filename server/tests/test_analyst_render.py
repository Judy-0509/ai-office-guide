from __future__ import annotations

from aioffice.analyst import render, store


def _report(rid="r1", date="2026-07-01", broker="A증권", title="제목"):
    return {"id": rid, "path": f"reports/{date}_a_{rid}.md", "broker": broker, "title": title,
            "date": date, "date_source": "front_matter", "status": "learned", "review": None,
            "fictional": True}


def _claim(**overrides):
    base = {"id": "c1", "report_id": "r1", "report_date": "2026-07-01", "broker": "A증권",
            "sids": ["S1"], "text": "주장 텍스트", "type": "fact", "direction": "up",
            "entities": ["Apple"], "metric": "가격", "value": "10", "unit": "%", "period": None,
            "quote": "원문 인용문", "number_ok": True, "topic_id": "t001", "relation": "new",
            "target_claim_id": None, "valid": True, "invalid_at": None, "invalidated_by": None}
    base.update(overrides)
    return base


def _topic(**overrides):
    base = {"id": "t001", "name": "메모리 가격", "summary": "요약문", "trend": "up",
            "summary_history": [{"date": "2026-07-01", "summary": "요약문", "trend": "up"}],
            "created_report_id": "r1", "created_date": "2026-07-01"}
    base.update(overrides)
    return base


def test_wiki_link_with_and_without_label():
    assert render.wiki_link("topics/t001") == "[[topics/t001]]"
    assert render.wiki_link("topics/t001", "메모리 가격") == "[[topics/t001|메모리 가격]]"


def test_report_markdown_new_claim_shows_new_badge_and_topic_link(tmp_path):
    state = store.AnalystState(vault=tmp_path, reports=[_report()], claims=[_claim()],
                                topics={"t001": _topic()}, relations=[])
    md = render.report_markdown(state, "r1")
    assert "{새로움}" in md
    assert "[[topics/t001|메모리 가격]]" in md
    assert "주장 텍스트" in md
    assert "> 원문 인용문" in md
    assert "(S1)" in md


def test_report_markdown_invalid_claim_is_struck_through_with_invalidator_link(tmp_path):
    invalidator = _claim(id="c2", report_id="r2", report_date="2026-08-01", text="새 주장")
    invalidated = _claim(id="c1", valid=False, invalid_at="2026-08-01", invalidated_by="c2")
    state = store.AnalystState(
        vault=tmp_path,
        reports=[_report(rid="r1"), _report(rid="r2", date="2026-08-01", title="갱신 리포트")],
        claims=[invalidated, invalidator], topics={"t001": _topic()}, relations=[])
    md = render.report_markdown(state, "r1")
    assert "~~주장 텍스트~~" in md
    assert "{무효}" in md
    assert "에 의해 무효" in md
    assert "[[reports/2026-08-01_a_r2|2026-08-01]]" in md


def test_report_markdown_number_check_section_lists_only_failed_claims(tmp_path):
    ok_claim = _claim(id="c1", number_ok=True)
    bad_claim = _claim(id="c2", number_ok=False, value="999", text="틀린 숫자 주장")
    state = store.AnalystState(vault=tmp_path, reports=[_report()], claims=[ok_claim, bad_claim],
                                topics={"t001": _topic()}, relations=[])
    md = render.report_markdown(state, "r1")
    section = md.split("## 숫자 확인 필요")[1]
    assert "틀린 숫자 주장" in section
    assert "999" in section


def test_report_markdown_compare_section_shows_old_to_new(tmp_path):
    old = _claim(id="c1", report_id="r0", report_date="2026-06-01", broker="B증권", text="이전 판단")
    new = _claim(id="c2", report_id="r1", relation="updates", target_claim_id="c1", text="새 판단")
    state = store.AnalystState(
        vault=tmp_path, reports=[_report(rid="r0", date="2026-06-01"), _report(rid="r1")],
        claims=[old, new], topics={"t001": _topic()}, relations=[])
    md = render.report_markdown(state, "r1")
    section = md.split("## 기존 기억과 비교")[1].split("##")[0]
    assert "이전 판단" in section and "새 판단" in section
    assert "B증권" in section and "A증권" in section


def test_topic_markdown_shows_trend_history_relations_and_entities(tmp_path):
    other_topic = _topic(id="t002", name="다른 주제")
    state = store.AnalystState(
        vault=tmp_path, reports=[_report()], claims=[_claim()],
        topics={"t001": _topic(), "t002": other_topic},
        relations=[{"from": "t001", "to": "t002", "kind": "causes", "why": "설명",
                    "first_seen": "2026-07-01", "last_seen": "2026-07-01", "evidence": ["c1"]}])
    md = render.topic_markdown(state, "t001")
    assert "# 메모리 가격" in md
    assert "추세: 상승" in md
    assert "[[reports/2026-07-01_a_r1|A증권 2026-07-01]]" in md
    assert "causes: [[topics/t002|다른 주제]] — 설명" in md
    assert "2026-07-01: 요약문 (추세: 상승)" in md
    assert "[[entities/Apple|Apple]]: 1회" in md


def test_entity_markdown_lists_mentions_with_report_and_topic_links(tmp_path):
    state = store.AnalystState(vault=tmp_path, reports=[_report()], claims=[_claim()],
                                topics={"t001": _topic()}, relations=[])
    md = render.entity_markdown(state, "Apple")
    assert "# Apple" in md
    assert "총 언급: 1회" in md
    assert "[[reports/2026-07-01_a_r1|A증권 2026-07-01]]" in md
    assert "[[topics/t001|메모리 가격]]" in md


def test_index_markdown_lists_recent_changes_and_counts(tmp_path):
    state = store.AnalystState(vault=tmp_path, reports=[_report()], claims=[_claim()],
                                topics={"t001": _topic()}, relations=[])
    md = render.index_markdown(state)
    assert "학습한 리포트 수: 1" in md
    assert "주제 수: 1" in md
    assert "[[topics/t001|메모리 가격]]" in md


def test_write_report_page_writes_to_the_vault_path(tmp_path):
    state = store.AnalystState(vault=tmp_path, reports=[_report()], claims=[_claim()],
                                topics={"t001": _topic()}, relations=[])
    rel_path = render.write_report_page(tmp_path, state, "r1")
    assert (tmp_path / rel_path).exists()
    assert "메모리 가격" in (tmp_path / rel_path).read_text(encoding="utf-8")


def test_log_line_formats_are_korean_and_stable():
    assert "학습 시작" in render.log_line("batch_start", {"batch": 1, "n": 10})
    assert "저장" in render.log_line("report_done", {"report_id": "abc", "path": "reports/x.md"})
    assert render.log_line("unknown_event", {"a": 1}) == "unknown_event: {'a': 1}"
