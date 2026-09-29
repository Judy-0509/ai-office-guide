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

    db_path = Path(db)
    if not db_path.is_absolute():
        db_path = (path.parent / db_path).resolve()

    return SourceConfig(db=db_path, query=query, rename=rename, path=path)
