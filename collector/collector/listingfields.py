"""Reading the fields a *listing row* carries, shared by both channels.

Round 034, first batch, item 2: "实现共用的列表项相对字段抽取：无需详情也保留已有
地址/业务/联系方式；有详情时增补而不是空值覆盖，冲突保留，字段来源准确。配置化预设，
不只写一次性转换脚本。"

What was wrong, measured rather than guessed (by a local audit run during development):
the 顺企网 listing page carries 100 business rows, and each row states the company's
address, product line, founding date and registered capital **inside the row**. The
round-025 offline extraction read them and produced 97 addresses and 98 descriptions out
of 98 rows. The product's ``local_pages`` provider kept only the link text for every row
whose detail page had not been saved — one row out of 98 kept its fields — so 97 rows
reached the matcher with a name and nothing else, and the matcher honestly answered
"待确认" 97 times. The data was never missing from the page; the path dropped it.

The rule this module exists to enforce
--------------------------------------
A value may only be read from **inside the row that names the business**. Never from the
page. That is the whole difference between "the listing says this company is at X" and
"some address on the page is at X", and :meth:`collector.selector.Element.text` already
warns about the second one. Three guards make it hold:

1. the row is the nearest list container around the anchor (``li``/``tr``/configured);
2. a value is the text that follows a **label** (``地址：``) and stops at the next label,
   so a row's fields cannot bleed into each other;
3. a container that holds more than one named detail link is refused outright rather
   than read, because at that point "relative to the row" is no longer true.

Presets, not special cases
--------------------------
:data:`LIST_PRESETS` holds one entry per verified site. A preset is data: labels plus an
optional row selector, so adding a site is a config edit and the generated YAML shows
every label it used. ``generic`` is the fallback and states plainly that it will often
find nothing.

What is deliberately *not* done here
-----------------------------------
* No HTTP, no file reading, no guessing. The caller supplies parsed HTML and states how
  it was obtained.
* No body-text harvesting as a fallback. A row with no labels yields a name and nothing
  else — which is what the page supported.
* Truncation is reported, not repaired: 顺企网 writes ``...`` into its own list rows, and
  a repaired description would be our words instead of the source's.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

from .selector import Element

#: Field name -> label prefixes that introduce it in a directory's list row.
#: Longest first matters: "主营产品" must win over "主营" when both are configured,
#: otherwise the shorter label matches inside the longer one.
DEFAULT_LABELS: dict[str, tuple[str, ...]] = {
    "address": ("公司地址", "所在地", "地址"),
    "description": ("主营产品", "经营范围", "主营业务", "简介"),
    "phone": ("联系电话", "电话号码", "电话", "手机"),
    "email": ("电子邮箱", "电子邮件", "邮箱"),
    "contact": ("联系人", "负责人"),
    "category": ("主营行业", "所属行业", "行业"),
    "est_date": ("成立时间", "成立日期", "注册时间"),
    "reg_capital": ("注册资本", "注册资金"),
}

#: Fields the collector models as first-class record fields. Anything else a row states
#: (founding date, registered capital) is still kept — verbatim, under
#: ``native["listing_fields"]`` — because "the page had it" is not a reason to drop it,
#: but inventing new record fields would change every historical export.
CANONICAL_FIELDS = ("address", "description", "phone", "email", "category")

#: Tags that may be the row container. ``div``/``section`` are included because some
#: directories do not use lists at all; the multi-link guard is what keeps them safe.
ROW_TAGS = ("li", "tr", "div", "section", "article")

#: How far up from the anchor to look for the row container. The page root is never a
#: row: without a bound, a page whose list is not marked up as a list would resolve
#: every anchor to ``body`` and every field would be the page's.
MAX_ROW_DEPTH = 6

#: Trailing markers a directory uses to say "this text was cut short here". Kept in the
#: value (it is what the page said) and reported, so a reader can see it was truncated.
TRUNCATION_MARKS = ("...", "…", "．．．")

#: What a directory writes when it has nothing to say. 顺企网 puts a bare ``-`` after
#: 「地址：」 for a company whose address it does not hold; round 034's audit found 1 of
#: 100 rows like that. Storing the dash as an address would be worse than storing
#: nothing: it is a *value* downstream, it will be exported, and it looks like a
#: statement the source never made. These are matched after stripping, whole value.
PLACEHOLDER_VALUES = frozenset(
    {
        "-", "--", "—", "–", "－", "无", "暂无", "未填", "未填写", "未公开", "未提供",
        "未公布", "未知", "不详", "待补充", "n/a", "na", "null", "none", "/", "",
    }
)

_COLON = "：:"
_SPACE = " \t　"
#: Characters that may sit immediately *before* a real label. A label word found
#: after any other character is part of a value, not a label.
_BOUNDARY = r"\s|,，;；、|·•/>）)》】"


@dataclass
class RowFields:
    """What one listing row stated, field by field, with where it came from."""

    values: dict[str, str] = field(default_factory=dict)
    #: Labels the row actually carried, in document order. Kept so the export can show
    #: the reader the shape of the evidence rather than only its conclusions.
    labels_seen: tuple[str, ...] = ()
    #: Fields whose value the source itself cut short.
    truncated: frozenset[str] = frozenset()
    #: Fields the source left as a dash or "暂无". Not stored as values, but named
    #: here so the reader can tell "the page left this blank" from "we did not look".
    blank: frozenset[str] = frozenset()
    #: True when the row could not be isolated, so nothing above the name was read.
    row_isolated: bool = True
    #: Why the row could not be isolated, when it could not.
    refusal: str = ""

    def get(self, name: str) -> str | None:
        value = self.values.get(name)
        return value or None


def preset_names() -> tuple[str, ...]:
    return tuple(LIST_PRESETS)


def resolve_preset(name: str | None) -> dict:
    """A preset by name, falling back to ``generic``.

    An unknown name is not an error: a config written for a newer version of the
    collector should still run, and ``generic`` says out loud that it will often
    find nothing rather than silently behaving like a verified site.
    """
    key = str(name or "").strip()
    return LIST_PRESETS.get(key) or LIST_PRESETS["generic"]


def preset_labels(name: str | None) -> dict[str, tuple[str, ...]]:
    return dict(resolve_preset(name).get("labels") or DEFAULT_LABELS)


def row_for_anchor(
    anchor: Element,
    *,
    detail_pattern: re.Pattern | None = None,
    row_selector: str | None = None,
) -> Element | None:
    """The list row this anchor is the business link of, or None.

    ``None`` is a normal answer, not a failure: a page whose links are not inside
    per-business rows yields listing-only records with a name, exactly as before.
    """
    if row_selector:
        from .selector import select

        try:
            containers = select(anchor, row_selector)
        except Exception:  # noqa: BLE001 - a bad selector must not kill the run
            containers = []
        for container in containers:
            if _holds_one_named_link(container, anchor, detail_pattern):
                return container
        return None

    for depth, node in enumerate(anchor.ancestors()):
        if depth >= MAX_ROW_DEPTH:
            return None
        if node.tag in ("html", "body"):
            return None
        if node.tag in ROW_TAGS and _holds_one_named_link(node, anchor, detail_pattern):
            return node
    return None


def _holds_one_named_link(
    container: Element, anchor: Element, detail_pattern: re.Pattern | None
) -> bool:
    """True when this container is one business's row and not a wrapper around several.

    The test is the one that matters for field safety: count the links that look like
    business links *and* carry text. A wrapper around three companies has three; a row
    has one (its own name link) plus at most unnamed links such as a logo or a
    "details" icon. Reading a wrapper would put a neighbour's address into this record,
    which is the exact failure this module exists to prevent.
    """
    import urllib.parse

    named = 0
    for element in container.elements():
        if element.tag != "a":
            continue
        href = (element.attr("href") or "").strip()
        text = re.sub(r"\s+", " ", element.text() or "").strip()
        if not text:
            continue
        if detail_pattern is not None:
            # Tested against the path *and* the whole URL: a directory that hosts
            # business pages under ``https://<id>.b2b.example.com`` has an empty
            # path there, so a path-only pattern would count none of them and the
            # guard would refuse rows it is supposed to accept.
            absolute = urllib.parse.urljoin("http://x.invalid/", href)
            path = urllib.parse.urlsplit(absolute).path
            if not (detail_pattern.search(path or "/") or detail_pattern.search(absolute)):
                continue
        named += 1
        if named > 1:
            return False
    return named == 1


def read_listing_row(
    container: Element,
    *,
    labels: dict[str, tuple[str, ...]] | None = None,
) -> RowFields:
    """Read the labelled fields out of one row.

    The row's own text is split at label boundaries; a value is whatever follows its
    label up to the next label. Nothing outside the container is consulted.
    """
    labels = labels or DEFAULT_LABELS
    text = _row_text(container)
    if not text:
        return RowFields(values={}, row_isolated=False, refusal="row has no text")

    hits = _label_hits(text, labels)
    values: dict[str, str] = {}
    order: list[str] = []
    truncated: set[str] = set()
    blank: set[str] = set()
    for index, (start, end, label, field_name) in enumerate(hits):
        stop = hits[index + 1][0] if index + 1 < len(hits) else len(text)
        value = text[end:stop].strip(" \t　,，;；")
        if not value:
            continue
        if _is_placeholder(value):
            blank.add(field_name)
            continue
        if field_name in values:
            # The same label twice in one row: the first is the row's own statement,
            # the second is usually a repeated label in a different block. Keeping the
            # first is the conservative choice; keeping both would need a field the
            # record model does not have.
            continue
        values[field_name] = value
        order.append(label)
        if any(mark in value for mark in TRUNCATION_MARKS):
            truncated.add(field_name)
    return RowFields(
        values=values,
        labels_seen=tuple(order),
        truncated=frozenset(truncated),
        blank=frozenset(blank),
    )


def fields_for_anchor(
    anchor: Element,
    *,
    detail_pattern: re.Pattern | None = None,
    row_selector: str | None = None,
    preset: str | None = None,
) -> RowFields | None:
    """The fields the listing row around ``anchor`` states, or None.

    ``None`` means the anchor is not inside one business's row — a link in the page
    furniture, or a wrapper around several companies. The caller then falls back to
    the old behaviour (a name and nothing else) rather than reading a page-wide
    value that belongs to somebody else.
    """
    row = row_for_anchor(anchor, detail_pattern=detail_pattern, row_selector=row_selector)
    if row is None:
        return None
    return read_listing_row(row, labels=preset_labels(preset))


def as_native(fields: RowFields | None) -> dict | None:
    """The row's own statement, shaped for ``SourceRecord.native``.

    Everything the row said is kept, including the fields the collector does not
    model (成立 time, registered capital) and the ones the source left blank. A
    reader comparing the export against the saved page needs the whole row, not
    only the five fields the record model has.
    """
    if fields is None or not fields.row_isolated:
        return None
    block: dict[str, Any] = {"values": dict(fields.values), "labels": list(fields.labels_seen)}
    if fields.truncated:
        block["truncated"] = sorted(fields.truncated)
    if fields.blank:
        block["blank"] = sorted(fields.blank)
    return block


def _is_placeholder(value: str) -> bool:
    """True when the value is the directory's way of writing "nothing here"."""
    stripped = value.strip(" \t　.,，。;；:：-—~～")
    return stripped.casefold() in PLACEHOLDER_VALUES


