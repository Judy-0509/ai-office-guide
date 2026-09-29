from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

BROKERS_PATH = Path(__file__).resolve().parents[2] / "brokers.yaml"
_BROKER_SUFFIXES = tuple(sorted((
    "capital markets", "global markets", "securities", "research", "limited",
    "투자증권", "금융투자", "증권", "group", "capital", "markets", "co",
    "llc", "inc", "ltd",
), key=len, reverse=True))
_BROKER_KOREAN_SUFFIXES = ("투자증권", "금융투자", "증권")

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}
MONTH_ALT = "|".join(list(MONTHS) + [m[:3] for m in MONTHS])

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})"), "ymd"),
    (re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"), "ymd"),
    (re.compile(rf"({MONTH_ALT})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})", re.IGNORECASE), "mdy"),
    (re.compile(rf"(\d{{1,2}})\s+({MONTH_ALT})\.?,?\s+(\d{{4}})", re.IGNORECASE), "dmy"),
]


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def regex_date(text: str) -> str | None:
    for pattern, order in _PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        a, b, c = match.groups()
        if order == "ymd":
            out = _iso(int(a), int(b), int(c))
        elif order == "mdy":
            out = _iso(int(c), MONTHS[_full_month(a)], int(b))
        else:
            out = _iso(int(c), MONTHS[_full_month(b)], int(a))
        if out:
            return out
    return None


def _full_month(token: str) -> str:
    token = token.lower()
    for name in MONTHS:
        if name.startswith(token):
            return name
    return token


def _broker_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"\(\s*주\s*\)|주식회사", " ", text)
    text = " ".join(
        "".join(" " if unicodedata.category(ch).startswith("P") else ch for ch in text).split()
    )
    changed = True
    while changed:
        changed = False
        for suffix in _BROKER_KOREAN_SUFFIXES:
            if text.endswith(suffix):
                text = text[:-len(suffix)].rstrip()
                changed = True
                break
        if changed:
            continue
        for suffix in _BROKER_SUFFIXES:
            if text == suffix or text.endswith(" " + suffix):
                text = text[:-len(suffix)].rstrip()
                changed = True
                break
    return "".join(text.split())


@lru_cache(maxsize=4)
def load_brokers(path: str) -> dict[str, str]:
    """Aliases are stable for the life of a process."""
    records = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    aliases: dict[str, str] = {}
    for canonical, names in records.items():
        for name in [canonical, *(names or [])]:
            key = _broker_key(str(name))
            if key:
                aliases[key] = str(canonical)
    return aliases


def normalize_broker(raw: str | None, path: str | Path = BROKERS_PATH) -> str:
    value = (raw or "").strip()
    if not value:
        return value
    return load_brokers(str(Path(path))).get(_broker_key(value), value)


def detect_language(text: str) -> str:
    hangul = sum(1 for ch in text if "가" <= ch <= "힣")
    letters = sum(1 for ch in text if ch.isalpha()) + hangul
    if letters == 0:
        return "en"
    return "ko" if hangul / letters > 0.15 else "en"
