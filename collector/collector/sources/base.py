"""Shared abstractions for discovery providers.

Task book 004, stage E: "实现统一 provider 接口，至少接入：现有 Go Maps、
OpenStreetMap、用户 URL/CSV 列表、一个可配置企业目录。搜索 provider 设计成可替换
接口" and "所有来源共享预算、重试退避、域级限速、检查点；尊重 Retry-After，
限制最大尝试次数".

Every provider, whatever it talks to, obeys one contract:

1. :meth:`Provider.tasks` describes the units of work without doing any;
2. :meth:`Provider.run_task` performs exactly one of them and writes canonical
   records;
3. the records are written as the *same* ``osint.record/1`` envelope the Go
   scraper emits, so ingestion, checkpointing, de-duplication and export are
   shared with stage C/D instead of being re-implemented per source.

The canonical record is deliberately expressed in the field names the collector
already understands (``title``, ``phone``, ``web_site``, ``emails``,
``latitude`` ...). A provider that cannot fill a field leaves it out; ingest
records it as ``missing`` with a reason rather than guessing, which is the
stage C contract and must not be relaxed just because a new source shows up.

Shared budget and politeness live here rather than in each provider, because the
task book asks for them *across* sources: one run-wide request and time budget,
one per-domain minimum interval, and one retry policy that honours
``Retry-After`` and caps attempts.
"""

from __future__ import annotations

import json
import pathlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..errors import CollectorError
from ..selector import SelectorError, select
from ..util import utc_now_iso
from ..webfetch import Fetcher, UrlPolicy

#: Envelope written by every provider. It is the same schema stage C ingested,
#: so a raw file from any source is interchangeable as far as the rest of the
#: pipeline is concerned.
ENVELOPE_SCHEMA = "osint.record/1"

#: Content types a provider may need beyond HTML. Overpass answers with JSON.
JSON_CONTENT_TYPES = ("application/json", "text/javascript", "application/javascript")

#: How the collector obtained a record's data. A closed vocabulary on purpose:
#: a reader must be able to tell a page this run fetched from a file the operator
#: handed over, because only the first is "we visited it".
#:
#: - ``live``   — fetched from the site during this run.
#: - ``cache``  — a snapshot that was fetched earlier and is re-read now.
#: - ``import`` — a file (or saved page) the operator supplied; the collector
#:   never contacted the site, so nothing here may read as a visit.
ACQUIRED_VIA = ("live", "cache", "import")

#: Provenance keys an envelope context may carry. ``write_envelopes`` copies them
#: off each record, so a provider states them once on the record and every
#: consumer (ingest, export, the reader view) sees the same values instead of
#: re-deriving them.
PROVENANCE_KEYS = (
    "acquired_via",
    "source_page_url",
    "detail_url",
    "detail_fetched",
    "origin",
    "snapshot_id",
    "observed_at",
)

# 061: the set now lives in ``config``, which both the providers and the ingest
# path already import; it is re-exported here so existing callers are unchanged
# and there is still exactly one definition.
from ..config import NEVER_LIVE_PROVIDERS  # noqa: F401  (re-exported)

# --- canonical record -------------------------------------------------------


