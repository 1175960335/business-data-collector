"""Candidate website discovery: find a business's own site when the listing has none.

Task book 026 P1 asks for a *reusable* pipeline, not a one-off search:

    candidate discovery -> ownership determination -> the existing enrichment
    -> merge / match / export refresh

The stage D enricher (:mod:`collector.enrich`) only ever visited a business's
site when the listing already carried one. This module fills that gap and hands
its *confirmed* results to the enricher unchanged: it writes an ordinary
``website`` fact and fills ``entities.website``, so everything downstream --
enrichment, cross-source merge, matching, scoring, export -- keeps working
exactly as before. Nothing about stage D is rewritten.

Three rules this module will not bend
-------------------------------------
1. **A candidate is not a website.** A discovered URL is stored in
   ``site_candidates`` with the finder that produced it and the evidence for the
   verdict. Only a *confirmed* candidate writes a ``website`` fact, because
   binding the wrong contact details to a company is worse than leaving the
   field empty.
2. **No search service is claimed.** ``search`` is declared and reported as
   unavailable rather than silently doing nothing (task book 026 P1.1: "无可用
   搜索服务时功能如实降级为用户 URL/页面外链，不能伪称全网自动搜").
3. **A name is not an identity.** Ownership needs the page's own name *and* a
   second, independent signal (the same phone or the same address). A directory
   or platform host is never an official site, however similar its name.
"""

from __future__ import annotations

import csv as csv_module
import datetime as _datetime
import hashlib
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from . import db
from .config import Config
from .htmlextract import extract_page
from .identity import normalize_text
from .util import clip, utc_now_iso
from .webfetch import Fetcher, PageCache, UrlPolicy, normalize_url, registrable_domain

#: The provider name written on a confirmed ``website`` fact. Distinct from
#: ``website`` (stage D) so a reader can tell a discovered site from a listed
#: one, and so the two can never be confused in a conflict.
PROVIDER = "site_discovery"

#: Candidate verdicts.
STATUS_CONFIRMED = "confirmed"
STATUS_PENDING = "pending"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"

#: Why a candidate was rejected or left pending. Kept as a closed vocabulary so
#: the UI and the report can group by it instead of parsing prose.
REASON_NAME_MISMATCH = "name_mismatch"
REASON_ONLY_NAME = "only_the_name_agrees"
REASON_DIRECTORY_HOST = "directory_or_platform_host"
REASON_NO_NAME_ON_PAGE = "no_name_on_the_page"
REASON_NOT_FETCHED = "not_fetched"
REASON_BOUNDARY = "url_boundary_rejected"
REASON_LISTING_SITE = "listing_already_has_a_site"

#: 042: a verdict that cost a request and failed is not an answer. It is
#: retried, but bounded -- on the *candidate*, not globally -- and only after a
#: cooldown, so a host that refused once is not asked again immediately by a
#: resumed run. ``max_attempts`` counts attempts in total, including the first.
DEFAULT_RETRY_MAX_ATTEMPTS = 2
DEFAULT_RETRY_COOLDOWN_SECONDS = 600

#: Bumped whenever :func:`judge_ownership`, :func:`is_directory_host` or the
#: evidence the verdict may use changes. Stored in every checkpoint: a verdict
#: reached under a different rules id is re-judged instead of standing forever.
#: It is a *claim* by the code, so bumping it is a deliberate act, and the test
#: suite pins the current value rather than the number itself.
#:
#: ``/2`` (045): the judgement may now read a site's contact/Impressum pages in
#: addition to its home page, within ``sitediscovery.pages_per_site``. That is
#: more evidence than ``/1`` was allowed to use, so every ``/1`` verdict -- in
#: particular the ones left ``pending`` because only the name matched -- is
#: re-judgeable instead of standing as if the pages had been read and settled
#: the question.
JUDGE_RULES_VERSION = "site-ownership/2"

#: Verdicts reached without spending a request, from the URL and the listing
#: alone. While the input and the rules are the same they cannot come out
#: differently, so a resumed run does not recompute them.
DETERMINISTIC_REASONS = frozenset(
    {REASON_BOUNDARY, REASON_DIRECTORY_HOST, REASON_LISTING_SITE}
)

#: Verdicts that answered the question. Re-fetching would spend a request to
#: reach the same answer, so they stand until the evidence or the rules change.
#: ``error`` is deliberately not one of them: a failed fetch answered nothing.
SETTLED_STATUSES = frozenset({STATUS_CONFIRMED, STATUS_PENDING, STATUS_REJECTED})

#: Appended to a verdict that was not judged this run but taken from an earlier
#: one. Without it a reused row is indistinguishable from a fresh one, and the
#: export would state a confirmation nobody re-checked.
REUSED_NOTE = " [复用上次已确认判定，本次未重新请求]"

#: Host patterns that are a directory, a marketplace, a platform or an ICP
#: filing page -- never the business's own site. Matched against the host and
#: its labels, so ``foo.b2b168.com`` is caught by ``b2b168.com``.
DIRECTORY_HOST_MARKERS = (
    "b2b168.com",
    "11467.com",
    "huangye88.com",
    "1688.com",
    "made-in-china.com",
    "alibaba.com",
    "qcc.com",
    "tianyancha.com",
    "aiqicha.baidu.com",
    "beian.miit.gov.cn",
    "map.baidu.com",
    "dianping.com",
    "meituan.com",
    "58.com",
    "ganji.com",
    "chinab2b.com",
    "china.cn",
    "sohu.com",
    "sina.com.cn",
    "weibo.com",
    "zhihu.com",
    "baidu.com",
    "taobao.com",
    "jd.com",
)

#: Host where the operator explicitly says "this is the company's own site".
#: Nothing else overrides the deny list: an explicit configuration is a decision
#: the operator made, and the verdict records that it was taken.
@dataclass(frozen=True)
class Candidate:
    """One URL a finder believes may be a business's own site."""

    url: str
    found_by: str
    origin: str = ""
    note: str = ""


@dataclass
class DiscoveryStats:
    entities_considered: int = 0
    #: How many of the run's entities were merged into another before discovery
    #: ran. Reported because it is the number the request de-duplication saves:
    #: a business that three records describe is discovered once, not three
    #: times (034 第一批第 4 条).
    entities_merged_away: int = 0
    entities_without_site: int = 0
    candidates_found: int = 0
    #: Candidates dropped because another record of the same business already
    #: proposed the same URL. Not an error: it is the de-duplication working.
    candidates_deduped: int = 0
    #: Verdicts taken from an earlier run instead of a new request. Only ever
    #: non-zero when ``sitediscovery.reuse_within_days`` is set (default 0).
    candidates_reused: int = 0
    candidates_skipped_seen: int = 0
    candidates_skipped_budget: int = 0
    #: 042: a failed fetch is not an answer, so it is retried -- bounded per
    #: candidate, and only after a cooldown. These three counters say which way
    #: each re-visit went, so "it did not ask again" is never a bare omission.
    candidates_retried: int = 0
    candidates_rejudged: int = 0
    candidates_legacy_adopted: int = 0
    candidates_skipped_cooling: int = 0
    candidates_failed_given_up: int = 0
    #: The rules id every verdict in this run was reached under. Written into
    #: each verdict's evidence so a reader can tell two runs apart.
    rules_id: str = ""
    pages_fetched: int = 0
    pages_failed: int = 0
    confirmed: int = 0
    pending: int = 0
    rejected: int = 0
    boundary_rejections: int = 0
    errors: int = 0
    budget_reason: str = ""
    finders: list[str] = field(default_factory=list)
    unavailable_finders: list[str] = field(default_factory=list)
    #: Finders that are configured but still cannot produce anything, e.g. a
    #: named file that is not there. Reported next to the summary so a run with
    #: zero candidates says *which* of the three situations it is: nothing was
    #: asked for, the thing asked for is missing, or it was asked and found none.
    finder_notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# --- finders ----------------------------------------------------------------


