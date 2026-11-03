"""Turn raw scraper output into observations, entities and field facts.

The raw files are written by the Go scraper with ``-json-records``, so every line
is one ``osint.record/1`` envelope:

    {"schema": "...", "context": {...}, "raw": {...}, "missing": [...], ...}

Plain (unenveloped) objects and fast-mode arrays are also accepted, so a file
produced by an older command line still ingests.

De-duplication is by content hash, which makes re-ingesting the same file a no-op
and makes a resumed run safe: evidence is never duplicated and never dropped.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import db
from .addresscheck import STATE_NAME_NOT_ADDRESS, is_name_shaped
from .config import COLLECT_FIELDS, NEVER_LIVE_PROVIDERS
from .identity import (
    coerce_float,
    domain_of,
    extract_emails,
    fallback_key,
    is_placeholder_email,
    is_valid_email_syntax,
    normalize_phone,
    normalize_text,
    resolve_stable_key,
)
from .util import canonical_json, clip, content_hash, utc_now_iso

#: Default provider for a raw file written before stage E, or by a command line
#: that predates the ``provider`` envelope field. Google Maps is what the file
#: came from, so the old files keep resolving to the same entities.
PROVIDER = "google_maps"

#: Field values that the provider returns as numbers.
NUMERIC_FIELDS = {"latitude", "longitude", "rating", "review_count"}

#: Values a provider fills in as a *default* when it did not actually fetch the
#: field. They are the provider's own "absent" marker, not a measured value.
#:
#: ``google_maps.review_count`` is the known case. The Go scraper parses the
#: count with ``getNthElementAndCast[float64](business, 4, 8)`` (``gmaps/
#: multiple.go``), which yields ``0`` when that slot is missing, and its own
#: ``leadsdb`` writer only stores the count when it is ``> 0`` — the scraper
#: admitting that ``0`` means "not retrieved". Recording that 0 as a
#: *verified* review count is exactly the "把缺失默认0当已核实" failure the task
#: book names, so it is stored as a missing value instead. The raw payload keeps
#: its literal 0 (the raw file is never rewritten), and the fact keeps the raw
#: text so the evidence is still inspectable — only its validation status says
#: "this is not a value we can stand behind".
#:
#: 061: fast mode does **not** always withhold the count -- a real run returned
#: 344, 37, 71 and so on for 19 of 19 rows -- so the rule above is a statement
#: about the literal ``0`` only, never about the mode. An earlier comment here
#: said fast mode never returns counts and the workbook repeated it; both were
#: wrong, and both now say what is true.
PROVIDER_ABSENT_DEFAULTS: dict[tuple[str, str], frozenset[str]] = {
    ("google_maps", "review_count"): frozenset({"0", "0.0"}),
}


def is_absent_default(provider: str, field_name: str, value: Any) -> bool:
    """True when ``value`` is the provider's placeholder for "did not fetch it".

    Deliberately conservative: it fires only for the exact (provider, field)
    pairs listed above, so a genuine 0 from a source that does measure it (an
    OSM record, a CSV row) is left untouched.
    """
    markers = PROVIDER_ABSENT_DEFAULTS.get((str(provider), str(field_name)))
    if not markers or value is None:
        return False
    return str(value).strip() in markers


@dataclass
class IngestStats:
    files: int = 0
    lines: int = 0
    envelopes: int = 0
    malformed_lines: int = 0
    skipped_elements: int = 0
    observations_new: int = 0
    observations_repeated: int = 0
    entities_new: int = 0
    entities_seen: int = 0
    facts_new: int = 0
    facts_repeated: int = 0
    missing_identity: int = 0
    malformed_examples: list[tuple[str, int]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = self.__dict__.copy()
        return out


def requested_fields(cfg) -> list[str]:
    """Fields the run must materialise facts for.

    Union of what is collected and what is exported; anything else is neither
    fetched nor stored.
    """
    names = list(cfg.collect_fields)
    for name in cfg.export_fields:
        if name in COLLECT_FIELDS and name not in names:
            names.append(name)
    return names


# --- parsing ----------------------------------------------------------------


def iter_payloads(path: pathlib.Path, start_line: int = 0) -> Iterator[tuple[int, dict, dict, str, list[str], list[str]]]:
    """Yield (line_no, raw_object, context, schema, missing, unknown) per record."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no <= start_line:
                continue
            text = line.strip()
            if not text:
                continue

            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                yield (line_no, {}, {"__malformed__": True}, "", [], [])
                continue

            items = decoded if isinstance(decoded, list) else [decoded]
            for item in items:
                if not isinstance(item, dict):
                    yield (line_no, {}, {"__skipped_element__": True}, "", [], [])
                    continue

                if "raw" in item and isinstance(item.get("raw"), dict):
                    context = dict(item.get("context") or {})
                    # ``write_envelopes`` puts the provider at the envelope's
                    # top level; hand-written envelopes (and the stage E
                    # fixtures) put it inside context. Accept both — context
                    # wins — so a provider can never fall back to the Google
                    # default just because of where the field happens to sit.
                    if item.get("provider") and not context.get("provider"):
                        context["provider"] = item.get("provider")
                    yield (
                        line_no,
                        item["raw"],
                        context,
                        str(item.get("schema") or ""),
                        list(item.get("missing") or []),
                        list(item.get("unknown") or []),
                    )
                else:
                    yield (line_no, item, {}, "", [], [])


