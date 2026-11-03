"""Basic export: SQLite -> CSV / JSONL.

Stage C only needs "基础导出"; the richer XLSX deliverable with per-field
evidence sheets belongs to stage F. This module therefore writes:

* ``entities.csv`` / ``entities.jsonl`` — one row per entity, columns chosen by
  ``export.fields``;
* ``field_facts.jsonl`` — every fact, including superseded values, so nothing is
  hidden behind the "latest value" view;
* ``errors.jsonl`` — structured failures, so a partial run is explainable.

CSV is written as UTF-8 with a BOM so Excel on Windows opens non-ASCII names
correctly; JSONL is plain UTF-8.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Iterator

from . import db, matching, regions, scoring
from .config import COLLECT_FIELDS, CONTEXT_COLUMNS, SCORE_COLUMNS
from .errors import ExportError
from .ingest import is_absent_default
from .sources import manifest
from .util import utc_now_iso

#: Separator used when a field has several values for one entity (emails).
MULTI_VALUE_SEPARATOR = " | "

#: File name of the reader-facing bilingual workbook. The technical workbook
#: (``entities.xlsx``) keeps its name and its four sheets; this one is added
#: next to it, so a reader who does not want evidence sheets has one obvious
#: file to open.
#:
#: Stage 019 adds ``entities_zh.xlsx`` and ``entities_en.xlsx`` beside it. The
#: bilingual file keeps this exact name *and its exact content*, so a workbook
#: a client already has does not change under them.
BILINGUAL_FILENAME = "entities_bilingual.xlsx"

#: What the main table shows where a source did not provide a value. Both
#: languages, because the two readers of this file are the user and the client.
NOT_AVAILABLE = "未获取 / Not available"

#: Beijing time (UTC+8). The task book asks for a stated timezone rather than a
#: bare timestamp, so every time in the reader-facing workbook is rendered in
#: this zone and the header says so.
BEIJING = dt.timezone(dt.timedelta(hours=8))
TIME_ZONE_LABEL = "北京时间 UTC+8"

#: Chinese labels for the provider names that appear in the ``sources`` column.
SOURCE_LABELS: dict[str, str] = {
    "google_maps": "Google 地图",
    "openstreetmap": "OpenStreetMap",
    "csv_list": "CSV 名单",
    "directory": "企业目录",
}

#: The same sources for the English workbook. A source name is our own label,
#: not source text, so translating it is a wording choice — the identifier is
#: unchanged in the technical files either way.
SOURCE_LABELS_EN: dict[str, str] = {
    "google_maps": "Google Maps",
    "openstreetmap": "OpenStreetMap",
    "csv_list": "CSV list",
    "directory": "Business directory",
}

#: Fields ``reader_rows`` asks :func:`entity_rows` for, independent of the run's
#: own ``export.fields``: the reader-facing sheet must look the same even when
#: the technical export was narrowed down.
#:
#: ``description`` is here because it is the evidence the industry match rests
#: on (task book 021 A). It is *not* shown as a column; the match reason quotes
#: the part of it that decided the verdict.
BILINGUAL_SOURCE_FIELDS = [
    "entity_id", "title", "address", "phone", "website", "emails",
    "rating", "review_count", "sources", "scraped_at", "first_seen_at", "provider",
    "description", "category",
]

#: Columns the matched reader tables add to the fixed reader columns, as
#: ``(key, 中文表头, english header)``. The bilingual header is the two joined.
#: The verdict is one word a reader can filter on; the reason and the evidence
#: are what make it checkable.
MATCH_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("match_status", "匹配结果", "Verdict"),
    ("match_region", "地区判断", "Region"),
    ("match_industry", "行业判断", "Industry"),
    ("match_reason", "匹配理由", "Reason"),
    ("match_evidence", "证据与来源", "Evidence"),
)


@dataclass(frozen=True)
class LangSpec:
    """Everything that changes between the reader-facing languages.

    The *data* never changes with the language — only headers, sheet names,
    notes and the placeholder for a missing value. Names, addresses and source
    text stay exactly as the source wrote them, which is the task book's
    "名称、地址及来源文本保留原文，本轮不做全文机器翻译".
    """

    code: str
    filename: str
    download_label: str
    download_note: str
    sheets: tuple[str, str, str]
    #: The three verdict sheets, used instead of ``sheets`` when the config
    #: carries a ``match`` section. Kept separate so a run without matching
    #: still writes exactly the three sheets it always did.
    match_sheets: tuple[str, str, str]
    columns: tuple[tuple[str, str, int, bool], ...]
    not_available: str
    coverage_labels: tuple[tuple[str, str], ...]
    verification_default: str
    verification_invalid: str
    verification_conflict: str
    verification_review: str
    texts: dict[str, str]
    field_notes: dict[str, tuple[str, str]]


#: Column order of the main table. ``(key, header, width, always shown)``. A
#: column that is not "always shown" is dropped when every row is empty — the
#: task book's "全空的非必要列可不展示" — and the dropped column is then listed
#: under the "not obtained" heading instead of being silently lost.
_BILINGUAL_COLUMNS: tuple[tuple[str, str, int, bool], ...] = (
    ("no", "序号 / No.", 7, True),
    ("title", "商家名称 / Business name", 40, True),
    ("address", "地址 / Address", 40, True),
    ("phone", "电话 / Phone", 20, True),
    ("website", "官网 / Website", 32, False),
    ("emails", "邮箱 / Email", 26, False),
    ("rating", "地图评分 / Rating", 15, False),
    ("review_count", "评论数 / Reviews", 15, False),
    ("sources", "来源 / Source", 22, True),
    ("scraped_at", "采集时间 / Collected at", 26, True),
    ("verification", "核实情况 / Verification", 34, True),
    ("entity_id", "记录ID / Record ID", 14, True),
)

_ZH_COLUMNS: tuple[tuple[str, str, int, bool], ...] = (
    ("no", "序号", 7, True),
    ("title", "商家名称", 40, True),
    ("address", "地址", 40, True),
    ("phone", "电话", 20, True),
    ("website", "官网", 32, False),
    ("emails", "邮箱", 26, False),
    ("rating", "地图评分", 15, False),
    ("review_count", "评论数", 15, False),
    ("sources", "来源", 22, True),
    ("scraped_at", "采集时间", 26, True),
    ("verification", "核实情况", 34, True),
    ("entity_id", "记录ID", 14, True),
)

_EN_COLUMNS: tuple[tuple[str, str, int, bool], ...] = (
    ("no", "No.", 7, True),
    ("title", "Business name", 40, True),
    ("address", "Address", 40, True),
    ("phone", "Phone", 20, True),
    ("website", "Website", 32, False),
    ("emails", "Email", 26, False),
    ("rating", "Rating", 15, False),
    ("review_count", "Reviews", 15, False),
    ("sources", "Source", 22, True),
    ("scraped_at", "Collected at", 26, True),
    ("verification", "Verification", 34, True),
    ("entity_id", "Record ID", 14, True),
)

_COVERAGE_BILINGUAL = (
    ("phone", "电话 / Phone"),
    ("website", "官网 / Website"),
    ("emails", "邮箱 / Email"),
    ("rating", "地图评分 / Rating"),
    ("review_count", "评论数 / Reviews"),
)

#: Shared English wording for the "language controls headers and notes, not the
#: values" statement the task book asks for.
_VALUE_POLICY_EN = (
    "This language option controls headers and explanatory notes only. Business "
    "names and addresses are kept exactly as the source wrote them and are not "
    "translated; a Chinese address in the English sheet is therefore expected."
)
_VALUE_POLICY_ZH = (
    "语言选项只控制表头与说明文字；商家名称与地址一律保留来源原文，不做翻译。"
    "英文版里出现中文地址是正常的。"
)

#: What the exporter does to external text so a spreadsheet cannot execute it
#: (026 P3.4). Stated in the workbook rather than left implicit: the CSV rule
#: changes the cell text, and a reader who cannot see that would be entitled to
#: think the CSV was a byte-faithful copy of the source wording.
_CELL_SAFETY_ZH = (
    "以 = @ 开头，或 + - 后面还跟着运算符的值，可能被表格软件当公式执行："
    "在 XLSX 里按文本单元格写入（「电话」等列固定为文本格式），"
    "在 CSV 里这类单元格会加一个 ' 前缀，因为 CSV 没有单元格类型可依。"
    "电话 +49 30 12345678、地址 - 1st Street 这类值不会被加前缀。"
    "原始值逐字保留在 JSONL 与技术版里。来源文本中的不可见控制字符会被清掉，否则 Excel 无法保存。"
)
_CELL_SAFETY_EN = (
    "Values that start with = or @, or whose leading + - is followed by an "
    "operator, could be executed as a formula: they are written as text cells in "
    "the XLSX (phone-like columns are fixed to text format), and in the CSV such "
    "a cell gets a leading ' because a CSV has no cell type to rely on. Values "
    "like the phone +49 30 12345678 or the address - 1st Street are not prefixed. "
    "The value is kept verbatim in the JSONL and in the technical workbook. "
    "Invisible control characters are removed from source text; Excel cannot "
    "store them."
)
_CELL_SAFETY_BILINGUAL = f"{_CELL_SAFETY_ZH} / {_CELL_SAFETY_EN}"

LANG_SPECS: dict[str, LangSpec] = {
    "bilingual": LangSpec(
        code="bilingual",
        filename=BILINGUAL_FILENAME,
        download_label="中英双语商家名单（推荐阅读）",
        download_note="中文在前、英文在后；含概览与字段说明，适合直接看和交给甲方",
        sheets=("商家名单", "概览", "来源与字段说明"),
        # Excel sheet names may not contain "/" (or []:*?), so the bilingual
        # group sheets use a middle dot instead of the "/" the headers use.
        match_sheets=("符合名单 · Matched", "待确认 · Needs review", "不符合 · No match"),
        columns=_BILINGUAL_COLUMNS,
        not_available=NOT_AVAILABLE,
        coverage_labels=_COVERAGE_BILINGUAL,
        verification_default="来源记录，未逐家人工核实",
        verification_invalid="来源记录；{n} 项字段未通过校验（见技术版）",
        verification_conflict="来源记录；{n} 项字段存在冲突（见技术版）",
        verification_review="来源记录；{n} 项字段待核实（见技术版）",
        texts={
            "what.title": "这是什么 / What this is",
            "what.purpose.label": "文件用途",
            "what.purpose.value": "中英双语商家名单（推荐阅读版）；原始证据在 entities.xlsx（技术版）",
            "what.run.label": "运行编号 / Run id",
            "what.counting.label": "记录口径 / Counting rule",
            "what.counting.value": "入库记录 {records} → 实体 {entities} → 跨源合并 {merges} → 最终商家 {stores}",
            "what.counting.value_single": (
                "入库记录 {records} → 实体 {entities} → 同一来源内合并 {merges} → 最终商家 {stores} / "
                "ingested records {records} -> entities {entities} -> merges within one source "
                "{merges} -> final stores {stores}"
            ),
            "what.stores.label": "最终商家数 / Stores",
            "what.kept_apart.label": "同名同电话、地址冲突 / Same name+phone, address conflict",
            # 059: the wording must not claim what the evidence cannot show.
            # Two rows with one name and one phone but two addresses may be two
            # branches, or one business the source listed twice -- the records
            # cannot tell those apart, so the sentence says "suspected" and
            # "not confirmed" instead of asserting branches.
            "what.kept_apart.value": (
                "{n} 对保留为两行：名称与电话一致但地址冲突，疑似不同网点或来源重复登记"
                "（是哪种未确认）；未自动合并，明细见技术版「失败与冲突」 / "
                "{n} pair(s) kept as two rows: same name and phone but conflicting "
                "addresses -- a suspected second location or a duplicate listing "
                "(which one is not confirmed). Not merged; see 失败与冲突 in the "
                "technical file."
            ),
            "cond.title": "任务条件 / Run conditions",
            "cond.region.label": "地区 / Region",
            "cond.industry.label": "行业 / Industry",
            "cond.query.label": "搜索词 / Query",
            "cond.city.label": "城市 / City",
            "cond.target.label": "目标条数 / Target",
            "cond.mode.label": "采集模式 / Mode",
            "cond.sources.label": "本次实际来源 / Sources in this list",
            "cond.configured.label": "配置的来源 / Configured sources",
            "acq.title": "本批实际获取方式 / How this batch was obtained",
            "acq.method.label": "获取方式 / Method",
            "acq.live": "实时抓取 / live",
            "acq.cache": "缓存快照 / cache",
            "acq.import": "离线导入（未访问来源站） / import",
            "acq.unknown_mode": "未知 / unknown",
            "acq.no_rows": "本批没有记录 / no rows in this batch",
            "acq.modes.note": "（一条记录经多种方式取得时会各计一次，故合计可能大于记录数）",
            "acq.time.label": "来源时间 / Source time",
            "acq.time.range": "{first} ～ {last}（北京时间）",
            "acq.time.range_date": "{first} ～ {last}（来源只给到日期） / {first} to {last} (dates only)",
            "acq.time.date_only": "{date}（来源只给到日期，没给具体时刻） / {date} (the source stated a date only)",
            "acq.time.unknown": "来源未给出时间 / not stated by the source",
            "acq.sites.label": "来源站与页面 / Source sites and pages",
            "acq.sites.value": "{sites}；名录/列表页共 {pages} 个",
            "acq.sites.no_pages": "{sites}；本批没有记录来源页面（数据来自导入的文件，未访问来源站）",
            "coverage.title": "字段有值数量 / Fields with a value（非验证通过率，not a pass rate）",
            "missing.title": "未获取项目 / Not obtained",
            "missing.columns.label": "本表未列出的空列",
            "missing.note.label": "说明",
            "missing.note.value": "空列在名单中已省略；需要空白占位时请用技术版 entities.xlsx",
            "time.title": "时间与时区 / Time",
            "time.zone.label": "时区",
            "time.zone.value": TIME_ZONE_LABEL,
            "time.note.label": "说明",
            "time.note.value": "采集时间已由 UTC 转换为北京时间显示",
            "limits.title": "限制与声明 / Limits",
            "limits.verify.label": "核实程度",
            "limits.verify.value": "来源记录，未逐家人工核实；请以官方渠道二次确认",
            "limits.reviews.label": "评论数 / Reviews",
            "limits.reviews.value": (
                "评论数取自来源记录，未独立核实；Google 快速模式并不总是给出这个数字，"
                "来源在这格填 0 时本表按「未获取」处理，不会显示成 0 条评论。"
                "需要逐家核实的真实评论数，请改用 browser 模式或另行查证 / "
                "Review counts come from the source record and are not independently "
                "verified; fast mode does not always supply one, and a source 0 is "
                "shown as not obtained rather than as zero reviews."
            ),
            "limits.reviews.value_other": (
                "本来源不提供评论数 / 评分（非 Google 来源）；0 表示该字段未采集，"
                "不是已核实的评论数 / Not applicable: this source provides no reviews/rating"
            ),
            "limits.source.label": "来源链接 / Source link",
            "limits.source.value": (
                "本次来源为通用搜索页（如 maps.google.com/search），"
                "不是该商家的详情页链接，请勿当作商家官网"
            ),
            "limits.source.value_other": (
                "来源链接指向该记录在来源目录或详情页的地址（逐行见「证据与来源」列），"
                "不是商家官网 / The link points at the record's page on the source "
                "directory, not the business's own website"
            ),
            "limits.score.label": "质量分说明",
            "limits.score.value": (
                "本表不展示内部质量分；内部质量分只描述字段完整度与证据，"
                "不代表商家质量、准确率或成交率"
            ),
            "limits.language.label": "语言选项说明 / Language option",
            "limits.language.value": _VALUE_POLICY_ZH,
            "limits.cell.label": "单元格安全 / Cell safety",
            "limits.cell.value": _CELL_SAFETY_BILINGUAL,
            "key.sources.title": "数据来源 / Sources",
            "key.sources.headers": ("来源 / Source", "许可 / Licence", "署名 / Attribution"),
            "key.fields.title": "字段说明 / Fields",
            "key.fields.headers": ("字段 / Field", "中文名", "说明 / Note"),
            "key.division.title": " · 行政区划数据 / Administrative divisions",
            "key.division.labels": ("数据集", "许可", "上游来源", "数据截止", "覆盖边界", "城市中心坐标"),
        },
        field_notes={
            "no": ("序号", "名单内的行号，便于阅读时指认"),
            "title": ("商家名称", "保留来源原文，不翻译、不改写"),
            "address": ("地址", "保留来源原文，不翻译、不改写"),
            "phone": ("电话", "以文本保存，保留开头的 + 号"),
            "website": ("官网", "可点击；来自来源记录"),
            "emails": ("邮箱", "仅当实际采集到时才有值"),
            "rating": ("地图评分", "来源给出的评分，保留原文"),
            "review_count": ("评论数", "见概览页的评论数说明"),
            "sources": ("来源", "该商家由哪些来源记录（合并后可能多于一个）"),
            "scraped_at": ("采集时间", TIME_ZONE_LABEL),
            "verification": ("核实情况", "由字段的实际校验状态生成，不是统一话术"),
            "entity_id": ("记录ID", "与技术版 entities.xlsx 的 entity_id 对应，用于追溯"),
        },
    ),
    "zh": LangSpec(
        code="zh",
        filename="entities_zh.xlsx",
        download_label="中文商家名单（推荐阅读）",
        download_note="中文表头与说明；名称与地址保留来源原文",
        sheets=("商家名单", "概览", "来源与字段说明"),
        match_sheets=("符合名单", "待确认", "不符合"),
        columns=_ZH_COLUMNS,
        not_available="未获取",
        coverage_labels=(
            ("phone", "电话"),
            ("website", "官网"),
            ("emails", "邮箱"),
            ("rating", "地图评分"),
            ("review_count", "评论数"),
        ),
        verification_default="来源记录，未逐家人工核实",
        verification_invalid="来源记录；{n} 项字段未通过校验（见技术版）",
        verification_conflict="来源记录；{n} 项字段存在冲突（见技术版）",
        verification_review="来源记录；{n} 项字段待核实（见技术版）",
        texts={
            "what.title": "这是什么",
            "what.purpose.label": "文件用途",
            "what.purpose.value": "中文商家名单（推荐阅读版）；原始证据在 entities.xlsx（技术版）",
            "what.run.label": "运行编号",
            "what.counting.label": "记录口径",
            "what.counting.value": "入库记录 {records} → 实体 {entities} → 跨源合并 {merges} → 最终商家 {stores}",
            # 061: one source can still produce duplicates when the source lists
            # the same business twice; printing "跨源" there credits a second
            # source that never took part in the run.
            "what.counting.value_single": "入库记录 {records} → 实体 {entities} → 同一来源内合并 {merges} → 最终商家 {stores}",
            "what.stores.label": "最终商家数",
            # 056: two rows that share a name *and* a phone but sit at different
            # addresses are kept apart on purpose (that is the branch rule).
            # Without this line the second one reads as a duplicate nobody
            # explained; with it, the reader knows which rows are the pair.
            "what.kept_apart.label": "同名同电话、地址冲突",
            "what.kept_apart.value": (
                "{n} 对保留为两行：名称与电话一致但地址冲突，疑似不同网点或来源重复登记"
                "（是哪种未确认）；未自动合并，明细见技术版「失败与冲突」"
            ),
            "cond.title": "任务条件",
            "cond.region.label": "地区",
            "cond.industry.label": "行业",
            "cond.query.label": "搜索词",
            "cond.city.label": "城市",
            "cond.target.label": "目标条数",
            "cond.mode.label": "采集模式",
            "cond.sources.label": "本次实际来源",
            "cond.configured.label": "配置的来源",
            "acq.title": "本批实际获取方式",
            "acq.method.label": "获取方式",
            "acq.live": "实时抓取",
            "acq.cache": "缓存快照",
            "acq.import": "离线导入（未访问来源站）",
            "acq.unknown_mode": "未知",
            "acq.no_rows": "本批没有记录",
            "acq.modes.note": "（一条记录经多种方式取得时会各计一次，故合计可能大于记录数）",
            "acq.time.label": "来源时间",
            "acq.time.range": "{first} ～ {last}（北京时间）",
            "acq.time.range_date": "{first} ～ {last}（来源只给到日期）",
            "acq.time.date_only": "{date}（来源只给到日期，没给具体时刻）",
            "acq.time.unknown": "来源未给出时间",
            "acq.sites.label": "来源站与页面",
            "acq.sites.value": "{sites}；名录/列表页共 {pages} 个",
            "acq.sites.no_pages": "{sites}；本批没有记录来源页面（数据来自导入的文件，未访问来源站）",
            "coverage.title": "字段有值数量（不是验证通过率）",
            "missing.title": "未获取项目",
            "missing.columns.label": "本表未列出的空列",
            "missing.note.label": "说明",
            "missing.note.value": "空列在名单中已省略；需要空白占位时请用技术版 entities.xlsx",
            "time.title": "时间与时区",
            "time.zone.label": "时区",
            "time.zone.value": TIME_ZONE_LABEL,
            "time.note.label": "说明",
            "time.note.value": "采集时间已由 UTC 转换为北京时间显示",
            "limits.title": "限制与声明",
            "limits.verify.label": "核实程度",
            "limits.verify.value": "来源记录，未逐家人工核实；请以官方渠道二次确认",
            "limits.reviews.label": "评论数",
            "limits.reviews.value": (
                "评论数取自来源记录，未独立核实；Google 快速模式并不总是给出这个数字，"
                "来源在这格填 0 时本表按「未获取」处理，不会显示成 0 条评论。"
                "需要逐家核实的真实评论数，请改用 browser 模式或另行查证"
            ),
            "limits.reviews.value_other": (
                "本来源不提供评论数 / 评分（非 Google 来源）；0 表示该字段未采集，不是已核实的评论数"
            ),
            "limits.source.label": "来源链接",
            "limits.source.value": (
                "本次来源为通用搜索页（如 maps.google.com/search），"
                "不是该商家的详情页链接，请勿当作商家官网"
            ),
            "limits.source.value_other": (
                "来源链接指向该记录在来源目录或详情页的地址（见每行「证据与来源」列），不是商家官网"
            ),
            "limits.score.label": "质量分说明",
            "limits.score.value": (
                "本表不展示内部质量分；内部质量分只描述字段完整度与证据，"
                "不代表商家质量、准确率或成交率"
            ),
            "limits.language.label": "语言选项说明",
            "limits.language.value": _VALUE_POLICY_ZH,
            "limits.cell.label": "单元格安全",
            "limits.cell.value": _CELL_SAFETY_ZH,
            "key.sources.title": "数据来源",
            "key.sources.headers": ("来源", "许可", "署名"),
            "key.fields.title": "字段说明",
            "key.fields.headers": ("字段", "中文名", "说明"),
            "key.division.title": " · 行政区划数据",
            "key.division.labels": ("数据集", "许可", "上游来源", "数据截止", "覆盖边界", "城市中心坐标"),
        },
        field_notes={
            "no": ("序号", "名单内的行号，便于阅读时指认"),
            "title": ("商家名称", "保留来源原文，不翻译、不改写"),
            "address": ("地址", "保留来源原文，不翻译、不改写"),
            "phone": ("电话", "以文本保存，保留开头的 + 号"),
            "website": ("官网", "可点击；来自来源记录"),
            "emails": ("邮箱", "仅当实际采集到时才有值"),
            "rating": ("地图评分", "来源给出的评分，保留原文"),
            "review_count": ("评论数", "见概览页的评论数说明"),
            "sources": ("来源", "该商家由哪些来源记录（合并后可能多于一个）"),
            "scraped_at": ("采集时间", TIME_ZONE_LABEL),
            "verification": ("核实情况", "由字段的实际校验状态生成，不是统一话术"),
            "entity_id": ("记录ID", "与技术版 entities.xlsx 的 entity_id 对应，用于追溯"),
        },
    ),
    "en": LangSpec(
        code="en",
        filename="entities_en.xlsx",
        download_label="English business list (recommended reading)",
        download_note="English headers and notes; names and addresses keep the source's own text",
        sheets=("Business list", "Overview", "Sources and fields"),
        match_sheets=("Matched", "Needs review", "No match"),
        columns=_EN_COLUMNS,
        not_available="Not available",
        coverage_labels=(
            ("phone", "Phone"),
            ("website", "Website"),
            ("emails", "Email"),
            ("rating", "Rating"),
            ("review_count", "Reviews"),
        ),
        verification_default="Source record; not individually verified by hand",
        verification_invalid="Source record; {n} field(s) failed validation (see the technical file)",
        verification_conflict="Source record; {n} field(s) conflict (see the technical file)",
        verification_review="Source record; {n} field(s) need review (see the technical file)",
        texts={
            "what.title": "What this is",
            "what.purpose.label": "Purpose",
            "what.purpose.value": "English business list (recommended reading). Raw evidence is in entities.xlsx (technical file).",
            "what.run.label": "Run id",
            "what.counting.label": "Counting rule",
            "what.counting.value": "ingested records {records} -> entities {entities} -> cross-source merges {merges} -> final stores {stores}",
            "what.counting.value_single": "ingested records {records} -> entities {entities} -> merges within the one source {merges} -> final stores {stores}",
            "what.stores.label": "Final stores",
            "what.kept_apart.label": "Same name+phone, address conflict",
            "what.kept_apart.value": (
                "{n} pair(s) kept as two rows: same name and phone but conflicting "
                "addresses -- a suspected second location or a duplicate listing "
                "(which one is not confirmed). Not merged; see the technical file's "
                "失败与冲突 sheet."
            ),
            "cond.title": "Run conditions",
            "cond.region.label": "Region",
            "cond.industry.label": "Industry",
            "cond.query.label": "Query",
            "cond.city.label": "City",
            "cond.target.label": "Target",
            "cond.mode.label": "Mode",
            "cond.sources.label": "Sources in this list",
            "cond.configured.label": "Configured sources",
            "acq.title": "How this batch was obtained",
            "acq.method.label": "Method",
            "acq.live": "live fetch",
            "acq.cache": "cache snapshot",
            "acq.import": "offline import (source site not visited)",
            "acq.unknown_mode": "unknown",
            "acq.no_rows": "no rows in this batch",
            "acq.modes.note": "(a record obtained more than one way is counted under each, so the numbers can sum above the row count)",
            "acq.time.label": "Source time",
            "acq.time.range": "{first} to {last} (Beijing time)",
            "acq.time.range_date": "{first} to {last} (dates only)",
            "acq.time.date_only": "{date} (the source stated a date only)",
            "acq.time.unknown": "not stated by the source",
            "acq.sites.label": "Source sites and pages",
            "acq.sites.value": "{sites}; {pages} listing page(s)",
        "acq.sites.no_pages": "{sites}; no source page recorded (imported from a file; the source site was not visited)",
            "coverage.title": "Fields with a value (this is a fill count, NOT a pass rate)",
            "missing.title": "Not obtained",
            "missing.columns.label": "Empty columns omitted from the list",
            "missing.note.label": "Note",
            "missing.note.value": "Empty columns are omitted here; use the technical file entities.xlsx if you need a blank placeholder",
            "time.title": "Time and time zone",
            "time.zone.label": "Time zone",
            "time.zone.value": TIME_ZONE_LABEL,
            "time.note.label": "Note",
            "time.note.value": "Collection times are converted from UTC and shown in Beijing time",
            "limits.title": "Limits and statements",
            "limits.verify.label": "Verification",
            "limits.verify.value": "Source records, not individually verified by hand; confirm through official channels",
            "limits.reviews.label": "Reviews",
            "limits.reviews.value": (
                "Review counts come from the source record and are not independently "
                "verified. Google fast mode does not always supply one; when the source "
                "writes 0 there, this sheet treats it as not obtained rather than as "
                "zero reviews. For a verified count per business, use browser mode or "
                "check separately."
            ),
            "limits.reviews.value_other": (
                "This source provides no review count or rating (non-Google); 0 means the "
                "field was not collected, not a verified count."
            ),
            "limits.source.label": "Source link",
            "limits.source.value": (
                "The source URL is a generic search page (e.g. maps.google.com/search), "
                "not this business's detail page; do not treat it as the business website."
            ),
            "limits.source.value_other": (
                "The source link points at the record's page on the source directory "
                "(see the evidence column on each row), not the business's own website."
            ),
            "limits.score.label": "Quality score",
            "limits.score.value": (
                "This sheet does not publish the internal quality score. That score describes "
                "field completeness and evidence only; it is not business quality, accuracy or a deal rate."
            ),
            "limits.language.label": "Language option",
            "limits.language.value": _VALUE_POLICY_EN,
            "limits.cell.label": "Cell safety",
            "limits.cell.value": _CELL_SAFETY_EN,
            "key.sources.title": "Data sources",
            "key.sources.headers": ("Source", "Licence", "Attribution"),
            "key.fields.title": "Fields",
            "key.fields.headers": ("Field", "Name (zh)", "Note"),
            "key.division.title": " · Administrative division data",
            "key.division.labels": (
                "Dataset", "Licence", "Upstream source", "Data as of",
                "Coverage boundary", "City centre coordinates",
            ),
        },
        field_notes={
            "no": ("序号", "Row number within this list, for reference when reading"),
            "title": ("商家名称", "Kept exactly as the source wrote it; not translated or rewritten"),
            "address": ("地址", "Kept exactly as the source wrote it; not translated or rewritten"),
            "phone": ("电话", "Stored as text so the leading + survives"),
            "website": ("官网", "Clickable; taken from the source record"),
            "emails": ("邮箱", "Only present when one was actually collected"),
            "rating": ("地图评分", "The rating the source published, kept verbatim"),
            "review_count": ("评论数", "See the review-count note on the Overview sheet"),
            "sources": ("来源", "Which sources describe this business (more than one after a merge)"),
            "scraped_at": ("采集时间", TIME_ZONE_LABEL),
            "verification": ("核实情况", "Generated from the fields' actual validation states, not boilerplate"),
            "entity_id": ("记录ID", "Matches entity_id in the technical file entities.xlsx, for tracing"),
        },
    ),
}



def _value_from_facts(rows: list[Any], field: str) -> str | None:
    """The value as the source stated it, joined when a field has several.

    Three rules, all of which matter once website enrichment (stage D) adds a
    second source for the same field:

    * ``raw_value`` wins over ``normalized_value`` — an export column must show
      what the page actually said, not the casefolded form used for matching;
    * facts whose ``validation_status`` is ``invalid`` are left out of the
      *view*. They stay in ``field_facts.jsonl`` with their reason, so nothing
      is hidden, but a broken address must not be presented as the business's
      email;
    * values that are the same thing written differently collapse. Rows arrive
      ordered by id, and the listing's fact is always written before the
      website's, so the listing's spelling is the one that survives — which is
      what "the website must not overwrite the original source" means in
      practice;
    * a fact whose status is ``missing`` is not a value. That covers both an
      absent field and a provider's absent-default placeholder (a fast-mode
      Google ``review_count`` of 0), which is stored as missing precisely so it
      cannot be shown here as a verified number.
    """
    seen: dict[str, str] = {}
    for row in rows:
        status = str(row["validation_status"])
        if status in ("invalid", "missing"):
            continue
        if is_absent_default(row["provider"], field, row["raw_value"]):
            # A database written before the ingest rule existed still carries a
            # *valid* 0 for google_maps review_count; the view must not present
            # it either. Re-exporting an old run therefore reads the same as a
            # freshly ingested one.
            continue
        text = row["raw_value"] or row["normalized_value"]
        if text is None:
            continue
        text = str(text)
        if not text:
            continue
        key = _value_key(field, row)
        seen.setdefault(key, text)
    if not seen:
        return None
    return MULTI_VALUE_SEPARATOR.join(sorted(seen.values()))


def _value_key(field: str, row: Any) -> str:
    """What makes two facts for the same field "the same value"."""
    text = str(row["raw_value"] or row["normalized_value"] or "")
    if field == "phone":
        digits = "".join(char for char in text if char.isdigit()).lstrip("0")
        if digits:
            return f"phone:{digits}"
    if field == "address":
        skeleton = "".join(char for char in text.casefold() if char.isalnum())
        if skeleton:
            return f"address:{skeleton}"
    normalized = str(row["normalized_value"] or "")
    return (normalized or text).casefold()


def entity_rows(conn, run_id: int, cfg, *, fields: list[str] | None = None) -> Iterator[dict[str, Any]]:
    """One dict per store, keyed by the requested export columns.

    ``fields`` defaults to the run's ``export.fields``; the bilingual sheet
    passes its own fixed column set so the reader-facing view does not change
    when the technical export is narrowed down.

    A store that several sources describe is exported **once**: the entities
    that stage E merged into it contribute their field facts to the same row,
    because a lead list with the same dental practice on three lines is not a
    lead list. The merged entities themselves are not dropped — they keep every
    fact in ``field_facts.jsonl`` and the merge reason in the database — so the
    collapse is a *view*, not a deletion.
    """
    fields = list(cfg.export_fields) if fields is None else list(fields)
    canonical = db.canonical_map(conn)

    facts_by_key: dict[tuple[int, str], list[Any]] = {}
    for row in db.iter_run_facts(conn, run_id):
        facts_by_key.setdefault((int(row["entity_id"]), str(row["field"])), []).append(row)

    # entity_id -> the entity whose columns the export shows (the survivor).
    display: dict[int, int] = {}
    members: dict[int, list[int]] = {}
    for entity in db.iter_run_entities(conn, run_id):
        entity_id = int(entity["id"])
        survivor = canonical.get(entity_id, entity_id)
        display[survivor] = survivor
        members.setdefault(survivor, []).append(entity_id)
        # The survivor's own row is the one used for its columns.
        if survivor == entity_id:
            display[survivor] = entity_id

    seen: set[int] = set()
    for entity in db.iter_run_entities(conn, run_id):
        entity_id = int(entity["id"])
        survivor = canonical.get(entity_id, entity_id)
        if survivor in seen:
            continue
        seen.add(survivor)

        group = members.get(survivor, [survivor])
        # Stage F scores every store on all its sources' evidence — the same
        # grouping the row itself uses — so a merged row's score describes
        # what the reader sees, not just the survivor's own facts.
        group_facts = [
            row for (eid, _field), rows in facts_by_key.items() if eid in group for row in rows
        ]
        score = scoring.score_entity(
            survivor,
            group_facts,
            requested_fields=cfg.collect_fields,
            stable_key=entity["stable_key"],
        )
        score_payload = score.as_dict()
        record: dict[str, Any] = {}
        for name in fields:
            if name in SCORE_COLUMNS:
                key = {"score_composite": "composite", "score_scorer_version": "scorer_version"}.get(
                    name, name
                )
                record[name] = score_payload.get(key)
            elif name in CONTEXT_COLUMNS:
                record[name] = _context_value(entity, facts_by_key, group, name, conn)
            elif name in COLLECT_FIELDS:
                rows: list[Any] = []
                for member in group:
                    rows.extend(facts_by_key.get((member, name), []))
                record[name] = _value_from_facts(rows, name)
            else:  # pragma: no cover - guarded by config validation
                record[name] = None

        yield record


def _context_value(entity, facts_by_key, group: list[int], name: str, conn) -> Any:
    if name == "entity_id":
        return int(entity["id"])
    if name == "stable_key":
        return entity["stable_key"]
    if name == "id_kind":
        return entity["id_kind"]
    if name == "first_seen_at":
        return entity["first_seen_at"]
    if name == "last_seen_at":
        return entity["last_seen_at"]

    if name == "sources":
        # Which sources describe this store, so a merged row says what it is
        # made of rather than hiding the provenance.
        providers: list[str] = []
        for member in group:
            for (_fact_entity, _field), rows in facts_by_key.items():
                if _fact_entity != member:
                    continue
                for row in rows:
                    provider = str(row["provider"])
                    if provider and provider not in providers:
                        providers.append(provider)
        return " | ".join(sorted(providers)) or None

    # provider / query / source_url / scraped_at come from the facts, which are
    # the only rows that carry the full provenance tuple.
    for member in group:
        for (fact_entity, _field), rows in facts_by_key.items():
            if fact_entity != member:
                continue
            for row in rows:
                if name == "provider":
                    return row["provider"]
                if name == "query":
                    # A record's query is the search that produced its
                    # *listing*. A website fact carries none (the page was
                    # reached through the listing, not through a query), so an
                    # empty one must not blank the column for an entity whose
                    # listing facts do state one.
                    if row["query_context"]:
                        return row["query_context"]
                    continue
                if name == "source_url":
                    return row["source_url"]
                if name == "scraped_at":
                    return row["scraped_at"]
    return None


def export_run(conn, run_id: int, cfg, directory: pathlib.Path) -> dict[str, pathlib.Path]:
    """Write the export files and return their paths."""
    directory = pathlib.Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"could not create export directory {directory}: {exc}") from exc

    fields = cfg.export_fields
    rows = list(entity_rows(conn, run_id, cfg))
    # Decided here, once: the verdicts are computed from the facts in the
    # database at export time, so a resumed run or an edited criteria file
    # re-decides and nothing stale can survive (task book 021, D.5).
    criteria, matches, record_evidence = match_map(conn, run_id, cfg)
    if criteria is None:
        # No match section: the historical three sheets, unchanged columns.
        matches = None
        record_evidence = None
    written: dict[str, pathlib.Path] = {}

    if "csv" in cfg.formats:
        path = directory / "entities.csv"
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({name: _csv_cell(row.get(name)) for name in fields})
        except OSError as exc:
            raise ExportError(f"could not write {path}: {exc}") from exc
        written["csv"] = path

    if "jsonl" in cfg.formats:
        path = directory / "entities.jsonl"
        _write_jsonl(path, rows)
        written["jsonl"] = path

    if "xlsx" in cfg.formats:
        path = directory / "entities.xlsx"
        _write_xlsx(
            path,
            fields=fields,
            rows=rows,
            facts=list(_fact_rows(conn, run_id)),
            errors=list(_error_rows(conn, run_id)),
            kept_apart=_kept_apart_pairs(conn, run_id),
        )
        written["xlsx"] = path

        # The reader-facing companions. One per configured language, written
        # next to the technical workbook so whoever gets the technical file also
        # gets one a non-technical reader can open without a key. A config that
        # does not mention languages produces exactly the bilingual file it
        # always did (see config.DEFAULT_LANGUAGES).
        for lang in cfg.languages:
            spec = LANG_SPECS.get(lang)
            if spec is None:  # pragma: no cover - guarded by config validation
                continue
            reader_path = directory / spec.filename
            _write_reader_xlsx(
                reader_path,
                lang=lang,
                rows=reader_rows(
                    conn, run_id, cfg, lang, matches=matches, record_evidence=record_evidence
                ),
                cfg=cfg,
                conn=conn,
                run_id=run_id,
                matches=matches,
                criteria=criteria,
                record_evidence=record_evidence,
            )
            written[f"xlsx_{lang}"] = reader_path

    facts_path = directory / "field_facts.jsonl"
    _write_jsonl(facts_path, _fact_rows(conn, run_id))
    written["field_facts"] = facts_path

    # Candidate-site verdicts, exported rather than left in the database.
    # Written unconditionally: an empty file with a header says "none were
    # judged", which is a different statement from a file that is absent -- and
    # the finder block inside it says whether the run *could* look. Both halves
    # are what keeps "found none" apart from "never asked" (034 第二批 2.2).
    candidates = list(candidate_rows(conn, run_id))
    candidates_csv = directory / "site_candidates.csv"
    try:
        with open(candidates_csv, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=CANDIDATE_CSV_COLUMNS, extrasaction="ignore"
            )
            writer.writeheader()
            for row in candidates:
                writer.writerow({name: _csv_cell(row.get(name)) for name in CANDIDATE_CSV_COLUMNS})
    except OSError as exc:
        raise ExportError(f"could not write {candidates_csv}: {exc}") from exc
    written["site_candidates_csv"] = candidates_csv

    candidates_jsonl = directory / "site_candidates.jsonl"
    _write_jsonl(candidates_jsonl, candidates)
    written["site_candidates"] = candidates_jsonl

    errors_path = directory / "errors.jsonl"
    _write_jsonl(errors_path, _error_rows(conn, run_id))
    written["errors"] = errors_path

    # The source manifest, written next to the data rather than only into the
    # run record: it carries each provider's licence and attribution, which is
    # what decides whether the collected rows may be republished.
    manifest_path = directory / "sources_manifest.json"
    _write_json(
        manifest_path,
        {
            "run_id": run_id,
            "sources": manifest(cfg),
            #: How the run looked for official sites, including the finders that
            #: could not look. Without this a sheet with no candidates reads as
            #: "there are none" rather than "nothing was able to ask".
            "site_discovery": _discovery_manifest(cfg, candidates),
            # 034 第三批 3.2: the same three-way split the screen shows, carried
            # into the export. "已验证" alone reads as "a live run will work",
            # which is false for 顺企网 -- so each preset states its offline
            # state and its live state separately, with the evidence for both.
            "directory_presets": _directory_preset_manifest(),
        },
    )
    written["sources_manifest"] = manifest_path

    return written


def _directory_preset_manifest() -> dict[str, dict]:
    from .sources.directory import PRESET_VERIFICATION, verification_label

    return {
        key: dict(row, label=verification_label(key))
        for key, row in PRESET_VERIFICATION.items()
    }


#: The candidate sheet, in the order a reviewer reads it: who it is, what was
#: proposed, where the proposal came from, what was decided and why.
CANDIDATE_CSV_COLUMNS = (
    "entity_id",
    "stable_key",
    "name",
    "url",
    "host",
    "found_by",
    "found_by_label",
    "origin",
    "origin_label",
    "status",
    "reason",
    "note",
    "http_status",
    "judged_at",
    #: 042: what the verdict was reached under. Without these a "confirmed" in a
    #: sheet cannot be told apart from one taken before a merge brought in an
    #: address, or before the rules changed -- which is exactly the difference
    #: that decides whether it may be trusted.
    "judged_rules",
    "judged_evidence",
    "attempts",
)

#: ``found_by`` -> what a reader is told. The three are not interchangeable: an
#: operator's own file, a link on a page the run had, and a programmatic lookup
#: are different weights of evidence and must not be merged into "found".
FOUND_BY_LABELS = {
    "user_list": "人工提供的清单",
    "page_links": "已有页面上的外链",
    "search": "程序搜索（需密钥，未启用）",
    "wikidata": "程序查询公开结构化数据（Wikidata）",
}

#: ``origin`` -> what a reader is told about where the URL itself came from.
ORIGIN_LABELS = {
    "operator-supplied": "人工提供",
    "operator": "人工提供",
    "page-link": "已有页面外链",
    "structured-open-data": "公开结构化数据（程序）",
}


def candidate_rows(conn, run_id: int) -> list[dict[str, Any]]:
    """Every candidate the run judged, with where it came from.

    The verdicts live in ``site_candidates``; until 034 nothing read them back
    out, so a run that found and rejected forty URLs looked exactly like a run
    that never looked. This is the read side.
    """
    from . import db as _db

    raw_rows = [dict(row) for row in _db.iter_site_candidates(conn, run_id)]
    wanted = {int(row["entity_id"]) for row in raw_rows}
    names: dict[int, str] = {}
    keys: dict[int, str] = {}
    for entity_id in sorted(wanted):
        row = conn.execute(
            "SELECT name, stable_key FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is not None:
            names[entity_id] = row["name"] or ""
            keys[entity_id] = row["stable_key"] or ""

    out: list[dict[str, Any]] = []
    for item in raw_rows:
        entity_id = int(item["entity_id"])
        found_by = str(item["found_by"] or "")
        origin = str(item["origin"] or "")
        item["name"] = names.get(entity_id, "")
        item["stable_key"] = keys.get(entity_id, "")
        item["found_by_label"] = FOUND_BY_LABELS.get(found_by, found_by)
        # An origin that is itself a URL is "this page linked to it"; anything
        # else is a category, and an unknown one is shown as-is rather than
        # silently relabelled as if it were.
        item["origin_label"] = (
            ORIGIN_LABELS.get(origin, origin)
            if not origin.startswith(("http://", "https://"))
            else "已有页面外链（" + origin + "）"
        )
        # The audit keys travel with the verdict into the sheet: which rules,
        # which evidence, and how many attempts it took (042). A row written
        # before this existed simply shows them empty -- it is not backfilled,
        # because inventing a rules id for an old verdict would be a lie.
        judgement = _judgement_of(item.get("evidence_json"))
        item["judged_rules"] = judgement.get("rules", "")
        item["judged_evidence"] = judgement.get("evidence", "")
        item["attempts"] = judgement.get("attempts", "")
        out.append(item)
    return out


def _judgement_of(evidence_json: Any) -> dict:
    """The ``_judgement`` block of a stored candidate evidence payload."""
    if not evidence_json:
        return {}
    try:
        payload = json.loads(evidence_json)
    except (TypeError, ValueError):
        return {}
    block = payload.get("_judgement")
    return block if isinstance(block, dict) else {}


def _discovery_manifest(cfg, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """What looked for official sites in this run, and what could not."""
    if not cfg.sitediscovery_enabled:
        return {"enabled": False, "candidates": len(candidates), "finders": []}
    from .sitediscovery import SiteDiscoverer  # local: export must not import it eagerly

    discoverer = SiteDiscoverer(cfg)
    finders = [finder.describe() for finder in discoverer.finders()]
    return {
        "enabled": True,
        "candidates": len(candidates),
        "finders": finders,
        "unavailable": [
            f"{item['name']}: {item['not_configured_reason']}"
            for item in finders
            if not item["configured"] or item["note"]
        ],
    }


def _stable_key_of(conn, entity_id: int) -> str | None:
    row = conn.execute("SELECT stable_key FROM entities WHERE id = ?", (entity_id,)).fetchone()
    return row["stable_key"] if row else None


def _fact_rows(conn, run_id: int):
    """Every fact of the run, with its evidence — the JSONL and XLSX share it."""
    for row in db.iter_run_facts(conn, run_id):
        yield {
            "entity_id": row["entity_id"],
            "stable_key": _stable_key_of(conn, int(row["entity_id"])),
            "field": row["field"],
            "raw_value": row["raw_value"],
            "normalized_value": row["normalized_value"],
            "provider": row["provider"],
            "source_url": row["source_url"],
            "query_context": row["query_context"],
            "scraped_at": row["scraped_at"],
            "evidence_snippet": row["evidence_snippet"],
            "evidence_ref": row["evidence_ref"],
            "extractor_version": row["extractor_version"],
            "validation_status": row["validation_status"],
            "validation_state": row["validation_state"],
            "validation_note": row["validation_note"],
            "checks": json.loads(row["checks_json"]) if row["checks_json"] else None,
            "observed_at": row["observed_at"],
        }


def _conflict_counterpart(fact: dict) -> str:
    """Both halves of a page's self-contradiction, as one readable cell.

    Only source conflicts have two values worth showing (a link's target and the
    text it prints); every other uncertain or invalid fact leaves this blank.
    The wording names which half is which, because "two numbers" without that is
    not checkable against the page.
    """
    checks = fact.get("checks")
    if not isinstance(checks, dict) or not checks.get("source_conflict"):
        return ""
    parts = []
    href = str(checks.get("href_value") or fact.get("raw_value") or "").strip()
    shown = str(checks.get("display_text") or "").strip()
    if href:
        parts.append(f"链接目标 {href}")
    if shown:
        parts.append(f"页面显示 {shown}")
    return "；".join(parts)


def _error_rows(conn, run_id: int):
    for row in db.list_errors(conn, run_id):
        yield {
            "stage": row["stage"],
            "kind": row["kind"],
            "message": row["message"],
            "context": json.loads(row["context_json"]) if row["context_json"] else None,
            "occurred_at": row["occurred_at"],
        }


def _csv_cell(value: Any) -> str:
    """CSV cells are text; None becomes an empty cell, never the string 'None'.

    026 P3.4: a CSV cell has no cell *type*, so unlike the workbook it cannot be
    protected by declaring the value a string -- Excel and LibreOffice will run
    ``=SUM(...)`` the moment the file is opened. The only lever is the text
    itself, so a value that would be read as a formula gets a leading ``'``.
    (Excel shows the apostrophe-prefixed value as text; it does not hide it, but
    it cannot execute it.) The rule is deliberately narrow -- see
    :data:`_CSV_FORMULA_RE` -- so a real address like ``- 1st Street`` and a real
    phone number like ``+49 30 12345678`` are left exactly as the source wrote
    them.

    The JSONL export and the workbook keep the value verbatim -- a lossless copy
    of what the source said always exists.
    """
    if value is None:
        return ""
    text = str(value)
    if _csv_formula_shaped(text):
        return "'" + text
    return text


#: The shapes a spreadsheet reads as a formula at the start of a *CSV* cell.
#:
#: Deliberately narrow, because the prefix changes the value. A leading ``=`` or
#: ``@`` is always a formula. A leading ``+``/``-`` only counts when an operator
#: appears later in the same whitespace-free run, which is what separates
#: ``-1+1`` (a formula) from ``+49 30 12345678`` (an international phone number)
#: and from ``- 1st Street`` (an address). Getting this wrong is not theoretical:
#: a first attempt matched any ``+``-leading value and put an apostrophe in front
#: of every E.164 phone number in the CSV.
_CSV_FORMULA_RE = re.compile(r"^\s*(?:=|@)|^\s*[+\-](?=[^\s]*[+\-*/^=])")


def _csv_formula_shaped(text: str) -> bool:
    return bool(text) and bool(_CSV_FORMULA_RE.match(text))


def _write_jsonl(path: pathlib.Path, rows) -> None:
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
    except OSError as exc:
        raise ExportError(f"could not write {path}: {exc}") from exc


#: Excel treats ``=``, ``+``, ``-`` and ``@`` at the start of a cell as a
#: formula (or a Lotus-style command). A listing whose address is "- 1st
#: Street" would then vanish into a *#NAME?* error. Every cell is therefore
#: written as an explicit string, never inferred.
_FORMULA_PREFIXES = ("=", "+", "-", "@")

#: Identifiers and phone numbers are text, not quantities: a leading ``+`` is
#: part of an E.164 number, and a long ``0x…:0x…`` id is not a float. These
#: columns get Excel's ``@`` (text) number format so nothing is "helpfully"
#: reformatted on open.
TEXT_SEMANTIC_FIELDS = frozenset(
    {"phone", "place_id", "cid", "data_id", "link", "plus_code", "source_id", "stable_key"}
)

#: Excel's hard cell limit. An over-long value is reported, never silently
#: truncated — a clipped evidence snippet is a false quote.
CELL_MAX_CHARS = 32767

#: Characters openpyxl refuses to store (it raises ``IllegalCharacterError``).
#: They reach us from scraped HTML -- a stray NUL, a vertical tab -- and a
#: single one aborts the whole workbook. They are removed, and the overview
#: declares it, so the export never dies on invisible source bytes and a reader
#: is not told the text was verbatim when it was not.
_ILLEGAL_XLSX_CHARS = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")

_FACT_SHEET_COLUMNS = (
    "entity_id",
    "stable_key",
    "field",
    "raw_value",
    "normalized_value",
    "provider",
    "source_url",
    "evidence_snippet",
    "evidence_ref",
    "extractor_version",
    "validation_status",
    "validation_state",
    "validation_note",
    "observed_at",
)


def _set_text(cell, value: Any, *, text_semantic: bool = False) -> None:
    """Write ``value`` as an explicit string cell, never a formula or number."""
    if value is None:
        cell.value = None
        return
    text = value if isinstance(value, str) else str(value)
    if _ILLEGAL_XLSX_CHARS.search(text):
        text = _ILLEGAL_XLSX_CHARS.sub("", text)
    if len(text) > CELL_MAX_CHARS:
        raise ExportError(
            f"cell value is {len(text)} characters; Excel's limit is {CELL_MAX_CHARS}. "
            "Shorten the value in the source or export the field as JSONL."
        )
    cell.value = text
    if text.startswith(_FORMULA_PREFIXES):
        # Keep the text the text it is: the value setter would have marked a
        # leading "=" as a formula.
        cell.data_type = "s"
    if text_semantic:
        cell.number_format = "@"


def _sheet_header(ws, columns) -> None:
    for index, name in enumerate(columns, start=1):
        ws.cell(row=1, column=index, value=name)


def _write_xlsx(path: pathlib.Path, *, fields, rows, facts, errors, kept_apart=None) -> None:
    """Write the stage F workbook: records, field evidence, failures/conflicts.

    The task book asks for "记录、字段证据、失败/冲突说明" at minimum, for the
    record table to keep the entity id so evidence can be joined back, and for
    formula-shaped strings and phone/postcode text semantics to survive. The
    scoring rule is written into its own sheet so the workbook explains the
    score columns it carries.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ExportError(
            "XLSX export needs the openpyxl package "
            "(install with: .venv\\Scripts\\python.exe -m pip install openpyxl)"
        ) from exc

    workbook = Workbook()

    # --- sheet 1: the records ------------------------------------------------
    records = workbook.active
    records.title = "记录"
    record_columns = list(fields)
    if "entity_id" not in record_columns:
        # The join key is part of the deliverable, not a debug column: the
        # evidence sheet references it.
        record_columns.insert(0, "entity_id")
    _sheet_header(records, record_columns)
    for row_index, row in enumerate(rows, start=2):
        for column_index, name in enumerate(record_columns, start=1):
            _set_text(
                records.cell(row=row_index, column=column_index),
                row.get(name),
                text_semantic=name in TEXT_SEMANTIC_FIELDS,
            )
    records.freeze_panes = "A2"

    # --- sheet 2: the field evidence -----------------------------------------
    evidence = workbook.create_sheet("字段证据")
    _sheet_header(evidence, _FACT_SHEET_COLUMNS)
    for row_index, fact in enumerate(facts, start=2):
        for column_index, name in enumerate(_FACT_SHEET_COLUMNS, start=1):
            value = fact.get(name)
            if name == "checks" and value is not None:
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            _set_text(
                evidence.cell(row=row_index, column=column_index),
                value,
                text_semantic=name in TEXT_SEMANTIC_FIELDS,
            )
    evidence.freeze_panes = "A2"

    # --- sheet 3: failures and conflicts --------------------------------------
    problems = workbook.create_sheet("失败与冲突")
    bold = Font(bold=True)
    row_index = 1
    problems.cell(row=row_index, column=1, value="失败/错误（steps that did not complete）").font = bold
    row_index += 1
    error_columns = ("stage", "kind", "message", "context", "occurred_at")
    _sheet_header_at(problems, row_index, error_columns)
    row_index += 1
    for error in errors:
        for column_index, name in enumerate(error_columns, start=1):
            value = error.get(name)
            if name == "context" and value is not None:
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            _set_text(problems.cell(row=row_index, column=column_index), value)
        row_index += 1

    conflicting = [
        fact for fact in facts
        if str(fact.get("validation_status")) in ("uncertain", "invalid")
    ]
    row_index += 1
    problems.cell(
        row=row_index, column=1,
        value="冲突/验证未过字段（website-vs-listing conflicts, failed checks）",
    ).font = bold
    row_index += 1
    conflict_columns = (
        "entity_id", "field", "provider", "raw_value", "normalized_value",
        "validation_status", "validation_state", "validation_note",
        # 048.4: a source_conflict note says "both values are kept, marked for
        # checking" -- and the second value was only in the JSONL, so a reader of
        # the workbook was told about a value they could not see. This column
        # carries both halves of the conflict in the technical file itself.
        "conflicting_values",
        "observed_at",
    )
    _sheet_header_at(problems, row_index, conflict_columns)
    row_index += 1
    for fact in conflicting:
        for column_index, name in enumerate(conflict_columns, start=1):
            value = fact.get(name)
            if name == "conflicting_values":
                value = _conflict_counterpart(fact)
            _set_text(
                problems.cell(row=row_index, column=column_index),
                value,
                text_semantic=name in TEXT_SEMANTIC_FIELDS,
            )
        row_index += 1
    if not errors and not conflicting:
        problems.cell(row=row_index, column=1, value="（无：本运行没有失败或冲突记录）")

    # --- kept-apart rows (056) ------------------------------------------------
    # Deliberately its own section rather than a line in either list above.
    # These are not failures and not field conflicts: two records that share a
    # name and a phone but sit at different addresses, which the merge stage
    # keeps apart on purpose. A reader who sees the same company twice needs to
    # find out here that it was a decision, which rows it was, and why.
    kept_apart = kept_apart or {"total": 0, "pairs": []}
    row_index += 2
    problems.cell(
        row=row_index, column=1,
        value="同名同电话但地址冲突（保留为两行，未合并；疑似不同网点或来源重复登记，是哪种未确认）",
    ).font = bold
    row_index += 1
    if kept_apart["total"]:
        kept_columns = ("name", "phone", "address_a", "address_b", "source_a", "source_b")
        _sheet_header_at(problems, row_index, kept_columns)
        row_index += 1
        for entry in kept_apart["pairs"]:
            for column_index, name in enumerate(kept_columns, start=1):
                _set_text(problems.cell(row=row_index, column=column_index), entry.get(name))
            row_index += 1
        if kept_apart["total"] > len(kept_apart["pairs"]):
            problems.cell(
                row=row_index, column=1,
                value=f"（共 {kept_apart['total']} 对，此处只列出前 {len(kept_apart['pairs'])} 对）",
            )
    else:
        problems.cell(row=row_index, column=1, value="（无：没有这种成对的记录）")

    # --- sheet 4: the published scoring rule ----------------------------------
    rule = workbook.create_sheet("评分说明")
    rule.cell(row=1, column=1, value="评分规则（公开，版本化）").font = bold
    for index, text in enumerate(
        [f"scorer_version: {scoring.SCORER_VERSION}", *scoring.CHANGELOG], start=2
    ):
        rule.cell(row=index, column=1, value=text)
    header_row = 2 + len(scoring.CHANGELOG) + 1
    _sheet_header_at(rule, header_row, ("维度", "权重", "含义"))
    descriptions = {
        "completeness": "按配置请求字段计算的填充比例（缺字段不猜）",
        "validation": "字段验证结果为 valid 的比例，uncertain 计一半",
        "evidence": "带 evidence_snippet/evidence_ref 的事实比例",
        "freshness": "最近观测时间分档（≤7天=1.0 … >365天=0.1）",
        "conflict": "1 减去冲突扣分（uncertain 每个 -0.25，invalid 每个 -0.15）",
    }
    for offset, (name, weight) in enumerate(scoring.DIMENSION_WEIGHTS.items(), start=1):
        base = header_row + offset
        _set_text(rule.cell(row=base, column=1), name)
        _set_text(rule.cell(row=base, column=2), f"{weight:.2f}")
        _set_text(rule.cell(row=base, column=3), descriptions[name])
    note_row = header_row + len(scoring.DIMENSION_WEIGHTS) + 2
    _set_text(
        rule.cell(row=note_row, column=1),
        "注意：综合分是上述权重的加权平均，不是准确率，也不代表销售价值。",
    )

    try:
        workbook.save(path)
    except OSError as exc:
        raise ExportError(f"could not write {path}: {exc}") from exc


