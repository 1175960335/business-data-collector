"""Region and industry matching: does this record answer the run's question?

Task book 021 rejects "the collector can fetch data" as a result. What it wants
is a tool that answers, for every record, whether it belongs to the region and
the industry the operator asked for -- and says so with evidence, or says it
cannot tell. Two independent dimensions are judged and then combined:

region
    Decided by the entity address and the administrative information in it. A
    listing page's title, the source's own city, an area code or a bare province
    are *not* evidence: "浙江" is not "杭州". A record in another province or
    city is rejected; an incomplete address, a historical district name or a
    conflict is left for a human. A city-level target does not exclude the
    districts under it; a district-level target is judged at that granularity.

industry
    Decided by what the business says it does -- the products or services on its
    own record -- combined with its name. Neither the name nor the directory's
    own column heading is sufficient on its own, and a mention in a page's
    "related" or navigation block is not evidence at all. The scope is recorded
    too, because "pet-related businesses" and "offline pet stores" are different
    questions: a pet-supplies manufacturer must not be handed over as a store.

Everything here is an explainable rule with a version. There is no opaque score
and no paid model. The Chinese keyword vocabulary for a named profile is
published with the code, is extended from real samples, and travels with the
output through :data:`MATCH_RULE_VERSION`.

The matching is computed at export time from the stored facts. Nothing is
cached: a re-export after new records, a resumed run or an edited criteria file
re-decides every record from the evidence in the database, so an old verdict
can never outlive the data it was made from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import regions
from .addresscheck import parse_address
from .util import utc_now_iso

#: Version of the rules in this module. Written into every export that carries a
#: verdict, so a list can always be traced back to the rule that produced it.
#:
#: ``/2`` (026 P2.6) changed what a *withdrawn* district name does. Under ``/1``
#: any historical name was 待确认 regardless of the target; under ``/2`` the
#: published adjustment decides the geographic attribution, so a company in a
#: renamed Hangzhou district comes back 符合 for a city-level target instead of
#: 待确认. The number moves because the verdicts move: on the two shipped
#: samples that is b2b168 6/2/14 -> 8/0/14 and 顺企网 91/7/0 -> 95/3/0.
MATCH_RULE_VERSION = "cn-region-industry/2"

# --- verdicts -----------------------------------------------------------------

#: Both dimensions match the configured criteria.
STATUS_MATCH = "match"
#: Not enough evidence either way: a human decides.
STATUS_UNKNOWN = "unknown"
#: The record is affirmatively outside the criteria.
STATUS_REJECT = "reject"
#: That dimension was not configured, so it decides nothing.
STATUS_NOT_APPLICABLE = "not_applicable"

#: What a reader sees per verdict, per language. The identifiers above are the
#: machine-readable form; these are the workbook's words.
STATUS_LABELS: dict[str, dict[str, str]] = {
    STATUS_MATCH: {"zh": "符合", "en": "Match", "bilingual": "符合 / Match"},
    STATUS_UNKNOWN: {"zh": "待确认", "en": "Needs review", "bilingual": "待确认 / Needs review"},
    STATUS_REJECT: {"zh": "不符合", "en": "No match", "bilingual": "不符合 / No match"},
    STATUS_NOT_APPLICABLE: {
        "zh": "不适用",
        "en": "Not applicable",
        "bilingual": "不适用 / Not applicable",
    },
}

#: The three groups a classification produces. They are mutually exclusive, and
#: together they hold every classified record -- the export states that sum so
#: nobody has to trust it.
GROUPS: tuple[str, ...] = (STATUS_MATCH, STATUS_UNKNOWN, STATUS_REJECT)


def status_label(status: str, lang: str = "zh") -> str:
    labels = STATUS_LABELS.get(status) or STATUS_LABELS[STATUS_UNKNOWN]
    return labels.get(lang) or labels["zh"]


# --- evidence ----------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One piece of source text that a verdict rests on.

    ``text`` is verbatim: a snippet that has been reworded is not evidence. It
    is clipped for display only, and the clip is marked with an ellipsis so a
    shortened quote cannot read as a complete one.
    """

    field: str
    text: str
    source_url: str = ""
    note: str = ""

    def clipped(self, limit: int = 90) -> str:
        text = str(self.text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit] + "…"


@dataclass(frozen=True)
class DimensionVerdict:
    """The result of judging one dimension of one record."""

    dimension: str
    status: str
    reason: str
    evidence: tuple[Evidence, ...] = ()
    #: What matched, in the source's own words (``杭州市`` / ``宠物用品``).
    matched_value: str = ""
    #: Which rule fired, for the report and the tests.
    rule: str = ""

    @property
    def label(self) -> str:
        return "地区" if self.dimension == "region" else "行业"


@dataclass(frozen=True)
class MatchResult:
    """Both dimensions, combined by the task book's rule.

    Both dimensions matching is the only way into the 符合 group; one explicit
    mismatch is enough for 不符合; everything else is 待确认. A dimension that
    was not configured decides nothing and is reported as such.
    """

    region: DimensionVerdict
    industry: DimensionVerdict
    rule_version: str = MATCH_RULE_VERSION
    judged_at: str = ""

    @property
    def status(self) -> str:
        configured = [v for v in (self.region, self.industry) if v.status != STATUS_NOT_APPLICABLE]
        if not configured:
            return STATUS_NOT_APPLICABLE
        if any(v.status == STATUS_REJECT for v in configured):
            return STATUS_REJECT
        if all(v.status == STATUS_MATCH for v in configured):
            return STATUS_MATCH
        return STATUS_UNKNOWN

    def dimension(self, name: str) -> DimensionVerdict:
        return self.region if name == "region" else self.industry

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rule_version": self.rule_version,
            "judged_at": self.judged_at,
            "region": {
                "status": self.region.status,
                "reason": self.region.reason,
                "matched_value": self.region.matched_value,
                "rule": self.region.rule,
                "evidence": [
                    {"field": e.field, "text": e.text, "source_url": e.source_url, "note": e.note}
                    for e in self.region.evidence
                ],
            },
            "industry": {
                "status": self.industry.status,
                "reason": self.industry.reason,
                "matched_value": self.industry.matched_value,
                "rule": self.industry.rule,
                "evidence": [
                    {"field": e.field, "text": e.text, "source_url": e.source_url, "note": e.note}
                    for e in self.industry.evidence
                ],
            },
        }


# --- criteria -----------------------------------------------------------------


@dataclass(frozen=True)
class RegionCriterion:
    """The region the operator asked for.

    ``district`` empty means a city-level target: the districts under the city
    are not excluded, they are simply not required.
    """

    province: str = ""
    city: str = ""
    district: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.province or self.city or self.district)

    def as_dict(self) -> dict[str, str]:
        return {"province": self.province, "city": self.city, "district": self.district}