# --- normalisation of one field ---------------------------------------------


def _normalise(field_name: str, value: Any, cfg) -> tuple[str | None, str, str | None]:
    """Return (raw_value, validation_status, validation_note)."""
    if value is None or value == "" or value == [] or value == {}:
        return None, "missing", "field absent or empty in the provider payload"

    if field_name == "emails":
        # Handled per address by the caller; this branch is a fallback.
        return clip(canonical_json(value)), "unverified", None

    if field_name == "phone":
        raw = str(value).strip()
        digits = normalize_phone(raw)
        if cfg.rules().get("phone_syntax", True) and len(digits.lstrip("+")) < 7:
            return raw, "invalid", "fewer than 7 digits"
        if cfg.rules().get("require_country_phone") and not digits.startswith("+"):
            return raw, "uncertain", "no international prefix; country not inferred"
        return raw, "valid", None

    if field_name in NUMERIC_FIELDS:
        number = coerce_float(value)
        if number is None:
            return str(value), "invalid", "not a number"
        # Keep integers looking like integers: a review count of 10 must not be
        # stored as "10.0".
        text = str(int(number)) if number.is_integer() else repr(number)
        return text, "valid", None

    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip(), "unverified", None

    return clip(canonical_json(value)), "unverified", None


def _fact_rows(raw: dict, field_name: str, cfg) -> list[tuple[str, str | None, str, str | None]]:
    """Expand one config field into (raw_value, normalized_value, status, note)."""
    raw_key = COLLECT_FIELDS.get(field_name)
    if raw_key is None:
        return []
    value = raw.get(raw_key)
    if field_name == "category" and not _has_text(value):
        # 061: Google's payload carries the business categories as a list and
        # leaves the singular ``category`` empty, so every row in the run came
        # back with no category at all while the source had said "Dog day care
        # center; Dog walker; ...". The list is the same fact the singular key
        # is meant to hold, so it is used when that one is blank -- a reader
        # judging whether a row is really in the industry needs it, and an empty
        # cell reads as "the source did not say".
        value = _categories_value(raw.get("categories"))

    if field_name == "emails":
        addresses = extract_emails(value)
        if not addresses:
            return [(None, None, "missing", "field absent or empty in the provider payload")]
        rows = []
        for address in addresses:
            if not cfg.rules().get("email_syntax", True):
                rows.append((address, normalize_text(address), "unverified", None))
            elif not is_valid_email_syntax(address):
                rows.append((address, None, "invalid", "fails email syntax check"))
            elif is_placeholder_email(address):
                rows.append(
                    (address, normalize_text(address), "uncertain", "placeholder address from the page")
                )
            else:
                rows.append((address, normalize_text(address), "valid", None))
        return rows

    raw_value, status, note = _normalise(field_name, value, cfg)
    if raw_value is None:
        return [(None, None, status, note)]

    if field_name == "website":
        return [(raw_value, domain_of(raw_value) or None, status, note)]

    if field_name in ("title", "address", "complete_address"):
        if field_name != "title" and is_name_shaped(raw_value, raw.get("title")):
            # 048/050: the source put the business's name in the address field.
            # Kept as the evidence snippet (the raw file is never rewritten) but
            # recorded as *uncertain* with the name-shaped state, so nothing
            # downstream can use the name as an address, and so the reading
            # version says 待确认 rather than 冲突 or a bare 缺失. The raw value
            # stays: it is the evidence of what the source wrote.
            return [
                (
                    raw_value,
                    None,
                    "uncertain",
                    "来源的地址字段里写的是企业名称本身，不是地址；按无地址证据记录为待确认，"
                    "原值保留为证据，不作为地址依据",
                )
            ]
        return [(raw_value, normalize_text(raw_value), status, note)]

    if field_name == "phone":
        return [(raw_value, normalize_phone(raw_value) or None, status, note)]

    return [(raw_value, None, status, note)]


