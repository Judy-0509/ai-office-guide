from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aioffice.library import mdlibrary as ml
from aioffice.tools import library_mcp


@pytest.fixture(autouse=True)
def _reset_library():
    library_mcp.set_library(Path("."), {})
    yield
    library_mcp._library = None
    library_mcp._env = {}


def test_get_library_raises_before_set():
    library_mcp._library = None
    with pytest.raises(RuntimeError):
        library_mcp.get_library()


def test_set_library_roundtrip(tmp_path: Path):
    library_mcp.set_library(tmp_path, {"EMBED_BASE_URL": "http://fake"})
    assert library_mcp.get_library() == tmp_path
    assert library_mcp._env == {"EMBED_BASE_URL": "http://fake"}


def test_search_reports_delegates_to_mdlibrary(tmp_path: Path, monkeypatch):
    seen = {}

    def fake_search_chunks(library, query, *, week=None, broker=None, k=8, env=None):
        seen.update(library=library, query=query, week=week, broker=broker, k=k, env=env)
        return [{"file": "x.md"}]

    monkeypatch.setattr(ml, "search_chunks", fake_search_chunks)
    library_mcp.set_library(tmp_path, {"EMBED_BASE_URL": "http://fake"})

    out = library_mcp.search_reports("생산 감축", week="2026-W02", broker="삼성증권", k=3)

    assert out == [{"file": "x.md"}]
    assert seen == {
        "library": tmp_path, "query": "생산 감축", "week": "2026-W02", "broker": "삼성증권",
        "k": 3, "env": {"EMBED_BASE_URL": "http://fake"},
    }


def test_list_reports_delegates_to_mdlibrary(tmp_path: Path, monkeypatch):
    seen = {}

    def fake_list_reports(library, *, week=None, broker=None, company=None):
        seen.update(library=library, week=week, broker=broker, company=company)
        return [{"title": "리포트"}]

    monkeypatch.setattr(ml, "list_reports", fake_list_reports)
    library_mcp.set_library(tmp_path)

    out = library_mcp.list_reports(company="Apple")

    assert out == [{"title": "리포트"}]
    assert seen == {"library": tmp_path, "week": None, "broker": None, "company": "Apple"}


def test_search_reports_end_to_end_against_a_real_index(tmp_path: Path):
    """No mocks: builds a tiny real search.sqlite via mdlibrary and searches through the
    library_mcp wrapper, the same call path an MCP client would make."""
    library = tmp_path / "library"
    conn = ml.open_search_db(library)
    try:
        meta = ml.ReportMeta(id="f" * 16, id12="f" * 12, id8="f" * 8, broker="삼성증권",
                              title="반도체 전망", pub_date="2026-01-05", date_source="regex",
                              date_unknown=False, week="2026-W02")
        ml.index_document(conn, meta, "이번 분기 생산감축 가능성은 낮다는 판단입니다.",
                          "2026-W02/reports/ffffffff.md", {}, embed=False)
    finally:
        conn.close()

    library_mcp.set_library(library)
    results = library_mcp.search_reports("생산 감축")
    assert results and "생산감축" in results[0]["text"]


def test_build_server_registers_both_tools():
    server = library_mcp.build_server()
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"search_reports", "list_reports"}


def test_main_sets_library_and_env_without_running_stdio(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("EMBED_BASE_URL=http://fake\n", encoding="utf-8")

    class _FakeServer:
        def __init__(self):
            self.ran = False

        def run(self):
            self.ran = True

    fake_server = _FakeServer()
    monkeypatch.setattr(library_mcp, "build_server", lambda: fake_server)

    library_mcp.main(["--library", str(tmp_path), "--env", str(env_file)])

    assert library_mcp.get_library() == tmp_path
    assert library_mcp._env.get("EMBED_BASE_URL") == "http://fake"
    assert fake_server.ran is True
