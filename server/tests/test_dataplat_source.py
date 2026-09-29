"""Tests for aioffice.dataplat.source: source.yaml parsing."""
from __future__ import annotations

import pytest

from aioffice.dataplat.source import load_source


def test_view_expands_to_select_star(tmp_path):
    p = tmp_path / "source.yaml"
    p.write_text("db: data.sqlite\nview: v_obs\n", encoding="utf-8")
    cfg = load_source(p)
    assert cfg.query == "SELECT * FROM v_obs"
    assert cfg.db == (tmp_path / "data.sqlite").resolve()


def test_query_used_as_is(tmp_path):
    p = tmp_path / "source.yaml"
    p.write_text("db: data.sqlite\nquery: SELECT * FROM my_view\n", encoding="utf-8")
    cfg = load_source(p)
    assert cfg.query == "SELECT * FROM my_view"


def test_rename_map_parsed(tmp_path):
    p = tmp_path / "source.yaml"
    p.write_text("db: data.sqlite\nview: v_obs\nrename:\n  broker: source\n  dept: region\n",
                  encoding="utf-8")
    cfg = load_source(p)
    assert cfg.rename == {"broker": "source", "dept": "region"}


def test_absolute_db_path_kept_as_is(tmp_path):
    abs_db = tmp_path / "elsewhere" / "team.sqlite"
    p = tmp_path / "source.yaml"
    p.write_text(f"db: {abs_db.as_posix()}\nview: v_obs\n", encoding="utf-8")
    cfg = load_source(p)
    assert cfg.db == abs_db


def test_missing_db_raises(tmp_path):
    p = tmp_path / "source.yaml"
    p.write_text("view: v_obs\n", encoding="utf-8")
    with pytest.raises(ValueError, match="db"):
        load_source(p)


def test_both_query_and_view_raises(tmp_path):
    p = tmp_path / "source.yaml"
    p.write_text("db: data.sqlite\nview: v_obs\nquery: SELECT 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="query.*view|view.*query"):
        load_source(p)


def test_neither_query_nor_view_raises(tmp_path):
    p = tmp_path / "source.yaml"
    p.write_text("db: data.sqlite\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_source(p)


def test_unsafe_view_name_raises(tmp_path):
    p = tmp_path / "source.yaml"
    p.write_text("db: data.sqlite\nview: 'v_obs; DROP TABLE x'\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_source(p)


def test_version_column_defaults_to_none(tmp_path):
    p = tmp_path / "source.yaml"
    p.write_text("db: data.sqlite\nview: v_obs\n", encoding="utf-8")
    cfg = load_source(p)
    assert cfg.version_column is None


def test_version_column_parsed(tmp_path):
    p = tmp_path / "source.yaml"
    p.write_text("db: data.sqlite\nview: v_obs\nversion_column: vintage\n", encoding="utf-8")
    cfg = load_source(p)
    assert cfg.version_column == "vintage"


def test_version_column_empty_string_raises(tmp_path):
    p = tmp_path / "source.yaml"
    p.write_text("db: data.sqlite\nview: v_obs\nversion_column: ''\n", encoding="utf-8")
    with pytest.raises(ValueError, match="version_column"):
        load_source(p)