@dataclass(frozen=True)
class IndustryCriterion:
    """The industry question, as a named profile plus optional extra words.

    ``profile`` names a published vocabulary (see :data:`INDUSTRY_PROFILES`), so
    the words a verdict rests on are part of the rule rather than a per-run
    secret. ``keywords`` adds operator words of their own; they extend the
    profile, they do not replace it. ``scope`` narrows the question -- an empty
    scope accepts every pet-related business, ``store_service`` asks only for
    offline stores and services.
    """

    profile: str = ""
    scope: str = ""
    keywords: tuple[str, ...] = ()
    #: Words that disqualify a record even when a keyword hits (026 P2.6).
    #: They extend the profile's own list, and theirs overrules a match: an
    #: explicit exclusion is the operator saying "not this trade, however it is
    #: worded", which is a different statement from "I forgot to list it".
    exclude_keywords: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        """Whether this dimension asks a *positive* question.

        Exclusions do not count: an exclusion can only remove a record from a
        group that a profile or a keyword put it in. A criterion that is nothing
        but exclusions would put every record in 待确认, so the config loader
        refuses it rather than running a question that cannot be answered.
        """
        return bool(self.profile or self.keywords)

    def vocabulary(self) -> tuple[str, ...]:
        """The profile's words plus the operator's, deduplicated, in order."""
        base = INDUSTRY_PROFILES[self.profile].keywords if self.profile in INDUSTRY_PROFILES else ()
        out: list[str] = []
        for word in list(base) + list(self.keywords):
            text = str(word).strip()
            if text and text not in out:
                out.append(text)
        return tuple(out)

    def exclusions(self) -> tuple[str, ...]:
        """The profile's exclusion words plus the operator's, deduplicated."""
        base = (
            INDUSTRY_PROFILES[self.profile].exclude_keywords
            if self.profile in INDUSTRY_PROFILES
            else ()
        )
        out: list[str] = []
        for word in list(base) + list(self.exclude_keywords):
            text = str(word).strip()
            if text and text not in out:
                out.append(text)
        return tuple(out)

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "scope": self.scope,
            "keywords": list(self.keywords),
            "exclude_keywords": list(self.exclude_keywords),
            "profile_status": industry_profile_status(self.profile),
        }


@dataclass(frozen=True)
class MatchCriteria:
    region: RegionCriterion = RegionCriterion()
    industry: IndustryCriterion = IndustryCriterion()

    @property
    def configured(self) -> bool:
        return self.region.configured or self.industry.configured

    def as_dict(self) -> dict[str, Any]:
        return {"region": self.region.as_dict(), "industry": self.industry.as_dict()}

    def describe(self, lang: str = "zh") -> str:
        """One line a reader can check the verdicts against.

        026 P2.6: the industry part carries whether the vocabulary behind it was
        verified, so a workbook decided by an unchecked word list says so on the
        page that states the conditions — and a profile name this build does not
        know reads as an unverified rule rather than as a silent custom question.

        Both languages are produced here so the criteria line in an English
        workbook is readable English rather than a Chinese sentence dropped into
        an English sheet.
        """
        english = lang == "en"
        parts: list[str] = []
        if self.region.configured:
            joined = "".join(part for part in (
                self.region.province, self.region.city, self.region.district
            ) if part)
            parts.append(("Region=" if english else "地区=") + joined)
        if self.industry.configured:
            profile = INDUSTRY_PROFILES.get(self.industry.profile)
            if profile is not None:
                name = profile.label_en if english else profile.label_zh
            elif self.industry.profile:
                # An unknown profile name is not "custom keywords": the operator
                # asked for a named rule and this build does not have it.
                name = (
                    f"unknown industry preset {self.industry.profile}"
                    if english
                    else f"未知行业预设 {self.industry.profile}"
                )
            else:
                name = "custom keywords" if english else "自定义关键词"
            if self.industry.scope:
                scope = scope_label(self.industry.scope, lang)
                name += f" ({scope} only)" if english else f"（仅{scope}）"
            parts.append(("Industry=" if english else "行业=") + name)
            exclusions = self.industry.exclusions()
            if exclusions:
                # The *effective* list, profile words included: a verdict an
                # exclusion decided is only re-derivable if the sheet names the
                # exclusion, and the profile's own words are just as binding as
                # the operator's (026 P2.6).
                parts.append(
                    ("Exclusions=" if english else "排除词=")
                    + ("; " if english else "、").join(exclusions)
                )
            status = industry_profile_status(self.industry.profile, lang)
            if self.industry.profile:
                parts.append(("Vocabulary: " if english else "行业词表：") + status)
        return ("; " if english else "；").join(parts) or (
            "Not configured" if english else "未配置"
        )


#: The scope labels, in the operator's words. ``products`` covers the makers and
#: sellers of pet goods, ``store_service`` the shops and the services.
SCOPE_LABELS: dict[str, str] = {
    "store_service": "线下门店/服务",
    "products": "用品与食品产品",
    "products_manufacturer": "用品与食品产品（生产）",
    "products_trade": "用品与食品产品（销售/贸易）",
}


def scope_label(scope: str, lang: str = "zh") -> str:
    if not scope:
        return "未判定" if lang != "en" else "undetermined"
    if lang == "en":
        return {
            "store_service": "offline store / service",
            "products": "pet goods",
            "products_manufacturer": "pet goods (manufacturer)",
            "products_trade": "pet goods (sales/trade)",
        }.get(scope, scope)
    return SCOPE_LABELS.get(scope, scope)


# --- industry vocabulary ------------------------------------------------------

#: Markers that mean the text is about a shop or a hands-on service. Used to
#: tell "pet-related businesses" apart from "offline pet stores", which the task
#: book explicitly forbids conflating.
_STORE_MARKERS: tuple[str, ...] = (
    "宠物店", "宠物用品店", "宠物生活馆", "宠物会所", "犬舍", "猫舍",
    "宠物美容", "宠物寄养", "宠物洗护", "洗澡", "美容", "寄养", "婚配",
    "宠物训导", "宠物训练", "宠物摄影", "宠物殡葬", "宠物托运", "诊疗", "出诊",
)

#: Markers of making things.
_MANUFACTURE_MARKERS: tuple[str, ...] = ("制造", "生产", "加工", "厂")

#: Markers of selling things.
_TRADE_MARKERS: tuple[str, ...] = ("贸易", "进出口", "电子商务", "电商", "销售", "批发", "零售")


@dataclass(frozen=True)
class IndustryProfile:
    """A named, published industry vocabulary."""

    name: str
    label_zh: str
    label_en: str
    #: What this profile covers, said plainly so the reader can check it.
    scope_note_zh: str
    scope_note_en: str
    keywords: tuple[str, ...]
    #: When set, only records in this scope can match the profile.
    required_scope: str = ""
    #: Whether this vocabulary was derived from and checked against real
    #: collected samples (round 026 P2.6). ``False`` is not a defect: it means
    #: the word list is a starting point the operator must check, and the report
    #: says which one it is instead of presenting both as equally reliable.
    verified: bool = False
    #: Where the vocabulary came from, in the operator's words. Shown in the
    #: overview so "which list decided this" is answerable from the workbook.
    source_note_zh: str = ""
    source_note_en: str = ""
    #: Words that disqualify a record even when a keyword hits. A profile that
    #: matches 宠物 must not sweep in 宠物食品包装机械; the caller names what to
    #: exclude rather than the matcher guessing.
    exclude_keywords: tuple[str, ...] = ()


#: Tokens that name a business as something other than what the profile means.
#: Shared by every profile because they describe the *trade*, not the industry:
#: a pet shop's supplies are not a packaging-machinery business, whatever the
#: supplies are made of.
GENERIC_EXCLUDE_KEYWORDS: tuple[str, ...] = (
    "包装机械", "食品机械", "机械设备", "自动化设备", "养殖设备",
)