@dataclass
class SourceRecord:
    """One business as one provider saw it.

    ``source_id`` and ``source_id_kind`` are the provider's own stable identity
    for the record. They are what makes cross-source de-duplication possible
    without the collector pretending to know another provider's ids: the entity
    keeps its own ``stable_key`` and the merge stage links records it can prove
    refer to the same store.

    ``native`` is the untouched provider payload. It is stored verbatim under
    ``raw.native`` so evidence can always be traced back to what the source
    actually said.
    """

    title: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    emails: tuple[str, ...] = ()
    latitude: float | None = None
    longitude: float | None = None
    #: The coordinate reference system ``latitude``/``longitude`` are in, e.g.
    #: "WGS84" (Google Maps, OpenStreetMap), "GCJ-02" (Amap), "BD-09" (Baidu).
    #: Stated by the provider because coordinates without their system cannot be
    #: compared: the same place is 300-500 m apart between WGS84 and GCJ-02.
    crs: str | None = None
    category: str | None = None
    #: What the source says the business does, in its own words: the profile
    #: paragraph, the product line, the scope of trade. It is the evidence the
    #: industry match rests on (task book 021 A), so it is stored verbatim like
    #: every other field and never summarised or translated.
    description: str | None = None
    source_id: str = ""
    source_id_kind: str = ""
    native: dict = field(default_factory=dict)
    #: Free-form extras the provider knows about but the collector does not model
    #: as a first-class field. Stored, never guessed at.
    extra: dict = field(default_factory=dict)

    # -- provenance (round 026 P0) ------------------------------------------
    #: How this record's data was obtained: one of :data:`ACQUIRED_VIA`. Kept
    #: apart from the facts so an imported row can never be presented as a page
    #: this run visited.
    acquired_via: str = "live"
    #: The page the facts were *actually read from*, and therefore what the
    #: evidence cites. For a listing row this is the listing page, not the
    #: company's own page.
    source_page_url: str | None = None
    #: The entity's own page on the origin site, when the source merely *links*
    #: to it (a listing row that carries a detail link). Recorded so it is not
    #: lost, but it is never evidence unless ``detail_fetched`` is True.
    detail_url: str | None = None
    #: True only when this run genuinely fetched ``detail_url``. Defaults to
    #: False, so forgetting to set it errs toward "not visited".
    detail_fetched: bool = False
    #: The site the data originally came from, when that differs from the
    #: collector's provider name (csv_list carrying rows exported from another
    #: site). Provider is the *input channel*; origin is the *source site*.
    origin: str | None = None
    #: Identifier of the immutable artifact a row came from (file name + digest),
    #: so evidence points at a specific snapshot rather than "the file we have".
    snapshot_id: str | None = None
    #: When the source page was captured, as the operator stated it. Left None
    #: when unknown: the import moment is never allowed to stand in for it.
    observed_at: str | None = None

    def provenance(self) -> dict:
        """The provenance this record carries, as envelope context entries.

        Only non-empty values are returned. ``acquired_via`` is always present,
        because "we did not say" must not be readable as "we fetched it".
        """
        out: dict[str, Any] = {"acquired_via": self.acquired_via or "live"}
        for key in ("source_page_url", "detail_url", "origin", "snapshot_id", "observed_at"):
            value = getattr(self, key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
        if self.detail_fetched:
            out["detail_fetched"] = True
        return out

    def as_raw(self) -> dict:
        """The canonical payload, in the field names ingest already maps."""
        raw: dict[str, Any] = {}
        if self.title is not None:
            raw["title"] = self.title
        if self.address is not None:
            raw["address"] = self.address
        if self.phone is not None:
            raw["phone"] = self.phone
        if self.website is not None:
            raw["web_site"] = self.website
        if self.emails:
            raw["emails"] = list(self.emails)
        if self.latitude is not None:
            raw["latitude"] = self.latitude
        if self.longitude is not None:
            raw["longitude"] = self.longitude
        if self.crs is not None:
            raw["crs"] = self.crs
        if self.category is not None:
            raw["category"] = self.category
        if self.description is not None:
            raw["description"] = self.description
        if self.source_id:
            raw["source_id"] = self.source_id
        if self.source_id_kind:
            raw["source_id_kind"] = self.source_id_kind
        if self.native:
            raw["native"] = self.native
        if self.extra:
            raw["extra"] = self.extra
        return raw


# --- task descriptors -------------------------------------------------------


@dataclass
class ProviderTask:
    """One unit of work for one provider, described before anything runs."""

    label: str
    query: str = ""
    #: URLs or arguments the task will touch. Recorded so a dry run can be
    #: inspected and an error can be traced without re-reading the code.
    targets: tuple[str, ...] = ()
    argv: tuple[str, ...] = ()
    attempt: int = 1
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "query": self.query,
            "targets": list(self.targets),
            "argv": list(self.argv),
            "attempt": self.attempt,
        }


@dataclass
class ProviderOutcome:
    """What one provider task actually did."""

    ok: bool
    records: int = 0
    raw_path: pathlib.Path | None = None
    error: str = ""
    error_kind: str = ""
    #: Why the task did not run at all (budget, skipped). Distinct from failing.
    skipped_reason: str = ""
    attempts: int = 1
    duration_seconds: float = 0.0
    #: Process exit status where the task was an external command. Only the Go
    #: provider has one; recording it here keeps ``provider_tasks.exit_code``
    #: populated exactly as it was before sources were unified, which is what a
    #: post-mortem of a failed Go run reads.
    exit_code: int | None = None
    #: How long the site asked us to wait, when it answered 429/503 with a
    #: ``Retry-After``. Round 026 P2: the run persists this as a per-source
    #: cooldown and the screen shows it, so "the site is not answering right
    #: now" is a visible state rather than a silent shortfall.
    retry_after_seconds: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "records": self.records,
            "raw_path": str(self.raw_path) if self.raw_path else None,
            "error": self.error,
            "error_kind": self.error_kind,
            "skipped_reason": self.skipped_reason,
            "attempts": self.attempts,
            "duration_seconds": round(self.duration_seconds, 3),
            "exit_code": self.exit_code,
            "retry_after_seconds": self.retry_after_seconds,
        }


