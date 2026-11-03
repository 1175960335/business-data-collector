"""The configurable business-directory provider.

Stage E asks for "一个可配置企业目录". The design vocabulary — a start URL, a
detail-page pattern, per-field page selectors, a politeness delay, and stop
conditions for empty or repeated listing pages — is borrowed from the
configuration layout of ``Business-Directory-Scraper``.

That project ships **no LICENSE file**, so this module follows the task book's
rule strictly: the *design vocabulary above* is reused, and **none of its code
is copied**. The implementation is written against this collector's own
facilities — the stage D :class:`~collector.webfetch.Fetcher` with its URL
boundary policy, and the selector engine in :mod:`collector.selector` — which
did not exist in that project.

The selector engine's limits are recorded in :class:`SelectorError`: a
pseudo-class or sibling combinator is refused at compile time rather than
silently matching nothing, because a selector that cannot work and does not say
so turns a typo into "the directory returned no businesses".

Each listing page is fetched exactly once. Its detail links are collected from
the same response, and the pagination link is taken from the same response, so
the run spends its budget on new pages rather than re-reading ones it already
has.
"""

from __future__ import annotations

import re
import time
import urllib.parse

# --- what is actually known about each preset's site ------------------------
#
# Round 034, item 3.2: three states, and they must not be written as one.
#
#   在线目录    -- a live run against this site has been done here;
#   离线已验证  -- saved pages from this site have been read end to end, but the
#                  live path is not something this project has working;
#   实时受限    -- the live path was tried and the site refuses it (429).
#
# The distinction matters because "已验证" on its own reads as "a live run will
# work", which is false for 顺企网: its offline samples are verified and its live
# path answers 429. So offline and live are two separate fields, each with its
# own evidence, and neither is allowed to stand in for the other.
OFFLINE_VERIFIED = "verified"
OFFLINE_UNKNOWN = "unknown"
LIVE_VERIFIED = "verified"
LIVE_RATE_LIMITED = "rate_limited"
LIVE_UNKNOWN = "unknown"

PRESET_VERIFICATION: dict[str, dict[str, str]] = {
    "b2b168": {
        "offline": OFFLINE_VERIFIED,
        "live": LIVE_VERIFIED,
        "evidence": "019/020 轮在线实跑：6 符合 / 2 待确认 / 14 不符合",
    },
    "shunqiwang": {
        "offline": OFFLINE_VERIFIED,
        "live": LIVE_RATE_LIMITED,
        "evidence": "025 轮离线样本端到端验证；实时路径对连续请求返回 429",
    },
    "generic": {
        "offline": OFFLINE_UNKNOWN,
        "live": LIVE_UNKNOWN,
        "evidence": "用适配器默认选择器，多数站点取不到值，未做验证",
    },
}

_LABELS = {
    (OFFLINE_VERIFIED, LIVE_VERIFIED): "离线与实时均可用",
    (OFFLINE_VERIFIED, LIVE_RATE_LIMITED): "离线样本已验证，实时采集受限",
    (OFFLINE_VERIFIED, LIVE_UNKNOWN): "离线样本已验证，实时未验证",
    (OFFLINE_UNKNOWN, LIVE_VERIFIED): "实时可用，离线未验证",
    (OFFLINE_UNKNOWN, LIVE_RATE_LIMITED): "实时采集受限，离线未验证",
    (OFFLINE_UNKNOWN, LIVE_UNKNOWN): "未验证",
}


def verification_label(preset_key: str) -> str:
    """One sentence for a preset's offline and live state, from the same table.

    The screen and the export both read this, so they cannot drift: a third
    state added to :data:`PRESET_VERIFICATION` shows up in both or in neither.
    """
    row = PRESET_VERIFICATION.get(preset_key) or {}
    label = _LABELS.get((row.get("offline", ""), row.get("live", "")), "未验证")
    evidence = str(row.get("evidence") or "").strip()
    return f"{label}（{evidence}）" if evidence else label
from dataclasses import dataclass
from typing import Any, Iterable

from ..htmlextract import extract_page
from ..selector import Element, SelectorError, compile_selector, parse_html, select
from .base import (
    Provider,
    ProviderOutcome,
    ProviderTask,
    RunContext,
    SourceRecord,
    listing_scope,
    write_envelopes,
)

