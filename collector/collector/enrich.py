"""Website enrichment: visit each business's own site and record what it says.

Task book 004, stage D:

    "首页 + 有预算的同域 Contact/About/Locations 页面；支持 mailto/tel 和
     JSON-LD；每字段保留真实来源页。提取值与官网身份分开判定；名称/位置/电话
     冲突进入 uncertain，不覆盖原来源。"

The rules this module enforces
------------------------------
* **A budget, not a crawl.** ``enrich.pages_per_site`` caps how many pages of a
  site are visited (the homepage counts as one), and ``enrich.budget`` caps the
  whole run. Fetches are sequential with a politeness delay, because the point
  is to read a business's contact page, not to mirror its site.
* **Same site only.** Follow-up pages come from
  :func:`collector.htmlextract.find_link_candidates`, which already drops
  off-site links; the fetcher then refuses an off-site redirect. Both the link
  filter and the redirect check exist because a same-site URL can still redirect
  away.
* **Every value keeps its page.** A fact's ``source_url`` is the page the value
  was read from, never the site root, and ``evidence_ref`` locates it inside that
  page.
* **Conflicts are reported, not resolved.** If the website disagrees with the
  listing about the name, the phone number or the address, the fact is stored as
  ``uncertain`` with both values named. The listing's value is never overwritten
  — the two are separate rows, and the export keeps the first-written one.
* **One bad site cannot lose a batch.** A fetch failure, a boundary rejection, a
  broken page or an unparsable JSON-LD block becomes an error row and the loop
  moves on.
"""

from __future__ import annotations

import json
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import db
from .addresscheck import STATE_NAME_NOT_ADDRESS, parse_address
from .config import ENRICHABLE_FIELDS, Config
from .emailcheck import DnsResolver, check_emails
from .htmlextract import ExtractedValue, extract_page
from .identity import domain_of, normalize_text
from .phonecheck import check_phone
from .util import clip, content_hash, utc_now_iso
from .webfetch import (
    FetchResult,
    Fetcher,
    PageCache,
    UrlPolicy,
    normalize_url,
    registrable_domain,
)

PROVIDER = "website"

#: Seconds between two requests to the same site. Small, but it keeps a run
#: from hammering a business's server.
DEFAULT_POLITE_DELAY_SECONDS = 0.5

#: Fields compared against the listing for an identity conflict.
IDENTITY_FIELDS = ("title", "phone", "address")

#: Machine-readable states for website-derived facts.
STATE_FROM_WEBSITE = "from_website"
STATE_CONFIRMED_BY_WEBSITE = "confirmed_by_website"
STATE_CONFLICTS_WITH_LISTING = "conflicts_with_listing"
STATE_BOUNDARY_REJECTED = "boundary_rejected"
#: The page contradicts itself: a ``tel:``/``mailto:`` anchor links one value
#: and prints another. Distinct from ``conflicts_with_listing`` (the page
#: disagrees with the directory) -- here the *source* is internally
#: inconsistent, and format validation of either value is not a resolution.
STATE_SOURCE_CONFLICT = "source_conflict"

#: An address written out in visible anchor text. Deliberately the same shape
#: as :mod:`collector.emailcheck` accepts; used only to tell "the text shows a
#: different address" from "the text is a label".
_EMAIL_TOKEN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


