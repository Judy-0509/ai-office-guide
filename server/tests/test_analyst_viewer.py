from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from aioffice import db
from aioffice.analyst import store, viewer
from aioffice.config import Settings


@pytest.fixture()
def analyst_server(tmp_path):
    vault = tmp_path / "vault"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data"),
                                             "LLM_MODEL_DEFAULT": "qwen-test"})
    srv = viewer.build_server(vault, inbox, settings, "qwen-test", 8000, "127.0.0.1", 0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, port, vault, inbox
    finally:
        srv.stop_event.set()
        srv.shutdown()
        thread.join(5)
        srv.server_close()


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post(port, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _seed_state(vault):
    state = store.AnalystState.load(vault)
    report = {"id": "abc123ef" + "0" * 56, "path": "reports/2026-07-01_a_abc123ef.md",
              "broker": "A증권", "title": "리포트", "date": "2026-07-01",
              "date_source": "front_matter", "status": "learned", "review": None, "fictional": True}
    claim = {"id": "abc123ef-c1", "report_id": report["id"], "report_date": "2026-07-01",
             "broker": "A증권", "sids": ["S1"], "text": "주장", "type": "fact", "direction": "up",
             "entities": ["Apple"], "metric": "가격", "value": "10", "unit": "%", "period": None,
             "quote": "인용", "number_ok": True, "topic_id": "t001", "relation": "new",
             "target_claim_id": None, "valid": True, "invalid_at": None, "invalidated_by": None}
    topic = {"id": "t001", "name": "주제", "summary": "요약", "trend": "up", "summary_history": [],
             "created_report_id": report["id"], "created_date": "2026-07-01"}
    state.reports = [report]
    state.claims = [claim]
    state.topics = {"t001": topic}
    state.save_all()
    from aioffice.analyst import render
    render.write_report_page(vault, state, report["id"])
    render.write_topic_page(vault, state, "t001")
    render.write_index_and_log(vault, state)

    # also keep the SQL mirror in sync (viewer.build_server only rebuilds it once, at
    # startup, before this seeding runs) -- a fresh short-lived connection is enough.
    conn = db.connect(store.db_path(vault))
    db.init_schema(conn)
    store.rebuild_mirror_db(state, conn)
    conn.close()

    return report, claim, topic


# --- port fallback (shared by `viewer.main()` and `analyst.run`) --------------------------


def test_find_open_port_returns_the_port_itself_when_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert viewer.find_open_port("127.0.0.1", free_port, tries=1) == free_port


def test_find_open_port_skips_a_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        busy_port = blocker.getsockname()[1]
        blocker.listen(1)
        port = viewer.find_open_port("127.0.0.1", busy_port, tries=10)
    assert busy_port < port <= busy_port + 10


def test_find_open_port_raises_when_every_try_is_busy(monkeypatch):
    monkeypatch.setattr(viewer, "_port_is_free", lambda host, port: False)
    try:
        viewer.find_open_port("127.0.0.1", 12345, tries=3)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "12345" in str(exc)


def test_viewer_main_falls_back_to_the_next_port_when_the_requested_one_is_busy(
    tmp_path, monkeypatch, capsys,
):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        busy_port = blocker.getsockname()[1]
        blocker.listen(1)

        built = {}
        real_build_server = viewer.build_server

        def fake_build_server(vault_, inbox_, settings, model, max_tokens, host, port):
            srv = real_build_server(vault_, inbox_, settings, model, max_tokens, host, port)
            built["server"] = srv
            built["port"] = port
            raise SystemExit  # stop main() right after it builds the server -- no serve_forever

        monkeypatch.setattr(viewer, "build_server", fake_build_server)
        try:
            viewer.main(["--vault", str(vault), "--inbox", str(inbox), "--port", str(busy_port)])
        except SystemExit:
            pass
        finally:
            if "server" in built:
                built["server"].server_close()

    assert built["port"] != busy_port
    out = capsys.readouterr().out
    assert "사용 중이라" in out


# --- basic GET endpoints -----------------------------------------------------------------------


def test_index_serves_placeholder_when_no_viewer_html(analyst_server):
    _srv, port, _vault, _inbox = analyst_server
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
        body = r.read().decode("utf-8")
        content_type = r.headers.get("Content-Type")
    assert "text/html" in content_type
    assert "html" in body.lower()


def test_api_v1_is_mounted_on_the_viewer_server(analyst_server):
    """The knowledge HTTP API is reachable at /api/v1/... on the same viewer server, with
    no extra auth (the viewer already binds 127.0.0.1 by default)."""
    _srv, port, vault, _inbox = analyst_server
    _seed_state(vault)
    status, data = _get(port, "/api/v1/overview")
    assert status == 200
    assert data["reports"] == 1

    status, data = _get(port, "/api/v1/topics/t001")
    assert status == 200 and data["name"] == "주제"


def test_status_reports_counts_and_model(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    _seed_state(vault)
    status, data = _get(port, "/api/status")
    assert status == 200
    assert data["running"] is False
    assert data["learned"] == 1
    assert data["topics"] == 1
    assert data["claims"] == 1
    assert data["model"] == "qwen-test"


def test_status_reports_a_stuck_item_as_error_when_nothing_is_running(analyst_server):
    """Simulates a killed run: extract_start logged, no report_done/error ever followed, and
    the server is not currently running a batch -- /api/status must not show it spinning."""
    _srv, port, vault, _inbox = analyst_server
    store.append_event(vault, "batch_start", {"batch": 1, "n": 1}, ts="2026-07-01T00:00:00")
    store.append_event(vault, "report_start", {"i": 1, "n": 1, "report_id": "r1",
                                                "broker": "A증권", "title": "제목", "date": "2026-07-01"},
                        ts="2026-07-01T00:00:01")
    store.append_event(vault, "extract_start", {"report_id": "r1"}, ts="2026-07-01T00:00:02")

    status, data = _get(port, "/api/status")
    assert status == 200
    assert data["running"] is False
    item = data["progress"]["items"][0]
    assert item["state"] == "error"
    assert item["error"] == "중단됨"


def test_tree_lists_topics_reports_entities_sorted(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    _seed_state(vault)
    status, data = _get(port, "/api/tree")
    assert status == 200
    assert data["topics"][0]["id"] == "t001"
    assert data["reports"][0]["broker"] == "A증권"
    assert any(e["name"] == "Apple" for e in data["entities"])
    assert data["index"] == "index.md"
    assert data["log"] == "log.md"


def test_page_returns_markdown_for_a_valid_path(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    report, _claim, _topic = _seed_state(vault)
    status, data = _get(port, f"/api/page?path={report['path']}")
    assert status == 200
    assert data["path"] == report["path"]
    assert "주제" in data["markdown"]


def test_page_404s_for_a_missing_file(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    _seed_state(vault)
    status, data = _get(port, "/api/page?path=reports/nope.md")
    assert status == 404
    assert "error" in data


@pytest.mark.parametrize("bad_path,already_encoded", [
    ("../../etc/passwd", False),
    ("..%2F..%2Fsecret", True),  # a raw encoded slash in the query value -- single-decodes to ".."
    ("/etc/passwd", False),
])
def test_page_rejects_path_traversal_and_absolute_paths(analyst_server, bad_path, already_encoded):
    _srv, port, vault, _inbox = analyst_server
    _seed_state(vault)
    query_value = bad_path if already_encoded else urllib.parse.quote(bad_path, safe="")
    status, data = _get(port, f"/api/page?path={query_value}")
    assert status == 400
    assert "error" in data


def test_graph_endpoint_returns_nodes_and_edges(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    _seed_state(vault)
    status, data = _get(port, "/api/graph")
    assert status == 200
    assert any(n["type"] == "topic" for n in data["nodes"])


def test_report_changes_endpoint(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    report, _claim, _topic = _seed_state(vault)
    status, data = _get(port, f"/api/report_changes?id={report['id']}")
    assert status == 200
    assert len(data["claims"]) == 1


def test_report_changes_404_for_unknown_id(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    _seed_state(vault)
    status, data = _get(port, "/api/report_changes?id=nope")
    assert status == 404


# --- SSE replay -----------------------------------------------------------------------------


def test_events_sse_replays_last_events_on_connect(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    store.append_event(vault, "batch_start", {"batch": 1, "n": 5}, ts="2026-07-01T00:00:00")
    store.append_event(vault, "report_done", {"report_id": "x", "path": "reports/x.md"},
                        ts="2026-07-01T00:00:01")

    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    assert resp.status == 200

    # a bare `resp.read(n)` blocks until `n` bytes arrive or the socket closes (BufferedReader
    # semantics) -- this SSE connection never closes, so read with `read1` instead, which
    # returns as soon as any data is available.
    collected = ""
    deadline = time.time() + 3
    while "report_done" not in collected and time.time() < deadline:
        piece = resp.fp.read1(4096)
        if not piece:
            break
        collected += piece.decode("utf-8", "replace")
    conn.close()
    assert "event: batch_start" in collected
    assert "event: report_done" in collected


# --- review / claim endpoints ------------------------------------------------------------------


def test_review_ok_marks_report_reviewed(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    report, _claim, _topic = _seed_state(vault)
    status, data = _post(port, "/api/review", {"report_id": report["id"], "verdict": "ok"})
    assert status == 200 and data["ok"] is True
    state = store.AnalystState.load(vault)
    assert state.report_by_id(report["id"])["review"] == "ok"


def test_review_wrong_rolls_back_the_report(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    report, _claim, _topic = _seed_state(vault)
    status, data = _post(port, "/api/review", {"report_id": report["id"], "verdict": "wrong"})
    assert status == 200 and data["ok"] is True
    state = store.AnalystState.load(vault)
    assert state.report_by_id(report["id"])["status"] == "rolled_back"
    assert state.claims == []
    assert state.topics == {}


def test_review_wrong_also_rerenders_the_entity_page(analyst_server):
    """The rolled-back report's claim mentioned Apple -- its entity page must drop that
    mention too, not just the report/topic pages."""
    _srv, port, vault, _inbox = analyst_server
    report, claim, _topic = _seed_state(vault)
    from aioffice.analyst import render
    render.write_entity_pages(vault, store.AnalystState.load(vault), {"Apple"})
    entity_path = vault / "entities" / f"{store.safe_entity_name('Apple')}.md"
    assert claim["text"] in entity_path.read_text(encoding="utf-8")

    status, data = _post(port, "/api/review", {"report_id": report["id"], "verdict": "wrong"})
    assert status == 200 and data["ok"] is True

    assert entity_path.exists()
    assert "(없음)" in entity_path.read_text(encoding="utf-8")


def test_review_rejects_unknown_verdict(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    report, _claim, _topic = _seed_state(vault)
    status, data = _post(port, "/api/review", {"report_id": report["id"], "verdict": "maybe"})
    assert status == 400


def test_claim_correction_marks_report_fixed_and_stores_feedback(analyst_server):
    _srv, port, vault, _inbox = analyst_server
    report, claim, _topic = _seed_state(vault)
    status, data = _post(port, "/api/claim", {"claim_id": claim["id"], "text": "교정된 주장"})
    assert status == 200 and data["ok"] is True
    state = store.AnalystState.load(vault)
    assert state.claim_by_id(claim["id"])["text"] == "교정된 주장"
    assert state.report_by_id(report["id"])["review"] == "fixed"
    feedback = store.read_feedback(vault)
    assert len(feedback) == 1
    assert feedback[0]["corrected_text"] == "교정된 주장"


def test_claim_correction_also_rerenders_the_entity_page(analyst_server):
    """The entity page quotes each mention's claim text -- it must show the corrected text,
    not the stale original."""
    _srv, port, vault, _inbox = analyst_server
    _report, claim, _topic = _seed_state(vault)
    from aioffice.analyst import render
    render.write_entity_pages(vault, store.AnalystState.load(vault), {"Apple"})
    entity_path = vault / "entities" / f"{store.safe_entity_name('Apple')}.md"
    assert "주장" in entity_path.read_text(encoding="utf-8")

    status, data = _post(port, "/api/claim", {"claim_id": claim["id"], "text": "교정된 주장"})
    assert status == 200 and data["ok"] is True

    updated = entity_path.read_text(encoding="utf-8")
    assert "교정된 주장" in updated


# --- learn 409 while running ------------------------------------------------------------------


def test_learn_returns_202_then_409_while_running(analyst_server, monkeypatch):
    _srv, port, vault, inbox = analyst_server
    release = threading.Event()

    def fake_learn_batch(vault_, inbox_, llm, model, max_tokens, batch, git_ok, **kwargs):
        release.wait(5)
        return {"reports": 0, "results": []}

    monkeypatch.setattr(viewer.learn, "learn_batch", fake_learn_batch)

    status, data = _post(port, "/api/learn", {"batch": 10})
    assert status == 202 and data["ok"] is True

    for _ in range(50):
        s, status_data = _get(port, "/api/status")
        if status_data["running"]:
            break
        time.sleep(0.05)
    assert status_data["running"] is True

    status2, data2 = _post(port, "/api/learn", {"batch": 10})
    assert status2 == 409
    assert "학습 중" in data2["error"]

    release.set()
    for _ in range(50):
        _s, status_data = _get(port, "/api/status")
        if not status_data["running"]:
            break
        time.sleep(0.05)
    assert status_data["running"] is False


def test_learn_stop_sets_the_cooperative_cancel_flag(analyst_server, monkeypatch):
    _srv, port, vault, inbox = analyst_server
    started = threading.Event()
    stopped_seen = threading.Event()

    def fake_learn_batch(vault_, inbox_, llm, model, max_tokens, batch, git_ok, *, on_event=None,
                          should_stop=None):
        started.set()
        for _ in range(100):
            if should_stop and should_stop():
                stopped_seen.set()
                break
            time.sleep(0.02)
        return {"reports": 0, "results": []}

    monkeypatch.setattr(viewer.learn, "learn_batch", fake_learn_batch)
    _post(port, "/api/learn", {"batch": 10})
    assert started.wait(3)
    status, data = _post(port, "/api/learn/stop", {})
    assert status == 200
    assert stopped_seen.wait(3)
