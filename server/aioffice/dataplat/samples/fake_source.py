"""Builds a small, fully fictional SQLite database standing in for a team's existing
Excel-ingest/aggregation database, for tests and the live-check (`dataplat` never talks to a
real internal DB in this repo). Two raw tables in two different shapes (one already "long", one
"wide" with quarters as columns) plus a view that maps/unions both into the standard columns --
see `samples/example_view.sql` for the same view as a standalone, commented example, and
`samples/source.yaml` for the matching config.

`build(path, state="v1")` writes state v1; `state="v2"` changes one `shipments` value and drops
one `shipments` row, leaving `price_index` unchanged -- enough to exercise both "value changed"
and "row removed" in a diff, and a same-content skip on the untouched dataset.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SHIPMENTS_V1 = [
    # metric, entity, period, dept(region), inst(source), qty
    ("출하량", "모델A", "2024Q1", "한국", "기관A", "1,200"),
    ("출하량", "모델A", "2024Q2", "한국", "기관A", "1,350"),
    ("출하량", "모델B", "2024Q1", "한국", "기관A", "800"),
    ("출하량", "모델B", "2024Q2", "한국", "기관A", "910"),
    ("출하량", "모델C", "2024Q1", "북미", "기관B", "n.a."),  # dropped: non-numeric marker
    ("출하량", "모델C", "2024Q2", "북미", "기관B", "430"),
]

_SHIPMENTS_V2 = [
    ("출하량", "모델A", "2024Q1", "한국", "기관A", "1,500"),  # changed 1200 -> 1500
    ("출하량", "모델A", "2024Q2", "한국", "기관A", "1,350"),
    ("출하량", "모델B", "2024Q1", "한국", "기관A", "800"),
    ("출하량", "모델B", "2024Q2", "한국", "기관A", "910"),
    ("출하량", "모델C", "2024Q1", "북미", "기관B", "n.a."),
    # 모델C 2024Q2 row removed entirely
]

_PRICE_INDEX = [
    # entity, region, q1, q2, q3, q4
    ("제품A", "KR", 100.0, 101.5, 99.0, 103.2),
    ("제품B", "US", 98.0, 97.4, 96.8, 95.1),
]

_VIEW_SQL = """
CREATE VIEW v_dataplat_observations AS
SELECT 'shipments' AS dataset, metric, entity, dept AS region, period, inst AS broker,
       qty AS value, '' AS unit
FROM tbl_shipments_long
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q1' AS period,
       '' AS broker, q1 AS value, 'pt' AS unit FROM tbl_price_index_wide
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q2' AS period,
       '' AS broker, q2 AS value, 'pt' AS unit FROM tbl_price_index_wide
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q3' AS period,
       '' AS broker, q3 AS value, 'pt' AS unit FROM tbl_price_index_wide
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q4' AS period,
       '' AS broker, q4 AS value, 'pt' AS unit FROM tbl_price_index_wide
"""


def build(path: Path, state: str = "v1") -> Path:
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            "CREATE TABLE tbl_shipments_long "
            "(metric TEXT, entity TEXT, period TEXT, dept TEXT, inst TEXT, qty TEXT);"
            "CREATE TABLE tbl_price_index_wide "
            "(entity TEXT, region TEXT, q1 REAL, q2 REAL, q3 REAL, q4 REAL);"
        )
        shipments = _SHIPMENTS_V1 if state == "v1" else _SHIPMENTS_V2
        conn.executemany("INSERT INTO tbl_shipments_long VALUES (?,?,?,?,?,?)", shipments)
        conn.executemany("INSERT INTO tbl_price_index_wide VALUES (?,?,?,?,?,?)", _PRICE_INDEX)
        conn.executescript(_VIEW_SQL)
        conn.commit()
    finally:
        conn.close()
    return path


# --- a table holding two forecast vintages at once (source.yaml's version_column) --------------

_FORECAST_2026_07 = [
    # metric, entity, period, value, vintage -- all three entities' 07 forecast
    ("출하량전망", "모델A", "2026Q3", 100.0, "2026-07"),
    ("출하량전망", "모델B", "2026Q3", 200.0, "2026-07"),
    ("출하량전망", "모델C", "2026Q3", 150.0, "2026-07"),
]
_FORECAST_2026_08 = [
    # 08 vintage: A unchanged, B +25% (largest relative change), C -20%
    ("출하량전망", "모델A", "2026Q3", 100.0, "2026-08"),
    ("출하량전망", "모델B", "2026Q3", 250.0, "2026-08"),
    ("출하량전망", "모델C", "2026Q3", 120.0, "2026-08"),
]

_VERSIONED_VIEW_SQL = """
CREATE VIEW v_dataplat_versioned AS
SELECT 'forecast' AS dataset, metric, entity, '' AS region, period, '' AS source, value,
       '대' AS unit, vintage
FROM tbl_forecast
"""


def build_versioned(path: Path, state: str = "v1") -> Path:
    """A `forecast` dataset table carrying one or two forecast vintages in its `vintage`
    column. `state="v1"`: only the 2026-07 vintage (as if 08 hasn't arrived yet). `state="v2"`:
    both 2026-07 AND 2026-08 rows present together (08 appended alongside 07, exactly how a real
    vintage-carrying table grows) -- the scenario `version_column` exists for."""
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            "CREATE TABLE tbl_forecast (metric TEXT, entity TEXT, period TEXT, value REAL, "
            "vintage TEXT);"
        )
        rows = list(_FORECAST_2026_07)
        if state == "v2":
            rows += _FORECAST_2026_08
        conn.executemany("INSERT INTO tbl_forecast VALUES (?,?,?,?,?)", rows)
        conn.executescript(_VERSIONED_VIEW_SQL)
        conn.commit()
    finally:
        conn.close()
    return path


# --- two-digit-year quarter periods ("25Q3"), matching the first real in-house table -----------

_MODEL_FORECAST_25 = [
    # metric, entity, period ("YYQn"), value
    ("출하량", "모델A", "25Q1", 100.0), ("출하량", "모델A", "25Q2", 110.0),
    ("출하량", "모델A", "25Q3", 120.0), ("출하량", "모델A", "25Q4", 130.0),
    ("출하량", "모델B", "25Q1", 200.0), ("출하량", "모델B", "25Q2", 210.0),
    ("출하량", "모델B", "25Q3", 220.0), ("출하량", "모델B", "25Q4", 230.0),
]

_QUARTERLY_VIEW_SQL = """
CREATE VIEW v_dataplat_quarterly AS
SELECT 'model_forecast' AS dataset, metric, entity, '' AS region, period, '' AS source, value,
       '대' AS unit
FROM tbl_model_forecast
"""


def build_quarterly(path: Path) -> Path:
    """A single-vintage `model_forecast` dataset with periods spelled the way the first real
    in-house table wrote them -- two-digit-year quarters like "25Q1".."25Q4" (see
    `normalize.normalize_period`'s alt-spelling support). Used for the "2025년 모델별 합계"
    (year rollup) live-check."""
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            "CREATE TABLE tbl_model_forecast (metric TEXT, entity TEXT, period TEXT, value REAL);"
        )
        conn.executemany("INSERT INTO tbl_model_forecast VALUES (?,?,?,?)", _MODEL_FORECAST_25)
        conn.executescript(_QUARTERLY_VIEW_SQL)
        conn.commit()
    finally:
        conn.close()
    return path
