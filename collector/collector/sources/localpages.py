"""The offline saved-page provider (round 026 P2).

Task book P2.1: "已存网页/CSV作为正式可复用入口，支持本地网页导入与按预设抽取
列表行，用户通过界面能操作；复用解析器。导入是离线证据处理，不伪装实时。"

This provider reads HTML pages the operator already saved to disk — a directory
listing page and, where they exist, the detail pages it links to — and writes
the same ``osint.record/1`` envelopes the live sources write. It makes **no HTTP
request at all**: the file *is* the snapshot, and every row it produces is
stamped ``acquired_via="import"`` by :func:`~collector.sources.base.write_envelopes`.

Three things it deliberately refuses to do, because a directory that answers 429
is not a reason to start making things up:

* **It never fetches a link to find out what it says.** A detail link with no
  saved file becomes a *listing-only* row: the name the listing wrote, the
  detail URL this run did not read, and ``detail_fetched`` absent. It is stored
  because the row genuinely appeared on the listing page, and its provenance
  says exactly that.
* **It never invents a capture time.** ``observed_at`` is whatever the operator
  stated on the entry and stays absent otherwise. A file's mtime is when it was
  downloaded, not when the page was published, so it is never used.
* **It never guesses the encoding silently.** Chinese saved pages are commonly
  GB18030 while the HTML declares UTF-8 (or nothing); the reader tries UTF-8
  first and falls back, recording which one worked on the row it produced.

The field reading is not reimplemented. It calls
:func:`collector.sources.directory.build_detail_record`, so a selector fix
reaches the live walk and the offline import at the same time.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import time
import urllib.parse
from dataclasses import replace
from typing import Any

from ..listingfields import as_native, fields_for_anchor, merge_listing_and_detail
from ..selector import Element, parse_html, select
from ..selector import SelectorError
from .base import (
    Provider,
    ProviderOutcome,
    ProviderTask,
    RunContext,
    SourceRecord,
    listing_scope,
    write_envelopes,
)
from .directory import (
    build_detail_record,
    compile_detail_pattern,
    compile_url_pattern,
    detail_anchor_elements,
)

#: Encodings tried, in order, when a saved page does not state a usable one.
#: ``utf-8-sig`` first so a BOM does not end up inside the first company name.
_ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1")

#: How many detail files to read from one listing page by default. Bounded for
#: the same reason the live walk is bounded: one saved page should not turn into
#: an unbounded read of everything the operator has on disk.
DEFAULT_MAX_DETAIL_PAGES = 200


class LocalPagesProvider(Provider):
    """Businesses read out of HTML pages the operator saved earlier."""

    name = "local_pages"
    title = "Saved web pages on disk (offline import; no requests)"
    license = "the target site's terms — the operator saved the pages"
    attribution = (
        "Rows are read from HTML files the operator supplied. They are an import: "
        "this run did not contact the site, and the export says so per row."
    )
    requires_account = False
    provenance = (
        "Reads local HTML with the collector's own selector engine and the same "
        "record builder as the live directory provider "
        "(collector/sources/directory.py:build_detail_record). No third-party "
        "parser and no network access."
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
            if isinstance(entry, dict) and str(entry.get("listing_path") or "").strip()
        ]

    def is_configured(self) -> bool:
        return self.name in (self.cfg.data.get("sources") or []) and bool(self.entries())

    def not_configured_reason(self) -> str:
        if self.name not in (self.cfg.data.get("sources") or []):
            return "local_pages is not listed in sources"
        return (
            "local_pages needs sources_map.local_pages.entries with at least one "
            "listing_path (the saved listing page) and its origin_url"
        )

    def configuration_note(self) -> str:
        """A fact about the *configured* entries, not about this run.

        The CLI's ``plan`` prints it as a WARNING line. The interesting mistake
        here is a path that looks right and is not on disk: the run would then
        find no rows, which reads like "the page had no businesses".
        """
        notes: list[str] = []
        for entry in self.entries():
            listing = pathlib.Path(_expand(str(entry.get("listing_path") or "")))
            if not listing.is_file():
                notes.append(f"{entry.get('name') or 'entry'}: listing_path 不存在 {listing}")
            details_dir = str(entry.get("details_dir") or "").strip()
            if details_dir and not pathlib.Path(_expand(details_dir)).is_dir():
                notes.append(f"{entry.get('name') or 'entry'}: details_dir 不是目录 {details_dir}")
            if not str(entry.get("origin_url") or "").strip():
                notes.append(
                    f"{entry.get('name') or 'entry'}: 没有 origin_url，"
                    "页面里的相对链接无法解析，也无法在证据里写明它来自哪个网址"
                )
        return "；".join(notes)

    # -- planning ------------------------------------------------------------

    def tasks(self) -> list[ProviderTask]:
        out: list[ProviderTask] = []
        for index, entry in enumerate(self.entries(), start=1):
            name = str(entry.get("name") or f"entry{index}").strip() or f"entry{index}"
            listing = _expand(str(entry.get("listing_path") or "").strip())
            out.append(
                ProviderTask(
                    label=f"import-pages-{_slug(name)}",
                    query=listing,
                    targets=(listing,),
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
        listing_path = pathlib.Path(_expand(str(entry.get("listing_path") or "").strip()))
        if not listing_path.is_file():
            # Not a crash: the operator pointed at a path that is not there. Say
            # which one, so the failure is one sentence rather than an empty run.
            return ProviderOutcome(
                ok=False,
                error=f"listing_path 不存在或不是文件：{listing_path}",
                error_kind="input_missing",
            )

        origin_url = str(entry.get("origin_url") or "").strip()
        started = time.monotonic()
        # One task, one spend, like every provider: ``RunBudget`` counts tasks.
        # Nothing is fetched, so nothing is spent per page.
        ctx.spend()

        text, encoding = _read_text(listing_path)
        if text is None:
            return ProviderOutcome(
                ok=False,
                error=f"无法以任何已知编码读取 {listing_path}",
                error_kind="input_unreadable",
            )
        listing_snapshot = _snapshot_id(listing_path, text)

        root = parse_html(text)
        notes: list[str] = []
        pattern = compile_detail_pattern(entry.get("detail_pattern"))
        url_pattern = compile_url_pattern(entry.get("detail_url_pattern"))
        host_scope = str(entry.get("detail_host_scope") or "same_host").strip()
        # Read the listing page's own rows too, not only the detail pages it links
        # to (round 034, first batch): a row that the page states an address and a
        # product line for must keep them even when its detail page was never
        # saved. ``list_preset`` names the label set; it is data, so a new site is
        # a config edit.
        list_preset = str(entry.get("list_preset") or entry.get("local_preset") or "generic").strip()
        read_rows = _bool(entry.get("read_listing_fields"), default=True)
        # The container the businesses are listed in, when the page has one.
        # Restricting the harvest to it is what keeps the page's furniture out of
        # the export; without it, a widened host rule picks up recommendation
        # links that sit outside the list. A selector the engine refuses is
        # reported, not fatal: the harvest then runs unrestricted, as before.
        scope, scope_error = _listing_scope(root, entry.get("list_selector"))
        if scope_error:
            notes.append(scope_error)
        # Without an origin URL, relative ``href`` values cannot be resolved.
        # The listing file's own name is not a substitute for the page's address,
        # so ``origin_url`` is required rather than defaulted.
        if not origin_url:
            return ProviderOutcome(
                ok=False,
                error=(
                    "local_pages 需要 origin_url：它是这个页面保存时的网址，"
                    "用来解析相对链接并在证据里写明数据来自哪里。"
                ),
                error_kind="config_incomplete",
            )
        anchors = detail_anchor_elements(
            root,
            origin_url,
            # The row guard needs the pattern that actually recognises this site's
            # business links, which for a site with sub-domain pages is the URL
            # pattern rather than the path one.
            url_pattern or pattern,
            url_pattern=url_pattern,
            host_scope=host_scope,
            within=scope,
        )

        selectors = _entry_selectors(entry)
        observed_at = str(entry.get("observed_at") or "").strip() or None
        max_detail = _int(entry.get("max_detail_pages"), DEFAULT_MAX_DETAIL_PAGES)
        explicit = _explicit_details(entry)
        details_dir = str(entry.get("details_dir") or "").strip()
        details_root = pathlib.Path(_expand(details_dir)) if details_dir else None

        records: list[SourceRecord] = []
        listing_only = 0
        detail_read = 0
        unusable = 0
        row_fields_seen = 0
        for url, anchor_text, anchor in anchors[:max_detail]:
            # The row's own words, read before anything else: the detail page,
            # if there is one, may add to them but may never replace them with
            # nothing (see merge_listing_and_detail).
            row = (
                # The URL pattern when there is one: for a site whose business
                # pages sit on sub-domains, the path pattern recognises none of
                # them and the row guard would refuse every such row.
                fields_for_anchor(anchor, detail_pattern=url_pattern or pattern, preset=list_preset)
                if read_rows
                else None
            )
            row_native = as_native(row) if row else None
            if row_native:
                row_fields_seen += 1
            detail_file = _locate_detail(url, explicit=explicit, root=details_root)
            if detail_file is None:
                record = _listing_only_record(
                    url=url,
                    name=anchor_text,
                    listing_origin=origin_url,
                    snapshot_id=listing_snapshot,
                    observed_at=observed_at,
                    row=row,
                )
                if record is None:
                    unusable += 1
                    continue
                listing_only += 1
                records.append(record)
                continue

            detail_text, _ = _read_text(detail_file)
            if detail_text is None:
                unusable += 1
                continue
            record = build_detail_record(
                parse_html(detail_text),
                detail_text,
                url=url,
                selectors=selectors,
                listing_url=origin_url,
                # The file the operator saved is the snapshot; the run only
                # re-read it. ``import`` is the honest word for that.
                acquired_via="import",
                snapshot_id=_snapshot_id(detail_file, detail_text),
                observed_at=observed_at,
            )
            if record is None:
                unusable += 1
                continue
            record = _merge_row_into_detail(record, row, url=url, row_native=row_native)
            detail_read += 1
            records.append(record)

        raw_path = ctx.raw_path(task.label, task.attempt)
        written = write_envelopes(
            raw_path,
            self.name,
            records,
            query=origin_url,
            context={
                "source_url": origin_url,
                "origin": urllib.parse.urlsplit(origin_url).hostname or None,
                "mode": "local_pages",
                "entry_name": task.extra.get("entry_name"),
                "listing_path": str(listing_path),
                "listing_encoding": encoding,
                "snapshot_id": listing_snapshot,
                "detail_links_seen": len(anchors),
                "detail_read": detail_read,
                "listing_only": listing_only,
                "detail_unusable": unusable,
                # How many rows the listing page itself stated fields for. When
                # this is 0 on a page that plainly has rows, the label set is
                # wrong — which is a config problem, not an empty page.
                "listing_rows_with_fields": row_fields_seen,
                "list_preset": list_preset,
                "config_notes": notes,
                # Said once here as well as per record: a reader who only looks
                # at the run's file list must not read this as a live crawl.
                "imported": True,
            },
        )
        return ProviderOutcome(
            ok=True,
            records=written,
            raw_path=raw_path,
            duration_seconds=time.monotonic() - started,
        )


# --- reading files -----------------------------------------------------------


def _expand(value: str) -> str:
    return str(pathlib.Path(value).expanduser())


def _read_text(path: pathlib.Path) -> tuple[str | None, str]:
    """Read one saved page, returning ``(text, encoding)``.

    HTML saved from a browser often states an encoding in a ``<meta>`` tag the
    bytes do not actually obey, so the declared value is not trusted blindly:
    the candidates are tried in a fixed order and the first that decodes is
    used. ``errors="strict"`` on every candidate except the last is what makes
    "the first that decodes" meaningful.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None, ""
    for index, encoding in enumerate(_ENCODING_CANDIDATES):
        last = index == len(_ENCODING_CANDIDATES) - 1
        try:
            return raw.decode(encoding, errors="replace" if last else "strict"), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return None, ""