def _row_text(container: Element) -> str:
    """The row's text with the business's own link text removed.

    The name is the anchor's text; leaving it in would let a label-less row's name be
    mistaken for a value, and would make ``labels_seen`` claim a label that is not one.
    """
    parts: list[str] = []
    for child in container.children:
        if isinstance(child, str):
            parts.append(child)
        else:
            parts.append(child.text())
    joined = re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()
    return joined


def _label_hits(text: str, labels: dict[str, tuple[str, ...]]) -> list[tuple[int, int, str, str]]:
    """Every label occurrence in ``text``, longest-first, non-overlapping, in order.

    Two rules, both measured against real rows:

    * **Longest-first.** With both "主营产品" and "主营" configured, the short one
      matches inside the long one and would split the row at the wrong place.
    * **A label has to start at a boundary.** Without this, the label word
      occurring *inside a value* is read as another label and the value is cut
      short: a row reading 「地址： 乙地址」 produced the address ``乙``, because the
      second 「地址」 looked like the next field. The same rule also stops "地址"
      from matching inside "联系地址" when both are configured.
    """
    found: list[tuple[int, int, str, str]] = []
    taken: list[tuple[int, int]] = []
    for field_name, prefixes in labels.items():
        for prefix in sorted(prefixes, key=len, reverse=True):
            pattern = (
                r"(?:(?<=^)|(?<=[" + _BOUNDARY + r"]))"
                + re.escape(prefix)
                + r"\s*[" + _COLON + r"]?\s*"
            )
            for match in re.finditer(pattern, text):
                start, end = match.start(), match.end()
                if any(start < other_end and end > other_start for other_start, other_end in taken):
                    continue
                taken.append((start, end))
                found.append((start, end, prefix, field_name))
    found.sort(key=lambda hit: hit[0])
    return found


