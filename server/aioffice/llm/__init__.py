from __future__ import annotations


class BackendError(Exception):
    """One backend attempt failed (timeout, HTTP 429/5xx, connection, bad JSON)."""


class LLMError(Exception):
    """Every backend in the order failed for this call."""
