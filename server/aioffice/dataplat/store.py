"""dataplat.sqlite: schema init, snapshot writes, and the read queries the HTTP API and the
chatbot both use (catalog / query / history / loads). Write helpers commit exactly once per
snapshot (see `dataplat.snapshot.run`) inside `aioffice.db.WRITE_LOCK`, matching the rest of the
codebase's SQLite convention.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .. import db
from .normalize import normalize_period

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DIM_FIELDS = ("metric", "entity", "region", "period", "source")
QUERY_ROW_CAP = 5000
QUERY_CELL_CAP = 20000


def connect(path: str | Path) -> sqlite3.Connection:
    """dataplat.sqlite also carries the generic `llm_calls`/`embeddings` tables (same pattern
    as `analyst.store.db_path`'s vault DB) so this one connection can be handed straight to
    `LLMClient` for the chatbot -- no second SQLite file just for LLM call logging."""
    conn = db.connect(path)
    db.init_schema(conn)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --- writing (dataplat.snapshot) ------------------------------------------------------------


def content_hash(rows: list[dict]) -> str:
    """Order-independent fingerprint of one dataset's observation rows, used to detect "nothing
    changed since the last snapshot" without keeping a file hash (there is no file anymore)."""
    canon = sorted(
        (r["metric"], r["entity"], r["region"], r["period"], r["source"], r["value"], r["unit"])
        for r in rows
    )
    blob = json.dumps(canon, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def latest_ok_load_id(conn: sqlite3.Connection, dataset: str) -> int | None:
    row = db.fetchone(
        conn, "SELECT MAX(id) AS id FROM loads WHERE dataset=? AND status='ok'", (dataset,)
    )
    return int(row["id"]) if row and row["id"] is not None else None


def latest_content_hash(conn: sqlite3.Connection, dataset: str) -> str | None:
    load_id = latest_ok_load_id(conn, dataset)
    if load_id is None:
        return None
    row = db.fetchone(conn, "SELECT content_hash FROM loads WHERE id=?", (load_id,))
    return row["content_hash"] if row else None


def value_map(rows: list[dict]) -> tuple[dict[tuple, float], int]:
    """{(metric,entity,region,period,source): value}, plus a count of within-snapshot
    duplicate keys (last value wins, same as a real spreadsheet overwrite would)."""
    out: dict[tuple, float] = {}
    duplicates = 0
    for r in rows:
        key = tuple(r[f] for f in DIM_FIELDS)
        if key in out:
            duplicates += 1
        out[key] = r["value"]
    return out, duplicates


def _relative_change(old: float, new: float) -> float:
    if old == 0:
        # ponytail: no meaningful "%" from a zero baseline -- use the new value's magnitude as
        # the sort proxy so a 0 -> big jump still ranks near the top of "largest changes".
        return abs(new)
    return abs(new - old) / abs(old)


def diff_report(previous: dict[tuple, float], current: dict[tuple, float]) -> dict[str, Any]:
    prev_keys, cur_keys = set(previous), set(current)
    added = sorted(cur_keys - prev_keys)
    removed = sorted(prev_keys - cur_keys)
    changed = [(k, previous[k], current[k]) for k in (cur_keys & prev_keys) if previous[k] != current[k]]
    changed.sort(key=lambda item: _relative_change(item[1], item[2]), reverse=True)

    def key_dict(k: tuple) -> dict:
        return dict(zip(DIM_FIELDS, k))

    return {
        "added_count": len(added), "removed_count": len(removed), "changed_count": len(changed),
        "added_keys": [key_dict(k) for k in added[:50]],
        "removed_keys": [key_dict(k) for k in removed[:50]],
        "top_changes": [{**key_dict(k), "old": old, "new": new} for k, old, new in changed[:10]],
    }


def upsert_dataset(conn: sqlite3.Connection, name: str, title: str | None, updated_at: str) -> None:
    conn.execute(
        "INSERT INTO datasets (name, title, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at",
        (name, title or name, updated_at),
    )


def write_skip(conn: sqlite3.Connection, *, dataset: str, content_hash_value: str,
               started_at: str, finished_at: str) -> int:
    with db.WRITE_LOCK:
        load_id = db.insert(
            conn, "loads", dataset=dataset, content_hash=content_hash_value, rows=0,
            status="skipped_duplicate", error=None, started_at=started_at, finished_at=finished_at,
        )
        conn.commit()
    return load_id


def write_error(conn: sqlite3.Connection, *, dataset: str, error: str,
                 started_at: str, finished_at: str) -> int:
    with db.WRITE_LOCK:
        load_id = db.insert(
            conn, "loads", dataset=dataset, content_hash="", rows=0, status="error",
            error=error[:2000], started_at=started_at, finished_at=finished_at,
        )
        conn.commit()
    return load_id


def write_load(conn: sqlite3.Connection, *, dataset: str, content_hash_value: str,
               observations: list[dict], report: dict, started_at: str, finished_at: str) -> int:
    with db.WRITE_LOCK:
        load_id = db.insert(
            conn, "loads", dataset=dataset, content_hash=content_hash_value,
            rows=len(observations), status="ok", error=None,
            started_at=started_at, finished_at=finished_at,
        )
        if observations:
            conn.executemany(
                "INSERT INTO observations (load_id, dataset, metric, entity, region, period, "
                "period_sort, source, value, unit) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(load_id, dataset, o["metric"], o["entity"], o["region"], o["period"],
                  o["period_sort"], o["source"], o["value"], o["unit"]) for o in observations],
            )
        conn.execute("INSERT INTO load_reports (load_id, report) VALUES (?, ?)",
                      (load_id, json.dumps(report, ensure_ascii=False)))
        conn.commit()
    return load_id