def _snapshot_id(path: pathlib.Path, text: str) -> str:
    """A stable identity for the file this row came from.

    ``name:digest`` rather than a bare digest: two copies of the same page under
    different names are different artifacts to a reader, and the name is what
    they will search for.
    """
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{path.name}:{digest}"


# --- locating a saved detail page -------------------------------------------


def _explicit_details(entry: dict) -> dict[str, pathlib.Path]:
    """``details`` as ``{normalised url: path}``, ignoring unusable rows."""
    raw = entry.get("details")
    if not isinstance(raw, list):
        return {}
    out: dict[str, pathlib.Path] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        path = str(item.get("path") or "").strip()
        if not url or not path:
            continue
        out[_normalise_url(url)] = pathlib.Path(_expand(path))
    return out


def _normalise_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")
    )


def _locate_detail(
    url: str, *, explicit: dict[str, pathlib.Path], root: pathlib.Path | None
) -> pathlib.Path | None:
    """The saved file for one detail URL, or None when it was not saved.

    Two ways to say where it is, because both are natural: an explicit
    ``details: [{url, path}]`` list, or a ``details_dir`` the pages were saved
    into. The directory form matches on the URL's last path segment with either
    extension, which is exactly what "Save page as" produces.
    """
    hit = explicit.get(_normalise_url(url))
    if hit is not None and hit.is_file():
        return hit
    if root is None or not root.is_dir():
        return None
    parsed = urllib.parse.urlsplit(url)
    segment = pathlib.PurePosixPath(parsed.path or "/").name
    if not segment:
        return None
    stem = segment.rsplit(".", 1)[0] if "." in segment else segment
    for candidate in (segment, f"{stem}.html", f"{stem}.htm", stem):
        path = root / candidate
        if path.is_file():
            return path
    return None


