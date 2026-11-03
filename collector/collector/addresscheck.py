"""Address handling: keep the raw text, derive components, judge separately.

Task book 004, stage D:

    "地址：保存原文和组件，位置校验与邮政真实性分开；支持非英文字符。"

Three things are kept apart on purpose:

``raw``
    Exactly what the page or listing said, byte for byte. This is what an
    operator checks against the source, so it is never rewritten.

``components``
    A best-effort split into street / house number / postal code / city /
    region / country. Marked ``heuristic``: no address database is consulted, so
    a component may be missing or misplaced. It exists to make filtering and
    comparison possible, not to be authoritative.

``location_validated`` vs ``postal_verified``
    Two different questions. *Location validation* asks whether the address sits
    in the country the run was configured for — a consistency check between the
    record and the search context. *Postal reality* asks whether the address
    actually exists and receives mail — which this collector cannot establish,
    so it is always reported as not verified rather than quietly implied by a
    successful parse.

Non-Latin scripts are preserved. The parser never transliterates, never strips
characters outside Latin ranges and never lower-cases the stored value. It does
flag text that looks like it was already mangled by a bad decode (``MusterstraÃŸe``),
because that is a real defect worth surfacing rather than silently storing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import regions
from .identity import normalize_text

#: Machine-readable states.
STATE_COMPONENTS_PARSED = "components_parsed"
STATE_COMPONENTS_PARTIAL = "components_partial"
STATE_EMPTY = "empty"
STATE_NOT_CHECKED = "not_checked"
#: The value is not an address at all: it is the business's own name written
#: into the address slot (round 048). Wikidata's P6375 for
#: `Q30256951 <https://www.wikidata.org/wiki/Q30256951>`_ is literally
#: ``geomer GmbH``, and a source that puts a name in the address property is
#: stating no address. It is kept as evidence and never used as one.
STATE_NAME_NOT_ADDRESS = "name_not_an_address"

#: Postal-code shapes worth recognising. Deliberately a short list of common
#: forms; anything unmatched leaves ``postal_code`` empty rather than wrong.
_POSTAL_PATTERNS = (
    # US ZIP+4 and ZIP
    re.compile(r"\b(\d{5}-\d{4})\b"),
    re.compile(r"\b(\d{5})\b"),
    # Hyphenated continental forms, e.g. Polish 00-590
    re.compile(r"\b(\d{2,4}-\d{3,4})\b"),
    # UK outward+inward
    re.compile(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?)\s?(\d[A-Z]{2})\b", re.IGNORECASE),
    # Netherlands 1234 AB
    re.compile(r"\b(\d{4})\s?([A-Z]{2})\b"),
    # Generic 4-6 digit continental European code
    re.compile(r"\b(\d{4,6})\b"),
)

#: ISO-3166-1 alpha-2 codes. A bare two-letter part is only read as a country
#: when it is a real code, so a US state abbreviation ("Brooklyn, NY") or any
#: other two-letter token is not mistaken for one — a false positive here would
#: turn into a wrong "address is in the wrong country" verdict.
_ISO_COUNTRY_CODES = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM
    BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX
    CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG
    GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR
    IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV
    LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE
    NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO
    RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF
    TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF
    WS YE YT ZA ZM ZW
    """.split()
)

#: Two-letter codes and a few written-out names seen in real listings.
_COUNTRY_NAMES = {
    "deutschland": "DE",
    "germany": "DE",
    "österreich": "AT",
    "oesterreich": "AT",
    "austria": "AT",
    "schweiz": "CH",
    "switzerland": "CH",
    "suisse": "CH",
    "france": "FR",
    "frankreich": "FR",
    "italia": "IT",
    "italy": "IT",
    "italien": "IT",
    "españa": "ES",
    "espana": "ES",
    "spain": "ES",
    "nederland": "NL",
    "netherlands": "NL",
    "niederlande": "NL",
    "belgië": "BE",
    "belgium": "BE",
    "belgien": "BE",
    "united kingdom": "GB",
    "great britain": "GB",
    "england": "GB",
    "vereinigtes königreich": "GB",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "polska": "PL",
    "poland": "PL",
    "polen": "PL",
    "中国": "CN",
    "中国香港": "HK",
    "中国澳门": "MO",
    "中国台湾": "TW",
    "日本": "JP",
    "대한민국": "KR",
    "россия": "RU",
}

