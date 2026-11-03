"""Regions: what a configured country means for sources, language and divisions.

Task book 019 A:

    "支持CN、中国、省、市、可选区县和中文行业；兼容现有country/city及历史配置
    指纹、恢复机制。"
    "按地区给出合理来源默认值，用户可手动选已实现的来源。中国默认不能悄悄
    继续用Google/OSM当国内方案，未配置可用来源时明确显示。"
    "行政区数据若复用开源库，查官方仓库许可证、更新日期、来源和地名编码；
    记录版本与覆盖边界。未匹配地区允许明确填写，不猜坐标、不沿用柏林。"

Three separate things live here, because they have three different kinds of
truth:

``COUNTRY_ALIASES``
    "中国" and "CN" are the same country. Resolving the alias is a *typing*
    convenience, not data: the result is the ISO-3166 alpha-2 code the rest of
    the collector already uses.

``REGION_PROFILES``
    A region's *defaults* — which sources make sense and which languages the
    reader-facing export should carry. These are opinions, and they are written
    down as such with the reason attached, so "中国默认用国内目录" is a decision
    the operator can read and override rather than a hidden behaviour.

``CN_DIVISIONS``
    Real administrative-division data, with its licence, version and coverage
    boundary recorded next to it. It is used to *canonicalise* a typed province
    or city (and to report the official code); it is never used to invent a
    coordinate, because it does not carry one.

Coordinates
-----------
The dataset above has no coordinates, so the city centres in
:data:`CITY_CENTRES` are a separate, deliberately small, hand-checked table of
**approximate city centres** — good enough to centre a radius search, not
survey points. A city that is not in the table produces a visible request for
explicit coordinates. It never falls back to another city: collecting Berlin
because "Berlin" was the last default is precisely the failure this rule exists
to prevent.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any, Iterable

# --- country identity -------------------------------------------------------

#: Written-out country names a person actually types, mapped to ISO-3166
#: alpha-2. Only names, never guesses: an unrecognised value is rejected rather
#: than mapped to something plausible.
#:
#: 054: the list now carries a Chinese name for every country the tool has a
#: profile for. Before this, the screen's own placeholder said "填 中国 / CN /
#: Germany 均可" while ``意大利`` was refused -- the locale a user types in
#: decided whether a country resolved. The English and native names are kept so
#: nothing that used to work stops working.
COUNTRY_ALIASES: dict[str, str] = {
    "中国": "CN",
    "中國": "CN",
    "中华人民共和国": "CN",
    "china": "CN",
    "prc": "CN",
    "cn": "CN",
    "中国香港": "HK",
    "香港": "HK",
    "hong kong": "HK",
    "中国澳门": "MO",
    "澳门": "MO",
    "macao": "MO",
    "macau": "MO",
    "中国台湾": "TW",
    "台湾": "TW",
    "taiwan": "TW",
    "德国": "DE",
    "germany": "DE",
    "deutschland": "DE",
    "奥地利": "AT",
    "austria": "AT",
    "österreich": "AT",
    "瑞士": "CH",
    "switzerland": "CH",
    "schweiz": "CH",
    "美国": "US",
    "usa": "US",
    "united states": "US",
    "united states of america": "US",
    "英国": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "法国": "FR",
    "france": "FR",
    "荷兰": "NL",
    "netherlands": "NL",
    "holland": "NL",
    "日本": "JP",
    "japan": "JP",
    "意大利": "IT",
    "italy": "IT",
    "italia": "IT",
    "西班牙": "ES",
    "spain": "ES",
    "españa": "ES",
    "葡萄牙": "PT",
    "portugal": "PT",
    "比利时": "BE",
    "belgium": "BE",
    "加拿大": "CA",
    "canada": "CA",
    "澳大利亚": "AU",
    "australia": "AU",
    "新西兰": "NZ",
    "new zealand": "NZ",
    "新加坡": "SG",
    "singapore": "SG",
    "韩国": "KR",
    "大韩民国": "KR",
    "south korea": "KR",
    "korea": "KR",
    "俄罗斯": "RU",
    "russia": "RU",
    "巴西": "BR",
    "brazil": "BR",
    "印度": "IN",
    "india": "IN",
    "墨西哥": "MX",
    "mexico": "MX",
    "波兰": "PL",
    "poland": "PL",
    "瑞典": "SE",
    "sweden": "SE",
    "挪威": "NO",
    "norway": "NO",
    "丹麦": "DK",
    "denmark": "DK",
    "芬兰": "FI",
    "finland": "FI",
    "爱尔兰": "IE",
    "ireland": "IE",
    "希腊": "GR",
    "greece": "GR",
    "土耳其": "TR",
    "turkey": "TR",
    "阿联酋": "AE",
    "united arab emirates": "AE",
    "以色列": "IL",
    "israel": "IL",
    "南非": "ZA",
    "south africa": "ZA",
    "马来西亚": "MY",
    "malaysia": "MY",
    "泰国": "TH",
    "thailand": "TH",
    "越南": "VN",
    "vietnam": "VN",
    "印度尼西亚": "ID",
    "indonesia": "ID",
    "菲律宾": "PH",
    "philippines": "PH",
    "捷克": "CZ",
    "czechia": "CZ",
    "czech republic": "CZ",
    "匈牙利": "HU",
    "hungary": "HU",
    "罗马尼亚": "RO",
    "romania": "RO",
    "乌克兰": "UA",
    "ukraine": "UA",
}

#: Codes that denote a part of China. Used for the *source* defaults (a domestic
#: directory is the sensible default for all of them) and for the region label.
#: It is not a statement that these are one ISO country — it is the set for
#: which "which sources work here" has the same answer.
CHINA_REGION_CODES = ("CN", "HK", "MO", "TW")

#: Region labels. Hong Kong, Macao and Taiwan are parts of China and are named
#: as such.
REGION_LABELS: dict[str, str] = {
    "CN": "中国",
    "HK": "中国香港",
    "MO": "中国澳门",
    "TW": "中国台湾",
    "DE": "德国",
    "AT": "奥地利",
    "CH": "瑞士",
    "US": "美国",
    "GB": "英国",
    "FR": "法国",
    "NL": "荷兰",
    "JP": "日本",
    "IT": "意大利",
    "ES": "西班牙",
    "PT": "葡萄牙",
    "BE": "比利时",
    "CA": "加拿大",
    "AU": "澳大利亚",
    "NZ": "新西兰",
    "SG": "新加坡",
    "KR": "韩国",
    "RU": "俄罗斯",
    "BR": "巴西",
    "IN": "印度",
    "MX": "墨西哥",
    "PL": "波兰",
    "SE": "瑞典",
    "NO": "挪威",
    "DK": "丹麦",
    "FI": "芬兰",
    "IE": "爱尔兰",
    "GR": "希腊",
    "TR": "土耳其",
    "AE": "阿联酋",
    "IL": "以色列",
    "ZA": "南非",
    "MY": "马来西亚",
    "TH": "泰国",
    "VN": "越南",
    "ID": "印度尼西亚",
    "PH": "菲律宾",
    "CZ": "捷克",
    "HU": "匈牙利",
    "RO": "罗马尼亚",
    "UA": "乌克兰",
}


def normalize_country(value: Any) -> str:
    """The ISO-3166 alpha-2 code for a typed country, or '' when unusable.

    Accepts a code (``cn``), a Chinese name (``中国``) or an English name
    (``China``). Anything else returns '' so the caller can refuse it with the
    original text rather than substitute a country.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if lowered in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[lowered]
    upper = text.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper
    return ""


