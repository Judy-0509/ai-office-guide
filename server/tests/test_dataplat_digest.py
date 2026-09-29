"""Tests for aioffice.dataplat.digest: batch-generated summaries after a snapshot (version_diff
for versioned datasets, the load's own diff report for unversioned ones), each number-checked
against the code-computed table, idempotent per load_id, one dataset's failure never stops
others."""
from __future__ import annotations

import json

from aioffice.config import Settings
from aioffice.dataplat import digest, store
from aioffice.llm.client import LLMClient


class FakeContentBackend:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.model = "fake-model"
        self.name = "direct"
        self.calls: list[dict] = []

    def model_for(self):
        return self.model

    def run_content(self, messages, model, timeout=None, max_tokens=None):
        self.calls.append({"messages": messages, "model": model})
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return content, {"prompt_tokens": 10, "completion_tokens": 20}


def _obs(metric, entity, period, value):
    return {"metric": metric, "entity": entity, "region": "", "period": period,
            "period_sort": period, "source": "", "value": value, "unit": ""}


def _seed_unversioned(tmp_path):
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    conn = store.connect(tmp_path / "dataplat.sqlite")
    store.upsert_dataset(conn, "shipments", "출하", store.now_iso())
    rows1 = [_obs("출하량", "모델A", "2024Q1", 100.0), _obs("출하량", "모델B", "2024Q1", 50.0)]
    store.write_load(conn, dataset="shipments", content_hash_value=store.content_hash(rows1),
                      observations=rows1, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    rows2 = [_obs("출하량", "모델A", "2024Q1", 150.0), _obs("출하량", "모델B", "2024Q1", 50.0)]
    from aioffice.dataplat.store import diff_report, value_map
    prev_map, _ = value_map(rows1)
    cur_map, _ = value_map(rows2)
    diff = diff_report(prev_map, cur_map)
    store.write_load(conn, dataset="shipments", content_hash_value=store.content_hash(rows2),
                      observations=rows2, report={"diff": diff}, started_at=store.now_iso(),
                      finished_at=store.now_iso())
    return settings, conn


def _seed_versioned(tmp_path):
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    conn = store.connect(tmp_path / "dataplat.sqlite")
    store.upsert_dataset(conn, "forecast", "출하 전망", store.now_iso())
    rows07 = [_obs("출하량전망", "모델A", "2026Q3", 100.0), _obs("출하량전망", "모델B", "2026Q3", 200.0)]
    rows08 = [_obs("출하량전망", "모델A", "2026Q3", 100.0), _obs("출하량전망", "모델B", "2026Q3", 250.0)]
    vs07 = store.compute_version_sort(conn, "forecast", "2026-07", store.now_iso())
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows07),
                      observations=rows07, report={}, started_at=store.now_iso(),
                      finished_at=store.now_iso(), version_label="2026-07", version_sort=vs07)
    vs08 = store.compute_version_sort(conn, "forecast", "2026-08", store.now_iso())
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows08),
                      observations=rows08, report={}, started_at=store.now_iso(),
                      finished_at=store.now_iso(), version_label="2026-08", version_sort=vs08)
    return settings, conn


def test_unversioned_dataset_generates_load_summary(tmp_path):
    # only 모델A actually changed (100 -> 150; 모델B stayed 50) -- one load_summary + one
    # entity_note (모델A only).
    settings, conn = _seed_unversioned(tmp_path)
    backend = FakeContentBackend([
        {"sentence": "모델A는 100에서 150으로 늘었습니다."},  # load_summary
        {"sentence": "모델A: 100 -> 150"},  # entity_note
    ])
    llm = LLMClient(settings, conn, backend=backend)
    rows = digest.run_digest_for_dataset(conn, llm, "shipments")
    assert [r["kind"] for r in rows] == ["load_summary", "entity_note"]
    assert rows[0]["text"] == "모델A는 100에서 150으로 늘었습니다."