# --- the row a listing page alone can support -------------------------------


def _listing_only_record(
    *,
    url: str,
    name: str,
    listing_origin: str,
    snapshot_id: str,
    observed_at: str | None,
    row=None,
) -> SourceRecord | None:
    """A business the listing page named but whose own page was not read.

    ``detail_url`` records where the business's page *would* be, and
    ``detail_fetched`` is left False so nothing downstream can present this as
    "we checked the company's page". The evidence is the listing page.

    Round 034: the row's own fields go with it. Before this, such a row carried a
    name and nothing else, so a page that stated an address for 97 of 98 rows
    produced 97 records with no address — the matcher then honestly answered
    "待确认" for all of them, and the loss read as the page's fault.
    """
    title = re.sub(r"\s+", " ", str(name or "")).strip()
    if not title:
        # The link had no text and there is no saved page to read: there is
        # nothing here that a reader could identify, so nothing is written.
        return None
    native: dict = {"detail_url": url, "listing_url": listing_origin, "listing_only": True}
    row_native = as_native(row)
    if row_native:
        native["listing_fields"] = row_native
        native["field_sources"] = {
            name: "listing" for name in row_native.get("values") or {}
        }
    values = row.values if row is not None else {}
    return SourceRecord(
        title=title,
        address=values.get("address"),
        description=values.get("description"),
        phone=values.get("phone"),
        category=values.get("category"),
        source_id=url,
        source_id_kind="directory_url",
        native=native,
        extra={"listing_only": True},
        acquired_via="import",
        source_page_url=listing_origin,
        detail_url=url,
        snapshot_id=snapshot_id,
        observed_at=observed_at,
    )


