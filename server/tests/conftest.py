from __future__ import annotations

import json
from pathlib import Path

import pytest

from aioffice import db
from aioffice.config import Settings
from aioffice.llm import BackendError


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = Settings.load(environ={"DATA_DIR": str(tmp_path / "data")})
    s.ensure_dirs()
    return s


@pytest.fixture()
def conn(settings: Settings):
    c = db.connect(settings.db_path)
    db.init_schema(c)
    yield c
    c.close()


class FakeBackend:
    """Stands in for DirectBackend in client tests."""

    def __init__(self, result=None, error=None, model: str = ""):
        self.result = result
        self.error = error
        self.model = model
        self.name = "direct"
        self.calls = 0
        self.seen: list[dict] = []

    def model_for(self) -> str:
        return self.model

    def run(self, messages, schema, model, timeout=None, max_tokens=None):
        self.calls += 1
        self.seen.append({"messages": messages, "schema": schema, "model": model,
                          "max_tokens": max_tokens})
        if self.error:
            raise BackendError(self.error)
        return dict(self.result), {"prompt_tokens": 10, "completion_tokens": 5}


@pytest.fixture()
def fake_backend(settings):
    def make(result=None, error=None):
        backend = FakeBackend(result, error)
        # Settings may be mutated by tests after this fixture runs, so read them lazily.
        backend.model_for = lambda: settings.llm_model_default
        return backend

    return make


class FakeContentBackend:
    """Stands in for DirectBackend for `complete_json`/`run_content` callers (the analyst
    pipeline never uses tool_choice) -- returns queued JSON payloads as plain content text."""

    def __init__(self, responses=None, model: str = "fake-model"):
        self.responses = list(responses or [])
        self.model = model
        self.name = "direct"
        self.calls: list[dict] = []

    def model_for(self) -> str:
        return self.model

    def run_content(self, messages, model, timeout=None, max_tokens=None):
        self.calls.append({"messages": messages, "model": model, "max_tokens": max_tokens})
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return content, {"prompt_tokens": 10, "completion_tokens": 20}


@pytest.fixture()
def analyst_llm(settings, conn):
    """A ready-to-use LLMClient backed by FakeContentBackend; append to `.backend.responses`."""
    from aioffice.llm.client import LLMClient

    backend = FakeContentBackend([])
    return LLMClient(settings, conn, backend=backend)