def test_versioned_dataset_generates_version_summary_plus_entity_notes(tmp_path):
    settings, conn = _seed_versioned(tmp_path)
    backend = FakeContentBackend([
        {"sentence": "모델B가 200에서 250으로 가장 많이 늘었습니다."},  # version_summary
        {"sentence": "모델B: 200 -> 250 (+50)"},  # entity_note for 모델B (top changed)
        {"sentence": "모델A: 변화 없음"},  # entity_note for 모델A
    ])
    llm = LLMClient(settings, conn, backend=backend)
    rows = digest.run_digest_for_dataset(conn, llm, "forecast")
    kinds = [r["kind"] for r in rows]
    assert kinds == ["version_summary", "entity_note", "entity_note"]
    assert rows[0]["title"] == "이번 버전 주요 변화 요약"


def test_number_check_falls_back_to_template_on_mismatch(tmp_path):
    settings, conn = _seed_unversioned(tmp_path)
    backend = FakeContentBackend([
        {"sentence": "모델A는 9999로 급증했습니다."},  # invented number -- load_summary
        {"sentence": "모델A: 9999"},  # invented number -- entity_note too
    ])
    llm = LLMClient(settings, conn, backend=backend)
    rows = digest.run_digest_for_dataset(conn, llm, "shipments")
    assert "9999" not in rows[0]["text"]
    assert "모델A" in rows[0]["text"]  # template cites the real top-changed entity


def test_idempotent_per_load_id_second_call_generates_nothing(tmp_path):
    settings, conn = _seed_unversioned(tmp_path)
    backend = FakeContentBackend([
        {"sentence": "모델A는 100에서 150으로 늘었습니다."},
        {"sentence": "모델A: 100 -> 150"},
    ])
    llm = LLMClient(settings, conn, backend=backend)
    first = digest.run_digest_for_dataset(conn, llm, "shipments")
    assert len(first) == 2
    second = digest.run_digest_for_dataset(conn, llm, "shipments")
    assert second == []  # same load_id -- already digested
    assert len(backend.calls) == 2  # no new LLM call either


def test_no_previous_version_to_diff_against_generates_nothing(tmp_path):
    settings = Settings.load(None, environ={"DATA_DIR": str(tmp_path / "data")})
    conn = store.connect(tmp_path / "dataplat.sqlite")
    store.upsert_dataset(conn, "forecast", "출하 전망", store.now_iso())
    rows = [_obs("출하량전망", "모델A", "2026Q3", 100.0)]
    vs = store.compute_version_sort(conn, "forecast", "2026-07", store.now_iso())
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(),
                      finished_at=store.now_iso(), version_label="2026-07", version_sort=vs)
    llm = LLMClient(settings, conn, backend=FakeContentBackend([]))
    assert digest.run_digest_for_dataset(conn, llm, "forecast") == []


def test_latest_digests_returns_rows_for_most_recent_digested_load(tmp_path):
    settings, conn = _seed_unversioned(tmp_path)
    llm = LLMClient(settings, conn, backend=FakeContentBackend([
        {"sentence": "모델A는 100에서 150으로 늘었습니다."},
        {"sentence": "모델A: 100 -> 150"},
    ]))
    digest.run_digest_for_dataset(conn, llm, "shipments")
    latest = store.latest_digests(conn, "shipments")
    assert len(latest) == 2
    assert latest[0]["table"]["columns"] == ["entity", "old", "new"]


def test_run_one_dataset_failure_never_stops_others(tmp_path):
    settings, conn = _seed_unversioned(tmp_path)
    store.upsert_dataset(conn, "prices", "가격", store.now_iso())
    rows = [_obs("가격", "제품A", "2024Q1", 10.0)]
    store.write_load(conn, dataset="prices", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    conn.close()

    from aioffice.dataplat.source import load_source
    source_yaml = tmp_path / "source.yaml"
    source_yaml.write_text("db: dataplat.sqlite\nquery: SELECT 1\n", encoding="utf-8")
    source = load_source(source_yaml)

    class _BoomBackend(FakeContentBackend):
        def run_content(self, messages, model, timeout=None, max_tokens=None):
            raise RuntimeError("boom")  # an unexpected failure, not just a normal BackendError

    llm = LLMClient(settings, store.connect(tmp_path / "dataplat.sqlite"), backend=_BoomBackend([]))
    results = digest.run(source, tmp_path / "dataplat.sqlite", llm)
    assert set(results) == {"shipments", "prices"}
    assert results["shipments"] == [] and results["prices"] == []  # both failed, neither raised