#: Sequences that almost always mean the text was decoded with the wrong
#: encoding before it reached the collector.
_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "Ð", "Ñ")

_WHITESPACE = re.compile(r"\s+")

#: Legal forms a business name can end in. Used only to strip them before
#: comparing a value against a name, never to rewrite a stored value.
#:
#: Longest first, so ``GmbH & Co. KG`` is not reduced to ``GmbH`` and left with
#: ``& co. kg`` still attached.
_LEGAL_FORMS = (
    "gmbh & co. kg",
    "gmbh und co. kg",
    "gmbh & co. kgs",
    "aktiengesellschaft",
    "eingetragener verein",
    "e.v.",
    "e. v.",
    "gmbh",
    "mbh",
    "ag",
    "kg",
    "kgaa",
    "ohg",
    "ug (haftungsbeschränkt)",
    "ug",
    "se",
    "ltd.",
    "ltd",
    "limited",
    "llc",
    "inc.",
    "inc",
    "corp.",
    "corporation",
    "b.v.",
    "n.v.",
    "s.a.r.l.",
    "s.a.",
    "s.l.",
    "s.r.l.",
    "s.p.a.",
    "oü",
    "oy",
    "ab",
    "as",
    "aps",
)

#: Punctuation that separates a legal form from the rest of a name.
_NAME_SEPARATORS = " \t,.;:&+-—–"


def _strip_legal_form(text: str) -> str:
    """``geomer GmbH`` -> ``geomer``. Repeated once, so ``X GmbH & Co. KG`` works."""
    out = _normalize_keep_case(text).casefold()
    for _ in range(2):
        trimmed = out.rstrip(_NAME_SEPARATORS)
        for form in _LEGAL_FORMS:
            if trimmed.endswith(form):
                candidate = trimmed[: -len(form)].rstrip(_NAME_SEPARATORS)
                if candidate:
                    out = candidate
                break
    return out.strip(_NAME_SEPARATORS)


def is_name_shaped(value: object, name: object) -> bool:
    """True when an address-slot value is really the business's own name.

    Round 048 found ``P6375`` (street address) = ``geomer GmbH`` on Wikidata;
    050 found the blind spot in that round's guard. The first version refused to
    judge any value that contained a digit, reasoning that a real street address
    carries a house number. A company whose own *name* contains a number --
    ``Hansa 12 GmbH``, ``Bahn 7 Logistik GmbH`` -- walks straight through that
    gate, and once such a value sits in the address slot the name ends up
    agreeing with itself as the "independent" signal. That is a false
    confirmation, not a missed one, so the digit gate is gone: whether a value
    is *the name* is decided by comparing it with the name, never by whether it
    happens to carry a house number.

    Deliberately strict about what counts as the name:

    * equal after normalisation;
    * equal once a trailing legal form is removed from either side;
    * the name plus a short **digit-free** qualifier -- a branch, a legal-form
      variant. Digits in the remainder mean address content, and a qualifier
      never carries one.

    A value that is the name *and* something else -- ``Müller GmbH, Hauptstr. 1``
    -- keeps its own address content and is left alone, which is also why an
    address with no house number is not treated as a name: only equality (or
    equality modulo a legal form) is.

    What the strictness costs is the business named after its own address
    (``Am Hafen 1 GmbH`` at ``Am Hafen 1``): that address is withheld. The
    alternative -- letting the name stand in for an address -- produces a
    confirmation on no evidence, which is the worse error. The raw text is never
    dropped; the value is classified, and the fact row keeps it as evidence.
    """
    left = _normalize_keep_case("" if value is None else str(value))
    right = _normalize_keep_case("" if name is None else str(name))
    if not left or not right:
        return False

    left_key = normalize_text(left)
    right_key = normalize_text(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True

    stripped_left = _strip_legal_form(left)
    stripped_right = _strip_legal_form(right)
    if not stripped_left or not stripped_right:
        return False
    if stripped_left == stripped_right or stripped_left == right_key or left_key == stripped_right:
        return True

    # A branch or a legal-form variant: the value *begins* with the full name,
    # so what follows is a qualifier rather than a street. The remainder must be
    # short and digit-free -- ``geomer GmbH Niederlassung Hessen`` qualifies,
    # ``geomer GmbH Straße 5`` does not (050: a house number in the value is
    # address content, and the name is not proof of anything on its own).
    if left_key.startswith(right_key):
        remainder = left_key[len(right_key):].strip(_NAME_SEPARATORS)
        if (
            remainder
            and len(remainder.split()) <= 4
            and not any(char.isdigit() for char in remainder)
        ):
            return True
    return False



@dataclass
class AddressVerdict:
    raw: str
    normalized: str = ""
    match_key: str = ""
    state: str = STATE_NOT_CHECKED
    components: dict[str, str] = field(default_factory=dict)
    components_source: str = "heuristic"
    location_validated: bool | None = None
    location_note: str = ""
    postal_verified: bool = False
    postal_note: str = ""
    unicode_ok: bool = True
    has_non_ascii: bool = False
    scripts: tuple[str, ...] = ()
    mojibake_suspected: bool = False
    checks: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "normalized": self.normalized,
            "match_key": self.match_key,
            "state": self.state,
            "components": dict(self.components),
            "components_source": self.components_source,
            "location_validated": self.location_validated,
            "location_note": self.location_note,
            "postal_verified": self.postal_verified,
            "postal_note": self.postal_note,
            "unicode_ok": self.unicode_ok,
            "has_non_ascii": self.has_non_ascii,
            "scripts": list(self.scripts),
            "mojibake_suspected": self.mojibake_suspected,
            "checks": dict(self.checks),
        }