#: The pet vocabulary. Maintained from real samples (b2b168's own sub-category
#: names and the product lines actually collected in round 020) plus the
#: standard pet-trade words.
#:
#: The bare English token ``pet`` is deliberately **absent**: ``PET`` is also a
#: material and fibre abbreviation. Measured on a real row --
#: 杭州祥茂医疗用品有限公司 lists "PET" among its products while its business
#: is medical non-wovens -- so an English ``pet`` match would put a medical
#: supplier in a pet list.
PET_KEYWORDS: tuple[str, ...] = (
    # the word itself, and the trade's compounding forms
    "宠物",
    "猫粮", "狗粮", "犬粮", "宠物食品", "宠物零食", "宠物粮食", "宠物罐头",
    "宠物用品", "宠物玩具", "宠物窝", "宠物垫", "宠物衣服", "宠物服装",
    "狗链", "狗绳", "牵引带", "猫砂", "宠物食具", "宠物香波",
    # health and diagnostics
    "宠物医疗", "宠物医院", "宠物诊所", "兽药", "宠物疫苗", "宠物驱虫",
    "宠物试纸", "宠物保健品",
    # services
    "宠物美容", "宠物寄养", "宠物托运", "宠物训练", "宠物摄影", "宠物殡葬",
    "宠物繁殖", "宠物婚配",
    # live animals and adjacent trades
    "宠物活体", "宠物犬", "宠物猫", "幼犬", "水族用品", "鱼食", "鸟食",
)

INDUSTRY_PROFILES: dict[str, IndustryProfile] = {
    "pet_related": IndustryProfile(
        name="pet_related",
        label_zh="宠物相关企业",
        label_en="Pet-related businesses",
        scope_note_zh=(
            "经营内容涉及宠物的企业：用品/食品/医疗检测等产品的生产、销售与进出口，"
            "以及线下宠物店与服务。不含仅在名称里出现宠物、或仅在页面推荐与导航里"
            "被提及的企业。"
        ),
        scope_note_en=(
            "Businesses whose own record shows pet activity: making, selling or "
            "trading pet goods, food and veterinary products, and offline pet "
            "stores and services."
        ),
        keywords=PET_KEYWORDS,
        verified=True,
        source_note_zh=(
            "词表取自 b2b168 自身子类目名与 020 轮实采到的产品线，并在真实样本上核对过"
            "（例：英文 pet 因同时是材料缩写而被排除）。"
        ),
        source_note_en=(
            "Built from b2b168's own sub-category names and the product lines "
            "actually collected in round 020, then checked on real rows."
        ),
        exclude_keywords=GENERIC_EXCLUDE_KEYWORDS,
    ),
    "pet_store": IndustryProfile(
        name="pet_store",
        label_zh="线下宠物店与服务",
        label_en="Offline pet stores and services",
        scope_note_zh=(
            "只收线下门店与服务（销售活体、美容、寄养、洗护、训导等）。"
            "宠物用品/食品的生产厂家与贸易商不算，即使名称含宠物。"
        ),
        scope_note_en=(
            "Offline stores and services only (live-animal sales, grooming, "
            "boarding, training). Manufacturers and traders of pet goods do not "
            "qualify even when their name says 宠物."
        ),
        keywords=PET_KEYWORDS,
        required_scope="store_service",
        verified=True,
        source_note_zh="同 pet_related 的词表，另用 required_scope 限定只收线下门店与服务。",
        source_note_en="Same vocabulary as pet_related, narrowed by required_scope.",
        exclude_keywords=GENERIC_EXCLUDE_KEYWORDS,
    ),
    # --- unverified starting points (026 P2.6) ------------------------------
    #
    # These exist so the matcher is not a Hangzhou-pet special case: an operator
    # can ask a different industry question with a vocabulary that is spelled
    # out, and the report states which lists have been checked against real data.
    # They are deliberately marked verified=False — the words are a reasonable
    # starting point, not a validated instrument, and the overview says so.
    "food_service": IndustryProfile(
        name="food_service",
        label_zh="餐饮服务",
        label_en="Food and beverage service",
        scope_note_zh=(
            "提供餐饮服务的企业：餐厅、饭馆、快餐、小吃、面馆、火锅、烧烤、"
            "冷饮甜品、咖啡、茶饮、团餐与外卖配送。不含食品生产与批发贸易。"
        ),
        scope_note_en=(
            "Businesses serving food and drink: restaurants, fast food, snack "
            "shops, noodle houses, hotpot, barbecue, desserts, cafés, tea shops, "
            "catering. Food manufacturing and wholesale are excluded."
        ),
        keywords=(
            "餐饮", "餐厅", "饭馆", "饭店", "酒楼", "酒店餐饮", "快餐", "小吃",
            "面馆", "米粉", "火锅", "烧烤", "西餐", "中餐", "料理", "自助餐",
            "冷饮", "甜品", "奶茶", "茶饮", "咖啡", "咖啡厅", "烘焙", "糕点",
            "团餐", "配餐", "中央厨房", "外卖", "食品经营许可",
        ),
        exclude_keywords=("食品机械", "餐饮设备", "厨具", "包装机械") + GENERIC_EXCLUDE_KEYWORDS,
        verified=False,
        source_note_zh=(
            "词表按餐饮行业常用业态名整理，**尚未用真实样本核对**；"
            "用于实际任务前请先在自己的名单上抽检。"
        ),
        source_note_en=(
            "Compiled from common trade names, **not yet checked against real "
            "samples**. Spot-check on your own list before relying on it."
        ),
    ),
    "building_materials": IndustryProfile(
        name="building_materials",
        label_zh="建材五金",
        label_en="Building materials and hardware",
        scope_note_zh=(
            "建材与五金的生产、销售与批发：水泥、混凝土、砂石、砖瓦、钢材、"
            "管材、门窗、板材、防水保温、涂料、卫浴、灯具、五金件、"
            "电线电缆、装饰装修材料。不含纯工程施工与设计服务。"
        ),
        scope_note_en=(
            "Making, selling and wholesaling building materials and hardware: "
            "cement, concrete, aggregates, bricks, steel, pipes, doors and "
            "windows, panels, waterproofing, coatings, sanitary ware, lighting, "
            "hardware, wiring. Construction services and design are excluded."
        ),
        keywords=(
            "建材", "建筑材料", "水泥", "混凝土", "砂浆", "砂石", "骨料", "砖",
            "砖瓦", "砌块", "钢材", "型材", "钢筋", "管材", "管件", "阀门",
            "门窗", "幕墙", "板材", "石膏板", "石材", "瓷砖", "地板", "防水",
            "保温", "隔热", "涂料", "油漆", "玻璃", "卫浴", "洁具", "灯具",
            "照明", "五金", "紧固件", "电线电缆", "装潢材料", "装饰材料",
        ),
        exclude_keywords=("工程施工", "建筑工程", "装饰工程", "设计院") + GENERIC_EXCLUDE_KEYWORDS,
        verified=False,
        source_note_zh=(
            "词表按建材行业常用品类名整理，**尚未用真实样本核对**；"
            "用于实际任务前请先在自己的名单上抽检。"
        ),
        source_note_en=(
            "Compiled from common product-category names, **not yet checked "
            "against real samples**."
        ),
    ),
}