@dataclass
class EnrichStats:
    entities_considered: int = 0
    entities_with_website: int = 0
    sites_visited: int = 0
    pages_fetched: int = 0
    pages_failed: int = 0
    pages_skipped_budget: int = 0
    pages_skipped_duplicate: int = 0
    #: Pages read out of the run's cache instead of over the network (045.3):
    #: discovery already fetched them, so re-fetching would be a request spent
    #: against the site for a body in hand.
    pages_from_cache: int = 0
    values_extracted: int = 0
    facts_written: int = 0
    facts_repeated: int = 0
    confirmations: int = 0
    identity_conflicts: int = 0
    #: Anchors whose link target and visible text disagree -- the source
    #: contradicting itself. Counted apart from ``identity_conflicts``, which is
    #: the source disagreeing with the listing.
    source_conflicts: int = 0
    boundary_rejections: int = 0
    errors: int = 0
    dns_timeouts: int = 0
    budget_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Enricher:
    """Enriches the entities of one run from their own websites."""

    def __init__(
        self,
        cfg: Config,
        *,
        fetcher: Fetcher | None = None,
        resolver: DnsResolver | None = None,
        sleep: Callable[[float], None] = time.sleep,
        polite_delay_seconds: float = DEFAULT_POLITE_DELAY_SECONDS,
        page_cache: PageCache | None = None,
    ):
        self.cfg = cfg
        self.sleep = sleep
        self.polite_delay_seconds = polite_delay_seconds
        self.resolver = resolver
        #: Pages this run already obtained (045.3). Discovery reads a site's
        #: contact page to decide ownership and enrichment would read the same
        #: page again; sharing the cache across the two stages removes that
        #: second request without changing what either stage concludes.
        self.page_cache = page_cache
        self.fetcher = fetcher or Fetcher(
            timeout_seconds=min(cfg.timeout_seconds, 30),
            proxy=cfg.proxy,
            policy=UrlPolicy(),
        )

    # -- budgets -------------------------------------------------------------

    def _request_budget(self) -> int:
        enrich_budget = int(self.cfg.data["enrich"]["budget"]["max_requests"])
        if enrich_budget:
            return enrich_budget
        return int(self.cfg.data["runtime"]["budget"]["max_requests"])

    def _time_budget(self) -> int:
        enrich_budget = int(self.cfg.data["enrich"]["budget"]["max_seconds"])
        if enrich_budget:
            return enrich_budget
        return int(self.cfg.data["runtime"]["budget"]["max_seconds"])

    # -- entry point ---------------------------------------------------------

    def enrich_run(self, conn, run_id: int) -> EnrichStats:
        stats = EnrichStats()
        fields = list(self.cfg.data["enrich"]["fields"])
        pages_per_site = int(self.cfg.data["enrich"]["pages_per_site"])
        request_budget = self._request_budget()
        time_budget = self._time_budget()
        started_at = time.monotonic()
        fetches = 0
        email_cache: dict[str, Any] = {}

        for entity in db.iter_run_entities(conn, run_id):
            stats.entities_considered += 1
            entity_id = int(entity["id"])

            if db.get_checkpoint(conn, run_id, "enrich", f"entity:{entity['stable_key']}"):
                continue

            website = (entity["website"] or "").strip()
            if not website:
                continue
            stats.entities_with_website += 1

            if request_budget and fetches >= request_budget:
                stats.pages_skipped_budget += 1
                stats.budget_reason = f"request budget exhausted ({request_budget} fetches)"
                continue
            if time_budget and (time.monotonic() - started_at) >= time_budget:
                stats.pages_skipped_budget += 1
                stats.budget_reason = f"time budget exhausted ({time_budget}s)"
                continue

            used = self._enrich_entity(
                conn,
                run_id,
                entity,
                entity_id,
                website=website,
                fields=fields,
                pages_per_site=pages_per_site,
                stats=stats,
                email_cache=email_cache,
                remaining_requests=(request_budget - fetches) if request_budget else None,
                deadline=(started_at + time_budget) if time_budget else None,
            )
            fetches += used
            db.set_checkpoint(
                conn,
                run_id,
                "enrich",
                f"entity:{entity['stable_key']}",
                {"pages": used, "finished_at": utc_now_iso()},
            )

        return stats

    # -- one entity ----------------------------------------------------------

    def _enrich_entity(
        self,
        conn,
        run_id: int,
        entity,
        entity_id: int,
        *,
        website: str,
        fields: list[str],
        pages_per_site: int,
        stats: EnrichStats,
        email_cache: dict,
        remaining_requests: int | None,
        deadline: float | None,
    ) -> int:
        """Visit one site. Returns the number of fetches actually spent."""
        candidate = normalize_url(website)
        check = self.fetcher.policy.check(candidate)
        if not check.ok:
            stats.boundary_rejections += 1
            db.insert_error(
                conn,
                run_id=run_id,
                stage="enrich",
                kind="website_boundary_rejected",
                message=(
                    f"refused to fetch the website of {entity['stable_key']}: "
                    f"{check.reason} ({check.detail})"
                ),
                context={"website": website, "reason": check.reason, "host": check.host},
            )
            self._write_verdict_fact(
                conn,
                run_id,
                entity_id,
                field_name="website",
                raw_value=website,
                normalized=None,
                status="invalid",
                state=STATE_BOUNDARY_REJECTED,
                note=f"not fetched: {check.reason} — {check.detail}",
                source_url=website,
                evidence=clip(website),
                evidence_ref=website,
                provider=PROVIDER,
                extractor="website-url/1",
                checks={"url_check": check.reason, "detail": check.detail},
                stats=stats,
            )
            return 0

        site = check.registrable or registrable_domain(check.host)
        home = check.url
        fetched = 0

        home_result, charged = self._fetch_page(home, allowed_domains={site})
        if charged:
            fetched += 1
            self._record_fetch_result(conn, run_id, entity, home_result, stats)
        else:
            stats.pages_from_cache += 1

        pages: list[tuple[str, FetchResult]] = []
        if home_result.ok:
            pages.append((home, home_result))

        if home_result.ok and pages_per_site > 1:
            extraction = extract_page(home, home_result.body)
            for link in extraction.links:
                if len(pages) >= pages_per_site:
                    break
                if remaining_requests is not None and fetched >= remaining_requests:
                    stats.pages_skipped_budget += 1
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    stats.pages_skipped_budget += 1
                    break
                if any(existing == link.url for existing, _ in pages):
                    stats.pages_skipped_duplicate += 1
                    continue
                result, charged = self._fetch_page(link.url, allowed_domains={site})
                if charged:
                    if self.polite_delay_seconds:
                        self.sleep(self.polite_delay_seconds)
                    fetched += 1
                    self._record_fetch_result(conn, run_id, entity, result, stats)
                else:
                    stats.pages_from_cache += 1
                if result.ok:
                    pages.append((link.url, result))

        if pages:
            stats.sites_visited += 1

        listing = _listing_values(entity)
        for page_url, result in pages:
            extraction = extract_page(page_url, result.body)
            if extraction.parse_error:
                db.insert_error(
                    conn,
                    run_id=run_id,
                    stage="enrich",
                    kind="page_parse_error",
                    message=f"could not parse {page_url}: {extraction.parse_error}",
                    context={"stable_key": entity["stable_key"], "url": page_url},
                )
                stats.errors += 1
            if extraction.jsonld_errors:
                db.insert_error(
                    conn,
                    run_id=run_id,
                    stage="enrich",
                    kind="jsonld_parse_error",
                    message=(
                        f"{extraction.jsonld_errors} JSON-LD block(s) on {page_url} "
                        "could not be parsed; the page's other sources were still used"
                    ),
                    context={"stable_key": entity["stable_key"], "url": page_url},
                )
                stats.errors += 1
            stats.values_extracted += len(extraction.values)
            self._write_page_values(
                conn,
                run_id,
                entity,
                entity_id,
                page_url=page_url,
                scraped_at=utc_now_iso(),
                values=extraction.values,
                requested_fields=fields,
                listing=listing,
                stats=stats,
                email_cache=email_cache,
            )

        return fetched

    def _fetch_page(self, url: str, *, allowed_domains: set[str]) -> tuple[FetchResult, bool]:
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

    # -- failures ------------------------------------------------------------

    def _record_fetch_result(
        self, conn, run_id: int, entity, result: FetchResult, stats: EnrichStats
    ) -> None:
        if result.ok:
            stats.pages_fetched += 1
            return

        stats.pages_failed += 1
        if result.error_kind in ("dns_timeout", "timeout"):
            stats.dns_timeouts += 1
        db.insert_error(
            conn,
            run_id=run_id,
            stage="enrich",
            kind=f"fetch_{result.error_kind or 'error'}",
            message=(
                f"could not read {result.requested_url}: "
                f"{result.error_kind} ({result.error_detail})"
            ),
            context={
                "stable_key": entity["stable_key"],
                "requested_url": result.requested_url,
                "final_url": result.final_url,
                "status_code": result.status_code,
                "redirect_chain": result.redirect_chain,
                "error_kind": result.error_kind,
                "error_detail": result.error_detail,
            },
        )
        stats.errors += 1

    # -- writing facts -------------------------------------------------------

    def _write_page_values(
        self,
        conn,
        run_id: int,
        entity,
        entity_id: int,
        *,
        page_url: str,
        scraped_at: str,
        values: list[ExtractedValue],
        requested_fields: list[str],
        listing: dict[str, str],
        stats: EnrichStats,
        email_cache: dict,
    ) -> None:
        # Emails are validated per domain, so collect them first.
        email_values = [value for value in values if value.field == "emails"]
        # The page's own name, for the 048 address check: an "address" that is
        # the business's name is not an address.
        page_name = next((value.raw_value for value in values if value.field == "title"), "")
        email_verdicts = {}
        if email_values and "emails" in requested_fields:
            verdicts = check_emails(
                [value.raw_value for value in email_values],
                resolver=self.resolver,
                check_dns=True,
                rules=self.cfg.rules(),
                cache=email_cache,
            )
            for value, verdict in zip(email_values, verdicts):
                email_verdicts[id(value)] = verdict

        for value in values:
            field_name = value.field
            if field_name not in ENRICHABLE_FIELDS:
                continue

            requested = field_name in requested_fields
            conflicting = False
            confirmed = False
            source_conflict: dict | None = None
            note = ""
            state = STATE_FROM_WEBSITE
            status = "unverified"

            if field_name == "emails":
                verdict = email_verdicts.get(id(value))
                if verdict is None:
                    continue
                status = verdict.status
                state = verdict.state
                note = verdict.note
                normalized = verdict.normalized
                checks = dict(verdict.checks, extraction_source=value.source)
                checks["mx_hosts"] = list(verdict.mx_hosts)
                source_conflict = _display_conflict(
                    value,
                    href_normalized=verdict.normalized,
                    country=self.cfg.country,
                    rules=self.cfg.rules(),
                )
                if status == "invalid":
                    requested = True  # a broken address is worth recording
            elif field_name == "phone":
                verdict = check_phone(
                    value.raw_value, region=self.cfg.country, rules=self.cfg.rules()
                )
                status = verdict.status
                state = verdict.state
                note = verdict.note
                normalized = verdict.normalized
                checks = dict(verdict.checks, extraction_source=value.source)
                checks["e164"] = verdict.e164
                checks["extension"] = verdict.extension
                checks["number_type"] = verdict.number_type
                source_conflict = _display_conflict(
                    value,
                    href_normalized=verdict.e164,
                    country=self.cfg.country,
                    rules=self.cfg.rules(),
                )
            elif field_name == "address":
                # 048: the page's own name is what a name-shaped address value
                # has to be compared against. A page that writes its name into
                # its address field states no address, and recording it as one
                # would let the name stand as the independent signal.
                verdict = parse_address(
                    value.raw_value, country=self.cfg.country, name=page_name
                )
                normalized = verdict.match_key or None
                if verdict.state == STATE_NAME_NOT_ADDRESS:
                    status = "uncertain"
                    state = STATE_NAME_NOT_ADDRESS
                    note = verdict.checks.get("not_an_address", "")
                else:
                    status = "unverified"
                    state = STATE_FROM_WEBSITE
                    note = verdict.location_note
                checks = dict(verdict.checks, extraction_source=value.source)
                checks["components"] = verdict.components
                checks["location_validated"] = verdict.location_validated
                checks["postal_verified"] = verdict.postal_verified
                checks["unicode_ok"] = verdict.unicode_ok
                checks["scripts"] = list(verdict.scripts)
                checks["mojibake_suspected"] = verdict.mojibake_suspected
            else:
                normalized = normalize_text(value.raw_value) or None
                checks = {"extraction_source": value.source}
                note = "read from the business's own website"

            checks["jsonld_type"] = value.context.get("jsonld_type")
            checks["role"] = value.context.get("role")

            # A page that links one contact and prints another has not stated
            # one contact, whatever each half's format verdict says. Both halves
            # are kept (``href_value`` / ``display_text`` in ``checks``) and the
            # fact is stored as uncertain, so the conflict reaches the export
            # instead of being hidden behind a "valid" format verdict.
            if source_conflict:
                state = STATE_SOURCE_CONFLICT
                status = "uncertain"
                note = (
                    "页面自相矛盾：链接目标与可见文本是两个不同的值，两者均保留并标为待核实"
                    "（格式校验不构成语义消解）"
                )
                checks.update(source_conflict)
                stats.source_conflicts += 1

            # --- identity comparison, never overwriting the listing ----------
            if field_name in IDENTITY_FIELDS and not source_conflict:
                listed = listing.get(field_name, "")
                if listed and normalized:
                    if values_agree(
                        field_name,
                        listed,
                        normalized,
                        country=self.cfg.country,
                        rules=self.cfg.rules(),
                    ):
                        confirmed = True
                        state = STATE_CONFIRMED_BY_WEBSITE
                        status = "confirmed"
                        note = (
                            f"the website states the same {field_name} as the "
                            "listing; the listing value is kept"
                        )
                        checks["listing_value"] = listed
                        stats.confirmations += 1
                    else:
                        conflicting = True
                        state = STATE_CONFLICTS_WITH_LISTING
                        status = "uncertain"
                        note = (
                            f"the website disagrees with the listing about the "
                            f"{field_name}: listing={listed!r} website={value.raw_value!r}; "
                            "the listing value is kept and this one is marked uncertain"
                        )
                        checks["listing_value"] = listed
                        checks["website_value"] = value.raw_value
                        stats.identity_conflicts += 1

            if not requested and not conflicting and not source_conflict:
                continue

            self._write_verdict_fact(
                conn,
                run_id,
                entity_id,
                field_name=field_name,
                raw_value=value.raw_value,
                normalized=normalized,
                status=status,
                state=state,
                note=note,
                source_url=page_url,
                evidence=clip(value.evidence, 300),
                evidence_ref=_evidence_ref(page_url, value),
                provider=PROVIDER,
                extractor=f"website-{value.source}/1",
                checks=checks,
                stats=stats,
                # A website visit has no search term: the page was reached
                # through the listing's own website field, not through a query.
                # Stamping the *listing's* search onto a website fact is how
                # "dentists in Berlin" ended up on a batch of German company
                # facts (045). Empty is the truthful value here; the entity's
                # query column still comes from its listing facts.
                query_context=None,
                scraped_at=scraped_at,
            )

    def _write_verdict_fact(
        self,
        conn,
        run_id: int,
        entity_id: int,
        *,
        field_name: str,
        raw_value: str | None,
        normalized: str | None,
        status: str,
        state: str,
        note: str,
        source_url: str,
        evidence: str | None,
        evidence_ref: str,
        provider: str,
        extractor: str,
        checks: dict,
        stats: EnrichStats,
        query_context: str | None = None,
        scraped_at: str | None = None,
    ) -> None:
        key = content_hash([entity_id, field_name, raw_value, provider, source_url])
        inserted = db.insert_fact(
            conn,
            entity_id=entity_id,
            observation_id=None,
            run_id=run_id,
            field=field_name,
            raw_value=raw_value,
            normalized_value=normalized,
            provider=provider,
            source_url=source_url,
            query_context=query_context,
            scraped_at=scraped_at,
            evidence_snippet=evidence,
            evidence_ref=evidence_ref,
            extractor_version=extractor,
            validation_status=status,
            validation_note=note,
            validation_state=state,
            checks_json=json.dumps(checks, ensure_ascii=False, sort_keys=True),
            observed_at=utc_now_iso(),
            fact_key=key,
        )
        if inserted:
            stats.facts_written += 1
        else:
            stats.facts_repeated += 1