class CandidateFinder(ABC):
    """A replaceable way of proposing candidate sites.

    A finder says whether it is configured and never invents an answer: an
    unavailable finder reports *why*, so a run that found nothing is
    distinguishable from a run that could not look.
    """

    name: str = ""
    title: str = ""
    requires_account: bool = False
    #: A short note on where the candidates come from, shown in the report.
    provenance: str = ""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def settings(self) -> dict:
        node = self.cfg.sitediscovery.get(self.name)
        return node if isinstance(node, dict) else {}

    def is_configured(self) -> bool:
        return False

    def not_configured_reason(self) -> str:
        return f"{self.name} is not configured"

    def configuration_note(self) -> str:
        """A warning about a *configured* finder that still cannot produce.

        "Configured, and the file it names is not there" is neither of the two
        states :meth:`is_configured` can express: the operator did configure it,
        and the run will still find nothing. Saying so here is what keeps "found
        none" apart from "could not look" (task book 026 P1.1).
        """
        return ""

    @abstractmethod
    def find(
        self, entities: list[Any], by_key: dict[str, int], by_name: dict[str, int]
    ) -> list[tuple[int, Candidate]]: ...

    def describe(self) -> dict[str, Any]:
        configured = self.is_configured()
        return {
            "name": self.name,
            "title": self.title,
            "configured": configured,
            "requires_account": self.requires_account,
            "provenance": self.provenance,
            "not_configured_reason": "" if configured else self.not_configured_reason(),
            "note": self.configuration_note(),
        }


class UserListFinder(CandidateFinder):
    """Candidates the operator supplies in a file.

    Deterministic and offline, exactly like ``csv_list``. The file maps a row to
    the business it belongs to by stable key or by name, so the operator can look
    at the list they already have and add the sites they know.
    """

    name = "user_list"
    title = "Operator-supplied site list (local file)"
    provenance = (
        "A file the operator wrote. The collector did not search for these URLs; "
        "it only records that they were supplied."
    )

    def path(self) -> str:
        return str(self.settings().get("path") or "").strip()

    def is_configured(self) -> bool:
        return bool(self.path())

    def not_configured_reason(self) -> str:
        return "sitediscovery.user_list.path is not set"

    def configuration_note(self) -> str:
        path = self.path()
        if path and not pathlib.Path(path).is_file():
            return (
                f"清单文件不存在：{path}（因此这个发现器不会有候选；"
                "请修正 sitediscovery.user_list.path）"
            )
        return ""

    def find(self, entities, by_key, by_name):
        out: list[tuple[int, Candidate]] = []
        for row in read_csv_rows(self.path()):
            target = next(
                (row.get(name, "") for name in ("stable_key", "key", "entity", "name") if row.get(name)),
                "",
            )
            url = next(
                (row.get(name, "") for name in ("url", "website", "site") if row.get(name)),
                "",
            )
            if not target or not url:
                continue
            entity_id = by_key.get(target) or by_name.get(normalize_text(target))
            if entity_id is None:
                continue
            out.append(
                (
                    entity_id,
                    Candidate(
                        url=url,
                        found_by=self.name,
                        origin="operator-supplied",
                        note=f"supplied in {pathlib.Path(self.path()).name}",
                    ),
                )
            )
        return out