def industry_profile_status(profile: str, lang: str = "zh") -> str:
    """Whether a profile's vocabulary has been checked against real samples.

    026 P2.6 asks for an explicit "unverified rule" state. Three answers, kept
    apart on purpose:

    * no profile named — nothing to verify, the operator's own words decide;
    * a name this build does not know — the *rule* is unverified, and saying so
      is the only honest answer: the words still work, but nothing vouches for
      them and neither does any sample;
    * a known profile — says whether it was built from real data.

    Both languages are produced here rather than at each call site, so the
    screen and the workbook cannot say different things about the same rule.
    """
    english = lang == "en"
    name = str(profile or "").strip()
    if not name:
        return "No industry preset in use" if english else "未启用行业预设"
    entry = INDUSTRY_PROFILES.get(name)
    if entry is None:
        if english:
            return (
                f"Unverified rule: this build does not know the industry preset "
                f"{name!r}; only your own keywords apply, and no vocabulary "
                f"vouches for the result"
            )
        return (
            f"未验证规则：本版本不认识行业预设 {name!r}，"
            "已按「只用你给的关键词」处理，结果不带有任何词表验证"
        )
    label = entry.label_en if english else entry.label_zh
    if entry.verified:
        return (
            f"{label}: vocabulary verified against real samples"
            if english
            else f"{label}：词表已用真实样本验证"
        )
    return (
        f"{label}: vocabulary NOT verified (not checked against real samples; "
        f"spot-check your own list)"
        if english
        else f"{label}：词表尚未验证（未用真实样本核对，请自行抽检）"
    )


def industry_profile_note(profile: str, lang: str = "zh") -> str:
    entry = INDUSTRY_PROFILES.get(profile)
    if entry is None:
        return ""
    return entry.scope_note_zh if lang != "en" else entry.scope_note_en


def industry_profile_label(profile: str, lang: str = "zh") -> str:
    entry = INDUSTRY_PROFILES.get(profile)
    if entry is None:
        return profile
    return entry.label_zh if lang != "en" else entry.label_en


# --- region reference data ---------------------------------------------------

#: Current divisions of the cities a Chinese run is likely to target, checked
#: against the public administrative adjustments and against the division
#: dataset's city list. The dataset itself stops at city level, so a division
#: name is checked against this hand-checked table rather than guessed from the
#: address string.
#:
#: **County-level cities and counties are listed too**, because they belong to
#: the prefecture just as much as a district does: 建德市, 桐庐县 and 淳安县 are
#: administered by 杭州市 exactly like 西湖区 is. Leaving them out did not make
#: the verdict safer, it made a real Hangzhou company come back as 待确认 with
#: "不在已知区县表中" — so each city's list is its full set of county-level
#: divisions, not just the urban districts.
#:
#: A division that is *not* in the table is still never assumed to belong to the
#: target city: that is a 待确认 with the reason attached, not a rejection.
CITY_DISTRICTS: dict[str, tuple[str, ...]] = {
    # 10 districts + 1 county-level city（建德市）+ 2 counties（桐庐县、淳安县）.
    "杭州": (
        "上城区", "拱墅区", "西湖区", "滨江区", "萧山区", "余杭区",
        "富阳区", "临平区", "临安区", "钱塘区",
        "建德市", "桐庐县", "淳安县",
    ),
    "北京": ("东城区", "西城区", "朝阳区", "丰台区", "石景山区", "海淀区",
             "门头沟区", "房山区", "通州区", "顺义区", "昌平区", "大兴区",
             "怀柔区", "平谷区", "密云区", "延庆区"),
    "上海": ("黄浦区", "徐汇区", "长宁区", "静安区", "普陀区", "虹口区",
             "杨浦区", "闵行区", "宝山区", "嘉定区", "浦东新区", "金山区",
             "松江区", "青浦区", "奉贤区", "崇明区"),
    "广州": ("越秀区", "海珠区", "荔湾区", "天河区", "白云区", "黄埔区",
             "番禺区", "花都区", "南沙区", "从化区", "增城区"),
    "深圳": ("罗湖区", "福田区", "南山区", "宝安区", "龙岗区", "盐田区",
             "龙华区", "坪山区", "光明区", "大鹏新区"),
    "成都": ("锦江区", "青羊区", "金牛区", "武侯区", "成华区", "龙泉驿区",
             "青白江区", "新都区", "温江区", "双流区", "郫都区", "新津区",
             "天府新区", "高新区", "简阳市", "都江堰市", "彭州市", "邛崃市",
             "崇州市", "金堂县", "大邑县", "蒲江县"),
    "南京": ("玄武区", "秦淮区", "建邺区", "鼓楼区", "浦口区", "栖霞区",
             "雨花台区", "江宁区", "六合区", "溧水区", "高淳区"),
    "苏州": ("姑苏区", "虎丘区", "吴中区", "相城区", "吴江区", "工业园区",
             "常熟市", "张家港市", "昆山市", "太仓市"),
    "宁波": ("海曙区", "江北区", "北仑区", "镇海区", "鄞州区", "奉化区",
             "象山县", "宁海县", "余姚市", "慈溪市"),
    "武汉": ("江岸区", "江汉区", "硚口区", "汉阳区", "武昌区", "青山区",
             "洪山区", "东西湖区", "汉南区", "蔡甸区", "江夏区", "黄陂区",
             "新洲区"),
}


@dataclass(frozen=True)
class HistoricalDivision:
    """A division name that no longer exists, and where its territory went.

    026 P2.6 draws a line between two different questions a withdrawn name
    raises, because treating them as one is how a real company ends up 待确认
    for a renaming:

    * **地理归属** (where the land is) — settled by the published adjustment.
      A company whose address says 江干区 is in 杭州 today, full stop, so a
      city-level target does not need a human for this.
    * **现址新鲜度** (whether the company is still there) — *not* settled by any
      mapping. An old address says where the business was, and a directory row
      can be years old. That is why an old name never silently becomes a pass
      when the target is a *specific* district: only the mapping is known, not
      the present address.

    ``parts`` is the full set of successors the territory was divided into.
    One part means the mapping is complete, and the attribution is therefore
    decidable; more than one means "which district today" genuinely cannot be
    answered from the name alone.
    """

    successor: str
    city: str
    year: int
    note: str
    #: Every successor the territory went to, ``successor`` first. A single
    #: entry means the whole territory followed one path.
    parts: tuple[str, ...] = ()

    @property
    def split(self) -> bool:
        """Whether the territory was divided between more than one successor."""
        return len(self.parts) > 1

    @property
    def successors(self) -> tuple[str, ...]:
        return self.parts or (self.successor,)


