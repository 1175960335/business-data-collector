"""Normalisation and identity resolution.

Task book 004, stage C: entities must be de-duplicated across repeated runs
("重复执行不重复实体"), and missing values must be recorded as missing rather
than guessed ("缺失值用 null/空值与原因，不推测填充").

Identity priority for Google Maps payloads follows what stage B measured:

    place_id  ->  cid  ->  data_id  ->  link

Browser mode populates all four; fast mode only populates ``data_id``. When none
is present the record cannot be attributed reliably, so it is reported as a
missing-identity error instead of being merged on a guess.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

#: Ordered identity keys, most trustworthy first. ``source_id`` is the generic
#: slot stage E's non-Google providers fill with their own stable id.
IDENTITY_KEYS = ("place_id", "cid", "data_id", "link", "source_id")

_WHITESPACE = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"[^\d+]")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def normalize_text(value: object) -> str:
    """Unicode-normalise, casefold and collapse whitespace.

    Non-Latin scripts survive: the string is never transliterated or stripped of
    characters outside a Latin range.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\u00a0", " ")
    text = _WHITESPACE.sub(" ", text)
    return text.strip().casefold()


def normalize_phone(value: object) -> str:
    """Keep digits and a single leading '+'.

    Deliberately does not add a country code: guessing one and then marking the
    result valid is exactly what the task book forbids.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    digits = _NON_DIGIT.sub("", text)
    if digits.startswith("+"):
        digits = "+" + digits.lstrip("+")
    return digits


def domain_of(url: object) -> str:
    """Host of a URL without a leading 'www.', lower-cased. '' if unusable."""
    if not url:
        return ""
    text = str(url).strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        host = urlparse(text).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def is_valid_email_syntax(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or " " in text:
        return False
    if not _EMAIL.match(text):
        return False
    # The raw data contains placeholder text such as 'user@domain.com'; it is
    # syntactically valid, so it is kept but flagged by the caller.
    return True


def is_placeholder_email(value: object) -> bool:
    if not isinstance(value, str):
        return False
    local, _, domain = value.strip().partition("@")
    return local.casefold() in {"user", "your", "name", "email", "example", "test"} and domain.casefold() in {
        "domain.com",
        "example.com",
        "example.org",
        "email.com",
        "yourdomain.com",
    }


def coerce_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def first_non_empty(raw: dict, keys: tuple[str, ...] | list[str]) -> str | None:
    """First key holding a non-empty string. Does not fabricate values."""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def identity_key(raw: dict) -> tuple[str, str] | None:
    """Resolve (kind, value) for a payload, or None when it has no identity."""
    for key in IDENTITY_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return key, str(value)
    return None


def resolve_stable_key(raw: dict, provider: str = "") -> tuple[str, str] | None:
    """Resolve ``(id_kind, stable_key)`` for a payload, or None when it has none.

    Stage E adds provider-native ids (``source_id``). Such an id only means
    something *inside* its own provider — ``node/123`` is an arbitrary string
    until you know it is an OpenStreetMap element — so the stable key names the
    provider too. Two sources that both happen to use ``node/123`` then produce
    two entities, and the merge stage links them when the evidence says they are
    the same store.

    Google's own ids keep their historical form (``place_id:...``) so a database
    written by an earlier collector resolves to the same entities and a resumed
    run is not invalidated.

    A non-Google provider borrows **no** Google key: ``data_id:abc`` from a
    directory feed is not Google's ``data_id:abc``, and honouring it would give
    an unrelated record Google's identity (and dedupe it against the real
    Google record). Such providers resolve only their own ``source_id``.
    """
    is_google = provider in ("", "google_maps")
    for key in (IDENTITY_KEYS if is_google else ("source_id",)):
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, str):
            if not value.strip():
                continue
            text = value.strip()
        elif isinstance(value, (int, float)):
            text = str(value)
        else:
            continue

        if key == "source_id" and provider:
            kind_detail = str(raw.get("source_id_kind") or "").strip() or "source_id"
            kind = f"{provider}:{kind_detail}"
        else:
            kind = key
        return kind, stable_key(kind, text)
    return None


def stable_key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def fallback_key(raw: dict) -> tuple[str, str] | None:
    """A weaker key for payloads with no provider id.

    Only used to *report* that a record could not be attributed; the collector
    does not merge on this key, because name+address collisions merge distinct
    businesses.
    """
    name = normalize_text(raw.get("title"))
    address = normalize_text(raw.get("address") or raw.get("complete_address"))
    if name and address:
        return "name_address", f"{name}|{address}"
    lat = coerce_float(raw.get("latitude"))
    lon = coerce_float(raw.get("longitude"))
    if name and lat is not None and lon is not None:
        return "name_geo", f"{name}|{lat:.5f},{lon:.5f}"
    return None


def extract_emails(value: object) -> list[str]:
    """Normalise the emails field into a list of distinct addresses.

    The provider emits either a list or a single string. Nothing is inferred:
    an absent field yields an empty list.
    """
    if value is None:
        return []
    items: list[str] = []
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r"[,;\s]+", value) if part.strip()]
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                items.append(entry.strip())
    seen: list[str] = []
    for item in items:
        lowered = item.casefold()
        if lowered not in [s.casefold() for s in seen]:
            seen.append(item)
    return seen
