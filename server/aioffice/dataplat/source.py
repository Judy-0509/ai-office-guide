"""Source config: `source.yaml` points at the team's own SQLite (their existing Excel-ingest +
aggregation pipeline already writes it) and one query or view that returns rows shaped like the
standard columns `dataset, metric, entity, region, period, source, value, unit` (region/source/
unit may be absent -- `dataplat` treats them as blank). `dataplat` never writes to that
database: `dataplat.snapshot` opens it read-only and copies a point-in-time snapshot into its
own `dataplat.sqlite`.

Example `source.yaml`:

    db: C:/data/team.sqlite
    view: v_dataplat_observations
    rename:
      dept: region
      inst: source

or with a raw query instead of `view` (lets you UNION ALL several existing tables, or rename
columns in SQL directly -- see `samples/source.yaml` and `samples/example_view.sql`):

    db: C:/data/team.sqlite
    query: >
      SELECT dataset, metric, entity, region, period, source, value, unit
      FROM v_shipments

A table that holds several forecast vintages at once (e.g. a "2026-07" and a "2026-08" forecast
both present in the same source table, in one extra column beyond the 8 standard ones) needs
`version_column` -- see `samples/source_versioned.yaml` and 03-dataplat-manual.md. Without it, a
vintage-carrying table must NOT be queried as-is: summing/averaging across vintages silently
mixes forecasts from different dates into one meaningless number.

    db: C:/data/team.sqlite
    view: v_forecast_versions
    version_column: vintage
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

STANDARD_COLUMNS = ("dataset", "metric", "entity", "region", "period", "source", "value", "unit")
REQUIRED_COLUMNS = ("dataset", "metric", "entity", "period", "value")

_SAFE_NAME = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


@dataclass
class SourceConfig:
    db: Path
    query: str
    rename: dict[str, str] = field(default_factory=dict)
    version_column: str | None = None
    path: Path | None = None


def load_source(path: Path) -> SourceConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: source.yaml은 매핑(딕셔너리)이어야 합니다")

    db = raw.get("db")
    if not db:
        raise ValueError(f"{path}: db (소스 SQLite 경로)가 필요합니다")

    query, view = raw.get("query"), raw.get("view")
    if bool(query) == bool(view):
        raise ValueError(f"{path}: query 또는 view 중 정확히 하나를 지정해야 합니다")
    if view:
        if not view or any(ch not in _SAFE_NAME for ch in view):
            raise ValueError(f"{path}: view 이름이 올바르지 않습니다: {view!r}")
        query = f"SELECT * FROM {view}"

    rename = {str(k): str(v) for k, v in (raw.get("rename") or {}).items()}

    version_column = raw.get("version_column")
    if version_column is not None:
        version_column = str(version_column)
        if not version_column:
            raise ValueError(f"{path}: version_column은 빈 문자열일 수 없습니다")

    db_path = Path(db)
    if not db_path.is_absolute():
        db_path = (path.parent / db_path).resolve()

    return SourceConfig(db=db_path, query=query, rename=rename,
                         version_column=version_column, path=path)