def _merge_row_into_detail(
    record: SourceRecord, row, *, url: str, row_native: dict | None
) -> SourceRecord:
    """Add what the listing row said to a record read from the detail page.

    The reason this exists is the failure it fixes: ``build_detail_record``
    returns ``None`` for every selector that did not match, and that absence used
    to reach the record as "no address known" — erasing what the listing page had
    already stated. A detail page's *absence* is not a contradiction.

    Where both sides state a value and they differ, both survive: the detail
    value stays in the record's field and the listing's is kept in
    ``native.listing_fields`` next to ``field_sources``, so the export can say
    which is which rather than picking one quietly.
    """
    if row is None:
        return record
    detail = {
        "address": record.address,
        "description": record.description,
        "phone": record.phone,
        "email": (record.emails[0] if record.emails else None),
        "category": record.category,
    }
    values, decisions = merge_listing_and_detail(detail, row)
    record.address = values.get("address") or record.address
    record.description = values.get("description") or record.description
    record.phone = values.get("phone") or record.phone
    record.category = values.get("category") or record.category
    if not record.emails and values.get("email"):
        record = replace(record, emails=(values["email"],))
    record.native["listing_fields"] = row_native or as_native(row)
    record.native["field_sources"] = {
        name: decision.source for name, decision in decisions.items()
    }
    conflicts = {
        name: decision.conflict
        for name, decision in decisions.items()
        if decision.conflict
    }
    if conflicts:
        # "冲突保留" said in the record, not resolved in it.
        record.native["field_conflicts"] = conflicts
    return record


# --- entry helpers -----------------------------------------------------------


def _entry_selectors(entry: dict) -> dict[str, str]:
    raw = entry.get("field_selectors") or {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        field = str(key).strip()
        selector = str(value).strip() if value is not None else ""
        if field and selector:
            out[field] = selector
    return out


def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return (cleaned or "entry")[:limit]


def _listing_scope(root, raw):
    """Kept as the single call site for :func:`listing_scope`.

    059: the live directory walk now restricts its harvest the same way, so the
    rule lives in one place (``sources.base``) and the two channels cannot
    drift. The sentences it returns are unchanged -- they appear in run notes
    and in tests.
    """
    return listing_scope(root, raw)


def _bool(value: Any, *, default: bool) -> bool:
    """A config flag whose *absence* means the default, not False.

    ``read_listing_fields: false`` has to be distinguishable from "not written",
    otherwise turning the feature off in one entry would turn it off everywhere.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in ("1", "true", "yes", "on", "是"):
        return True
    if text in ("0", "false", "no", "off", "否"):
        return False
    return default


def _int(value: Any, default: int) -> int:
    try:
        if value is None or isinstance(value, bool):
            return default
        return max(1, int(value))
    except (TypeError, ValueError):
        return default
