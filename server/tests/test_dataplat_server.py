"""Tests for aioffice.dataplat.server: a real ThreadingHTTPServer covers query long/wide +
caps, history, loads, the admin-token rule for POST /api/refresh, the non-loopback refusal, and
SPA static serving of --site."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from aioffice.config import Settings
from aioffice.dataplat import server as dpserver
from aioffice.dataplat import snapshot, store
from aioffice.dataplat.samples import fake_source
from aioffice.dataplat.source import load_source


def _build_source(tmp_path, state="v1"):
    fake_source.build(tmp_path / "fake_source.sqlite", state=state)
    p = tmp_path / "source.yaml"
    p.write_text("db: fake_source.sqlite\nview: v_dataplat_observations\nrename:\n  broker: source\n",
                  encoding="utf-8")
    return load_source(p)


def _seed(tmp_path):
    source = _build_source(tmp_path)
    db_path = tmp_path / "dataplat.sqlite"
    snapshot.run(source, db_path)
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    return source, db_path, settings


def _maybe_json(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, _maybe_json(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, _maybe_json(exc.read().decode("utf-8")), dict(exc.headers)


def _post(port, path, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST",
                                  headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_build_server_refuses_non_loopback_host_without_token(tmp_path):
    source, db_path, settings = _seed(tmp_path)
    with pytest.raises(SystemExit, match="admin-token"):
        dpserver.build_server(db_path, source, settings, "0.0.0.0", 0)


def test_build_server_allows_non_loopback_with_a_token(tmp_path):
    source, db_path, settings = _seed(tmp_path)
    srv = dpserver.build_server(db_path, source, settings, "0.0.0.0", 0, admin_token="sekrit")
    srv.server_close()


@pytest.fixture()
def running_server(tmp_path):
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<html>대시보드</html>", encoding="utf-8")
    (site_dir / "app.js").write_text("console.log('hi')", encoding="utf-8")

    source, db_path, settings = _seed(tmp_path)
    srv = dpserver.build_server(db_path, source, settings, "127.0.0.1", 0, site=site_dir,
                                 admin_token="sekrit-token", cors="http://dash.local")
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, tmp_path, srv
    finally:
        srv.shutdown()
        thread.join(5)
        srv.server_close()


# --- read endpoints are open, no token needed --------------------------------------------------


def test_catalog_lists_both_datasets(running_server):
    port, *_ = running_server
    status, body, _ = _get(port, "/api/catalog")
    assert status == 200
    names = {d["name"] for d in body["datasets"]}
    assert names == {"shipments", "price_index"}


def test_query_long_default_format(running_server):
    port, *_ = running_server
    status, body, _ = _get(port, "/api/query?dataset=shipments")
    assert status == 200 and len(body["rows"]) > 0


def test_query_missing_dataset_is_400(running_server):
    port, *_ = running_server
    status, body, _ = _get(port, "/api/query")
    assert status == 400 and "error" in body


def test_query_wide_pivots_entity_by_period(running_server):
    port, *_ = running_server
    status, body, _ = _get(port, "/api/query?dataset=shipments&format=wide&rows=entity&cols=period")
    assert status == 200
    assert body["columns"][0] == "entity"


def test_query_wide_cell_cap_returns_400(running_server, monkeypatch):
    port, *_ = running_server
    monkeypatch.setattr(store, "QUERY_CELL_CAP", 1)
    status, body, _ = _get(port, "/api/query?dataset=shipments&format=wide&rows=entity&cols=period")
    assert status == 400 and "error" in body


def test_query_row_cap_returns_400(running_server, monkeypatch):
    port, *_ = running_server
    monkeypatch.setattr(store, "QUERY_ROW_CAP", 1)
    status, body, _ = _get(port, "/api/query?dataset=shipments")
    assert status == 400


def test_history_requires_all_params(running_server):
    port, *_ = running_server
    status, body, _ = _get(port, "/api/history?dataset=shipments")
    assert status == 400


def test_history_returns_points(running_server):
    port, *_ = running_server
    status, body, _ = _get(
        port, "/api/history?dataset=shipments&metric=%EC%B6%9C%ED%95%98%EB%9F%89&entity=%EB%AA%A8%EB%8D%B8A&period=2024Q1")
    assert status == 200 and "history" in body


def test_loads_list_and_detail(running_server):
    port, *_ = running_server
    status, body, _ = _get(port, "/api/loads?dataset=shipments")
    assert status == 200 and body["loads"]
    load_id = body["loads"][0]["id"]
    status2, body2, _ = _get(port, f"/api/loads/{load_id}")
    assert status2 == 200 and body2["id"] == load_id and "report" in body2


def test_load_detail_404(running_server):
    port, *_ = running_server
    status, body, _ = _get(port, "/api/loads/999999")
    assert status == 404


def test_cors_header_present(running_server):
    port, *_ = running_server
    _status, _body, headers = _get(port, "/api/catalog")
    assert headers.get("Access-Control-Allow-Origin") == "http://dash.local"


# --- SPA static serving --------------------------------------------------------------------------


def test_spa_serves_known_asset(running_server):
    port, *_ = running_server
    status, body, headers = _get(port, "/app.js")
    assert status == 200


def test_spa_falls_back_to_index_for_unknown_route(running_server):
    port, *_ = running_server
    req = urllib.request.Request(f"http://127.0.0.1:{port}/dashboard/some/deep/route")
    with urllib.request.urlopen(req) as r:
        body = r.read().decode("utf-8")
    assert "대시보드" in body


def test_spa_path_traversal_is_rejected(running_server):
    port, *_ = running_server
    status, body, _ = _get(port, "/../../etc/passwd")
    assert status in (400, 404)  # never 200 with file contents


# --- POST /api/refresh admin-token rule --------------------------------------------------------


def test_refresh_without_token_is_401(running_server):
    port, *_ = running_server
    status, body = _post(port, "/api/refresh", {})
    assert status == 401


def test_refresh_with_wrong_token_is_401(running_server):
    port, *_ = running_server
    status, body = _post(port, "/api/refresh", {}, headers={"Authorization": "Bearer nope"})
    assert status == 401


def test_refresh_with_correct_token_returns_202_and_completes(running_server):
    import time

    port, tmp_path, srv = running_server
    status, body = _post(port, "/api/refresh", {}, headers={"Authorization": "Bearer sekrit-token"})
    assert status == 202 and body["ok"] is True
    for _ in range(50):
        _status, status_body, _ = _get(port, "/api/refresh/status")
        if not status_body["running"]:
            break
        time.sleep(0.05)
    assert status_body["last_result"]["ok"] is True


def test_refresh_returns_409_when_already_running(running_server, monkeypatch):
    import time

    port, tmp_path, srv = running_server
    slow_event = threading.Event()

    def slow_run_refresh_once(*args, **kwargs):
        slow_event.wait(2)
        return {"ok": True, "stage": "snapshot", "results": []}

    monkeypatch.setattr(snapshot, "run_refresh_once", slow_run_refresh_once)
    status1, body1 = _post(port, "/api/refresh", {}, headers={"Authorization": "Bearer sekrit-token"})
    assert status1 == 202
    status2, body2 = _post(port, "/api/refresh", {}, headers={"Authorization": "Bearer sekrit-token"})
    assert status2 == 409
    slow_event.set()
    time.sleep(0.1)


def test_x_api_key_header_also_authorizes_refresh(running_server, monkeypatch):
    port, *_ = running_server
    monkeypatch.setattr(snapshot, "run_refresh_once",
                         lambda *a, **k: {"ok": True, "stage": "snapshot", "results": []})
    status, body = _post(port, "/api/refresh", {}, headers={"X-API-Key": "sekrit-token"})
    assert status == 202


def test_chat_page_served(running_server):
    port, *_ = running_server
    status, body, headers = _get(port, "/chat")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
