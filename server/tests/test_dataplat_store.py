"""Tests for aioffice.dataplat.store: schema init, versioned writes, diff, and the read queries
the HTTP API/chatbot use."""
from __future__ import annotations

import pytest

from aioffice.dataplat import store


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(tmp_path / "dataplat.sqlite")
    yield c
    c.close()


def _obs(metric, entity, period, value, *, region="", source="", unit=""):
    from aioffice.dataplat.normalize import normalize_period

    p = normalize_period(period)
    return {"metric": metric, "entity": entity, "region": region, "period": p.period,
            "period_sort": p.period_sort, "source": source, "value": value, "unit": unit}


def test_write_load_then_query_latest(conn):
    store.upsert_dataset(conn, "shipments", "출하", store.now_iso())
    rows = [_obs("출하량", "A", "2024Q1", 100.0), _obs("출하량", "B", "2024Q1", 50.0)]
    h = store.content_hash(rows)
    load_id = store.write_load(conn, dataset="shipments", content_hash_value=h, observations=rows,
                                report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    assert load_id == 1
    result = store.query_observations(conn, dataset="shipments", version="latest")
    assert len(result) == 2
    assert {r["entity"] for r in result} == {"A", "B"}


def test_duplicate_content_is_detected_by_hash(conn):
    rows = [_obs("출하량", "A", "2024Q1", 100.0)]
    h1 = store.content_hash(rows)
    store.write_load(conn, dataset="shipments", content_hash_value=h1, observations=rows,
                      report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    h2 = store.content_hash(list(rows))
    assert h1 == h2
    assert store.latest_content_hash(conn, "shipments") == h1


def test_skip_writes_no_observations_and_keeps_latest_pointing_at_previous_ok_load(conn):
    rows = [_obs("출하량", "A", "2024Q1", 100.0)]
    h = store.content_hash(rows)
    load1 = store.write_load(conn, dataset="shipments", content_hash_value=h, observations=rows,
                              report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    load2 = store.write_skip(conn, dataset="shipments", content_hash_value=h,
                              started_at=store.now_iso(), finished_at=store.now_iso())
    assert load2 > load1
    assert store.latest_ok_load_id(conn, "shipments") == load1
    loads = store.list_loads(conn, "shipments")
    assert loads[0]["status"] == "skipped_duplicate" and loads[0]["rows"] == 0


def test_error_load_recorded_and_excluded_from_latest(conn):
    store.write_error(conn, dataset="shipments", error="쿼리 실패",
                       started_at=store.now_iso(), finished_at=store.now_iso())
    assert store.latest_ok_load_id(conn, "shipments") is None
    loads = store.list_loads(conn, "shipments")
    assert loads[0]["status"] == "error" and loads[0]["error"] == "쿼리 실패"


def test_versioning_and_diff_report(conn):
    rows1 = [_obs("출하량", "A", "2024Q1", 100.0), _obs("출하량", "B", "2024Q1", 50.0)]
    h1 = store.content_hash(rows1)
    load1 = store.write_load(conn, dataset="shipments", content_hash_value=h1, observations=rows1,
                              report={}, started_at=store.now_iso(), finished_at=store.now_iso())

    rows2 = [_obs("출하량", "A", "2024Q1", 120.0)]  # A changed, B removed
    prev_map, _ = store.value_map(rows1)
    cur_map, dup = store.value_map(rows2)
    diff = store.diff_report(prev_map, cur_map)
    assert diff["changed_count"] == 1 and diff["removed_count"] == 1 and diff["added_count"] == 0
    assert diff["top_changes"][0]["old"] == 100.0 and diff["top_changes"][0]["new"] == 120.0
    assert diff["removed_keys"][0]["entity"] == "B"

    h2 = store.content_hash(rows2)
    load2 = store.write_load(conn, dataset="shipments", content_hash_value=h2, observations=rows2,
                              report={"diff": diff}, started_at=store.now_iso(), finished_at=store.now_iso())
    assert load2 > load1

    # version=<load_id> reaches an old version, not just latest
    old = store.query_observations(conn, dataset="shipments", version=str(load1))
    assert len(old) == 2
    latest = store.query_observations(conn, dataset="shipments", version="latest")
    assert len(latest) == 1 and latest[0]["value"] == 120.0

    detail = store.get_load(conn, load2)
    assert detail["report"]["diff"]["changed_count"] == 1


def test_value_map_counts_within_snapshot_duplicates():
    rows = [_obs("m", "A", "2024Q1", 1.0), _obs("m", "A", "2024Q1", 2.0)]
    m, dup = store.value_map(rows)
    assert dup == 1 and m[("m", "A", "", "2024Q1", "")] == 2.0  # last write wins


def test_diff_report_zero_baseline_uses_magnitude_proxy():
    diff = store.diff_report({("m", "A", "", "2024Q1", ""): 0.0},
                              {("m", "A", "", "2024Q1", ""): 50.0})
    assert diff["changed_count"] == 1
    assert diff["top_changes"][0]["new"] == 50.0


def test_catalog_reflects_latest_snapshot_only(conn):
    store.upsert_dataset(conn, "shipments", "출하", store.now_iso())
    rows1 = [_obs("출하량", "A", "2024Q1", 100.0, region="한국", unit="대")]
    store.write_load(conn, dataset="shipments", content_hash_value=store.content_hash(rows1),
                      observations=rows1, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    rows2 = [_obs("출하량", "A", "2024Q2", 110.0, region="한국", unit="대")]
    store.write_load(conn, dataset="shipments", content_hash_value=store.content_hash(rows2),
                      observations=rows2, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    cat = store.catalog(conn)
    entry = next(d for d in cat if d["name"] == "shipments")
    assert entry["period_from"] == "2024Q2" and entry["period_to"] == "2024Q2"  # only latest


def test_history_across_loads(conn):
    for value in (100.0, 110.0, 90.0):
        rows = [_obs("출하량", "A", "2024Q1", value)]
        store.write_load(conn, dataset="shipments", content_hash_value=store.content_hash(rows),
                          observations=rows, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    history = store.history(conn, dataset="shipments", metric="출하량", entity="A", period="2024Q1")
    assert [h["value"] for h in history] == [100.0, 110.0, 90.0]


def test_to_wide_pivots_and_sorts_periods_chronologically():
    rows = [
        {"entity": "A", "period": "2024Q2", "period_sort": "20240400", "value": 2.0},
        {"entity": "A", "period": "2024Q1", "period_sort": "20240100", "value": 1.0},
        {"entity": "B", "period": "2024Q1", "period_sort": "20240100", "value": 3.0},
    ]
    wide = store.to_wide(rows, ["entity"], ["period"])
    assert wide["columns"] == ["entity", "2024Q1", "2024Q2"]
    assert wide["rows"] == [["A", 1.0, 2.0], ["B", 3.0, None]]


def test_query_observations_unknown_version_raises(conn):
    with pytest.raises(ValueError):
        store.query_observations(conn, dataset="shipments", version="not-a-number")