def _sheet_header_at(ws, row_index: int, columns) -> None:
    for index, name in enumerate(columns, start=1):
        ws.cell(row=row_index, column=index, value=name)


# --- the bilingual reader-facing workbook -----------------------------------

_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


def _parse_iso(text: Any) -> dt.datetime | None:
    """Parse a collector/Go timestamp, or None when it is not one.

    Go writes nanosecond fractions (``…T15:53:43.6542002Z``) that
    ``datetime.fromisoformat`` refuses on some Python versions, and the
    collector writes microseconds. Both are normalised here; an unparsable
    value is returned as None rather than guessed at.
    """
    value = str(text or "").strip()
    match = _ISO_RE.match(value)
    if not match:
        return None
    date, clock, fraction, zone = match.groups()
    fraction = (fraction or "")[:6].ljust(6, "0")
    zone = zone or "Z"
    if zone == "Z":
        zone = "+00:00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    try:
        return dt.datetime.fromisoformat(f"{date}T{clock}.{fraction}{zone}")
    except ValueError:
        return None


def _beijing_time(value: Any) -> str:
    """A timestamp rendered in Beijing time, with the zone stated in the cell."""
    parsed = _parse_iso(value)
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M") + " (UTC+8)"


def _stated_date(value: Any) -> str:
    """A bare ``YYYY-MM-DD`` the source stated, or ``""``.

    A date is not an instant: it carries no clock and no zone, so it is never
    shifted, never given a time, and never dressed up as ``(UTC+8)``. The
    offline importer's "when was this page captured" field is answered with a
    date more often than with a timestamp, and a reader must be able to tell
    the two apart instead of reading "2026-10-13 08:00 (UTC+8)" as if the
    source had said a time.
    """
    text = str(value or "").strip()
    if len(text) != 10:
        return ""
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _source_time_sentence(value: Any, text: dict) -> str:
    """One source-stated time as a sentence, or ``""`` when there is none.

    Three outcomes, in order of what the source actually said: a full instant
    (rendered in Beijing time and labelled), a bare date (passed through and
    labelled as date-only), or nothing. An unreadable value is not guessed at.
    """
    instant = _beijing_time(value)
    if instant:
        return instant
    day = _stated_date(value)
    if day:
        return text["acq.time.date_only"].format(date=day)
    return ""


