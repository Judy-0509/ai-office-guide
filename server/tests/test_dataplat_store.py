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


# --- rollup/agg -----------------------------------------------------------------------------


def test_query_observations_rollup_year_sums_quarters(conn):
    store.upsert_dataset(conn, "forecast", "예측", store.now_iso())
    rows = [_obs("출하량", "A", "2024Q1", 10.0), _obs("출하량", "A", "2024Q2", 20.0),
            _obs("출하량", "A", "2024Q3", 30.0), _obs("출하량", "A", "2024Q4", 40.0)]
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    result = store.query_observations(conn, dataset="forecast", rollup="year", agg="sum")
    assert len(result) == 1
    assert result[0]["period"] == "2024" and result[0]["value"] == 100.0


def test_query_observations_rollup_quarter_avg(conn):
    store.upsert_dataset(conn, "forecast", "예측", store.now_iso())
    rows = [_obs("출하량", "A", "2024-01", 10.0), _obs("출하량", "A", "2024-02", 20.0),
            _obs("출하량", "A", "2024-03", 30.0)]
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    result = store.query_observations(conn, dataset="forecast", rollup="quarter", agg="avg")
    assert len(result) == 1
    assert result[0]["period"] == "2024Q1" and result[0]["value"] == pytest.approx(20.0)


def test_query_observations_rollup_keeps_separate_entities_and_metrics(conn):
    store.upsert_dataset(conn, "forecast", "예측", store.now_iso())
    rows = [_obs("출하량", "A", "2024Q1", 10.0), _obs("출하량", "B", "2024Q1", 5.0),
            _obs("가격", "A", "2024Q1", 1.0)]
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    result = store.query_observations(conn, dataset="forecast", rollup="year", agg="sum")
    assert len(result) == 3  # (출하량,A) (출하량,B) (가격,A) each stay separate groups


def test_query_observations_without_rollup_unchanged(conn):
    # no rollup= -> exactly today's behavior (own periods, untouched)
    store.upsert_dataset(conn, "forecast", "예측", store.now_iso())
    rows = [_obs("출하량", "A", "2024Q1", 10.0), _obs("출하량", "A", "2024Q2", 20.0)]
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    result = store.query_observations(conn, dataset="forecast")
    assert [r["period"] for r in result] == ["2024Q1", "2024Q2"]


