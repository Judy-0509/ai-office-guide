"""`python -m aioffice.dataplat.snapshot --source source.yaml --db dataplat.sqlite [--dry-run]`

Reads the team's existing SQLite through the query/view in `source.yaml` (read-only -- dataplat
never writes to their database), normalizes period/value cells, groups the result by the
`dataset` column, and for each dataset: if the content is byte-for-byte the same as the last
snapshot (content_hash match) records a `skipped_duplicate` load, otherwise writes a new
versioned load + a report (counts, and a diff against the previous version) to `dataplat.sqlite`.
One dataset's failure never stops the others; exit code 1 if any dataset errored.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from . import store
from .normalize import clean_text, normalize_period, normalize_value
from .source import REQUIRED_COLUMNS, SourceConfig, load_source


def open_source_readonly(path: Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _renamed_columns(cursor_description, rename: dict[str, str]) -> list[str]:
    return [rename.get(col[0], col[0]) for col in cursor_description]


def read_source_rows(source: SourceConfig) -> list[dict[str, Any]]:
    """Runs `source.query`, applies `rename`, and checks the required standard columns (and
    `version_column`, if configured) are present. Values/periods are NOT normalized here (see
    `normalize_rows`) -- this is just the raw read, kept separate so tests can feed rows
    straight in."""
    conn = open_source_readonly(source.db)
    try:
        cur = conn.execute(source.query)
        columns = _renamed_columns(cur.description, source.rename)
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise ValueError(f"쿼리 결과에 필수 컬럼이 없습니다: {missing} (실제: {columns})")
        if source.version_column and source.version_column not in columns:
            raise ValueError(
                f"version_column({source.version_column!r})이 쿼리 결과에 없습니다 (실제: {columns})")
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()


class NormalizeStats:
    def __init__(self) -> None:
        self.rows = 0
        self.dropped_blank = 0
        self.dropped_non_numeric = 0
        self.unparsed_periods = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "rows": self.rows, "dropped_blank": self.dropped_blank,
            "dropped_non_numeric": self.dropped_non_numeric,
            "unparsed_periods": self.unparsed_periods,
        }


VersionKey = tuple[str, str]  # (dataset, version_label) -- version_label is '' when unused


def normalize_rows(
    raw_rows: list[dict[str, Any]], version_column: str | None = None,
) -> tuple[dict[VersionKey, list[dict]], dict[VersionKey, NormalizeStats]]:
    """Raw source rows -> {(dataset, version_label): [observation, ...]}, {key: NormalizeStats}.
    `version_label` is '' for every row when `version_column` is None -- the whole versioning
    scheme then collapses to one implicit version per dataset, exactly today's behavior. Dropped
    (blank/non-numeric) rows still count against their key's stats even though they never become
    an observation."""
    stats_by_key: dict[VersionKey, NormalizeStats] = {}
    by_key: dict[VersionKey, list[dict]] = {}
    for raw in raw_rows:
        dataset = clean_text(raw.get("dataset"))
        version_label = clean_text(raw.get(version_column)) if version_column else ""
        key = (dataset, version_label)
        stats = stats_by_key.setdefault(key, NormalizeStats())
        stats.rows += 1
        value_res = normalize_value(raw.get("value"))
        if value_res.status == "blank":
            stats.dropped_blank += 1
            continue
        if value_res.status == "non_numeric":
            stats.dropped_non_numeric += 1
            continue
        period_res = normalize_period(raw.get("period"))
        if not period_res.ok:
            stats.unparsed_periods += 1
        unit = clean_text(raw.get("unit"))
        if value_res.unit == "%":
            unit = "%"
        by_key.setdefault(key, []).append({
            "metric": clean_text(raw.get("metric")), "entity": clean_text(raw.get("entity")),
            "region": clean_text(raw.get("region")), "period": period_res.period,
            "period_sort": period_res.period_sort, "source": clean_text(raw.get("source")),
            "value": value_res.value, "unit": unit,
        })
    return by_key, stats_by_key


def snapshot_version(conn: sqlite3.Connection, dataset: str, version_label: str,
                      observations: list[dict], *, version_stats: dict, started_at: str,
                      dry_run: bool = False) -> dict:
    """One (dataset, version_label)'s worth of the run: decides skip vs write, returns a
    Korean-friendly result dict for the console summary. `version_label` is '' when the dataset
    doesn't use `version_column` -- every check below then reduces to "this dataset's own
    previous snapshot", exactly today's behavior (see store.py's module docstring)."""
    new_hash = store.content_hash(observations)
    previous_hash = store.latest_content_hash_for_version(conn, dataset, version_label)
    finished_at = store.now_iso()
    version_sort = (store.compute_version_sort(conn, dataset, version_label, started_at)
                     if version_label else "")

    if previous_hash == new_hash:
        if not dry_run:
            store.write_skip(conn, dataset=dataset, content_hash_value=new_hash,
                              started_at=started_at, finished_at=finished_at,
                              version_label=version_label, version_sort=version_sort)
        result = {"dataset": dataset, "status": "skipped_duplicate", "rows": len(observations)}
        if version_label:
            result["version_label"] = version_label
        return result

    previous_load_id = store.previous_load_id_for_version(conn, dataset, version_sort)
    previous_rows = (store.query_observations(conn, dataset=dataset, version=str(previous_load_id))
                      if previous_load_id is not None else [])
    previous_map, _ = store.value_map(previous_rows)
    current_map, duplicate_keys = store.value_map(observations)
    diff = store.diff_report(previous_map, current_map) if previous_load_id is not None else None

    report = {
        **version_stats,
        "distinct_metrics": len({o["metric"] for o in observations}),
        "distinct_entities": len({o["entity"] for o in observations}),
        "distinct_periods": len({o["period"] for o in observations}),
        "distinct_sources": len({o["source"] for o in observations}),
        "duplicate_keys": duplicate_keys,
        "previous_load_id": previous_load_id,
        "diff": diff,
    }
    if version_label:
        report["version_label"] = version_label
    if not dry_run:
        store.write_load(conn, dataset=dataset, content_hash_value=new_hash,
                          observations=observations, report=report,
                          started_at=started_at, finished_at=finished_at,
                          version_label=version_label, version_sort=version_sort)
    result = {"dataset": dataset, "status": "ok", "rows": len(observations), "report": report}
    if version_label:
        result["version_label"] = version_label
    return result