# --- what a detail page adds to a listing row --------------------------------


@dataclass
class FieldDecision:
    """Where one field's surviving value came from, and what was set aside."""

    field: str
    value: str | None
    source: str  # "detail" | "listing" | "both" | "none"
    #: The other side's value when both stated one and they differ. Kept, not dropped:
    #: "冲突保留" means a reader can see both, not that we picked one quietly.
    conflict: dict[str, str] | None = None


def merge_listing_and_detail(
    detail: dict[str, Any],
    listing: RowFields | dict[str, Any],
    *,
    canonical: Iterable[str] = CANONICAL_FIELDS,
) -> tuple[dict[str, str], dict[str, FieldDecision]]:
    """Combine a detail page's fields with the listing row's.

    Two rules, both from the task book and both measured against the 顺企网 sample:

    * a detail page's value wins when it has one — it is the more specific evidence;
    * a detail page's **absence** never deletes what the listing row already said.
      ``build_detail_record`` used to return ``address=None`` for a page whose selector
      did not match, and that None reached the record as "no address known", erasing the
      listing's.

    When both sides state a value and they differ, both are kept: the detail value
    survives as the record's field, and the listing's is returned in
    :attr:`FieldDecision.conflict` with its source, so nothing is silently discarded.
    """
    listing_values = listing.values if isinstance(listing, RowFields) else dict(listing or {})
    values: dict[str, str] = {}
    decisions: dict[str, FieldDecision] = {}
    for name in canonical:
        from_detail = _clean(detail.get(name))
        from_listing = _clean(listing_values.get(name))
        if from_detail and from_listing:
            same = _same_text(from_detail, from_listing)
            values[name] = from_detail
            decisions[name] = FieldDecision(
                field=name,
                value=from_detail,
                source="both" if same else "detail",
                conflict=None if same else {"listing": from_listing, "detail": from_detail},
            )
        elif from_detail:
            values[name] = from_detail
            decisions[name] = FieldDecision(name, from_detail, "detail")
        elif from_listing:
            values[name] = from_listing
            decisions[name] = FieldDecision(name, from_listing, "listing")
        else:
            decisions[name] = FieldDecision(name, None, "none")
    return values, decisions


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip(" \t　,，;；")
    return text