def region_label(country: str) -> str:
    code = normalize_country(country)
    return REGION_LABELS.get(code, code or "未指定地区")


#: The same regions in English, for the English workbook. These are ordinary
#: exonyms, not claimed official names — the ISO code is always shown next to
#: the label so nothing depends on the wording. China's special administrative
#: regions and Taiwan are named the way the Chinese convention requires.
REGION_LABELS_EN: dict[str, str] = {
    "CN": "China",
    "HK": "Hong Kong, China",
    "MO": "Macao, China",
    "TW": "Taiwan, China",
    "DE": "Germany",
    "AT": "Austria",
    "CH": "Switzerland",
    "US": "United States",
    "GB": "United Kingdom",
    "FR": "France",
    "NL": "Netherlands",
    "JP": "Japan",
    "IT": "Italy",
    "ES": "Spain",
    "PT": "Portugal",
    "BE": "Belgium",
    "CA": "Canada",
    "AU": "Australia",
    "NZ": "New Zealand",
    "SG": "Singapore",
    "KR": "Korea",
    "RU": "Russia",
    "BR": "Brazil",
    "IN": "India",
    "MX": "Mexico",
    "PL": "Poland",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
    "FI": "Finland",
    "IE": "Ireland",
    "GR": "Greece",
    "TR": "Turkey",
    "AE": "United Arab Emirates",
    "IL": "Israel",
    "ZA": "South Africa",
    "MY": "Malaysia",
    "TH": "Thailand",
    "VN": "Vietnam",
    "ID": "Indonesia",
    "PH": "Philippines",
    "CZ": "Czechia",
    "HU": "Hungary",
    "RO": "Romania",
    "UA": "Ukraine",
}


def region_label_en(country: str) -> str:
    code = normalize_country(country)
    return REGION_LABELS_EN.get(code, code or "Unspecified region")


def region_display(country: str, lang: str = "zh") -> str:
    """The region as the reader of ``lang`` should see it, always with the code."""
    code = normalize_country(country) or "?"
    if lang == "en":
        return f"{region_label_en(code)} ({code})"
    if lang == "bilingual":
        return f"{region_label(code)} / {region_label_en(code)} ({code})"
    return f"{region_label(code)}（{code}）"


def is_china_region(country: str) -> bool:
    return normalize_country(country) in CHINA_REGION_CODES


# --- region profiles --------------------------------------------------------


@dataclass(frozen=True)
class RegionProfile:
    """A region's defaults, with the reason written down."""

    country: str
    label: str
    #: Sources that make sense as a *default* here, best first. An empty tuple
    #: means "no usable default": the operator must choose, and the tool says so
    #: instead of falling back to something that does not work.
    default_sources: tuple[str, ...]
    #: Reader-facing export languages for a run in this region.
    default_languages: tuple[str, ...]
    #: One sentence a user can read, explaining the defaults.
    note: str


#: Sources that actually return domestic Chinese businesses today. ``directory``
#: is the only one that is *validated* for China (task book 019 C); the others
#: are listed in the manifest as available-but-not-domestic.
CN_USABLE_SOURCES = ("directory",)