class PageLinksFinder(CandidateFinder):
    """Candidates read out of pages the run already obtained.

    The operator points at saved pages (a snapshot directory); the finder takes
    the *external* links off them, i.e. links that leave the page's own site.
    A link on a directory page is exactly where a business's own site is
    advertised, and the ownership check then decides whether it really is.
    """

    name = "page_links"
    title = "Outbound links on already-obtained pages (saved snapshots)"
    provenance = (
        "Links taken from HTML the operator saved. Off-site links only; the "
        "page's own domain is never a candidate."
    )

    def manifest_path(self) -> str:
        return str(self.settings().get("manifest") or "").strip()

    def pages(self) -> list[dict]:
        raw = self.settings().get("pages")
        inline = [entry for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []
        manifest = self.manifest_path()
        if not manifest:
            return inline
        # A manifest file is the screen's way in: a person picks a CSV, not a
        # nested YAML list. The two forms are additive so a config may carry
        # both, and the reader is the same one the screen writes against.
        return inline + read_pages_manifest(manifest)

    def is_configured(self) -> bool:
        # A named manifest counts as configured even when the file is absent:
        # the operator did point at something, and calling it "not configured"
        # would hide which of the two problems they actually have.
        return bool(self.pages() or self.manifest_path())

    def not_configured_reason(self) -> str:
        return "sitediscovery.page_links.pages and .manifest are both empty"

    def configuration_note(self) -> str:
        manifest = self.manifest_path()
        if manifest and not pathlib.Path(manifest).is_file():
            return f"已保存页面清单不存在：{manifest}（因此这个发现器不会有候选）"
        if manifest and not read_pages_manifest(manifest):
            return f"已保存页面清单没有可用行：{manifest}（需要 name, path 两列）"
        return ""

    def find(self, entities, by_key, by_name):
        out: list[tuple[int, Candidate]] = []
        for entry in self.pages():
            target = str(entry.get("entity") or "").strip()
            html_path = pathlib.Path(str(entry.get("path") or ""))
            if not target or not html_path.is_file():
                continue
            entity_id = by_key.get(target) or by_name.get(normalize_text(target))
            if entity_id is None:
                continue
            page_url = str(entry.get("page") or entry.get("url") or html_path.name)
            try:
                body = html_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            extraction = extract_page(page_url, body)
            for link in extraction.outbound:
                out.append(
                    (
                        entity_id,
                        Candidate(
                            url=link.url,
                            found_by=self.name,
                            origin=page_url,
                            note=f"outbound link on {page_url}",
                        ),
                    )
                )
        return out


class WikidataFinder(CandidateFinder):
    """Official sites from Wikidata's ``P856`` via its public SPARQL endpoint.

    Task book 034, second batch, item 2.3, asks whether *any* account-free
    search exists before concluding that search has to be paid for. There is
    one, and this is it: Wikidata's Query Service is free, needs no key and no
    registration, and publishes an identifying-User-Agent requirement rather
    than a sign-up wall. Its limits are documented publicly (60 seconds of
    query time per minute per IP+User-Agent, 60 s per query).

    What it is *not*: a web search. It answers "does Wikidata have a structured
    statement that this exact name has this official website", which is why

    * the match is on the **exact label**, never a fuzzy one -- a near-match
      would be a guess dressed as a fact;
    * a candidate still goes through the ordinary ownership check, so a hit is
      never written to ``website`` until the page itself agrees;
    * the finder reports how many names it asked about and how many answered,
      because on small domestic businesses the honest answer is almost always
      zero, and that has to be visible instead of looking like a bug.

    The request count is bounded here rather than by the stage budget: the
    budget bounds *candidate page fetches*, and this is a lookup before that.
    """

    name = "wikidata"
    title = "Wikidata / Wikipedia structured data (public SPARQL, no account)"
    requires_account = False
    provenance = (
        "Public SPARQL endpoint query.wikidata.org. Matches the business name "
        "to a Wikidata label exactly, then reads property P856 (official "
        "website). No key, no registration, no cost."
    )

    #: Wikidata asks for an identifying User-Agent; a generic one is throttled.
    DEFAULT_USER_AGENT = "osint-collector/1.0 (official-site lookup; contact: local operator)"
    DEFAULT_ENDPOINT = "https://query.wikidata.org/sparql"
    DEFAULT_TIMEOUT_SECONDS = 30
    DEFAULT_NAMES_PER_QUERY = 20
    DEFAULT_MAX_QUERIES = 5
    DEFAULT_LANGUAGES = ("zh", "en")
    #: 042: a name match is not an identity, so an item has to be declared an
    #: organisation/company before its website is proposed.
    DEFAULT_REQUIRE_ORG_TYPE = True

    #: Set by :meth:`find` so the run's report can say what happened.
    runtime_notes: list[str] = []

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self.runtime_notes = []

    # -- configuration ------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.settings().get("enabled"))

    def not_configured_reason(self) -> str:
        return "not enabled; set sitediscovery.wikidata.enabled to turn it on"

    def endpoint(self) -> str:
        return str(self.settings().get("endpoint") or self.DEFAULT_ENDPOINT).strip()

    def user_agent(self) -> str:
        return str(self.settings().get("user_agent") or self.DEFAULT_USER_AGENT).strip()

    def timeout_seconds(self) -> float:
        return float(self.settings().get("timeout_seconds") or self.DEFAULT_TIMEOUT_SECONDS)

    def names_per_query(self) -> int:
        return max(1, int(self.settings().get("names_per_query") or self.DEFAULT_NAMES_PER_QUERY))

    def max_queries(self) -> int:
        return max(1, int(self.settings().get("max_queries") or self.DEFAULT_MAX_QUERIES))

    def languages(self) -> tuple[str, ...]:
        raw = self.settings().get("languages")
        if isinstance(raw, list) and raw:
            return tuple(str(item) for item in raw)
        return self.DEFAULT_LANGUAGES

    def require_org_type(self) -> bool:
        """Whether an item has to *be* an organisation before it is a candidate.

        On by default, and the reason is measured rather than theoretical: the
        first live run asked for a name that also names an encyclopedia and got
        that project's software documentation back as an "official website".
        """
        value = self.settings().get("require_org_type")
        return self.DEFAULT_REQUIRE_ORG_TYPE if value is None else bool(value)

    def org_classes(self) -> frozenset:
        raw = self.settings().get("org_classes")
        if isinstance(raw, list) and raw:
            return frozenset(str(item) for item in raw)
        return WIKIDATA_ORG_CLASSES

    # -- the lookup ---------------------------------------------------------

    def find(self, entities, by_key, by_name):
        self.runtime_notes = []
        # The *original* spelling is what has to go to Wikidata. 042: the lookup
        # used to send ``normalize_text(name)``, which case-folds, and no label
        # with a capital letter in it can ever match a case-folded literal --
        # so every "0 of N names answered" the earlier rounds reported was
        # measuring our own lower-casing, not the source's coverage. The folded
        # form is still sent alongside, because a listing may legitimately be
        # typed in a different case than the label.
        wanted: dict[str, int] = {}
        literals: dict[str, str] = {}
        for entity in entities:
            name = str(entity["name"] or "").strip()
            if not name:
                continue
            key = normalize_text(name)
            wanted.setdefault(key, int(entity["id"]))
            literals.setdefault(name, key)
        if not wanted:
            return []

        names = sorted(literals)
        per_query = self.names_per_query()
        batches = [names[index:index + per_query] for index in range(0, len(names), per_query)]
        batches = batches[: self.max_queries()]
        if len(batches) < (len(names) + per_query - 1) // per_query:
            # Bounded on purpose: a run must not quietly grow into a bulk query
            # against a free public service. The shortfall is reported, not
            # silently retried later.
            self.runtime_notes.append(
                f"只查询了前 {len(batches) * per_query}/{len(names)} 个名称"
                f"（每次 {per_query} 个、最多 {self.max_queries()} 次，避免对免费公共服务批量查询）"
            )

        hits: dict[int, WikidataHit] = {}
        asked = 0
        wrong_type: list[str] = []
        errors: list[str] = []
        wanted_types = self.org_classes()
        require_type = self.require_org_type()
        for batch in batches:
            asked += len(batch)
            try:
                payload = self._query(batch)
            except WikidataUnavailable as exc:
                errors.append(str(exc))
                break
            for hit in payload:
                entity_id = wanted.get(normalize_text(hit.name))
                if entity_id is None or entity_id in hits:
                    continue
                if require_type and not wanted_types.intersection(hit.types):
                    wrong_type.append(
                        f"{hit.name}（{hit.item or '无条目 id'}："
                        f"{','.join(hit.types) or '未声明类型'}）"
                    )
                    continue
                hits[entity_id] = hit

        if errors:
            # A query that failed measured nothing, so it must not also report a
            # clean "0 of N answered" -- that number is what a reader takes as
            # the source's coverage.
            self.runtime_notes.append(
                f"查询未能完成：{asked} 个名称里只问到 {len(hits)} 个官网声明，"
                "其余未查（" + "；".join(errors) + "）"
            )
        else:
            self.runtime_notes.append(
                f"向 Wikidata 公开 SPARQL 查询了 {asked} 个企业名称，"
                f"{len(hits)} 个有官网声明（P856）"
            )
        if wrong_type:
            self.runtime_notes.append(
                f"{len(wrong_type)} 个名称命中了非企业/组织条目，已排除"
                f"（名称精确匹配不等于企业身份）：" + "；".join(wrong_type[:5])
            )

        return [
            (
                entity_id,
                Candidate(
                    url=hit.url,
                    found_by=self.name,
                    origin="structured-open-data",
                    note=(
                        f"Wikidata P856 官方网址声明（名称精确匹配；"
                        f"条目 {hit.item or '未知'}，类型 "
                        f"{','.join(hit.types) or '未声明'}）"
                    ),
                ),
            )
            for entity_id, hit in sorted(hits.items())
        ]

    def _query(self, names: list[str]) -> list[WikidataHit]:
        """Run one SPARQL query. Raises :class:`WikidataUnavailable` on failure.

        Kept separate and dependency-free so a test can exercise the query text
        and the parsing without a socket: the network call is the only part that
        is not local.
        """
        query = self._sparql(names)
        request = urllib.request.Request(
            self.endpoint()
            + "?"
            + urllib.parse.urlencode({"query": query, "format": "json"}),
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": self.user_agent(),
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds()) as response:
                body = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise WikidataUnavailable(f"{type(exc).__name__}: {exc}") from exc
        return self._parse(body)

    def _sparql(self, names: list[str]) -> str:
        """Exact-label match, language-tagged, so the lookup uses an index.

        A ``FILTER`` over ``STR(?label)`` would scan every item that has a
        website -- slow enough to hit the endpoint's 60-second query timeout,
        which is a discourteous way to use a free service. ``VALUES`` binds the
        constant first instead.
        """
        literals: list[str] = []
        seen: set[str] = set()
        for name in names:
            for candidate in (name, normalize_text(name)):
                if candidate in seen:
                    continue
                seen.add(candidate)
                escaped = candidate.replace("\\", "\\\\").replace('"', '\\"')
                for language in self.languages():
                    literals.append(f'"{escaped}"@{language}')
        values = " ".join(literals)
        # ``?type`` is what separates a company from an encyclopedia that happens
        # to be called the same thing; it is optional in the query because an
        # item with no P31 must still be *reported* as untyped rather than
        # silently dropped.
        return (
            "SELECT ?name ?website ?item ?type WHERE {\n"
            f"  VALUES ?name {{ {values} }}\n"
            "  ?item rdfs:label ?name ; wdt:P856 ?website .\n"
            "  OPTIONAL { ?item wdt:P31 ?type . }\n"
            "}\n"
        )

    @staticmethod
    def _parse(body: str) -> list[WikidataHit]:
        """Read the endpoint's answer.

        One item can come back once per ``P31``, so the rows are grouped by
        (name, website, item) and the types collected -- a company that is both
        a business and a public company is one candidate with two types, not two
        candidates.
        """
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise WikidataUnavailable(f"endpoint returned something that is not JSON: {exc}") from exc
        grouped: dict[tuple[str, str, str], list[str]] = {}
        for binding in (payload.get("results") or {}).get("bindings") or []:
            name = ((binding.get("name") or {}).get("value") or "").strip()
            url = ((binding.get("website") or {}).get("value") or "").strip()
            if not name or not url.startswith(("http://", "https://")):
                continue
            item = ((binding.get("item") or {}).get("value") or "").rsplit("/", 1)[-1]
            kind = ((binding.get("type") or {}).get("value") or "").rsplit("/", 1)[-1]
            bucket = grouped.setdefault((name, url, item), [])
            if kind and kind not in bucket:
                bucket.append(kind)
        return [
            WikidataHit(name=name, url=url, item=item, types=tuple(sorted(types)))
            for (name, url, item), types in grouped.items()
        ]


class WikidataUnavailable(Exception):
    """The public endpoint could not be reached or did not answer JSON."""


#: One answer from the endpoint. Kept as a record rather than a tuple because
#: 042 asks for the *type* of thing that answered to be carried with the
#: candidate: an exact label match says nothing about what the item is.
@dataclass(frozen=True)
class WikidataHit:
    name: str
    url: str
    #: The Wikidata id (``Q12345``), so a reader can look the claim up.
    item: str = ""
    #: ``P31`` (instance of) ids, e.g. ``("Q783794",)`` for a company.
    types: tuple[str, ...] = ()

    @property
    def item_url(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.item}" if self.item else ""


#: Wikidata classes that make an item a plausible *business*, verified against
#: the endpoint's own labels: Q43229 organization, Q783794 company, Q4830453
#: business, Q6881511 enterprise, Q891723 public company, Q167037 corporation.
#:
#: Why a filter at all: the label "Wikipedia" matches the Wikidata item for the
#: encyclopedia as exactly as "Siemens" matches the company, and the former has
#: an official website (the readthedocs pages of the underlying software). An
#: exact name match is a string fact, not an identity -- what the item *is* is
#: the only thing here that separates the two.
WIKIDATA_ORG_CLASSES = frozenset(
    {"Q43229", "Q783794", "Q4830453", "Q6881511", "Q891723", "Q167037"}
)


class SearchFinder(CandidateFinder):
    """A whole-web search for an official site -- declared, never claimed.

    No account-free, unblocked search entry point is available for this project:
    the general search APIs need a key and a paid quota, and the domestic
    directories that could be searched answer 429 (see the 025/027 reports). The
    task book allows paid search only as an optional, explicitly-approved
    addition, so the finder reports itself unavailable instead of pretending to
    have searched.
    """

    name = "search"
    title = "General web search for an official site (not available)"
    requires_account = True
    provenance = "Would need a search API with a key and a paid quota; not enabled."

    def is_configured(self) -> bool:
        raw = self.settings()
        return bool(raw.get("endpoint") and raw.get("api_key_env"))

    def not_configured_reason(self) -> str:
        return (
            "no account-free search service is available; a general search API "
            "needs a key and a paid quota, which this round does not buy "
            "(task book 026 forbids paid APIs). Degrading to operator-supplied "
            "URLs and outbound links on saved pages."
        )

    def find(self, entities, by_key, by_name):
        # Deliberately empty: the finder never fabricates a search result.
        return []


#: Registration order = report order. ``search`` is listed so its *absence* is
#: visible rather than the feature quietly not existing.
FINDER_CLASSES: tuple[type[CandidateFinder], ...] = (
    UserListFinder,
    PageLinksFinder,
    WikidataFinder,
    SearchFinder,
)

FINDER_NAMES = tuple(cls.name for cls in FINDER_CLASSES)


# --- ownership --------------------------------------------------------------


@dataclass(frozen=True)
class Ownership:
    """The verdict on one candidate URL."""

    status: str
    reason: str
    note: str
    evidence: dict = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.status == STATUS_CONFIRMED


def is_directory_host(host: str) -> bool:
    """Whether a host is a directory / marketplace / platform, not a company site."""
    text = (host or "").casefold()
    if not text:
        return True
    for marker in DIRECTORY_HOST_MARKERS:
        if text == marker or text.endswith("." + marker):
            return True
    return False


def judge_ownership(
    *,
    entity_name: str,
    listing_phone: str,
    listing_address: str,
    page_url: str,
    page_values: dict[str, str],
    country: str,
    rules: dict,
) -> Ownership:
    """Decide whether a page is the business's own site.

    The rule is deliberately conjunctive. A page must state a name that agrees
    with the listing **and** supply one independent signal (the same phone or the
    same address); the same name alone is what a directory, a competitor's
    mention or a namesake elsewhere also produces, and the task book is explicit
    that those must not confirm an official site.

    Nothing here is a "deliverable contact" claim: a confirmed site means the
    page is *this business's own*, which is what stage D then visits.
    """
    from .addresscheck import is_name_shaped
    from .enrich import values_agree  # local import: enrich imports config, not this

    host = registrable_domain(urlsplit(page_url).hostname or "") or (
        urlsplit(page_url).hostname or ""
    )
    if is_directory_host(host):
        return Ownership(
            STATUS_REJECTED,
            REASON_DIRECTORY_HOST,
            f"{host} 是目录/平台/聚合站，不是企业官网",
            {"host": host},
        )

    # 048: a listing whose "address" is the business's own name states no
    # address. Left in place it would count as the independent signal whenever
    # the page repeats the name in its address field too -- the name agreeing
    # with itself, which is precisely the conjunction this rule exists to
    # prevent. New ingests no longer store such a value; this guard is for rows
    # written before that fix, so no historical row has to be rewritten.
    address_ignored = ""
    if listing_address and is_name_shaped(listing_address, entity_name):
        address_ignored = listing_address
        listing_address = ""

    page_name = str(page_values.get("title") or "").strip()
    if not page_name:
        return Ownership(
            STATUS_REJECTED,
            REASON_NO_NAME_ON_PAGE,
            "候选页面没有可读的企业名称，无法确认归属",
            {"url": page_url},
        )

    name_ok = values_agree(
        "title", entity_name, page_name, country=country, rules=rules
    )
    if not name_ok:
        return Ownership(
            STATUS_REJECTED,
            REASON_NAME_MISMATCH,
            f"页面名称与名录名称不一致：名录={entity_name!r} 页面={page_name!r}",
            {"listing_name": entity_name, "page_name": page_name},
        )

    # 050: the mirror of the listing guard above. A page that writes its own
    # name into its address field states no address either, and two copies of
    # the name -- one from each side -- are still one fact. Guarding only the
    # listing side left this half of the pair open, and 049 had already found
    # the shape on real pages.
    page_address = str(page_values.get("address") or "").strip()
    page_address_ignored = ""
    if page_address and is_name_shaped(page_address, page_name):
        page_address_ignored = page_address
        page_address = ""

    phone_ok = bool(
        listing_phone
        and page_values.get("phone")
        and values_agree("phone", listing_phone, page_values["phone"], country=country, rules=rules)
    )
    address_ok = bool(
        listing_address
        and page_address
        and values_agree("address", listing_address, page_address, country=country, rules=rules)
    )

    evidence = {
        "host": host,
        "page_name": page_name,
        "listing_name": entity_name,
        "phone_agrees": phone_ok,
        "address_agrees": address_ok,
    }
    if address_ignored:
        evidence["listing_address_ignored"] = {
            "value": address_ignored,
            "why": "名录的地址字段写的是企业名称本身；它不是地址，不能作为独立信号",
        }
    if page_address_ignored:
        evidence["page_address_ignored"] = {
            "value": page_address_ignored,
            "why": "页面的地址字段写的是页面自己的名称；它不是地址，不能作为独立信号",
        }
    if phone_ok or address_ok:
        which = "电话" if phone_ok else "地址"
        return Ownership(
            STATUS_CONFIRMED,
            "confirmed_by_name_and_" + ("phone" if phone_ok else "address"),
            f"页面名称与名录一致，且{which}一致",
            evidence,
        )

    # Name agrees but nothing independent does: a lead for a human, not a fact.
    return Ownership(
        STATUS_PENDING,
        REASON_ONLY_NAME,
        "只有名称相符，缺电话/地址佐证，留待人工确认（不写入官网）",
        evidence,
    )


def _page_contact_values(page_url: str, body: str) -> dict[str, str]:
    """The identity fields one page states, by the rule the enricher uses.

    Kept identical to :meth:`collector.enrich.Enricher`'s reading of a page so a
    site does not look different depending on which stage looked at it: the same
    extractor, the same fields, and the ``<title>`` used only when no JSON-LD
    organisation name was found.
    """
    extraction = extract_page(page_url, body or "")
    values: dict[str, str] = {}
    for value in extraction.values:
        if value.field in ("title", "phone", "address") and value.raw_value:
            values.setdefault(value.field, str(value.raw_value).strip())
    if not values.get("title"):
        values["title"] = extraction.title
    return values


# --- the stage --------------------------------------------------------------


DEFAULT_POLITE_DELAY_SECONDS = 0.5
DEFAULT_PAGES_PER_SITE = 2


class SiteDiscoverer:
    """Runs the candidate finders and judges what they propose."""

    def __init__(
        self,
        cfg: Config,
        *,
        fetcher: Fetcher | None = None,
        sleep: Callable[[float], None] = time.sleep,
        polite_delay_seconds: float = DEFAULT_POLITE_DELAY_SECONDS,
        now_iso: Callable[[], str] = utc_now_iso,
        page_cache: PageCache | None = None,
    ):
        self.cfg = cfg
        self.sleep = sleep
        self.polite_delay_seconds = polite_delay_seconds
        #: Injectable so a test can move time: the retry cooldown is compared
        #: against this, and a test that had to sleep for ten minutes would
        #: either be slow or silently never exercise the path.
        self.now_iso = now_iso
        #: Pages this run already obtained (045.3). Shared with the enrichment
        #: stage when both run in one process, so the contact page discovery
        #: read is not read a second time a minute later.
        self.page_cache = page_cache
        self.fetcher = fetcher or Fetcher(
            timeout_seconds=min(cfg.timeout_seconds, 30),
            proxy=cfg.proxy,
            policy=UrlPolicy(),
        )

    # -- configuration -------------------------------------------------------

    def section(self) -> dict:
        return self.cfg.sitediscovery

    def enabled(self) -> bool:
        return self.cfg.sitediscovery_enabled

    def finders(self) -> list[CandidateFinder]:
        configured = self.cfg.sitediscovery_sources
        names = configured or [
            cls.name for cls in FINDER_CLASSES if cls.name != SearchFinder.name
        ]
        out: list[CandidateFinder] = []
        for cls in FINDER_CLASSES:
            if cls.name in names:
                out.append(cls(self.cfg))
        return out

    def manifests(self) -> list[dict]:
        return [finder.describe() for finder in (cls(self.cfg) for cls in FINDER_CLASSES)]

    def _budget(self) -> tuple[int, int]:
        return self.cfg.sitediscovery_request_budget, self.cfg.sitediscovery_seconds_budget

    def pages_per_site(self) -> int:
        return max(1, self.cfg.sitediscovery_pages_per_site)

    # -- entry point ---------------------------------------------------------

    def discover_run(self, conn, run_id: int) -> DiscoveryStats:
        stats = DiscoveryStats()
        entities = list(db.iter_run_entities(conn, run_id))
        # One business, one discovery. A run's entities include records that
        # stage E already merged into another; walking them all would check the
        # same company once per record and spend the request budget on
        # duplicates (034 第一批第 4 条). Candidates are still *collected* from
        # every record — a merged record may carry the only address — but they
        # are keyed by the business that survived, so they collapse.
        canonical = db.canonical_map(conn)

        def survivor_of(entity_id: int) -> int:
            return int(canonical.get(entity_id, entity_id))

        survivors: dict[int, dict] = {}
        members: dict[int, list[dict]] = {}
        for entity in entities:
            entity_id = int(entity["id"])
            survivor = survivor_of(entity_id)
            members.setdefault(survivor, []).append(entity)
            if survivor == entity_id:
                survivors[survivor] = entity
        stats.entities_considered = len(survivors)
        stats.entities_merged_away = len(entities) - len(survivors)

        finders = self.finders()
        stats.finders = [finder.name for finder in finders]
        stats.unavailable_finders = [
            f"{cls.name}: {cls(self.cfg).not_configured_reason()}"
            for cls in FINDER_CLASSES
            if cls.name in [f.name for f in finders] and not cls(self.cfg).is_configured()
        ]
        stats.finder_notes = [
            f"{finder.name}: {finder.configuration_note()}"
            for finder in finders
            if finder.configuration_note()
        ]

        by_key = {str(entity["stable_key"]): int(entity["id"]) for entity in entities}
        by_name = {
            normalize_text(entity["name"]): int(entity["id"])
            for entity in entities
            if entity["name"]
        }

        proposals: list[tuple[int, Candidate]] = []
        for finder in finders:
            if not finder.is_configured():
                continue
            proposals.extend(finder.find(entities, by_key, by_name))

        # Collected *after* the finders ran, not before: "the lookup answered
        # for 0 of 100 names" is something only the run itself knows, and it is
        # the difference between "no official site is published anywhere" and
        # "this source does not cover businesses like these".
        stats.finder_notes += [
            f"{finder.name}: {note}"
            for finder in finders
            for note in getattr(finder, "runtime_notes", [])
        ]

        # One candidate per (business, url), first finder wins, order stable.
        seen: dict[tuple[int, str], Candidate] = {}
        for entity_id, candidate in proposals:
            url = normalize_url(candidate.url)
            if not url:
                continue
            key = (survivor_of(entity_id), url)
            if key in seen:
                stats.candidates_deduped += 1
                continue
            seen[key] = candidate
        stats.candidates_found = len(seen)

        max_requests, max_seconds = self._budget()
        started_at = time.monotonic()
        fetches = 0
        rules = self.cfg.rules()
        country = self.cfg.country
        rules_id = rules_fingerprint(rules)
        now_iso = self.now_iso()
        max_attempts = self.cfg.sitediscovery_retry_max_attempts
        cooldown_seconds = self.cfg.sitediscovery_retry_cooldown_seconds
        #: The per-site page budget (the home page counts as one) and the wall
        #: clock the contact-page reads must also respect.
        pages_per_site = self.pages_per_site()
        deadline = (started_at + max_seconds) if max_seconds else None
        stats.rules_id = rules_id

        entity_by_id = {int(entity["id"]): entity for entity in entities}
        for (survivor_id, url), candidate in seen.items():
            # The surviving record carries the business's name and address; a
            # merged-away member may carry the website the listing gave, so the
            # "already has a site" test looks at the whole group.
            group = members.get(survivor_id) or []
            entity = survivors.get(survivor_id) or entity_by_id.get(survivor_id)
            if entity is None:
                continue
            group_website = next((e["website"] for e in group if e["website"]), None)

            # A verdict is only as good as what it was allowed to use and the
            # rules it was reached under, so both are part of the key a resumed
            # run compares against (042).
            checkpoint_key = f"c:{survivor_id}:{url}"
            other_site = "" if not group_website or group_website == url else group_website
            fingerprint = evidence_fingerprint(entity, other_site)
            stored, legacy = self._load_checkpoint(
                conn, run_id, survivor_id, url, group
            )
            again, why = decide_recheck(
                stored,
                fingerprint=fingerprint,
                rules_id=rules_id,
                now_iso=now_iso,
                max_attempts=max_attempts,
            )
            if not again:
                stats.candidates_skipped_seen += 1
                if legacy:
                    stats.candidates_legacy_adopted += 1
                if why == "cooling_down":
                    stats.candidates_skipped_cooling += 1
                elif why == "attempts_exhausted":
                    stats.candidates_failed_given_up += 1
                if legacy:
                    # Upgrade it in place: the verdict was adopted under today's
                    # rules and evidence, so the next pass reads it directly
                    # instead of recovering it again. No request is spent.
                    db.set_checkpoint(
                        conn, run_id, "sitediscovery", checkpoint_key,
                        {
                            "status": stored.get("status"),
                            "reason": stored.get("reason"),
                            "attempts": int(stored.get("attempts") or 1),
                            "rules": rules_id,
                            "fingerprint": fingerprint,
                            "judged_at": now_iso,
                        },
                    )
                continue
            if stored:
                if why == "retry_failure":
                    stats.candidates_retried += 1
                elif why in ("rules_changed", "evidence_changed"):
                    stats.candidates_rejudged += 1
                elif legacy:
                    stats.candidates_legacy_adopted += 1

            # Across runs, opt-in: reuse a confirmation that is still inside the
            # configured window. Off by default (0 days) so a new run re-asks —
            # the safe direction, since a site that changed has to be able to
            # show it. Only *confirmed* verdicts are reused; a rejection or a
            # pending is re-judged.
            reuse_days = self.cfg.sitediscovery_reuse_days
            if reuse_days > 0:
                cutoff = _days_ago_iso(reuse_days)
                earlier = db.recent_site_verdict(
                    conn, entity_id=survivor_id, url=url, newer_than=cutoff
                )
                if earlier is not None:
                    db.upsert_site_candidate(
                        conn,
                        run_id=run_id,
                        entity_id=survivor_id,
                        url=url,
                        host=registrable_domain(urlsplit(url).hostname or ""),
                        found_by=candidate.found_by,
                        origin=candidate.origin,
                        status=STATUS_CONFIRMED,
                        reason=earlier["reason"],
                        note=(earlier["note"] or "") + REUSED_NOTE,
                        evidence=_with_judgement(
                            json.loads(earlier["evidence_json"] or "{}"),
                            rules_id=rules_id,
                            fingerprint=fingerprint,
                            attempts=int((stored or {}).get("attempts") or 0),
                            basis="reused_confirmed_verdict",
                        ),
                        http_status=earlier["http_status"],
                    )
                    stats.candidates_reused += 1
                    db.set_checkpoint(
                        conn, run_id, "sitediscovery", checkpoint_key,
                        {
                            "status": STATUS_CONFIRMED,
                            "reason": earlier["reason"],
                            "attempts": 1,
                            "rules": rules_id,
                            "fingerprint": fingerprint,
                            "judged_at": now_iso,
                            "basis": "reused_confirmed_verdict",
                        },
                    )
                    continue

            if group_website and not db.has_fact(
                conn,
                entity_id=survivor_id,
                field="website",
                provider=PROVIDER,
                raw_value=url,
            ):
                # The listing already named a site; discovery does not second-guess
                # it. Stage D visits it as it always did.
                #
                # The exception is a website *this* stage bound earlier: that is
                # not a second opinion to defer to, it is the verdict itself, and
                # a re-judgement -- new evidence, or new rules -- has to be able
                # to revisit it instead of being told to stop. Revisiting it
                # costs a request and reaches the same answer while the page
                # still says the same thing.
                db.upsert_site_candidate(
                    conn,
                    run_id=run_id,
                    entity_id=survivor_id,
                    url=url,
                    host=registrable_domain(urlsplit(url).hostname or ""),
                    found_by=candidate.found_by,
                    origin=candidate.origin,
                    status=STATUS_REJECTED,
                    reason=REASON_LISTING_SITE,
                    note="名录已给出官网，候选不参与判定",
                    evidence=_with_judgement(
                        {}, rules_id=rules_id, fingerprint=fingerprint,
                        attempts=0, basis="listing_already_has_a_site",
                    ),
                )
                stats.rejected += 1
                db.set_checkpoint(
                    conn, run_id, "sitediscovery", checkpoint_key,
                    {
                        "status": STATUS_REJECTED,
                        "reason": REASON_LISTING_SITE,
                        "attempts": 1,
                        "rules": rules_id,
                        "fingerprint": fingerprint,
                        "judged_at": now_iso,
                    },
                )
                continue

            stats.entities_without_site += 1

            if max_requests and fetches >= max_requests:
                stats.candidates_skipped_budget += 1
                stats.budget_reason = f"request budget exhausted ({max_requests} fetches)"
                continue
            if max_seconds and (time.monotonic() - started_at) >= max_seconds:
                stats.candidates_skipped_budget += 1
                stats.budget_reason = f"time budget exhausted ({max_seconds}s)"
                continue

            verdict, fetched, status_code = self._check_candidate(
                conn,
                run_id,
                entity,
                url,
                country=country,
                rules=rules,
                stats=stats,
                pages_per_site=pages_per_site,
                remaining_requests=(max_requests - fetches) if max_requests else None,
                deadline=deadline,
            )
            fetches += fetched

            attempts = int((stored or {}).get("attempts") or 0) + 1
            new_state = {
                "status": verdict.status,
                "reason": verdict.reason,
                "attempts": attempts,
                "rules": rules_id,
                "fingerprint": fingerprint,
                "http_status": status_code,
                "judged_at": now_iso,
            }
            if verdict.status == STATUS_ERROR:
                # A failure is not an answer, but it is also not a reason to ask
                # again at once: the next attempt waits out a cooldown, and the
                # attempt count is what eventually stops asking.
                new_state["retry_after"] = _plus_seconds_iso(now_iso, cooldown_seconds)
            db.set_checkpoint(conn, run_id, "sitediscovery", checkpoint_key, new_state)

            host = registrable_domain(urlsplit(url).hostname or "")
            recorded = db.upsert_site_candidate(
                conn,
                run_id=run_id,
                entity_id=survivor_id,
                url=url,
                host=host,
                found_by=candidate.found_by,
                origin=candidate.origin,
                status=verdict.status,
                reason=verdict.reason,
                note=verdict.note,
                evidence=_with_judgement(
                    verdict.evidence,
                    rules_id=rules_id,
                    fingerprint=fingerprint,
                    attempts=attempts,
                    basis=why,
                ),
                http_status=status_code,
            )
            if not recorded:
                continue

            # Every verdict is counted here, from the same value that was just
            # stored, so ``stats`` and :meth:`summary` -- which reads the rows
            # back -- can never disagree. ``error`` is already counted by
            # ``_check_candidate`` when the fetch failed.
            if verdict.confirmed:
                # Bound to the business the verdict is about -- the surviving
                # entity, not whichever record happened to be walked last.
                self._bind_website(conn, run_id, survivor_id, url, verdict, stats)
            elif verdict.status == STATUS_PENDING:
                stats.pending += 1
            elif verdict.status == STATUS_REJECTED:
                stats.rejected += 1

        return stats

    # -- one candidate -------------------------------------------------------

    def _load_checkpoint(self, conn, run_id: int, survivor_id: int, url: str, group) -> tuple[dict | None, bool]:
        """The stored verdict on this candidate, wherever it was filed.

        Under the surviving business's key first; then under the key of any
        record the group now contains, because a merge that happened *after*
        the first pass moved the candidate onto another business and the verdict
        is still the one taken when it was proposed. Re-keying it here is what
        keeps that merge from costing a second request (034 第一批第 4 条).
        """
        for entity_id in [survivor_id] + [
            int(member["id"]) for member in group if int(member["id"]) != survivor_id
        ]:
            state = _adopt_checkpoint(
                conn,
                run_id,
                entity_id,
                url,
                db.get_checkpoint(conn, run_id, "sitediscovery", f"c:{entity_id}:{url}"),
            )
            if state[0] is not None:
                return state
        return None, False

    def _check_candidate(
        self,
        conn,
        run_id: int,
        entity,
        url: str,
        *,
        country: str,
        rules: dict,
        stats,
        pages_per_site: int = 1,
        remaining_requests: int | None = None,
        deadline: float | None = None,
    ) -> tuple[Ownership, int, int | None]:
        check = self.fetcher.policy.check(normalize_url(url))
        if not check.ok:
            stats.boundary_rejections += 1
            db.insert_error(
                conn,
                run_id=run_id,
                stage="sitediscovery",
                kind="candidate_boundary_rejected",
                message=f"refused to fetch candidate {url}: {check.reason} ({check.detail})",
                context={"entity": entity["stable_key"], "url": url, "reason": check.reason},
            )
            return (
                Ownership(STATUS_REJECTED, REASON_BOUNDARY, f"未抓取：{check.reason}", {"url": url}),
                0,
                None,
            )

        # A host that cannot possibly be a company's own site is refused before
        # the request, not after it. Reading a directory page to then decide it
        # is a directory would waste a request against exactly the kind of host
        # the task book says not to hammer -- and the verdict is the same either
        # way, so the cheaper order is also the more honest one.
        host = registrable_domain(check.host) or check.host
        if is_directory_host(host):
            return (
                Ownership(
                    STATUS_REJECTED,
                    REASON_DIRECTORY_HOST,
                    f"{host} 是目录/平台/聚合站，不是企业官网（未抓取）",
                    {"host": host},
                ),
                0,
                None,
            )

        site = check.registrable or registrable_domain(check.host)
        result = self.fetcher.fetch(check.url, allowed_domains={site})
        if not result.ok:
            stats.pages_failed += 1
            db.insert_error(
                conn,
                run_id=run_id,
                stage="sitediscovery",
                kind=f"fetch_{result.error_kind or 'error'}",
                message=f"could not read candidate {check.url}: {result.error_kind}",
                context={"entity": entity["stable_key"], "url": check.url},
            )
            stats.errors += 1
            return (
                Ownership(
                    STATUS_ERROR,
                    REASON_NOT_FETCHED,
                    f"抓取失败：{result.error_kind or 'error'}（不能据此确认官网）",
                    {"url": url},
                ),
                1,
                result.status_code,
            )

        stats.pages_fetched += 1
        if self.page_cache is not None:
            self.page_cache.put(result)

        visited: list[tuple[str, dict[str, str]]] = [
            (check.url, _page_contact_values(check.url, result.body or ""))
        ]
        verdict = self._judge_pages(entity, visited, country=country, rules=rules)
        fetched = 1

        # A home page often states only the name. German businesses put the
        # address and the phone in Impressum/Kontakt, and the same question --
        # "is this page this business's own?" -- is answered there. While the
        # budget allows, read those pages of the same site before settling for
        # "only the name agrees". The conjunction is not weakened: the answer
        # still needs the name *and* one independent signal.
        budget = max(0, int(pages_per_site) - 1)
        if verdict.status == STATUS_PENDING and budget:
            for page_url in self._contact_pages(check.url, result.body or "", limit=budget):
                if remaining_requests is not None and fetched >= remaining_requests:
                    stats.pages_skipped_budget += 1
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    stats.pages_skipped_budget += 1
                    break
                page, charged = self._fetch_page(page_url, allowed_domains={site})
                if charged:
                    fetched += 1
                if not page.ok:
                    stats.pages_failed += 1
                    db.insert_error(
                        conn,
                        run_id=run_id,
                        stage="sitediscovery",
                        kind=f"fetch_{page.error_kind or 'error'}",
                        message=(
                            f"could not read contact page {page_url}: "
                            f"{page.error_kind or 'error'}"
                        ),
                        context={"entity": entity["stable_key"], "url": page_url},
                    )
                    stats.errors += 1
                    continue
                stats.pages_fetched += 1
                visited.append((page_url, _page_contact_values(page_url, page.body or "")))
                verdict = self._judge_pages(entity, visited, country=country, rules=rules)
                if verdict.confirmed:
                    break

        evidence = dict(verdict.evidence)
        evidence["pages_visited"] = [page_url for page_url, _ in visited]
        evidence["pages_budget"] = int(pages_per_site)
        return (
            Ownership(verdict.status, verdict.reason, verdict.note, evidence),
            fetched,
            result.status_code,
        )

    # -- multi-page judgement ------------------------------------------------

    def _judge_pages(self, entity, visited, *, country: str, rules: dict) -> Ownership:
        """Judge ownership across the pages of one site, first value wins.

        Each identity field is taken from the first page that states it, and the
        page it came from is kept next to the verdict. A reader can therefore
        see not just "confirmed" but *which page* supplied the address or the
        phone that carried the confirmation -- the difference between "the site
        looks right" and "Impressum says so".
        """
        merged: dict[str, str] = {}
        sources: dict[str, str] = {}
        for page_url, values in visited:
            for field in ("title", "phone", "address"):
                if field not in merged and values.get(field):
                    merged[field] = values[field]
                    sources[field] = page_url
        ownership = judge_ownership(
            entity_name=entity["name"] or "",
            listing_phone=entity["phone_norm"] or "",
            listing_address=entity["address"] or "",
            page_url=visited[0][0],
            page_values=merged,
            country=country,
            rules=rules,
        )
        evidence = dict(ownership.evidence)
        evidence["page_values_from"] = sources
        return Ownership(ownership.status, ownership.reason, ownership.note, evidence)

    def _contact_pages(self, base_url: str, body: str, *, limit: int) -> list[str]:
        """Same-site contact/about/imprint pages worth reading, best first.

        Reuses :meth:`PageLinksFinder`'s reader, i.e. the same page kinds the
        enrichment stage follows, so the two stages agree on what a "contact
        page" is. Off-site links are dropped by the reader and the redirect
        check is the second line of defence.
        """
        extraction = extract_page(base_url, body)
        out: list[str] = []
        for link in extraction.links:
            if link.url and link.url != base_url and link.url not in out:
                out.append(link.url)
            if len(out) >= limit:
                break
        return out

    def _fetch_page(self, url: str, *, allowed_domains: set[str]) -> tuple[Any, bool]:
        """Read a page, reusing one this run already obtained.

        Returns ``(result, charged)``. ``charged`` is ``False`` on a cache hit,
        so the request budget is not spent on a page nobody re-requested.
        """
        if self.page_cache is not None:
            found = self.page_cache.get(url)
            if found is not None and found.ok:
                return found, False
        result = self.fetcher.fetch(url, allowed_domains=allowed_domains)
        if self.page_cache is not None:
            self.page_cache.put(result)
        return result, True

    # -- binding -------------------------------------------------------------

    def _bind_website(self, conn, run_id: int, entity_id: int, url: str, verdict, stats) -> None:
        """Write the confirmed site as an ordinary ``website`` fact.

        Everything downstream reads the fact and the entity column, so the
        enrichment, merge, match and export stages need no change at all.
        """
        from .util import content_hash

        key = content_hash([entity_id, "website", url, PROVIDER, url])
        inserted = db.insert_fact(
            conn,
            entity_id=entity_id,
            observation_id=None,
            run_id=run_id,
            field="website",
            raw_value=url,
            normalized_value=registrable_domain(urlsplit(url).hostname or "") or None,
            provider=PROVIDER,
            source_url=url,
            query_context=None,
            scraped_at=utc_now_iso(),
            evidence_snippet=clip(verdict.note, 300),
            evidence_ref=url,
            extractor_version="site-discovery/1",
            validation_status="confirmed",
            validation_note=verdict.note,
            validation_state="site_confirmed_by_own_page",
            checks_json=json.dumps(verdict.evidence, ensure_ascii=False, sort_keys=True),
            observed_at=utc_now_iso(),
            fact_key=key,
        )
        if inserted:
            stats.confirmed += 1
        db.fill_entity_website(conn, entity_id, url)

    # -- reporting -----------------------------------------------------------

    def summary(self, conn, run_id: int) -> dict:
        """Counts for the UI and the report."""
        rows = db.site_candidate_counts(conn, run_id)
        return {
            "candidates": first_int(rows, "candidates"),
            "confirmed": first_int(rows, "confirmed"),
            "pending": first_int(rows, "pending"),
            "rejected": first_int(rows, "rejected"),
            "errors": first_int(rows, "errors"),
            "by_finder": db.site_candidate_finders(conn, run_id),
        }


def first_int(counts: dict, key: str) -> int:
    return int(counts.get(key) or 0)


def _days_ago_iso(days: int) -> str:
    """The cut-off instant for "a verdict judged within the last *days* days".

    Compared as text against ``site_candidates.judged_at``, which is written by
    :func:`collector.util.utc_now_iso` -- same format, UTC, lexicographically
    ordered, so SQLite's string comparison is a correct time comparison and no
    date parsing is needed.
    """
    from .util import to_iso, utc_now  # local: keeps the import block flat

    return to_iso(utc_now() - _datetime.timedelta(days=int(days))) or ""


# --- what a verdict depended on ----------------------------------------------
#
# 042: the checkpoint used to be the bare value True, which said only "a previous
# pass judged this candidate". It did not say *what* it concluded -- so a fetch
# that failed was skipped for good, and a verdict taken before a merge brought in
# an address, or before the rules changed, stood unchallenged. The checkpoint now
# records the verdict, what it was allowed to use, and under which rules.


def _sha16(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def rules_fingerprint(rules: dict) -> str:
    """Everything a verdict depends on besides the input.

    The version, the validation rules the name/phone/address agreement is tested
    with, and the directory deny list. Change any of them and every stored
    verdict becomes re-judgeable, which is what "the rules changed" has to mean.
    """
    return ":".join(
        (
            JUDGE_RULES_VERSION,
            _sha16(rules or {}),
            _sha16(sorted(DIRECTORY_HOST_MARKERS)),
        )
    )


def evidence_fingerprint(entity: Any, group_website: str = "") -> str:
    """What the verdict was allowed to use.

    The surviving record's name, address and phone -- exactly what
    :func:`judge_ownership` is handed -- plus a site the group already carries,
    which is what decides the "the listing named one" branch.

    Deliberately *not* every member's fields: a merge that folds a true
    duplicate into the survivor leaves this unchanged, so it costs no second
    request, while a merge that fills in an address the first verdict did not
    have changes it, and a "only the name agrees" verdict taken without that
    address is no longer the verdict we would reach now.

    ``group_website`` is ignored when it *is* the candidate: a confirmation
    writes the candidate into ``entities.website``, and counting that as new
    evidence would make every confirmed verdict invalidate itself on the very
    next pass, which is the self-defeating version of this rule.
    """
    payload = [
        _field(entity, "name_norm"),
        _field(entity, "address_norm"),
        _field(entity, "phone_norm"),
        str(group_website or ""),
    ]
    return _sha16(payload)


def _field(row: Any, key: str) -> str:
    """One column of a row that may be a dict or a ``sqlite3.Row``."""
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return ""
    return "" if value is None else str(value)


def decide_recheck(
    state: dict | None,
    *,
    fingerprint: str,
    rules_id: str,
    now_iso: str,
    max_attempts: int,
) -> tuple[bool, str]:
    """Whether a checkpointed candidate has to be judged again, and why.

    Returns ``(again, why)``. ``why`` is one of ``new``, ``legacy_unknown``,
    ``rules_changed``, ``evidence_changed``, ``retry_failure`` -- all of which
    spend a request -- or ``settled``, ``attempts_exhausted``,
    ``cooling_down`` -- which do not. The caller counts them, so a run that did
    not re-ask says *which* of the three reasons kept it from doing so.
    """
    if not state:
        return True, "new"

    status = state.get("status")
    legacy = bool(state.get("legacy")) or not status
    attempts = int(state.get("attempts") or 0)
    if legacy and not status:
        # A pre-042 checkpoint that says nothing recoverable: one anonymous
        # attempt. Judged once, then written in the new shape -- never skipped
        # on the strength of a value that cannot be read.
        return True, "legacy_unknown"

    # Only compared when the stored state actually carries them: a legacy
    # checkpoint has neither, and an empty field is "unknown", not "different".
    if state.get("rules") and state["rules"] != rules_id:
        return True, "rules_changed"
    if state.get("fingerprint") and state["fingerprint"] != fingerprint:
        return True, "evidence_changed"

    if status == STATUS_ERROR:
        if attempts >= max(1, int(max_attempts)):
            return False, "attempts_exhausted"
        if state.get("retry_after") and str(now_iso) < str(state["retry_after"]):
            return False, "cooling_down"
        return True, "retry_failure"
    if status in SETTLED_STATUSES:
        return False, "settled"
    return True, "legacy_unknown"


def _with_judgement(
    evidence: dict | None,
    *,
    rules_id: str,
    fingerprint: str,
    attempts: int,
    basis: str,
) -> dict:
    """Attach what the verdict depended on to the stored evidence.

    042: the verdict row is what the export shows, so the rules id and the
    evidence fingerprint belong next to it -- otherwise "confirmed" can be read
    without knowing which rules or which evidence it was reached under, and a
    judgement taken before a merge brought in an address looks identical to one
    taken after.
    """
    out = dict(evidence or {})
    out["_judgement"] = {
        "rules": rules_id,
        "evidence": fingerprint,
        "attempts": int(attempts),
        "basis": basis,
    }
    return out


def _adopt_checkpoint(conn, run_id: int, entity_id: int, url: str, state: Any) -> tuple[dict | None, bool]:
    """Normalise a stored checkpoint; pre-042 wrote the bare value ``True``.

    A legacy value says "judged" without saying what. Rather than guess, the
    verdict the same run stored in ``site_candidates`` is adopted -- that row is
    the evidence, and it is on disk. If even that is gone, the result is one
    anonymous attempt, which is judged again once instead of standing.
    """
    if state is None:
        return None, False
    if isinstance(state, dict):
        return state, False
    row = db.site_candidate_row(conn, run_id, entity_id, url)
    if row is None:
        return {"status": None, "attempts": 0, "legacy": True}, True
    return (
        {
            "status": row["status"],
            "reason": row["reason"],
            "attempts": 1,
            "legacy": True,
        },
        True,
    )


def _plus_seconds_iso(moment: str, seconds: int) -> str:
    """``moment`` plus ``seconds``, in the same lexicographically ordered form."""
    from .util import to_iso, utc_now

    text = str(moment or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        base = _datetime.datetime.fromisoformat(text)
    except ValueError:
        base = utc_now()
    return to_iso(base + _datetime.timedelta(seconds=int(seconds))) or ""


# --- file readers shared with the screen ------------------------------------
#
# The screen cannot invent a page list per business out of thin air, and asking
# a person to hand-edit YAML does not make the feature part of the screen. Both
# finders therefore read the *same* two file shapes here, so what the screen
# writes and what the collector reads cannot drift apart.


def _sniff_delimiter(sample: str) -> str:
    if not sample.strip():
        return ","
    try:
        return csv_module.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv_module.Error:
        return ","


def read_csv_rows(path: str | pathlib.Path) -> list[dict[str, str]]:
    """Read a small CSV into a list of ``{lower-case header: value}`` dicts.

    Blank rows are dropped and the header is case-folded, so ``Name`` and
    ``name`` are the same column. A missing file is an empty list rather than an
    error: "the operator has not written this file yet" and "the file has no
    matching rows" both mean no candidates, and neither is a crash.
    """
    file = pathlib.Path(path)
    if not file.is_file():
        return []
    with open(file, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        reader = csv_module.reader(handle, delimiter=_sniff_delimiter(sample))
        header: list[str] = []
        rows: list[dict[str, str]] = []
        for row in reader:
            if not row or all(not cell.strip() for cell in row):
                continue
            if not header:
                header = [cell.strip().casefold() for cell in row]
                continue
            rows.append(
                {
                    name: (row[index].strip() if index < len(row) else "")
                    for index, name in enumerate(header)
                }
            )
    return rows


#: Column names a page manifest may use for the business it belongs to, and for
#: the saved file. Kept as alternatives so a person can write the header they
#: find natural instead of one the parser insists on.
_PAGE_TARGET_COLUMNS = ("stable_key", "entity", "name", "key")
_PAGE_PATH_COLUMNS = ("path", "file", "html")
_PAGE_URL_COLUMNS = ("page", "url", "source_url")


def read_pages_manifest(path: str | pathlib.Path) -> list[dict[str, str]]:
    """Read a saved-page manifest: one row per business, pointing at its HTML.

    Columns (any of the accepted aliases works):

    ``stable_key``/``entity``/``name``  the business the page belongs to
    ``path``/``file``/``html``          the saved HTML file
    ``page``/``url``/``source_url``     the URL the page was saved from

    The page URL is what makes the finder useful -- without it there is no
    "elsewhere" to compare a link against, so a row without one is kept (the
    file name stands in) but is effectively unable to propose anything, and that
    shows up in the counts rather than in a silent nothing.
    """
    out: list[dict[str, str]] = []
    for row in read_csv_rows(path):
        target = next((row.get(name, "") for name in _PAGE_TARGET_COLUMNS if row.get(name)), "")
        file_path = next((row.get(name, "") for name in _PAGE_PATH_COLUMNS if row.get(name)), "")
        page_url = next((row.get(name, "") for name in _PAGE_URL_COLUMNS if row.get(name)), "")
        if not target or not file_path:
            continue
        out.append({"entity": target, "path": file_path, "page": page_url})
    return out
