"""Small shared helpers."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def utc_now_iso() -> str:
    """Timestamp with microsecond precision, always UTC and always 'Z'."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def to_iso(value: _dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(value: Any) -> str:
    """Stable hash of a payload, used to recognise a repeated observation."""
    return sha256_hex(canonical_json(value))


def clip(text: str | None, limit: int = 500) -> str | None:
    """Shorten a value for storage as an evidence snippet."""
    if text is None:
        return None
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"