CN_PROFILE = RegionProfile(
    country="CN",
    label="中国",
    default_sources=("directory",),
    default_languages=("zh",),
    note=(
        "中国地区默认使用国内企业目录来源（directory）。Google 地图与 "
        "OpenStreetMap 在中国不是默认方案：它们对国内商户的覆盖不足，"
        "而且需要代理才能访问。未配置可用国内来源时，本工具会明确显示，"
        "不会悄悄改用它。"
    ),
)

FOREIGN_PROFILE = RegionProfile(
    country="*",
    label="海外",
    default_sources=("google_maps", "openstreetmap"),
    default_languages=("en", "bilingual"),
    note=(
        "海外地区默认使用 Google 地图与 OpenStreetMap，导出默认英文加中英双语。"
    ),
)

REGION_PROFILES: dict[str, RegionProfile] = {
    "CN": CN_PROFILE,
    "HK": CN_PROFILE,
    "MO": CN_PROFILE,
    "TW": CN_PROFILE,
}
#: Every other resolved country uses the overseas defaults. Built from the
#: label tables rather than repeated by hand, so "a country that resolves at
#: all gets a profile" is structural: 054 found ``意大利`` was refused at the
#: alias table, and this keeps the next country added from being half-added.
REGION_PROFILES.update(
    {
        code: RegionProfile(
            country=code,
            label=REGION_LABELS.get(code, code),
            default_sources=FOREIGN_PROFILE.default_sources,
            default_languages=FOREIGN_PROFILE.default_languages,
            note=FOREIGN_PROFILE.note,
        )
        for code in REGION_LABELS
        if code not in CHINA_REGION_CODES
    }
)


def profile_for(country: str) -> RegionProfile:
    """The profile for a country, falling back to the generic overseas one."""
    code = normalize_country(country)
    return REGION_PROFILES.get(code, FOREIGN_PROFILE)


def default_sources(country: str) -> tuple[str, ...]:
    return profile_for(country).default_sources


def default_languages(country: str) -> tuple[str, ...]:
    return profile_for(country).default_languages


def source_warnings(country: str, sources: Iterable[str]) -> list[str]:
    """Region/source combinations a user should see rather than discover later.

    Never an error: an operator may have a reason to search China through
    Overpass. But it must not be *silent*, which is what the task book forbids.
    """
    code = normalize_country(country)
    listed = [str(name) for name in sources]
    notes: list[str] = []

    if code in CHINA_REGION_CODES:
        domestic = [name for name in listed if name in CN_USABLE_SOURCES]
        if not domestic:
            notes.append(
                f"{region_label(code)}地区没有配置国内来源（当前：{', '.join(listed) or '无'}）。"
                "Google 地图与 OpenStreetMap 不是国内方案：它们的国内商户覆盖不足，"
                "且需要代理才能访问。"
            )
        else:
            foreign = [name for name in listed if name not in CN_USABLE_SOURCES]
            if foreign:
                notes.append(
                    f"{region_label(code)}地区同时配置了非国内来源（{', '.join(foreign)}）；"
                    "它们的结果会与国内来源合并，来源列会标明每条记录来自哪里。"
                )
    else:
        if listed and all(name == "directory" for name in listed):
            notes.append(
                f"{region_label(code)}地区只配置了企业目录来源；"
                "海外商户通常还需要 Google 地图或 OpenStreetMap 才有覆盖。"
            )
    return notes


# --- administrative divisions (open-source data) ----------------------------

#: The division dataset's provenance, quoted verbatim into the export and the
#: report so the version and the coverage boundary travel with the data.
DIVISION_SOURCE = "modood/Administrative-divisions-of-China"
DIVISION_LICENSE = "WTFPL"
DIVISION_SOURCE_URL = "https://github.com/modood/Administrative-divisions-of-China"
DIVISION_UPSTREAM = "国家统计局 统计用区划代码和城乡划分代码"
DIVISION_DATA_AS_OF = "2023-06-30（发布 2023-09-11）"
DIVISION_COVERAGE_NOTE = (
    "仅中国大陆省级与地级（31 省 / 333 市）；不含中国香港、中国澳门、中国台湾。"
    "上游说明：国家统计局自 2024 年 10 月起不再公开具体区划代码，"
    "该数据集已停止更新。"
)

_DIVISION_FILE = pathlib.Path(__file__).resolve().parent / "data" / "cn_divisions.json"