def test_query_observations_rollup_bad_agg_raises(conn):
    store.upsert_dataset(conn, "forecast", "예측", store.now_iso())
    rows = [_obs("출하량", "A", "2024Q1", 10.0)]
    store.write_load(conn, dataset="forecast", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    with pytest.raises(ValueError):
        store.query_observations(conn, dataset="forecast", rollup="year", agg="median")


# --- catalog_version / chat_spec_cache -------------------------------------------------------


def test_catalog_version_changes_when_latest_load_changes(conn):
    store.upsert_dataset(conn, "shipments", "출하", store.now_iso())
    v0 = store.catalog_version(conn)
    rows = [_obs("출하량", "A", "2024Q1", 10.0)]
    store.write_load(conn, dataset="shipments", content_hash_value=store.content_hash(rows),
                      observations=rows, report={}, started_at=store.now_iso(), finished_at=store.now_iso())
    v1 = store.catalog_version(conn)
    assert v0 != v1
    v2 = store.catalog_version(conn)
    assert v1 == v2  # stable when nothing changed


def test_chat_spec_cache_roundtrip_and_scoped_by_catalog_version(conn):
    assert store.get_cached_spec(conn, "제품별 가격지수", "v1") is None
    spec = {"dataset": "prices", "metrics": ["가격지수"]}
    store.set_cached_spec(conn, "제품별 가격지수", "v1", spec)
    assert store.get_cached_spec(conn, "제품별 가격지수", "v1") == spec
    assert store.get_cached_spec(conn, "제품별 가격지수", "v2") is None  # different catalog_version misses
    store.set_cached_spec(conn, "제품별 가격지수", "v1", {"dataset": "prices", "metrics": ["다른"]})
    assert store.get_cached_spec(conn, "제품별 가격지수", "v1")["metrics"] == ["다른"]  # overwrite


# --- version labels (source.yaml's version_column) ---------------------------------------------


def _write_version(conn, dataset, label, rows, *, report=None):
    vs = store.compute_version_sort(conn, dataset, label, store.now_iso())
    return store.write_load(conn, dataset=dataset, content_hash_value=store.content_hash(rows),
                             observations=rows, report=report or {}, started_at=store.now_iso(),
                             finished_at=store.now_iso(), version_label=label, version_sort=vs)


def test_compute_version_sort_period_shaped_label_is_chronological(conn):
    s1 = store.compute_version_sort(conn, "forecast", "2026-07", store.now_iso())
    s2 = store.compute_version_sort(conn, "forecast", "2026-08", store.now_iso())
    assert s1 < s2
    assert s1.startswith("P") and s2.startswith("P")


def test_compute_version_sort_non_period_label_falls_back_to_first_seen(conn):
    s1 = store.compute_version_sort(conn, "forecast", "draft", store.now_iso())
    assert s1.startswith("F")
    # once a load with this label exists, later calls reuse the SAME sort key (not a fresh one)
    _write_version(conn, "forecast", "draft", [_obs("m", "A", "2024Q1", 1.0)])
    s2 = store.compute_version_sort(conn, "forecast", "draft", store.now_iso())
    assert s2 == s1


def test_latest_content_hash_for_version_is_per_label(conn):
    rows_a = [_obs("출하량", "A", "2024Q1", 100.0)]
    _write_version(conn, "forecast", "2026-07", rows_a)
    assert store.latest_content_hash_for_version(conn, "forecast", "2026-07") == store.content_hash(rows_a)
    assert store.latest_content_hash_for_version(conn, "forecast", "2026-08") is None


def test_previous_load_id_for_version_same_label_beats_lower_label(conn):
    l07 = _write_version(conn, "forecast", "2026-07", [_obs("m", "A", "2024Q1", 1.0)])
    l08a = _write_version(conn, "forecast", "2026-08", [_obs("m", "A", "2024Q1", 2.0)])
    vs08 = store.compute_version_sort(conn, "forecast", "2026-08", store.now_iso())
    # a new 2026-08 load (re-issue) -> previous is the earlier 2026-08 load, not 2026-07
    assert store.previous_load_id_for_version(conn, "forecast", vs08) == l08a
    vs07 = store.compute_version_sort(conn, "forecast", "2026-07", store.now_iso())
    assert store.previous_load_id_for_version(conn, "forecast", vs07) == l07  # only same-label exists


def test_previous_load_id_for_version_new_higher_label_uses_next_lower(conn):
    l07 = _write_version(conn, "forecast", "2026-07", [_obs("m", "A", "2024Q1", 1.0)])
    vs08 = store.compute_version_sort(conn, "forecast", "2026-08", store.now_iso())
    assert store.previous_load_id_for_version(conn, "forecast", vs08) == l07


def test_previous_load_id_for_version_first_ever_is_none(conn):
    vs = store.compute_version_sort(conn, "forecast", "2026-07", store.now_iso())
    assert store.previous_load_id_for_version(conn, "forecast", vs) is None


def test_list_version_labels_ordered_and_empty_by_default(conn):
    assert store.list_version_labels(conn, "forecast") == []
    _write_version(conn, "forecast", "2026-08", [_obs("m", "A", "2024Q1", 1.0)])
    _write_version(conn, "forecast", "2026-07", [_obs("m", "A", "2024Q1", 1.0)])
    assert store.list_version_labels(conn, "forecast") == ["2026-07", "2026-08"]


def test_query_observations_by_version_label(conn):
    _write_version(conn, "forecast", "2026-07", [_obs("m", "A", "2024Q1", 100.0)])
    _write_version(conn, "forecast", "2026-08", [_obs("m", "A", "2024Q1", 120.0)])
    latest = store.query_observations(conn, dataset="forecast", version="latest")
    assert latest[0]["value"] == 120.0
    old = store.query_observations(conn, dataset="forecast", version="2026-07")
    assert old[0]["value"] == 100.0


def test_query_observations_unknown_version_label_raises(conn):
    _write_version(conn, "forecast", "2026-07", [_obs("m", "A", "2024Q1", 100.0)])
    with pytest.raises(ValueError):
        store.query_observations(conn, dataset="forecast", version="2099-01")


def test_catalog_lists_version_labels(conn):
    store.upsert_dataset(conn, "forecast", "예측", store.now_iso())
    _write_version(conn, "forecast", "2026-07", [_obs("m", "A", "2024Q1", 1.0)])
    _write_version(conn, "forecast", "2026-08", [_obs("m", "A", "2024Q1", 2.0)])
    cat = next(d for d in store.catalog(conn) if d["name"] == "forecast")
    assert cat["version_labels"] == ["2026-07", "2026-08"]


def test_catalog_version_labels_empty_without_version_column(conn):
    store.upsert_dataset(conn, "shipments", "출하", store.now_iso())
    store.write_load(conn, dataset="shipments", content_hash_value="h",
                      observations=[_obs("m", "A", "2024Q1", 1.0)], report={},
                      started_at=store.now_iso(), finished_at=store.now_iso())
    cat = next(d for d in store.catalog(conn) if d["name"] == "shipments")
    assert cat["version_labels"] == []


def test_history_one_point_per_version_label_when_versioned(conn):
    _write_version(conn, "forecast", "2026-07", [_obs("출하량", "A", "2024Q1", 100.0)])
    # a re-issue (correction) of 2026-07 -- history should show its LATEST value, not both
    _write_version(conn, "forecast", "2026-07", [_obs("출하량", "A", "2024Q1", 105.0)])
    _write_version(conn, "forecast", "2026-08", [_obs("출하량", "A", "2024Q1", 120.0)])
    hist = store.history(conn, dataset="forecast", metric="출하량", entity="A", period="2024Q1")
    assert [(h["version_label"], h["value"]) for h in hist] == [("2026-07", 105.0), ("2026-08", 120.0)]


def test_history_without_version_column_still_shows_every_load(conn):
    # unversioned dataset: exactly today's behavior -- every snapshot is its own point
    for value in (100.0, 110.0, 90.0):
        rows = [_obs("출하량", "A", "2024Q1", value)]
        store.write_load(conn, dataset="shipments", content_hash_value=store.content_hash(rows),
                          observations=rows, report={}, started_at=store.now_iso(),
                          finished_at=store.now_iso())
    hist = store.history(conn, dataset="shipments", metric="출하량", entity="A", period="2024Q1")
    assert [h["value"] for h in hist] == [100.0, 110.0, 90.0]


def test_latest_loads_view_picks_highest_version_label(conn):
    l07 = _write_version(conn, "forecast", "2026-07", [_obs("m", "A", "2024Q1", 1.0)])
    l08 = _write_version(conn, "forecast", "2026-08", [_obs("m", "A", "2024Q1", 2.0)])
    assert store.latest_ok_load_id(conn, "forecast") == l08
    # a re-issue of the OLDER label must not become "latest" even though its id is newest
    _write_version(conn, "forecast", "2026-07", [_obs("m", "A", "2024Q1", 3.0)])
    assert store.latest_ok_load_id(conn, "forecast") == l08


# --- version_diff -------------------------------------------------------------------------------


def _seed_forecast(conn):
    """2026-07: A=100 B=200 C=150. 2026-08: A=100(unchanged) B=250(+25%) C=120(-20%)."""
    rows07 = [_obs("출하량", "A", "2024Q1", 100.0), _obs("출하량", "B", "2024Q1", 200.0),
              _obs("출하량", "C", "2024Q1", 150.0)]
    rows08 = [_obs("출하량", "A", "2024Q1", 100.0), _obs("출하량", "B", "2024Q1", 250.0),
              _obs("출하량", "C", "2024Q1", 120.0)]
    _write_version(conn, "forecast", "2026-07", rows07)
    _write_version(conn, "forecast", "2026-08", rows08)


def test_version_diff_defaults_to_latest_vs_previous_label(conn):
    _seed_forecast(conn)
    result = store.version_diff(conn, "forecast")
    assert result["from_label"] == "2026-07" and result["to_label"] == "2026-08"
    assert result["totals"] == {"changed": 2, "added": 0, "removed": 0, "total_keys": 3}


def test_version_diff_sort_abs_ranks_by_absolute_diff(conn):
    _seed_forecast(conn)
    result = store.version_diff(conn, "forecast", sort="abs")
    assert [r["entity"] for r in result["rows"]] == ["B", "C", "A"]  # |50| > |-30| > |0|


def test_version_diff_sort_rel_ranks_by_relative_change(conn):
    _seed_forecast(conn)
    result = store.version_diff(conn, "forecast", sort="rel")
    assert [r["entity"] for r in result["rows"]] == ["B", "C", "A"]  # 25% > 20% > 0%
    assert result["rows"][0]["pct"] == pytest.approx(25.0)
    assert result["rows"][1]["pct"] == pytest.approx(-20.0)


def test_version_diff_top_caps_rows_but_not_totals(conn):
    _seed_forecast(conn)
    result = store.version_diff(conn, "forecast", top=1)
    assert len(result["rows"]) == 1
    assert result["totals"]["changed"] == 2  # totals count everything, not just the top slice


def test_version_diff_zero_baseline_and_added_key_have_null_pct(conn):
    rows07 = [_obs("m", "A", "2024Q1", 0.0)]
    rows08 = [_obs("m", "A", "2024Q1", 50.0), _obs("m", "B", "2024Q1", 10.0)]
    _write_version(conn, "forecast", "2026-07", rows07)
    _write_version(conn, "forecast", "2026-08", rows08)
    result = store.version_diff(conn, "forecast")
    by_entity = {r["entity"]: r for r in result["rows"]}
    assert by_entity["A"]["old"] == 0.0 and by_entity["A"]["pct"] is None
    assert by_entity["B"]["old"] is None and by_entity["B"]["pct"] is None
    assert result["totals"]["added"] == 1


def test_version_diff_removed_key(conn):
    rows07 = [_obs("m", "A", "2024Q1", 1.0), _obs("m", "B", "2024Q1", 2.0)]
    rows08 = [_obs("m", "A", "2024Q1", 1.0)]  # B dropped in 08
    _write_version(conn, "forecast", "2026-07", rows07)
    _write_version(conn, "forecast", "2026-08", rows08)
    result = store.version_diff(conn, "forecast")
    assert result["totals"]["removed"] == 1
    by_entity = {r["entity"]: r for r in result["rows"]}
    assert by_entity["B"]["new"] is None and by_entity["B"]["old"] == 2.0


def test_version_diff_explicit_from_to_labels(conn):
    _seed_forecast(conn)
    _write_version(conn, "forecast", "2026-09",
                    [_obs("출하량", "A", "2024Q1", 999.0), _obs("출하량", "B", "2024Q1", 999.0),
                     _obs("출하량", "C", "2024Q1", 999.0)])
    result = store.version_diff(conn, "forecast", from_label="2026-07", to_label="2026-08")
    assert result["from_label"] == "2026-07" and result["to_label"] == "2026-08"
    assert {r["entity"] for r in result["rows"]} == {"A", "B", "C"}
    assert all(r["new"] != 999.0 for r in result["rows"])


def test_version_diff_unknown_label_raises(conn):
    _seed_forecast(conn)
    with pytest.raises(ValueError, match="버전"):
        store.version_diff(conn, "forecast", to_label="2099-01")


def test_version_diff_unknown_group_by_dim_raises(conn):
    _seed_forecast(conn)
    with pytest.raises(ValueError, match="group_by"):
        store.version_diff(conn, "forecast", group_by=["bogus"])


def test_version_diff_bad_sort_raises(conn):
    _seed_forecast(conn)
    with pytest.raises(ValueError, match="sort"):
        store.version_diff(conn, "forecast", sort="banana")


def test_version_diff_no_previous_version_raises(conn):
    _write_version(conn, "forecast", "2026-07", [_obs("m", "A", "2024Q1", 1.0)])
    with pytest.raises(ValueError):
        store.version_diff(conn, "forecast")  # only one label ever -- nothing to compare


def test_version_diff_aggregates_across_periods_for_group_by(conn):
    # same entity, two periods -- group_by=["entity"] must sum both periods per version
    rows07 = [_obs("m", "A", "2024Q1", 10.0), _obs("m", "A", "2024Q2", 20.0)]
    rows08 = [_obs("m", "A", "2024Q1", 15.0), _obs("m", "A", "2024Q2", 25.0)]
    _write_version(conn, "forecast", "2026-07", rows07)
    _write_version(conn, "forecast", "2026-08", rows08)
    result = store.version_diff(conn, "forecast", group_by=["entity"])
    assert result["rows"] == [{"entity": "A", "old": 30.0, "new": 40.0, "diff": 10.0,
                                "pct": pytest.approx(10.0 / 30.0 * 100)}]


def test_version_diff_filters_by_metric_entity_region_source(conn):
    rows07 = [{"metric": "m1", "entity": "A", "region": "KR", "period": "2024Q1",
               "period_sort": "20240100", "source": "S1", "value": 1.0, "unit": ""},
              {"metric": "m2", "entity": "A", "region": "US", "period": "2024Q1",
               "period_sort": "20240100", "source": "S2", "value": 2.0, "unit": ""}]
    rows08 = [{"metric": "m1", "entity": "A", "region": "KR", "period": "2024Q1",
               "period_sort": "20240100", "source": "S1", "value": 5.0, "unit": ""},
              {"metric": "m2", "entity": "A", "region": "US", "period": "2024Q1",
               "period_sort": "20240100", "source": "S2", "value": 9.0, "unit": ""}]
    _write_version(conn, "forecast", "2026-07", rows07)
    _write_version(conn, "forecast", "2026-08", rows08)
    result = store.version_diff(conn, "forecast", metric="m1", region="KR", source="S1")
    assert result["rows"] == [{"entity": "A", "old": 1.0, "new": 5.0, "diff": 4.0, "pct": 400.0}]
