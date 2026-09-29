from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Every key this deployment still reads, with its default. "" means "unset".
ENV_DEFAULTS: dict[str, str] = {
    "LLM_BASE_URL": "",
    "LLM_API_KEY": "",
    "LLM_MODEL_DEFAULT": "",
    "LLM_TIMEOUT_SEC": "300",
    "LLM_MAX_TOKENS": "8192",
    "LLM_TEMPERATURE": "0.2",
    "LLM_DISABLE_THINKING": "false",
    "LLM_CONTEXT_TOKENS": "32768",
    "KO_TOKENS_PER_CHAR": "1.0",
    "LLM_MAX_CONCURRENCY": "4",
    "EMBED_BASE_URL": "",
    "EMBED_API_KEY": "",
    "EMBED_MODEL": "BAAI-bge-m3",
    "EMBED_BATCH_SIZE": "32",
    "RERANK_BASE_URL": "",
    "RERANK_API_KEY": "",
    "RERANK_MODEL": "BAAI-bge-reranker-v2-m3",
    "RERANK_API_STYLE": "cohere",
    "RERANK_BATCH_SIZE": "32",
    "DATA_DIR": "",
}

RERANK_API_STYLES = ("cohere", "tei", "score")

# The deployment's config file, beside pyproject.toml. `Settings.load()` stays explicit --
# only a caller that wants file-backed config passes this, library callers and tests keep
# reading the process environment alone.
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file. KEY=VALUE per line, # comments, optional quotes."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


@dataclass
class Settings:
    data_dir: Path
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model_default: str = ""
    llm_timeout_sec: float = 300.0
    llm_max_tokens: int = 8192
    llm_temperature: float = 0.2
    llm_disable_thinking: bool = False
    llm_context_tokens: int = 32768
    ko_tokens_per_char: float = 1.0
    llm_max_concurrency: int = 4
    embed_base_url: str = ""
    embed_api_key: str = ""
    embed_model: str = "BAAI-bge-m3"
    embed_batch_size: int = 32
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = "BAAI-bge-reranker-v2-m3"
    rerank_api_style: str = "cohere"
    rerank_batch_size: int = 32

    @classmethod
    def load(cls, env_file: Path | None = None, environ: dict[str, str] | None = None) -> "Settings":
        environ = os.environ if environ is None else environ
        values = dict(ENV_DEFAULTS)
        if env_file is not None:
            values.update(read_env_file(Path(env_file)))
        for key in ENV_DEFAULTS:
            if environ.get(key):
                values[key] = environ[key]

        if not values["DATA_DIR"]:
            raise ValueError("DATA_DIR is required (set it in .env or the environment)")
        if values["RERANK_API_STYLE"] not in RERANK_API_STYLES:
            raise ValueError(f"RERANK_API_STYLE must be one of {RERANK_API_STYLES}")

        def as_int(key: str) -> int:
            return int(values[key])

        def as_float(key: str) -> float:
            return float(values[key])

        def as_bool(key: str) -> bool:
            value = values[key].strip().casefold()
            if value in {"true", "1", "yes", "on"}:
                return True
            if value in {"false", "0", "no", "off"}:
                return False
            raise ValueError(f"{key} must be a boolean")

        return cls(
            data_dir=Path(values["DATA_DIR"]),
            llm_base_url=values["LLM_BASE_URL"].rstrip("/"),
            llm_api_key=values["LLM_API_KEY"],
            llm_model_default=values["LLM_MODEL_DEFAULT"],
            llm_timeout_sec=as_float("LLM_TIMEOUT_SEC"),
            llm_max_tokens=as_int("LLM_MAX_TOKENS"),
            llm_temperature=as_float("LLM_TEMPERATURE"),
            llm_disable_thinking=as_bool("LLM_DISABLE_THINKING"),
            llm_context_tokens=as_int("LLM_CONTEXT_TOKENS"),
            ko_tokens_per_char=as_float("KO_TOKENS_PER_CHAR"),
            llm_max_concurrency=as_int("LLM_MAX_CONCURRENCY"),
            embed_base_url=values["EMBED_BASE_URL"].rstrip("/"),
            embed_api_key=values["EMBED_API_KEY"],
            embed_model=values["EMBED_MODEL"],
            embed_batch_size=as_int("EMBED_BATCH_SIZE"),
            rerank_base_url=values["RERANK_BASE_URL"].rstrip("/"),
            rerank_api_key=values["RERANK_API_KEY"] or values["LLM_API_KEY"],
            rerank_model=values["RERANK_MODEL"],
            rerank_api_style=values["RERANK_API_STYLE"],
            rerank_batch_size=as_int("RERANK_BATCH_SIZE"),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aioffice.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