def _normalize_keep_case(value: str) -> str:
    """NFKC + whitespace collapse, case preserved.

    Case is preserved because ``Musterstraße 1`` and ``musterstraße 1`` are the
    same address for matching but only the first is right for display.
    """
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\u00a0", " ")
    return _WHITESPACE.sub(" ", text).strip()


def _script_of(char: str) -> str | None:
    try:
        name = unicodedata.name(char)
    except ValueError:
        return None
    for prefix, label in (
        ("LATIN", "latin"),
        ("GREEK", "greek"),
        ("CYRILLIC", "cyrillic"),
        ("CJK", "han"),
        ("HIRAGANA", "japanese"),
        ("KATAKANA", "japanese"),
        ("HANGUL", "hangul"),
        ("ARABIC", "arabic"),
        ("HEBREW", "hebrew"),
        ("DEVANAGARI", "devanagari"),
        ("THAI", "thai"),
    ):
        if name.startswith(prefix):
            return label
    return None


def _detect_scripts(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for char in text:
        if ord(char) < 128:
            continue
        label = _script_of(char)
        if label and label not in found:
            found.append(label)
    return tuple(found)


def _looks_like_country(part: str) -> str | None:
    text = part.strip().strip(".,")
    if not text:
        return None
    if len(text) == 2 and text.isalpha():
        code = text.upper()
        return code if code in _ISO_COUNTRY_CODES else None
    return _COUNTRY_NAMES.get(text.casefold())


def _find_postal_code(part: str) -> str | None:
    for pattern in _POSTAL_PATTERNS:
        match = pattern.search(part)
        if match:
            groups = [group for group in match.groups() if group]
            return " ".join(groups) if len(groups) > 1 else groups[0]
    return None


# --- Chinese addresses ------------------------------------------------------
#
# A Chinese address is written as one unspaced run — ``浙江省杭州市余杭区五常街道
# 文一西路 969 号`` — so the comma-based heuristic below reads the whole thing as
# a single "street" and loses every level the task book asks to keep
# ("中文地址保留省市区/道路/门牌"). It also loses the house number, and a lost
# house number is how two branches of one company get merged.
#
# Province and city are **looked up** in the frozen 国家统计局 name list that
# ships with the collector (see :mod:`collector.regions`), never pattern-guessed:
# a token is only recorded as a city when it really is one. The remaining levels
# have no closed list, so they are read by their administrative suffix, in
# descending order, each match consuming what it took — which is what stops
# ``五常街道`` being read as a road ending in ``道``. Nothing is invented: a level
# the source did not write is simply absent from the components.

_CN_DISTRICT_RE = re.compile(r"(?P<name>[\u4e00-\u9fff]{1,8}?(?:新区|区|县|旗|市))")
_CN_TOWN_RE = re.compile(r"(?P<name>[\u4e00-\u9fff]{1,8}?(?:街道|镇|乡))")
_CN_ROAD_RE = re.compile(
    r"(?P<name>[\u4e00-\u9fff]{1,12}?(?:大道|大街|公路|路|街|巷|弄|道))"
)
#: ``100号``, ``乙18号``, ``3号楼``, ``2幢``, ``201室``. The 号 form is what the
#: merge branch-guard needs; the rest is kept because it is part of the address.
#: Whitespace is allowed before 号 because sources write ``文三路 100 号``.
_CN_HOUSE_RE = re.compile(
    r"(?P<name>(?:[甲乙丙丁戊己庚辛壬癸]?\d+(?:\s*[-－~]\s*\d+)?\s*号)"
    r"|(?:\d+\s*号楼)"
    r"|(?:\d+\s*[幢栋座])"
    r"|(?:\d+\s*室))"
)

#: Characters that separate levels and carry no information of their own.
_CN_SEPARATORS = " \t,，、;；"

#: A suffix-less city name followed by one of these is a district or a town,
#: not a city: ``朝阳区`` must not be read as 朝阳市 just because that city
#: exists in Liaoning.
_CN_SUB_LEVEL_SUFFIXES = ("区", "县", "市", "旗", "镇", "乡", "街道")

_CN_COUNTRY_PREFIXES = ("中华人民共和国", "中国")

#: The names to try at a position, longest first, so ``内蒙古自治区`` is not
#: read as the shorter name that happens to be its prefix.
_CN_PROVINCE_NAMES = sorted(
    (str(row["name"]) for row in regions.CN_PROVINCES), key=len, reverse=True
)
_CN_CITY_NAMES = sorted(
    (str(row["name"]) for row in regions.CN_CITIES), key=len, reverse=True
)


def _cn_match_name(text: str, names: Sequence[str]) -> str | None:
    """The longest official name ``text`` starts with, or None."""
    for name in names:
        if text.startswith(name):
            return name
    return None


def _cn_match_suffixless(text: str, names: Sequence[str]) -> str | None:
    """A known province/city written without its suffix (``杭州富阳区…``).

    This is a lookup in the same official list, not a guess: ``杭州`` is only
    accepted because ``杭州市`` is in the dataset, and only when what follows is
    *not* a sub-level suffix — ``朝阳区`` must not become the city 朝阳市.
    The value recorded is the text the source wrote.
    """
    for name in names:
        stem = regions._strip_suffix(name)
        if not stem or stem == name or not text.startswith(stem):
            continue
        following = text[len(stem):].lstrip(_CN_SEPARATORS)
        if following.startswith(_CN_SUB_LEVEL_SUFFIXES):
            continue
        return stem
    return None


#: The administrative levels, most significant first. A level is only read when
#: no more significant one matches at the current position: to a plain longest
#: match ``浙江省杭州江干区九环路`` is a road, but it is the province, the city,
#: the district and then the road.
_CN_LEVELS = ("province", "city", "district", "town", "road")

_CN_LEVEL_PATTERNS = {
    "district": _CN_DISTRICT_RE,
    "town": _CN_TOWN_RE,
    "road": _CN_ROAD_RE,
}


def _cn_same_place(left: str, right: str) -> bool:
    """Are two spellings the same place (``杭州市`` vs ``杭州``)?"""
    return left == right or regions._strip_suffix(left) == regions._strip_suffix(right)


def _cn_level_match(rest: str, level: str) -> str | None:
    """The text at the start of ``rest`` that names this level, or None."""
    if level == "province":
        return _cn_match_name(rest, _CN_PROVINCE_NAMES) or _cn_match_suffixless(
            rest, _CN_PROVINCE_NAMES
        )
    if level == "city":
        return _cn_match_name(rest, _CN_CITY_NAMES) or _cn_match_suffixless(
            rest, _CN_CITY_NAMES
        )
    match = _CN_LEVEL_PATTERNS[level].match(rest)
    return match.group("name") if match else None


def _parse_chinese_components(text: str) -> dict[str, str]:
    """The administrative levels of a Chinese address, as written.

    The levels are walked left to right in order of significance rather than
    each pattern being run once over the whole remainder. A Chinese source
    commonly repeats the upper levels (``浙江省杭州江干区`` followed by
    ``浙江 杭州市江干区``), and running the road pattern over that remainder
    either misses the street entirely or swallows the repeated province into it.
    A repeat of a level already recorded is consumed silently; a *different*
    place at the same level ends the walk, and whatever is left is kept verbatim
    as the address detail — the address is never rewritten, only split.
    """
    components: dict[str, str] = {}
    rest = text.strip(_CN_SEPARATORS)

    for prefix in _CN_COUNTRY_PREFIXES:
        if rest.startswith(prefix):
            components["country"] = "CN"
            rest = rest[len(prefix):].strip(_CN_SEPARATORS)
            break

    while rest:
        for level in _CN_LEVELS:
            name = _cn_level_match(rest, level)
            if not name:
                continue
            recorded = components.get(level)
            if recorded is None:
                components[level] = name
                if level == "city" and not name.endswith("市"):
                    # The missing suffix is stated rather than silently
                    # "corrected" into the official name.
                    components["city_suffix_absent"] = "1"
            elif not _cn_same_place(recorded, name):
                # A different place at a level already read: the address has
                # moved on to the detail, so the walk stops here.
                return _cn_finish(components, rest)
            rest = rest[len(name):].strip(_CN_SEPARATORS)
            break
        else:
            break

    return _cn_finish(components, rest)


def _cn_finish(components: dict[str, str], rest: str) -> dict[str, str]:
    """Record the house number, the street and whatever detail is left."""
    house = _CN_HOUSE_RE.search(rest)
    if house:
        components["house_number"] = "".join(house.group("name").split())
        rest = rest[house.end():].strip(_CN_SEPARATORS)

    street = "".join(
        part for part in (components.get("road"), components.get("house_number")) if part
    )
    if street:
        components["street"] = street
    remainder = rest.strip(_CN_SEPARATORS + "号")
    if remainder:
        components["detail"] = remainder
    return components


def parse_address(
    raw: object, *, country: str | None = None, name: str | None = None
) -> AddressVerdict:
    """Split an address into components and judge location separately.

    ``country`` is the ISO-3166 alpha-2 code the run is configured for. It is
    used only for the *location consistency* check; the address is never
    rewritten to match it.

    ``name`` is the business's own name, when the caller knows it. It is used
    for one question only — whether this value is an address at all
    (:func:`is_name_shaped`) — and a name-shaped value is reported as absent
    with no components, so every comparison that reads ``components`` or
    ``match_key`` automatically stops treating it as an address. The raw text is
    still on the verdict: the value is not dropped, it is classified.
    """
    text = "" if raw is None else str(raw)
    verdict = AddressVerdict(raw=text)

    if not text.strip():
        verdict.state = STATE_EMPTY
        verdict.location_note = "empty address"
        verdict.postal_note = "no address to verify"
        return verdict

    verdict.normalized = _normalize_keep_case(text)

    if name and is_name_shaped(verdict.normalized, name):
        verdict.state = STATE_NAME_NOT_ADDRESS
        # ``match_key`` stays empty: it is the key every comparison matches on,
        # and a name-shaped value must never match as an address.
        verdict.checks["not_an_address"] = (
            "该值等于企业名称本身，不是地址：按地址缺失处理，不作为地址依据"
        )
        verdict.checks["name_shaped_value"] = verdict.normalized
        verdict.location_note = "no address to check: the value is the business's name"
        verdict.postal_note = "no address to verify"
        return verdict

    verdict.match_key = normalize_text(text)
    verdict.has_non_ascii = any(ord(char) > 127 for char in verdict.normalized)
    verdict.scripts = _detect_scripts(verdict.normalized)
    verdict.mojibake_suspected = any(marker in verdict.normalized for marker in _MOJIBAKE_MARKERS)

    parts = [part.strip() for part in verdict.normalized.split(",") if part.strip()]
    components: dict[str, str] = {}
    postal_index = -1

    # A Chinese address is one unspaced run, so the comma-based split below
    # would collapse every level into a single "street" and lose the house
    # number. It gets its own parser, driven by the official division names.
    if "han" in verdict.scripts:
        chinese = _parse_chinese_components(verdict.normalized)
        if chinese:
            verdict.components = chinese
            verdict.components_source = "cn_administrative_names"
            verdict.checks["chinese_levels"] = sorted(
                key for key in chinese if key != "city_suffix_absent"
            )
            verdict.state = (
                STATE_COMPONENTS_PARTIAL
                if len(chinese) < 2
                else STATE_COMPONENTS_PARSED
            )
            _judge_location(
                verdict, country, chinese.get("country"), province=chinese.get("province")
            )
            return verdict

    for index, part in enumerate(parts):
        code = _find_postal_code(part)
        if code and "postal_code" not in components:
            components["postal_code"] = code
            postal_index = index
            remainder = _WHITESPACE.sub(" ", part.replace(code, " ")).strip(" ,-")
            if remainder:
                components["city"] = remainder

    # A trailing part that is only a country name/code.
    if parts:
        maybe_country = _looks_like_country(parts[-1])
        if maybe_country:
            components["country"] = maybe_country
            parts = parts[:-1]

    # "Brooklyn, NY 11201": the two-letter part is a state, not the city, and
    # the city is the part before it. Only applied when that part is a real
    # two-letter token and the preceding part carries no digits.
    city_candidate = components.get("city", "")
    if (
        postal_index > 0
        and re.fullmatch(r"[A-Za-z]{2}", city_candidate)
        and city_candidate.upper() not in _ISO_COUNTRY_CODES
    ):
        previous = parts[postal_index - 1] if postal_index - 1 < len(parts) else ""
        if previous and not re.search(r"\d", previous):
            components["region"] = city_candidate.upper()
            components["city"] = previous

    # Street is the part before the postal-code part, when there is one.
    if postal_index > 0:
        street_part = parts[0] if parts else ""
        if street_part:
            components["street"] = street_part
    elif parts and "postal_code" not in components:
        # No postal code found: keep the first part as a street hint only if it
        # carries a house number, otherwise it is more likely a city.
        first = parts[0]
        if re.search(r"\d", first):
            components["street"] = first
        elif first:
            components["city"] = first

    if "street" in components:
        match = re.search(r"^(.*?)[\s,]+(\d+[a-zA-Z]?(?:[-/]\d+[a-zA-Z]?)?)$", components["street"])
        if match:
            components["house_number"] = match.group(2)
            components["street_name"] = match.group(1).strip()

    verdict.components = components
    verdict.state = STATE_COMPONENTS_PARSED if components else STATE_COMPONENTS_PARTIAL
    if not components:
        verdict.checks["parse_note"] = "no component could be identified; the raw value is kept as-is"

    # --- location validation, separate from postal reality -------------------

    _judge_location(verdict, country, components.get("country"))

    verdict.postal_verified = False
    verdict.postal_note = (
        "postal reality is not verified: the collector does not consult a postal "
        "address database or a geocoder, so a well-formed address may still not exist"
    )
    return verdict


def _judge_location(
    verdict: AddressVerdict,
    country: str | None,
    address_country: str | None,
    *,
    province: str | None = None,
) -> None:
    """Decide whether the address agrees with the run's configured country.

    Two independent signals, and neither is a guess:

    * an explicit country in the address (``…, Germany`` / a leading ``中国``);
    * a Chinese province name that is in the official division list, which is
      only meaningful when the run is configured for a Chinese region.

    Postal reality stays a separate question and is always "not verified".
    """
    if address_country and country:
        if address_country.upper() == country.upper():
            verdict.location_validated = True
            verdict.location_note = (
                f"the address names {address_country}, which matches the configured country"
            )
        else:
            verdict.location_validated = False
            verdict.location_note = (
                f"the address names {address_country} but the run is configured for "
                f"{country.upper()}"
            )
    elif address_country:
        verdict.location_validated = None
        verdict.location_note = (
            f"the address names {address_country}; no country was configured to compare against"
        )
    elif province and country and country.upper() in regions.CHINA_REGION_CODES:
        verdict.location_validated = True
        verdict.checks["province_from_division_list"] = province
        verdict.location_note = (
            f"the address names the Chinese province {province}, which is in the "
            f"official division list and matches the configured country ({country.upper()})"
        )
    else:
        verdict.location_validated = None
        verdict.location_note = (
            "no country in the address and no separate country context to compare; "
            "location consistency is left undetermined"
        )

    verdict.postal_verified = False
    verdict.postal_note = (
        "postal reality is not verified: the collector does not consult a postal "
        "address database or a geocoder, so a well-formed address may still not exist"
    )