# --- helpers ----------------------------------------------------------------


def _listing_values(entity) -> dict[str, str]:
    """The values the listing supplied, used for the identity comparison."""
    return {
        "title": normalize_text(entity["name"]) or "",
        "phone": (entity["phone_norm"] or "").strip(),
        "address": normalize_text(entity["address"]) or "",
    }


def _display_conflict(
    value: ExtractedValue,
    *,
    href_normalized: str | None,
    country: str,
    rules: dict,
) -> dict | None:
    """Whether a ``tel:``/``mailto:`` anchor contradicts itself.

    The element states two things -- what the link targets and what the page
    prints -- and this is the only place both are in hand. Three outcomes:

    * the visible text is not itself a value (``Kontakt``, ``Schreiben Sie
      uns``, ``Anrufen``) -- there is nothing to disagree with, so ``None``;
    * both normalise to the same thing (``030 12345678`` linked as
      ``tel:+493012345678``) -- a formatting difference, not a conflict;
    * both are values and they differ -- a real conflict, returned as the
      checks to attach.

    Returning checks rather than raising keeps this in the same shape as the
    identity comparison: the fact is written either way, and what changes is
    how it is marked.
    """
    display = str(value.context.get("display_text") or "").strip()
    if not display:
        return None
    href_value = str(value.context.get("href_value") or value.raw_value or "").strip()

    if value.field == "phone":
        # Only two *numbers* can conflict. If the href does not parse as one,
        # there is nothing to compare the shown text against.
        if not href_normalized:
            return None
        shown = check_phone(display, region=country, rules=rules)
        if not shown.e164 or shown.e164 == href_normalized:
            return None
        return {
            "source_conflict": True,
            "href_value": href_value,
            "display_text": display,
            "href_normalized": href_normalized,
            "display_normalized": shown.e164,
        }

    if value.field == "emails":
        # The address the link targets is compared as written: a mailto: that
        # fails DNS is still an address the page chose to link, and the shown
        # text is a *different* address, not a broken one.
        matches = _EMAIL_TOKEN.findall(display)
        if not matches:
            return None
        shown = matches[0].strip().lower()
        href = href_value.lower()
        if not href or shown == href:
            return None
        return {
            "source_conflict": True,
            "href_value": href_value,
            "display_text": display,
            "href_normalized": href,
            "display_normalized": shown,
        }

    return None


