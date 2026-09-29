"""Tests for aioffice.dataplat.snapshot: reading the fake source (read-only), normalizing,
grouping by dataset, dedup by content hash, per-dataset diff, dry-run, and the CLI."""
from __future__ import annotations

import subprocess
import sys

import pytest

from aioffice.dataplat import snapshot, store
from aioffice.dataplat.samples import fake_source
from aioffice.dataplat.source import load_source


@pytest.fixture()
def source_and_db(tmp_path):
    fake_source.build(tmp_path / "fake_source.sqlite", state="v1")
    source_yaml = tmp_path / "source.yaml"
    source_yaml.write_text(
        "db: fake_source.sqlite\nview: v_dataplat_observations\nrename:\n  broker: source\n",
        encoding="utf-8",
    )
    return load_source(source_yaml), tmp_path / "dataplat.sqlite"


def test_read_source_rows_applies_rename_and_required_columns(source_and_db):
    source, _db = source_and_db
    rows = snapshot.read_source_rows(source)
    assert rows
    assert "source" in rows[0] and "broker" not in rows[0]
    assert {"dataset", "metric", "entity", "period", "value"} <= set(rows[0])


def test_source_db_opened_read_only(source_and_db):
    source, _db = source_and_db
    conn = snapshot.open_source_readonly(source.db)
    try:
        with pytest.raises(Exception):
            conn.execute("INSERT INTO tbl_shipments_long VALUES ('x','y','z','w','v','1')")
    finally:
        conn.close()


def test_first_run_writes_ok_loads_for_each_dataset(source_and_db):
    source, db_path = source_and_db
    results = snapshot.run(source, db_path)
    by_name = {r["dataset"]: r for r in results}
    assert by_name["shipments"]["status"] == "ok"
    assert by_name["price_index"]["status"] == "ok"
    assert by_name["price_index"]["rows"] == 8


def test_second_run_unchanged_is_skipped_duplicate(source_and_db):
    source, db_path = source_and_db
    snapshot.run(source, db_path)
    results = snapshot.run(source, db_path)
    assert all(r["status"] == "skipped_duplicate" for r in results)


def test_changed_source_produces_diff_and_leaves_untouched_dataset_skipped(tmp_path):
    fake_source.build(tmp_path / "fake_source.sqlite", state="v1")
    source_yaml = tmp_path / "source.yaml"
    source_yaml.write_text(
        "db: fake_source.sqlite\nview: v_dataplat_observations\nrename:\n  broker: source\n",
        encoding="utf-8",
    )
    source = load_source(source_yaml)
    db_path = tmp_path / "dataplat.sqlite"
    snapshot.run(source, db_path)

    fake_source.build(tmp_path / "fake_source.sqlite", state="v2")
    results = snapshot.run(source, db_path)
    by_name = {r["dataset"]: r for r in results}
    assert by_name["price_index"]["status"] == "skipped_duplicate"
    assert by_name["shipments"]["status"] == "ok"
    diff = by_name["shipments"]["report"]["diff"]
    assert diff["changed_count"] == 1 and diff["removed_count"] == 1


def test_dry_run_does_not_write(source_and_db):
    source, db_path = source_and_db
    snapshot.run(source, db_path, dry_run=True)
    assert not db_path.exists() or store.list_loads(store.connect(db_path)) == []


def test_dataset_filter_only_processes_that_dataset(source_and_db):
    source, db_path = source_and_db
    results = snapshot.run(source, db_path, dataset="shipments")
    assert [r["dataset"] for r in results] == ["shipments"]
    conn = store.connect(db_path)
    assert store.list_loads(conn, "price_index") == []


def test_one_dataset_error_does_not_stop_others(tmp_path, monkeypatch):
    fake_source.build(tmp_path / "fake_source.sqlite", state="v1")
    source_yaml = tmp_path / "source.yaml"
    source_yaml.write_text(
        "db: fake_source.sqlite\nview: v_dataplat_observations\nrename:\n  broker: source\n",
        encoding="utf-8",
    )
    source = load_source(source_yaml)
    db_path = tmp_path / "dataplat.sqlite"

    real_write_load = store.write_load

    def boom(conn, *, dataset, **kwargs):
        if dataset == "shipments":
            raise RuntimeError("디스크 오류(가짜)")
        return real_write_load(conn, dataset=dataset, **kwargs)

    monkeypatch.setattr(store, "write_load", boom)
    results = snapshot.run(source, db_path)
    by_name = {r["dataset"]: r for r in results}
    assert by_name["shipments"]["status"] == "error"
    assert by_name["price_index"]["status"] == "ok"


def test_missing_required_column_raises(tmp_path):
    fake_source.build(tmp_path / "fake_source.sqlite", state="v1")
    source_yaml = tmp_path / "source.yaml"
    source_yaml.write_text("db: fake_source.sqlite\nquery: SELECT metric FROM tbl_shipments_long\n",
                            encoding="utf-8")
    source = load_source(source_yaml)
    with pytest.raises(ValueError, match="필수 컬럼"):
        snapshot.read_source_rows(source)


def test_run_refresh_once_skips_snapshot_when_pre_command_fails(source_and_db):
    from types import SimpleNamespace

    source, db_path = source_and_db

    def fake_runner(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")

    result = snapshot.run_refresh_once(source, db_path, pre_command="does-not-matter",
                                        runner=fake_runner)
    assert result == {"ok": False, "stage": "pre_command", "returncode": 1, "results": []}
    assert not db_path.exists()


def test_run_refresh_once_runs_snapshot_when_pre_command_succeeds(source_and_db, tmp_path):
    from types import SimpleNamespace

    source, db_path = source_and_db
    log_path = tmp_path / "run.log"

    def fake_runner(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b"ok\n", stderr=b"")

    result = snapshot.run_refresh_once(source, db_path, pre_command="aggregate.exe",
                                        runner=fake_runner, log_path=log_path)
    assert result["ok"] is True and result["stage"] == "snapshot"
    assert log_path.exists() and "ok" in log_path.read_text(encoding="utf-8")


def test_cli_dry_run_exits_zero_and_writes_nothing(source_and_db, tmp_path):
    source_yaml = tmp_path / "source.yaml"
    source_yaml.write_text(
        "db: fake_source.sqlite\nview: v_dataplat_observations\nrename:\n  broker: source\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "dataplat.sqlite"
    result = subprocess.run(
        [sys.executable, "-m", "aioffice.dataplat.snapshot", "--source", str(source_yaml),
         "--db", str(db_path), "--dry-run"],
        capture_output=True, encoding="utf-8", cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # connect() creates the (empty-of-loads) schema like every other read path in this
    # codebase does (see store.connect's docstring) -- dry-run's contract is "no loads
    # recorded", not "the file never exists".
    assert store.list_loads(store.connect(db_path)) == []
