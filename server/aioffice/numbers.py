"""Shared number-text normalization: strips currency/percent/comma/whitespace noise so two
different renderings of the same number ("1,234" vs "1234", "12.5%" vs "12.5") compare equal.
Used by the analyst pipeline's number-checked claims (`analyst.steps`) and dataplat's
number-checked chat explanations (`dataplat.chat`) -- kept dependency-free (stdlib only) so
either can import it without pulling in the other.
"""
from __future__ import annotations

import re
from typing import Any

_NUM_STRIP_RE = re.compile(r"[,%$\s]")


def normalize_number(value: Any) -> str:
    if value is None:
        return ""
    return _NUM_STRIP_RE.sub("", str(value))
