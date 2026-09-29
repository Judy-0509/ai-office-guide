"""Tests for aioffice.dataplat.aliases: aliases.yaml loading and phrase -> catalog value
resolution (list form, prefix form, groups, stale-name filtering)."""
from __future__ import annotations

import pytest

from aioffice.dataplat import aliases


def test_load_aliases_missing_path_is_empty(tmp_path):
    cfg = aliases.load_aliases(tmp_path / "does_not_exist.yaml")
    assert cfg.raw == {}


def test_load_aliases_none_path_is_empty():
    assert aliases.load_aliases(None).raw == {}


def test_resolve_list_form(tmp_path):
    p = tmp_path / "aliases.yaml"
    p.write_text("shipments:\n  entities:\n    삼성: [\"모델A\", \"모델B\"]\n", encoding="utf-8")
    cfg = aliases.load_aliases(p)
    resolved = aliases.resolve_aliases(cfg, "shipments", "entities", ["모델A", "모델B", "모델C"])
    assert resolved == {"삼성": ["모델A", "모델B"]}


def test_resolve_string_form(tmp_path):
    p = tmp_path / "aliases.yaml"
    p.write_text("shipments:\n  regions:\n    중국: China\n", encoding="utf-8")
    cfg = aliases.load_aliases(p)
    resolved = aliases.resolve_aliases(cfg, "shipments", "regions", ["China", "Korea"])
    assert resolved == {"중국": ["China"]}


def test_resolve_prefix_form(tmp_path):
    p = tmp_path / "aliases.yaml"
    p.write_text(
        "shipments:\n  entities:\n    갤럭시:\n      column: entities\n      prefix: Galaxy\n",
        encoding="utf-8",
    )
    cfg = aliases.load_aliases(p)
    resolved = aliases.resolve_aliases(
        cfg, "shipments", "entities", ["Galaxy S24", "Galaxy Z Fold", "iPhone 16"])
    assert resolved == {"갤럭시": ["Galaxy S24", "Galaxy Z Fold"]}


def test_resolve_groups_folded_into_entities(tmp_path):
    p = tmp_path / "aliases.yaml"
    p.write_text(
        "shipments:\n  groups:\n    폴더블: [\"모델A\", \"모델C\"]\n", encoding="utf-8")
    cfg = aliases.load_aliases(p)
    resolved = aliases.resolve_aliases(cfg, "shipments", "entities", ["모델A", "모델B", "모델C"])
    assert resolved == {"폴더블": ["모델A", "모델C"]}


def test_resolve_filters_out_stale_names_not_in_catalog(tmp_path):
    p = tmp_path / "aliases.yaml"
    p.write_text("shipments:\n  entities:\n    삼성: [\"모델A\", \"단종모델\"]\n", encoding="utf-8")
    cfg = aliases.load_aliases(p)
    resolved = aliases.resolve_aliases(cfg, "shipments", "entities", ["모델A", "모델B"])
    assert resolved == {"삼성": ["모델A"]}  # 단종모델 dropped, not an error


def test_resolve_unknown_dataset_is_empty(tmp_path):
    p = tmp_path / "aliases.yaml"
    p.write_text("shipments:\n  entities:\n    삼성: [\"모델A\"]\n", encoding="utf-8")
    cfg = aliases.load_aliases(p)
    assert aliases.resolve_aliases(cfg, "other_dataset", "entities", ["모델A"]) == {}


def test_resolve_bad_dim_raises(tmp_path):
    cfg = aliases.AliasConfig()
    with pytest.raises(ValueError):
        aliases.resolve_aliases(cfg, "shipments", "bogus", [])


def test_load_aliases_non_mapping_top_level_raises(tmp_path):
    p = tmp_path / "aliases.yaml"
    p.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError):
        aliases.load_aliases(p)