# --- reading (HTTP API + chatbot) -----------------------------------------------------------


def catalog(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for ds in db.fetchall(conn, "SELECT name, title FROM datasets ORDER BY name"):
        rows = db.fetchall(
            conn, "SELECT metric, entity, region, source, unit, period, period_sort "
                  "FROM latest_observations WHERE dataset=?", (ds["name"],),
        )
        last_load = db.fetchone(
            conn, "SELECT id, status, started_at, finished_at FROM loads "
                  "WHERE dataset=? ORDER BY id DESC LIMIT 1", (ds["name"],),
        )
        period_rows = sorted(rows, key=lambda r: r["period_sort"])
        out.append({
            "name": ds["name"], "title": ds["title"],
            "metrics": sorted({r["metric"] for r in rows if r["metric"]}),
            "entities": sorted({r["entity"] for r in rows if r["entity"]}),
            "regions": sorted({r["region"] for r in rows if r["region"]}),
            "sources": sorted({r["source"] for r in rows if r["source"]}),
            "units": sorted({r["unit"] for r in rows if r["unit"]}),
            "period_from": period_rows[0]["period"] if period_rows else None,
            "period_to": period_rows[-1]["period"] if period_rows else None,
            "last_load": dict(last_load) if last_load else None,
        })
    return out


def query_observations(conn: sqlite3.Connection, *, dataset: str, metrics: list[str] | None = None,
                        entities: list[str] | None = None, regions: list[str] | None = None,
                        sources: list[str] | None = None, period_from: str | None = None,
                        period_to: str | None = None, version: str = "latest") -> list[dict]:
    if version == "latest":
        table = "latest_observations"
    elif version == "all":
        table = "observations"
    else:
        try:
            load_id = int(version)
        except ValueError as exc:
            raise ValueError(f"잘못된 version 값입니다: {version!r}") from exc
        row = db.fetchone(conn, "SELECT id FROM loads WHERE id=? AND dataset=?", (load_id, dataset))
        if row is None:
            raise ValueError(f"해당 dataset의 load를 찾을 수 없습니다: {version}")
        table = "observations"

    clauses, params = ["dataset=?"], [dataset]
    if version not in ("latest", "all"):
        clauses.append("load_id=?")
        params.append(int(version))

    def _in_clause(col: str, values: list[str] | None) -> None:
        if values:
            clauses.append(f"{col} IN ({','.join('?' for _ in values)})")
            params.extend(values)

    _in_clause("metric", metrics)
    _in_clause("entity", entities)
    _in_clause("region", regions)
    _in_clause("source", sources)
    # period_from/period_to arrive as human period text ("2024Q1"), same as `period` itself --
    # normalize to period_sort for the comparison (a no-op if already period_sort-shaped, since
    # normalize_period only rewrites its own recognized formats).
    if period_from:
        clauses.append("period_sort >= ?")
        params.append(normalize_period(period_from).period_sort)
    if period_to:
        clauses.append("period_sort <= ?")
        params.append(normalize_period(period_to).period_sort)

    sql = (f"SELECT metric, entity, region, period, period_sort, source, value, unit "
           f"FROM {table} WHERE " + " AND ".join(clauses) +
           " ORDER BY period_sort, metric, entity, region, source")
    return [dict(r) for r in db.fetchall(conn, sql, params)]


def history(conn: sqlite3.Connection, *, dataset: str, metric: str, entity: str,
            period: str, source: str | None = None) -> list[dict]:
    clauses = ["l.dataset=?", "o.metric=?", "o.entity=?", "o.period=?", "l.status='ok'"]
    params: list[Any] = [dataset, metric, entity, period]
    if source is not None:
        clauses.append("o.source=?")
        params.append(source)
    sql = (
        "SELECT l.id AS load_id, l.started_at, l.finished_at, o.value, o.unit, o.region, o.source "
        "FROM observations o JOIN loads l ON o.load_id=l.id "
        "WHERE " + " AND ".join(clauses) + " ORDER BY l.id"
    )
    return [dict(r) for r in db.fetchall(conn, sql, params)]


def list_loads(conn: sqlite3.Connection, dataset: str | None = None) -> list[dict]:
    if dataset:
        rows = db.fetchall(conn, "SELECT * FROM loads WHERE dataset=? ORDER BY id DESC", (dataset,))
    else:
        rows = db.fetchall(conn, "SELECT * FROM loads ORDER BY id DESC")
    return [dict(r) for r in rows]


def get_load(conn: sqlite3.Connection, load_id: int) -> dict | None:
    row = db.fetchone(conn, "SELECT * FROM loads WHERE id=?", (load_id,))
    if row is None:
        return None
    out = dict(row)
    report_row = db.fetchone(conn, "SELECT report FROM load_reports WHERE load_id=?", (load_id,))
    out["report"] = json.loads(report_row["report"]) if report_row else None
    return out


def to_wide(rows: list[dict], row_dims: list[str], col_dims: list[str]) -> dict:
    """Long observation rows -> {"columns": [...], "rows": [[...]]}. Column order follows
    `period_sort` when `col_dims == ["period"]` (the common case, for a chronological x-axis);
    otherwise plain sorted order."""
    col_keys: list[tuple] = []
    seen: set[tuple] = set()
    period_sort_by_period: dict[str, str] = {}
    for r in rows:
        ck = tuple(str(r.get(d, "")) for d in col_dims)
        if ck not in seen:
            seen.add(ck)
            col_keys.append(ck)
        if col_dims == ["period"]:
            period_sort_by_period[r["period"]] = r["period_sort"]

    if col_dims == ["period"]:
        col_keys.sort(key=lambda ck: period_sort_by_period.get(ck[0], ck[0]))
    else:
        col_keys.sort()

    pivot: dict[tuple, dict[tuple, Any]] = {}
    for r in rows:
        rk = tuple(str(r.get(d, "")) for d in row_dims)
        ck = tuple(str(r.get(d, "")) for d in col_dims)
        pivot.setdefault(rk, {})[ck] = r["value"]

    row_keys = sorted(pivot)
    columns = list(row_dims) + [" / ".join(ck) for ck in col_keys]
    out_rows = [list(rk) + [pivot[rk].get(ck) for ck in col_keys] for rk in row_keys]
    return {"columns": columns, "rows": out_rows}