#: Selector defaults, keyed by canonical field. An operator's
#: ``field_selectors`` replaces them.
#:
#: ``phone`` and ``email`` deliberately have **no default**. A directory that
#: carries them as links needs the ``href``, not the anchor's label: selecting
#: ``a[href^='tel:']`` and taking its text yields "call us" as the phone number,
#: which then reaches the database as a fact. The link and JSON-LD paths below
#: read the value itself, and an operator who wants a visible-number element can
#: still say so explicitly in ``field_selectors``.
DEFAULT_SELECTORS: dict[str, str] = {
    "title": "h1",
    "address": "[class~='adr'], [class~='address'], [class~='anschrift']",
    "website": "a[href*='website='], a[href*='url=']",
}

#: How many listing pages to follow by default when no limit is configured.
DEFAULT_MAX_LISTING_PAGES = 20

#: How many detail pages to read from one entry by default.
DEFAULT_MAX_DETAIL_PAGES = 50

#: Canonical fields a detail page can produce. An operator's
#: ``field_selectors`` may add more; these are the ones this collector models,
#: and ``description`` is what the industry match reads (task book 021 A).
DETAIL_FIELDS = ("title", "address", "phone", "website", "category", "description")


@dataclass
class ListingWalk:
    """What a listing walk read, and why it stopped.

    ``stop["reason"]`` is a closed set: ``no_more_rows``, ``no_next_link``,
    ``repeated_page``, ``revisited_url``, ``listing_page_limit``,
    ``fetch_failed``, ``none``. Keeping it closed is what lets the screen and
    the report print a sentence instead of guessing from the row count.
    """

    listings: list[str]
    details: list[tuple[str, str]]
    stop: dict
    #: ``(state, note)`` for the container the harvest was (or was not) confined
    #: to. ``state`` is one of ``not_configured``, ``restricted``,
    #: ``unrestricted_no_match``, ``unrestricted_selector_error``.
    scope: tuple[str, str] = ("not_configured", "")


def _http_kind(status: Any, fallback: str = "") -> str:
    """The error kind for a failed fetch, naming the status when there is one.

    ``http_429`` rather than a bare ``http_error`` because the run treats a
    rate limit differently from a broken page: the first is a cooldown to
    record and show, the second is a URL to fix.
    """
    try:
        code = int(status)
    except (TypeError, ValueError):
        return fallback or "fetch_failed"
    if code == 429:
        return "http_429"
    if code in (401, 403):
        return f"http_{code}"
    if code >= 500:
        return f"http_{code}"
    return f"http_{code}"


