"""Phone validation via libphonenumber.

Task book 004, stage D:

    "电话：成熟号码库、国家上下文、E.164、分机和原值；不能猜国家后强行标
     valid。"

Hand-written phone regular expressions are exactly what the task book rules out.
This module uses ``phonenumbers``, the Python port of Google's libphonenumber,
which knows the national numbering plans.

The country-context rule
------------------------
A number written ``+49 30 12345678`` carries its own country, so it can be
judged on its own. A number written ``030 12345678`` cannot: it is only
meaningful relative to a country. The run's configured ``location.country`` is a
declared context, not a guess, so it is used to parse such a number — but the
result is reported as ``uncertain``, never as ``valid``, because the source
itself did not say which country it meant. That is the distinction the task book
draws.

The raw value is always kept. ``normalized_value`` holds the E.164 form when one
could be derived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Machine-readable states (see :mod:`collector.emailcheck` for the convention).
STATE_E164_VALID = "e164_valid"
STATE_NATIONAL_VALID_REGION_CONTEXT = "national_valid_with_region_context"
STATE_NO_COUNTRY_CONTEXT = "no_country_context"
STATE_PARSE_ERROR = "parse_error"
STATE_NOT_A_VALID_NUMBER = "not_a_valid_number"
STATE_EMPTY = "empty"
STATE_NOT_CHECKED = "syntax_not_checked"

STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_UNCERTAIN = "uncertain"
STATUS_UNVERIFIED = "unverified"

_STATE_TO_STATUS = {
    STATE_E164_VALID: STATUS_VALID,
    STATE_NATIONAL_VALID_REGION_CONTEXT: STATUS_UNCERTAIN,
    STATE_NO_COUNTRY_CONTEXT: STATUS_UNCERTAIN,
    STATE_PARSE_ERROR: STATUS_INVALID,
    STATE_NOT_A_VALID_NUMBER: STATUS_INVALID,
    STATE_EMPTY: STATUS_INVALID,
    STATE_NOT_CHECKED: STATUS_UNVERIFIED,
}

#: libphonenumber's number types, as readable labels for the report.
_TYPE_NAMES = {
    0: "fixed_line_or_mobile",
    1: "fixed_line",
    2: "mobile",
    3: "toll_free",
    4: "premium_rate",
    5: "shared_cost",
    6: "voip",
    7: "personal_number",
    8: "pager",
    9: "uan",
    10: "voicemail",
    27: "unknown",
}


@dataclass
class PhoneVerdict:
    raw: str
    state: str = STATE_NOT_CHECKED
    status: str = STATUS_UNVERIFIED
    note: str = ""
    e164: str | None = None
    extension: str | None = None
    country_code: int | None = None
    region: str | None = None
    number_type: str = ""
    national_format: str | None = None
    international_format: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)

    @property
    def normalized(self) -> str | None:
        """The value used for matching: E.164 when known, else the raw digits."""
        if self.e164:
            return self.e164
        if self.status == STATUS_INVALID:
            return None
        return self.raw.strip() or None

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "state": self.state,
            "status": self.status,
            "note": self.note,
            "e164": self.e164,
            "extension": self.extension,
            "country_code": self.country_code,
            "region": self.region,
            "number_type": self.number_type,
            "checks": dict(self.checks),
        }


def _strip_tel_prefix(value: str) -> tuple[str, bool]:
    text = value.strip()
    if text.lower().startswith("tel:"):
        return text[4:].strip(), True
    return text, False


#: Chinese sources write the extension with a Chinese word, not "ext":
#: ``0571-88889999转802``, ``010-12345678 分机 12``. libphonenumber only knows
#: the Latin markers, so without this the whole number is a ``parse_error`` —
#: i.e. a perfectly good Hangzhou landline would be thrown away as invalid
#: purely because the source is written in Chinese.
_CN_EXTENSION_RE = re.compile(
    r"(?P<marker>转接|转|分机|分機|内线|內線|轉)\s*(?P<ext>\d{1,6})\s*$"
)


def _normalise_extension_marker(value: str) -> tuple[str, str | None]:
    """Rewrite a Chinese extension marker into the ``ext`` form, or leave as-is.

    The raw value is what the verdict keeps and what the export shows; this only
    produces something libphonenumber can parse.
    """
    match = _CN_EXTENSION_RE.search(value)
    if not match:
        return value, None
    head = value[: match.start()].strip(" \t,;、-－—")
    if not head:
        # Nothing but an extension marker: not a phone number at all, and
        # rewriting it would invent one.
        return value, None
    return f"{head} ext. {match.group('ext')}", match.group("marker")


def check_phone(
    value: object,
    *,
    region: str | None = None,
    rules: dict | None = None,
) -> PhoneVerdict:
    """Validate one phone number.

    ``region`` is the country context from the configuration (ISO-3166 alpha-2).
    Passing ``None`` means "no context available", and any number without its own
    country code is then left ``uncertain``.
    """
    rules = rules or {}
    text = "" if value is None else str(value).strip()
    verdict = PhoneVerdict(raw=text)

    if not text:
        verdict.state = STATE_EMPTY
        verdict.status = STATUS_INVALID
        verdict.note = "empty value"
        return verdict

    if not rules.get("phone_syntax", True):
        verdict.state = STATE_NOT_CHECKED
        verdict.status = STATUS_UNVERIFIED
        verdict.note = "phone syntax checking is disabled by validation.rules.phone_syntax"
        return verdict

    body, had_prefix = _strip_tel_prefix(text)
    if had_prefix:
        verdict.checks["stripped_tel_prefix"] = True

    body, extension_marker = _normalise_extension_marker(body)
    if extension_marker:
        verdict.checks["extension_marker"] = extension_marker
        verdict.checks["extension_normalised_from"] = text

    import phonenumbers
    from phonenumbers import NumberParseException

    has_international_prefix = body.strip().startswith("+")
    parse_region = None if has_international_prefix else (region or None)

    if not has_international_prefix and not parse_region:
        verdict.state = STATE_NO_COUNTRY_CONTEXT
        verdict.status = STATUS_UNCERTAIN
        verdict.note = (
            "the number has no international prefix and the configuration "
            "provides no country context; the country is not guessed"
        )
        return verdict

    verdict.checks["parsed_with_region"] = parse_region or "(number's own country code)"

    try:
        parsed = phonenumbers.parse(body, parse_region)
    except NumberParseException as exc:
        verdict.state = STATE_PARSE_ERROR
        verdict.status = STATUS_INVALID
        verdict.note = f"libphonenumber could not parse the value: {exc}"
        verdict.checks["parse_error"] = str(exc)
        return verdict

    verdict.extension = parsed.extension or None
    if parsed.country_code:
        verdict.country_code = int(parsed.country_code)

    try:
        verdict.region = phonenumbers.region_code_for_number(parsed) or None
    except Exception:  # noqa: BLE001 - a missing region is not fatal
        verdict.region = None

    if not phonenumbers.is_possible_number(parsed):
        verdict.state = STATE_NOT_A_VALID_NUMBER
        verdict.status = STATUS_INVALID
        verdict.note = (
            "the digit count does not match any numbering plan for "
            f"{verdict.region or parse_region or 'the given context'}"
        )
        return verdict

    if not phonenumbers.is_valid_number(parsed):
        verdict.state = STATE_NOT_A_VALID_NUMBER
        verdict.status = STATUS_INVALID
        verdict.note = (
            "the number is not valid for "
            f"{verdict.region or parse_region or 'the given context'}"
        )
        return verdict

    from phonenumbers import PhoneNumberFormat

    verdict.e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    verdict.national_format = phonenumbers.format_number(
        parsed, PhoneNumberFormat.NATIONAL
    )
    verdict.international_format = phonenumbers.format_number(
        parsed, PhoneNumberFormat.INTERNATIONAL
    )
    try:
        verdict.number_type = _TYPE_NAMES.get(
            int(phonenumbers.number_type(parsed)), "unknown"
        )
    except Exception:  # noqa: BLE001
        verdict.number_type = "unknown"

    if has_international_prefix:
        verdict.state = STATE_E164_VALID
        verdict.status = STATUS_VALID
        verdict.note = (
            f"valid number in {verdict.region or 'its own country'}"
            + (f", extension {verdict.extension}" if verdict.extension else "")
        )
    else:
        # Valid only relative to the configured country: reported as uncertain
        # so a wrong country context can never masquerade as a verified number.
        verdict.state = STATE_NATIONAL_VALID_REGION_CONTEXT
        verdict.status = STATUS_UNCERTAIN
        verdict.note = (
            f"valid in the configured country context ({parse_region}) but the "
            "source wrote no country code, so the country is not confirmed"
            + (f"; extension {verdict.extension}" if verdict.extension else "")
        )
    return verdict