def _as_float(value: Any) -> float | None:
    """A number for the rating cell, or None when it is not one."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _source_label(value: Any, lang: str = "zh") -> str:
    def one(part: str) -> str:
        if lang == "en":
            return SOURCE_LABELS_EN.get(part, part)
        if lang == "bilingual":
            zh = SOURCE_LABELS.get(part, part)
            en = SOURCE_LABELS_EN.get(part, part)
            return zh if zh == en else f"{zh} / {en}"
        return SOURCE_LABELS.get(part, part)

    parts = [part.strip() for part in str(value or "").split("|") if part.strip()]
    return " / ".join(one(part) for part in parts)


def _sources_present(rows: list[dict[str, Any]], spec: LangSpec) -> str:
    """The sources that actually produced a row, in a stable order."""
    seen: list[str] = []
    for row in rows:
        for part in str(row.get("sources") or "").split("/"):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
    if not seen:
        return spec.not_available
    return " / ".join(_source_label(name, spec.code) for name in seen)


#: Fact states whose ``uncertain`` status means **two values disagree**: the page
#: contradicted itself (a ``tel:``/``mailto:`` target that is not the printed
#: text), or the website contradicted the listing. Anything else that is
#: uncertain — an address slot holding the business's own name, a number with no
#: country context — is a lead for a human, and the export must not call it a
#: conflict (051 A3: 确认与待核实不能混用).
_CONFLICT_STATES = frozenset(
    {
        "source_conflict",
        "conflicts_with_listing",
    }
)


def _verification_map(conn, run_id: int, spec: LangSpec) -> dict[int, str]:
    """One evidence-based 核实情况 sentence per survivor entity.

    The text is derived from the facts' own validation statuses, never asserted
    wholesale: a store whose fields all passed says so, and a store with
    conflicting or failed fields says how many. The default wording is the
    honest one the task book requires — these are source records, and nobody
    checked each business by hand.

    ``uncertain`` is split in two (051 A3). A *conflict* means two values
    disagree — the page contradicted itself, or the website contradicted the
    listing. Everything else that is uncertain (an address slot holding the
    business's own name, a number with no country context) is a lead for a human.
    Reporting both with the word 冲突 would hide the difference between "the
    source told us two things" and "the source told us one thing we cannot use",
    and a reader who trusted the conflict count would go looking for a
    disagreement that does not exist.
    """
    canonical = db.canonical_map(conn)
    members: dict[int, list[int]] = {}
    for entity in db.iter_run_entities(conn, run_id):
        entity_id = int(entity["id"])
        members.setdefault(canonical.get(entity_id, entity_id), []).append(entity_id)

    facts_by_entity: dict[int, list[Any]] = {}
    for row in db.iter_run_facts(conn, run_id):
        facts_by_entity.setdefault(int(row["entity_id"]), []).append(row)

    out: dict[int, str] = {}
    for survivor, group in members.items():
        invalid = conflicts = review = 0
        for member in group:
            for row in facts_by_entity.get(member, []):
                status = str(row["validation_status"])
                if status == "invalid":
                    invalid += 1
                elif status == "uncertain":
                    if str(row["validation_state"]) in _CONFLICT_STATES:
                        conflicts += 1
                    else:
                        review += 1
        if invalid:
            out[survivor] = spec.verification_invalid.format(n=invalid)
        elif conflicts:
            out[survivor] = spec.verification_conflict.format(n=conflicts)
        elif review:
            out[survivor] = spec.verification_review.format(n=review)
        else:
            out[survivor] = spec.verification_default
    return out


def reader_rows(
    conn,
    run_id: int,
    cfg,
    lang: str = "bilingual",
    matches: dict | None = None,
    record_evidence: dict | None = None,
):
    """The main-table rows: fixed reader columns, source labels, Beijing time.

    Identical data in every language; only the language-dependent *text* differs
    (see :class:`LangSpec).

    ``matches`` carries the per-record verdicts from :func:`match_map`. When it
    is None the sheet is exactly the list 017 shipped -- a run whose config
    carries no ``match`` section decides nothing and must not grow columns.

    ``record_evidence`` carries each record's provenance, so the evidence cell
    can say how the row was obtained and can name a detail page the directory
    listed but this run never opened.
    """
    spec = LANG_SPECS.get(lang) or LANG_SPECS["bilingual"]
    verification = _verification_map(conn, run_id, spec)
    rows: list[dict[str, Any]] = []
    for index, record in enumerate(
        entity_rows(conn, run_id, cfg, fields=BILINGUAL_SOURCE_FIELDS), start=1
    ):
        entity_id = record.get("entity_id")
        survivor = int(entity_id) if entity_id is not None else 0
        row: dict[str, Any] = {
            "no": index,
            "title": record.get("title"),
            "address": record.get("address"),
            "phone": record.get("phone"),
            "website": record.get("website"),
            "emails": record.get("emails"),
            "rating": record.get("rating"),
            "review_count": record.get("review_count"),
            "sources": _source_label(record.get("sources"), spec.code),
            "scraped_at": _collection_time(
                record, (record_evidence or {}).get(survivor), spec
            ),
            "verification": verification.get(survivor, spec.verification_default),
            "entity_id": entity_id,
        }
        if matches is not None:
            evidence = record_evidence.get(survivor) if record_evidence else None
            _add_match_columns(row, matches.get(survivor), spec, evidence)
        rows.append(row)
    return rows


def _collection_time(record: dict, evidence, spec: LangSpec) -> str:
    """When the data was obtained — or an honest "the source did not say".

    Three cases, and their order is the whole point (034 第一批第 3 条):

    1. the source or the operator **stated** a time (``observed_at``) — for a
       saved page that is the only real capture time there is;
    2. this run **fetched** the page (``scraped_at``);
    3. neither — the answer is "not stated by the source", *not* the storage
       time. ``first_seen_at`` used to fill this cell, which made an import
       performed tonight read as data collected tonight.
    """
    stated = _source_time_sentence(getattr(evidence, "observed_at", "") or "", spec.texts)
    if stated:
        return stated
    if _imported_only(getattr(evidence, "acquired_via", "") or ""):
        # A row that came only from files has no collection moment of its own.
        # Rows written before round 034 do carry a ``scraped_at`` — the writer
        # stamped every envelope with "now" — and showing it would present the
        # moment somebody imported a page as the moment the data was collected.
        # The value is left in the database untouched; it is simply not read as
        # a collection time.
        return spec.texts["acq.time.unknown"]
    fetched = _beijing_time(record.get("scraped_at"))
    if fetched:
        return fetched
    return spec.texts["acq.time.unknown"]


def _imported_only(acquired_via: str) -> bool:
    """True when every channel behind this row was an import.

    A row that also has a live part did get bytes fetched this run, so its
    ``scraped_at`` says something real even if its import half does not.
    """
    parts = [part.strip().casefold() for part in acquired_via.split(",") if part.strip()]
    return bool(parts) and all(part == "import" for part in parts)


def _acquired_label(value: str, spec: LangSpec) -> str:
    """How a row was obtained, in the reader's language.

    A mixed value (two providers, one live and one imported) is reported as
    such rather than being rounded to either.
    """
    raw = (value or "").strip()
    if not raw:
        return spec.texts["match.acquired_unknown"]
    labels = {
        "live": spec.texts["match.acquired_live"],
        "cache": spec.texts["match.acquired_cache"],
        "import": spec.texts["match.acquired_import"],
    }
    parts = []
    for mode in dict.fromkeys(part.strip() for part in raw.split(",")):
        if mode:
            parts.append(labels.get(mode, spec.texts["match.acquired_unknown"]))
    return "、".join(parts) if parts else spec.texts["match.acquired_unknown"]


def _add_match_columns(
    row: dict[str, Any], result, spec: LangSpec, evidence=None
) -> None:
    """The verdict columns for one row, in ``spec``'s language."""
    if result is None:
        # A record the matcher did not see should never look like a silent
        # pass: it is reported as needing review, with the reason.
        row["_match_raw"] = matching.STATUS_UNKNOWN
        row["match_status"] = matching.status_label(matching.STATUS_UNKNOWN, spec.code)
        row["match_region"] = spec.texts["match.missing"]
        row["match_industry"] = spec.texts["match.missing"]
        row["match_reason"] = spec.texts["match.missing_reason"]
        row["match_evidence"] = _evidence_text(None, spec, evidence)
        return

    row["_match_raw"] = result.status
    row["match_status"] = matching.status_label(result.status, spec.code)
    row["match_region"] = _dimension_text(result.region, spec)
    row["match_industry"] = _dimension_text(result.industry, spec)
    row["match_reason"] = _reason_text(result, spec)
    row["match_evidence"] = _evidence_text(result, spec, evidence)


def _industry_profile_name(cfg) -> str:
    """The industry preset a config named, or "" when it named none.

    Guarded because the overview and the rule sheet are rendered from whatever
    config the run stored: a hand-edited or older blob must not take the whole
    workbook down over a missing key.
    """
    data = getattr(cfg, "match_data", None) or {}
    if not isinstance(data, dict):
        return ""
    industry = data.get("industry")
    if not isinstance(industry, dict):
        return ""
    return str(industry.get("profile") or "")


def _dimension_text(verdict, spec: LangSpec) -> str:
    """``符合：<reason>`` in the reader's language.

    The reason itself is the rule's Chinese prose in every language, because
    the words it quotes are the source's own Chinese text. The label in front
    is what a non-Chinese reader filters on.
    """
    label = matching.status_label(verdict.status, spec.code)
    if not verdict.reason:
        return label
    separator = " — " if spec.code == "en" else "："
    return f"{label}{separator}{verdict.reason}"


def _reason_text(result, spec: LangSpec) -> str:
    parts = []
    for verdict in (result.region, result.industry):
        if verdict.status == matching.STATUS_NOT_APPLICABLE:
            parts.append(spec.texts["match.na"])
        else:
            parts.append(f"{verdict.reason}")
    separator = "; " if spec.code == "en" else "；"
    return separator.join(parts)


def _evidence_text(result, spec: LangSpec, evidence=None) -> str:
    """The source text a verdict rests on, with where it was read.

    Round 026 P0 adds two honest statements to the cell: *how* the row was
    obtained (a page this run fetched vs. a file the operator imported), and --
    when the directory published a detail link this run never opened -- that the
    link is a lead rather than a verified page. Neither is inferred: they come
    from the record's recorded provenance.
    """
    chunks: list[str] = []
    if result is not None:
        for verdict in (result.region, result.industry):
            for item in verdict.evidence:
                if not item.text:
                    continue
                url = item.source_url or ""
                location = spec.texts["match.evidence_at"].format(url=url) if url else ""
                if spec.code == "en":
                    chunks.append(f"{item.field}: {item.clipped(80)}{location}")
                else:
                    chunks.append(f"{item.field}：{item.clipped(80)}{location}")
    lines = ["\n".join(chunks)] if chunks else []
    if evidence is not None:
        lines.append(
            spec.texts["match.acquired"].format(
                mode=_acquired_label(getattr(evidence, "acquired_via", ""), spec)
            )
        )
        listed = str(getattr(evidence, "detail_url", "") or "")
        if listed:
            lines.append(spec.texts["match.detail_listed"].format(url=listed))
        # A website this run *found* is named with its basis: a reader who sees
        # a URL in the website column must be able to see why it is there, the
        # same way a listed value cites the page it was read from.
        found = str(getattr(evidence, "website", "") or "")
        if found:
            lines.append(
                spec.texts["match.website_discovered"].format(
                    url=found,
                    note=str(getattr(evidence, "website_note", "") or ""),
                    source=str(getattr(evidence, "website_source", "") or ""),
                )
            )
    if not lines:
        return spec.not_available
    return "\n".join(lines)


def match_map(conn, run_id: int, cfg):
    """Decide every record of a run against the config's criteria.

    Returns ``(criteria, {survivor_entity_id: MatchResult}, {survivor: RecordEvidence})``.
    ``criteria`` is None when the config carries no ``match`` section, and then
    the maps are empty: nothing is decided, and a historical workbook is
    untouched.

    The verdicts are computed here, from the facts in the database, every time
    an export runs. Nothing is stored, so there is no old verdict to outlive its
    data: a resumed run, a new record or an edited criteria file all re-decide.
    """
    criteria = matching.criteria_from_config(cfg)
    if criteria is None:
        return None, {}, {}

    canonical = db.canonical_map(conn)
    members: dict[int, list[int]] = {}
    titles: dict[int, str] = {}
    for entity in db.iter_run_entities(conn, run_id):
        entity_id = int(entity["id"])
        members.setdefault(canonical.get(entity_id, entity_id), []).append(entity_id)
        titles[entity_id] = str(entity["name"] or "")

    facts_by_key: dict[tuple[int, str], list[Any]] = {}
    for row in db.iter_run_facts(conn, run_id):
        facts_by_key.setdefault((int(row["entity_id"]), str(row["field"])), []).append(row)

    judged_at = utc_now_iso()
    out: dict[int, Any] = {}
    evidence_out: dict[int, Any] = {}
    for survivor, group in members.items():
        record_evidence = _record_evidence(conn, survivor, group, facts_by_key, titles)
        evidence_out[survivor] = record_evidence
        out[survivor] = matching.match_record(
            record_evidence,
            criteria,
            judged_at=judged_at,
        )
    return criteria, out, evidence_out


def _group_provenance(conn, group: list[int]) -> dict[str, Any]:
    """Merged provenance for a survivor and every entity merged into it."""
    merged: dict[str, Any] = {
        "acquired_via": [],
        "source_page_urls": [],
        "detail_urls": [],
        "detail_fetched": False,
        "origins": [],
        "snapshot_ids": [],
        "observed_ats": [],
        "input_paths": [],
    }
    for member in group:
        prov = db.entity_provenance(conn, member)
        for key in (
            "acquired_via",
            "source_page_urls",
            "detail_urls",
            "origins",
            "snapshot_ids",
            "observed_ats",
            "input_paths",
        ):
            for value in prov.get(key, []):
                if value and value not in merged[key]:
                    merged[key].append(value)
        merged["detail_fetched"] = merged["detail_fetched"] or bool(
            prov.get("detail_fetched")
        )
    return merged


def _legacy_fetched_page(conn, group: list[int]) -> str:
    """The detail page a pre-026 directory record was read from, or ''.

    Rows written before round 026 recorded no ``source_page_url``. Only a
    directory record is recovered here, and only because the provider writes a
    record *after* fetching its detail page: the URL the source itself stored in
    ``native.detail_url`` is therefore the page that was read. Nothing is
    scraped out of an identifier and no other provider is guessed at — a record
    without this evidence is left without an evidence URL.
    """
    if not group:
        return ""
    placeholders = ",".join("?" for _ in group)
    rows = conn.execute(
        f"""
        SELECT o.provider, o.raw_json
          FROM observations o
          JOIN entity_observations eo ON eo.observation_id = o.id
         WHERE eo.entity_id IN ({placeholders})
        """,
        list(group),
    ).fetchall()
    for row in rows:
        if str(row["provider"]) != "directory":
            continue
        try:
            payload = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            continue
        native = payload.get("native") if isinstance(payload, dict) else None
        if isinstance(native, dict):
            url = str(native.get("detail_url") or "").strip()
            if url.startswith(("http://", "https://")):
                return url
    return ""


def _record_evidence(conn, survivor, group, facts_by_key, titles) -> matching.RecordEvidence:
    """One record's own words, gathered from every merged member's facts.

    Round 026 P0: the evidence URL is an explicitly recorded provenance field,
    not something re-derived from the record's identifier. The 024 code scraped
    an http(s) URL out of the stable key and cited it as the page the facts came
    from, which for a csv_list run pointed at a detail page the run had never
    opened. A URL that merely appears in an identifier is a *lead*, so it is now
    surfaced as a listed-and-unvisited detail link instead of as evidence.
    """
    provenance = _group_provenance(conn, group)
    page = provenance["source_page_urls"][0] if provenance["source_page_urls"] else ""
    detail_url = provenance["detail_urls"][0] if provenance["detail_urls"] else ""
    if provenance["detail_fetched"]:
        # The detail page was fetched this run, so it *is* the evidence.
        page = page or detail_url
    if not page:
        page = _legacy_fetched_page(conn, group)

    def first(field_name: str) -> tuple[str, str]:
        for member in group:
            for row in facts_by_key.get((member, field_name), []):
                if str(row["validation_status"]) in ("invalid", "missing"):
                    continue
                text = row["raw_value"] or row["normalized_value"]
                if text:
                    return str(text), str(row["source_url"] or "")
        return "", ""

    title, title_url = first("title")
    if not title:
        title = titles.get(survivor, "")
    address, address_url = first("address")
    description, description_url = first("description")
    category, category_url = first("category")

    # A website stage P1 *discovered* is reported with its basis. The listed
    # website (a value the source published) is not treated this way: it needs
    # no justification beyond its own page, which the field citations already
    # give. Only the found one is an inference, so only it says why.
    website, website_note, website_source = _discovered_website(facts_by_key, group)

    def _cite(*candidates: str) -> str:
        """The evidence URL to show, preferring a real page over a bare path.

        The page the facts were read from wins; a field URL is the fallback for
        a record whose page was not recorded. A local file path is only ever
        shown when nothing better exists, because a path is transport, not a
        source.
        """
        for candidate in candidates:
            if candidate and candidate.startswith(("http://", "https://")):
                return candidate
        for candidate in candidates:
            if candidate:
                return candidate
        return ""

    field_urls = {
        "title": _cite(page, title_url),
        "address": _cite(page, address_url),
        "description": _cite(page, description_url),
        "category": _cite(page, category_url),
    }
    evidence_url = _cite(page, address_url, description_url, title_url)
    # A detail link is only a *lead* worth flagging when it is not already the
    # page the facts came from: if the two are the same URL, the record was read
    # there and there is nothing unvisited to warn about.
    listed_detail = "" if provenance["detail_fetched"] else detail_url
    if listed_detail and listed_detail == evidence_url:
        listed_detail = ""
    return matching.RecordEvidence(
        title=title,
        address=address,
        description=description,
        category=category,
        source_url=evidence_url,
        field_urls=field_urls,
        acquired_via=", ".join(provenance["acquired_via"]),
        # Only carried when the page was *not* fetched: a fetched detail page is
        # already the evidence and needs no separate "listed" annotation.
        detail_url=listed_detail,
        detail_fetched=provenance["detail_fetched"],
        origin=", ".join(provenance["origins"]),
        observed_at=provenance["observed_ats"][0] if provenance["observed_ats"] else "",
        website=website,
        website_note=website_note,
        website_source=website_source,
    )


def _discovered_website(facts_by_key, group) -> tuple[str, str, str]:
    """The website stage P1 confirmed for a record, with its basis.

    Reads the ``site_discovery`` fact specifically: a ``website`` fact written by
    stage D records that a *listed* site was visited, which is a different
    statement from "this is their site, and here is why we think so".
    """
    from .sitediscovery import PROVIDER as SITE_DISCOVERY

    for member in group:
        for row in facts_by_key.get((member, "website"), []):
            if str(row["provider"]) != SITE_DISCOVERY:
                continue
            value = str(row["raw_value"] or row["normalized_value"] or "")
            if not value:
                continue
            return value, str(row["validation_note"] or ""), str(row["source_url"] or "")
    return "", "", ""


#: Backwards-compatible alias: the bilingual sheet was the first reader view.
bilingual_rows = reader_rows


def _match_columns(spec: LangSpec) -> list[tuple[str, str, int, bool]]:
    """The verdict columns, in ``spec``'s language, appended to the reader set.

    ``match_region`` / ``match_industry`` / ``match_reason`` / ``match_evidence``
    are dropped when every row is empty the same way an optional reader column
    is, so a sheet with nothing to say does not grow empty columns.
    """
    out: list[tuple[str, str, int, bool]] = []
    for key, zh, en in MATCH_COLUMNS:
        header = en if spec.code == "en" else (f"{zh} / {en}" if spec.code == "bilingual" else zh)
        out.append((key, header, 34 if key != "match_status" else 18, key == "match_status"))
    return out


def _visible_columns(
    rows: list[dict[str, Any]], spec: LangSpec, *, with_match: bool = False
) -> list[tuple[str, str, int, bool]]:
    """Drop the non-essential columns that are empty in every row.

    ``with_match`` adds the verdict columns. It is only on when the run decided
    something: a config without a ``match`` section produces exactly the columns
    017 shipped, and the verdict column is not one of them.
    """
    source = spec.columns + (tuple(_match_columns(spec)) if with_match else ())
    visible: list[tuple[str, str, int, bool]] = []
    for key, header, width, essential in source:
        if essential:
            visible.append((key, header, width, essential))
            continue
        if any(row.get(key) not in (None, "") for row in rows):
            visible.append((key, header, width, essential))
    return visible


def _missing_column_labels(
    rows: list[dict[str, Any]],
    visible: list[tuple[str, str, int, bool]],
    spec: LangSpec,
) -> list[str]:
    shown = {key for key, *_ in visible}
    return [
        f"{header}（{key}）" if spec.code != "en" else f"{header} ({key})"
        for key, header, _width, _essential in spec.columns
        if key not in shown and key != "entity_id"
    ]


def _run_counts(conn, run_id: int) -> dict[str, int]:
    """The de-duplication ledger quoted in the overview.

    ``records`` is what was ingested, ``entities`` is how many distinct
    entities those records resolved to, ``merges`` is how many of those were
    folded into another, and ``stores`` is the final count the list shows —
    so "records -> entities -> merges -> stores" is auditable rather than a
    single number nobody can reproduce.
    """
    def scalar(sql: str) -> int:
        row = conn.execute(sql, (run_id,)).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    return {
        "records": scalar("SELECT COUNT(*) FROM entity_observations WHERE run_id = ?"),
        "entities": db.count_run_entities(conn, run_id),
        "merges": scalar("SELECT COUNT(*) FROM entity_merges WHERE run_id = ?"),
        "stores": db.count_run_stores(conn, run_id),
        # 061: how many sources actually contributed rows to this run. A merge
        # between two rows of the *same* source is a duplicate the source listed
        # twice, and the overview has to be able to say that instead of implying
        # two independent sources agreed.
        "sources": scalar(
            """
            SELECT COUNT(DISTINCT o.provider)
              FROM entity_observations eo
              JOIN observations o ON o.id = eo.observation_id
             WHERE eo.run_id = ?
            """
        ),
    }


def _kept_apart_pairs(conn, run_id: int) -> dict[str, Any]:
    """Records kept as two rows because only the address disagreed (056).

    The merge stage refuses to fold two records together when they share a name
    and a confirmation but sit at different parsed addresses — that rule is what
    stops a branch or a second shop from being swallowed by its head office. The
    ambiguous half of that rule is a pair whose *phone* is identical: a
    directory that lists one company twice looks exactly like that, and so does
    a real second branch. The rows must stay separate either way, so instead of
    merging them the run names them, and this is where a reader finds out which
    rows are the pair and why they were not combined.
    """
    checkpoint = db.get_checkpoint(conn, run_id, "merge", "stats") or {}
    if not isinstance(checkpoint, dict):
        return {"total": 0, "pairs": []}
    pairs = checkpoint.get("branch_pairs") or []
    total = int(checkpoint.get("branch_pairs_total") or len(pairs))
    return {"total": total, "pairs": [p for p in pairs if isinstance(p, dict)]}


def _coverage(rows: list[dict[str, Any]], spec: LangSpec) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for key, label in spec.coverage_labels:
        filled = sum(1 for row in rows if row.get(key) not in (None, ""))
        out.append((label, filled, len(rows)))
    return out


def _rows_by_group(
    rows: list[dict[str, Any]], matches: dict | None
) -> dict[str, list[dict[str, Any]]]:
    """Split the rows into the three verdicts, keeping the sheet order."""
    groups: dict[str, list[dict[str, Any]]] = {
        matching.STATUS_MATCH: [],
        matching.STATUS_UNKNOWN: [],
        matching.STATUS_REJECT: [],
    }
    if matches is None:
        return groups
    for row in rows:
        status = str(row.get("_match_raw") or "")
        groups.get(status, groups[matching.STATUS_UNKNOWN]).append(row)
    return groups


def match_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """How many rows carry each verdict, from the rows themselves."""
    counts = {status: 0 for status in matching.GROUPS}
    for row in rows:
        status = str(row.get("_match_raw") or "")
        if status in counts:
            counts[status] += 1
        else:
            counts[matching.STATUS_UNKNOWN] += 1
    return counts


def _count_with_contact(conn, run_id: int) -> int:
    """Businesses in this run that carry a phone or an email.

    Counted on the *surviving* business, so a merged duplicate is not counted
    twice, and only for values that were not rejected as invalid or missing --
    a phone that failed validation is not a way to be reached.
    """
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT COALESCE(m.canonical_entity_id, e.id)) AS n
          FROM entity_observations eo
          JOIN entities e ON e.id = eo.entity_id
          LEFT JOIN entity_merges m ON m.entity_id = e.id
         WHERE eo.run_id = ?
           AND EXISTS (
                SELECT 1 FROM field_facts f
                 WHERE f.entity_id = e.id
                   AND f.field IN ('phone', 'email')
                   AND COALESCE(f.raw_value, f.normalized_value, '') <> ''
                   AND COALESCE(f.validation_status, '') NOT IN ('invalid', 'missing')
           )
        """,
        (int(run_id),),
    ).fetchone()
    return int(row["n"]) if row else 0


def run_summary(conn, run_id: int, cfg) -> dict[str, Any]:
    """What a reader needs to judge one finished run, recomputed from the facts.

    Returns the collection count, the target, and — when the config asked a
    matching question — how many records are in each of the three groups. The
    screen shows all of it because ``target_count`` is a *collection* ceiling,
    not a promise about matched businesses: a run with a target of 30 that
    matched 4 is not a broken run, it is a run whose sources did not contain 30
    matching businesses, and only the two numbers together say that.

    Recomputed rather than read from a stored mirror, for the same reason the
    export recomputes its verdicts: a stored count would outlive the facts it
    was computed from.
    """
    collected = int(db.count_run_stores(conn, run_id))
    summary: dict[str, Any] = {
        # 034 第三批 3.6: the two collection numbers are different questions.
        # ``candidates`` is what the sources yielded (records, before any
        # cross-source merge); ``collected`` is what survived de-duplication.
        # A run that found 120 records and 98 businesses is not missing 22 --
        # and a card that printed only one of the two would look like it was.
        "candidates": int(db.count_run_entities(conn, run_id)),
        "collected": collected,
        "target_count": int(cfg.target_count),
        "shortfall": max(0, int(cfg.target_count) - collected),
        "matched": None,
        "unknown": None,
        "rejected": None,
        #: How many of the businesses actually carry a way to be reached. The
        #: task book asks for it separately because "98 businesses" and "98
        #: businesses you can call" are different deliverables.
        "with_contact": _count_with_contact(conn, run_id),
    }
    criteria, matches, _evidence = match_map(conn, run_id, cfg)
    if criteria is None:
        return summary

    counts = {status: 0 for status in matching.GROUPS}
    for verdict in matches.values():
        status = getattr(verdict, "status", "")
        if status in counts:
            counts[status] += 1
        else:
            counts[matching.STATUS_UNKNOWN] += 1
    summary["matched"] = counts[matching.STATUS_MATCH]
    summary["unknown"] = counts[matching.STATUS_UNKNOWN]
    summary["rejected"] = counts[matching.STATUS_REJECT]
    return summary


def _reader_list_sheet(
    workbook, name: str, rows: list[dict[str, Any]], spec: LangSpec, columns=None
):
    """One list table: the reader columns, the fixed set of rows, filterable.

    An empty group still gets its header and one line that says the group is
    empty, because a blank sheet reads as "not exported" rather than "none
    qualified" -- and the task book asks for that distinction to be explicit.
    """
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    sheet = workbook.create_sheet(name)
    if columns is None:
        columns = _visible_columns(rows, spec)
    for column_index, (_key, header, width, _essential) in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=column_index, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3A5F")
        sheet.column_dimensions[get_column_letter(column_index)].width = width

    if not rows:
        sheet.cell(row=2, column=1, value=spec.texts["match.empty_group"])
        sheet.freeze_panes = "A2"
        return sheet, columns

    for row_index, row in enumerate(rows, start=2):
        for column_index, (key, _header, _width, _essential) in enumerate(columns, start=1):
            value = row.get(key)
            cell = sheet.cell(row=row_index, column=column_index)
            if value in (None, ""):
                _set_text(cell, spec.not_available)
                cell.font = Font(color="9AA0A6")
            elif key == "website":
                _set_text(cell, value)
                url = str(value)
                if url.startswith(("http://", "https://")):
                    cell.hyperlink = url
                    cell.font = Font(color="0563C1", underline="single")
            elif key == "phone":
                # Text, not a quantity: the leading "+" is part of the number.
                _set_text(cell, value, text_semantic=True)
            elif key in ("no", "entity_id") and isinstance(value, int):
                # Row number and traceable id: plain integers, so filtering and
                # sorting work. Neither can be mistaken for a formula.
                cell.value = value
            elif key == "rating":
                number = _as_float(value)
                if number is None:
                    _set_text(cell, value)
                else:
                    cell.value = number
            elif key in ("match_reason", "match_evidence"):
                # Long prose with newlines: keep it readable in a fixed row.
                _set_text(cell, value)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            else:
                _set_text(cell, value)
            if key not in ("match_reason", "match_evidence"):
                cell.alignment = Alignment(vertical="top", wrap_text=True)

    sheet.freeze_panes = "A2"
    last_column = get_column_letter(len(columns))
    sheet.auto_filter.ref = f"A1:{last_column}{max(1, len(rows) + 1)}"
    return sheet, columns


def acquisition_summary(conn, run_id: int, cfg) -> dict[str, Any]:
    """How this batch's rows were actually obtained, and when the source said so.

    026 P3.1 wants the reader's first screen to state the real acquisition
    method and the source time. Both are facts about the *rows*, so they are
    derived from the same provenance the evidence cells use -- never from the
    config, which only describes what was asked for. A run whose config says
    "live directory" and whose rows were imported from a saved page must say
    ``import`` on the overview, because that is what happened.
    """
    # 061: the fetch time is asked for explicitly -- a run whose configured
    # export columns omit it would otherwise report "the source stated no time"
    # for rows that carry one.
    fields = list(cfg.export_fields)
    if "scraped_at" not in fields:
        fields.append("scraped_at")
    canonical = db.canonical_map(conn)
    modes: dict[str, int] = {}
    observed: list[str] = []
    origins: list[str] = []
    pages: list[str] = []
    rows = 0
    for record in entity_rows(conn, run_id, cfg, fields=fields):
        entity_id = record.get("entity_id")
        if entity_id is None:
            continue
        rows += 1
        survivor = int(canonical.get(int(entity_id), int(entity_id)))
        provenance = _group_provenance(conn, db.merge_members(conn, survivor))
        # A row is counted once per method it was obtained by: a record merged
        # from a live visit and an import legitimately has two, and the note
        # under the line says the numbers can therefore sum above the row count.
        via = [str(mode) for mode in provenance.get("acquired_via") or [] if mode]
        for mode in via or ["unknown"]:
            modes[mode] = modes.get(mode, 0) + 1
        # 061: the same three-case order the per-row cell uses (034 第一批第 3
        # 条). Without the middle case a live batch printed "来源时间：来源未给出
        # 时间" on the overview while every row in the same file showed a real
        # collection time -- the sheet contradicting itself.
        stated_ats = [str(v) for v in provenance.get("observed_ats") or [] if v]
        if stated_ats:
            candidates = stated_ats
        elif not _imported_only(", ".join(via)):
            candidates = [str(record.get("scraped_at") or "")] if record.get("scraped_at") else []
        else:
            # An import has no collection moment of its own; its ``scraped_at``
            # is the moment somebody imported the file.
            candidates = []
        for value in candidates:
            if value and value not in observed:
                observed.append(str(value))
        for value in provenance.get("origins") or []:
            if value and value not in origins:
                origins.append(str(value))
        for value in provenance.get("source_page_urls") or []:
            if value and value not in pages:
                pages.append(str(value))
    return {
        "rows": rows,
        "modes": modes,
        "observed_from": min(observed) if observed else "",
        "observed_to": max(observed) if observed else "",
        "observed_count": len(observed),
        "origins": origins,
        "source_pages": len(pages),
    }


def _acquisition_lines(summary: dict[str, Any], spec: LangSpec) -> list[tuple[str, str]]:
    """The Acquisition section, as ``(label, value)`` pairs.

    Separated from the renderer so the sentences can be tested without building
    a workbook, and so the three languages describe the same facts.
    """
    text = spec.texts
    english = spec.code == "en"
    labels = {
        "live": text["acq.live"],
        "cache": text["acq.cache"],
        "import": text["acq.import"],
        "unknown": text["acq.unknown_mode"],
    }
    order = ("live", "cache", "import", "unknown")
    modes = summary.get("modes") or {}
    parts = [
        f"{labels.get(mode, mode)} {modes[mode]}"
        for mode in order
        if modes.get(mode)
    ]
    joiner = ", " if english else "、"
    method = joiner.join(parts) if parts else text["acq.no_rows"]
    note = " " + text["acq.modes.note"] if len([m for m in order if modes.get(m)]) > 1 else ""
    lines = [(text["acq.method.label"], method + note)]

    first_raw = summary.get("observed_from")
    last_raw = summary.get("observed_to")
    first = _source_time_sentence(first_raw, text)
    last = _source_time_sentence(last_raw, text)
    if not summary.get("observed_count"):
        when = text["acq.time.unknown"]
    elif first and last and first != last:
        # Two different endpoints: the range sentence has to name its own
        # precision. "（北京时间）" is only true when both ends were instants.
        both_instants = bool(_beijing_time(first_raw)) and bool(_beijing_time(last_raw))
        key = "acq.time.range" if both_instants else "acq.time.range_date"
        when = text[key].format(first=first, last=last)
    else:
        when = first or text["acq.time.unknown"]
    lines.append((text["acq.time.label"], when))

    origins = summary.get("origins") or []
    if origins:
        pages = int(summary.get("source_pages") or 0)
        # "共 0 个" reads like a counting failure. A batch whose rows record no
        # page at all -- a CSV import, where the facts were read out of a file --
        # says that in words instead, because the number is the honest answer to
        # a question the batch never asked.
        site_value = (
            text["acq.sites.value"].format(sites=joiner.join(origins), pages=pages)
            if pages
            else text["acq.sites.no_pages"].format(sites=joiner.join(origins))
        )
        lines.append((text["acq.sites.label"], site_value))
    return lines


def acquisition_lines(conn, run_id: int, cfg, lang: str = "zh") -> list[tuple[str, str]]:
    """The ``本批实际获取方式`` section for one run, in one language.

    The screen and the workbook both show this section, so both go through here:
    a page that said "import" while the workbook said "live" would be the same
    class of lie as never saying it at all. ``(label, value)`` pairs, ready to
    render, in whatever language was asked for.
    """
    spec = LANG_SPECS.get(lang) or LANG_SPECS["bilingual"]
    return _acquisition_lines(acquisition_summary(conn, run_id, cfg), spec)


def _write_reader_xlsx(
    path: pathlib.Path,
    *,
    lang: str,
    rows: list[dict[str, Any]],
    cfg,
    conn,
    run_id: int,
    matches: dict | None = None,
    criteria=None,
    record_evidence: dict | None = None,
) -> None:
    """Write one reader-facing workbook in one language.

    Three sheets when the config decides nothing: the list itself, an overview
    that says which batch this is and what was and was not obtained, and a
    source/field key. The technical workbook (``entities.xlsx``) is untouched
    and still carries the evidence.

    Five sheets when a ``match`` section is configured: the three mutually
    exclusive groups (符合 / 待确认 / 不符合), the overview with the matching
    criteria and counts, and the key. ``bilingual`` still renders exactly what
    stage 017 shipped when there is no matching, so an existing
    ``entities_bilingual.xlsx`` is not silently reworded; ``zh`` and ``en`` are
    the same workbook with one language's headers and notes.
    """
    spec = LANG_SPECS.get(lang) or LANG_SPECS["bilingual"]
    text = spec.texts
    kept_apart = _kept_apart_pairs(conn, run_id)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ExportError(
            "XLSX export needs the openpyxl package "
            "(install with: .venv\\Scripts\\python.exe -m pip install openpyxl)"
        ) from exc

    workbook = Workbook()
    # openpyxl starts with one empty sheet; removing it keeps the workbook to
    # exactly the sheets named here (the 017 delivery had no unnamed first tab).
    workbook.remove(workbook.active)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    section_font = Font(bold=True)
    top_wrap = Alignment(vertical="top", wrap_text=True)

    # --- the list(s) ---------------------------------------------------------
    groups = _rows_by_group(rows, matches)
    columns = _visible_columns(rows, spec, with_match=matches is not None)
    if matches is None:
        _reader_list_sheet(workbook, spec.sheets[0], rows, spec, columns)
    else:
        # The three groups partition every classified record; the overview
        # states the sum so the split can be checked without trusting it.
        for status, name in (
            (matching.STATUS_MATCH, spec.match_sheets[0]),
            (matching.STATUS_UNKNOWN, spec.match_sheets[1]),
            (matching.STATUS_REJECT, spec.match_sheets[2]),
        ):
            _reader_list_sheet(workbook, name, groups[status], spec, columns)

    # --- sheet 2: the overview ----------------------------------------------
    overview = workbook.create_sheet(spec.sheets[1])
    overview.column_dimensions["A"].width = 34
    overview.column_dimensions["B"].width = 66
    cursor = {"row": 1}

    def heading(title_text: str) -> None:
        row = cursor["row"]
        overview.cell(row=row, column=1, value=title_text).font = section_font
        cursor["row"] += 1

    def pair(label: str, value: Any) -> None:
        row = cursor["row"]
        overview.cell(row=row, column=1, value=label)
        _set_text(overview.cell(row=row, column=2), value)
        overview.cell(row=row, column=2).alignment = top_wrap
        cursor["row"] += 1

    counts = _run_counts(conn, run_id)
    missing = _missing_column_labels(rows, columns, spec)
    coverage = _coverage(rows, spec)
    region = cfg.region_facts()
    # 024: which providers actually produced these rows decides the wording of
    # the Google-only statements in the Limits section.
    providers_blob = " ".join(
        str(row.get("sources") or row.get("provider") or "") for row in rows
    ).casefold()
    uses_google = "google" in providers_blob

    heading(text["what.title"])
    pair(text["what.purpose.label"], text["what.purpose.value"])
    pair(text["what.run.label"], run_id)
    # 061: with one source, no merge can be cross-source -- it is the source
    # listing one business twice -- and the overview must not read as if a
    # second source had agreed. (With several sources the number still mixes
    # both kinds; splitting it is left as a follow-up, not guessed here.)
    counting_key = (
        "what.counting.value_single" if counts.get("sources", 0) <= 1 else "what.counting.value"
    )
    pair(text["what.counting.label"], text[counting_key].format(**counts))
    pair(text["what.stores.label"], counts["stores"])
    # 056: only shown when it happened, so the line is a fact about this batch
    # rather than boilerplate every file carries.
    if kept_apart["total"]:
        pair(
            text["what.kept_apart.label"],
            text["what.kept_apart.value"].format(n=kept_apart["total"]),
        )

    heading(text["cond.title"])
    pair(text["cond.region.label"], regions.region_display(cfg.country, spec.code))
    pair(text["cond.industry.label"], cfg.industry)
    pair(text["cond.query.label"], cfg.query)
    pair(text["cond.city.label"], " ".join(
        part for part in (cfg.province, cfg.city, cfg.country) if part
    ))
    pair(text["cond.target.label"], cfg.target_count)
    pair(text["cond.mode.label"], cfg.mode)
    pair(text["cond.sources.label"], _sources_present(rows, spec))
    pair(text["cond.configured.label"],
         _source_label("|".join(cfg.sources_list), spec.code))

    # 026 P3.1: how the rows were *actually* obtained, and when the source said
    # so, on the reader's first screen. Derived from the rows' provenance, so a
    # config that says "live" cannot describe an import as a visit.
    heading(text["acq.title"])
    for label, value in _acquisition_lines(acquisition_summary(conn, run_id, cfg), spec):
        pair(label, value)

    heading(text["coverage.title"])
    for label, filled, total in coverage:
        pair(label, f"{filled} / {total}")

    heading(text["missing.title"])
    separator = "、" if spec.code != "en" else "; "
    pair(text["missing.columns.label"], separator.join(missing) if missing else "—")
    pair(text["missing.note.label"], text["missing.note.value"])

    heading(text["time.title"])
    pair(text["time.zone.label"], text["time.zone.value"])
    pair(text["time.note.label"], text["time.note.value"])

    heading(text["limits.title"])
    pair(text["limits.verify.label"], text["limits.verify.value"])
    # 024: the reviews/source-link statements were written for Google runs and
    # used to print on *every* workbook — a domestic directory/CSV list then
    # claimed "the source is a generic Google search page", which is false. They
    # now follow the sources this run actually used.
    pair(text["limits.reviews.label"], text["limits.reviews.value"] if uses_google
         else text.get("limits.reviews.value_other", text["limits.reviews.value"]))
    pair(text["limits.source.label"], text["limits.source.value"] if uses_google
         else text.get("limits.source.value_other", text["limits.source.value"]))
    pair(text["limits.score.label"], text["limits.score.value"])
    pair(text["limits.language.label"], text["limits.language.value"])
    pair(text["limits.cell.label"], text["limits.cell.value"])

    # --- the matching verdict, when the config asked for one ------------------
    if matches is not None:
        verdicts = match_counts(rows)
        heading(text["match.title"])
        pair(text["match.criteria.label"], criteria.describe(spec.code) if criteria else text["match.missing"])
        pair(text["match.rule.label"], matching.MATCH_RULE_VERSION)
        pair(text["match.judged.label"], _beijing_time(
            next(iter(matches.values())).judged_at if matches else ""
        ))
        pair(text["match.match.label"], verdicts[matching.STATUS_MATCH])
        pair(text["match.unknown.label"], verdicts[matching.STATUS_UNKNOWN])
        pair(text["match.reject.label"], verdicts[matching.STATUS_REJECT])
        pair(text["match.total.label"], text["match.total.value"].format(total=len(rows)))
        pair(text["match.collected.label"], text["match.collected.value"].format(
            records=counts["records"], entities=counts["entities"], stores=counts["stores"],
            target=cfg.target_count,
        ))
        if not verdicts[matching.STATUS_MATCH]:
            # Zero matches is a result, and it must be said out loud rather
            # than shown as an empty sheet nobody knows how to read.
            pair(text["match.none_match.label"], text["match.none_match.value"])
        if cfg.match_data:
            profile = _industry_profile_name(cfg)
            note = matching.industry_profile_note(profile, spec.code)
            if note:
                pair(text["match.profile.label"], note)
            # 026 P2.6: the vocabulary's own verified state, said in the
            # overview next to the criteria. A count of 符合 decided by an
            # unchecked word list must not read like an instrument reading.
            if profile or str(cfg.match_data.get("industry") or {}).get("keywords"):
                pair(
                    text["match.vocab_status.label"],
                    matching.industry_profile_status(profile, spec.code),
                )
        pair(text["match.boundary.label"], text["match.boundary.value"])

    # --- sheet 3: source and field key --------------------------------------
    key_sheet = workbook.create_sheet(spec.sheets[2])
    key_sheet.column_dimensions["A"].width = 22
    key_sheet.column_dimensions["B"].width = 26
    key_sheet.column_dimensions["C"].width = 58

    def key_heading(text: str, row_index: int) -> int:
        cell = key_sheet.cell(row=row_index, column=1, value=text)
        cell.font = section_font
        return row_index + 1

    row_index = 1
    row_index = key_heading(text["key.sources.title"], row_index)
    _sheet_header_at(key_sheet, row_index, text["key.sources.headers"])
    for cell in key_sheet[row_index]:
        if cell.value:
            cell.font = section_font
    row_index += 1
    for entry in manifest(cfg):
        _set_text(key_sheet.cell(row=row_index, column=1), entry.get("title") or entry.get("name"))
        _set_text(key_sheet.cell(row=row_index, column=2), entry.get("license"))
        _set_text(key_sheet.cell(row=row_index, column=3), entry.get("attribution"))
        row_index += 1

    row_index += 1
    row_index = key_heading(text["key.fields.title"], row_index)
    _sheet_header_at(key_sheet, row_index, text["key.fields.headers"])
    for cell in key_sheet[row_index]:
        if cell.value:
            cell.font = section_font
    row_index += 1
    for key, header, _width, _essential in list(spec.columns) + (
        list(_match_columns(spec)) if matches is not None else []
    ):
        _set_text(key_sheet.cell(row=row_index, column=1), header)
        chinese, note = spec.field_notes.get(key, ("", ""))
        _set_text(key_sheet.cell(row=row_index, column=2), chinese)
        _set_text(key_sheet.cell(row=row_index, column=3), note)
        row_index += 1

    # The matching rule travels with the workbook, for the same reason the
    # scoring rule does: a verdict nobody can re-derive is an opinion.
    if matches is not None:
        row_index += 1
        row_index = key_heading(text["match.rule_sheet.title"], row_index)
        for label, value in (
            (text["match.rule.label"], matching.MATCH_RULE_VERSION),
            (
                text["match.criteria.label"],
                criteria.describe(spec.code) if criteria else "",
            ),
            (
                text["match.vocab_status.label"],
                matching.industry_profile_status(_industry_profile_name(cfg), spec.code),
            ),
            (text["match.region_rule.label"], text["match.region_rule.value"]),
            (text["match.industry_rule.label"], text["match.industry_rule.value"]),
            (text["match.boundary.label"], text["match.boundary.value"]),
        ):
            _set_text(key_sheet.cell(row=row_index, column=1), label)
            _set_text(key_sheet.cell(row=row_index, column=2), value)
            key_sheet.cell(row=row_index, column=2).alignment = top_wrap
            row_index += 1

    # The administrative-division data (when a Chinese run used it) is a data
    # dependency with its own licence, so it is stated here rather than left in
    # a report nobody reading the workbook will see.
    if region["country"] in ("CN", "HK", "MO", "TW"):
        row_index += 1
        row_index = key_heading(text["key.sources.title"] + text["key.division.title"], row_index)
        labels = text["key.division.labels"]
        for label, value in (
            (labels[0], region["division_source"]),
            (labels[1], region["division_license"]),
            (labels[2], region["division_upstream"]),
            (labels[3], region["division_data_as_of"]),
            (labels[4], region["division_coverage_note"]),
            (labels[5], region["city_centre_note"]),
        ):
            _set_text(key_sheet.cell(row=row_index, column=1), label)
            _set_text(key_sheet.cell(row=row_index, column=2), value)
            row_index += 1

    try:
        workbook.save(path)
    except OSError as exc:
        raise ExportError(f"could not write {path}: {exc}") from exc


def _write_json(path: pathlib.Path, payload: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
    except OSError as exc:
        raise ExportError(f"could not write {path}: {exc}") from exc


# --- matching wording --------------------------------------------------------
#
# The task book asks for a stated timezone rather than a bare timestamp, so
# every time in the reader-facing workbook is rendered in this zone and the
# header says so.

#: The zh / en wording for the matching sections. Merged into each
#: :class:`LangSpec` below rather than written three times, so the three
#: languages cannot drift apart.
_MATCH_TEXTS: dict[str, dict[str, str]] = {
    "zh": {
        "match.title": "匹配汇总（地区 / 行业）",
        "match.criteria.label": "匹配口径",
        "match.rule.label": "规则版本",
        "match.rule_sheet.title": " · 匹配规则",
        "match.judged.label": "判断时间",
        "match.match.label": "符合（地区与行业都符合）",
        "match.unknown.label": "待确认（证据不足或信息冲突）",
        "match.reject.label": "不符合（明确不在条件内）",
        "match.total.label": "合计",
        "match.total.value": "三组互斥，合计 {total} 条，等于本表记录总数",
        "match.collected.label": "采集与符合的关系",
        "match.collected.value": (
            "入库记录 {records} 条，实体 {entities} 个，最终商家 {stores} 家。"
            "目标条数 {target} 只约束采集数量，不等于符合数量："
            "采够数不代表业务目标已满足。"
        ),
        "match.none_match.label": "本批没有符合条件的记录",
        "match.none_match.value": (
            "符合名单为空：这批记录里没有同时满足地区与行业条件的。"
            "没有用其他名单顶替，待确认与不符合的数量见上。"
        ),
        "match.profile.label": "行业口径说明",
        "match.vocab_status.label": "行业词表状态",
        "match.boundary.label": "匹配不等于核实",
        "match.boundary.value": (
            "匹配只说明记录内容与所填条件相符；不等于电话打得通、企业仍在营业、"
            "或对方有购买意向。"
        ),
        "match.region_rule.label": "地区判定依据",
        "match.region_rule.value": (
            "按实体地址中的行政区信息判定。分类页标题、来源所在城市、区号、省份"
            "都不能单独证明属于目标城市（浙江不等于杭州）。明确外省外市判不符合；"
            "地址不全、已撤销的旧区名、信息冲突判待确认并保留原文。目标仅市级时不"
            "额外排除其下属区县，指定区县时按区县粒度判定。"
        ),
        "match.industry_rule.label": "行业判定依据",
        "match.industry_rule.value": (
            "按记录自述的经营内容（主营产品/服务项目）判定，名称与来源分类作为组合"
            "证据；目录栏目名与名称关键词都不能独立成为充分证据，页面推荐区或导航里"
            "的提及不算命中。命中时同时记录匹配范围（用品与食品产品 / 线下门店与"
            "服务），不把宠物用品制造商当作门店。"
        ),
        "match.empty_group": "本组没有记录。",
        "match.missing": "未判定",
        "match.missing_reason": "该记录没有参与匹配，需人工复核",
        "match.na": "不适用（该维度未配置）",
        "match.evidence_at": "（来源：{url}）",
        "match.acquired": "获取方式：{mode}",
        "match.acquired_live": "本批实时抓取",
        "match.acquired_cache": "缓存快照",
        "match.acquired_import": "离线导入（未访问来源站）",
        "match.acquired_unknown": "未知",
        "match.detail_listed": "名录所列详情页（本次未访问）：{url}",
        "match.website_discovered": "官网（本批发现）：{url}｜判定依据：{note}｜候选页：{source}",
    },
    "en": {
        "match.title": "Matching summary (region / industry)",
        "match.criteria.label": "Criteria",
        "match.rule.label": "Rule version",
        "match.rule_sheet.title": " · Matching rule",
        "match.judged.label": "Judged at",
        "match.match.label": "Match (both dimensions)",
        "match.unknown.label": "Needs review (insufficient evidence or a conflict)",
        "match.reject.label": "No match (explicitly outside the criteria)",
        "match.total.label": "Total",
        "match.total.value": "The three groups are exclusive and hold {total} records in total",
        "match.collected.label": "Collected vs matched",
        "match.collected.value": (
            "{records} records ingested, {entities} entities, {stores} stores. "
            "The target of {target} bounds collection only: it is not a count of "
            "matches, and reaching it does not mean the business goal is met."
        ),
        "match.none_match.label": "No record matched in this batch",
        "match.none_match.value": (
            "The matched sheet is empty: no record satisfied both the region and "
            "the industry criteria. Nothing has been substituted for it; see the "
            "counts above for the other two groups."
        ),
        "match.profile.label": "Industry scope note",
        "match.vocab_status.label": "Industry vocabulary status",
        "match.boundary.label": "Matching is not verification",
        "match.boundary.value": (
            "A match says the record's content agrees with the criteria. It does "
            "not say the phone is answered, the business still trades, or that it "
            "is a buyer."
        ),
        "match.region_rule.label": "Region rule",
        "match.region_rule.value": (
            "Decided from the administrative information in the entity address. A "
            "listing page's title, the source's own city, an area code or a bare "
            "province prove nothing (a province is not the city). Another province "
            "or city is rejected; an incomplete address, a withdrawn district name "
            "or a conflict is left for review with the original text kept. A "
            "city-level target does not exclude its districts; a district-level "
            "target is judged at that granularity."
        ),
        "match.industry_rule.label": "Industry rule",
        "match.industry_rule.value": (
            "Decided from what the record itself says it does (products or "
            "services), with the name and the source's classification as "
            "combining evidence. Neither a column heading nor a name keyword is "
            "sufficient on its own, and a mention in a recommendation or "
            "navigation block does not count. The matched scope is recorded "
            "(pet goods vs offline stores and services) so a manufacturer is "
            "never handed over as a store."
        ),
        "match.empty_group": "No records in this group.",
        "match.missing": "Undecided",
        "match.missing_reason": "This record was not matched; it needs a human review",
        "match.na": "Not applicable (dimension not configured)",
        "match.evidence_at": "(source: {url})",
        "match.acquired": "Obtained by: {mode}",
        "match.acquired_live": "fetched this run",
        "match.acquired_cache": "cached snapshot",
        "match.acquired_import": "imported offline (source site not visited)",
        "match.acquired_unknown": "unknown",
        "match.detail_listed": "detail page listed by the directory (not visited this run): {url}",
        "match.website_discovered": (
            "website (found this run): {url} | basis: {note} | candidate page: {source}"
        ),
    },
    "bilingual": {
        "match.title": "匹配汇总（地区 / 行业） / Matching summary (region / industry)",
        "match.criteria.label": "匹配口径 / Criteria",
        "match.rule.label": "规则版本 / Rule version",
        "match.rule_sheet.title": " · 匹配规则 / Matching rule",
        "match.judged.label": "判断时间 / Judged at",
        "match.match.label": "符合（地区与行业都符合） / Match (both dimensions)",
        "match.unknown.label": "待确认（证据不足或信息冲突） / Needs review",
        "match.reject.label": "不符合（明确不在条件内） / No match",
        "match.total.label": "合计 / Total",
        "match.total.value": "三组互斥，合计 {total} 条，等于本表记录总数 / The three groups are exclusive and hold {total} records in total",
        "match.collected.label": "采集与符合的关系 / Collected vs matched",
        "match.collected.value": (
            "入库记录 {records} 条，实体 {entities} 个，最终商家 {stores} 家；"
            "目标条数 {target} 只约束采集数量，不等于符合数量。 / {records} records "
            "ingested, {entities} entities, {stores} stores; the target of {target} "
            "bounds collection only."
        ),
        "match.none_match.label": "本批没有符合条件的记录 / No record matched in this batch",
        "match.none_match.value": (
            "符合名单为空：这批记录里没有同时满足地区与行业条件的，没有用其他名单顶替。"
            " / The matched sheet is empty; nothing has been substituted for it."
        ),
        "match.profile.label": "行业口径说明 / Industry scope note",
        "match.vocab_status.label": "行业词表状态 / Industry vocabulary status",
        "match.boundary.label": "匹配不等于核实 / Matching is not verification",
        "match.boundary.value": (
            "匹配只说明记录内容与所填条件相符；不等于电话打得通、企业仍在营业、或对方有"
            "购买意向。 / A match does not say the phone is answered or the business "
            "still trades."
        ),
        "match.region_rule.label": "地区判定依据 / Region rule",
        "match.region_rule.value": (
            "按实体地址中的行政区信息判定；分类页标题、来源所在城市、区号、省份都不能"
            "单独证明属于目标城市（浙江不等于杭州）。明确外省外市判不符合；地址不全、"
            "已撤销的旧区名、信息冲突判待确认并保留原文。目标仅市级时不额外排除其下属"
            "区县。 / Decided from the address's administrative information; another "
            "province or city is rejected, incomplete or conflicting addresses are "
            "left for review."
        ),
        "match.industry_rule.label": "行业判定依据 / Industry rule",
        "match.industry_rule.value": (
            "按记录自述的经营内容判定，名称与来源分类作为组合证据；目录栏目名与名称关键词"
            "都不能独立成为充分证据，页面推荐区或导航里的提及不算命中。 / Decided from "
            "what the record itself says it does; a column heading or a name keyword "
            "alone is not evidence."
        ),
        "match.empty_group": "本组没有记录。 / No records in this group.",
        "match.missing": "未判定 / Undecided",
        "match.missing_reason": "该记录没有参与匹配，需人工复核 / not matched; needs a human review",
        "match.na": "不适用（该维度未配置） / Not applicable",
        "match.evidence_at": "（来源：{url}） / (source: {url})",
        "match.acquired": "获取方式 / Obtained by：{mode}",
        "match.acquired_live": "本批实时抓取 / fetched this run",
        "match.acquired_cache": "缓存快照 / cached snapshot",
        "match.acquired_import": "离线导入（未访问来源站） / imported offline (site not visited)",
        "match.acquired_unknown": "未知 / unknown",
        "match.detail_listed": "名录所列详情页（本次未访问） / detail page listed, not visited：{url}",
        "match.website_discovered": (
            "官网（本批发现） / website found this run：{url}"
            "｜判定依据 / basis：{note}｜候选页 / candidate page：{source}"
        ),
    },
}

#: The verdict columns' field notes, per language.
_MATCH_NOTES: dict[str, dict[str, tuple[str, str]]] = {
    "zh": {
        "match_status": ("匹配结果", "符合=两个维度都符合；待确认=证据不足或信息冲突；不符合=明确不在条件内"),
        "match_region": ("地区判断", "按实体地址的行政区信息判定，理由写明依据"),
        "match_industry": ("行业判断", "按记录自述经营内容判定，并标注匹配范围"),
        "match_reason": ("匹配理由", "规则给出的可解释理由，不是统一话术"),
        "match_evidence": ("证据与来源", "判定所依据的原文片段与来源页"),
    },
    "en": {
        "match_status": ("Verdict", "Match = both dimensions agree; Needs review = insufficient evidence or a conflict; No match = explicitly outside the criteria"),
        "match_region": ("Region", "Judged from the address's administrative information"),
        "match_industry": ("Industry", "Judged from the record's own description of its business, with the scope recorded"),
        "match_reason": ("Reason", "The rule's explainable reason, not boilerplate"),
        "match_evidence": ("Evidence", "The source text and page the verdict rests on"),
    },
}

for _spec in LANG_SPECS.values():
    _spec.texts.update(_MATCH_TEXTS.get(_spec.code, {}))
    _spec.field_notes.update(_MATCH_NOTES.get(_spec.code, {}))
