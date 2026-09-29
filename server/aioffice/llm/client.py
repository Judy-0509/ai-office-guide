from __future__ import annotations

import sqlite3
import hashlib
import math
import time
import threading
from typing import Any, Callable, Iterator

from .. import db
from ..config import Settings
from . import BackendError, LLMError, schemas
from .direct import DirectBackend

MAX_ATTEMPTS = 2  # one retry on a schema/backend error before the call counts as failed


class LLMClient:
    """The one entry point to the in-house model: a single direct backend."""

    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        backend: Any = None,
        now: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.conn = conn
        self.now = now
        self.backend = backend or DirectBackend(settings)
        self._semaphore = threading.Semaphore(max(1, settings.llm_max_concurrency))

    def estimate_tokens(self, text: str) -> int:
        east_asian = sum(
            1 for ch in text
            if _is_hangul_or_cjk(ord(ch))
        )
        other = len(text) - east_asian
        return max(1, math.ceil(
            east_asian * self.settings.ko_tokens_per_char + other / 4
        ))

    @property
    def embeddings_enabled(self) -> bool:
        return bool(self.settings.embed_base_url)

    @property
    def rerank_enabled(self) -> bool:
        return bool(self.settings.rerank_base_url)

    def rerank(
        self, query: str, documents: list[str], *, task_id: int | None = None
    ) -> list[float]:
        """Return reranker scores in input order; relevance never generates text."""
        if not self.rerank_enabled:
            raise LLMError("RERANK_BASE_URL is not configured")
        if not documents:
            return []
        if not hasattr(self.backend, "post_json"):
            raise LLMError("backend cannot send reranker requests")
        result = [None] * len(documents)
        size = max(1, self.settings.rerank_batch_size)
        style = self.settings.rerank_api_style
        for start in range(0, len(documents), size):
            batch = [text[:2000] for text in documents[start:start + size]]
            payload = (
                {"model": self.settings.rerank_model, "query": query, "documents": batch}
                if style == "cohere" else
                {"query": query, "texts": batch}
                if style == "tei" else
                {"model": self.settings.rerank_model, "text_1": query, "text_2": batch}
            )
            started = time.perf_counter()
            try:
                response = self.backend.post_json(
                    f"{self.settings.rerank_base_url}/{'score' if style == 'score' else 'rerank'}",
                    payload, base_url=self.settings.rerank_base_url,
                    api_key=self.settings.rerank_api_key,
                )
                rows = (response.get("results", []) if style == "cohere" else
                        response if style == "tei" else response.get("data", []))
                scores: dict[int, float] = {}
                for row in rows:
                    index = int(row["index"])
                    score = float(row.get("relevance_score", row.get("score")))
                    if index < 0 or index >= len(batch) or index in scores or not math.isfinite(score):
                        raise ValueError("invalid reranker index or score")
                    if score < 0 or score > 1:
                        score = 1.0 / (1.0 + math.exp(-max(-709.0, min(709.0, score))))
                    scores[index] = score
                if len(scores) != len(batch):
                    raise ValueError("reranker response omitted an input index")
                for index, score in scores.items():
                    result[start + index] = score
                self._log(task_id=task_id, agent="", step="rerank", backend="rerank",
                          model=self.settings.rerank_model,
                          latency_ms=int((time.perf_counter() - started) * 1000), status="ok")
            except Exception as exc:
                self._log(task_id=task_id, agent="", step="rerank", backend="rerank",
                          model=self.settings.rerank_model,
                          latency_ms=int((time.perf_counter() - started) * 1000),
                          status="error", error=str(exc)[:500])
                raise LLMError(f"reranker batch failed: {exc}") from exc
        return [float(score) for score in result]

    def embed(self, texts: list[str], *, task_id: int | None = None) -> list[list[float]]:
        """Embed only cache misses and return vectors in the input order."""
        if not texts:
            return []
        if not self.embeddings_enabled:
            raise LLMError("EMBED_BASE_URL is not configured")

        import numpy as np

        model = self.settings.embed_model
        hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
        vectors: dict[str, Any] = {}
        with db.WRITE_LOCK:
            for text_hash in dict.fromkeys(hashes):
                row = db.fetchone(
                    self.conn,
                    "SELECT dim, vector FROM embeddings WHERE text_hash=? AND model=?",
                    (text_hash, model),
                )
                if row is not None:
                    vector = np.frombuffer(row["vector"], dtype=np.float32)
                    norm = float(np.linalg.norm(vector))
                    if (len(vector) == row["dim"] and len(vector) and norm
                            and np.isfinite(vector).all()):
                        vectors[text_hash] = vector / norm

        missing = [text_hash for text_hash in dict.fromkeys(hashes) if text_hash not in vectors]
        text_by_hash = dict(zip(hashes, texts))
        if not hasattr(self.backend, "post_json"):
            raise LLMError("backend cannot send embedding requests")
        batch_size = max(1, self.settings.embed_batch_size)
        api_key = self.settings.embed_api_key or self.settings.llm_api_key
        for start in range(0, len(missing), batch_size):
            batch_hashes = missing[start:start + batch_size]
            started = time.perf_counter()
            try:
                response = self.backend.post_json(
                    f"{self.settings.embed_base_url.rstrip('/')}/embeddings",
                    {"model": model, "input": [text_by_hash[h] for h in batch_hashes]},
                    base_url=self.settings.embed_base_url,
                    api_key=api_key,
                )
                records = response["data"]
                ordered = [None] * len(batch_hashes)
                for record in records:
                    index = int(record["index"])
                    if index < 0 or index >= len(ordered) or ordered[index] is not None:
                        raise ValueError("invalid embedding index")
                    vector = np.asarray(record["embedding"], dtype=np.float32)
                    if vector.ndim != 1 or not len(vector) or not np.isfinite(vector).all():
                        raise ValueError("invalid embedding vector")
                    norm = float(np.linalg.norm(vector))
                    if norm == 0:
                        raise ValueError("embedding vector has zero length")
                    ordered[index] = vector / norm
                if any(vector is None for vector in ordered):
                    raise ValueError("embedding response omitted an input index")
                with db.WRITE_LOCK:
                    for text_hash, vector in zip(batch_hashes, ordered):
                        vectors[text_hash] = vector
                        self.conn.execute(
                            "INSERT OR REPLACE INTO embeddings(text_hash, model, dim, vector) "
                            "VALUES (?, ?, ?, ?)",
                            (text_hash, model, len(vector), vector.tobytes()),
                        )
                    self.conn.commit()
                self._log(
                    task_id=task_id, agent="", step="embed", backend="embed",
                    model=model,
                    latency_ms=int((time.perf_counter() - started) * 1000), status="ok",
                )
            except Exception as exc:
                self._log(
                    task_id=task_id, agent="", step="embed", backend="embed",
                    model=model,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status="error", error=str(exc)[:500],
                )
                raise LLMError(f"embedding batch failed: {exc}") from exc
        return [vectors[text_hash].astype("float32").tolist() for text_hash in hashes]

    # --- logging ------------------------------------------------------------
    def _log(self, **values: Any) -> None:
        with db.WRITE_LOCK:
            db.insert(self.conn, "llm_calls", **values)
            self.conn.commit()

    # --- the one entry point ------------------------------------------------
    def complete(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        step: str,
        agent: str,
        task_id: int | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        use_model = model if model is not None else self.backend.model_for()
        errors: list[str] = []

        for _ in range(MAX_ATTEMPTS):
            started = time.perf_counter()
            try:
                data, usage = self._run(messages, schema, use_model, max_tokens)
                data = schemas.coerce(data, schema)
                schemas.validate(data, schema)
            except BackendError as exc:
                self._log(
                    task_id=task_id, agent=agent, step=step, backend=self.backend.name,
                    model=use_model, latency_ms=int((time.perf_counter() - started) * 1000),
                    status="error", error=str(exc)[:500],
                )
                errors.append(str(exc))
                continue
            except schemas.SchemaError as exc:
                self._log(
                    task_id=task_id, agent=agent, step=step, backend=self.backend.name,
                    model=use_model, latency_ms=int((time.perf_counter() - started) * 1000),
                    status="error", error=str(exc)[:500],
                )
                errors.append(str(exc))
                continue
            self._log(
                task_id=task_id, agent=agent, step=step, backend=self.backend.name,
                model=use_model, prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_ms=int((time.perf_counter() - started) * 1000), status="ok",
            )
            return data

        raise LLMError(f"step={step} failed: {'; '.join(errors)}")

    def _run(self, messages, schema, model, max_tokens=None) -> tuple[dict, dict]:
        with self._semaphore:
            return self.backend.run(messages, schema, model, max_tokens=max_tokens)

    # --- no-tools JSON-in-content entry point (analyst pipeline) -------------------------
    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        step: str,
        agent: str,
        task_id: int | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Like `complete()`, but for backends where named `tool_choice` is unverified: the
        model answers with plain JSON in `content` instead of a tool call. Same retry count,
        schema validation and `llm_calls` logging as `complete()`."""
        use_model = model if model is not None else self.backend.model_for()
        errors: list[str] = []

        for _ in range(MAX_ATTEMPTS):
            started = time.perf_counter()
            try:
                content, usage = self._run_content(messages, use_model, max_tokens)
                data = schemas.extract_json_object(content)
                if data is None:
                    raise schemas.SchemaError("no JSON object found in model output")
                data = schemas.coerce(data, schema)
                schemas.validate(data, schema)
            except BackendError as exc:
                self._log(
                    task_id=task_id, agent=agent, step=step, backend=self.backend.name,
                    model=use_model, latency_ms=int((time.perf_counter() - started) * 1000),
                    status="error", error=str(exc)[:500],
                )
                errors.append(str(exc))
                continue
            except schemas.SchemaError as exc:
                self._log(
                    task_id=task_id, agent=agent, step=step, backend=self.backend.name,
                    model=use_model, latency_ms=int((time.perf_counter() - started) * 1000),
                    status="error", error=str(exc)[:500],
                )
                errors.append(str(exc))
                continue
            self._log(
                task_id=task_id, agent=agent, step=step, backend=self.backend.name,
                model=use_model, prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_ms=int((time.perf_counter() - started) * 1000), status="ok",
            )
            return data

        raise LLMError(f"step={step} failed: {'; '.join(errors)}")

    def _run_content(self, messages, model, max_tokens=None) -> tuple[str, dict]:
        with self._semaphore:
            return self.backend.run_content(messages, model, max_tokens=max_tokens)

    # --- streaming entry point (slides studio) -------------------------------
    def stream_complete(
        self,
        messages: list[dict[str, str]],
        *,
        step: str,
        agent: str,
        task_id: int | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        cancel: Any = None,
    ) -> Iterator[tuple[str, Any]]:
        """Stream a chat completion, forwarding every `llm.stream.stream_chat` event, and
        log one llm_calls row at the end. If the backend sent no usage, estimates completion
        tokens from the accumulated content text. Never logs the API key.

        A generator must not yield from inside `finally` (the consumer stopping early raises
        "generator ignored GeneratorExit" back through it) -- `finally` only logs; the final
        usage event is yielded after the try/finally, so it is skipped if an exception
        propagates instead of falling through."""
        from .stream import stream_chat  # lazy: keeps this method's httpx use out of the hot path

        use_model = model if model is not None else self.backend.model_for()
        started = time.perf_counter()
        prompt_tokens = completion_tokens = 0
        reasoning_tokens: int | None = None
        content_text = ""
        got_usage = False
        status, error = "ok", None
        try:
            with self._semaphore:
                for event, payload in stream_chat(
                    self.settings, messages, max_tokens or self.settings.llm_max_tokens,
                    cancel=cancel,
                ):
                    if event == "content":
                        content_text += payload
                    elif event == "usage":
                        prompt_tokens = int(payload.get("prompt_tokens", 0))
                        completion_tokens = int(payload.get("completion_tokens", 0))
                        details = payload.get("completion_tokens_details") or {}
                        if "reasoning_tokens" in details:
                            reasoning_tokens = int(details["reasoning_tokens"])
                        got_usage = True
                    elif event == "error":
                        status, error = "error", str(payload)[:500]
                    yield event, payload
        except BackendError as exc:
            status, error = "error", str(exc)[:500]
            raise
        finally:
            if cancel is not None and cancel.is_set():
                status = "cancelled"
            if not got_usage:
                completion_tokens = self.estimate_tokens(content_text) if content_text else 0
            log_kwargs = dict(
                task_id=task_id, agent=agent, step=step, backend="direct", model=use_model,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                latency_ms=int((time.perf_counter() - started) * 1000), status=status,
            )
            if error:
                log_kwargs["error"] = error
            self._log(**log_kwargs)
        yield ("usage", {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "estimated": not got_usage, "reasoning_tokens": reasoning_tokens,
        })


def _is_hangul_or_cjk(codepoint: int) -> bool:
    return (
        0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xA960 <= codepoint <= 0xA97F
        or 0xAC00 <= codepoint <= 0xD7A3
        or 0xD7B0 <= codepoint <= 0xD7FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
        or 0x30000 <= codepoint <= 0x323AF
    )