def _load_divisions() -> dict:
    try:
        return json.loads(_DIVISION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - a missing data file
        return {"provinces": [], "cities": []}


_DIVISIONS = _load_divisions()

CN_PROVINCES: list[dict] = list(_DIVISIONS.get("provinces") or [])
CN_CITIES: list[dict] = list(_DIVISIONS.get("cities") or [])

_PROVINCE_BY_CODE = {str(row["code"]): str(row["name"]) for row in CN_PROVINCES}

# --- county level (034 第三批 3.5) ------------------------------------------
#
# The bundled dataset stops at prefecture-level cities, so a county-level place
# -- 建德市, 桐庐县, 淳安县 -- resolved to nothing and the run could not say
# which city it belongs to. That is not a small gap: 建德 is administered by
# 杭州市, and collecting "建德" without knowing that puts the search in the
# wrong prefecture.
#
# Rather than extend the dataset nationwide by hand (which would be unverifiable
# at this size), this table carries the county-level units of the prefectures
# the project actually ships presets for, transcribed from the Ministry of Civil
# Affairs' published 2023 county-level-and-above code list, with the level
# derived from the code itself under GB/T 2260: ``xx01-xx20`` 市辖区,
# ``xx21-xx80`` 县/自治县, ``xx81-xx99`` 县级市. The derivation is checked by a
# test rather than trusted, and the coverage boundary is stated next to the
# data instead of being implied.
#
# 2026-11-04: the 13 rows below were checked entry by entry against the bundled
# dataset's ``areas.json`` entries for ``3301`` -- same 13 codes, same 13 names,
# no row of ours outside it. That turns "hand-transcribed, trust me" into a
# claim a reader can re-run, and it is why the table is kept rather than dropped
# from the first public release.
COUNTY_LEVEL_SOURCE = "中华人民共和国民政部 2023 年县以上行政区划代码"
COUNTY_LEVEL_SOURCE_URL = "https://www.mca.gov.cn/mzsj/xzqh/2023/202301xzqh.html"
COUNTY_LEVEL_DATA_AS_OF = "2023"
COUNTY_LEVEL_COVERAGE_NOTE = (
    "仅本项目已配预设的地级市下辖县级单位（当前为杭州市 13 个）；"
    "其他地级市下辖县级单位不在表内，会如实返回「未收录」而不是猜一个上级市。"
    "这 13 条已与随包区划数据集的杭州项逐条核对一致（2026-11-04）。"
)

#: code, name, level, and the prefecture city / province it belongs to.
CN_COUNTIES: list[dict] = [
    {"code": "330102", "name": "上城区", "level": "district",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330105", "name": "拱墅区", "level": "district",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330106", "name": "西湖区", "level": "district",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330108", "name": "滨江区", "level": "district",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330109", "name": "萧山区", "level": "district",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330110", "name": "余杭区", "level": "district",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330111", "name": "富阳区", "level": "district",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330112", "name": "临安区", "level": "district",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330113", "name": "临平区", "level": "district",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330114", "name": "钱塘区", "level": "district",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330122", "name": "桐庐县", "level": "county",
     "cityCode": "3301", "provinceCode": "33"},
    {"code": "330127", "name": "淳安县", "level": "county",
     "cityCode": "3301", "provinceCode": "33"},
    # The one the task book named: a 县级市, administered by 杭州市 -- not a
    # district of it and not a prefecture of its own.
    {"code": "330182", "name": "建德市", "level": "county_city",
     "cityCode": "3301", "provinceCode": "33"},
]

#: Names that were divisions and no longer are. They are still typed by people,
#: so they are recognised -- and *not* mapped, because the level changed:
#: 富阳市 was a county-level city and is now 富阳区, a district. Silently
#: answering "富阳区" for "富阳市" would quietly change what kind of place it is.
CN_FORMER_NAMES: list[dict] = [
    {"code": "330103", "name": "下城区", "province_name": "浙江省",
     "note": "2023 年民政部县以上行政区划代码中已无此名（旧代码表有 330103）；"
             "区划已调整，按旧名匹配会错配等级，需人工确认"},
    {"code": "330104", "name": "江干区", "province_name": "浙江省",
     "note": "2023 年民政部县以上行政区划代码中已无此名（旧代码表有 330104）；"
             "区划已调整，按旧名匹配会错配等级，需人工确认"},
    {"code": "330183", "name": "富阳市", "province_name": "浙江省",
     "note": "撤市设区，现为杭州市富阳区（330111）；旧名为县级市，"
             "等级与现行区级不同，需人工确认"},
]

_CITY_BY_CODE = {str(row["code"]): row for row in CN_CITIES}


def county_level_code_to_level(code: str) -> str:
    """The GB/T 2260 level a six-digit county-level code implies.

    ``xx01-xx20`` 市辖区, ``xx21-xx80`` 县（自治县等）, ``xx81-xx99`` 县级市.
    Used to *check* the hand-entered table above rather than to derive it, so a
    transcription error shows up as a failing test instead of as a wrong answer.
    """
    tail = int(code[4:6]) if len(code) == 6 and code.isdigit() else 0
    if 1 <= tail <= 20:
        return "district"
    if 21 <= tail <= 80:
        return "county"
    if 81 <= tail <= 99:
        return "county_city"
    return "unknown"


def _strip_suffix(name: str) -> str:
    """``杭州市`` -> ``杭州``; ``内蒙古自治区`` -> ``内蒙古``.

    Only the administrative suffixes are removed, so a city genuinely named with
    a suffix-like word is not mangled.
    """
    for suffix in (
        "特别行政区", "自治区", "自治州", "地区", "盟",
        "省", "市", "县", "区",
    ):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def lookup_cn_division(text: Any) -> dict | None:
    """Resolve a typed Chinese province, city or county-level name.

    Returns a dict with ``level``, ``code``, ``name`` and ``province_name``, or
    None when the name is not in the dataset. Accepts the name with or without
    its administrative suffix (``杭州`` / ``杭州市``) — and it really is both
    ways round: the stored name carries its suffix, so the caller's stripped
    form has to be compared against the stored name's stripped form, not
    against the stored name with the suffix still on it.

    A None result is not an error: the operator may legitimately want a place
    the frozen 2023 dataset does not carry. The caller says so and asks for
    explicit coordinates rather than substituting a coordinate.

    A **former** name (``下城区``, ``江干区``, ``富阳市``) returns
    ``{"level": "former", ...}`` instead of a place. Those names were real and
    are still typed by people, but mapping them to a division would silently
    change the level -- a county-level city that is now a district of the
    prefecture is not the same answer as the district it became.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    wanted = raw.casefold()
    stripped = _strip_suffix(raw).casefold()

    def matches(stored: str) -> bool:
        bare = _strip_suffix(stored).casefold()
        return wanted in (stored.casefold(), bare) or stripped in (stored.casefold(), bare)

    for row in CN_PROVINCES:
        name = str(row["name"])
        if matches(name):
            return {
                "level": "province",
                "code": str(row["code"]),
                "name": name,
                "province_name": name,
            }

    former = _lookup_former(raw)
    if former is not None:
        return former

    for row in CN_CITIES:
        name = str(row["name"])
        if matches(name):
            province_code = str(row.get("provinceCode") or "")
            return {
                "level": "city",
                "code": str(row["code"]),
                "name": name,
                "province_name": _PROVINCE_BY_CODE.get(province_code, ""),
            }

    for row in CN_COUNTIES:
        name = str(row["name"])
        if matches(name):
            city = _CITY_BY_CODE.get(str(row.get("cityCode") or ""), {})
            return {
                "level": row.get("level") or "county",
                "code": str(row["code"]),
                "name": name,
                "province_name": _PROVINCE_BY_CODE.get(str(row.get("provinceCode") or ""), ""),
                # A county-level place is only meaningful with its prefecture:
                # 建德 is administered by 杭州市, and saying "建德, 浙江" without
                # 杭州 would put a search in the wrong city.
                "city_name": str(city.get("name") or ""),
                "city_code": str(row.get("cityCode") or ""),
            }
    return None


def _lookup_former(raw: str) -> dict | None:
    """Strictly the full, old name -- never its bare stem.

    ``富阳市`` (a county-level city) and ``富阳区`` (the district it became)
    share the stem ``富阳``. Matching a former name on its stem would make the
    current name unresolvable and would answer the wrong level for both, so a
    former name is only recognised when it is typed with its own suffix. A bare
    ``富阳`` falls through to the current division, which is the one that exists.
    """
    wanted = raw.casefold()
    for row in CN_FORMER_NAMES:
        if wanted == str(row["name"]).casefold():
            return {
                "level": "former",
                "code": str(row.get("code") or ""),
                "name": str(row["name"]),
                "province_name": str(row.get("province_name") or ""),
                "note": str(row.get("note") or ""),
            }
    return None


# --- city centres (approximate, hand-checked) -------------------------------

#: Approximate city centres, ``(lat, lon)``, for centring a radius search.
#:
#: **Not** official survey points and **not** from the division dataset (which
#: carries no coordinates). They are rounded city centres taken from public
#: map data, accurate enough to place a search radius and no more. A city that
#: is missing is a question for the user, never a silent substitution.
CITY_CENTRES: dict[tuple[str, str], tuple[float, float]] = {
    # China
    ("北京", "CN"): (39.9042, 116.4074),
    ("beijing", "CN"): (39.9042, 116.4074),
    ("上海", "CN"): (31.2304, 121.4737),
    ("shanghai", "CN"): (31.2304, 121.4737),
    ("广州", "CN"): (23.1291, 113.2644),
    ("guangzhou", "CN"): (23.1291, 113.2644),
    ("深圳", "CN"): (22.5431, 114.0579),
    ("shenzhen", "CN"): (22.5431, 114.0579),
    ("成都", "CN"): (30.5728, 104.0668),
    ("chengdu", "CN"): (30.5728, 104.0668),
    ("杭州", "CN"): (30.2741, 120.1551),
    ("hangzhou", "CN"): (30.2741, 120.1551),
    ("武汉", "CN"): (30.5928, 114.3055),
    ("wuhan", "CN"): (30.5928, 114.3055),
    ("西安", "CN"): (34.3416, 108.9398),
    ("xian", "CN"): (34.3416, 108.9398),
    ("南京", "CN"): (32.0603, 118.7969),
    ("nanjing", "CN"): (32.0603, 118.7969),
    ("重庆", "CN"): (29.5630, 106.5516),
    ("chongqing", "CN"): (29.5630, 106.5516),
    ("天津", "CN"): (39.3434, 117.3616),
    ("tianjin", "CN"): (39.3434, 117.3616),
    ("苏州", "CN"): (31.2989, 120.5853),
    ("suzhou", "CN"): (31.2989, 120.5853),
    ("青岛", "CN"): (36.0671, 120.3826),
    ("qingdao", "CN"): (36.0671, 120.3826),
    ("济南", "CN"): (36.6512, 117.1201),
    ("jinan", "CN"): (36.6512, 117.1201),
    ("宁波", "CN"): (29.8683, 121.5440),
    ("ningbo", "CN"): (29.8683, 121.5440),
    ("长沙", "CN"): (28.2278, 112.9388),
    ("changsha", "CN"): (28.2278, 112.9388),
    ("郑州", "CN"): (34.7466, 113.6254),
    ("zhengzhou", "CN"): (34.7466, 113.6254),
    ("合肥", "CN"): (31.8206, 117.2272),
    ("hefei", "CN"): (31.8206, 117.2272),
    ("福州", "CN"): (26.0745, 119.2965),
    ("fuzhou", "CN"): (26.0745, 119.2965),
    ("厦门", "CN"): (24.4798, 118.0894),
    ("xiamen", "CN"): (24.4798, 118.0894),
    # Germany / Austria / Switzerland
    ("berlin", "DE"): (52.5200, 13.4050),
    ("hamburg", "DE"): (53.5511, 9.9937),
    ("münchen", "DE"): (48.1351, 13.5470),
    ("munich", "DE"): (48.1351, 13.5470),
    ("köln", "DE"): (50.9375, 6.9603),
    ("cologne", "DE"): (50.9375, 6.9603),
    ("frankfurt", "DE"): (50.1109, 8.6821),
    ("frankfurt am main", "DE"): (50.1109, 8.6821),
    ("stuttgart", "DE"): (48.7758, 9.1829),
    ("düsseldorf", "DE"): (51.2277, 6.7735),
    ("dusseldorf", "DE"): (51.2277, 6.7735),
    ("leipzig", "DE"): (51.3397, 12.3731),
    ("dresden", "DE"): (51.0504, 13.7373),
    ("hannover", "DE"): (52.3759, 9.7320),
    ("nürnberg", "DE"): (49.4521, 11.0767),
    ("nuremberg", "DE"): (49.4521, 11.0767),
    ("bremen", "DE"): (53.0793, 8.8017),
    ("wien", "AT"): (48.2082, 16.3738),
    ("vienna", "AT"): (48.2082, 16.3738),
    ("zürich", "CH"): (47.3769, 8.5417),
    ("zurich", "CH"): (47.3769, 8.5417),
    # English-speaking and other
    ("london", "GB"): (51.5074, -0.1278),
    ("manchester", "GB"): (53.4808, -2.2426),
    ("new york", "US"): (40.7128, -74.0060),
    ("los angeles", "US"): (34.0522, -118.2437),
    ("san francisco", "US"): (37.7749, -122.4194),
    ("chicago", "US"): (41.8781, -87.6298),
    ("paris", "FR"): (48.8566, 2.3522),
    ("amsterdam", "NL"): (52.3676, 4.9041),
    ("madrid", "ES"): (40.4168, -3.7038),
    ("barcelona", "ES"): (41.3874, 2.1686),
    ("lisboa", "PT"): (38.7223, -9.1393),
    # Italy -- 054: the country that produced the report, so its main cities are
    # listed under the spelling an Italian source would use, with the English
    # and Chinese names resolving to them through CITY_ALIASES.
    ("roma", "IT"): (41.9028, 12.4964),
    ("milano", "IT"): (45.4642, 9.1900),
    ("torino", "IT"): (45.0703, 7.6869),
    ("firenze", "IT"): (43.7696, 11.2558),
    ("napoli", "IT"): (40.8518, 14.2681),
    ("venezia", "IT"): (45.4408, 12.3155),
    ("bologna", "IT"): (44.4949, 11.3426),
    ("genova", "IT"): (44.4056, 8.9463),
    ("palermo", "IT"): (38.1157, 13.3615),
    ("bari", "IT"): (41.1171, 16.8719),
    ("catania", "IT"): (37.5079, 15.0830),
    ("verona", "IT"): (45.4384, 10.9916),
    ("padova", "IT"): (45.4064, 11.8768),
    ("trieste", "IT"): (45.6495, 13.7768),
    # Canada / Australia
    ("toronto", "CA"): (43.6532, -79.3832),
    ("vancouver", "CA"): (49.2827, -123.1207),
    ("montreal", "CA"): (45.5019, -73.5674),
    ("sydney", "AU"): (-33.8688, 151.2093),
    ("melbourne", "AU"): (-37.8136, 144.9631),
    ("brisbane", "AU"): (-27.4698, 153.0251),
    ("perth", "AU"): (-31.9523, 115.8613),
    ("auckland", "NZ"): (-36.8485, 174.7633),
    ("wellington", "NZ"): (-41.2865, 174.7762),
    # Other major markets, so a country that resolves is not immediately a dead
    # end for its obvious capital.
    ("beijing_capital_en", "CN"): (39.9042, 116.4074),
    ("seoul", "KR"): (37.5665, 126.9780),
    ("busan", "KR"): (35.1796, 129.0756),
    ("moskva", "RU"): (55.7558, 37.6173),
    ("sankt-peterburg", "RU"): (59.9311, 30.3609),
    ("singapore", "SG"): (1.3521, 103.8198),
    ("são paulo", "BR"): (-23.5505, -46.6333),
    ("sao paulo", "BR"): (-23.5505, -46.6333),
    ("rio de janeiro", "BR"): (-22.9068, -43.1729),
    ("mumbai", "IN"): (19.0760, 72.8777),
    ("delhi", "IN"): (28.6139, 77.2090),
    ("new delhi", "IN"): (28.6139, 77.2090),
    ("bengaluru", "IN"): (12.9716, 77.5946),
    ("ciudad de méxico", "MX"): (19.4326, -99.1332),
    ("mexico city", "MX"): (19.4326, -99.1332),
    ("warszawa", "PL"): (52.2297, 21.0122),
    ("warsaw", "PL"): (52.2297, 21.0122),
    ("stockholm", "SE"): (59.3293, 18.0686),
    ("oslo", "NO"): (59.9139, 10.7522),
    ("københavn", "DK"): (55.6761, 12.5683),
    ("copenhagen", "DK"): (55.6761, 12.5683),
    ("helsinki", "FI"): (60.1699, 24.9384),
    ("dublin", "IE"): (53.3498, -6.2603),
    ("athina", "GR"): (37.9838, 23.7275),
    ("athens", "GR"): (37.9838, 23.7275),
    ("istanbul", "TR"): (41.0082, 28.9784),
    ("ankara", "TR"): (39.9334, 32.8597),
    ("dubai", "AE"): (25.2048, 55.2708),
    ("abu dhabi", "AE"): (24.4539, 54.3773),
    ("tel aviv", "IL"): (32.0853, 34.7818),
    ("jerusalem", "IL"): (31.7683, 35.2137),
    ("johannesburg", "ZA"): (-26.2041, 28.0473),
    ("cape town", "ZA"): (-33.9249, 18.4241),
    ("kuala lumpur", "MY"): (3.1390, 101.6869),
    ("bangkok", "TH"): (13.7563, 100.5018),
    ("hanoi", "VN"): (21.0278, 105.8342),
    ("ho chi minh city", "VN"): (10.8231, 106.6297),
    ("jakarta", "ID"): (-6.2088, 106.8456),
    ("manila", "PH"): (14.5995, 120.9842),
    ("praha", "CZ"): (50.0755, 14.4378),
    ("prague", "CZ"): (50.0755, 14.4378),
    ("budapest", "HU"): (47.4979, 19.0402),
    ("bucuresti", "RO"): (44.4268, 26.1025),
    ("bucharest", "RO"): (44.4268, 26.1025),
    ("kyiv", "UA"): (50.4501, 30.5234),
    ("kiev", "UA"): (50.4501, 30.5234),
    ("brussels", "BE"): (50.8503, 4.3517),
    ("bruxelles", "BE"): (50.8503, 4.3517),
    ("rome", "IT"): (41.9028, 12.4964),
    ("tokyo", "JP"): (35.6762, 139.6503),
    ("osaka", "JP"): (34.6937, 135.5023),
}

CITY_CENTRES_NOTE = (
    "城市中心坐标为近似值（公开地图资料取整），仅用于确定搜索半径的中心，"
    "不是官方测绘点。表内没有的城市必须显式填写「纬度,经度」，"
    "本工具不会改用其他城市。"
)

#: A city spelled more than one way, **and the country it belongs to**.
#:
#: 054: a user typed ``米兰`` with country IT and was told the centre was
#: unknown, because the table only carried the Latin spelling of a handful of
#: cities. The fix is not to add one city -- that would move the wall, not
#: remove it -- but to give each city its other names *and* to bind every name
#: to a country, so spelling cannot silently retarget a search.
#:
#: The binding is checked, never guessed: ``Milan`` with country DE does not
#: resolve, because a city is only usable in the country it is in. A name that
#: appears in two countries is written once per country, so the table itself
#: cannot be ambiguous.
#:
#: Coverage is deliberately partial and stated as such: this makes the common
#: cities nameable in the language the operator thinks in, and an unlisted city
#: still produces the explicit "I don't know X" message with the two ways out.
CITY_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    # alias (casefolded)                 country    canonical key in CITY_CENTRES
    ("milan", "IT"): ("milano", "IT"),
    ("米兰", "IT"): ("milano", "IT"),
    ("milan, italy", "IT"): ("milano", "IT"),
    ("rome", "IT"): ("roma", "IT"),
    ("罗马", "IT"): ("roma", "IT"),
    ("turin", "IT"): ("torino", "IT"),
    ("turín", "IT"): ("torino", "IT"),
    ("都灵", "IT"): ("torino", "IT"),
    ("florence", "IT"): ("firenze", "IT"),
    ("佛罗伦萨", "IT"): ("firenze", "IT"),
    ("naples", "IT"): ("napoli", "IT"),
    ("那不勒斯", "IT"): ("napoli", "IT"),
    ("venice", "IT"): ("venezia", "IT"),
    ("威尼斯", "IT"): ("venezia", "IT"),
    ("bologna", "IT"): ("bologna", "IT"),
    ("genoa", "IT"): ("genova", "IT"),
    ("热那亚", "IT"): ("genova", "IT"),
    # Germany: the umlaut-free and Chinese spellings of the existing table
    ("慕尼黑", "DE"): ("münchen", "DE"),
    ("科隆", "DE"): ("köln", "DE"),
    ("法兰克福", "DE"): ("frankfurt", "DE"),
    ("柏林", "DE"): ("berlin", "DE"),
    ("汉堡", "DE"): ("hamburg", "DE"),
    ("杜塞尔多夫", "DE"): ("düsseldorf", "DE"),
    ("莱比锡", "DE"): ("leipzig", "DE"),
    ("德累斯顿", "DE"): ("dresden", "DE"),
    ("汉诺威", "DE"): ("hannover", "DE"),
    ("纽伦堡", "DE"): ("nürnberg", "DE"),
    ("不莱梅", "DE"): ("bremen", "DE"),
    # Austria / Switzerland
    ("维也纳", "AT"): ("wien", "AT"),
    ("苏黎世", "CH"): ("zürich", "CH"),
    # Spain / Portugal
    ("madrid", "ES"): ("madrid", "ES"),
    ("马德里", "ES"): ("madrid", "ES"),
    ("barcelona", "ES"): ("barcelona", "ES"),
    ("巴塞罗那", "ES"): ("barcelona", "ES"),
    ("lisbon", "PT"): ("lisboa", "PT"),
    ("里斯本", "PT"): ("lisboa", "PT"),
    # France / Netherlands
    ("巴黎", "FR"): ("paris", "FR"),
    ("阿姆斯特丹", "NL"): ("amsterdam", "NL"),
    # United States
    ("纽约", "US"): ("new york", "US"),
    ("洛杉矶", "US"): ("los angeles", "US"),
    ("旧金山", "US"): ("san francisco", "US"),
    ("圣弗朗西斯科", "US"): ("san francisco", "US"),
    ("芝加哥", "US"): ("chicago", "US"),
    # United Kingdom
    ("伦敦", "GB"): ("london", "GB"),
    ("曼彻斯特", "GB"): ("manchester", "GB"),
    # Japan
    ("东京", "JP"): ("tokyo", "JP"),
    ("大阪", "JP"): ("osaka", "JP"),
    # Canada / Australia
    ("toronto", "CA"): ("toronto", "CA"),
    ("多伦多", "CA"): ("toronto", "CA"),
    ("vancouver", "CA"): ("vancouver", "CA"),
    ("温哥华", "CA"): ("vancouver", "CA"),
    ("sydney", "AU"): ("sydney", "AU"),
    ("悉尼", "AU"): ("sydney", "AU"),
    ("melbourne", "AU"): ("melbourne", "AU"),
    ("墨尔本", "AU"): ("melbourne", "AU"),
    # Russia / Korea / Singapore
    ("moscow", "RU"): ("moskva", "RU"),
    ("莫斯科", "RU"): ("moskva", "RU"),
    ("seoul", "KR"): ("seoul", "KR"),
    ("首尔", "KR"): ("seoul", "KR"),
    ("汉城", "KR"): ("seoul", "KR"),
    ("新加坡市", "SG"): ("singapore", "SG"),
    # China: the Chinese names for cities the table has in Latin script only,
    # plus the Latin names for the ones it has in Chinese only.
    ("北京", "CN"): ("beijing", "CN"),
    ("上海市", "CN"): ("shanghai", "CN"),
    ("广州市", "CN"): ("guangzhou", "CN"),
    ("深圳市", "CN"): ("shenzhen", "CN"),
    ("成都市", "CN"): ("chengdu", "CN"),
    ("杭州市", "CN"): ("hangzhou", "CN"),
    ("武汉市", "CN"): ("wuhan", "CN"),
    ("西安市", "CN"): ("xian", "CN"),
    ("南京市", "CN"): ("nanjing", "CN"),
    ("重庆市", "CN"): ("chongqing", "CN"),
    ("天津市", "CN"): ("tianjin", "CN"),
    ("苏州市", "CN"): ("suzhou", "CN"),
    ("青岛市", "CN"): ("qingdao", "CN"),
    ("济南市", "CN"): ("jinan", "CN"),
    ("宁波市", "CN"): ("ningbo", "CN"),
    ("长沙市", "CN"): ("changsha", "CN"),
    ("郑州市", "CN"): ("zhengzhou", "CN"),
    ("合肥市", "CN"): ("hefei", "CN"),
    ("福州市", "CN"): ("fuzhou", "CN"),
    ("厦门市", "CN"): ("xiamen", "CN"),
}


def resolve_city(city: str, country: str) -> str:
    """The table spelling of a city in a country, or the typed text unchanged.

    Aliases are pure spelling: ``米兰``/``Milan``/``Milano`` all become
    ``milano`` for country IT and nothing else changes about the run. The typed
    text is what the config and the export keep, so a person's own wording
    survives into the export either way.
    """
    text = str(city or "").strip()
    code = normalize_country(country)
    alias = CITY_ALIASES.get((text.casefold(), code))
    if alias is None:
        return text
    target, target_country = alias
    # A city is only usable in its own country: this keeps ``Milan`` from
    # quietly resolving when the country field still says DE.
    return target if target_country == code else text


def city_centre(city: str, country: str) -> tuple[float, float] | None:
    """The approximate centre of a city, or None when it is not in the table."""
    code = normalize_country(country)
    text = str(city or "").strip()
    for candidate in (resolve_city(text, code), text):
        hit = CITY_CENTRES.get((candidate.casefold(), code))
        if hit is not None:
            return hit
    return None


def region_facts(country: str) -> dict:
    """What the export/report should say about the region and its data."""
    code = normalize_country(country)
    profile = profile_for(code)
    return {
        "country": code,
        "label": region_label(code),
        "default_sources": list(profile.default_sources),
        "default_languages": list(profile.default_languages),
        "note": profile.note,
        "division_source": DIVISION_SOURCE,
        "division_license": DIVISION_LICENSE,
        "division_source_url": DIVISION_SOURCE_URL,
        "division_upstream": DIVISION_UPSTREAM,
        "division_data_as_of": DIVISION_DATA_AS_OF,
        "division_coverage_note": DIVISION_COVERAGE_NOTE,
        "city_centre_note": CITY_CENTRES_NOTE,
    }


__all__ = [
    "CHINA_REGION_CODES",
    "CITY_CENTRES",
    "CITY_CENTRES_NOTE",
    "CN_CITIES",
    "CN_PROVINCES",
    "CN_USABLE_SOURCES",
    "COUNTRY_ALIASES",
    "DIVISION_COVERAGE_NOTE",
    "DIVISION_DATA_AS_OF",
    "DIVISION_LICENSE",
    "DIVISION_SOURCE",
    "DIVISION_SOURCE_URL",
    "DIVISION_UPSTREAM",
    "REGION_LABELS",
    "REGION_PROFILES",
    "RegionProfile",
    "city_centre",
    "default_languages",
    "default_sources",
    "is_china_region",
    "lookup_cn_division",
    "normalize_country",
    "profile_for",
    "region_facts",
    "region_label",
    "source_warnings",
]