#: Withdrawn division names seen in Chinese business directories, with the
#: public adjustment that withdrew them. Zhejiang first: that is where the
#: first domestic source's data lives.
HISTORICAL_DIVISIONS: dict[str, HistoricalDivision] = {
    # Hangzhou, April 2021: three central districts were merged into two, and
    # the Xiasha/Baiyang subdistricts of 江干区 went to the new 钱塘区.
    "江干区": HistoricalDivision(
        "上城区", "杭州", 2021,
        "2021年4月杭州区划调整后撤销，主体并入上城区，下沙/白杨街道划入钱塘区",
        ("上城区", "钱塘区"),
    ),
    "下城区": HistoricalDivision(
        "拱墅区", "杭州", 2021, "2021年4月并入拱墅区", ("拱墅区",)
    ),
    # Cities turned into districts.
    # 萧山市 became 萧山区 in full in 2001; the 大江东 area later went to
    # 钱塘区 (2021), so today's attribution is no longer single-valued.
    "萧山市": HistoricalDivision(
        "萧山区", "杭州", 2001, "2001年撤市设区；大江东区块2021年划入钱塘区",
        ("萧山区", "钱塘区"),
    ),
    "余杭县": HistoricalDivision(
        "余杭区", "杭州", 2001, "2001年撤县设区；2021年余杭区析出临平区",
        ("余杭区", "临平区"),
    ),
    "富阳市": HistoricalDivision(
        "富阳区", "杭州", 2015, "2015年撤市设区", ("富阳区",)
    ),
    "临安市": HistoricalDivision(
        "临安区", "杭州", 2017, "2017年撤市设区", ("临安区",)
    ),
    # Well-known Zhejiang neighbours, so a record from them is readable too.
    "绍兴县": HistoricalDivision(
        "柯桥区", "绍兴", 2013, "2013年撤县设区", ("柯桥区",)
    ),
    "鄞县": HistoricalDivision(
        "鄞州区", "宁波", 2002, "2002年撤县设区", ("鄞州区",)
    ),
}


def is_historical_division(name: str) -> bool:
    return str(name or "").strip() in HISTORICAL_DIVISIONS


def historical_division(name: str) -> HistoricalDivision | None:
    return HISTORICAL_DIVISIONS.get(str(name or "").strip())


def historical_note(name: str) -> str:
    entry = HISTORICAL_DIVISIONS.get(str(name or "").strip())
    if entry is None:
        return ""
    where = "、".join(f"{entry.city}{part}" for part in entry.successors)
    return f"{name}已撤销（{entry.note}，现属{where}）"