def values_agree(field_name: str, listed: str, website: str, *, country: str, rules: dict) -> bool:
    """Whether the website agrees with the listing about one field.

    This decides whether a fact is a *confirmation* or a *conflict*, so a false
    "disagreement" is not harmless: it would fill the report with noise and hide
    the real conflicts. Each field therefore compares on the form that carries
    the meaning rather than on the raw text:

    ``phone``    both sides are parsed with libphonenumber and compared as E.164,
                 so ``030 12345678`` and ``+49 30 12345678`` agree. Only if
                 neither parses does it fall back to a digit comparison.
    ``address``  both sides are split into components and compared on
                 postal code + street name + house number, so a site that omits
                 the country ("…, 10115 Berlin") is not reported as a conflict.
                 Only if the components are too thin does it compare the
                 alphanumeric skeleton.
    ``title``    containment after normalisation: ``Praxis Beispiel`` and
                 ``Zahnarztpraxis Praxis Beispiel`` agree. This is deliberately
                 permissive — the trade-off is documented, and a missed
                 difference is a *conservative* outcome, not a wrong claim.
    """
    if not listed or not website:
        return False
    if listed == website:
        return True

    if field_name == "phone":
        left = check_phone(listed, region=country, rules=rules)
        right = check_phone(website, region=country, rules=rules)
        if left.e164 and right.e164:
            return left.e164 == right.e164
        return _digits(listed) == _digits(website)

    if field_name == "address":
        return _addresses_agree(listed, website, country=country)

    if field_name == "title":
        left = normalize_text(listed)
        right = normalize_text(website)
        return bool(left) and bool(right) and (left in right or right in left)

    return False


