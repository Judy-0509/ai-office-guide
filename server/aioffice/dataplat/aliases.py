"""Optional `aliases.yaml` next to `source.yaml` (path configurable) -- short/company/internal
names the catalog itself doesn't have, so the rule-based fast path (`dataplat.chat`) can resolve
them without ever calling the LLM.

Shape, per dataset:

    <dataset name>:
      entities:
        삼성: ["모델A", "모델B"]              # an explicit list of real catalog entity names
        갤럭시: {column: entities, prefix: Galaxy}  # OR any catalog entity starting with a prefix
      regions:
        중국: China
      metrics:
        물량: volume_mu
        출하: volume_mu
      groups:                                # named entity groups -- same effect as `entities`,
        폴더블: ["모델A", "모델C"]              # kept separate only for readability in the file

Grouping by a source-table attribute that isn't one of the standard columns (e.g. a `company`
field only present in an `extra` JSON column) is NOT implemented here -- it would need a schema
change to carry that attribute through snapshot/observations, which isn't cheap. Use `groups`
(a hand-written list) for that instead; see 03-dataplat-manual.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DIMENSIONS = ("entities", "regions", "metrics")


@dataclass
class AliasConfig:
    raw: dict[str, dict[str, Any]] = field(default_factory=dict)  # {dataset: {dim: {...}}}


def load_aliases(path: Path | str | None) -> AliasConfig:
    if not path or not Path(path).exists():
        return AliasConfig()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"aliases.yaml의 최상위는 데이터셋별 매핑이어야 합니다: {path}")
    return AliasConfig(raw=data)


def resolve_aliases(cfg: AliasConfig, dataset: str, dim: str,
                     catalog_values: list[str]) -> dict[str, list[str]]:
    """alias phrase -> real catalog values for `dataset`'s `dim` ("entities"/"regions"/
    "metrics"), filtered to values that actually exist in the current catalog (so a stale alias
    pointing at a renamed/removed name just resolves to nothing, never an invalid spec).
    `groups` (entities only) are folded in alongside `entities`."""
    if dim not in DIMENSIONS:
        raise ValueError(f"dim은 {DIMENSIONS} 중 하나여야 합니다: {dim!r}")
    ds_cfg = cfg.raw.get(dataset) or {}
    entries: dict[str, Any] = dict(ds_cfg.get(dim) or {})
    if dim == "entities":
        entries.update(ds_cfg.get("groups") or {})

    catalog_set = set(catalog_values)
    out: dict[str, list[str]] = {}
    for phrase, spec in entries.items():
        if isinstance(spec, str):
            values = [spec] if spec in catalog_set else []
        elif isinstance(spec, list):
            values = [v for v in spec if v in catalog_set]
        elif isinstance(spec, dict) and spec.get("prefix"):
            prefix = str(spec["prefix"])
            values = [v for v in catalog_values if v.startswith(prefix)]
        else:
            values = []
        if values:
            out[phrase] = values
    return out
