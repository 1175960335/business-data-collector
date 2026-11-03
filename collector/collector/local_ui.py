"""Stage F: a deliberately small local control screen.

The task book asks for a one-click Windows entry or a simple local screen:
pick a config (or fill in industry/region), start, watch status, stop,
resume, download the result — and _nothing else_. Debug parameters stay out
of the interface; they remain available in the CLI for whoever wants them.

Design decisions that keep this small and honest:

* **The screen never runs a pipeline itself.** It starts the same CLI the
  terminal uses (``python -m collector run --config ...``) as a child
  process, so there is exactly one execution path to trust and to test.
* **Stop kills the process tree**, the same way a Ctrl+C or a crashed
  shell would; resume is just another start, and the orchestrator's
  checkpoint logic (proven in stage C with a real ``taskkill``) continues
  the unfinished run. A stopped run is therefore a resumable run, not a
  lost one.
* **Status is read from the run database**, the same source ``collector
  status`` reads. The screen cannot invent progress the database does not
  have.
* **Default bind address is 127.0.0.1.** Binding to every interface is an
  explicit, warned-about flag, not a default.

Only the Python standard library is used, so the entry works on a fresh
checkout with nothing installed beyond the project virtualenv.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import pathlib
import re
import subprocess
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from typing import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlsplit

from . import db, export, regions
from .config import SUPPORTED_LANGUAGES, SUPPORTED_SOURCES, Config, load, validate
from .errors import CollectorError
from .matching import INDUSTRY_PROFILES, criteria_from_config
from .orchestrator import Orchestrator
from .sources import directory
from .sources.csvlist import FIELDS as CSV_LIST_FIELDS
from .webfetch import registrable_domain
from .version import __version__

#: Centres for the cities a daily user is likely to type. A short table beats
#: silently reusing Berlin: an unknown city must produce a visible request for
#: coordinates, never a wrong run. Users can always paste exact coordinates.
#:
#: Stage 019 moved the table into :mod:`collector.regions` so the CLI, the
#: screen and the region profiles all read one list (it grew Chinese cities
#: too). This name is kept because it is the screen's documented entry point.
CITY_COORDINATES: dict[tuple[str, str], tuple[float, float]] = dict(regions.CITY_CENTRES)

#: Everything the form is allowed to ask for. A field that is not here is a
#: CLI-level concern and does not belong on a daily-use screen. Stage 019 added
#: the region fields (``country``/``province``/``district``) and the two
#: choices the task book asks a person to make per run: which sources to use
#: and which languages to export.
FORM_FIELDS = (
    "industry", "city", "country", "province", "district",
    "query", "target_count", "mode", "sources", "languages", "enrich", "formats",
    # 021 D.7: the domestic source needs its own knobs, because a directory
    # adapter is configured per site (list URL, detail-link pattern, selectors)
    # rather than by a search term.
    "directory_url", "directory_detail_pattern", "directory_preset",
    # 021 D.5/D.7: the matching question is a reporting choice, made per run.
    # 026 P2.6 adds the exclusion list: naming the trades that are *out* is how
    # "宠物食品包装机械" stops landing in a pet list.
    "match_profile", "match_keywords", "match_exclude_keywords",
    # 026 P1: candidate-site discovery. It needs a way to *name* the businesses
    # and the sites, which on a screen means two files, so those are fields
    # rather than a nested list only a hand-edited YAML could express.
    # 034 第二批 2.3: the account-free structured lookup is opt-in precisely
    # because its coverage is narrow -- it answers for businesses Wikidata has
    # a statement about, which small domestic firms usually are not.
    "sitediscovery", "sitediscovery_sites", "sitediscovery_pages",
    "sitediscovery_budget", "sitediscovery_reuse_days", "sitediscovery_wikidata",
    # 026 P2.1: the offline importer. A saved listing page plus the address it
    # was saved from, and either a folder the detail pages were saved into or an
    # explicit url->path list. Same selectors as the live directory, so the
    # preset dropdown is shared.
    "local_pages", "local_listing_path", "local_origin_url", "local_details",
    "local_preset", "local_observed_at", "local_budget",
    # 034: whether a listing row's own fields (address, main business, ...) are
    # kept when no detail page was saved. On by default.
    "local_listing_fields",
    # 050 P1: the CSV list's own block -- the file, its column mapping, and the
    # original source stated separately from it.
    "csv_list", "csv_path", "csv_columns", "csv_id_column", "csv_delimiter",
    "csv_origin_url", "csv_origin_name", "csv_source_time", "csv_source_time_unknown",
)

#: Collected fields a daily run asks for by default. Completion (stage D) is
#: opt-in because it spends requests per site.
DEFAULT_COLLECT_FIELDS = [
    "title", "address", "phone", "website", "latitude", "longitude", "category",
    "rating", "review_count", "place_id", "cid", "data_id", "link",
]

DEFAULT_EXPORT_FIELDS = [
    "entity_id", "stable_key", "id_kind", "title", "address", "phone", "website",
    "latitude", "longitude", "category", "rating", "review_count", "provider",
    "sources", "query", "source_url", "scraped_at", "first_seen_at", "last_seen_at",
    "score_composite", "score_completeness", "score_completeness_reason",
]

_PLACEHOLDER = "__LOCAL_UI__"

#: What each export file is, in the reader's words, and which one to open
#: first. ``(label, note, recommended)``. The order of this mapping is the
#: order the download list shows, so the recommended file is always on top and
#: nobody has to guess which of seven files is the one they want.
FILE_ROLES: dict[str, tuple[str, str, bool]] = {
    "entities_zh.xlsx": (
        "中文商家名单（推荐阅读）",
        "表头与说明全中文；中国地区默认导出这一份",
        True,
    ),
    "entities_en.xlsx": (
        "英文商家名单（推荐阅读）",
        "表头与说明全英文；中国地址为中文原文，语言选项只控制表头与说明",
        True,
    ),
    "entities_bilingual.xlsx": (
        "中英双语商家名单（推荐阅读）",
        "中文在前、英文在后；含概览与字段说明，适合直接看和交给甲方",
        True,
    ),
    "entities.xlsx": (
        "原始数据（技术版）",
        "记录 + 字段证据 + 失败/冲突 + 评分说明四张表",
        False,
    ),
    "entities.csv": ("原始数据（技术版 · CSV）", "与 entities.xlsx 记录表同列", False),
    "entities.jsonl": ("原始数据（技术版 · JSONL）", "一行一条记录", False),
    "field_facts.jsonl": ("字段证据（技术版）", "逐字段事实，含被覆盖的旧值", False),
    "errors.jsonl": ("失败与错误（技术版）", "未隐藏的失败记录", False),
    "sources_manifest.json": ("来源清单与许可（技术版）", "每个来源的许可与署名", False),
}


def _file_role(name: str) -> tuple[str, str, bool]:
    return FILE_ROLES.get(name, (name, "", False))


def _ordered_exports(files: list[pathlib.Path]) -> list[pathlib.Path]:
    """Recommended file first, then the known order, then anything unknown."""
    order = {name: index for index, name in enumerate(FILE_ROLES)}
    return sorted(files, key=lambda f: (order.get(f.name, len(order)), f.name))


#: The screen's wording for each source. The label states the honest coverage
#: rather than a bare identifier: picking Google for a China run is allowed, but
#: it must not look like the domestic option.
SOURCE_LABELS: tuple[tuple[str, str], ...] = (
    ("google_maps", "Google 地图（海外主力；国内需代理且覆盖不足）"),
    ("openstreetmap", "OpenStreetMap（海外可用；国内覆盖不足）"),
    ("directory", "企业目录（directory 适配器；国内当前可用来源）"),
    ("csv_list", "本地 CSV 清单（不联网）"),
    ("local_pages", "本地已存网页（离线导入，不联网）"),
)

LANGUAGE_LABELS: tuple[tuple[str, str], ...] = (
    ("zh", "中文"),
    ("en", "英文"),
    ("bilingual", "中英双语"),
)

#: 021 D.7: the directory source is configured per site, so the screen ships
#: the one preset that was verified end to end (八方资源网, collected and
#: re-checked in rounds 019/020/021) plus a generic option that uses the
#: adapter's own defaults. A preset is a set of selectors, not a hidden
#: special case: the generated YAML shows every one of them.
#:
#: ``field_selectors`` are the *listing row* fields plus the detail page's
#: profile block (``description``), which is what the industry match reads.
DIRECTORY_PRESETS: dict[str, dict] = {
    "b2b168": {
        "label": "八方资源网 b2b168.com（国内已验证，含公司简介）",
        "detail_pattern": r"/c168-\d+\.html",
        #: 054: a preset is a set of selectors *for one site's page structure*.
        #: Pasting a 顺企网 list URL while the preset still says b2b168 does not
        #: fail loudly -- it selects the wrong nodes and quietly returns few or
        #: zero values, which reads as "this site has no companies". The hosts
        #: are recorded here so the screen can warn *before* the run starts;
        #: it warns and still starts, because the person may know better than
        #: the guess does.
        "host_hints": ("b2b168.com",),
        # 059: the businesses are the rows of one ``ul.list`` inside
        # ``div.list-right``; every one of the 23 links on the saved 杭州·宠物
        # page sits in it. Naming the container keeps a later redesign that puts
        # business links in a sidebar from quietly becoming rows. Verified: with
        # this restriction the saved page still yields the same 23 links.
        "list_selector": "ul.list",
        "field_selectors": {
            "title": "ul.company",
            "address": "dl.codl dd:nth-of-type(1)",
            "phone": "dl.codl dd:nth-of-type(2)",
            "description": "ul.Cgsjj",
        },
        "live_status": directory.verification_label("b2b168"),
        "note": (
            "选择器按 b2b168 的详情页写成；换别的目录站要改生成 YAML 里的 "
            "field_selectors（界面只认这一套预设）。"
        ),
    },
    "shunqiwang": {
        # 026 P2.2: the label states the honest split. Offline samples from this
        # site were verified end to end (round 025: 98 saved rows -> 91 符合);
        # the live path answers 429. A label that only said "已验证" would let a
        # person expect a live run to work.
        "label": "顺企网 11467.com（离线样本已验证，实时采集受限）",
        "detail_pattern": r"/qiye/\d+\.htm",
        # 034: 100 家里面有 2 家的链接指向 https://<id>.b2b.11467.com（路径是 /），
        # 只按路径认不出它们，所以另给一条按整条 URL 判定的规则；
        # 同时把「详情页可以同域子域」和「公司都在 ul.companylist 里」写进预设，
        # 免得把页面边栏的推荐链接也当成列表里的公司。
        "detail_url_pattern": r"^https?://[^/]+\.b2b\.11467\.com(?:/|$)?|/qiye/\d+\.htm",
        "detail_host_scope": "registrable_domain",
        "host_hints": ("11467.com",),
        "list_selector": "ul.companylist",
        "field_selectors": {
            "title": "h1",
            "address": "#contact dl.codl dd:nth-of-type(1)",
            "phone": "#contact dl.codl dd:nth-of-type(2)",
            "description": "#aboutuscontent",
        },
        # 顺企网的限速比 b2b168 严（连续请求会 429），所以这一档单独把
        # 请求间隔放宽，避免用别的站的节奏去撞它。
        "delay_seconds": 4.0,
        #: What is known about this site's *live* path, said per preset. The
        #: status panel and the offline-import block both read it, so the screen
        #: states one fact in one place.
        "live_status": directory.verification_label("shunqiwang"),
        "note": (
            "顺企网的分类列表页形如 https://www.11467.com/hangzhou/search/9092.htm"
            "（杭州宠物公司黄页）；填列表页、不是详情页。该站限速较严，"
            "请求间隔已按它放宽；建议「目标数量」填小一点（如 20），别一次抓太多。"
            "如果实时采集被限速，用下面的「本地已存网页导入」读已经存下来的页面，"
            "结果会标明是导入而不是实时。"
        ),
    },
    "generic": {
        "label": "其他目录站（用适配器默认选择器；多数站点取不到值）",
        "detail_pattern": "",
        "field_selectors": {},
        "host_hints": (),
        "live_status": directory.verification_label("generic"),
        "note": (
            "默认选择器只认常见的 class 名（h1 / address 之类）。"
            "如果列表页跑完记录是空的，请改用上面的已验证预设或手改 YAML。"
        ),
    },
}

#: How a vocabulary's trustworthiness is said out loud. Kept next to the labels
#: so the dropdown, the on-screen note and the export overview cannot drift from
#: each other or from :data:`collector.matching.INDUSTRY_PROFILES`.
_VOCAB_STATUS_ZH: dict[bool, str] = {
    True: "词表已用真实样本验证",
    False: "词表尚未验证，请自行抽检",
}

#: 021 A + 026 P2.6: the industry questions a run may ask. Built from the
#: published registry rather than a second hand-written list, so a profile added
#: to the matcher shows up here on its own -- and a profile the matcher does not
#: know cannot be offered at all. Each label says whether the vocabulary was
#: checked against real samples, because this screen is where an operator decides
#: how much to trust a verdict; 026 P2.6 asks for that state to be visible.
MATCH_PROFILE_LABELS: tuple[tuple[str, str], ...] = (
    ("", "不启用（导出不含符合/待确认/不符合分组）"),
) + tuple(
    (
        name,
        f"{entry.label_zh}（{_VOCAB_STATUS_ZH[entry.verified]}）",
    )
    for name, entry in INDUSTRY_PROFILES.items()
)


def _directory_entry(one, target: int) -> dict:
    """The ``sources_map.directory`` entry the screen's fields describe.

    A missing list URL is refused here, with the field named, because a
    directory run without one would start and collect nothing -- the failure
    would look like "the source has no companies" instead of "no URL given".
    """
    preset_key = one("directory_preset") or "b2b168"
    preset = DIRECTORY_PRESETS.get(preset_key) or DIRECTORY_PRESETS["b2b168"]
    url = one("directory_url")
    if not url:
        raise CollectorError(
            "选择了「企业目录」来源，但没有填目录列表页地址。"
            "请填一个分类列表页的完整网址（例如 https://www.b2b168.com/zhejiang-hangzhou-chongwu/），"
            "它必须是列表页而不是详情页。"
        )
    if "://" not in url:
        raise CollectorError(
            f"目录列表页地址看起来不是完整网址：{url!r}。"
            "请以 http:// 或 https:// 开头。"
        )
    pattern = one("directory_detail_pattern") or preset["detail_pattern"]
    entry = {
        "name": "ui-entry",
        "start_url": url,
        # One listing page and a bounded number of detail pages: the screen is
        # for a first look, and a rate-limited crawl is the polite default.
        # A preset may ask for a slower pace (顺企网 limits harder than b2b168).
        "delay_seconds": float(preset.get("delay_seconds", 1.5)),
        "max_listing_pages": 1,
        "max_detail_pages": max(10, min(target, 60)),
        "stop_after_empty_pages": 1,
        "stop_after_repeated_pages": 1,
    }
    if pattern:
        entry["detail_pattern"] = pattern
    selectors = preset["field_selectors"]
    if selectors:
        entry["field_selectors"] = dict(selectors)
    return entry


def _match_node(one) -> dict | None:
    """The ``match`` section the screen's fields describe, or None.

    Matching is written only when the operator asked a question: a run that
    did not ask exports the workbook it always did, with no grouping and no
    verdict columns. The region side comes from the province/city fields above
    the button, so it is visible on the same screen as the choice.

    026 P2.6: exclusions are read here too. An exclusion list on its own is a
    question ("anything except these trades"), so it is enough to write the
    section even with no profile and no positive keywords -- otherwise a person
    who only wants to rule things out would silently get no matching at all.
    """
    profile = one("match_profile")
    keywords = _split_words(one("match_keywords"))
    exclude_keywords = _split_words(one("match_exclude_keywords"))
    if not profile and not keywords and not exclude_keywords:
        return None
    industry: dict = {}
    if profile:
        industry["profile"] = profile
    if keywords:
        industry["keywords"] = keywords
    if exclude_keywords:
        industry["exclude_keywords"] = exclude_keywords
    node: dict = {"industry": industry}
    region = {
        "province": one("province"),
        "city": one("city"),
    }
    if any(region.values()):
        node["region"] = {key: value for key, value in region.items() if value}
    return node


def _split_words(raw: str) -> list[str]:
    """Split a comma-separated field into words, accepting the full-width comma.

    A Chinese keyboard produces ``，`` far more often than ``,``; treating the
    two the same is the difference between a filled field and a silently ignored
    one.
    """
    return [word.strip() for word in str(raw or "").replace("，", ",").split(",") if word.strip()]


def _sitediscovery_node(one) -> dict | None:
    """The ``sitediscovery`` section the screen's fields describe, or None.

    Two files, because the screen can only name what a person can point at:

    * an **operator site list** -- ``stable_key|name,url`` per row, the sites a
      person already knows;
    * a **saved-page manifest** -- ``name,path,page`` per row, the listing pages
      already saved to disk, from which the off-site links are read.

    Neither is a search. Nothing here makes the collector look anywhere the
    operator did not put in front of it, and the run's report says so. Turning
    the switch on without naming at least one file is refused here, with the
    field named, because that configuration would look for candidates nowhere
    and report "found none" -- the exact conflation task book 026 forbids.
    """
    if one("sitediscovery") != "on":
        return None
    sites = one("sitediscovery_sites")
    pages = one("sitediscovery_pages")
    if not sites and not pages:
        raise CollectorError(
            "开启了「候选官网发现」，但既没填「已知官网清单 CSV」也没填「已保存页面清单 CSV」。"
            "这个功能不会去全网搜索，只能读这两份文件里的地址；"
            "至少填一份（例如已知官网清单：一列企业名、一列网址），否则它会一条也找不到。"
        )
    if sites and not _is_file(sites):
        # A path that does not exist would silently produce nothing; naming it
        # here turns "0 candidates" into "you typed the wrong path".
        raise CollectorError(
            f"「已知官网清单 CSV」找不到这个文件：{sites!r}。请填写完整路径，"
            "或先导出一次把名单存成 CSV 再填回来。"
        )
    if pages and not _is_file(pages):
        raise CollectorError(
            f"「已保存页面清单 CSV」找不到这个文件：{pages!r}。请填写完整路径。"
        )
    budget = one("sitediscovery_budget") or "40"
    try:
        max_requests = max(0, int(budget))
    except ValueError:
        raise CollectorError(
            f"「候选官网抓取上限」要填整数（0 表示不限）：{budget!r}"
        ) from None
    node: dict = {
        "enabled": True,
        # ``search`` is listed on purpose: the report then states that no search
        # service is available instead of leaving the reader to wonder.
        "sources": ["user_list", "page_links", "wikidata", "search"],
        "pages_per_site": 1,
        "reuse_within_days": _reuse_days(one),
        "budget": {"max_requests": max_requests, "max_seconds": 300},
        "user_list": {"path": sites},
        "page_links": {"manifest": pages},
        # Off unless the operator ticks it: it is free and account-free, but it
        # reaches a public service the operator may not want this run to touch,
        # and its hit rate on small domestic businesses is close to zero.
        "wikidata": {"enabled": one("sitediscovery_wikidata") == "on"},
    }
    return node


def _reuse_days(one) -> int:
    """``sitediscovery.reuse_within_days`` from the screen, default 0.

    0 means every run re-asks. That is the safe direction and it is the value
    every configuration before 034 behaves like, so an operator who never opens
    this field keeps the old behaviour. It is a number of days, not a boolean,
    because "trust a verdict for a week" and "trust it for a year" are different
    promises about a site that may have changed.
    """
    raw = str(one("sitediscovery_reuse_days") or "").strip()
    if not raw:
        return 0
    try:
        days = int(raw)
    except ValueError:
        raise CollectorError(
            f"「已确认判定可复用天数」要填整数天数（0 表示每次重问）：{raw!r}"
        ) from None
    if days < 0:
        raise CollectorError(
            f"「已确认判定可复用天数」不能是负数：{raw!r}（0 表示每次重问）"
        )
    return days


def _local_pages_node(one) -> dict | None:
    """The ``sources_map.local_pages`` entry the screen's fields describe.

    Only written when the operator ticked the 「本地已存网页」 source, so a run
    that did not ask produces exactly the config it did before this existed.

    The address the page was saved from (``origin_url``) is required and named
    when missing: without it a relative ``href`` cannot be resolved and the
    evidence cannot say which site the data came from — the two things that make
    an import auditable rather than a folder of anonymous HTML.
    """
    listing = one("local_listing_path")
    if not listing:
        raise CollectorError(
            "选择了「本地已存网页」来源，但没有填「已存列表页文件」。"
            "请填那个 HTML 文件的完整路径（浏览器另存为得到的 .html/.htm）。"
        )
    if not _is_file(listing):
        raise CollectorError(
            f"「已存列表页文件」找不到这个文件：{listing!r}。请填写完整路径。"
        )
    origin = one("local_origin_url")
    if not origin:
        raise CollectorError(
            "「本地已存网页」需要填「这些页面保存时的网址」。"
            "页面里的链接大多是相对地址，没有原始网址就解不出来；"
            "导出时也无法说明数据来自哪个网站。"
        )
    if "://" not in origin:
        raise CollectorError(
            f"保存时的网址看起来不完整：{origin!r}。请以 http:// 或 https:// 开头。"
        )
    details = one("local_details")
    preset_key = one("local_preset") or "shunqiwang"
    preset = DIRECTORY_PRESETS.get(preset_key) or DIRECTORY_PRESETS["shunqiwang"]
    budget = one("local_budget") or "200"
    try:
        max_detail = max(1, int(budget))
    except ValueError:
        raise CollectorError(f"「最多读取详情页数」要填整数：{budget!r}") from None

    entry: dict = {
        "name": "ui-offline",
        "listing_path": listing,
        "origin_url": origin,
        "max_detail_pages": max_detail,
    }
    # 034: the row-reading knobs travel with the preset, because they are facts
    # about the site, not choices the operator is equipped to make. They land in
    # the generated YAML verbatim, so what ran is what a reader can inspect.
    for key in ("detail_url_pattern", "detail_host_scope", "list_selector"):
        if preset.get(key):
            entry[key] = preset[key]
    entry["list_preset"] = preset_key
    # The one row-reading switch that is genuinely the operator's call: the
    # preset knows *which* labels a site uses, but whether to keep the fields a
    # listing row already states is a decision about the output. Default on --
    # turning it off is how a reader reproduces the pre-034 behaviour, not how
    # the product ships.
    entry["read_listing_fields"] = one("local_listing_fields") != "off"
    if preset.get("detail_pattern"):
        entry["detail_pattern"] = preset["detail_pattern"]
    if preset.get("field_selectors"):
        entry["field_selectors"] = dict(preset["field_selectors"])
    if details:
        if not _is_dir(details):
            raise CollectorError(
                f"「详情页文件夹」不是目录或不存在：{details!r}。"
                "可以留空——留空时只会写出列表页上出现的公司名，"
                "并标明没有读它们各自的页面。"
            )
        entry["details_dir"] = details
    stated = one("local_observed_at")
    if stated:
        # Only ever the operator's own statement. The file's mtime is when it
        # was downloaded, not when the page was published, so it is not used.
        entry["observed_at"] = stated
    return {"entries": [entry]}


def _is_dir(value: str) -> bool:
    return bool(value) and pathlib.Path(value).expanduser().is_dir()


def _is_file(value: str) -> bool:
    """Whether a screen path points at a real file.

    A tiny named helper so the error messages above read as sentences about a
    file rather than as ``pathlib.Path(...).is_file()`` noise at the call site.
    """
    return bool(value) and pathlib.Path(value).expanduser().is_file()


def _checkbox_group(name: str, options: tuple[tuple[str, str], ...], checked) -> str:
    """A tick group. ``width: auto`` keeps a checkbox from filling the row.

    056 P2: the group leads with a hidden empty value for the same name. A
    browser submits nothing at all for an unticked box, so without it "every
    box unticked" and "this page never had the group" reach the server as
    exactly the same request -- and the server could only guess, which is how
    switching every source off came back with the region's defaults ticked
    again. With the hidden value the group is *always* submitted, so an
    emptied group arrives as present-but-blank and can be told apart from an
    absent one (which still means "never filled in", and still gets defaults).
    The blank is filtered out wherever the values are read.
    """
    ticked = {str(item) for item in checked}
    return (
        f'<input type="hidden" name="{html.escape(name)}" value="">'
        + "".join(
            f'<label class="tick"><input type="checkbox" name="{html.escape(name)}" '
            f'value="{html.escape(value)}"{" checked" if value in ticked else ""}>'
            f"{html.escape(label)}</label>"
            for value, label in options
        )
    )


# --- what a failed submission keeps ----------------------------------------

#: The form's own fields, carried back to the browser after a refusal.
#:
#: 054: a refused submit redirected to a bare ``/``, so every field the person
#: had filled in was gone and the country fell back to the DE default. Fixing
#: one typo meant retyping the whole form -- and the reset country made the
#: next submit fail differently, which reads as "the tool is broken".
#:
#: The values are carried in the redirect's own query string rather than in
#: server memory: nothing is retained about a request the server refused, so a
#: second browser tab or a server restart cannot mix two people's half-filled
#: forms together. A URL is a modest carrier, so the text fields are included
#: whole and long ones are not truncated (they are a person's own typing, not a
#: secret: no password ever reaches this form).
ECHO_FIELDS = (
    "industry", "city", "country", "province", "district", "geo", "query",
    "target_count", "mode", "formats", "enrich",
    "directory_url", "directory_detail_pattern", "directory_preset",
    "match_profile", "match_keywords", "match_exclude_keywords",
    "sitediscovery", "sitediscovery_sites", "sitediscovery_pages",
    "sitediscovery_budget", "sitediscovery_reuse_days", "sitediscovery_wikidata",
    "local_pages", "local_listing_path", "local_origin_url", "local_details",
    "local_preset", "local_observed_at", "local_budget", "local_listing_fields",
    "csv_list", "csv_path", "csv_columns", "csv_id_column", "csv_delimiter",
    "csv_origin_url", "csv_origin_name", "csv_source_time", "csv_source_time_unknown",
)

#: The multi-value fields. Kept separately because they are ticks, and an empty
#: tick set and an absent one mean different things (``many()`` falls back to
#: the region default only when nothing was submitted).
ECHO_LISTS = ("sources", "languages")

#: A submission fingerprint, so a stale error can be told apart from a fresh
#: one. Deliberately *not* a hash of everything: it covers exactly the fields
#: whose change makes an earlier refusal obsolete.
FINGERPRINT_FIELDS = (
    "industry", "city", "country", "province", "district", "geo",
    "directory_url", "directory_preset", "csv_path", "local_listing_path",
    "sitediscovery", "sources", "languages", "mode",
)


def _form_fingerprint(form: dict[str, list[str]]) -> str:
    """A short, stable name for *this* submission's inputs."""
    import hashlib

    parts: list[str] = []
    for key in FINGERPRINT_FIELDS:
        values = [str(v).strip() for v in (form.get(key) or [])]
        parts.append(f"{key}={'|'.join(values)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _echo_query(form: dict[str, list[str]], *, error: str, fingerprint: str) -> str:
    """The error redirect: the message plus every value the person had typed.

    A tick group that was submitted **empty** carries an explicit marker
    (``empty_<key>=1``), because a browser omits an unticked checkbox
    altogether and an omitted key is indistinguishable from "this page was
    never submitted". Without the marker, unchecking every source and being
    refused came back with the region's defaults ticked again -- and for
    sources that means the run would go online through Google/OSM after the
    person had just switched them all off (056 P2).
    """
    pairs: list[tuple[str, str]] = [("error", error), ("fp", fingerprint)]
    for key in ECHO_FIELDS:
        values = [str(v) for v in (form.get(key) or []) if str(v).strip()]
        if values:
            pairs.append((f"v_{key}", values[0]))
    for key in ECHO_LISTS:
        values = [str(v) for v in (form.get(key) or []) if str(v).strip()]
        if values:
            pairs.extend((key, value) for value in values)
        elif key in form:
            # Only a group that was *in* the submission can have been emptied.
            # Marking an absent one would make the page show the region's
            # defaults as unticked -- the same silent change, the other way
            # round -- on a request that simply never mentioned the group.
            pairs.append((_empty_marker(key), "1"))
    return "/?" + urllib.parse.urlencode(pairs)


def _empty_marker(key: str) -> str:
    """The query key that says "this tick group was submitted with none ticked"."""
    return f"empty_{key}"


#: What each tick group is called, in the sentence shown when one is empty.
#: Kept next to ``ECHO_LISTS`` so a new group cannot be added without a name.
_GROUP_LABELS_ZH = {"sources": "「来源」", "languages": "「导出语言」"}


def _EMPTY_GROUP_NOTE(empty_groups: list[str]) -> str:
    """Why a run with no source (or no language) is not started.

    One sentence per empty group, and the sentence says what the group is
    *for*: sources are where anything comes from at all, languages are which
    readable workbooks get written. Neither is a detail to be defaulted.
    """
    named = " 和 ".join(_GROUP_LABELS_ZH.get(key, key) for key in empty_groups)
    reasons = []
    if "sources" in empty_groups:
        reasons.append(
            "来源决定从哪里采集，一个都没选就没有可采集的东西；"
            "本工具不会替你补回默认来源（那会在你刚全部取消后仍然联网）。"
        )
    if "languages" in empty_groups:
        reasons.append("导出语言决定生成哪些可读表格，一个都没选就不会生成任何阅读版文件。")
    return f"{named}一个都没有勾选。{' '.join(reasons)}请在上面至少勾选一个再开始。"


#: The fields a pre-flight reminder is *about*. Changing one of these makes an
#: earlier acknowledgement obsolete; re-typing the industry or the target count
#: does not, so confirming once is still enough for that.
PREFLIGHT_FIELDS = (
    "sources", "directory_url", "directory_preset",
    "local_listing_path", "local_origin_url", "local_preset",
)


def _preflight_fingerprint(form: dict[str, list[str]]) -> str:
    """A short name for the setup a reminder was shown about.

    056 P2: the acknowledgement used to be a bare ``confirmed=1``, so it
    outlived the very thing it acknowledged -- the person could be shown
    "your preset does not match this URL", change the URL to a *different*
    mismatch, and the next click started the run with no reminder at all.
    Binding it to these fields means the server can tell "I read it and
    resubmitted" apart from "I read it and then changed the setup".
    """
    import hashlib

    parts = [
        f"{key}={'|'.join(str(v).strip() for v in (form.get(key) or []))}"
        for key in PREFLIGHT_FIELDS
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def cleared_groups(query: dict[str, list[str]]) -> set[str]:
    """Which tick groups this submission deliberately emptied.

    Only ever non-empty for a *reprised* form: a first visit has no query at
    all, so it keeps the region's defaults, which is what "defaults" are for.
    """
    return {
        key
        for key in ECHO_LISTS
        if (query.get(_empty_marker(key)) or [""])[0] == "1"
    }


def _form_defaults(query: dict[str, list[str]]) -> dict[str, object]:
    """The page's field defaults: the echoed submission, or nothing.

    Returns a mapping the page template can splat in, so a refusal and a first
    visit are the same code path with different input -- there is one way the
    form gets its values, which is what keeps them from drifting apart.
    """
    values: dict[str, object] = {}
    for key in ECHO_FIELDS:
        found = query.get(f"v_{key}") or []
        values[key] = found[0] if found else ""
    for key in ECHO_LISTS:
        values[key] = list(query.get(key) or [])
    return values


def _form_from_values(values: dict[str, object]) -> dict[str, list[str]]:
    """The inverse of :func:`_form_defaults`: values back to a form shape.

    The page renders from a flat mapping, but the config builders are written
    against the browser's ``dict[str, list[str]]``. Going back through one
    converter keeps the two shapes from growing different rules about what an
    absent field means.
    """
    form: dict[str, list[str]] = {}
    for key, value in values.items():
        if isinstance(value, list):
            form[key] = [str(item) for item in value]
        elif value:
            form[key] = [str(value)]
    return form


def _stale_note(fingerprint: str, query: dict[str, list[str]]) -> str:
    """A note that an earlier refusal no longer matches what is on screen.

    The fingerprint carried in the URL is compared with the one the *echoed*
    values on this page produce. When they agree, the message above describes
    exactly the form the person is looking at -- a current refusal, and it is
    shown as one. When they differ, the person has edited the form since the
    refusal (a resubmit carries its own matching fingerprint, so an ordinary
    resubmit of unchanged fields stays "current"), and the honest presentation
    is to say the message is about the previous submission.

    The message is never silently dropped: "why did that fail?" still matters
    even after the field has been corrected.
    """
    if not fingerprint:
        return ""
    echoed = _form_defaults(query)
    if _form_fingerprint(_form_from_values(echoed)) == fingerprint:
        return ""
    return (
        "<p class='note' id='stale-error'>上面的报错对应的是<b>之前那次提交</b>；"
        "你已经改过表单，改完再提交一次即可。这条旧报错在下次提交成功后会消失。</p>"
    )



#: How each per-source network mode is described on screen (026 P2.3). The
#: wording says what the program *does*, because "inherit" is not a network
#: the reader can picture.
NETWORK_MODE_LABELS: dict[str, str] = {
    "inherit": "继承运行代理（runtime.proxy；为空时再看系统环境变量）",
    "direct": "直连（忽略 runtime.proxy 与系统环境变量）",
    "proxy": "指定代理",
    "": "未记录（该来源还没跑过）",
}

#: The honest limit of "direct" (026 P2.3). Said on the screen rather than left
#: for the operator to discover: a machine-wide tunnel intercepts the socket no
#: matter what an individual program asks for, and this tool does not touch the
#: system's network settings.
TUN_CAVEAT = (
    "说明：程序里的「直连」只是不使用本工具的代理设置。"
    "如果系统开着全局 TUN / 虚拟网卡，流量仍会走进那条隧道——本工具不修改系统网络设置，"
    "需要真正直连请自行调整。"
)


def _network_mode_text(mode: str, proxy: str) -> str:
    label = NETWORK_MODE_LABELS.get(str(mode or ""), NETWORK_MODE_LABELS[""])
    if str(mode or "") == "proxy" and proxy:
        return f"{label}：{proxy}"
    return label


def _source_status_block(rows: list[dict]) -> str:
    """What each source is doing right now (026 P2.2/P2.3).

    Shows the honest four: how it reaches the network, when it last answered,
    whether it is cooling down and until when, and what went wrong last. A
    source that has never run says so instead of showing blank fields, because a
    blank cell reads as "fine".
    """
    if not rows:
        return f"""
      <label>来源状态</label>
      <p class="hint">还没有任何来源跑过，所以这里没有可显示的成功时间或冷却状态。
        {html.escape(TUN_CAVEAT)}</p>"""
    body = "".join(
        f"<tr><td>{html.escape(str(row.get('source') or ''))}</td>"
        f"<td class='note'>{html.escape(_network_mode_text(row.get('network_mode') or '', row.get('proxy') or ''))}</td>"
        f"<td>{html.escape(_fmt_stamp(row.get('last_success_at')))}</td>"
        f"<td>{_cooldown_cell(row)}</td>"
        f"<td class='note'>{html.escape(_last_error_text(row))}</td></tr>"
        for row in rows
    )
    return f"""
      <label>来源状态（每个来源的最近情况）</label>
      <table><tr><th>来源</th><th>网络方式</th><th>最近成功</th><th>冷却</th><th>最近错误</th></tr>
        {body}</table>
      <p class="hint">「冷却」用的是站点自己回的 Retry-After，本工具不会延长，也不会为了刷新状态再打一次请求。
        {html.escape(TUN_CAVEAT)}</p>"""


def _cooldown_cell(row: dict) -> str:
    if not row.get("cooldown_until"):
        return "<span class='note'>—</span>"
    until = html.escape(_fmt_stamp(row.get("cooldown_until")))
    if row.get("cooling_down"):
        seconds = row.get("cooldown_seconds")
        extra = f"（{int(seconds)} 秒）" if seconds else ""
        return f"<span class='status partial'>冷却中</span> 到 {until}{extra}"
    return f"<span class='note'>已结束（{until}）</span>"


def _last_error_text(row: dict) -> str:
    kind = str(row.get("last_error_kind") or "")
    message = str(row.get("last_error") or "")
    at = _fmt_stamp(row.get("last_error_at")) if row.get("last_error_at") else "—"
    if not message:
        return "—"
    prefix = f"{kind} " if kind else ""
    return f"{at} {prefix}{message}"


def _run_summary_block(
    summary: dict,
    *,
    stop_reason: str,
    stop_note: str,
    criteria_note: str = "",
    acquisition: list[tuple[str, str]] | None = None,
) -> str:
    """The first thing a reader sees on a finished run (026 P2.4).

    Keeps the two numbers apart: how many records were *collected* against the
    target, and — when a matching question was asked — how many of them are
    符合 / 待确认 / 不符合. A target of 30 that matched 4 is not a failure, and
    the card has to be able to say so.

    ``criteria_note`` names the conditions the grouping used, including whether
    the industry vocabulary was verified (026 P2.6): a count of 符合 is only
    readable next to the rule that produced it.

    ``acquisition`` is the same ``本批实际获取方式`` block the workbook prints
    (026 P3.1). It comes from :func:`collector.export.acquisition_lines`, so the
    page cannot describe a batch differently from its own export.
    """
    collected = int(summary.get("collected") or 0)
    target = int(summary.get("target_count") or 0)
    shortfall = int(summary.get("shortfall") or 0)
    reason = _stop_note_text(stop_reason, stop_note)
    rows = [
        # 034 第三批 3.6: six numbers, and they are not interchangeable.
        # 候选 is what the sources yielded; 最终商家 is what survived merging.
        ("候选记录数（合并前）", f"{summary.get('candidates', collected)}",
         "来源给出的原始记录条数，可能含同一家公司的重复"),
        # 061: a run with one source folds that source's own duplicates -- it
        # cannot fold anything "across sources", and saying so credits a second
        # source that took no part in the run.
        ("最终商家数（合并后）", f"{collected}",
         "去重后的商家数；与上面的差额就是被合并掉的重复"),
        ("目标条数", f"{target}", "目标只约束采集数量；来源给不出更多时不强凑"),
    ]
    if shortfall:
        rows.append(("目标缺口", f"{shortfall}", "来源已给不出更多不重复的商家"))
    else:
        rows.append(("目标缺口", "0", "已达到目标条数"))
    rows.append(
        (
            "有联系方式的商家",
            f"{summary.get('with_contact', 0)} / {collected}",
            "有电话或邮箱、且未判为无效/缺失的商家数；不是全部商家都可联系",
        )
    )
    matched = summary.get("matched")
    if matched is None:
        # 061: the run did set a region and an industry -- it is the *grouping*
        # that was switched off. Saying "没有设置" made a deliberate choice look
        # like a missing one.
        rows.append(
            ("符合 / 待确认 / 不符合", "未启用分组", "本次没有启用按条件的分组，不按行业/地区条件分开")
        )
    else:
        rows.append(
            (
                "符合 / 待确认 / 不符合",
                f"{matched} / {summary.get('unknown')} / {summary.get('rejected')}",
                "从已落库的事实重新判定，不是采集时的估计",
            )
        )
    if criteria_note:
        rows.append(("判定条件", criteria_note, "取自本批次自己的配置，不是页面当前的选项"))
    for label, value in acquisition or []:
        rows.append((label, value, "按已落库记录的来源证据统计，不是按配置猜的"))
    body = "".join(
        f"<tr><td>{html.escape(label)}</td><td><b>{html.escape(value)}</b></td>"
        f"<td class='note'>{html.escape(note)}</td></tr>"
        for label, value, note in rows
    )
    return f"""
  <div class="card">
    <h2>本批结果</h2>
    <table><tr><th>项目</th><th>数值</th><th>说明</th></tr>{body}</table>
    <p class="note">停止原因：{html.escape(reason)}</p>
  </div>"""


#: Reader-facing sentences for the run's stop code. The orchestrator stores both
#: the code and its own sentence; this map is the fallback for a code that
#: arrived without one (an older run) so the card never shows a bare identifier.
STOP_REASON_LABELS: dict[str, str] = {
    "target_reached": "已达到目标条数",
    "sources_exhausted": "来源已给不出更多不重复的商家",
    "source_cooling_down": "来源被限速，处于冷却中",
    "budget_exhausted": "请求或时间预算用尽",
    "source_failed": "来源采集失败",
    "no_tasks": "没有可执行的任务",
    "failed": "运行失败",
}


def _stop_note_text(stop_reason: str, stop_note: str) -> str:
    if stop_reason:
        return stop_note or STOP_REASON_LABELS.get(stop_reason, stop_reason)
    return "运行尚未结束（可能仍在进行中）"


def _criteria_note(cfg: Config | None) -> str:
    """The conditions a run's verdicts were decided under, in one line.

    Read from the run's *own* stored config so the line describes what the batch
    actually used. The industry half carries the vocabulary's verified state, so
    an unverified word list cannot be read as an instrument (026 P2.6).
    """
    if cfg is None:
        return ""
    criteria = criteria_from_config(cfg)
    if criteria is None:
        return ""
    return criteria.describe()


def _region_note(cfg: Config | None) -> str:
    """The region/source notes for a config, as a visible line — never silent."""
    if cfg is None:
        return ""
    warnings = cfg.region_warnings()
    if not warnings:
        return ""
    items = "".join(f"<li>{html.escape(text)}</li>" for text in warnings)
    return f'<div class="banner"><b>地区与来源提示：</b><ul>{items}</ul></div>'


def _pick(values: dict | None, key: str, fallback: str) -> str:
    """A dropdown's selected value: what the person chose, else the default.

    Absent and empty mean the same thing here -- a form that was never
    submitted and a form submitted with this box untouched should both show the
    preset, and the generated config already treats them alike. What must not
    happen is a *refusal* quietly resetting a dropdown to its default, which is
    how 054's reported country reset happened.
    """
    chosen = str((values or {}).get(key) or "")
    return chosen or fallback


def _text(values: dict | None, key: str) -> str:
    """A text box's value: exactly what was typed, or nothing.

    Deliberately not falling back to the placeholder: a placeholder is an
    example, not a value, and showing an example as filled-in text is how
    "Milan" would silently become an input the person never chose.
    """
    return html.escape(str((values or {}).get(key) or ""))


def _attr(value: object) -> str:
    """One value rendered into an HTML attribute, escaped exactly once.

    056 P1: a double quote inside ``value="..."`` ends the attribute early and
    starts a new one, so a keyword typed as ``pet "boarding"`` reached the
    browser as ``pet `` plus a stray ``boarding""`` attribute. The person's
    keyword was silently truncated, and *any* attribute could be added the
    same way.

    Escaping belongs at the render boundary and nowhere else: the form value
    and the generated config keep the person's text exactly as typed, and only
    the copy that becomes HTML is quoted. The per-source blocks already do
    this through :func:`_text`; this is the same rule for the fields the
    top-level template interpolates directly.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _domestic_block(values: dict | None = None) -> str:
    """The domestic-source and matching settings, rendered once.

    Everything the ``directory`` source needs sits here so the China path is a
    form a person can fill, not a YAML file they have to hand-edit (task book
    021 D.7). The generated YAML is still the authority: the screen writes the
    same keys it shows.

    ``values`` is the echoed submission when the last one was refused, and an
    empty mapping on a first visit (:func:`_form_defaults`). Every box below
    reads it, so a refusal cannot quietly drop the list-page URL the person
    pasted.
    """
    preset = _pick(values, "directory_preset", "b2b168")
    preset_options = "".join(
        f'<option value="{html.escape(key)}"{" selected" if key == preset else ""}>'
        f"{html.escape(entry['label'])}</option>"
        for key, entry in DIRECTORY_PRESETS.items()
    )
    profile = _pick(values, "match_profile", "off")
    profile_options = "".join(
        f'<option value="{html.escape(key)}"'
        f' data-status="{html.escape(_VOCAB_STATUS_ZH[INDUSTRY_PROFILES[key].verified])}"'
        f' data-scope="{html.escape(INDUSTRY_PROFILES[key].scope_note_zh)}"'
        f'{" selected" if key == profile else ""}'
        f">{html.escape(label)}</option>"
        if key in INDUSTRY_PROFILES
        else f'<option value="{html.escape(key)}"{" selected" if key == profile else ""}>'
        f"{html.escape(label)}</option>"
        for key, label in MATCH_PROFILE_LABELS
    )
    notes = "".join(
        f'<p class="hint" data-preset-note="{html.escape(key)}">'
        f"{html.escape(entry['note'])}</p>"
        for key, entry in DIRECTORY_PRESETS.items()
    )
    return f"""
      <div class="row">
        <div><label>目录列表页地址（企业目录来源必填）</label>
          <input name="directory_url" id="directory-url" value="{_text(values, 'directory_url')}"
                 placeholder="https://www.b2b168.com/zhejiang-hangzhou-chongwu/"></div>
        <div><label>详情页链接特征（可留空=用预设）</label>
          <input name="directory_detail_pattern" value="{_text(values, 'directory_detail_pattern')}"
                 placeholder="/c168-\\d+\\.html"></div>
        <div><label>目录站选择器预设</label>
          <select name="directory_preset" id="directory-preset">{preset_options}</select></div>
      </div>
      {notes}
      <p class="hint" id="directory-missing" hidden>
        选了「企业目录」但没填列表页地址：填一个分类列表页网址（不是详情页），否则这个来源一条也采不到。
      </p>
      <div class="row">
        <div><label>导出时按条件分组（021 匹配）</label>
          <select name="match_profile" id="match-profile">{profile_options}</select></div>
        <div><label>额外的行业关键词（可选，逗号分隔）</label>
          <input name="match_keywords" value="{_text(values, 'match_keywords')}" placeholder="水族,鸟食"></div>
        <div><label>排除词（可选，命中即判不符合）</label>
          <input name="match_exclude_keywords" value="{_text(values, 'match_exclude_keywords')}" placeholder="包装机械,工程施工"></div>
      </div>
      <p class="hint" id="match-vocab-status">行业词表状态：不启用，导出与以前完全一样。</p>
      <p class="hint">分组用的是上面「省/市」一栏的地区；导出会多出符合/待确认/不符合三张表，
        并在概览写明各类数量、判定口径与<b>词表是否验证过</b>。留空「不启用」则导出和以前完全一样。
        排除词优先于关键词：一处命中排除词就判「不符合」，不管同时命中了多少行业词；
        排除词必须和行业预设或关键词一起用，只填排除词会被拒绝。
        上表未列的行业请选「不启用」并只填自己的关键词，概览会写明这类判定没有词表可依。</p>"""


def _discovery_block(values: dict | None = None) -> str:
    """Candidate-site discovery (026 P1) as a screen block.

    Deliberately blunt about what this does not do. A screen that offered a
    "find the official site" button would imply a web search; this one asks for
    two files and says so, because the collector has no search service and the
    report must not read as if it had one.
    """
    on = _pick(values, "sitediscovery", "off") == "on"
    wikidata = _pick(values, "sitediscovery_wikidata", "off") == "on"
    return f"""
      <label>候选官网发现（名录没给官网时，从你给的两份文件里找）</label>
      <div class="row">
        <div><label>开关</label>
          <select name="sitediscovery"><option value="off"{" selected" if not on else ""}>关闭</option>
            <option value="on"{" selected" if on else ""}>开启</option></select></div>
        <div><label>候选官网抓取上限（0=不限）</label>
          <input name="sitediscovery_budget" value="{_text(values, 'sitediscovery_budget') or '40'}"
                 inputmode="numeric" placeholder="40"></div>
        <div><label>已确认判定可复用天数（0=每次重问）</label>
          <input name="sitediscovery_reuse_days" value="{_text(values, 'sitediscovery_reuse_days') or '0'}"
                 inputmode="numeric" placeholder="0"></div>
      </div>
      <div class="row">
        <div><label>已知官网清单 CSV（列：<code>stable_key</code>或<code>name</code>, <code>url</code>）</label>
          <input name="sitediscovery_sites" value="{_text(values, 'sitediscovery_sites')}"
                 placeholder="D:\\名单\\已知官网.csv"></div>
        <div><label>已保存页面清单 CSV（列：<code>name</code>, <code>path</code>, <code>page</code>）</label>
          <input name="sitediscovery_pages" value="{_text(values, 'sitediscovery_pages')}"
                 placeholder="D:\\名单\\已存页面.csv"></div>
        <div><label>公开结构化数据查询（Wikidata，免费无需账号）</label>
          <select name="sitediscovery_wikidata"><option value="off"{" selected" if not wikidata else ""}>关闭</option>
            <option value="on"{" selected" if wikidata else ""}>开启</option></select></div>
      </div>
      <p class="hint">本地没有搜索服务，所以这里不提供「全网搜索」。
        确认规则是：候选页自己写的企业名要和名录一致，<b>并且</b>电话或地址至少有一项对得上；
        只有名字对上的一律记为「待确认」，不写进官网字段。目录站/平台/同名异地企业的整站会被直接排除。
        每个候选的判定原因都会存进数据库，导出时一并可查。
        「可复用天数」填 0 时每次运行都重新判定；填大于 0 的天数时，这个天数内<b>已确认</b>过的同一家企业同一网址
        不再重新抓取，判定里会注明是复用；被拒绝或待确认的判定永远不复用。
        公开结构化数据查询读的是 Wikidata 的公开 SPARQL（无需注册、无需密钥），按企业名
        <b>精确</b>匹配后取其「官网」声明；它<b>不是</b>全网搜索，绝大多数中小企业在里面没有条目，
        查到几条会在运行报告里如实写出。<b>名字对上不等于身份对上</b>：条目被声明为企业/公司/组织类别的才算候选，
        同名但不是企业的条目（百科、软件文档等）会被排除并写明排除原因，候选记录里保留条目编号与类型。
        全网搜索仍需付费密钥，本地没有，界面如实标为不可用。</p>"""


def _csv_list_node(one) -> dict:
    """The ``sources_map.csv_list`` entry the screen's fields describe.

    050 P1: the screen could name ``csv_list`` as a source but had nowhere to
    configure it, so the only way to import a file was to hand-edit the YAML or
    run a script. This block closes that, and it keeps the four things 050 asks
    to be kept apart apart:

    * the **input file** — ``path``, the bytes actually read;
    * the **original source** — ``provenance.origin`` (a human-readable name and
      the site it came from) and ``provenance.source_page_url`` when a *column*
      names the page each row was read from;
    * the **source time** — ``provenance.observed_at``, or nothing at all when
      the operator says the source did not state one. There is no default: a
      file's mtime is when it was downloaded, not when the page was published;
    * the **entry channel** — ``acquired_via`` is ``import``, always, because a
      CSV is a file and nothing it produces may read as a page we visited.

    The field mapping is written as the operator typed it, so the generated
    YAML shows which column filled which field instead of a normalised echo.
    """
    path = one("csv_path")
    if not path:
        raise CollectorError(
            "选择了「本地 CSV 清单」来源，但没有填「CSV 文件路径」。"
            "请填那个文件的完整路径（.csv / .tsv / .txt）。"
        )
    if not _is_file(path):
        raise CollectorError(
            f"「CSV 文件路径」找不到这个文件：{path!r}。请填写完整路径。"
        )

    columns: dict[str, str] = {}
    for pair in re.split(r"[,;，；]", one("csv_columns") or ""):
        if not pair.strip():
            continue
        column, _, target = pair.partition("=")
        column = column.strip()
        target = target.strip()
        if not column or not target:
            raise CollectorError(
                f"字段映射这一项看不懂：{pair.strip()!r}。"
                "请按「CSV列名=字段」写，字段只能是 "
                "title/address/phone/website/emails/latitude/longitude/"
                "category/description，多项之间用逗号分隔。"
            )
        if target not in CSV_LIST_FIELDS:
            raise CollectorError(
                f"字段映射里的 {target!r} 不是可填入的字段。可选："
                + "、".join(CSV_LIST_FIELDS)
            )
        columns[column] = target

    node: dict = {"path": path}
    if one("csv_delimiter") == "tab":
        node["delimiter"] = "tab"
    id_column = one("csv_id_column")
    if id_column:
        node["id_column"] = id_column
    if columns:
        node["columns"] = columns

    # The original source, stated separately from the file. ``origin`` is the
    # source *site*; the URL alone is not a name, and a name alone is not a
    # link, so both are carried when both are known -- and neither is invented
    # when it is not.
    origin_url = one("csv_origin_url")
    origin_name = one("csv_origin_name")
    if origin_url and "://" not in origin_url:
        raise CollectorError(
            f"原始来源网址看起来不完整：{origin_url!r}。请以 http:// 或 https:// 开头。"
        )
    if origin_name or origin_url:
        node["provenance"] = {"origin": _origin_label(origin_name, origin_url)}

    stated = "" if one("csv_source_time_unknown") == "on" else one("csv_source_time")
    if stated:
        node.setdefault("provenance", {})["observed_at"] = stated
    return node


def _origin_label(name: str, url: str) -> str:
    """The source site as one readable string: name, and the site it was at.

    A name without a link cannot be checked, and a link without a name is not
    something a person recognises, so a source that gave us both keeps both.
    When only one is known only that one is written — nothing is filled in from
    the other side. A URL alone is written **as given**: deriving a domain from
    it would be a claim about the site that the URL itself does not make.
    """
    name = (name or "").strip()
    url = (url or "").strip()
    if name and url:
        host = urlsplit(url).hostname or ""
        site = registrable_domain(host) or host
        return f"{name}（{site}）" if site else name
    return name or url


def _csv_block(values: dict | None = None) -> str:
    """The CSV list import (050 P1) as a screen block.

    Four separate boxes on purpose. The file, the site it came from, when that
    site published it, and how it entered the collector are different facts, and
    a single "来源" box is how they used to collapse into one wrong sentence.
    The 字段映射 box exists because a real export does not always name its
    columns the way the collector expects.
    """
    options = "、".join(CSV_LIST_FIELDS)
    on = _pick(values, "csv_list", "off") == "on"
    unknown = _pick(values, "csv_source_time_unknown", "off") == "on"
    tab = _pick(values, "csv_delimiter", "comma") == "tab"
    return f"""
      <label>本地 CSV 清单导入（不联网；读你已经有的表格文件）</label>
      <div class="row">
        <div><label>开关</label>
          <select name="csv_list"><option value="off"{" selected" if not on else ""}>关闭</option>
            <option value="on"{" selected" if on else ""}>开启</option></select></div>
        <div><label>CSV 文件路径（必填，.csv/.tsv/.txt）</label>
          <input name="csv_path" value="{_text(values, 'csv_path')}" placeholder="D:\\名单\\hangzhou-pet.csv"></div>
        <div><label>ID 列（可留空；留空时按行号生成）</label>
          <input name="csv_id_column" value="{_text(values, 'csv_id_column')}" placeholder="company_id"></div>
      </div>
      <div class="row">
        <div><label>字段映射（可留空；按「CSV列名=字段」，逗号分隔）</label>
          <input name="csv_columns" value="{_text(values, 'csv_columns')}" style="min-width:420px"
                 placeholder="Firma=title,Anschrift=address,Telefon=phone,Webseite=website"></div>
        <div><label>分隔符</label>
          <select name="csv_delimiter"><option value="comma"{" selected" if not tab else ""}>逗号（自动识别）</option>
            <option value="tab"{" selected" if tab else ""}>制表符 Tab</option></select></div>
      </div>
      <div class="row">
        <div><label>原始来源名称（可留空；例：顺企网）</label>
          <input name="csv_origin_name" value="{_text(values, 'csv_origin_name')}" placeholder="顺企网"></div>
        <div><label>原始来源网址（可留空；例：https://www.11467.com/）</label>
          <input name="csv_origin_url" value="{_text(values, 'csv_origin_url')}" placeholder="https://www.11467.com/"></div>
      </div>
      <div class="row">
        <div><label>来源给出的时间（可留空）</label>
          <input name="csv_source_time" value="{_text(values, 'csv_source_time')}" placeholder="2026-09-20 或 2026-09-20T08:30:00Z"></div>
        <div><label>来源没有给出时间</label>
          <select name="csv_source_time_unknown"><option value="off"{" selected" if not unknown else ""}>否（按左边填的算）</option>
            <option value="on"{" selected" if unknown else ""}>是（导出时写「来源未给出时间」）</option></select></div>
      </div>
      <p class="hint">这里不发任何网络请求，读的就是上面那个文件。
        可填入的字段只有：{options}；留空映射时按列名自动认
        （title/name/company、address/street/adresse、phone/tel/telefon、
        website/url/homepage 等，大小写不敏感）。
        「原始来源」和「输入文件」是两件事：文件是你手上的这份表格，
        原始来源是这份表格里的数据最初来自哪个网站——两者都已知时会一起写进导出，
        只有一个已知就只写那一个，都没有就留空并标明未知，<b>不猜</b>。
        导入时间由系统记录，「来源给出的时间」只在来源确实写了的情况下填；
        没有写就选「来源没有给出时间」，导出会写「来源未给出时间」，
        不会用文件的修改时间或导入当晚冒充采集时间。
        入口渠道固定记为「导入」，导出里不会出现「实时抓取」字样。</p>"""


def _offline_block(values: dict | None = None) -> str:
    """The offline saved-page import (026 P2.1) as a screen block.

    The selectors are the *same* presets the live directory uses, because it is
    the same parser and the same page structure — the only difference is where
    the bytes come from. That is said on the screen so a person does not have to
    learn a second set of field names to read a page they already have.
    """
    preset = _pick(values, "local_preset", "shunqiwang")
    preset_options = "".join(
        f'<option value="{html.escape(key)}"{" selected" if key == preset else ""}>'
        f"{html.escape(entry['label'])}</option>"
        for key, entry in DIRECTORY_PRESETS.items()
    )
    on = _pick(values, "local_pages", "off") == "on"
    keep_fields = _pick(values, "local_listing_fields", "on") == "on"
    return f"""
      <label>本地已存网页导入（不联网；读取你已经保存到本机的页面）</label>
      <div class="row">
        <div><label>开关</label>
          <select name="local_pages"><option value="off"{" selected" if not on else ""}>关闭</option>
            <option value="on"{" selected" if on else ""}>开启</option></select></div>
        <div><label>使用哪套选择器预设</label>
          <select name="local_preset">{preset_options}</select></div>
        <div><label>最多读取详情页数</label>
          <input name="local_budget" value="{_text(values, 'local_budget') or '200'}"
                 inputmode="numeric" placeholder="200"></div>
      </div>
      <div class="row">
        <div><label>已存列表页文件（必填，.html/.htm）</label>
          <input name="local_listing_path" value="{_text(values, 'local_listing_path')}"
                 placeholder="D:\\名单\\已存页面\\hangzhou-pet.html"></div>
        <div><label>这些页面保存时的网址（必填）</label>
          <input name="local_origin_url" value="{_text(values, 'local_origin_url')}"
                 placeholder="https://www.11467.com/hangzhou/search/9092.htm"></div>
      </div>
      <div class="row">
        <div><label>详情页文件夹（可留空）</label>
          <input name="local_details" value="{_text(values, 'local_details')}"
                 placeholder="D:\\名单\\已存页面\\detail"></div>
        <div><label>页面的采集时间（可留空＝未知；不要填下载时间）</label>
          <input name="local_observed_at" value="{_text(values, 'local_observed_at')}" placeholder="留空即可"></div>
        <div><label>保留列表行上已有的字段</label>
          <select name="local_listing_fields"><option value="on"{" selected" if keep_fields else ""}>开启</option>
            <option value="off"{" selected" if not keep_fields else ""}>关闭</option></select></div>
      </div>
      <p class="hint">这里不发任何网络请求。列表页里每一行公司名都会按预设抽出来；
        该行上本来就写着的地址、主营产品、成立时间、注册资本会<b>一并保留</b>
        ——不需要有详情页，也不会因为导入而丢掉。
        如果它对应的详情页也在上面那个文件夹里，就再按同一套选择器把电话、简介等读出来，
        <b>只增补、不覆盖</b>已有字段；两边不一致时保留原值并在结果里标明。
        详情链接与实际是否读取分别记录，导出时每条都会写清「获取方式＝导入」，
        不会伪装成实时采集，也不会用下载时间冒充采集时间。
        文件夹留空时，得到的就是列表行上原有的那些字段。</p>"""


class RunManager:
    """Tracks the child run process; the database tracks everything else."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: subprocess.Popen | None = None
        self._config: str = ""

    def start(self, config_path: pathlib.Path, base_dir: pathlib.Path) -> int:
        with self._lock:
            if self._current is not None and self._current.poll() is None:
                raise RuntimeError("a run is already in progress; stop it first")
            # ``-m collector`` resolves the package from the working directory,
            # so the child must start in the directory that *contains* the
            # package -- the project's ``collector/`` folder in the layout the
            # launcher uses. Deriving it from this file rather than from
            # ``base_dir`` is what lets the screen drive a run against any
            # output directory: with ``base_dir/collector`` the child could only
            # ever import a copy of the package that happened to sit inside the
            # output directory, and an isolated library (an acceptance run, a
            # second project on the same machine) started a run that died on
            # import while the screen said it had started.
            package_parent = pathlib.Path(__file__).resolve().parents[1]
            proc = subprocess.Popen(
                [
                    sys.executable, "-m", "collector", "run",
                    "--config", str(config_path),
                    "--base-dir", str(base_dir),
                ],
                cwd=str(package_parent),
            )
            self._current = proc
            self._config = str(config_path)
            return proc.pid

    def stop(self) -> bool:
        """Kill the whole process tree, the way a crashed shell would.

        The Go scraper is a grandchild of this UI (CLI -> orchestrator ->
        subprocess), so killing only the CLI would leave a collector running
        with nobody watching it. ``taskkill /T`` takes the tree down; the run
        is left ``interrupted`` and resumable, exactly as the stage C
        interruption experiment established.
        """
        with self._lock:
            proc = self._current
            if proc is None or proc.poll() is not None:
                return False
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, check=False,
                )
            else:
                proc.terminate()
            proc.wait(timeout=30)
            self._current = None
            return True

    def running_config(self) -> str:
        with self._lock:
            if self._current is not None and self._current.poll() is None:
                return self._config
            return ""

    def is_running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.poll() is None


#: Sources that genuinely need a search centre. Only these make an unknown city
#: an error: a CSV file, a saved HTML page and a directory category page each
#: state where they are, so demanding coordinates for them blocked a user who
#: had no use for them (054).
GEO_SOURCES = ("google_maps", "openstreetmap")


def needs_geo(sources: Iterable[str]) -> bool:
    """Whether any chosen source needs a coordinate at all."""
    return any(name in GEO_SOURCES for name in sources)


def resolve_location(
    form: dict[str, list[str]], *, sources: Iterable[str] | None = None
) -> tuple[float, float]:
    """Where the run should centre: an explicit coordinate beats the table.

    An unknown city is an error the user sees *when a source needs a centre*.
    Falling back to a hardcoded city would collect the wrong place and look
    like a result — the task book's "不把调试参数和实现细节堆给用户" and honesty
    rules both point the same way: ask instead of guessing.

    054 split three things that used to be one:

    * a city typed into the province box is *reported*, never moved;
    * an unlisted city in a run that never reads a map is not an error at all —
      the coordinate is not used, so it is not demanded;
    * an unlisted city in a map run is still a question, with the two ways out
      spelled out (another spelling of the name, or explicit coordinates).
    """
    def one(key: str) -> str:
        values = form.get(key) or []
        return (values[0] if values else "").strip()

    explicit = one("geo").replace(";", ",")
    if explicit:
        parts = [part.strip() for part in explicit.split(",") if part.strip()]
        if len(parts) != 2:
            raise CollectorError(
                "地图中心格式应为「纬度,经度」，例如 52.52,13.405；收到 "
                f"{explicit!r}。"
            )
        try:
            latitude, longitude = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise CollectorError(
                f"地图中心无法解析为数字：{explicit!r}。应为「纬度,经度」，例如 52.52,13.405。"
            ) from exc
        if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
            raise CollectorError(
                f"地图中心超出范围：纬度须在 -90..90、经度在 -180..180，收到 {explicit!r}。"
            )
        return latitude, longitude

    country = regions.normalize_country(one("country"))
    city = regions.resolve_city(one("city"), country)
    hit = regions.city_centre(city, country)
    if hit is not None:
        return hit

    chosen = list(sources) if sources is not None else list(GEO_SOURCES)
    if not needs_geo(chosen):
        # No chosen source reads a map, so nothing will consult this value. The
        # honest move is to leave it unset rather than invent a city: the run
        # says which sources it used, and the export says the centre was not
        # needed. No provider substitutes 0,0 for a real place (the map sources
        # are not in this run at all).
        return 0.0, 0.0

    # The province box is a documented way to get here by accident (054: 米兰
    # typed one line up, city left empty). Say so, and do not move the value.
    province = one("province")
    if not city:
        if province and regions.city_centre(province, country) is not None:
            raise CollectorError(
                f"「城市」一栏是空的，而「省/直辖市」一栏填的是城市名 {province!r}。"
                f"请把 {province!r} 填到「城市」一栏（本工具不会替你搬动它）；"
                "省/直辖市一栏可以留空，也可以填省份（如「浙江省」）。"
            )
        raise CollectorError(
            "「城市」一栏是空的。请填城市名（例如 柏林 / Berlin、米兰 / Milan），"
            "或在地图中心一栏填「纬度,经度」。"
        )

    # Not in the centre table. Say *why* with what the division data does know,
    # because "I don't know 建德" and "I don't know 建德, and by the way it
    # belongs to 杭州市" lead a person to two different next moves.
    division = regions.lookup_cn_division(city) if regions.is_china_region(country) else None
    if division and division.get("level") == "former":
        raise CollectorError(
            f"{city!r} 是旧区名：{division.get('note') or '区划已调整'}。"
            "请改填现行的区名，或在地图中心一栏填「纬度,经度」。"
        )
    if division and division.get("city_name"):
        raise CollectorError(
            f"{city!r} 是{division.get('city_name')}下辖的县级单位"
            f"（代码 {division.get('code')}），本工具的中心坐标表里没有它。"
            f"可以把城市填成「{division.get('city_name')}」用它的中心，"
            "或在地图中心一栏填「纬度,经度」（地图应用里右键该地即可取到），"
            "本工具不会默默改用其他城市。"
        )
    label = f"{regions.region_label(country)}（{country}）" if country else "未填国家/地区"
    raise CollectorError(
        f"不知道 {city!r}（{label}）的中心坐标。"
        "可以：① 换一种常见写法（如 米兰 / Milan / Milano）；"
        "② 在地图中心一栏填「纬度,经度」（在地图应用里右键该城市即可取到）。"
        "本工具不会默默改用其他城市。若这次只用本地 CSV 或已存网页，"
        "可以取消勾选「Google 地图」与「OpenStreetMap」，就不再需要坐标。"
    )


def preflight_warnings(form: dict[str, list[str]]) -> list[str]:
    """Things worth saying *before* a run starts, without refusing it.

    054 asks for two of these by name. Both describe a run that is legal and
    will start -- refusing them would block a person who knows what they are
    doing -- but that will very likely come back empty, and an empty result
    presented without the reason reads as "the site has no companies".

    Returns sentences in the order they matter; empty when there is nothing to
    say. Nothing here is inferred from a *default*: a mismatch is only reported
    when the URL's host is one of another preset's known hosts, so a URL on a
    site this screen has never heard of passes through quietly.
    """
    def one(key: str) -> str:
        values = form.get(key) or []
        return (values[0] if values else "").strip()

    warnings: list[str] = []
    sources = list(form.get("sources") or [])
    url = one("directory_url")
    preset_key = one("directory_preset") or "b2b168"

    if "directory" in sources and not url:
        warnings.append(
            "选了「企业目录」来源但没填「目录列表页地址」：这个来源一条也采不到，"
            "其余来源不受影响。要采的话填一个分类列表页的完整网址（不是详情页）。"
        )
    elif url and "://" not in url:
        warnings.append(
            f"「目录列表页地址」看起来不是完整网址：{url!r}；应以 http:// 或 https:// 开头。"
        )
    elif url:
        host = (urlsplit(url).hostname or "").lower()
        expected = DIRECTORY_PRESETS.get(preset_key, {}).get("host_hints") or ()
        if expected and not any(host == hint or host.endswith("." + hint) for hint in expected):
            others = [
                (key, entry)
                for key, entry in DIRECTORY_PRESETS.items()
                if key != preset_key
                and any(host == h or host.endswith("." + h) for h in entry.get("host_hints") or ())
            ]
            if others:
                name, entry = others[0]
                warnings.append(
                    f"「目录站选择器预设」选的是 {DIRECTORY_PRESETS[preset_key]['label']}，"
                    f"但地址的域名是 {host!r}，看起来属于「{entry['label']}」。"
                    f"选择器是按站点页面结构写的，选错了会挑不到内容、结果偏少或为空；"
                    f"请把预设改成 {name}（或确认确实要用当前这套）。"
                )
            else:
                warnings.append(
                    f"「目录站选择器预设」是 {DIRECTORY_PRESETS[preset_key]['label']}，"
                    f"但地址域名 {host!r} 不在已知站点里。选择器是按站点写的，"
                    "不匹配时通常取不到值；跑出来记录偏少是选择器的问题，不是这个站点没有公司。"
                )

    # The offline import has the same shape of trap: a preset describes one
    # site's page structure, and a saved page from another site will not match.
    local_path = one("local_listing_path")
    if "local_pages" in sources and local_path:
        local_preset = one("local_preset") or "shunqiwang"
        origin = one("local_origin_url")
        host = (urlsplit(origin).hostname or "").lower() if origin else ""
        expected = DIRECTORY_PRESETS.get(local_preset, {}).get("host_hints") or ()
        if host and expected and not any(host == h or host.endswith("." + h) for h in expected):
            warnings.append(
                f"「本地已存网页导入」的预设是 {DIRECTORY_PRESETS[local_preset]['label']}，"
                f"但页面保存时的网址域名是 {host!r}；选择器不匹配时列表行抽不出来，"
                "会看起来像「页面里没有公司」。请改选对应站点的预设。"
            )
    return warnings


def build_config(form: dict[str, list[str]], *, base_dir: pathlib.Path, name: str) -> Config:
    """Turn screen fields into a validated config. Nothing hidden is added.

    The screen's form maps one-to-one onto config keys a person can also see
    and edit in the generated YAML — the generated file is written next to
    the run and is itself a valid, inspectable starting point.
    """
    def one(key: str, default: str = "") -> str:
        values = form.get(key) or []
        return (values[0] if values else default).strip()

    def many(key: str, *, allowed: tuple[str, ...]) -> list[str]:
        """Every ticked value, in the order ticked, filtered to what exists.

        A checkbox group submits one entry per tick; a stale bookmark that
        carries a source this build does not implement is dropped rather than
        failing the run, the same way an unknown field is ignored.
        """
        seen: list[str] = []
        for value in form.get(key) or []:
            text = str(value).strip()
            if text in allowed and text not in seen:
                seen.append(text)
        return seen

    industry = one("industry") or "dentists"
    city = one("city") or "Berlin"

    # A region may be written as a code (``DE``), a Chinese name (``中国``) or
    # an English name (``Germany``). What is *collected* depends on the resolved
    # code, so an unrecognised value is refused here with the text that was
    # typed — never silently mapped to a country and never falling back to a
    # default region.
    country_typed = one("country") or "DE"
    country = regions.normalize_country(country_typed)
    if not country:
        raise CollectorError(
            f"不认识的国家/地区：{country_typed!r}。可以填 CN、中国、Germany 等，"
            "请改用已知名称或两位 ISO-3166 代码。"
        )

    # Chinese queries read "杭州宠物服务", not "pet services in 杭州"; every other
    # region keeps the existing "industry in city" shape so nothing changes for
    # the overseas flow.
    default_query = (
        f"{city}{industry}" if regions.is_china_region(country) else f"{industry} in {city}"
    )
    query = one("query") or default_query

    mode = one("mode") or "fast"
    if mode not in ("fast", "browser"):
        mode = "fast"
    target_count = one("target_count") or "30"
    try:
        target = max(1, int(target_count))
    except ValueError:
        target = 30
    enrich = one("enrich") == "on"
    formats = [f for f in one("formats", "csv,jsonl,xlsx").split(",") if f] or ["csv", "jsonl", "xlsx"]

    # Sources and languages fall back to the *region's* defaults, not to a
    # hardcoded overseas pair: a China run must not quietly collect through
    # Google/OSM. What was chosen on screen always wins.
    #
    # 056 P2: the fallback applies only when the group was **absent** from the
    # submission. A browser omits an unticked checkbox entirely, so on the face
    # of it "absent" and "emptied" look alike -- but this function is also
    # reachable from a config file and from a hand-built POST, and there the
    # difference is real: ``sources: []`` in a file means "none", and quietly
    # turning that into Google+OSM is exactly the silent-defaults bug. The
    # screen's own path refuses an empty group before it ever gets here.
    sources = many("sources", allowed=SUPPORTED_SOURCES)
    languages = many("languages", allowed=SUPPORTED_LANGUAGES)
    if form.get("sources") is None:
        sources = sources or list(regions.default_sources(country))
    if form.get("languages") is None:
        languages = languages or list(regions.default_languages(country))

    # The directory source is the domestic one and is configured per site, so
    # the screen carries its own fields for it (021 D.7). No per-source network
    # block is written: a source without one inherits the run's proxy, which
    # the runtime section already sets.
    sources_map: dict = {}
    collect_fields = list(DEFAULT_COLLECT_FIELDS)
    if "directory" in sources:
        sources_map["directory"] = {"entries": [_directory_entry(one, target)]}
        # The directory's own profile block is the industry evidence (021 A):
        # without it the industry dimension has nothing but the company name to
        # read, which is exactly the gap round 020's deliverable had.
        if "description" not in collect_fields:
            collect_fields.append("description")

    # The offline importer (026 P2.1) is not a ``sources`` entry with settings
    # under ``sources_map`` only by coincidence: it is chosen the same way, in
    # the same ``sources`` list, and reads the same presets. The switch on the
    # screen is the source itself, so ticking it *is* listing it.
    if one("local_pages") == "on":
        if "local_pages" not in sources:
            sources = [*sources, "local_pages"]
        sources_map["local_pages"] = _local_pages_node(one)
        if "description" not in collect_fields:
            collect_fields.append("description")

    # 050 P1: the CSV list is configured here rather than by hand-editing the
    # YAML. It is the same ``sources`` entry and the same ``sources_map`` node
    # the config format has always had, so nothing new is invented -- only a
    # place on the screen to fill it in.
    if one("csv_list") == "on":
        if "csv_list" not in sources:
            sources = [*sources, "csv_list"]
        sources_map["csv_list"] = _csv_list_node(one)
        if "description" not in collect_fields:
            collect_fields.append("description")

    latitude, longitude = resolve_location(form, sources=sources)

    raw = {
        "name": name,
        "industry": industry,
        "location": {
            "query": query,
            "country": country_typed,
            "city": city,
            "province": one("province"),
            "district": one("district"),
            "geo": [latitude, longitude],
        },
        "target_count": target,
        "sources": sources,
        **({"sources_map": sources_map} if sources_map else {}),
        "collect": {"mode": mode, "fields": collect_fields},
        "enrich": {
            "enabled": enrich,
            "fields": ["title", "emails", "phone", "address", "website"] if enrich else [],
            "pages_per_site": 2 if enrich else 0,
            "budget": {"max_requests": 60, "max_seconds": 300},
        },
        "export": {
            "fields": list(DEFAULT_EXPORT_FIELDS),
            "formats": formats,
            "languages": languages,
            "directory": "export",
        },
        "runtime": {
            "concurrency": 1,
            "timeout_seconds": 300,
            "inactivity_seconds": 60,
            "proxy": __import__("os").environ.get("COLLECTOR_PROXY", ""),
            "go_binary": str(base_dir / "google-maps-scraper" / "bin" / "google-maps-scraper.exe"),
            "budget": {"max_requests": 0, "max_seconds": 0},
        },
        "validation": {"rules": {}},
        "output": {"directory": "runs", "database": "collector.sqlite", "raw_directory": "raw"},
    }
    # The matching question, when the screen asked one (021 A/D.7). It changes
    # nothing about what is collected: ``match`` is a non-collection section.
    match_node = _match_node(one)
    if match_node:
        raw["match"] = match_node
    # Candidate-site discovery (026 P1). Like ``match`` it is written only when
    # the operator asked for it, so a run that did not ask produces the same
    # config -- and the same fingerprint -- as before the feature existed.
    discovery = _sitediscovery_node(one)
    if discovery:
        raw["sitediscovery"] = discovery
    return Config(validate(raw, source=f"<local-ui:{name}>"), source=f"<local-ui:{name}>")


def write_config(cfg: Config, path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    import yaml  # local import: only this path needs it

    # ``validate`` adds a runtime-only ``source_path`` key that ``load``
    # refuses on re-read, and a ``match`` key that is None when the run asked
    # no matching question; the written file keeps exactly the keys a
    # hand-written config would have.
    payload = {
        key: value
        for key, value in cfg.data.items()
        if key != "source_path" and value is not None
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _fmt_stamp(text) -> str:
    if not text:
        return "—"
    raw = str(text).replace("T", " ").replace("+00:00", " UTC")
    return raw[:19]


def _fmt_size(path: pathlib.Path) -> str:
    """A human-readable size. One division per unit step, never two.

    The previous version divided the running value inside the same branch it
    formatted, so a 28 KB file was divided twice and shown as "0.0 KB". The
    value is now converted once and then formatted.
    """
    try:
        size = float(path.stat().st_size)
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{size:.0f} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>线索采集控制台</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; background: #f5f6f8; color: #1f2329; }}
header {{ background: #1f3a5f; color: #fff; padding: 14px 22px; display: flex; align-items: baseline; gap: 14px; }}
header h1 {{ font-size: 18px; margin: 0; font-weight: 600; }}
header .sub {{ font-size: 12px; opacity: .75; }}
main {{ max-width: 980px; margin: 18px auto; padding: 0 16px 40px; }}
.card {{ background: #fff; border: 1px solid #e3e6ea; border-radius: 8px; padding: 16px 18px; margin-bottom: 16px; }}
.card h2 {{ font-size: 15px; margin: 0 0 12px; }}
label {{ display: block; font-size: 13px; margin: 8px 0 3px; color: #444; }}
input, select {{ width: 100%; padding: 7px 9px; border: 1px solid #c9ced6; border-radius: 6px; font-size: 14px; }}
.row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 0 14px; }}
button {{ background: #1f3a5f; color: #fff; border: 0; border-radius: 6px; padding: 9px 18px; font-size: 14px; cursor: pointer; }}
button.stop {{ background: #a33; }}
button.ghost {{ background: #eef1f4; color: #1f2329; }}
button:disabled {{ background: #b9c2cc; cursor: not-allowed; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ text-align: left; padding: 7px 8px; border-bottom: 1px solid #eceff2; }}
th {{ color: #666; font-weight: 600; font-size: 12px; }}
.status {{ display: inline-block; padding: 1px 9px; border-radius: 10px; font-size: 12px; }}
.status.completed {{ background: #e2f3e5; color: #1c6b32; }}
.status.failed {{ background: #fbe4e4; color: #a02020; }}
.status.running, .status.partial, .status.interrupted {{ background: #fff3d6; color: #8a5a00; }}
.note {{ font-size: 12px; color: #777; margin-top: 8px; }}
.banner {{ background: #fdecea; border: 1px solid #f5c6c0; color: #8a1c10;
          border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; font-size: 14px; }}
.banner.stale {{ background: #fdf6e3; border-color: #e8d9a8; color: #6b5518; }}
form.inline {{ display: inline; }}
.muted {{ color: #888; }}
a {{ color: #1f3a5f; }}
.tick {{ display: inline-flex; align-items: center; gap: 6px; margin: 4px 16px 4px 0;
        font-size: 13px; color: #444; }}
.tick input {{ width: auto; margin: 0; }}
.hint {{ font-size: 12px; color: #777; margin: 2px 0 6px; }}
.table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
/* 026 P3.3: a phone in portrait. The form and the download links are the only
   things that matter there, so fields go full width, tap targets grow, and a
   wide table scrolls sideways instead of being clipped. This is local
   responsive CSS only -- no public deployment, nothing security-related. */
@media (max-width: 560px) {{
  header {{ flex-wrap: wrap; gap: 4px 10px; padding: 12px 14px; }}
  main {{ margin: 10px auto; padding: 0 10px 28px; }}
  .card {{ padding: 12px; border-radius: 6px; }}
  .row {{ grid-template-columns: 1fr; }}
  button {{ width: 100%; padding: 12px 16px; margin: 6px 0 0; }}
  input, select {{ padding: 10px; font-size: 16px; }}  /* 16px stops iOS zoom */
  table {{ min-width: 460px; }}
}}
</style>
</head>
<body>
<header>
  <h1>线索采集控制台</h1>
  <span class="sub">本地界面 · 只监听 127.0.0.1 · collector {version}</span>
</header>
<main>
  {error_banner}
  {preflight_banner}
  <div class="card">
    <h2>1. 选择任务</h2>
    <form method="post" action="/runs">
      <div class="row">
        <div><label>行业（中文或英文都行；国内填中文，如 宠物服务）</label><input name="industry" value="{industry}" placeholder="dentists / 宠物服务"></div>
        <div><label>城市</label><input name="city" value="{city}" placeholder="Berlin / 杭州"></div>
        <div><label>国家/地区（填 中国 / CN / Germany 均可）</label><input name="country" value="{country}" placeholder="中国 或 DE"></div>
      </div>
      <div class="row">
        <div><label>省/直辖市（可留空；中国地区建议填）</label><input name="province" value="{province}" placeholder="浙江省"></div>
        <div><label>区/县（可留空）</label><input name="district" value="{district}" placeholder="西湖区"></div>
      </div>
      <div class="row">
        <div><label>地图中心「纬度,经度」（留空=按城市自动；未知城市必填）</label><input name="geo" value="{geo}" placeholder="52.52,13.405"></div>
        <div><label>搜索词（留空则按行业+城市自动生成）</label><input name="query" value="{query}" placeholder="dentists in Berlin / 杭州宠物服务"></div>
        <div><label>目标条数</label><input name="target_count" value="{target_count}" inputmode="numeric"></div>
        <div><label>模式</label>
          <select name="mode">
            <option value="fast"{fast_selected}>fast（HTTP，快，推荐）</option>
            <option value="browser"{browser_selected}>browser（真实浏览器，慢，email 需要它）</option>
          </select>
        </div>
      </div>
      <label>来源（可多选；中国地区默认「企业目录」，因为 Google/OSM 不是国内方案）</label>
      <div>{sources_boxes}</div>
      {domestic_block}
      <label>导出语言（可多选；中国默认中文，海外默认英文+双语）</label>
      <div>{languages_boxes}</div>
      <div class="row">
        <div><label>导出格式</label><input name="formats" value="{formats}" placeholder="csv,jsonl,xlsx"></div>
        <div><label>官网补全（D 阶段，多花请求）</label>
          <select name="enrich"><option value="off"{enrich_off}>关闭</option><option value="on"{enrich_on}>开启</option></select>
        </div>
      </div>
      {discovery_block}
      {offline_block}
      {csv_block}
      {status_block}
      <p class="note">国内怎么试：国家/地区填「中国」，城市填「杭州」这类城市名，来源只勾「企业目录」，
        导出语言勾「中文」；行业填中文（如 宠物服务）。</p>
      <p class="note">生成的任务配置会写入 <code>verification/ui/generated/</code>，可直接打开查看和修改。</p>
      {confirm_field}
      <button type="submit" id="start-button"{start_disabled}>开始采集</button>
      <p class="note">任务在服务端跑：<b>关掉这个页面（或合上手机）不会取消采集</b>，
        回来刷新就能看到进度。要停就在下面那一行的「详情」里点「停止」。</p>
    </form>
    <span id="running-note" class="note">{running_note}</span>
    {region_note}
    {empty_note}
  </div>

  <div class="card">
    <h2>2. 运行记录</h2>
    <div class="table-wrap">{table}</div>
  </div>
</main>
<script>
// Moderate auto-refresh: while a run is active, poll the status endpoint and
// update the badges in place. It never reloads the page (so a half-filled form
// is never wiped) and it never posts anything (so it can never start, stop or
// resubmit a task). Once nothing is running it stops polling entirely.
(function () {{
  var timer = null;
  var note = document.getElementById('running-note');
  var noteText = note ? note.innerHTML : '';
  function paint(data) {{
    // A feed that could not be read says so instead of pretending: repainting
    // from a half-empty answer would claim "nothing is running" on no evidence.
    if (data.error) {{ return; }}
    var active = false;
    (data.runs || []).forEach(function (run) {{
      var el = document.querySelector('[data-run-status="' + run.id + '"]');
      if (el) {{
        el.textContent = run.status_text || run.status;
        el.className = 'status ' + run.status;
      }}
      if (run.active) {{ active = true; }}
    }});
    var button = document.getElementById('start-button');
    if (button) {{ button.disabled = !!data.running; }}
    // The server rendered the sentence once (026 P3.5 keeps it in one place:
    // BUSY_NOTE). The poll only has to put it back or clear it.
    if (note) {{ note.innerHTML = data.running ? noteText : ""; }}
    if (!active && !data.running && timer) {{ clearInterval(timer); timer = null; }}
  }}
  function poll() {{
    fetch('/api/runs', {{cache: 'no-store'}})
      .then(function (r) {{ return r.json(); }})
      .then(paint)
      .catch(function () {{}});
  }}
  if (document.querySelector('[data-run-status]')) {{ timer = setInterval(poll, 3000); }}

  // The domestic source needs a list URL. The server refuses a directory run
  // without one; this makes the same rule visible *before* the click and shows
  // the matching preset note, so a person is not sent to an error page to learn
  // what the field wanted.
  var form = document.querySelector('form[action="/runs"]');
  if (form) {{
    var urlField = document.getElementById('directory-url');
    var hint = document.getElementById('directory-missing');
    var preset = document.getElementById('directory-preset');
    function directoryTicked() {{
      var boxes = form.querySelectorAll('input[name="sources"]');
      for (var i = 0; i < boxes.length; i++) {{
        if (boxes[i].value === 'directory' && boxes[i].checked) {{ return true; }}
      }}
      return false;
    }}
    function refreshHint() {{
      var missing = directoryTicked() && urlField && !urlField.value.trim();
      if (hint) {{ hint.hidden = !missing; }}
    }}
    function refreshPresetNote() {{
      var notes = document.querySelectorAll('[data-preset-note]');
      for (var i = 0; i < notes.length; i++) {{
        notes[i].hidden = preset ? notes[i].getAttribute('data-preset-note') !== preset.value : true;
      }}
    }}
    if (urlField) {{ urlField.addEventListener('input', refreshHint); }}
    form.addEventListener('change', function (event) {{
      if (event.target && event.target.name === 'sources') {{ refreshHint(); }}
      if (event.target === preset) {{ refreshPresetNote(); }}
    }});
    if (preset) {{ preset.addEventListener('change', refreshPresetNote); }}
    refreshHint();
    refreshPresetNote();
    form.addEventListener('submit', function (event) {{
      if (directoryTicked() && urlField && !urlField.value.trim()) {{
        event.preventDefault();
        urlField.focus();
        if (hint) {{ hint.hidden = false; }}
      }}
    }});
  }}

  // 026 P2.6: say which vocabulary is in force and whether it was verified,
  // on the same screen that chooses it. A verdict a person cannot date is a
  // verdict they cannot trust; the export repeats this line.
  var profileSelect = document.getElementById('match-profile');
  var vocabStatus = document.getElementById('match-vocab-status');
  function refreshVocabStatus() {{
    if (!vocabStatus || !profileSelect) {{ return; }}
    var option = profileSelect.options[profileSelect.selectedIndex];
    var chosen = option ? option.getAttribute('data-status') : '';
    var scope = option ? option.getAttribute('data-scope') : '';
    if (!profileSelect.value) {{
      vocabStatus.textContent = '行业词表状态：不启用，导出与以前完全一样。';
    }} else if (chosen) {{
      vocabStatus.textContent = '行业词表状态：' + chosen + '。范围：' + scope;
    }} else {{
      vocabStatus.textContent = '行业词表状态：未验证规则。'
        + '本版本不认识这个预设，只会用你填的关键词与排除词，结果不带有任何词表验证。';
    }}
  }}
  if (profileSelect) {{
    profileSelect.addEventListener('change', refreshVocabStatus);
    refreshVocabStatus();
  }}

  // 054: an error message must describe the submission it came from. The
  // server decides this on load (a mismatched fingerprint marks the message
  // stale), but a person *editing* the form after reading the error has not
  // submitted anything yet -- so the browser marks the message as no longer
  // matching the form the moment any field changes. It is never hidden: the
  // banner stays, with a line saying the fix is not submitted yet. That is the
  // honest reading of "改完了但还没提交", and it stops an old error from being
  // mistaken for the verification result of the shown values.
  var banner = document.getElementById('submit-error');
  var stale = document.getElementById('stale-error');
  if (banner && form && !banner.classList.contains('stale')) {{
    var markStale = function () {{
      if (banner.classList.contains('stale')) {{ return; }}
      banner.classList.add('stale');
      banner.insertAdjacentHTML(
        'beforeend',
        '<p class="note">（这是上一次提交的报错；你已改动表单，尚未重新提交。）</p>'
      );
    }};
    form.addEventListener('input', markStale);
    form.addEventListener('change', markStale);
  }}
}})();
</script>
</body>
</html>"""


#: 026 P3.2: what a stored run status means to the person reading the screen,
#: plus what to do next. The stored identifier stays the CSS class and the API
#: field, so the colours and the polling keep working; only the words change.
#:
#: "来源冷却" is deliberately *not* here: it is a property of a site, not of a
#: run, and the source-status card owns it. Putting it in this table would let a
#: cooldown be reported as a run outcome.
RUN_STATUS_LABELS: dict[str, tuple[str, str]] = {
    "pending": ("待开始", "点上面的「开始采集」后回来看这一行。"),
    "running": ("运行中", "采集在服务端跑，关掉这个页面不会取消任务，可以回来再看。"),
    "interrupted": ("已中断", "点进详情页的「恢复/重跑」从断点继续，已采到的记录保留。"),
    "partial": ("部分完成", "有任务没跑完：详情页的错误列表写明是哪个来源、什么原因；可恢复或换来源重跑。"),
    "completed": ("已完成", "在详情页下载导出文件；只想换条件看数就用「重新导出（不重抓）」。"),
    "failed": ("失败", "详情页的错误列表写明失败阶段与原因；已落库的记录与证据都还在。"),
}


def _run_status_text(status: object) -> tuple[str, str]:
    """``(中文状态, 下一步)`` for a stored status, never inventing a label."""
    raw = str(status or "").strip()
    label, hint = RUN_STATUS_LABELS.get(raw, (raw or "未知", ""))
    return label, hint


#: 026 P3.5: what the screen says when a second run is asked for while one is
#: already going. One sentence, used by the disabled-button note and by the
#: refusal path, so the reason does not change shape between them.
BUSY_NOTE = "已经有一个采集在跑：先停止它，或等它跑完，再开始新的。"

#: 026 P3.5: a run whose process is gone but whose row still says ``running``.
#: 「停止」 kills the process tree (so does closing the console window), and a
#: killed process never gets to write ``interrupted`` — the row would say
#: 运行中 forever. This pair is what the screen shows instead.
_ORPHANED_RUN = (
    "已中断（进程不在了）",
    "本界面启动过这个任务，但它现在没有对应进程——多半是点了「停止」或关过窗口。"
    "点详情页的「恢复/重跑」从断点继续，已采到的记录保留。",
)

#: The same fact, one level down: a stage of such a run. Kept as its own string
#: because the sentence is about a stage row, not the run row.
ORPHANED_STAGE_LABEL = "已中断（进程不在了）"


def _comparable_path(value: object) -> str:
    """One config path in a single spelling, for comparing two of them.

    The path is stored exactly as it was handed to the CLI (``..\\verification\\
    ui\\generated\\x.yaml``), so two spellings of the same file must compare
    equal. Windows paths are case-insensitive, hence ``casefold``.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(pathlib.Path(text).resolve()).casefold()
    except OSError:  # pragma: no cover - a path length/permission failure
        return text.casefold()


def _config_belongs_to_screen(config_source: object, own_dir: pathlib.Path | None) -> bool:
    """Whether a run's config file sits in this screen's own config directory.

    "In", not "directly in": the file name is built from the industry field, and
    a name containing a separator makes ``export_root / name`` a *sub*directory —
    that is what ``run.config_filename`` now prevents, but a config written
    before it exists must not be mistaken for a run started somewhere else.
    """
    raw = str(config_source or "").strip()
    if not raw or not own_dir:
        return False
    parent = pathlib.Path(_comparable_path(raw)).parent
    root = pathlib.Path(_comparable_path(own_dir))
    return parent == root or parent.is_relative_to(root)


#: 026 P3.5: what may survive from a run's name into its config file name. The
#: name comes from the industry field, so it is free text; everything that means
#: something to a filesystem is replaced. CJK, ASCII letters/digits and the
#: full-width brackets a person actually types are kept, because the file name is
#: also what a person reads in the generated directory.
_SAFE_FILENAME_KEEP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff（）()._+\- ]+")


def config_filename(name: object, *, stamp: str) -> str:
    """A file name for a run's generated config, with nothing path-like in it.

    Found by the 026 P3.5 bat end-to-end run: an industry of
    ``宠物服务（停止/恢复夹具）`` made ``export_root / f"{name}-{stamp}.yaml"``
    a path inside a subdirectory called ``宠物服务（停止``, which ``write_config``
    then created. The same shape would let ``..`` write the config outside the
    project. The *industry itself* is untouched — it is stored verbatim inside
    the YAML; only the file name is made safe.
    """
    cleaned = _SAFE_FILENAME_KEEP.sub("-", str(name or "")).strip("-. ")
    return f"{cleaned or 'run'}-{stamp}.yaml"


def run_status_view(
    row, *, live_config: str = "", own_dir: pathlib.Path | None = None
) -> tuple[str, str]:
    """``(中文状态, 下一步)`` for one stored run row, adapted to what this screen knows.

    The row carries the status the run's own process wrote. This screen knows one
    extra fact the row cannot: whether it still has a process for that
    configuration. Two conditions must both hold before the careful label is
    used, and both are about *this* screen's own knowledge:

    * the run was started from this screen's generated config directory — a run
      somebody started from the terminal is not this screen's to reinterpret;
    * no live process of this server is working on that same configuration.

    Only then does "运行中" get replaced: otherwise the page would promise a
    collection is running while nothing is, which is the one thing the screen is
    not allowed to do. The raw value stays visible on the detail page.
    """
    status = str(row["status"] or "").strip()
    if status == "running":
        config = str(row["config_source"] or "")
        mine = _config_belongs_to_screen(config, own_dir)
        alive = _comparable_path(config) != "" and _comparable_path(
            config
        ) == _comparable_path(live_config)
        if mine and not alive:
            return _ORPHANED_RUN
    return _run_status_text(status)


#: 026 P3.2: the pipeline stage names and their states, in Chinese. A reader who
#: is told "sitediscovery: skipped" learns nothing; the identifier stays in the
#: title attribute so a bug report can still quote it exactly.
STAGE_NAME_LABELS: dict[str, str] = {
    "discover": "采集（来源抓取）",
    "ingest": "入库（写记录与证据）",
    # 061: the stage folds duplicates whatever the source count is; a single
    # source listing one business twice is merged here too, so the label says
    # "同一商家归类" without crediting a second source.
    "merge": "合并（同一商家归类）",
    "sitediscovery": "候选官网发现",
    "enrich": "网站补全（联系信息）",
    "export": "导出（生成下载文件）",
}

STAGE_STATUS_LABELS: dict[str, str] = {
    "pending": "待开始",
    "running": "运行中",
    "completed": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "skipped": "已跳过（条件不满足）",
    "interrupted": "已中断",
}


def _stage_text(stage: object, status: object) -> tuple[str, str]:
    raw_stage = str(stage or "")
    raw_status = str(status or "")
    return (
        STAGE_NAME_LABELS.get(raw_stage, raw_stage or "未知阶段"),
        STAGE_STATUS_LABELS.get(raw_status, raw_status or "未知状态"),
    )


def _row_cells(
    row, *, live_config: str = "", own_dir: pathlib.Path | None = None
) -> str:
    status = str(row["status"])
    label, hint = run_status_view(row, live_config=live_config, own_dir=own_dir)
    run_id = int(row["id"])
    return (
        f"<tr><td>{run_id}</td>"
        f"<td><span class='status {html.escape(status)}' data-run-status='{run_id}'"
        f" title='{html.escape(hint)}'>{html.escape(label)}</span></td>"
        f"<td>{html.escape(str(row['started_at'] or '')[:19])}</td>"
        f"<td>{int(row['target_count'])}</td>"
        f"<td><a href='/runs/{run_id}'>详情</a></td></tr>"
    )


DETAIL_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>运行 {run_id} · 线索采集控制台</title>
<style>
body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; background: #f5f6f8; color: #1f2329; }}
header {{ background: #1f3a5f; color: #fff; padding: 14px 22px; }}
header a {{ color: #cfe0f5; font-size: 13px; text-decoration: none; }}
main {{ max-width: 980px; margin: 18px auto; padding: 0 16px 40px; }}
.card {{ background: #fff; border: 1px solid #e3e6ea; border-radius: 8px; padding: 16px 18px; margin-bottom: 16px; }}
h2 {{ font-size: 15px; margin: 0 0 10px; }}
button {{ background: #1f3a5f; color: #fff; border: 0; border-radius: 6px; padding: 9px 18px; font-size: 14px; cursor: pointer; margin-right: 8px; }}
button.stop {{ background: #a33; }}
.status {{ display: inline-block; padding: 1px 9px; border-radius: 10px; font-size: 12px; background: #eef1f4; }}
.status.completed {{ background: #e2f3e5; color: #1c6b32; }}
.status.failed {{ background: #fbe4e4; color: #a02020; }}
/* 026 P3.5: the detail page got the same refusal banner as the home page, so a
   refused 「恢复/重跑」 reads as a sentence instead of a dropped connection. */
.banner {{ background: #fdecea; border: 1px solid #f5c6c0; color: #8a1c10;
          border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; font-size: 14px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ text-align: left; padding: 7px 8px; border-bottom: 1px solid #eceff2; }}
th {{ color: #666; font-weight: 600; font-size: 12px; }}
.note {{ font-size: 12px; color: #777; }}
.table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
/* 026 P3.3: a phone in portrait -- see the note on PAGE's stylesheet. */
@media (max-width: 560px) {{
  header {{ padding: 12px 14px; }}
  main {{ margin: 10px auto; padding: 0 10px 28px; }}
  .card {{ padding: 12px; border-radius: 6px; }}
  button {{ width: 100%; padding: 12px 16px; margin: 6px 0 0; }}
  table {{ min-width: 460px; }}
}}
</style>
</head>
<body>
<header><a href="/">&larr; 返回控制台</a><h1 style="font-size:18px;margin:6px 0 0">运行 {run_id}</h1></header>
<main>
  {error_banner}
  {region_note}
  {summary_block}
  <div class="card">
    <h2>状态 <span class="status {status_class}" data-run-status="{run_id}"
        data-run-status-raw="{status_raw}" title="{status_raw}">{status}</span></h2>
    <p class="note">开始于 {started_at} ｜ 结束于 {finished_at} ｜ 说明：{note}
      ｜ 机器状态：<code>{status_raw}</code></p>
    <p class="note"><b>下一步：</b>{status_next}</p>
    <form method="post" action="/runs/{run_id}/stop" style="display:inline"><button class="stop" type="submit">停止</button></form>
    <form method="post" action="/runs/{run_id}/resume" style="display:inline"><button type="submit">恢复/重跑</button></form>
    <p class="note">{resume_note}</p>
    <form method="post" action="/runs/{run_id}/reexport">
      <label>重新导出（不重抓）：只按已存数据重写下载文件，不联网、不改原始记录、不改采集条件</label>
      <div>{reexport_languages}</div>
      <button class="ghost" type="submit">重新导出（不重抓）</button>
    </form>
  </div>
  <div class="card">
    <h2>阶段</h2>
    <div class="table-wrap">
      <table><tr><th>阶段</th><th>状态</th><th>说明</th></tr>{stages}</table>
    </div>
  </div>
  <div class="card">
    <h2>结果下载</h2>
    <p class="note">想直接看名单，就下带「推荐」徽章的那一份——它按你选的语言导出（中文 / 英文 / 中英双语）。
      语言选项只控制表头与说明：商家名称、地址和来源文本一律保留原文，不做机器翻译。</p>
    <div class="table-wrap">
      <table><tr><th>文件</th><th>大小</th><th>用途</th><th></th></tr>{exports}</table>
    </div>
  </div>
  <div class="card">
    <h2>失败与错误（未隐藏）</h2>
    <div class="table-wrap">
      <table><tr><th>阶段</th><th>类型</th><th>信息</th><th>时间</th></tr>{errors}</table>
    </div>
  </div>
</main>
<script>
// While this run is active, poll its status and reload once when it finishes,
// so the stage table and the download list update themselves. Nothing is
// posted: the buttons stay the only way to start, stop or resubmit a task.
(function () {{
  var runId = "{run_id}";
  var el = document.querySelector('[data-run-status="' + runId + '"]');
  if (!el) {{ return; }}
  // The badge shows a Chinese label, so the machine value is read from its own
  // attribute. Comparing the label against the API's raw status would differ on
  // every poll and reload the page in a loop (026 P3.2/P3.3).
  var last = el.getAttribute('data-run-status-raw') || el.textContent.trim();
  var timer = null;
  function poll() {{
    fetch('/api/runs', {{cache: 'no-store'}})
      .then(function (r) {{ return r.json(); }})
      .then(function (data) {{
        var mine = null;
        (data.runs || []).forEach(function (run) {{
          if (String(run.id) === String(runId)) {{ mine = run; }}
        }});
        if (!mine) {{ return; }}
        if (mine.status !== last) {{ window.location.reload(); return; }}
        if (mine.status !== 'running' && timer) {{ clearInterval(timer); timer = null; }}
      }})
      .catch(function () {{}});
  }}
  if (last === 'running') {{ timer = setInterval(poll, 3000); }}
}})();
</script>
</body>
</html>"""


def make_handler(base_dir: pathlib.Path, manager: RunManager):
    export_root = base_dir / "verification" / "ui" / "generated"

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalCollectorUI/1.0"

        # -- helpers ---------------------------------------------------------
        def _send_html(self, text: str, code: int = 200) -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def _database(self):
            return db.connect(base_dir / "runs" / "collector.sqlite")

        def log_message(self, *_args):  # keep the console clean
            return

        # -- pages -----------------------------------------------------------
        def do_GET(self) -> None:
            """Dispatch, and never drop the connection without a word.

            026 P3.5: an unhandled exception here used to close the socket. The
            browser showed nothing at all and the console filled with a
            traceback, which is the worst version of "the screen did not tell
            the truth": it told nothing. Now the reason reaches the page (and the
            console keeps the full stack for whoever is debugging).
            """
            try:
                self._route_get()
            except Exception as exc:  # noqa: BLE001 - a page must render regardless
                self._server_failure(exc)

        def _route_get(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                return self._page_home()
            if path == "/api/runs":
                return self._api_runs()
            if path.startswith("/runs/") and path.endswith("/download"):
                run_id = int(path.split("/")[2])
                return self._download(run_id, parse_qs(parsed.query).get("file", [""])[0])
            if path.startswith("/runs/") and path.count("/") == 2:
                return self._page_detail(int(path.split("/")[2]))
            self._send_html("<p>not found</p>", 404)

        def do_POST(self) -> None:
            try:
                self._route_post()
            except Exception as exc:  # noqa: BLE001 - see ``do_GET``
                self._server_failure(exc)

        def _route_post(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            length = int(self.headers.get("Content-Length") or 0)
            # ``keep_blank_values`` is the whole point here. 056 P2: a browser
            # sends ``sources=`` (the hidden blank that leads each tick group)
            # when every box is unticked, and an *empty* value is the only
            # difference between "I switched them all off" and "this request
            # never mentioned the group". Dropping it -- which is parse_qs's
            # default -- is what made the two indistinguishable and let the
            # region's defaults creep back in after the person emptied the
            # group. Blank values are filtered where they are read.
            form = (
                parse_qs(
                    self.rfile.read(length).decode("utf-8"), keep_blank_values=True
                )
                if length
                else {}
            )
            if path == "/runs":
                return self._start(form)
            if path.startswith("/runs/") and path.endswith("/stop"):
                manager.stop()
                return self._redirect(f"/runs/{path.split('/')[2]}")
            if path.startswith("/runs/") and path.endswith("/resume"):
                return self._resume(int(path.split("/")[2]))
            if path.startswith("/runs/") and path.endswith("/reexport"):
                return self._reexport(int(path.split("/")[2]), form)
            self._send_html("<p>not found</p>", 404)

        def _server_failure(self, exc: BaseException) -> None:
            """Say that the screen itself failed, in the reader's words.

            The message names the exception type because a person reporting the
            problem benefits from it, and because guessing a friendlier cause
            would be inventing a fact. The status feed gets a body *without*
            ``running``/``runs`` keys: a page that cannot read the database must
            not tell the poller that nothing is running — that would unlock the
            start button and clear the "one run at a time" note on no evidence.
            """
            traceback.print_exc()
            if urlparse(self.path).path.startswith("/api/"):
                body = json.dumps(
                    {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
                ).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_html(
                "<p><b>界面自己出错了</b>（"
                f"{html.escape(type(exc).__name__)}）：{html.escape(str(exc))}</p>"
                "<p class='note'>控制台窗口里有完整的堆栈；"
                "采集任务本身在服务端跑，不受这个错误影响。</p>",
                500,
            )

        # -- handlers --------------------------------------------------------
        def _api_runs(self) -> None:
            """The status feed the pages poll. Read-only by construction."""
            conn = self._database()
            live_config = manager.running_config() if manager.is_running() else ""
            try:
                runs = []
                for row in db.list_runs(conn):
                    status = str(row["status"])
                    label, _hint = run_status_view(
                        row, live_config=live_config, own_dir=export_root
                    )
                    # ``active`` is what the poll uses to decide whether to keep
                    # polling: the run is in progress *and* this screen is not
                    # reporting it as interrupted.
                    adapted = label != _run_status_text(status)[0]
                    runs.append(
                        {
                            "id": int(row["id"]),
                            "status": status,
                            # The label travels with the raw value so the poll can
                            # repaint the badge in Chinese without a second lookup.
                            # It is the *adapted* label, so a run whose process is
                            # gone is not repainted back to 运行中 by the poll.
                            "status_text": label,
                            "active": status == "running" and not adapted,
                        }
                    )
            finally:
                conn.close()
            body = json.dumps(
                {"running": manager.is_running(), "runs": runs}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        def _page_home(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            error = (query.get("error") or [""])[0]
            error_banner = (
                f"<div class='banner' id='submit-error'><b>无法开始：</b>{html.escape(error)}</div>"
                if error
                else ""
            )
            conn = self._database()
            try:
                runs = list(db.list_runs(conn))
                # Per-source status is about the *sites*, not this run, so it is
                # read here rather than derived from the runs table.
                source_status = db.list_source_status(conn)
            finally:
                conn.close()
            # Read the live child *before* rendering the rows: whether a run is
            # still going is part of what each row means (026 P3.5).
            running = manager.is_running()
            live_config = manager.running_config() if running else ""
            rows = "".join(
                _row_cells(row, live_config=live_config, own_dir=export_root)
                for row in runs
            )
            table = (
                "<table><tr><th>#</th><th>状态</th><th>开始时间</th><th>目标</th><th></th></tr>"
                + rows + "</table>"
                if runs
                else "<p class='note'>还没有运行记录。</p>"
            )
            # The blank form keeps the overseas default it always had (DE /
            # Google+OSM / English+bilingual) so the existing flow is unchanged;
            # the China path is spelled out in the note next to the button.
            #
            # After a refusal the same rendering runs with the echoed submission
            # instead of the defaults, so "fix one field and resubmit" does not
            # mean retyping the form (054).
            kept = _form_defaults(query)
            shown_country = str(kept["country"] or "DE")
            # 056 P2: a group the person *emptied* stays empty. The region
            # default is for a form that was never filled in, not for a choice
            # that was made and then taken away -- restoring it silently is how
            # "I turned every source off" became "it is collecting again".
            cleared = cleared_groups(query)
            shown_sources = (
                [] if "sources" in cleared
                else list(kept["sources"] or regions.default_sources(shown_country))
            )
            shown_languages = (
                [] if "languages" in cleared
                else list(kept["languages"] or regions.default_languages(shown_country))
            )
            # Saying it out loud, because an empty group is a state a person can
            # reach by accident and then not notice.
            empty_note = (
                "<p class='note' id='empty-groups'>"
                + "、".join(_GROUP_LABELS_ZH[key] for key in ECHO_LISTS if key in cleared)
                + "目前<b>一个都没有勾</b>。这不是默认值，是上一次提交时的选择；"
                "开始采集前需要至少勾选一个。</p>"
                if cleared
                else ""
            )
            stale = _stale_note((query.get("fp") or [""])[0], query)
            # Only a *reprised* form can produce a preflight warning: on a first
            # visit there is nothing to warn about, and warning about the blank
            # form's own defaults would be noise no one asked for.
            _echoed = any(key.startswith("v_") for key in query)
            # Two sources of warnings, one banner. The ``warn`` parameter is what
            # a pre-start confirmation carries (054 follow-up); recomputing from
            # the echoed form is what a refusal carries. Both are the same
            # sentences, and the recomputed set is only used when the form was
            # actually reprised.
            warnings = preflight_warnings(_form_from_values(kept)) if _echoed else []
            carried = (query.get("warn") or [""])[0]
            if carried:
                try:
                    loaded = json.loads(carried)
                    if isinstance(loaded, list):
                        warnings = [str(item) for item in loaded]
                except (ValueError, TypeError):
                    warnings = warnings or [carried]
            # What the next click will say it acknowledged: the setup the
            # reminder was shown about. A reminder page carries its own
            # fingerprint; a refusal page that happens to raise warnings has
            # none, so it is computed from the values being shown -- the same
            # values the person is about to resubmit, which is what makes one
            # extra click enough instead of an endless re-reminder.
            confirm_value = (query.get("confirmed") or [""])[0]
            if not confirm_value and warnings and _echoed:
                confirm_value = _preflight_fingerprint(_form_from_values(kept))
            page = PAGE.format(
                version=__version__,
                # 056 P1: every one of these is a person's own typing going
                # into a ``value="..."`` attribute, so each is escaped here --
                # the one place where a value becomes HTML.
                industry=_attr(kept["industry"]),
                city=_attr(kept["city"]),
                country=_attr(shown_country),
                query=_attr(kept["query"]),
                province=_attr(kept["province"]),
                district=_attr(kept["district"]),
                target_count=_attr(kept["target_count"] or "30"),
                formats=_attr(kept["formats"] or "csv,jsonl,xlsx"),
                geo=_attr(kept["geo"]),
                sources_boxes=_checkbox_group("sources", SOURCE_LABELS, shown_sources),
                languages_boxes=_checkbox_group(
                    "languages", LANGUAGE_LABELS, shown_languages
                ),
                fast_selected=" selected" if (kept["mode"] or "fast") == "fast" else "",
                browser_selected=" selected" if kept["mode"] == "browser" else "",
                enrich_off=" selected" if kept["enrich"] != "on" else "",
                enrich_on=" selected" if kept["enrich"] == "on" else "",
                domestic_block=_domestic_block(kept),
                discovery_block=_discovery_block(kept),
                offline_block=_offline_block(kept),
                csv_block=_csv_block(kept),
                status_block=_source_status_block(source_status),
                start_disabled=" disabled" if running else "",
                running_note=(BUSY_NOTE if running else ""),
                region_note=stale,
                empty_note=empty_note,
                table=table,
                error_banner=error_banner,
                # The acknowledgement rides on the *next* click only, and only
                # when a warning is actually on screen. A first visit must not
                # carry it, or the warnings would be skipped entirely.
                #
                # 056 P2: it carries the *fingerprint* of the setup that was
                # warned about, not a bare "1", so the server can tell "I read
                # it and started" from "I read it and then changed the URL".
                confirm_field=(
                    '<input type="hidden" name="confirmed" value="'
                    + _attr(confirm_value)
                    + '">'
                    if warnings
                    else ""
                ),
                preflight_banner=(
                    '<div class="banner"><b>开始前的提醒（看完再点一次「开始采集」就会开始）：</b><ul>'
                    + "".join(f"<li>{html.escape(text)}</li>" for text in warnings)
                    + "</ul></div>"
                    if warnings
                    else ""
                ),
            )
            self._send_html(page)

        def _refuse(self, form, message: str) -> None:
            """Send a refusal back *with the form the person just filled in*.

            054: this used to be ``/?error=<text>``, which threw away every
            field. The person fixed one typo and retyped the whole form -- and
            because the country fell back to DE, the retry failed differently,
            which reads as a broken tool rather than as one wrong box.

            The message and the values travel in the same redirect, so the page
            can show the reason *and* the boxes it is about, and the fingerprint
            lets the page tell this refusal apart from one the person has since
            edited away.
            """
            self._redirect(
                _echo_query(form, error=message, fingerprint=_form_fingerprint(form))
            )

        def _warn_before_start(self, form, warnings: list[str]) -> None:
            """Show the form again with the pre-flight warnings, then let it start.

            054: the warnings describe a legal submission that will very likely
            come back empty, so refusing it would be wrong and starting it
            silently is worse -- the person would read an empty result as "this
            site has no companies". The values ride back in the redirect exactly
            as a refusal's do, plus ``confirmed=<fingerprint>`` so the next
            click is the one that starts -- and so it can only start *this*
            setup. 056 P2: a bare ``confirmed=1`` outlived the reminder it
            acknowledged, so changing the URL to a different mismatch started
            the run with no reminder at all. The person's own input is never dropped, and the
            form is the same form.
            """
            parts = ["confirmed=" + _preflight_fingerprint(form)]
            parts.append(
                "warn=" + urllib.parse.quote(json.dumps(warnings, ensure_ascii=False))
            )
            query = _echo_query(form, error="", fingerprint="")
            # ``_echo_query`` always leads with ``/?``; splice the marker on.
            return self._redirect(query + "&" + "&".join(parts))

        def _start(self, form) -> None:
            name = form.get("industry", ["run"])[0] or "run"
            # 026 P3.5: a second POST while a run is in progress used to kill the
            # handler (the manager refuses, nothing catches it, and the browser
            # gets a dropped connection with no explanation). Two tabs, or one
            # stale page from before the button was disabled, is an ordinary way
            # to get here, so the refusal is a sentence on the page like every
            # other refusal.
            if manager.is_running():
                self._refuse(form, BUSY_NOTE)
                return
            # 056 P2: "no sources ticked" is a choice, not an absent answer, so
            # it must not fall through to the region's defaults -- for a China
            # run that meant switching every source off and still collecting
            # through the directory, and for an overseas run it meant going
            # online through Google/OSM after the person had turned them off.
            # Refusing is also the only way to keep the promise that this tool
            # never starts a network call nobody asked for.
            empty = [
                key
                for key in ECHO_LISTS
                # Only a group that was *submitted* blank counts as emptied. An
                # absent key is a request that carried no ticks at all (a
                # hand-built call, a page from before the group had its hidden
                # blank): guessing "emptied" there would refuse every such
                # request, and guessing "defaults" on a real emptied group is
                # the bug being fixed. The hidden blank is what makes the two
                # distinguishable, so this reads presence, not emptiness.
                if key in form
                and not [v for v in form.get(key) or [] if str(v).strip()]
            ]
            if empty:
                self._refuse(form, _EMPTY_GROUP_NOTE(empty))
                return
            # 054 follow-up: the pre-flight warnings are *about starting a run*,
            # so they have to be shown when a run is about to start -- not only
            # when an earlier refusal happened to echo the form back. They were
            # gated on the echo, which meant a preset/host mismatch (a legal
            # submission that is very likely to come back empty) started the run
            # silently, contradicting the banner's own "开始前的提醒".
            #
            # The person is shown the form again with the warnings, and their
            # next click (which carries ``confirmed=<fingerprint>``) starts the
            # run. That is
            # one extra click, it never hides their input, and it is the only
            # way to keep the promise "understood before it starts" without
            # refusing a legal run.
            warnings = preflight_warnings(form)
            if warnings and (form.get("confirmed") or [""])[0] != _preflight_fingerprint(form):
                # 056 P2: the acknowledgement has to belong to *this* setup.
                # A bare "confirmed" let a person read the reminder, change the
                # URL or preset to something else that also mismatches, and
                # start anyway. The server compares, so it holds even with
                # JavaScript off or a hand-built POST.
                self._warn_before_start(form, warnings)
                return
            try:
                cfg = build_config(form, base_dir=base_dir, name=name)
                # The name comes from a text field, so it becomes a file name
                # only after the path-like characters are taken out of it
                # (026 P3.5: a slash in the industry used to create a directory).
                path = write_config(
                    cfg,
                    export_root
                    / config_filename(name, stamp=f"{dt.datetime.now():%Y%m%d-%H%M%S}"),
                )
            except CollectorError as exc:
                # A run that cannot be placed is not started: the screen shows
                # the reason instead of quietly collecting somewhere else, and
                # keeps what the person typed so the fix is one edit.
                self._refuse(form, str(exc))
                return
            try:
                manager.start(path, base_dir)
            except RuntimeError:
                # Belt and braces: the check above and this child process
                # starting are not one atomic step.
                self._refuse(form, BUSY_NOTE)
                return
            self._redirect("/")

        def _resume(self, run_id: int) -> None:
            # Same one-run-at-a-time rule as ``_start``: resuming from a stale
            # page while something is already going is refused with a sentence,
            # not by dropping the connection.
            if manager.is_running():
                self._redirect(f"/runs/{run_id}?error=" + urllib.parse.quote(BUSY_NOTE))
                return
            conn = self._database()
            try:
                row = db.get_run(conn, run_id)
                if row is None:
                    return self._send_html("<p>run not found</p>", 404)
                config_path = export_root / f"resumed-{run_id}.yaml"
                # The stored config is the resume authority: write it back out
                # so the running CLI reuses exactly what the run started with.
                import yaml

                stored = json.loads(row["config_json"] or "{}")
                stored.pop("source_path", None)  # runtime-only, load() refuses it
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(
                    yaml.safe_dump(stored, allow_unicode=True, sort_keys=False), encoding="utf-8"
                )
            finally:
                conn.close()
            try:
                manager.start(config_path, base_dir)
            except RuntimeError:
                # Belt and braces, as in ``_start``.
                self._redirect(f"/runs/{run_id}?error=" + urllib.parse.quote(BUSY_NOTE))
                return
            self._redirect(f"/runs/{run_id}")

        def _page_detail(self, run_id: int) -> None:
            error = parse_qs(urlparse(self.path).query).get("error", [""])[0]
            error_banner = (
                f"<div class='banner'><b>无法开始：</b>{html.escape(error)}</div>"
                if error else ""
            )
            conn = self._database()
            try:
                row = db.get_run(conn, run_id)
                if row is None:
                    return self._send_html("<p>run not found</p>", 404)
                # 026 P3.5: the same view the list and the feed use, so a run
                # whose process is gone is never described as running on any
                # page. A stage of such a run is in the same position: the
                # process that would have been running it is the one that is
                # gone, so its row is not left claiming 运行中 either.
                status_label, status_next = run_status_view(
                    row,
                    live_config=manager.running_config() if manager.is_running() else "",
                    own_dir=export_root,
                )
                processes_gone = status_label != _run_status_text(row["status"])[0]
                stages = "".join(
                    "<tr><td title='{raw_stage}'>{stage}</td>"
                    "<td><span class='status {raw_status}'>{status}</span></td>"
                    "<td>{error}</td></tr>".format(
                        raw_stage=html.escape(str(s["stage"]), quote=True),
                        stage=html.escape(_stage_text(s["stage"], s["status"])[0]),
                        raw_status=html.escape(str(s["status"]), quote=True),
                        status=html.escape(
                            ORPHANED_STAGE_LABEL
                            if processes_gone and str(s["status"]) == "running"
                            else _stage_text(s["stage"], s["status"])[1]
                        ),
                        error=html.escape(str(s["error"] or "")),
                    )
                    for s in db.list_stages(conn, run_id)
                ) or "<tr><td colspan=3 class='note'>尚未开始</td></tr>"
                errors = "".join(
                    f"<tr><td>{html.escape(str(e['stage']))}</td><td>{html.escape(str(e['kind']))}</td>"
                    f"<td>{html.escape(str(e['message']))}</td>"
                    f"<td>{html.escape(str(e['occurred_at'] or '')[:19])}</td></tr>"
                    for e in db.list_errors(conn, run_id)
                ) or "<tr><td colspan=4 class='note'>无错误记录</td></tr>"
                export_dir = self._export_dir(conn, row)
                files = _ordered_exports(
                    [f for f in export_dir.glob("*") if f.is_file()]
                ) if export_dir.is_dir() else []
                exports = "".join(
                    self._export_row(run_id, f) for f in files
                ) or "<tr><td colspan=4 class='note'>运行结束后出现</td></tr>"
                # The stored config is the authority for what this run collects,
                # so the region/source notes and the summary are derived from it
                # — not from whatever the form says now. A config that cannot be
                # read (an older row with an empty blob) simply has no notes and
                # no summary instead of crashing the page.
                region_note = ""
                run_languages = list(regions.default_languages("DE"))
                summary_block = ""
                try:
                    stored = json.loads(row["config_json"] or "{}")
                    stored.pop("source_path", None)
                    run_cfg = Config(validate(stored, source=f"<ui:{run_id}>"))
                    region_note = _region_note(run_cfg)
                    run_languages = run_cfg.languages
                    checkpoint = db.get_checkpoint(conn, run_id, "run", "summary")
                    stored_summary = checkpoint if isinstance(checkpoint, dict) else {}
                    # Recomputed from the stored facts with the run's own config,
                    # so the numbers on the page and the numbers in the workbook
                    # cannot disagree.
                    summary_block = _run_summary_block(
                        export.run_summary(conn, run_id, run_cfg),
                        stop_reason=str(stored_summary.get("stop_reason") or ""),
                        stop_note=str(stored_summary.get("stop_note") or ""),
                        criteria_note=_criteria_note(run_cfg),
                        # 026 P3.1: how this batch was really obtained, read off
                        # the stored provenance. The screen and the workbook
                        # print the identical lines.
                        acquisition=export.acquisition_lines(conn, run_id, run_cfg, "zh"),
                    )
                except Exception:  # noqa: BLE001 - a page must render regardless
                    pass
            finally:
                conn.close()
            page = DETAIL_PAGE.format(
                run_id=run_id,
                status=html.escape(status_label),
                status_raw=html.escape(str(row["status"])),
                status_next=html.escape(status_next),
                status_class=html.escape(str(row["status"])),
                started_at=html.escape(str(row["started_at"] or "")[:19]),
                finished_at=html.escape(str(row["finished_at"] or "")[:19]),
                note=html.escape(str(row["note"] or "")),
                region_note=region_note,
                summary_block=summary_block,
                # What 「恢复/重跑」 really does depends on whether the run
                # finished. ``latest_resumable_run`` only hands back a run that
                # is still running / interrupted / failed / partial, so pressing
                # the button on a *completed* run starts a **new** run with the
                # same config and collects again -- the old note promised
                # "same config only runs once" for that case too, which was
                # false. A finished run's honest re-delivery path is the
                # re-export form below, and the note now says so.
                resume_note=(
                    "「停止」只对正在采集的运行有意义；这次已经跑完。"
                    "「恢复/重跑」会用同一套配置重新开始一次采集（不是接着这次继续，会重新发出请求）；"
                    "只想按已存数据重写下载文件、不重新抓取，请用下面的「重新导出（不重抓）」。"
                    if str(row["status"]) == "completed"
                    else "「停止」会结束采集进程，已完成的结果保留；"
                    "「恢复/重跑」从中断处继续（同一配置只跑一遍，不重复采集）。"
                ),
                reexport_languages=_checkbox_group(
                    "languages", LANGUAGE_LABELS, run_languages
                ),
                stages=stages, errors=errors, exports=exports,
                error_banner=error_banner,
            )
            self._send_html(page)

        def _export_dir(self, conn, row) -> pathlib.Path:
            # The export directory is per run next to the database.
            return base_dir / "runs" / row["fingerprint"][:12] / f"run-{int(row['id'])}" / "export"

        def _export_row(self, run_id: int, path: pathlib.Path) -> str:
            """One download line: what the file is, its size, and a download link."""
            label, note, recommended = _file_role(path.name)
            badge = (
                "<span style='background:#e2f3e5;color:#1c6b32;border-radius:8px;"
                "padding:1px 7px;font-size:11px;margin-left:6px'>推荐</span>"
                if recommended else ""
            )
            return (
                f"<tr><td>{html.escape(label)}{badge}"
                f"<div class='note'>{html.escape(path.name)}</div></td>"
                f"<td>{_fmt_size(path)}</td>"
                f"<td class='note'>{html.escape(note)}</td>"
                f"<td><a href='/runs/{run_id}/download?file={path.name}'>下载</a></td></tr>"
            )

        def _reexport(self, run_id: int, form=None) -> None:
            """Rewrite a run's export files from the data already stored.

            Deliberately *not* a resume: no stage that talks to the network is
            run, so a finished run's downloads can be regenerated — to add a
            language workbook, or to pick up a changed export column — without
            pretending a new collection happened. The raw records and the
            database are read, never written.

            The language tick boxes override the stored ``export.languages``
            for this re-export only. Nothing else about the run changes: the
            collection conditions and the raw facts stay exactly as collected,
            which is what makes "re-export in Chinese only" a safe operation.
            """
            conn = self._database()
            try:
                row = db.get_run(conn, run_id)
                if row is None:
                    return self._send_html("<p>run not found</p>", 404)
                stored = json.loads(row["config_json"] or "{}")
                stored.pop("source_path", None)  # runtime-only, validate refuses it
                chosen = [
                    value for value in (form or {}).get("languages", [])
                    if value in SUPPORTED_LANGUAGES
                ]
                if chosen:
                    export_node = stored.setdefault("export", {})
                    if isinstance(export_node, dict):
                        export_node["languages"] = chosen
                cfg = Config(validate(stored, source=f"<ui-reexport:{run_id}>"))
                directory = (
                    base_dir / "runs" / str(row["fingerprint"])[:12]
                    / f"run-{run_id}" / cfg.export_directory_name
                )
                export.export_run(conn, run_id, cfg, directory)
                db.set_stage(conn, run_id, "export", "completed", touch_finished=True)
            except (CollectorError, ValueError, KeyError, TypeError) as exc:
                return self._send_html(
                    f"<p>重新导出失败：{html.escape(str(exc))}</p>", 400
                )
            finally:
                conn.close()
            self._redirect(f"/runs/{run_id}")

        def _download(self, run_id: int, filename: str) -> None:
            if "/" in filename or "\\" in filename or ".." in filename:
                return self._send_html("<p>bad filename</p>", 400)
            conn = self._database()
            try:
                row = db.get_run(conn, run_id)
            finally:
                conn.close()
            if row is None:
                return self._send_html("<p>run not found</p>", 404)
            target = self._export_dir(conn, row) / filename
            if not target.is_file():
                return self._send_html("<p>file not found</p>", 404)
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{target.name}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(*, base_dir: pathlib.Path, port: int = 8757, host: str = "127.0.0.1", open_browser: bool = True) -> int:
    if host != "127.0.0.1":
        print(
            "WARNING: binding to a non-loopback address exposes the collector to "
            "the network. Anyone who can reach the port can start runs.",
            file=sys.stderr,
        )
    manager = RunManager()
    handler = make_handler(base_dir, manager)
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"本地控制台: {url}  (Ctrl+C 停止)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止控制台")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry
    import argparse

    parser = argparse.ArgumentParser(description="本地采集控制台（默认只监听本机）")
    parser.add_argument("--base-dir", default=str(pathlib.Path.cwd().parent))
    parser.add_argument("--port", type=int, default=8757)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    raise SystemExit(serve(
        base_dir=pathlib.Path(args.base_dir), port=args.port, host=args.host,
        open_browser=not args.no_browser,
    ))
