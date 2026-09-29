from __future__ import annotations

import json
import logging
import re
from typing import Any

DIRECTION_ENUM = ["▲", "▼", "–"]
CONFIDENCE_ENUM = ["high", "med", "low"]

_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_FENCE_RE = re.compile(r"```[a-zA-Z]*")


class SchemaError(ValueError):
    """Model output did not match the submitted JSON schema."""


def extract_json_object(text: Any) -> Any | None:
    """Pulls the first balanced `{...}` JSON object out of raw model content.

    For backends where named `tool_choice` is unverified, the model answers with plain
    JSON in `content` instead of a tool call -- strips `<think>...</think>` blocks and
    markdown code fences first, since the in-house model wraps reasoning and answers that
    way. Returns None if no balanced, parseable object is found.
    """
    if not isinstance(text, str):
        return None
    text = _THINK_RE.sub("", text)
    text = _FENCE_RE.sub("", text).replace("```", "")
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


logger = logging.getLogger(__name__)
_TYPES: dict[str, Any] = {
    "object": dict, "array": list, "string": str, "number": (int, float),
    "integer": int, "boolean": bool,
}


def validate(data: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the JSON Schema subset used by the model prompts."""
    expected = schema.get("type")
    if expected:
        python_type = _TYPES[expected]
        if expected in {"integer", "number"} and isinstance(data, bool):
            raise SchemaError(f"{path}: expected {expected}, got bool")
        if not isinstance(data, python_type):
            raise SchemaError(f"{path}: expected {expected}, got {type(data).__name__}")
    if "enum" in schema and data not in schema["enum"]:
        raise SchemaError(f"{path}: {data!r} not in {schema['enum']}")
    if expected == "object":
        required = set(schema.get("required", []))
        for key in required:
            if key not in data:
                raise SchemaError(f"{path}: missing required field {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in data and (data[key] is not None or key in required):
                validate(data[key], sub, f"{path}.{key}")
    elif expected == "array" and "items" in schema:
        for i, item in enumerate(data):
            validate(item, schema["items"], f"{path}[{i}]")


def coerce(data: Any, schema: dict[str, Any]) -> Any:
    """Clean common model formatting slips before strict validation."""
    expected = schema.get("type")
    if expected == "object" and isinstance(data, dict):
        properties = schema.get("properties", {})
        return {key: coerce(value, properties[key]) if key in properties else value
                for key, value in data.items()}
    if expected == "array" and isinstance(data, list):
        item_schema = schema.get("items")
        if not item_schema:
            return list(data)
        cleaned, dropped = [], 0
        for item in data:
            item = coerce(item, item_schema)
            try:
                validate(item, item_schema)
            except SchemaError:
                dropped += 1
            else:
                cleaned.append(item)
        if dropped:
            logger.warning("dropped %d invalid items from model array", dropped)
        return cleaned
    if expected == "integer":
        if isinstance(data, bool) or isinstance(data, int):
            return data
        if isinstance(data, float) and data.is_integer():
            return int(data)
        if isinstance(data, str):
            match = re.fullmatch(r"\s*(?:p\.?\s*)?([+-]?\d+)(?:\.0+)?\s*(?:페이지)?\s*", data, re.IGNORECASE)
            if match:
                return int(match.group(1))
    if expected == "number":
        if isinstance(data, (int, float)) and not isinstance(data, bool):
            return data
        if isinstance(data, str):
            try:
                return float(data.strip().replace(",", "").removeprefix("$").removesuffix("%").strip())
            except ValueError:
                pass
    if expected == "boolean" and isinstance(data, str):
        value = data.strip().casefold()
        if value in {"true", "false"}:
            return value == "true"
    if expected == "string" and isinstance(data, (int, float)) and not isinstance(data, bool):
        return str(data)
    enum = schema.get("enum")
    if expected == "string" and enum and isinstance(data, str):
        value = data.strip()
        folded = value.casefold()
        if enum == DIRECTION_ENUM:
            if folded in {"up", "↑", "+", "increase", "rise", "상승", "증가", "확대"}:
                return "▲"
            if folded in {"down", "↓", "decrease", "fall", "하락", "감소", "축소"}:
                return "▼"
            if folded in {"-", "—", "−", "~", "→", "flat", "neutral", "보합", "유지", "중립"}:
                return "–"
        if enum == CONFIDENCE_ENUM and folded in {"medium", "mid", "m", "moderate"}:
            return "med"
        for allowed in enum:
            if folded == str(allowed).casefold():
                return allowed
    return data