# --- name helpers ------------------------------------------------------------


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _same_admin(left: str, right: str) -> bool:
    """Do two written-out administrative names denote the same place?

    ``杭州``, ``杭州市`` and ``杭州市`` all denote Hangzhou; only the
    administrative suffixes are removed, so a place genuinely spelled with a
    suffix-like word is not mangled.
    """
    left_text, right_text = _clean(left), _clean(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    return regions._strip_suffix(left_text) == regions._strip_suffix(right_text)


def _division_names(level: str) -> list[str]:
    rows = regions.CN_PROVINCES if level == "province" else regions.CN_CITIES
    return [str(row.get("name") or "") for row in rows if row.get("name")]


def _title_admin_tokens(title: str) -> list[str]:
    """Division names written into the company name, as the dataset spells them.

    The full spellings are used on purpose: ``桐乡市`` in a name is a place,
    while ``桐乡`` could be part of a word. A name that carries a place is still
    not *evidence* of location -- only a disagreement with the address is worth
    reporting, and it is reported as a conflict, never as a confirmation.
    """
    text = _clean(title)
    if not text:
        return []
    found: list[str] = []
    for name in _division_names("province") + _division_names("city"):
        if name and name in text and name not in found:
            found.append(name)
    return found


def _looks_like_city_name(name: str) -> bool:
    """Is this component itself a city name (``宁波市`` where a district belongs)?"""
    stem = regions._strip_suffix(_clean(name))
    for candidate in _division_names("city"):
        if candidate and regions._strip_suffix(candidate) == stem:
            return True
    return False


def _district_city(district: str) -> str:
    """Which city a known district belongs to, or '' when unknown."""
    name = _clean(district)
    for city, districts in CITY_DISTRICTS.items():
        if name in districts:
            return city
    return ""


# --- region matching ---------------------------------------------------------


@dataclass
class RecordEvidence:
    """What one record offers the matchers, in the record's own words.

    ``address``, ``title``, ``description`` and ``category`` are the raw field
    values; ``source_url`` is where the record was read. Nothing is normalised
    here: the matchers read the source's own text and the evidence it quotes is
    exactly that text.
    """

    title: str = ""
    address: str = ""
    description: str = ""
    category: str = ""
    source_url: str = ""
    #: Where each field was read, when it differs from ``source_url`` (a
    #: listing-row product line read on the listing page, for instance).
    field_urls: dict[str, str] = field(default_factory=dict)
    #: How this record was obtained: "live" | "cache" | "import". Empty when the
    #: source did not say, which is presented as unknown rather than as a visit.
    acquired_via: str = ""
    #: The entity's own page on the origin site, when the source only links to
    #: it. It is a *lead*, not evidence, unless ``detail_fetched`` is True.
    detail_url: str = ""
    detail_fetched: bool = False
    #: The site the data originally came from, when it differs from the
    #: collector's provider name (an export of another site's rows).
    origin: str = ""
    #: When the source page was captured; empty when unknown.
    observed_at: str = ""
    #: A website this run *found* rather than read from the source (round 026
    #: P1), with the basis for the verdict and the candidate page it was taken
    #: from. Kept apart from the listed fields so a discovered value always
    #: carries its own reason: a URL with no stated basis is indistinguishable
    #: from one the source published.
    website: str = ""
    website_note: str = ""
    website_source: str = ""

    def url_for(self, field_name: str) -> str:
        return self.field_urls.get(field_name) or self.source_url


def match_region(
    evidence: RecordEvidence, criterion: RegionCriterion
) -> DimensionVerdict:
    """Judge one record's region against the configured region.

    The order of the checks is the order of the task book: an explicit
    out-of-province or out-of-city record is rejected outright; a historical
    name, an incomplete address or a conflict is left for a human; everything
    else has to agree with the target at the granularity that was asked for.
    """
    if not criterion.configured:
        return DimensionVerdict(
            dimension="region",
            status=STATUS_NOT_APPLICABLE,
            reason="未配置地区条件（match.region 的 province/city/district 均为空）",
            rule="region.not_configured",
        )

    address = _clean(evidence.address)
    address_evidence = Evidence(
        field="address", text=address, source_url=evidence.url_for("address"),
        note="实体地址原文",
    )
    if not address:
        return DimensionVerdict(
            dimension="region",
            status=STATUS_UNKNOWN,
            reason="该记录没有地址，无法判断地区",
            evidence=(address_evidence,),
            rule="region.no_address",
        )

    verdict = parse_address(address, country="CN")
    parts = verdict.components or {}
    if "han" not in (verdict.scripts or ()):  # not a Chinese-script address
        return DimensionVerdict(
            dimension="region",
            status=STATUS_UNKNOWN,
                reason=(
                    f"地址不是中文书写（{address}），本规则版本只判断中国行政区划"
                ),
            evidence=(address_evidence,),
            rule="region.non_chinese_script",
        )

    province = _clean(parts.get("province"))
    city = _clean(parts.get("city"))
    district = _clean(parts.get("district"))

    # --- province -------------------------------------------------------------
    if criterion.province and province and not _same_admin(province, criterion.province):
        return DimensionVerdict(
            dimension="region",
            status=STATUS_REJECT,
            reason=f"地址属{province}，不在目标{criterion.province}",
            evidence=(address_evidence,),
            matched_value=province,
            rule="region.province_mismatch",
        )

    # --- city -----------------------------------------------------------------
    if criterion.city:
        if city and not _same_admin(city, criterion.city):
            return DimensionVerdict(
                dimension="region",
                status=STATUS_REJECT,
                reason=f"地址属{city}，不在目标{criterion.city}",
                evidence=(address_evidence,),
                matched_value=city,
                rule="region.city_mismatch",
            )
        if not city:
            # The address stops at the province (or starts at the road). A bare
            # province is never the target city: "浙江" is not "杭州".
            if district and _district_city(district) and _same_admin(
                _district_city(district), criterion.city
            ):
                pass  # the district names the city; fall through to the checks below
            elif province and criterion.province and _same_admin(province, criterion.province):
                return DimensionVerdict(
                    dimension="region",
                    status=STATUS_UNKNOWN,
                    reason=f"地址只写到{province}，没有写到市，无法确认属{criterion.city}",
                    evidence=(address_evidence,),
                    rule="region.province_only",
                )
            else:
                return DimensionVerdict(
                    dimension="region",
                    status=STATUS_UNKNOWN,
                    reason=f"地址没有写明市（{address}），无法确认属{criterion.city}",
                    evidence=(address_evidence,),
                    rule="region.city_absent",
                )

    # --- district -------------------------------------------------------------
    target_district = criterion.district
    if district and _looks_like_city_name(district) and not _same_admin(
        district, criterion.city
    ):
        # The component after the city is itself a city name: the address
        # carries two prefectures, which is a conflict rather than a location.
        return DimensionVerdict(
            dimension="region",
            status=STATUS_UNKNOWN,
            reason=f"地址同时出现{criterion.city}与{district}，行政区信息冲突",
            evidence=(address_evidence,),
            rule="region.conflicting_cities",
        )

    if district and is_historical_division(district):
        # 026 P2.6: separate where the land is (settled by the published
        # adjustment) from whether the company is still on it (settled by
        # nothing we have). The old blanket 待确认 treated the two as one and
        # pushed a real Hangzhou company into review over a renaming.
        entry = historical_division(district)
        assert entry is not None  # is_historical_division just said so
        note = historical_note(district)
        freshness = "来源地址是旧区名，现址新鲜度未知"
        if not target_district:
            # City-level (or province-level) target: the territory is inside
            # this prefecture whichever part it went to, so the attribution is
            # decided by the mapping and there is nothing for a human to add.
            return _region_match(
                evidence.title, address_evidence,
                f"{criterion.city or criterion.province}{district}", district,
                rule="region.historical_district_in_city",
                note=f"{note}；地理归属按公开区划调整确定，{freshness}",
            )
        if _same_admin(target_district, district):
            # Asking for the withdrawn name itself. The mapping answers it.
            return _region_match(
                evidence.title, address_evidence,
                f"{criterion.city}{district}", district,
                rule="region.historical_district_mapped",
                note=f"{note}；目标即该旧区名，按公开映射确定地理归属，{freshness}",
            )
        in_parts = [p for p in entry.successors if _same_admin(p, target_district)]
        if in_parts and not entry.split:
            return _region_match(
                evidence.title, address_evidence,
                f"{criterion.city}{target_district}", district,
                rule="region.historical_district_mapped",
                note=f"{note}；旧区整建制并入{entry.successor}，地理归属确定，{freshness}",
            )
        if in_parts:
            # Part of the old territory went to the target and part elsewhere:
            # only a human, or a newer address, can say which side this company
            # is on. Conservative 待确认, with the split spelled out.
            return DimensionVerdict(
                dimension="region",
                status=STATUS_UNKNOWN,
                reason=(
                    f"{note}；目标{target_district}只是该旧区的一部分，"
                    f"仅凭区名无法确定属于{target_district}还是"
                    f"{'、'.join(p for p in entry.successors if p not in in_parts)}，需人工确认"
                ),
                evidence=(address_evidence,),
                matched_value=district,
                rule="region.historical_district_split",
            )
        if not entry.split:
            # The whole territory went somewhere else: this is a real "not here",
            # and the mapping is what makes it decidable.
            return DimensionVerdict(
                dimension="region",
                status=STATUS_REJECT,
                reason=f"{note}；整建制并入{entry.successor}，不在目标{criterion.city}{target_district}",
                evidence=(address_evidence,),
                matched_value=district,
                rule="region.historical_district_elsewhere",
            )
        return DimensionVerdict(
            dimension="region",
            status=STATUS_UNKNOWN,
            reason=(
                f"{note}；旧区已拆分且目标{target_district}不在其去向内，"
                f"但来源地址为旧记录，保守待确认"
            ),
            evidence=(address_evidence,),
            matched_value=district,
            rule="region.historical_district_unplaced",
        )

    if target_district:
        if not district:
            return DimensionVerdict(
                dimension="region",
                status=STATUS_UNKNOWN,
                reason=f"地址未写明区县，无法确认属{target_district}",
                evidence=(address_evidence,),
                rule="region.district_absent",
            )
        if _same_admin(district, target_district):
            return _region_match(
                evidence.title, address_evidence, f"{criterion.city}{target_district}", district,
                rule="region.district_match",
                note=f"地址属{target_district}，与目标区县一致",
            )
        return DimensionVerdict(
            dimension="region",
            status=STATUS_REJECT,
            reason=f"地址属{district}，不在目标{target_district}",
            evidence=(address_evidence,),
            matched_value=district,
            rule="region.district_mismatch",
        )

    # City-level target: a district under the city is not excluded, but a
    # district nobody can place is a question, not a pass.
    if district:
        known_city = _district_city(district)
        if criterion.city:
            if known_city and _same_admin(known_city, criterion.city):
                return _region_match(
                    evidence.title, address_evidence, criterion.city, district,
                    rule="region.city_match_with_district",
                    note=f"地址属{district}，位于{criterion.city}内（目标仅市级，不额外排除区县）",
                )
            if not known_city:
                return DimensionVerdict(
                    dimension="region",
                    status=STATUS_UNKNOWN,
                    reason=(
                        f"区县“{district}”不在已知的{criterion.city}辖区清单内，"
                        "无法确认隶属关系"
                    ),
                    evidence=(address_evidence,),
                    matched_value=district,
                    rule="region.district_unplaced",
                )
        elif known_city:
            return _region_match(
                evidence.title, address_evidence, known_city, district,
                rule="region.city_match_with_district",
                note=f"地址属{district}，位于{known_city}内",
            )
        else:
            return DimensionVerdict(
                dimension="region",
                status=STATUS_UNKNOWN,
                reason=f"区县“{district}”无法确认隶属城市",
                evidence=(address_evidence,),
                matched_value=district,
                rule="region.district_unplaced",
            )

    if city and _same_admin(city, criterion.city):
        return _region_match(
            evidence.title, address_evidence, criterion.city, city,
            rule="region.city_match",
            note=f"地址写到{city}，目标仅市级",
        )
    if not criterion.city and province:
        return _region_match(
            evidence.title, address_evidence, criterion.province, province,
            rule="region.province_match",
            note=f"地址属{province}，目标仅省级",
        )
    if not city and not province and district:
        return DimensionVerdict(
            dimension="region",
            status=STATUS_UNKNOWN,
            reason=f"地址只有区县级信息（{address}），无法确认省市",
            evidence=(address_evidence,),
            rule="region.district_only",
        )
    return DimensionVerdict(
        dimension="region",
        status=STATUS_UNKNOWN,
        reason=f"地址行政区信息不足（{address}），无法确认属目标地区",
        evidence=(address_evidence,),
        rule="region.insufficient",
    )


def _region_match(
    title: str,
    evidence: Evidence,
    matched_value: str,
    district: str,
    *,
    rule: str,
    note: str,
) -> DimensionVerdict:
    conflict = _title_admin_conflict(title, evidence.text)
    if conflict:
        return DimensionVerdict(
            dimension="region",
            status=STATUS_UNKNOWN,
            reason=(
                f"地址属{district}，但公司名称中出现{conflict}，"
                "行政区信息不一致，需人工确认实际经营地"
            ),
            evidence=(evidence,),
            rule="region.title_address_conflict",
        )
    return DimensionVerdict(
        dimension="region",
        status=STATUS_MATCH,
        reason=note,
        evidence=(evidence,),
        matched_value=matched_value,
        rule=rule,
    )


def _title_admin_conflict(title: str, address: str) -> str:
    """A division written in the company name that disagrees with the address.

    The address decided the verdict. This is the one cross-check the task book
    permits, and it can only *downgrade* a match to 待确认: a name carrying
    "桐乡市" over a Hangzhou address is either a branch office or stale data,
    and only a human can tell. A name that agrees with the address adds
    nothing -- the name is not evidence of location.

    Two sources of tokens: the division dataset's own spellings (provinces and
    prefecture-level cities), and the ``X市``/``X县`` shape in the name, because
    the dataset stops at prefecture level and county-level cities such as
    桐乡市 are exactly the ones a company name carries. The shape match is
    deliberately narrow and only ever downgrades a verdict, so a name that is
    not a place costs a review, never a wrong pass.
    """
    title_text = _clean(title)
    address_text = _clean(address)
    if not title_text or not address_text:
        return ""
    for token in _title_place_tokens(title_text, address_text):
        return token
    return ""


#: The ``X市`` / ``X县`` shape, for the county-level places the division
#: dataset does not list. Two to five characters, so a whole sentence cannot
#: masquerade as a place name.
_TITLE_PLACE_RE = re.compile(r"[\u4e00-\u9fff]{2,5}(?:市|县|旗|盟)")

#: Words that take the same shape and are not places. A hit on one of these is
#: not evidence of anything, so they are dropped before the comparison.
_TITLE_PLACE_NOT_PLACES = frozenset(
    {
        "都市", "城市", "股市", "楼市", "熊市", "牛市", "集市", "夜市",
        "花市", "菜市", "米市", "书市", "小市", "黑市", "弊市", "县",
    }
)


def _title_place_tokens(title: str, address: str) -> list[str]:
    """Place-shaped tokens in a name that the address does not share."""
    out: list[str] = []
    for token in list(_title_admin_tokens(title)) + [
        match.group(0) for match in _TITLE_PLACE_RE.finditer(title)
    ]:
        if token in out:
            continue
        stem = regions._strip_suffix(token)
        if stem in _TITLE_PLACE_NOT_PLACES or token in _TITLE_PLACE_NOT_PLACES:
            continue
        if token in address or stem in address:
            continue
        out.append(token)
    return out


# --- industry matching -------------------------------------------------------

#: Where a directory's free-text profile keeps the business line. b2b168's
#: company profile reads "欢迎来到…网站，具体地址是…，联系人是…。\n主要经营A; B; C。",
#: so the part after the marker is the business and the part before it repeats
#: the name and the address. Without this split the *name* inside the intro
#: would count as business evidence, which is exactly what the rule forbids.
_BUSINESS_MARKERS: tuple[str, ...] = ("主要经营", "主营产品", "主营业务", "经营范围")

#: Text that means "the source published nothing here". Treated as absent
#: evidence rather than as a business description.
_BLANK_BUSINESS = frozenset({"", "未知", "暂无", "无", "-", "--", "不详", "空"})


def business_content(text: str) -> str:
    """The part of a free-text profile that describes what the business does."""
    value = str(text or "").strip()
    if not value:
        return ""
    for marker in _BUSINESS_MARKERS:
        index = value.find(marker)
        if index < 0:
            continue
        segment = value[index + len(marker):]
        for stop in ("。", "\n", "\r"):
            position = segment.find(stop)
            if position >= 0:
                segment = segment[:position]
        segment = segment.strip(" ：:;；,，、")
        return segment if segment not in _BLANK_BUSINESS else ""
    return "" if value in _BLANK_BUSINESS else value


def detect_scope(title: str, business: str) -> str:
    """What kind of pet business this is, recorded so the reader can see it.

    The task book's example: "宠物相关企业" and "线下宠物店" are different needs,
    and a pet-supplies manufacturer must not be handed over as a store. This is
    a label, not a filter, unless the criterion asks for a scope.
    """
    text = f"{_clean(title)}{_clean(business)}"
    if not text.strip():
        return ""
    if any(marker in text for marker in _STORE_MARKERS):
        return "store_service"
    if any(marker in text for marker in _MANUFACTURE_MARKERS):
        return "products_manufacturer"
    if any(marker in text for marker in _TRADE_MARKERS):
        return "products_trade"
    if any(marker in text for marker in ("用品", "食品", "粮", "零食", "玩具", "窝", "垫", "链", "砂")):
        return "products"
    return ""


def _hits(vocabulary: Sequence[str], *texts: str) -> list[str]:
    out: list[str] = []
    for word in vocabulary:
        if any(word and word in text for text in texts):
            out.append(word)
    return out


def match_industry(
    evidence: RecordEvidence, criterion: IndustryCriterion
) -> DimensionVerdict:
    """Judge one record's industry against the configured industry.

    The evidence ladder, strongest first:

    1. the business's own products/services text -- sufficient on its own;
    2. the source's classification *together with* the name -- the two agree;
    3. the name alone or the column heading alone -- not sufficient, and a
       record that rests on it is 待确认, not a pass.

    A keyword that appears only in a page's recommendation or navigation block
    never reaches this function: only the record's own fields are passed in.
    """
    if not criterion.configured:
        return DimensionVerdict(
            dimension="industry",
            status=STATUS_NOT_APPLICABLE,
            reason="未配置行业条件（match.industry 的 profile/keywords 均为空）",
            rule="industry.not_configured",
        )

    vocabulary = criterion.vocabulary()
    title = _clean(evidence.title)
    raw_description = _clean(evidence.description)
    business = business_content(raw_description)
    category = _clean(evidence.category)
    profile = INDUSTRY_PROFILES.get(criterion.profile)

    title_evidence = Evidence(
        field="title", text=title, source_url=evidence.url_for("title"), note="公司名称原文"
    )
    business_evidence = Evidence(
        field="description",
        text=business or raw_description,
        source_url=evidence.url_for("description"),
        note="来源自述经营内容" + ("（已截取“主要经营”段）" if business and business != raw_description else ""),
    )

    if not vocabulary:
        return DimensionVerdict(
            dimension="industry",
            status=STATUS_UNKNOWN,
            reason="行业条件没有配置任何关键词",
            evidence=(title_evidence,),
            rule="industry.no_keywords",
        )

    name_hits = _hits(vocabulary, title)
    business_hits = _hits(vocabulary, business)
    category_hits = _hits(vocabulary, category)

    # 0. an explicit exclusion overrules everything below (026 P2.6). Checked
    # first, on the business text and on the name, because a record that says
    # 宠物食品包装机械 must not be swept in by the keyword 宠物食品 -- the
    # operator named that trade as out of scope, and a longer keyword must not
    # quietly outvote them.
    exclusions = criterion.exclusions()
    if exclusions:
        excluded_hits = _hits(exclusions, f"{title} {business}")
        if excluded_hits:
            return DimensionVerdict(
                dimension="industry",
                status=STATUS_REJECT,
                reason=(
                    f"命中排除词：{'、'.join(excluded_hits)}，"
                    "按本次任务的排除条件判为不符合"
                ),
                evidence=(title_evidence, business_evidence),
                matched_value="、".join(excluded_hits),
                rule="industry.excluded",
            )

    # The profile's own name, for the reason lines. Falling back to a neutral
    # phrase rather than to 宠物 keeps the matcher from being a pet-only rule
    # with pet-only prose (026 P2.6).
    industry_label = profile.label_zh if profile is not None else "目标行业"

    # 1. the business says what it does, and it is the target industry.
    if business_hits:
        scope = detect_scope(title, business)
        if criterion.scope and scope != criterion.scope:
            return DimensionVerdict(
                dimension="industry",
                status=STATUS_REJECT,
                reason=(
                    f"经营内容与{industry_label}相关（命中：{'、'.join(business_hits)}），"
                    f"但属于{scope_label(scope)}，不符合“仅{scope_label(criterion.scope)}”的口径"
                ),
                evidence=(title_evidence, business_evidence),
                matched_value="、".join(business_hits),
                rule="industry.scope_mismatch",
            )
        return DimensionVerdict(
            dimension="industry",
            status=STATUS_MATCH,
            reason=(
                f"经营内容命中{industry_label}关键词：{'、'.join(business_hits)}"
                f"（{scope_label(scope)}）"
            ),
            evidence=(title_evidence, business_evidence),
            matched_value="、".join(business_hits),
            rule="industry.business_match",
        )

    # 2. the name says pet, the source's own classification agrees.
    if name_hits and category_hits:
        return DimensionVerdict(
            dimension="industry",
            status=STATUS_MATCH,
            reason=(
                f"名称命中{'、'.join(name_hits)}，且来源分类“{category[:40]}”一致"
            ),
            evidence=(title_evidence, Evidence(
                field="category", text=category, source_url=evidence.url_for("category"),
                note="来源分类",
            )),
            matched_value="、".join(name_hits),
            rule="industry.name_plus_category",
        )

    # 3. the business text affirmatively describes other trade.
    if business:
        return DimensionVerdict(
            dimension="industry",
            status=STATUS_REJECT,
            reason=(
                f"经营内容为“{business[:60]}”，未见{industry_label}相关业务"
                + (f"；名称虽含{'、'.join(name_hits)}但正文业务不符" if name_hits else "")
            ),
            evidence=(title_evidence, business_evidence),
            rule="industry.other_business",
        )

    # 4. nothing but the name (or nothing at all): not enough evidence either way.
    if name_hits:
        return DimensionVerdict(
            dimension="industry",
            status=STATUS_UNKNOWN,
            reason=(
                f"名称含{'、'.join(name_hits)}，但来源未提供经营内容"
                "（名称关键词不能独立成为充分证据）"
            ),
            evidence=(title_evidence,),
            matched_value="、".join(name_hits),
            rule="industry.name_only",
        )
    if category and category_hits:
        return DimensionVerdict(
            dimension="industry",
            status=STATUS_UNKNOWN,
            reason="只有来源分类提到行业关键词，缺少经营内容证据",
            evidence=(title_evidence, Evidence(
                field="category", text=category, source_url=evidence.url_for("category"),
                note="来源分类",
            )),
            rule="industry.category_only",
        )
    return DimensionVerdict(
        dimension="industry",
        status=STATUS_UNKNOWN,
        reason="来源未提供经营内容，无法判断行业",
        evidence=(title_evidence, business_evidence),
        rule="industry.no_evidence",
    )


# --- the whole verdict -------------------------------------------------------


def match_record(
    evidence: RecordEvidence, criteria: MatchCriteria, *, judged_at: str = ""
) -> MatchResult:
    """Judge both dimensions of one record and combine them."""
    return MatchResult(
        region=match_region(evidence, criteria.region),
        industry=match_industry(evidence, criteria.industry),
        judged_at=judged_at or utc_now_iso(),
    )


def criteria_from_config(cfg) -> MatchCriteria | None:
    """The criteria a config carries, or None when it carries none.

    A config without a ``match`` section gets no matching at all: an old run
    re-exports to exactly the workbook it produced before, which is what keeps
    the 017/018/019 deliverables stable.
    """
    node = (getattr(cfg, "data", None) or {}).get("match")
    if not isinstance(node, dict) or not node:
        return None
    region_node = node.get("region") if isinstance(node.get("region"), dict) else {}
    industry_node = node.get("industry") if isinstance(node.get("industry"), dict) else {}
    profile_name = _clean(industry_node.get("profile"))
    profile = INDUSTRY_PROFILES.get(profile_name)
    scope = _clean(industry_node.get("scope"))
    if not scope and profile is not None and profile.required_scope:
        # The profile carries its own scope: naming ``pet_store`` *is* asking
        # for the narrow question, and silently deciding the broad one instead
        # is exactly the conflation the task book forbids.
        scope = profile.required_scope
    criteria = MatchCriteria(
        region=RegionCriterion(
            province=_clean(region_node.get("province")),
            city=_clean(region_node.get("city")),
            district=_clean(region_node.get("district")),
        ),
        industry=IndustryCriterion(
            profile=profile_name,
            scope=scope,
            keywords=tuple(
                _clean(word) for word in (industry_node.get("keywords") or []) if _clean(word)
            ),
            exclude_keywords=tuple(
                _clean(word)
                for word in (industry_node.get("exclude_keywords") or [])
                if _clean(word)
            ),
        ),
    )
    return criteria if criteria.configured else None


def group_results(
    results: Iterable[tuple[int, MatchResult]],
) -> dict[str, list[int]]:
    """Split entity ids into the three groups, in the order they arrived."""
    groups: dict[str, list[int]] = {status: [] for status in GROUPS}
    for entity_id, result in results:
        status = result.status
        if status not in groups:
            status = STATUS_UNKNOWN
        groups[status].append(entity_id)
    return groups


def counts_by_group(results: Iterable[MatchResult]) -> dict[str, int]:
    counts = {status: 0 for status in GROUPS}
    for result in results:
        counts[result.status if result.status in counts else STATUS_UNKNOWN] += 1
    return counts


__all__ = [
    "CITY_DISTRICTS",
    "GROUPS",
    "HISTORICAL_DIVISIONS",
    "INDUSTRY_PROFILES",
    "MATCH_RULE_VERSION",
    "PET_KEYWORDS",
    "SCOPE_LABELS",
    "STATUS_LABELS",
    "STATUS_MATCH",
    "STATUS_NOT_APPLICABLE",
    "STATUS_REJECT",
    "STATUS_UNKNOWN",
    "DimensionVerdict",
    "Evidence",
    "HistoricalDivision",
    "IndustryCriterion",
    "IndustryProfile",
    "MatchCriteria",
    "MatchResult",
    "RecordEvidence",
    "RegionCriterion",
    "business_content",
    "counts_by_group",
    "criteria_from_config",
    "detect_scope",
    "group_results",
    "historical_division",
    "historical_note",
    "industry_profile_label",
    "industry_profile_note",
    "is_historical_division",
    "match_industry",
    "match_record",
    "match_region",
    "scope_label",
    "status_label",
]
