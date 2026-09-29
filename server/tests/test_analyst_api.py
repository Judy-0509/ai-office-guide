"""Tests for aioffice.analyst.api: dispatch() covers every endpoint (fast, no live server);
a real ThreadingHTTPServer covers token enforcement, CORS, and the non-loopback refusal.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from aioffice import db
from aioffice.analyst import api, store
from aioffice.config import Settings


def _seed_vault(tmp_path):
    vault = tmp_path / "vault"
    store.state_dir(vault).mkdir(parents=True)
    (vault / "batches").mkdir()
    (vault / "batches" / "001.md").write_text("# 배치 001\n", encoding="utf-8")

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
    return vault, report, claim, topic


# --- dispatch() covers every endpoint (no live server needed) ---------------------------------


def test_dispatch_root_lists_endpoints(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    status, body = api.dispatch(vault, "/api/v1", {})
    assert status == 200
    paths = {e["path"] for e in body["endpoints"]}
    assert "/api/v1/overview" in paths and "/api/v1/claims" in paths


def test_dispatch_overview(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    status, body = api.dispatch(vault, "/api/v1/overview", {})
    assert status == 200 and body["reports"] == 1


def test_dispatch_search_requires_query(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    status, body = api.dispatch(vault, "/api/v1/search", {})
    assert status == 400 and "error" in body

    status, body = api.dispatch(vault, "/api/v1/search", {"query": ["가격"]})
    assert status == 200 and "topics" in body and "claims" in body


def test_dispatch_topics_list(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    status, body = api.dispatch(vault, "/api/v1/topics", {})
    assert status == 200
    assert body["topics"][0]["id"] == "t001"


def test_dispatch_topic_detail_and_404(tmp_path):
    vault, _report, _claim, topic = _seed_vault(tmp_path)
    status, body = api.dispatch(vault, f"/api/v1/topics/{topic['id']}", {})
    assert status == 200 and body["name"] == "메모리 가격"

    status, body = api.dispatch(vault, "/api/v1/topics/없는주제", {})
    assert status == 404 and "error" in body


def test_dispatch_topic_detail_url_decodes_korean_name(tmp_path):
    import urllib.parse
    vault, *_ = _seed_vault(tmp_path)
    encoded = urllib.parse.quote("메모리 가격")
    status, body = api.dispatch(vault, f"/api/v1/topics/{encoded}", {})
    assert status == 200 and body["id"] == "t001"


def test_dispatch_changes_requires_both_dates(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    status, body = api.dispatch(vault, "/api/v1/changes", {"date_from": ["2026-07-01"]})
    assert status == 400

    status, body = api.dispatch(vault, "/api/v1/changes",
                                 {"date_from": ["2026-07-01"], "date_to": ["2026-07-31"]})
    assert status == 200 and len(body["reports"]) == 1


def test_dispatch_claims(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    status, body = api.dispatch(vault, "/api/v1/claims", {"metric": ["가격"]})
    assert status == 200 and body["total"] == 1


def test_dispatch_metric_history_requires_entity_and_metric(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    status, body = api.dispatch(vault, "/api/v1/metric_history", {"entity": ["memory"]})
    assert status == 400

    status, body = api.dispatch(vault, "/api/v1/metric_history",
                                 {"entity": ["memory"], "metric": ["가격"]})
    assert status == 200 and body["groups"]


def test_dispatch_unknown_path_is_404(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    status, body = api.dispatch(vault, "/api/v1/nope", {})
    assert status == 404


def test_dispatch_vault_not_found_is_404_json(tmp_path):
    status, body = api.dispatch(tmp_path / "nope", "/api/v1/overview", {})
    assert status == 404 and "error" in body


# --- standalone server: security + CORS ------------------------------------------------------


def test_build_server_refuses_non_loopback_host_without_token(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    with pytest.raises(SystemExit, match="token"):
        api.build_server(vault, settings, "0.0.0.0", 0)


def test_build_server_allows_non_loopback_with_a_token(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    srv = api.build_server(vault, settings, "0.0.0.0", 0, token="sekrit")
    srv.server_close()


@pytest.fixture()
def api_server(tmp_path):
    vault, report, claim, topic = _seed_vault(tmp_path)
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    srv = api.build_server(vault, settings, "127.0.0.1", 0, token="sekrit-token", cors="http://dash.local")
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, vault, report, claim, topic
    finally:
        srv.shutdown()
        thread.join(5)
        srv.server_close()


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), dict(exc.headers)


def test_missing_token_is_401(api_server):
    port, *_ = api_server
    status, body, _headers = _get(port, "/api/v1/overview")
    assert status == 401 and "error" in body


def test_wrong_token_is_401(api_server):
    port, *_ = api_server
    status, body, _headers = _get(port, "/api/v1/overview",
                                   {"Authorization": "Bearer nope"})
    assert status == 401


def test_correct_bearer_token_succeeds(api_server):
    port, *_ = api_server
    status, body, _headers = _get(port, "/api/v1/overview",
                                   {"Authorization": "Bearer sekrit-token"})
    assert status == 200 and body["reports"] == 1


def test_correct_x_api_key_header_also_succeeds(api_server):
    port, *_ = api_server
    status, body, _headers = _get(port, "/api/v1/overview", {"X-API-Key": "sekrit-token"})
    assert status == 200


def test_cors_header_present_only_when_configured(api_server):
    port, *_ = api_server
    _status, _body, headers = _get(port, "/api/v1/overview",
                                    {"Authorization": "Bearer sekrit-token"})
    assert headers.get("Access-Control-Allow-Origin") == "http://dash.local"


def test_cors_header_absent_without_config(tmp_path):
    vault, *_ = _seed_vault(tmp_path)
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    srv = api.build_server(vault, settings, "127.0.0.1", 0)  # no token, no cors
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        _status, _body, headers = _get(port, "/api/v1/overview")
        assert "Access-Control-Allow-Origin" not in headers
    finally:
        srv.shutdown()
        thread.join(5)
        srv.server_close()


def test_post_is_not_supported(api_server):
    port, *_ = api_server
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/overview", method="POST",
                                  headers={"Authorization": "Bearer sekrit-token"})
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code in (501, 405)
