from __future__ import annotations

from pathlib import Path

import pytest

from aioffice.config import Settings


def test_reranker_settings_are_validated_and_parsed(tmp_path: Path):
    settings = Settings.load(environ={
        "DATA_DIR": str(tmp_path), "RERANK_BASE_URL": "http://rerank/",
        "RERANK_API_KEY": "rank-key", "RERANK_MODEL": "custom",
        "RERANK_API_STYLE": "tei", "RERANK_BATCH_SIZE": "8",
    })
    assert settings.rerank_base_url == "http://rerank"
    assert settings.rerank_api_key == "rank-key"
    assert settings.rerank_model == "custom" and settings.rerank_api_style == "tei"
    assert settings.rerank_batch_size == 8
    with pytest.raises(ValueError, match="RERANK_API_STYLE"):
        Settings.load(environ={"DATA_DIR": str(tmp_path), "RERANK_API_STYLE": "unknown"})


def test_reranker_key_falls_back_to_llm_api_key(tmp_path: Path):
    s = Settings.load(environ={"DATA_DIR": str(tmp_path), "LLM_API_KEY": "secret"})
    assert s.rerank_base_url == ""
    assert s.rerank_api_key == "secret"
    assert s.rerank_model == "BAAI-bge-reranker-v2-m3"
    assert s.rerank_api_style == "cohere" and s.rerank_batch_size == 32


def test_env_file_is_parsed_and_process_environment_wins(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(f"DATA_DIR={tmp_path}\nLLM_MAX_CONCURRENCY=8\n", encoding="utf-8")
    s = Settings.load(env_file=env, environ={"LLM_MAX_CONCURRENCY": "2"})
    assert s.llm_max_concurrency == 2 and s.data_dir == tmp_path


def test_data_dir_is_required():
    with pytest.raises(ValueError, match="DATA_DIR"):
        Settings.load(environ={})


def test_derived_paths_and_directories(tmp_path: Path):
    settings = Settings.load(environ={"DATA_DIR": str(tmp_path)})
    settings.ensure_dirs()
    assert settings.db_path == tmp_path / "aioffice.db"
    assert tmp_path.is_dir()