def _addresses_agree(listed: str, website: str, *, country: str) -> bool:
    left = parse_address(listed, country=country).components
    right = parse_address(website, country=country).components
    keys = ("postal_code", "street_name", "house_number")
    if all(left.get(key) and right.get(key) for key in keys):
        return all(
            normalize_text(left[key]) == normalize_text(right[key]) for key in keys
        )
    return _address_skeleton(normalize_text(listed)) == _address_skeleton(normalize_text(website))


def _digits(value: str) -> str:
    text = "".join(char for char in value if char.isdigit())
    return text.lstrip("0") or text


def _address_skeleton(value: str) -> str:
    return "".join(char for char in value if char.isalnum())


def _evidence_ref(page_url: str, value: ExtractedValue) -> str:
    """A locator for the value inside its page."""
    if value.source == "jsonld":
        block = value.context.get("block", "?")
        return f"{page_url}#jsonld[{block}]"
    if value.source == "mailto":
        return f"{page_url}#a[href^=mailto]"
    if value.source == "tel":
        return f"{page_url}#a[href^=tel]"
    return page_url


def read_enrich_config(cfg: Config) -> dict[str, Any]:
    """The enrichment section as a plain dict, for reports."""
    return dict(cfg.data["enrich"])


def website_path(run_dir: pathlib.Path) -> pathlib.Path:
    """Where enrichment artifacts (page copies) would live if kept on disk.

    Stage D stores extracted values and their evidence in SQLite; full page
    copies are not kept, because the fact rows already carry the snippet and the
    locator, and mirroring third-party pages raises questions this stage does not
    need to answer.
    """
    return pathlib.Path(run_dir) / "enrich"