def run(source: SourceConfig, db_path: Path, *, dataset: str | None = None,
        dry_run: bool = False) -> list[dict]:
    """Runs one full snapshot. Returns one result dict per (dataset, version_label) -- just one
    per dataset when `source.version_column` is unset (status ok/error/skipped_duplicate). A
    key that raises is reported as status="error" and does not stop the others."""
    started_at = store.now_iso()
    try:
        raw_rows = read_source_rows(source)
    except Exception as exc:  # noqa: BLE001 -- the whole run failed before any dataset was seen
        return [{"dataset": dataset or "(전체)", "status": "error", "rows": 0, "error": str(exc)}]

    by_key, stats_by_key = normalize_rows(raw_rows, source.version_column)
    keys = sorted(set(by_key) | set(stats_by_key))
    if dataset is not None:
        keys = [k for k in keys if k[0] == dataset]

    conn = store.connect(db_path)
    try:
        results = []
        upserted: set[str] = set()
        for name, version_label in keys:
            observations = by_key.get((name, version_label), [])
            version_stats = stats_by_key.get((name, version_label), NormalizeStats()).as_dict()
            try:
                if name not in upserted:
                    store.upsert_dataset(conn, name, name, started_at)
                    upserted.add(name)
                result = snapshot_version(conn, name, version_label, observations,
                                           version_stats=version_stats, started_at=started_at,
                                           dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 -- one bad key never stops the run
                finished_at = store.now_iso()
                if not dry_run:
                    store.write_error(conn, dataset=name, error=str(exc),
                                       started_at=started_at, finished_at=finished_at,
                                       version_label=version_label)
                result = {"dataset": name, "status": "error", "rows": 0, "error": str(exc)}
                if version_label:
                    result["version_label"] = version_label
            results.append(result)
        return results
    finally:
        conn.close()


def _append_log(log_path: Path, result: Any) -> None:
    def as_text(x: Any) -> str:
        return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x or "")

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"--- pre-command {store.now_iso()} ---\n")
        f.write(as_text(getattr(result, "stdout", "")))
        f.write(as_text(getattr(result, "stderr", "")))
        f.write("\n")


def run_refresh_once(source: SourceConfig, db_path: Path, *, pre_command: str | None = None,
                      dataset: str | None = None, runner: Callable[..., Any] = subprocess.run,
                      log_path: Path | None = None) -> dict:
    """Shared by `POST /api/refresh` and the scheduled task's wrapper: run `pre_command` (the
    team's existing aggregation step) if configured, and only snapshot if it succeeded."""
    if pre_command:
        result = runner(pre_command, shell=True, capture_output=True)
        if log_path:
            _append_log(log_path, result)
        returncode = getattr(result, "returncode", 0)
        if returncode:
            return {"ok": False, "stage": "pre_command", "returncode": returncode, "results": []}
    results = run(source, db_path, dataset=dataset)
    ok = all(r["status"] != "error" for r in results)
    return {"ok": ok, "stage": "snapshot", "results": results}


def _print_summary(results: list[dict]) -> int:
    exit_code = 0
    for r in results:
        label = f"[{r['version_label']}] " if r.get("version_label") else ""
        if r["status"] == "ok":
            diff = (r.get("report") or {}).get("diff")
            diff_text = ""
            if diff:
                diff_text = (f" (추가 {diff['added_count']}, 삭제 {diff['removed_count']}, "
                              f"변경 {diff['changed_count']})")
            print(f"[OK] {r['dataset']} {label}: {r['rows']}행 적재{diff_text}", flush=True)
        elif r["status"] == "skipped_duplicate":
            print(f"[건너뜀] {r['dataset']} {label}: 이전 스냅샷과 동일", flush=True)
        else:
            exit_code = 1
            print(f"[오류] {r['dataset']} {label}: {r.get('error')}", flush=True)
    return exit_code


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="AI Office 데이터 플랫폼 스냅샷")
    parser.add_argument("--source", required=True, help="source.yaml 경로")
    parser.add_argument("--db", required=True, help="dataplat.sqlite 경로")
    parser.add_argument("--dataset", default=None, help="이 데이터셋만 처리 (기본: 전체)")
    parser.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 리포트만 출력")
    args = parser.parse_args(argv)

    source = load_source(Path(args.source))
    results = run(source, Path(args.db), dataset=args.dataset, dry_run=args.dry_run)
    exit_code = _print_summary(results)
    if args.dry_run:
        print("(dry-run: DB에 쓰지 않았습니다)", flush=True)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