# --- ingestion --------------------------------------------------------------


def ingest_file(
    conn,
    path: pathlib.Path,
    *,
    run_id: int,
    cfg,
    start_line: int = 0,
    default_acquired_via: str | None = None,
) -> IngestStats:
    """Ingest one raw file. Safe to call repeatedly on the same file.

    ``default_acquired_via`` is how a record gets a method when its envelope
    does not state one (061: the Go scraper never does). It is supplied by the
    caller that knows which provider wrote the file, never derived here -- see
    the note at the observation upsert.
    """
    stats = IngestStats(files=1)
    path = pathlib.Path(path)
    fields = requested_fields(cfg)
    rules = cfg.rules()

    for line_no, raw, context, schema, missing, unknown in iter_payloads(path, start_line):
        if context.get("__malformed__"):
            stats.malformed_lines += 1
            if len(stats.malformed_examples) < 20:
                stats.malformed_examples.append((str(path.name), line_no))
            continue
        if context.get("__skipped_element__"):
            stats.skipped_elements += 1
            continue

        stats.lines += 1
        stats.envelopes += 1
        # Stage E lets every provider write the same envelope, so the source is
        # read from it rather than assumed. A file without the field predates
        # stage E or came from a hand-written command line; the Go scraper is
        # the only thing that wrote those, so it stays the default.
        provider = str(context.get("provider") or "").strip() or PROVIDER

        # Round 026 P0: the page the facts were read from, kept apart from the
        # transport. A csv_list run carries the CSV path in ``source_url`` and
        # the real page in ``source_page_url``; citing the path was the 024 bug.
        # When a provider states no page, the old value is used unchanged, so
        # every existing source behaves exactly as before.
        evidence_url = _text(context.get("source_page_url")) or _text(context.get("source_url"))

        # 061: the Go scraper writes its own envelope and states no provenance,
        # so every Google row of a live run used to land with ``acquired_via``
        # NULL and the workbook printed "获取方式：未知 19" for a batch this run
        # had just fetched over the network.
        #
        # The method is still *not* guessed from the payload -- 026 P0 forbids
        # that, and a raw file handed over from an older run is not live. It is
        # supplied by the caller that knows which provider produced this file
        # (:class:`orchestrator.Orchestrator`, right after the provider's task
        # returns). A file ingested with no such caller stays "unknown", which
        # is what the existing contract demands, and a stated value always wins.
        stated_via = _text(context.get("acquired_via")) or _text(default_acquired_via)

        payload_json = canonical_json(raw)
        obs_id, created = db.upsert_observation(
            conn,
            provider=provider,
            payload=raw,
            raw_json=payload_json,
            content_hash=content_hash(raw),
            mode=context.get("mode"),
            query=context.get("query"),
            source_url=context.get("source_url"),
            scraped_at=context.get("scraped_at"),
            record_schema=schema or None,
            missing=missing,
            unknown=unknown,
            raw_path=str(path),
            raw_line=line_no,
            run_id=run_id,
            acquired_via=stated_via,
            source_page_url=_text(context.get("source_page_url")),
            detail_url=_text(context.get("detail_url")),
            detail_fetched=bool(context.get("detail_fetched")) or None,
            origin=_text(context.get("origin")),
            snapshot_id=_text(context.get("snapshot_id")),
            observed_at=_text(context.get("observed_at")),
            input_path=_text(context.get("input_path")),
        )
        if created:
            stats.observations_new += 1
        else:
            stats.observations_repeated += 1

        resolved = resolve_stable_key(raw, provider)
        if resolved is None:
            stats.missing_identity += 1
            fallback = fallback_key(raw)
            db.insert_error(
                conn,
                run_id=run_id,
                stage="ingest",
                kind="missing_identity",
                message=(
                    "record has no place_id/cid/data_id/link/source_id and cannot "
                    "be attributed"
                ),
                context={
                    "raw_path": str(path),
                    "raw_line": line_no,
                    "provider": provider,
                    "title": raw.get("title"),
                    "fallback_key": fallback[1] if fallback else None,
                },
            )
            continue

        kind, key = resolved
        entity_id, entity_created = _upsert_entity_from_raw(
            conn,
            raw=raw,
            kind=kind,
            stable_key_value=key,
            country=cfg.country,
            provider=provider,
        )
        if entity_created:
            stats.entities_new += 1
        else:
            stats.entities_seen += 1

        db.link_entity_observation(
            conn,
            entity_id=entity_id,
            observation_id=obs_id,
            run_id=run_id,
            query=context.get("query"),
        )

        observed_at = utc_now_iso()
        for field_name in fields:
            for raw_value, normalized, status, note in _fact_rows(raw, field_name, cfg):
                if status == "missing" and rules.get("require_identity", True) and field_name == "title":
                    status = "missing"

                # A provider's "did not fetch it" placeholder is not a value:
                # record it as missing so nothing downstream presents the 0 as a
                # verified review count. The raw text is kept as the evidence
                # snippet, so the original payload value is still inspectable.
                if is_absent_default(provider, field_name, raw_value):
                    status = "missing"
                    note = (
                        f"{provider} returns {raw_value!r} as its absent-default for "
                        f"{field_name}; the source did not report a value"
                    )

                key = content_hash(
                    [
                        entity_id,
                        field_name,
                        raw_value,
                        provider,
                        evidence_url,
                    ]
                )
                validation_state = _validation_state(field_name, status, provider)
                if (
                    status == "uncertain"
                    and field_name != "title"
                    and is_name_shaped(raw_value, raw.get("title"))
                ):
                    # 048/050: an address slot holding the business's own name is
                    # the one uncertain address there is. Its state names the
                    # reason, so the export can say 待确认 without guessing from
                    # the field name -- and the same test that decided the status
                    # decides the state, so the two cannot drift apart.
                    validation_state = STATE_NAME_NOT_ADDRESS
                inserted = db.insert_fact(
                    conn,
                    entity_id=entity_id,
                    observation_id=obs_id,
                    run_id=run_id,
                    field=field_name,
                    raw_value=raw_value,
                    normalized_value=normalized,
                    provider=provider,
                    source_url=evidence_url,
                    query_context=context.get("query"),
                    scraped_at=context.get("scraped_at"),
                    evidence_snippet=clip(raw_value),
                    evidence_ref=f"{path.name}#L{line_no}",
                    extractor_version=_extractor_version(),
                    validation_status=status,
                    validation_note=note,
                    validation_state=validation_state,
                    checks_json=_checks_json(field_name, status, provider),
                    observed_at=observed_at,
                    fact_key=key,
                )
                if inserted:
                    stats.facts_new += 1
                else:
                    stats.facts_repeated += 1

    return stats


