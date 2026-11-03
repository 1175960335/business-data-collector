"""The OpenStreetMap provider (Overpass API).

Chosen for stage E because it is a real, independent source that needs **no
account and no key**: the public Overpass instances are free to use under a
usage policy that asks for a modest rate, an identifiable user agent and a
small query volume. That is exactly the boundary this stage is allowed to work
inside ("能用现有无需新账号的来源则实现并验证") — the collector obeys it by
using one instance at a time, a per-domain pause, a run-wide request budget and
a single query per run.

Licence: OpenStreetMap data is ODbL 1.0, which *requires* attribution and
share-alike for derived databases. The collector records that in the source
manifest instead of quietly copying the data.

A paid Places/search/social API would be a different licence question and is
deliberately not wired here: the task book allows those only as optional and
forbids asking for keys or spending money.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from .base import (
    JSON_CONTENT_TYPES,
    Provider,
    ProviderOutcome,
    ProviderTask,
    RunContext,
    SourceRecord,
    write_envelopes,
)

#: The Overpass instances the collector may talk to, in order. Config picks one.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

#: Industry name -> the OSM key/value query OpenStreetMap actually indexes on.
#: Anything not listed falls back to a name regex search, which is weaker but
#: is still a real query rather than an invented one.
INDUSTRY_QUERIES: dict[str, tuple[str, str]] = {
    "dentists": ("amenity", "dentist"),
    "doctor": ("amenity", "doctors"),
    "doctors": ("amenity", "doctors"),
    "pharmacy": ("amenity", "pharmacy"),
    "hospital": ("amenity", "hospital"),
    "veterinary": ("amenity", "veterinary"),
    "restaurant": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "bar": ("amenity", "bar"),
    "fast_food": ("amenity", "fast_food"),
    "bakery": ("shop", "bakery"),
    "butcher": ("shop", "butcher"),
    "supermarket": ("shop", "supermarket"),
    "hairdresser": ("shop", "hairdresser"),
    "beauty": ("shop", "beauty"),
    "car_repair": ("shop", "car_repair"),
    "florist": ("shop", "florist"),
    "hardware": ("shop", "hardware"),
    "clothes": ("shop", "clothes"),
    "furniture": ("shop", "furniture"),
    "electronics": ("shop", "electronics"),
    "laundry": ("shop", "laundry"),
    "lawyer": ("office", "lawyer"),
    "accountant": ("office", "accountant"),
    "insurance": ("office", "insurance"),
    "estate_agent": ("office", "estate_agent"),
    "electrician": ("craft", "electrician"),
    "plumber": ("craft", "plumber"),
    "carpenter": ("craft", "carpenter"),
    "painter": ("craft", "painter"),
    "roofer": ("craft", "roofer"),
    "photographer": ("craft", "photographer"),
    "printer": ("craft", "printer"),
}

#: Default search radius when the config gives a centre but no bbox.
DEFAULT_RADIUS_METERS = 3000

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_BYTES = 8 * 1024 * 1024


class OpenStreetMapProvider(Provider):
    """Discovery through Overpass, the read-only query API for OSM data."""

    name = "openstreetmap"
    title = "OpenStreetMap (Overpass API, no account required)"
    license = "ODbL 1.0 (OpenStreetMap data)"
    attribution = (
        "Data © OpenStreetMap contributors, ODbL 1.0 "
        "(https://www.openstreetmap.org/copyright). A derived database must "
        "carry the same notice and share-alike terms."
    )
    requires_account = False
    provenance = ""
    #: Overpass returns OSM coordinates, which are WGS84 (EPSG:4326).
    coordinate_system = "WGS84"

    # -- configuration -------------------------------------------------------

    @property
    def settings(self) -> dict:
        node = (self.cfg.data.get("sources_map") or {}).get(self.name) or {}
        return node if isinstance(node, dict) else {}

    def endpoint(self) -> str:
        return str(self.settings.get("endpoint") or OVERPASS_ENDPOINTS[0])

    def radius_meters(self) -> int:
        value = self.settings.get("radius_meters")
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return DEFAULT_RADIUS_METERS

    def timeout_seconds(self) -> int:
        value = self.settings.get("timeout_seconds")
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return self.cfg.timeout_seconds or DEFAULT_TIMEOUT_SECONDS

    def is_configured(self) -> bool:
        if self.name not in (self.cfg.data.get("sources") or []):
            return False
        # Overpass needs a place to search. A bbox or a centre is enough; asking
        # for neither is a configuration mistake, not a reason to guess.
        return self.cfg.bbox is not None or self.cfg.geo is not None

    def not_configured_reason(self) -> str:
        if self.name not in (self.cfg.data.get("sources") or []):
            return "openstreetmap is not listed in sources"
        return "openstreetmap needs location.bbox or location.geo to search around"

    # -- planning ------------------------------------------------------------

    def _query(self) -> str:
        """The Overpass QL for this run, readable in a dry run."""
        override = self.settings.get("query")
        if override:
            return str(override).strip()

        industry = (self.cfg.industry or "").strip().casefold()
        found = INDUSTRY_QUERIES.get(industry)
        if found is not None:
            key, value = found
            selector = f'["{key}"="{value}"]'
        else:
            # A name search is a real query, but it is weaker: it misses
            # businesses whose OSM name differs from the industry word.
            selector = f'["name"~"{industry}",i]' if industry else '["name"]'

        area = self._area_clause()
        return (
            "[out:json][timeout:%d];\n(\n"
            "  nwr%s(%s);\n"
            ");\n"
            "out center tags;\n" % (self.timeout_seconds(), selector, area)
        )

    def _area_clause(self) -> str:
        bbox = self.cfg.bbox
        if bbox:
            min_lat, min_lon, max_lat, max_lon = bbox
            return f"{min_lat},{min_lon},{max_lat},{max_lon}"
        geo = self.cfg.geo or [0.0, 0.0]
        return f"around:{self.radius_meters()},{geo[0]},{geo[1]}"

    def tasks(self) -> list[ProviderTask]:
        label = f"discover-osm-{_slug(self.cfg.industry or 'all')}"
        url = self.endpoint()
        return [
            ProviderTask(
                label=label,
                query=self._query(),
                targets=(url,),
                extra={"endpoint": url},
            )
        ]

    # -- execution -----------------------------------------------------------

    def run_task(self, task: ProviderTask, ctx: RunContext) -> ProviderOutcome:
        import time

        endpoint = task.extra.get("endpoint") or self.endpoint()
        host = urllib.parse.urlsplit(endpoint).hostname or ""
        limit = ctx.budget_reason()
        if limit:
            return ProviderOutcome(ok=False, skipped_reason=limit)

        # One HTTP call carries the whole query, so the run-wide request budget
        # sees exactly one task for the whole source.
        ctx.spend()
        ctx.be_polite(host)

        url = f"{endpoint}?{urllib.parse.urlencode({'data': task.query})}"
        started = time.monotonic()
        result = ctx.fetcher_for(self.name).fetch(url, accept_types=JSON_CONTENT_TYPES)
        duration = time.monotonic() - started

        if not result.ok:
            return ProviderOutcome(
                ok=False,
                error=f"{result.error_kind}: {result.error_detail}",
                error_kind=result.error_kind or "fetch_failed",
                attempts=result.attempts,
                duration_seconds=duration,
            )

        try:
            payload = json.loads(result.body or "")
        except (json.JSONDecodeError, TypeError) as exc:
            return ProviderOutcome(
                ok=False,
                error=f"the endpoint did not return JSON: {exc}",
                error_kind="bad_payload",
                duration_seconds=duration,
            )

        records, bad = self._elements(payload)
        raw_path = ctx.raw_path(task.label, task.attempt)
        written = write_envelopes(
            raw_path,
            self.name,
            records,
            query=task.query,
            context={
                "source_url": endpoint,
                "mode": "overpass",
                "osm_elements": len(records) + bad,
                "osm_unusable": bad,
            },
        )
        return ProviderOutcome(
            ok=True,
            records=written,
            raw_path=raw_path,
            duration_seconds=duration,
        )

    # -- element -> canonical record -----------------------------------------

    def _elements(self, payload: Any) -> tuple[list[SourceRecord], int]:
        if not isinstance(payload, dict):
            return [], 1
        elements = payload.get("elements")
        if not isinstance(elements, list):
            return [], 1

        records: list[SourceRecord] = []
        unusable = 0
        for element in elements:
            record = self._record(element)
            if record is None:
                unusable += 1
                continue
            records.append(record)
        return records, unusable

    def _record(self, element: Any) -> SourceRecord | None:
        if not isinstance(element, dict):
            return None
        tags = element.get("tags")
        tags = tags if isinstance(tags, dict) else {}

        source_id = _element_ref(element)
        name = _first_str(tags, "name", "name:en", "operator")
        if not name or not source_id:
            # No name and no stable id: nothing to attribute, and inventing a
            # name from a category is exactly what the task book forbids.
            return None

        lat, lon = _coordinates(element)
        address = _osm_address(tags)
        phone = _first_str(tags, "phone", "contact:phone", "telephone")
        website = _first_str(tags, "website", "contact:website", "url")
        emails = _osm_emails(tags)
        category = _first_str(tags, "amenity", "shop", "office", "craft", "tourism", "leisure")

        return SourceRecord(
            title=name,
            address=address,
            phone=phone,
            website=website,
            emails=emails,
            latitude=lat,
            longitude=lon,
            crs="WGS84" if lat is not None and lon is not None else None,
            category=category,
            source_id=source_id,
            source_id_kind="osm",
            native=dict(tags),
        )


# --- helpers ----------------------------------------------------------------


def _element_ref(element: dict) -> str:
    kind = element.get("type")
    oid = element.get("id")
    if not kind or not oid:
        return ""
    return f"{str(kind).lower()}/{oid}"


def _coordinates(element: dict) -> tuple[float | None, float | None]:
    lat = _to_float(element.get("lat"))
    lon = _to_float(element.get("lon"))
    if lat is not None and lon is not None:
        return lat, lon
    center = element.get("center")
    if isinstance(center, dict):
        return _to_float(center.get("lat")), _to_float(center.get("lon"))
    return None, None


def _to_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_str(tags: dict, *keys: str) -> str | None:
    for key in keys:
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _osm_emails(tags: dict) -> tuple[str, ...]:
    out: list[str] = []
    for key in ("email", "contact:email"):
        value = tags.get(key)
        if not isinstance(value, str):
            continue
        for part in value.replace(";", ",").split(","):
            text = part.strip()
            if text and text not in out:
                out.append(text)
    return tuple(out)


def _osm_address(tags: dict) -> str | None:
    """One readable address line from OSM address tags, or None.

    The order is the German/European convention because that is where the
    project's sample city is; the tags themselves are format-independent and the
    raw value is kept in ``native`` for anyone who wants to re-join it.
    """
    street = _first_str(tags, "addr:street")
    number = _first_str(tags, "addr:housenumber")
    postcode = _first_str(tags, "addr:postcode")
    city = _first_str(tags, "addr:city", "addr:town", "addr:village")
    parts = [" ".join(part for part in (street, number) if part)]
    tail = " ".join(part for part in (postcode, city) if part)
    if tail:
        parts.append(tail)
    text = ", ".join(part for part in parts if part).strip(", ").strip()
    return text or None


def _slug(text: str, limit: int = 40) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return (cleaned or "all")[:limit]
