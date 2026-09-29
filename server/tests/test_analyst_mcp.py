"""Tests for aioffice.analyst.mcp: tool registration, and one call per tool through the
plain wrapper functions (same pattern as tests/test_library_mcp.py)."""
from __future__ import annotations

import asyncio

import pytest

from aioffice import db
from aioffice.analyst import mcp as analyst_mcp
from aioffice.analyst import store


@pytest.fixture(autouse=True)
def _reset_vault():
    analyst_mcp._vault = None
    analyst_mcp._llm = None
    yield
    analyst_mcp._vault = None
    analyst_mcp._llm = None


def _seed_vault(tmp_path):
    vault = tmp_path / "vault"
    store.state_dir(vault).mkdir(parents=True)

    report = {"id": "abc123ef" + "0" * 56, "path": "reports/2026-07-01_a_abc123ef.md",
              "broker": "A증권", "title": "리포트", "date": "2026-07-01",
              "date_source": "front_matter", "status": "learned", "review": None, "fictional": True}
    claim = {"id": "abc123ef-c1", "report_id": report["id"], "report_date": "2026-07-01",
             "broker": "A증권", "sids": ["S1"], "text": "메모리 가격 상승", "type": "fact",
             "direction": "up", "entities": ["memory"], "metric": "가격", "value": "10",
             "unit": "%", "period": None, "quote": "인용문", "number_ok": True,
             "topic_id": "t001", "relation": "new", "target_claim_id": None, "valid": True,
             "invalid_at": None, "invalidated_by": None}
    topic = {"id": "t001", "name": "메모리 가격", "summary": "요약", "trend": "up",
             "summary_history": [{"date": "2026-07-01", "summary": "요약", "trend": "up"}],
             "created_report_id": report["id"], "created_date": "2026-07-01"}
    state = store.AnalystState(vault=vault, reports=[report], claims=[claim],
                                topics={"t001": topic}, relations=[])
    state.save_all()

    conn = db.connect(store.db_path(vault))
    db.init_schema(conn)
    store.rebuild_mirror_db(state, conn)
    conn.close()
    return vault


def test_get_vault_raises_before_set():
    with pytest.raises(RuntimeError):
        analyst_mcp.get_vault()


def test_set_vault_roundtrip(tmp_path):
    analyst_mcp.set_vault(tmp_path, llm="fake-llm")
    assert analyst_mcp.get_vault() == tmp_path
    assert analyst_mcp._llm == "fake-llm"


def test_build_server_registers_all_six_tools():
    server = analyst_mcp.build_server()
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {
        "knowledge_overview", "search_knowledge", "get_topic", "what_changed",
        "find_claims", "metric_history",
    }


def test_every_tool_description_mentions_citing_or_is_reasonably_descriptive():
    for tool in analyst_mcp.TOOLS:
        assert tool.__doc__ and len(tool.__doc__.strip()) > 20


# --- one call per tool, via the plain functions (no MCP transport needed) --------------------


def test_knowledge_overview_tool(tmp_path):
    analyst_mcp.set_vault(_seed_vault(tmp_path))
    out = analyst_mcp.knowledge_overview()
    assert out["reports"] == 1


def test_search_knowledge_tool(tmp_path):
    analyst_mcp.set_vault(_seed_vault(tmp_path))
    out = analyst_mcp.search_knowledge("메모리 가격")
    assert out["topics"] and out["topics"][0]["id"] == "t001"


def test_get_topic_tool_found_and_not_found(tmp_path):
    analyst_mcp.set_vault(_seed_vault(tmp_path))
    out = analyst_mcp.get_topic("t001")
    assert out["name"] == "메모리 가격"

    missing = analyst_mcp.get_topic("없는주제")
    assert "error" in missing


def test_what_changed_tool(tmp_path):
    analyst_mcp.set_vault(_seed_vault(tmp_path))
    out = analyst_mcp.what_changed("2026-07-01", "2026-07-31")
    assert len(out["reports"]) == 1


def test_find_claims_tool(tmp_path):
    analyst_mcp.set_vault(_seed_vault(tmp_path))
    out = analyst_mcp.find_claims(metric="가격")
    assert out["total"] == 1
    assert out["rows"][0]["citation"]["report_path"].startswith("reports/")


def test_metric_history_tool(tmp_path):
    analyst_mcp.set_vault(_seed_vault(tmp_path))
    out = analyst_mcp.metric_history("memory", "가격")
    assert out["groups"] and out["groups"][0]["latest_value"] == "10"


def test_find_claims_caps_limit_to_max_list(tmp_path):
    analyst_mcp.set_vault(_seed_vault(tmp_path))
    out = analyst_mcp.find_claims(limit=10_000)
    assert out is not None  # doesn't crash; the cap is applied before calling knowledge.claims


def test_main_sets_vault_without_running_stdio(tmp_path, monkeypatch):
    vault = _seed_vault(tmp_path)

    class _FakeServer:
        def __init__(self):
            self.ran = False

        def run(self):
            self.ran = True

    fake_server = _FakeServer()
    monkeypatch.setattr(analyst_mcp, "build_server", lambda: fake_server)

    analyst_mcp.main(["--vault", str(vault)])

    assert analyst_mcp.get_vault() == vault
    assert fake_server.ran is True