def _extractor_version() -> str:
    from .version import EXTRACTOR_VERSION

    return EXTRACTOR_VERSION


def _text(value: Any) -> str | None:
    """A context value as a trimmed string, or None when it carries nothing.

    Provenance is stored as ``NULL`` when unknown rather than as an empty
    string, so "not stated" and "stated as blank" cannot be confused.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _has_text(value: Any) -> bool:
    """Whether a payload value actually says something."""
    return _text(value) is not None


def _categories_value(value: Any) -> str | None:
    """A provider's category list as the one string the category field holds.

    Lists are joined with a semicolon rather than turned into their first
    element: a business listed as both a day-care and a walker is both, and
    dropping the rest would make a later industry judgement look better
    supported than it is.
    """
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "; ".join(dict.fromkeys(parts)) or None
    return _text(value)


#: Machine-readable state for a fact that came from the provider listing.
#: Stage D added the column; listing facts fill it too, so a reader can tell
#: what was actually checked. In particular an email from the listing is
#: syntax-checked only — its domain's mail setup is established later, during
#: website enrichment — and saying so here is the difference between "valid"
#: meaning "well-formed" and meaning "deliverable".
_STATE_BY_FIELD_AND_STATUS = {
    "emails": {
        "valid": "syntax_ok_dns_not_checked",
        "invalid": "syntax_invalid",
        "uncertain": "placeholder_address",
    },
    "phone": {
        "valid": "listing_phone_syntax_ok",
        "invalid": "listing_phone_invalid",
        "uncertain": "listing_phone_no_country_context",
    },
}

_CHECK_BY_FIELD = {
    "emails": "email_syntax",
    "phone": "phone_syntax",
    "latitude": "numeric",
    "longitude": "numeric",
    "rating": "numeric",
    "review_count": "numeric",
}


def _validation_state(field_name: str, status: str, provider: str = PROVIDER) -> str:
    if status == "missing":
        return "missing"
    specific = _STATE_BY_FIELD_AND_STATUS.get(field_name, {}).get(status)
    if specific:
        return specific
    # "listing" is the Google Maps vocabulary and stays there. Every other
    # source gets its own prefix, so a reader can tell which source a value came
    # from without opening the raw file.
    return f"listing_{status}" if provider == PROVIDER else f"{provider}_{status}"


def _checks_json(field_name: str, status: str, provider: str = PROVIDER) -> str:
    return canonical_json(
        {
            "source": provider,
            "check": _CHECK_BY_FIELD.get(field_name, "none"),
            "status": status,
        }
    )


def _upsert_entity_from_raw(
    conn, *, raw: dict, kind: str, stable_key_value: str, country: str,
    provider: str = PROVIDER,
):
    name = raw.get("title")
    name = name.strip() if isinstance(name, str) and name.strip() else None

    address = raw.get("address") or raw.get("complete_address")
    address = address.strip() if isinstance(address, str) and address.strip() else None
    # 048: a source that wrote the business's own name into the address slot
    # stated no address. Storing it on the entity would make every later reader
    # (the ownership judgement above all) treat the name as a second, independent
    # signal -- two copies of the name are still one fact. The raw text is not
    # lost: the fact row keeps it as evidence, and the raw file keeps it verbatim
    # under ``raw.native``. What is dropped is the claim that it is an address.
    address_is_name = bool(address) and is_name_shaped(address, name)
    if address_is_name:
        address = None

    website = raw.get("web_site")
    website = website.strip() if isinstance(website, str) and website.strip() else None

    phone = raw.get("phone")
    phone = phone.strip() if isinstance(phone, str) and phone.strip() else None

    existing = conn.execute(
        "SELECT id FROM entities WHERE stable_key = ?", (stable_key_value,)
    ).fetchone()
    entity_id = db.upsert_entity(
        conn,
        stable_key=stable_key_value,
        id_kind=kind,
        name=name,
        name_norm=normalize_text(name) or None,
        address=address,
        address_norm=normalize_text(address) or None,
        latitude=coerce_float(raw.get("latitude")),
        longitude=coerce_float(raw.get("longitude")),
        phone_norm=normalize_phone(phone) or None,
        website=website,
        domain=domain_of(website) or None,
        country=country or None,
        crs=_coordinate_system(raw, provider),
    )
    return entity_id, existing is None


def _coordinate_system(raw: dict, provider: str) -> str | None:
    """Which CRS the coordinates in this record are in, or None.

    A record that carries its own ``crs`` is trusted; otherwise the provider's
    declared system is used. The Go scraper writes plain JSONL with no system in
    it, so without this its latitude would be stored with no way to tell it is
    WGS84 — and the merge stage would then be free to compare it against a
    GCJ-02 point. A provider with no declared system leaves it empty rather than
    inventing one.
    """
    declared = raw.get("crs")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    if raw.get("latitude") is None and raw.get("longitude") is None:
        return None
    from .sources import coordinate_system_of

    return coordinate_system_of(provider) or None


def ingest_one(
    conn,
    path: pathlib.Path,
    *,
    run_id: int,
    cfg,
    default_acquired_via: str | None = None,
) -> IngestStats:
    """Ingest one raw file from where the checkpoint left off.

    Called both after each provider task (so ``target_count`` can be evaluated
    against real numbers) and by the ingest stage as a catch-up pass. The
    checkpoint makes the second call cheap and the content hash makes it safe.

    The run-wide totals are updated here rather than by the caller, so an
    incremental pass and a catch-up pass both count.
    """
    path = pathlib.Path(path)
    key = f"file:{path.name}"
    start = int(db.get_checkpoint(conn, run_id, "ingest", key) or 0)
    stats = ingest_file(
        conn,
        path,
        run_id=run_id,
        cfg=cfg,
        start_line=start,
        default_acquired_via=default_acquired_via,
    )
    db.set_checkpoint(conn, run_id, "ingest", key, _count_lines(path))
    _accumulate(conn, run_id, stats)
    return stats


#: Counters that accumulate across every ingest pass of a run. ``files`` is not
#: among them: the same file is legitimately visited by the incremental pass and
#: again by the catch-up pass, so summing it would report two files for one.
CUMULATIVE_COUNTERS = (
    "lines",
    "envelopes",
    "malformed_lines",
    "skipped_elements",
    "observations_new",
    "observations_repeated",
    "entities_new",
    "entities_seen",
    "facts_new",
    "facts_repeated",
    "missing_identity",
)


def run_totals(conn, run_id: int) -> dict:
    """Run-wide ingest totals, as stored in the ingest checkpoint."""
    return db.get_checkpoint(conn, run_id, "ingest", "stats") or {}


def _accumulate(conn, run_id: int, stats: IngestStats) -> None:
    totals = run_totals(conn, run_id)
    current = stats.as_dict()
    for key in CUMULATIVE_COUNTERS:
        totals[key] = int(totals.get(key, 0)) + int(current.get(key, 0))
    db.set_checkpoint(conn, run_id, "ingest", "stats", totals)


def ingest_directory(conn, directory: pathlib.Path, *, run_id: int, cfg) -> IngestStats:
    """Ingest every ``*.jsonl`` file in a directory, oldest name first."""
    total = IngestStats()
    directory = pathlib.Path(directory)
    if not directory.is_dir():
        return total

    for path in sorted(directory.glob("*.jsonl")):
        file_stats = ingest_one(conn, path, run_id=run_id, cfg=cfg)
        total.files += file_stats.files
        total.lines += file_stats.lines
        total.envelopes += file_stats.envelopes
        total.malformed_lines += file_stats.malformed_lines
        total.skipped_elements += file_stats.skipped_elements
        total.observations_new += file_stats.observations_new
        total.observations_repeated += file_stats.observations_repeated
        total.entities_new += file_stats.entities_new
        total.entities_seen += file_stats.entities_seen
        total.facts_new += file_stats.facts_new
        total.facts_repeated += file_stats.facts_repeated
        total.missing_identity += file_stats.missing_identity
        total.malformed_examples += file_stats.malformed_examples

    return total


def _count_lines(path: pathlib.Path) -> int:
    count = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for _ in handle:
            count += 1
    return count