# --- shared budget and politeness ------------------------------------------


@dataclass
class RunBudget:
    """Run-wide request and wall-clock budget, shared by every provider.

    ``max_requests`` counts provider *tasks* started, matching stage C where
    ``runtime.budget`` caps the number of Go invocations. ``0`` disables a limit.
    """

    max_requests: int = 0
    max_seconds: int = 0
    started_at: float = field(default_factory=time.monotonic)
    used_requests: int = 0
    skipped: int = 0

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def reason(self) -> str | None:
        """Why another task must not start, or None when it may."""
        if self.max_requests and self.used_requests >= self.max_requests:
            return f"request budget exhausted ({self.max_requests} task(s))"
        if self.max_seconds and self.elapsed_seconds >= self.max_seconds:
            return f"time budget exhausted ({self.max_seconds}s)"
        return None

    def remaining_seconds(self) -> float | None:
        if not self.max_seconds:
            return None
        return max(0.0, self.max_seconds - self.elapsed_seconds)

    def spend(self, tasks: int = 1) -> None:
        self.used_requests += tasks

    def note_skip(self) -> None:
        self.skipped += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_requests": self.max_requests,
            "max_seconds": self.max_seconds,
            "used_requests": self.used_requests,
            "skipped": self.skipped,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


class DomainRateLimiter:
    """A minimum interval between requests to the same registrable domain.

    Deliberately process-local and wall-clock based: the run is sequential, so
    there is no concurrency to coordinate and no state that has to survive a
    restart (a checkpoint already covers that, and being polite on restart is
    better served by the run-wide budget).
    """

    def __init__(
        self,
        min_interval_seconds: float = 1.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.sleep = sleep
        self.clock = clock
        self._last: dict[str, float] = {}
        self.waits = 0

    def wait(self, domain: str) -> None:
        key = (domain or "").lower()
        if not key or not self.min_interval_seconds:
            return
        last = self._last.get(key)
        now = self.clock()
        if last is not None:
            due = last + self.min_interval_seconds
            if now < due:
                self.waits += 1
                self.sleep(due - now)
                now = self.clock()
        self._last[key] = now


@dataclass
class RetryPolicy:
    """Retry and backoff shared by every provider.

    ``max_attempts`` bounds the total tries for one task, and ``max_retry_wait``
    bounds how long a single honouring of ``Retry-After`` is allowed to last:
    a server asking for a very long wait is reported, not obeyed, because the
    decision to spend the run's budget that way belongs to the caller.
    """

    max_attempts: int = 3
    backoff_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0
    honor_retry_after: bool = True
    max_retry_wait_seconds: int = 5

    def delay_for(self, attempt: int, retry_after: int | None = None) -> float:
        """How long to wait before ``attempt`` (1-based, already finished)."""
        if self.honor_retry_after and retry_after is not None:
            if retry_after <= self.max_retry_wait_seconds:
                return float(retry_after)
            return min(float(retry_after), self.max_backoff_seconds)
        raw = self.backoff_seconds * (self.backoff_multiplier ** max(0, attempt - 1))
        return min(raw, self.max_backoff_seconds)


# --- run context ------------------------------------------------------------


class RunContext:
    """Everything a provider needs to perform one task of one run.

    Built by the orchestrator so all providers share the same budget, rate
    limiter and HTTP facilities. Providers never construct their own.
    """

    def __init__(
        self,
        *,
        run_id: int,
        run_dir: pathlib.Path,
        budget: RunBudget,
        rate_limiter: DomainRateLimiter,
        retry: RetryPolicy,
        fetcher: Fetcher | None = None,
        policy: UrlPolicy | None = None,
        proxy: str = "",
        timeout_seconds: int = 30,
        min_interval_seconds: float = 1.0,
        max_bytes: int = 4 * 1024 * 1024,
        sleep: Callable[[float], None] = time.sleep,
        adapters: dict[str, Any] | None = None,
        go_binary: pathlib.Path | None = None,
        cwd: pathlib.Path | None = None,
        networks: dict[str, dict] | None = None,
    ):
        self.run_id = run_id
        self.run_dir = pathlib.Path(run_dir)
        self.budget = budget
        self.rate_limiter = rate_limiter
        self.retry = retry
        self.policy = policy or UrlPolicy()
        self._fetch_kwargs = {
            "timeout_seconds": timeout_seconds,
            "max_bytes": max_bytes,
            "policy": self.policy,
            "max_attempts": 1,  # retrying is this context's job, not the fetcher's
            "max_retry_wait_seconds": retry.max_retry_wait_seconds,
            "sleep": sleep,
        }
        #: The run-wide proxy (``runtime.proxy``); what ``inherit`` resolves to.
        self._run_proxy = proxy
        self.fetcher = fetcher or Fetcher(proxy=proxy, **self._fetch_kwargs)
        #: Per-source routing from ``sources_map.<name>.network``. A source that
        #: is not listed (or lists ``inherit``) behaves exactly as it did before
        #: this existed: the run-wide proxy, then the environment.
        self.networks: dict[str, dict] = {
            str(name): dict(value) for name, value in (networks or {}).items()
        }
        self._fetchers: dict[str, Fetcher] = {}
        self.sleep = sleep
        self.min_interval_seconds = min_interval_seconds
        #: Named per-provider resources the orchestrator already owns. Seeding
        #: the Google Maps adapter here means a run started with a stub scraper
        #: keeps using that stub, which is what the test suite relies on.
        self.adapters: dict[str, Any] = dict(adapters or {})
        self.go_binary = pathlib.Path(go_binary) if go_binary else None
        self.cwd = pathlib.Path(cwd) if cwd else None

    # -- paths ---------------------------------------------------------------

    def raw_path(self, label: str, attempt: int) -> pathlib.Path:
        return self.run_dir / "raw" / f"{label}-a{attempt}.jsonl"

    def log_path(self, label: str, attempt: int, stream: str) -> pathlib.Path:
        return self.run_dir / "logs" / f"{label}-a{attempt}.{stream}.log"

    # -- budget --------------------------------------------------------------

    def budget_reason(self) -> str | None:
        return self.budget.reason()

    def spend(self, tasks: int = 1) -> None:
        self.budget.spend(tasks)

    def withdraw_task_timeout(self, fallback_seconds: int) -> int:
        """Task timeout, capped by whatever time budget is left."""
        remaining = self.budget.remaining_seconds()
        if remaining is None:
            return fallback_seconds
        return max(1, min(fallback_seconds, int(remaining)))

    # -- politeness ----------------------------------------------------------

    def be_polite(self, domain: str) -> None:
        self.rate_limiter.wait(domain)

    # -- per-source networking (stage 019 A4) --------------------------------

    def effective_proxy(self, source: str) -> str:
        """The proxy this source uses; '' means no proxy."""
        network = self.networks.get(source) or {}
        mode = str(network.get("mode") or "inherit")
        if mode == "direct":
            return ""
        if mode == "proxy":
            return str(network.get("proxy") or "")
        return self._run_proxy

    def trust_env_proxy(self, source: str) -> bool:
        """Whether the environment may supply a proxy for this source.

        Only ``inherit`` says yes, so ``direct`` and ``proxy`` both mean what
        they say on a machine that has ``HTTP_PROXY`` exported.
        """
        network = self.networks.get(source) or {}
        return str(network.get("mode") or "inherit") == "inherit"

    def fetcher_for(self, source: str) -> Fetcher:
        """The HTTP client for one source, built from its routing.

        Cached per source so a run with several sources does not rebuild an
        opener per fetch. A source that never configured ``network`` gets a
        client identical to :attr:`fetcher`, so nothing changes for it.
        """
        cached = self._fetchers.get(source)
        if cached is not None:
            return cached
        network = self.networks.get(source)
        if not network or str(network.get("mode") or "inherit") == "inherit":
            client = self.fetcher
        else:
            client = Fetcher(
                proxy=self.effective_proxy(source),
                trust_env_proxy=self.trust_env_proxy(source),
                **self._fetch_kwargs,
            )
        self._fetchers[source] = client
        return client


# --- envelope writing -------------------------------------------------------


def write_envelopes(
    path: pathlib.Path,
    provider: str,
    records: Iterable[SourceRecord],
    *,
    query: str = "",
    context: dict | None = None,
) -> int:
    """Write canonical records as ``osint.record/1`` envelopes.

    One JSON object per line, the framing ingest already knows. Returns the
    number of records written. Writing to a fresh file per attempt keeps the
    previous attempt as evidence and makes a retry safe.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            base = dict(context or {})
            # Per-record provenance wins over the file-wide context, so a
            # provider that stamps each row (a CSV row carrying its own page
            # URL) keeps that value while the run-wide defaults still apply to
            # the rows that do not.
            for key, value in record.provenance().items():
                base[key] = value
            # Providers that read a file and nothing else can never claim to
            # have visited a page. An unknown value is treated the same way: it
            # could be read as "we fetched it", which must not happen by
            # accident.
            stated = base.get("acquired_via")
            forced = "import" if provider in NEVER_LIVE_PROVIDERS else "live"
            if stated not in ACQUIRED_VIA or (provider in NEVER_LIVE_PROVIDERS and stated == "live"):
                base["acquired_via"] = forced
            if query:
                base.setdefault("query", query)
            if provider not in NEVER_LIVE_PROVIDERS:
                # Only a run that actually fetched bytes can say when it got
                # them. For an import, "now" is when we read a file, and stamping
                # that as ``scraped_at`` let the export present the import moment
                # as the source's capture time (034 第一批第 3 条). An imported
                # row's time is ``observed_at`` — what the source or the operator
                # stated — or absent, and the export then says so.
                base.setdefault("scraped_at", utc_now_iso())
            base.setdefault("mode", "provider")
            envelope = {
                "schema": ENVELOPE_SCHEMA,
                "provider": provider,
                "context": base,
                "raw": record.as_raw(),
            }
            handle.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            written += 1
    return written


def read_envelope_count(path: pathlib.Path) -> int:
    count = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


# --- provider contract ------------------------------------------------------


class Provider(ABC):
    """A replaceable discovery source.

    Subclasses describe themselves (``name``, ``title``, ``license``,
    ``attribution``) so the collector can publish a source manifest without the
    operator having to trust the code: a provider that needs an account says so,
    and one that is not configured says why instead of silently doing nothing.
    """

    name: str = ""
    title: str = ""
    #: SPDX-style identifier and short prose, e.g. "ODbL 1.0".
    license: str = ""
    #: What the licence obliges us to do (attribution wording, share-alike, ...).
    attribution: str = ""
    #: True when using this provider needs credentials the collector will not ask
    # for and must not invent.
    requires_account: bool = False
    #: Provenance note for ideas borrowed from another project, with the licence
    #: caveat spelled out. Blank when the design is original.
    provenance: str = ""
    #: The coordinate reference system this provider's coordinates are in, e.g.
    #: "WGS84", "GCJ-02", "BD-09". Blank when the provider publishes no
    #: coordinates. Declared rather than assumed: two systems put the same place
    #: 300-500 m apart, so the merge stage must never compare across them.
    coordinate_system: str = ""

    def __init__(self, cfg):
        self.cfg = cfg

    # -- description ---------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        configured, reason = self.availability()
        return {
            "name": self.name,
            "title": self.title,
            "license": self.license,
            "attribution": self.attribution,
            "requires_account": self.requires_account,
            "coordinate_system": self.coordinate_system,
            "configured": configured,
            "not_configured_reason": reason,
            "provenance": self.provenance,
        }

    def availability(self) -> tuple[bool, str]:
        """(configured, reason). ``reason`` is '' when configured."""
        if self.is_configured():
            return True, ""
        return False, self.not_configured_reason()

    def not_configured_reason(self) -> str:
        return f"{self.name} is not configured"

    # -- lifecycle -----------------------------------------------------------

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def tasks(self) -> list[ProviderTask]: ...

    @abstractmethod
    def run_task(self, task: ProviderTask, ctx: RunContext) -> ProviderOutcome: ...


def require_records(path: pathlib.Path | None, *, label: str) -> int:
    """How many records a task wrote, tolerating a missing file."""
    if path is None or not pathlib.Path(path).is_file():
        return 0
    try:
        return read_envelope_count(pathlib.Path(path))
    except OSError as exc:
        raise CollectorError(f"could not re-read the output of {label}: {exc}") from exc


def listing_scope(root, raw):
    """The container(s) a listing sits in, from a ``list_selector``.

    059: the same key used to be honoured by the offline importer and silently
    ignored by the live directory walk, so a person who wrote it down got no
    restriction and no warning. Both channels now read it through here, so the
    two cannot drift again.

    Returns ``(containers, error_note)``. An empty value means "do not restrict",
    which is every config written before round 034; a selector the engine cannot
    evaluate -- or one that matches nothing on this page -- is *reported*, not
    raised: the honest outcome is a slightly wider harvest with a sentence saying
    so, not a dead run.
    """
    text = str(raw or "").strip()
    if not text:
        return None, ""
    try:
        containers = select(root, text)
    except SelectorError as exc:
        return None, f"list_selector 无法求值（{text}）：{exc}；本次未限定列表范围"
    if not containers:
        return None, f"list_selector 在页面上没匹配到任何容器（{text}）；本次未限定列表范围"
    return containers, ""