class BusinessDirectoryProvider(Provider):
    """One or more hand-configured business directories."""

    name = "directory"
    title = "Configurable business directory (start URL + selectors)"
    license = "the target site's terms — configured by the operator"
    attribution = (
        "Records are copied from a website the operator selected. Its terms "
        "apply to the collected data, not to this tool."
    )
    requires_account = False
    provenance = (
        "Design vocabulary (start_url / detail_pattern / field_selectors / "
        "politeness / stop conditions) follows the configuration layout of "
        "Business-Directory-Scraper. That repository has no LICENSE file, so no "
        "code was copied from it; this implementation uses the collector's own "
        "Fetcher and selector engine."
    )

    # -- configuration -------------------------------------------------------

    @property
    def settings(self) -> dict:
        node = (self.cfg.data.get("sources_map") or {}).get(self.name) or {}
        return node if isinstance(node, dict) else {}

    def entries(self) -> list[dict]:
        raw = self.settings.get("entries")
        if not isinstance(raw, list):
            return []
        return [
            entry
            for entry in raw
            if isinstance(entry, dict) and str(entry.get("start_url") or "").strip()
        ]

    def is_configured(self) -> bool:
        return self.name in (self.cfg.data.get("sources") or []) and bool(self.entries())

    def not_configured_reason(self) -> str:
        if self.name not in (self.cfg.data.get("sources") or []):
            return "directory is not listed in sources"
        return "directory needs sources_map.directory.entries with at least one start_url"

    # -- planning ------------------------------------------------------------

    def tasks(self) -> list[ProviderTask]:
        out: list[ProviderTask] = []
        for index, entry in enumerate(self.entries(), start=1):
            name = str(entry.get("name") or f"entry{index}").strip() or f"entry{index}"
            start_url = str(entry.get("start_url") or "").strip()
            out.append(
                ProviderTask(
                    label=f"discover-dir-{_slug(name)}",
                    query=start_url,
                    targets=(start_url,),
                    attempt=1,
                    extra={"entry": entry, "entry_name": name},
                )
            )
        return out

    # -- execution -----------------------------------------------------------

    def run_task(self, task: ProviderTask, ctx: RunContext) -> ProviderOutcome:
        limit = ctx.budget_reason()
        if limit:
            return ProviderOutcome(ok=False, skipped_reason=limit)

        entry = task.extra.get("entry") or {}
        start_url = str(entry.get("start_url") or "").strip()
        base_host = urllib.parse.urlsplit(start_url).hostname or ""
        base_domain = _registrable(base_host)

        ctx.spend()
        ctx.be_polite(base_domain)

        # One task, one spend. ``RunBudget`` counts *tasks*, like every other
        # provider: counting each fetched page here as well would spend the
        # budget twice over (once for the task, once for its first page) and
        # make ``max_requests`` mean something different for this source than
        # for the others. How much one task may fetch is bounded by the
        # ``max_listing_pages`` / ``max_detail_pages`` / stop-condition settings
        # below, which are the right knob and are per-directory rather than
        # run-wide.
        # The client this source's ``network`` setting asks for: ``direct`` here
        # is a real direct connection, not "whatever the environment had".
        fetcher = ctx.fetcher_for(self.name)
        selectors = self._selectors(entry)
        detail_pattern = _compile_detail_pattern(entry)
        listing_selector = _compile(entry.get("pagination"))
        delay = _float(entry.get("delay_seconds"), 0.0)
        max_listing = _int(entry.get("max_listing_pages"), DEFAULT_MAX_LISTING_PAGES)
        max_detail = _int(entry.get("max_detail_pages"), DEFAULT_MAX_DETAIL_PAGES)
        stop_empty = max(1, _int(entry.get("stop_after_empty_pages"), 2))
        stop_repeated = max(1, _int(entry.get("stop_after_repeated_pages"), 2))

        started = time.monotonic()
        # The run-wide budget is checked once, before the task starts, and
        # counts this whole task as one request — the same accounting as every
        # other provider. Re-checking it inside the walk would make a task that
        # had just started refuse to do its own first page. How much one task
        # may fetch is bounded by the page limits and stop conditions below.
        walk = self._walk_listings(
            ctx,
            start_url,
            fetcher=fetcher,
            detail_pattern=detail_pattern,
            listing_selector=listing_selector,
            list_selector=entry.get("list_selector"),
            max_listing=max_listing,
            stop_empty=stop_empty,
            stop_repeated=stop_repeated,
            delay=delay,
        )
        listings, detail_urls, stop = walk.listings, walk.details, walk.stop
        scope_state, scope_note = walk.scope

        # Round 026 P2: a listing page that could not be read is a *failure with
        # a reason*, not an empty directory. Before this, a site answering 429 to
        # the very first request looked exactly like a site with no companies in
        # it: the walk broke on the failed fetch, the file was written empty, and
        # the run reported "0 recorded". The status panel needs the difference,
        # and so does anyone reading the run.
        if not listings and stop.get("reason") == "fetch_failed":
            return ProviderOutcome(
                ok=False,
                error=(
                    f"第一个列表页读取失败：{stop.get('url')} "
                    f"(status={stop.get('status')}, {stop.get('error_kind') or 'fetch_failed'})"
                    + (f"；站点要求等待 {stop['retry_after_seconds']} 秒"
                       if stop.get("retry_after_seconds") else "")
                ),
                error_kind=_http_kind(stop.get("status"), stop.get("error_kind")),
                retry_after_seconds=stop.get("retry_after_seconds"),
                duration_seconds=time.monotonic() - started,
            )

        records: list[SourceRecord] = []
        unusable = 0
        detail_failed = 0
        for detail_url in detail_urls[:max_detail]:
            listing_url = detail_url[1]
            ctx.be_polite(base_domain)
            record, failure = self._detail_record(
                ctx, fetcher, detail_url[0], selectors, listing_url
            )
            if record is None:
                # A detail page that could not be read and one whose selectors
                # found no name are both "unusable", but they are not the same
                # fact, so both are counted and the reason is kept.
                unusable += 1
                if failure == "fetch_failed":
                    detail_failed += 1
                continue
            records.append(record)
            if delay:
                ctx.sleep(delay)

        raw_path = ctx.raw_path(task.label, task.attempt)
        written = write_envelopes(
            raw_path,
            self.name,
            records,
            query=start_url,
            context={
                "source_url": start_url,
                "mode": "directory",
                "entry_name": task.extra.get("entry_name"),
                "origin": urllib.parse.urlsplit(start_url).hostname or None,
                "listing_pages": len(listings),
                "detail_urls_seen": len(detail_urls),
                "detail_unusable": unusable,
                "detail_failed": detail_failed,
                # Why the listing walk stopped. Without it, "the site had 3
                # pages" and "the site started refusing after 3 pages" read the
                # same in the output.
                "listing_stop_reason": stop.get("reason"),
                "listing_stop_url": stop.get("url"),
                "listing_stop_status": stop.get("status"),
                # 059: was the harvest confined to the list container, and if
                # not, why not. Without it, "the site listed 23 companies" and
                # "we read 23 links off the whole page, furniture included"
                # look identical in the export.
                "listing_scope": scope_state,
                "listing_scope_note": scope_note,
            },
        )
        return ProviderOutcome(
            ok=True,
            records=written,
            raw_path=raw_path,
            duration_seconds=time.monotonic() - started,
        )

    # -- listings ------------------------------------------------------------

    def _walk_listings(
        self,
        ctx: RunContext,
        start_url: str,
        *,
        fetcher,
        detail_pattern,
        listing_selector,
        list_selector=None,
        max_listing: int,
        stop_empty: int,
        stop_repeated: int,
        delay: float,
    ) -> "ListingWalk":
        """Follow the listing pagination; return the walk and why it stopped.

        059: ``list_selector`` confines the harvest to the container the
        businesses are actually listed in. The offline importer has done this
        since round 034; the live walk used to ignore the very same key, which
        made writing it down a silent no-op for a live run and left the page's
        furniture (recommendation strips, footer link blocks) one site redesign
        away from becoming rows. A selector that cannot be evaluated, or that
        matches nothing, is reported and the harvest runs unrestricted -- the
        same bargain the offline importer makes.

        Both stop conditions are honoured because a directory that stops
        returning rows has nothing left to give, and a repeated page means the
        "next" link points somewhere already read — continuing would burn the
        run's budget without producing evidence.

        The *reason* is returned rather than left implicit, because "there were
        no more pages" and "the site started answering 429 on page 4" produce
        the same short list of rows and are not the same fact (round 026 P2).
        """
        listings: list[str] = []
        details: list[tuple[str, str]] = []
        seen_details: set[str] = set()
        seen_signatures: set[str] = set()
        scope_state = "not_configured"
        scope_note = ""
        current = start_url
        empty_run = 0
        repeated_run = 0
        stop: dict = {"reason": "none", "url": None, "status": None, "error_kind": None,
                      "retry_after_seconds": None}

        while current and len(listings) < max_listing:
            if current in listings:
                stop = {"reason": "revisited_url", "url": current, "status": None,
                        "error_kind": None, "retry_after_seconds": None}
                break
            result = fetcher.fetch(current)
            if delay:
                ctx.sleep(delay)
            if not result.ok:
                # The status and error kind are carried out of here: a 429 is a
                # cooldown, a 404 is a wrong URL, and the two must not be
                # reported as the same thing.
                stop = {
                    "reason": "fetch_failed",
                    "url": current,
                    "status": getattr(result, "status_code", None),
                    "error_kind": getattr(result, "error_kind", "") or "fetch_failed",
                    "retry_after_seconds": getattr(result, "retry_after_seconds", None),
                    "error": getattr(result, "error", "") or "",
                }
                break

            listings.append(current)
            root = parse_html(result.body or "")
            scope = None
            if str(list_selector or "").strip():
                containers, note = listing_scope(root, list_selector)
                if note:
                    scope = None
                    scope_state = (
                        "unrestricted_selector_error"
                        if note.startswith("list_selector 无法求值")
                        else "unrestricted_no_match"
                    )
                    scope_note = note
                else:
                    scope = containers
                    scope_state = "restricted"
                    scope_note = ""
            links = [
                url
                for url, _text, _element in detail_anchor_elements(
                    root, current, detail_pattern, within=scope
                )
            ]
            # The signature is the *content*, not the URL: a "next" link that
            # points at a URL not yet read but serving the same rows is still a
            # repeat, and continuing would spend the run's budget re-collecting
            # links that are already in ``seen_details``. (A signature that
            # included the URL could never fire, because the loop above already
            # breaks on a revisited URL.)
            signature = "|".join(links)
            for link in links:
                if link not in seen_details:
                    seen_details.add(link)
                    details.append((link, current))

            if not links:
                empty_run += 1
                if empty_run >= stop_empty:
                    stop = {"reason": "no_more_rows", "url": current, "status": None,
                            "error_kind": None, "retry_after_seconds": None}
                    break
            else:
                empty_run = 0
            if signature in seen_signatures:
                repeated_run += 1
                if repeated_run >= stop_repeated:
                    stop = {"reason": "repeated_page", "url": current, "status": None,
                            "error_kind": None, "retry_after_seconds": None}
                    break
            else:
                repeated_run = 0
            seen_signatures.add(signature)

            current = _next_listing_url(root, current, listing_selector) or ""
            if not current:
                stop = {"reason": "no_next_link", "url": listings[-1], "status": None,
                        "error_kind": None, "retry_after_seconds": None}

        if stop["reason"] == "none" and len(listings) >= max_listing:
            stop = {"reason": "listing_page_limit", "url": listings[-1] if listings else start_url,
                    "status": None, "error_kind": None, "retry_after_seconds": None}

        return ListingWalk(
            listings=listings, details=details, stop=stop,
            scope=(scope_state, scope_note),
        )

    # -- one detail page -----------------------------------------------------

    def _detail_record(
        self, ctx: RunContext, fetcher, url: str, selectors: dict, listing_url: str
    ) -> tuple[SourceRecord | None, str]:
        """One detail page as a record, plus why it produced none.

        The second element is ``""`` when a record came back, ``fetch_failed``
        when the page could not be read, and ``no_name`` when it was read but
        the selectors found nothing to attribute. The caller counts them apart,
        because "we fetched 20 pages and 3 were empty" and "we could not fetch
        3" are different facts about the source.
        """
        result = fetcher.fetch(url)
        if not result.ok or not result.body:
            return None, "fetch_failed"

        root = parse_html(result.body)
        # Round 026 P2: the field reading and the record shape live in
        # :func:`build_detail_record` so the offline importer
        # (:mod:`collector.sources.localpages`) reads a saved page with exactly
        # the same parser, the same selectors and the same record shape. One
        # implementation, two input channels.
        #
        # Round 026 P0: ``acquired_via="live"`` states the provenance instead of
        # letting the reader guess it back out of the identifier. This record
        # *was* read from the detail page, which this run fetched, so the detail
        # page is its evidence.
        record = build_detail_record(
            root,
            result.body,
            url=url,
            selectors=selectors,
            listing_url=listing_url,
            acquired_via="live",
        )
        return record, "" if record is not None else "no_name"

    # -- selectors -----------------------------------------------------------

    def _selectors(self, entry: dict) -> dict:
        merged = dict(DEFAULT_SELECTORS)
        raw = entry.get("field_selectors") or {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                field = str(key).strip()
                selector = str(value).strip() if value is not None else ""
                if field and selector:
                    merged[field] = selector
        return merged


# --- shared page reading ----------------------------------------------------
#
# These two functions are the single implementation of "read one business
# record off one already-parsed page". The live directory walk and the offline
# importer both call them, so a selector fix reaches both channels at once.


def pick_detail_fields(root: Element, selectors: dict) -> tuple[dict[str, Any], list[str]]:
    """Read the configured selectors off a parsed page.

    Returns ``(picked, unusable_selectors)``. A selector the engine refuses is
    returned in the second element rather than raising: the caller records it on
    the row, so a typo shows up as "unusable_selectors: [...]" instead of as
    "this page had no data".
    """
    picked: dict[str, Any] = {}
    bad_selectors: list[str] = []
    for field, selector in selectors.items():
        try:
            matches = select(root, selector)
        except SelectorError:
            bad_selectors.append(selector)
            continue
        if not matches:
            continue
        if field == "address":
            # The container also holds the map/route controls, so its text is
            # read minus those (see _address_text) rather than wholesale.
            text = " ".join(_address_text(match) for match in matches)
            picked[field] = text.strip(" ,") or None
        else:
            picked[field] = matches[0].text() or None
    return picked, bad_selectors


def build_detail_record(
    root: Element,
    body: str,
    *,
    url: str,
    selectors: dict,
    listing_url: str,
    acquired_via: str = "live",
    snapshot_id: str | None = None,
    observed_at: str | None = None,
) -> SourceRecord | None:
    """One business record from one parsed page, or None when it has no name.

    ``url`` is the page's own address and ``listing_url`` the page that linked
    to it; both are recorded so the evidence can be walked back. ``acquired_via``
    is the caller's statement of how the bytes were obtained (``live`` for a
    fetched page, ``import`` for a file on disk) — the function never guesses it.
    """
    picked, bad_selectors = pick_detail_fields(root, selectors)

    title = picked.get("title")
    if not title:
        # No name means nothing to attribute; inventing one from the page's
        # first heading or its category is not allowed.
        return None

    # mailto:/tel: links and JSON-LD are read in addition to the selectors:
    # many directories carry structured data even where the HTML is vintage,
    # and the stage D extractor already parses it.
    page = extract_page(url, body)
    link_emails = _link_values(root, "mailto:")
    link_phones = _link_values(root, "tel:")
    emails = _merge(link_emails, [value.raw_value for value in page.of_field("emails")])
    phones = _merge(link_phones, [value.raw_value for value in page.of_field("phone")])

    preferred_phone = picked.get("phone") or (phones[0] if phones else None)
    website = picked.get("website") or _own_website(root, url, listing_url)

    return SourceRecord(
        title=title,
        address=picked.get("address") or None,
        phone=preferred_phone,
        website=website,
        emails=emails,
        latitude=None,
        longitude=None,
        category=picked.get("category") or None,
        #: The business's own words about what it does. Kept verbatim: the
        #: matcher decides which part of it is the business line, and a
        #: rewritten description would be our words, not the source's.
        description=picked.get("description") or None,
        source_id=url,
        source_id_kind="directory_url",
        native={
            "detail_url": url,
            "listing_url": listing_url,
            "selectors": selectors,
        },
        extra={
            "emails_from_links": list(link_emails),
            "phones_from_links": list(link_phones),
            "unusable_selectors": bad_selectors,
        },
        acquired_via=acquired_via,
        source_page_url=url,
        detail_url=url,
        detail_fetched=True,
        snapshot_id=snapshot_id,
        observed_at=observed_at,
    )


def compile_detail_pattern(raw: Any):
    """Compile a detail-page pattern, or None when there is nothing to compile."""
    return _compile_detail_pattern({"detail_pattern": raw})


def compile_url_pattern(raw: Any):
    """Compile a pattern matched against the *whole* detail URL, or None.

    Round 034: 顺企网 hosts 2 of the sample listing page's 100 business rows under
    ``https://<id>.b2b.11467.com``, whose path is ``/`` — no path pattern can
    recognise those. A bad pattern is not an error: it compiles to None, which
    means "no URL restriction", the same way a bad path pattern already did.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return re.compile(text)
    except re.error:
        return None


# --- helpers ----------------------------------------------------------------


def _registrable(host: str) -> str:
    from ..webfetch import registrable_domain

    return registrable_domain(host)


def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return (cleaned or "entry")[:limit]


def _float(value: Any, default: float) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        if value is None or isinstance(value, bool):
            return default
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _compile(value: Any) -> Any:
    """Compile a selector from a string or a ``{selector: ...}`` mapping."""
    if not value:
        return None
    if isinstance(value, str):
        selector = value.strip()
        return compile_selector(selector) if selector else None
    if isinstance(value, dict):
        selector = str(value.get("selector") or "").strip()
        return compile_selector(selector) if selector else None
    return None


def _compile_detail_pattern(entry: dict):
    """The detail-page pattern, checked against the URL path only."""
    raw = str(entry.get("detail_pattern") or "").strip()
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error:
        return None


#: How strictly a listing page's detail links must stay on the directory.
#:
#: ``same_host`` (the historical rule) keeps only links whose host is exactly the
#: listing page's. That is right for most directories, and it is what every config
#: written before round 034 means, so it stays the default.
#:
#: ``registrable_domain`` also keeps sub-domains of the directory's own
#: registrable domain. Measured on the 顺企网 sample: 100 of the listing page's
#: business rows are there, but 2 of them link to ``https://<id>.b2b.11467.com``
#: instead of ``/qiye/<id>.htm``, so the exact-host rule dropped 2 rows that the
#: page plainly carried. A sub-domain of the directory's own registrable domain is
#: the same operator, not an outbound link.
DETAIL_HOST_SCOPES = ("same_host", "registrable_domain")


def detail_anchor_elements(
    root: Element,
    base_url: str,
    detail_pattern=None,
    *,
    url_pattern=None,
    host_scope: str = "same_host",
    within: Iterable[Element] | None = None,
) -> list[tuple[str, str, Element]]:
    """Detail links from a listing page, as ``(url, anchor text, element)``.

    The element is what makes round 034's list-row reading possible: a row's
    fields can only be read *relative to the row*, and the row is the container
    around this anchor. :func:`detail_anchors` is the text-only view of exactly
    this, so the two can never disagree about which links count.

    ``within`` restricts the harvest to named containers (e.g. ``ul.companylist``).
    It is not cosmetic: on the 顺企网 sample, widening the host rule to the
    directory's sub-domains picks up 6 business links when only 2 of them are in
    the list — the other 4 are recommendations in the page furniture, and
    importing them would put names on the export that the listing never listed.
    """
    base_host = urllib.parse.urlsplit(base_url).hostname or ""
    base_domain = _registrable(base_host) if base_host else ""
    wide = str(host_scope or "same_host").strip() == "registrable_domain"
    allowed = set(id(node) for node in within) if within is not None else None
    out: list[tuple[str, str, Element]] = []
    seen: set[str] = set()
    for element in root.elements():
        if element.tag != "a":
            continue
        if allowed is not None and not any(
            id(node) in allowed for node in (element, *element.ancestors())
        ):
            continue
        href = element.attr("href")
        if not href:
            continue
        absolute = urllib.parse.urljoin(base_url, href.strip())
        parsed = urllib.parse.urlsplit(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").casefold()
        if base_host:
            if wide and base_domain:
                # Same registrable domain, including its sub-domains.
                if _registrable(host) != base_domain:
                    continue
            elif host != base_host.casefold():
                # A listing page's detail links stay on the directory. Anything
                # else is an outbound link the page happens to carry.
                continue
        if not _pattern_allows(detail_pattern, url_pattern, parsed.path or "/", absolute):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append((absolute, re.sub(r"\s+", " ", element.text() or "").strip(), element))
    return out


def _pattern_allows(path_pattern, url_pattern, path: str, url: str) -> bool:
    """Whether a link counts as a detail link.

    Two patterns because one is not enough: a directory may host its business
    pages under a sub-domain whose *path* is just ``/``, so a path pattern cannot
    express "this is a business page" there — it needs the whole URL. The URL
    pattern is tried first when configured; when it is configured and does not
    match, the path pattern is *not* consulted, so widening one site never
    silently widens another.
    """
    if url_pattern is not None:
        return bool(url_pattern.search(url))
    if path_pattern is not None:
        return bool(path_pattern.search(path))
    return True


def detail_anchors(root: Element, base_url: str, detail_pattern) -> list[tuple[str, str]]:
    """Same-host ``<a>`` links from a listing page, as ``(url, anchor text)``.

    The text is kept because an offline import needs it: a detail page that was
    never saved still yields a row from the listing page, and the row's only
    name is what the listing wrote on the link. ``_detail_links`` is the
    url-only view of this, so the two can never disagree about which links
    count.
    """
    return [
        (url, text)
        for url, text, _element in detail_anchor_elements(root, base_url, detail_pattern)
    ]


def _detail_links(root: Element, base_url: str, detail_pattern) -> list[str]:
    """Same-host links from a listing page, optionally restricted by pattern."""
    return [url for url, _text in detail_anchors(root, base_url, detail_pattern)]


def _next_listing_url(root: Element, current: str, listing_selector) -> str | None:
    """Where the "next" listing page is, or None when there is none.

    Both carriers of the hint are read: an ``<a rel="next">`` in the body, and
    the ``<link rel="next">`` that real directories put in ``<head>``. A site
    that only publishes the head form (dasoertliche.de does) would otherwise be
    read as a single-page directory and every listing past the first silently
    dropped.
    """
    for element in root.elements():
        if element.tag not in ("a", "link"):
            continue
        rel = (element.attr("rel") or "").casefold()
        if "next" in rel.split():
            href = element.attr("href")
            if href:
                return urllib.parse.urljoin(current, href.strip())
    if listing_selector is None:
        return None
    for element in listing_selector.select(root):
        href = element.attr("href")
        if href:
            return urllib.parse.urljoin(current, href.strip())
    return None


def _link_values(root: Element, scheme: str) -> list[str]:
    scheme = scheme.casefold()
    out: list[str] = []
    for element in root.elements():
        if element.tag != "a":
            continue
        href = element.attr("href") or ""
        if not href.casefold().startswith(scheme):
            continue
        value = urllib.parse.unquote(href[len(scheme):].split("?", 1)[0]).strip()
        if value and value not in out:
            out.append(value)
    return out


def _merge(*groups: list[str]) -> list[str]:
    out: list[str] = []
    for group in groups:
        for value in group or []:
            text = (value or "").strip()
            if text and text not in out:
                out.append(text)
    return out


#: Descendant tags that carry an action rather than address prose.
_ADDRESS_CONTROL_TAGS = frozenset({"a", "button", "select", "option"})

#: Phrases directories use for the map/route actions embedded in the address
#: block. Compared as a whole phrase, so a street named "Routenplanerweg" or
#: "Maplestraße" is left alone.
_ADDRESS_ACTION_PHRASES = frozenset(
    {
        "zum kartenausschnitt",
        "kartenausschnitt",
        "routenplaner",
        "route berechnen",
        "route planen",
        "anfahrt",
        "auf karte anzeigen",
        "in karte anzeigen",
        "show map",
        "view map",
        "open map",
        "get directions",
        "directions",
    }
)


def _is_address_action(element: Element) -> bool:
    """Is this descendant a page control rather than part of the address?"""
    if element.tag not in _ADDRESS_CONTROL_TAGS:
        return False
    href = (element.attr("href") or "").strip()
    if href.startswith("#"):
        # An in-page anchor is navigation by definition — it cannot be the
        # business's address, which would point somewhere off the page.
        return True
    if element.classes() & {"noprint", "no-print"}:
        return True
    return element.text().strip().casefold() in _ADDRESS_ACTION_PHRASES


def _address_text(node: Element) -> str:
    """An address container's text, without the actions embedded in it.

    Real directories put the map and route controls *inside* the address block
    (dasoertliche.de: ``Zum Kartenausschnitt`` and ``Routenplaner``), so reading
    the whole subtree appends both to every address the site returns. They are
    controls, not prose, so their text is skipped and the rest is kept verbatim
    — the address itself is never rewritten, only the page chrome removed.

    Deliberately narrow: a link that is *not* an action (a real ``href``, no
    ``noprint``, text that is not a known action phrase) keeps its text, so a
    directory that links the street name does not lose the street.
    """
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif not _is_address_action(child):
            parts.append(_address_text(child))
    joined = " ".join(part for part in parts if part)
    return re.sub(r"\s+", " ", joined).strip()


#: Extensions that mark a link as a file to download rather than a website.
#: A directory that offers its own client/plugin on the page links it from the
#: same place it links the business's site, so an installer is never a website.
_DOWNLOAD_SUFFIXES = frozenset(
    {
        ".exe",
        ".msi",
        ".zip",
        ".rar",
        ".7z",
        ".dmg",
        ".pkg",
        ".apk",
        ".deb",
        ".rpm",
        ".cab",
        ".crx",
        ".xpi",
    }
)


def _own_website(root: Element, detail_url: str, listing_url: str) -> str | None:
    """The business's own site, taken off the detail page.

    The directory's own domain, and anything under it, is not the business's
    website, so it is skipped. A host sharing the directory's second-level
    label under a different TLD is the same publisher (b2b168.net next to
    b2b168.com) and is skipped too, as is a link that points at a file to
    download — measured: without both rules every b2b168.com row came back
    carrying the site's own desktop-client installer as its "website". The raw
    value is kept because stage D refuses to fetch a private or loopback
    address before it is used.
    """
    directory_host = (urllib.parse.urlsplit(listing_url).hostname or "").casefold()
    directory_label = _registrable(directory_host).split(".", 1)[0]
    for element in root.elements():
        if element.tag != "a":
            continue
        href = element.attr("href")
        if not href:
            continue
        absolute = urllib.parse.urljoin(detail_url, href.strip())
        parsed = urllib.parse.urlsplit(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").casefold()
        if not host or host == directory_host:
            continue
        if _registrable(host) and _registrable(host) == _registrable(directory_host):
            continue
        if directory_label and _registrable(host).split(".", 1)[0] == directory_label:
            continue
        if parsed.path.casefold().endswith(tuple(_DOWNLOAD_SUFFIXES)):
            continue
        if "gov" in host.split("."):
            # Chinese sites must publish their MIIT filing number as a link to
            # the regulator (beian.miit.gov.cn), so it sits on every page. A
            # government portal is never the business's own website.
            continue
        return absolute
    return None