def _same_text(left: str, right: str) -> bool:
    """Same statement, allowing for the punctuation a directory adds around it."""
    strip = " \t　.,，。;；:：-—()（）"
    return left.strip(strip).casefold() == right.strip(strip).casefold()


# --- presets ----------------------------------------------------------------


#: One entry per site whose list rows were actually read. Every label here was checked
#: against a saved page from that site; ``generic`` is the honest fallback for a site
#: nobody has verified, and it says so in its own note.
LIST_PRESETS: dict[str, dict] = {
    "shunqiwang": {
        "label": "顺企网 11467.com 列表行（025 样本逐条核对过）",
        #: 顺企网的列表项把字段写成「标签：值」的 div/span，地址与主营产品各自一段。
        "labels": {
            "address": ("地址",),
            "description": ("主营产品",),
            "est_date": ("成立时间",),
            "reg_capital": ("注册资本",),
        },
        "note": (
            "标签按 11467.com 列表页写成：每个 <li> 内有「主营产品：」「地址：」"
            "「成立时间：」「注册资本：」四段。该站自己会把描述截断成 ...，"
            "抽取结果原样保留省略号并标记为截断。"
        ),
    },
    "b2b168": {
        "label": "八方资源网 b2b168.com 列表行（未逐条核对，按页面结构推断）",
        "labels": dict(DEFAULT_LABELS),
        "note": (
            "标签用通用集合；八方资源网的列表行若与推断不符，"
            "请在生成 YAML 的 list_fields 里改，不要改代码。"
        ),
    },
    "generic": {
        "label": "其他目录站（通用标签；多数站点取不到值）",
        "labels": dict(DEFAULT_LABELS),
        "note": (
            "通用标签只认中文目录常见的「地址：」「主营产品：」这类写法。"
            "列表行若不是这个形状，就只会留下名称——那是页面真实支持的信息量。"
        ),
    },
}
