"""Shared test helpers."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
COLLECTOR_DIR = HERE.parent
STUB = HERE / "stub_go.py"

BASE_CONFIG: dict = {
    "name": "test",
    "industry": "dentists",
    "location": {
        "query": "dentists in Berlin",
        "country": "DE",
        "city": "Berlin",
        "geo": [52.52, 13.405],
    },
    "target_count": 50,
    "sources": ["google_maps"],
    "collect": {
        "mode": "fast",
        "fields": [
            "title",
            "address",
            "phone",
            "website",
            "latitude",
            "longitude",
            "data_id",
            "link",
        ],
        "max_depth": 1,
        "zoom": 15,
        "language": "en",
        "radius_meters": 10000,
        "pages_per_site": 0,
    },
    "enrich": {"enabled": False, "fields": [], "pages_per_site": 0},
    "export": {
        "fields": [
            "entity_id",
            "stable_key",
            "title",
            "address",
            "phone",
            "website",
            "latitude",
            "longitude",
            "provider",
            "query",
            "source_url",
            "scraped_at",
            "first_seen_at",
            "last_seen_at",
        ],
        "formats": ["csv", "jsonl"],
        "directory": "export",
    },
    "runtime": {
        "concurrency": 1,
        "timeout_seconds": 60,
        "inactivity_seconds": 5,
        "proxy": "",
    },
    "validation": {"rules": {}},
    "output": {"directory": "runs", "database": "collector.sqlite", "raw_directory": "raw"},
}


def deep_merge(base: dict, overrides: dict) -> dict:
    out = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def config(overrides: dict | None = None, source: str = "<test>"):
    from collector.config import Config, validate

    raw = deep_merge(BASE_CONFIG, overrides or {})
    return Config(validate(raw, source=source), source=source)


def stub_adapter(env: dict | None = None, **kwargs):
    """A GoAdapter whose 'binary' is the test interpreter running the stub."""
    from collector.goadapter import GoAdapter

    merged = dict(env or {})
    return GoAdapter(
        binary=pathlib.Path(sys.executable),
        prefix=(str(STUB),),
        extra_env=merged,
        **kwargs,
    )


def envelope(
    raw: dict,
    *,
    query: str = "dentists in Berlin",
    mode: str = "fast",
    provider: str = "google_maps",
    scraped_at: str = "",
    provenance: dict | None = None,
    source_url: str = "",
) -> dict:
    """A stage E envelope. ``provider`` belongs in ``context`` — the ingest
    layer reads it from there, so a top-level assignment is silently ignored
    and every record resolves as Google.

    ``provenance`` adds the 026 P0 keys (``acquired_via``, ``source_page_url``,
    ``observed_at``, ``origin``, ``snapshot_id``, ``input_path``) to the same
    context block, because that is where ingest reads them from. Without it the
    record's provenance is genuinely unknown, which is what most fixtures want.

    The default ``query`` is the dentist fixture's, and so is the default
    ``source_url``, which is derived from it. **A caller describing a different
    batch must pass its own ``query``** -- 045 found a German-company batch
    carrying ``dentists in Berlin`` because a verification script took the
    default. Deriving the URL keeps a stated query and its source consistent
    instead of pairing a new query with the old fixture's URL; ``source_url``
    can still be given explicitly when a provider's URL is not query-shaped.
    """
    context = {
        "provider": provider,
        "mode": mode,
        "query": query,
        "source_url": source_url
        or f"https://www.google.com/maps/search/{urllib.parse.quote_plus(query)}",
        "scraped_at": scraped_at or None,
    }
    context.update(provenance or {})
    return {
        "schema": "osint.record/1",
        "context": context,
        "raw": raw,
        "missing": [],
        "unknown": [],
    }


def raw_record(**overrides) -> dict:
    base = {
        "title": "Praxis Beispiel",
        "address": "Musterstra\u00dfe 1, 10115 Berlin",
        "phone": "+49 30 12345678",
        "web_site": "https://www.praxis.example.de",
        "latitude": 52.52,
        "longitude": 13.40,
        "category": "Zahnarzt",
        "review_rating": 4.5,
        "review_count": 12,
        "data_id": "0xstub0001:0xabc",
        "link": "https://www.google.com/maps/place/praxis",
        "emails": ["praxis@example.de"],
    }
    base.update(overrides)
    return base


def temp_dir(test_case) -> pathlib.Path:
    """A temporary directory cleaned up when ``test_case`` finishes."""
    handle = tempfile.TemporaryDirectory()
    test_case.addCleanup(handle.cleanup)
    return pathlib.Path(handle.name)


# --- stage D helpers --------------------------------------------------------


class FakeResolver:
    """A DNS resolver with scripted answers, so every state is deterministic.

    Implements the same two methods :mod:`collector.emailcheck` calls, so no
    subclassing is needed.
    """

    def __init__(self, mx: dict | None = None, addresses: dict | None = None):
        self.mx_map = dict(mx or {})
        self.address_map = dict(addresses or {})
        self.mx_calls: list[str] = []
        self.address_calls: list[str] = []

    def mx(self, domain: str):
        self.mx_calls.append(domain)
        return self.mx_map.get(domain, ("noanswer", ()))

    def addresses(self, domain: str):
        self.address_calls.append(domain)
        return self.address_map.get(domain, ("nxdomain", ()))


def local_policy(*, allow_loopback: bool = True, allow_private: bool = True):
    """A policy that permits the loopback stub server.

    ``allow_private=False`` keeps loopback reachable but still refuses private
    ranges, which is what a test needs when it wants to check that a private
    address in a listing is rejected while the stub server stays usable.
    Production uses the default policy, which refuses both; a test asserts that.
    """
    from collector.webfetch import UrlPolicy

    return UrlPolicy(
        allow_loopback=allow_loopback, allow_private=allow_private, resolve_hosts=False
    )


def local_fetcher(*, allow_private: bool = True, **kwargs):
    """A fetcher wired for the loopback stub server: no proxy, no waiting."""
    from collector.webfetch import Fetcher

    kwargs.setdefault("policy", local_policy(allow_private=allow_private))
    kwargs.setdefault("trust_env_proxy", False)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return Fetcher(**kwargs)


def enrich_config(overrides: dict | None = None):
    """A config with enrichment enabled, ready for the enrich tests."""
    base = {
        "collect": {
            "fields": ["title", "address", "phone", "website", "emails"],
        },
        "enrich": {
            "enabled": True,
            "fields": ["title", "emails", "phone", "address", "website"],
            "pages_per_site": 3,
        },
    }
    if overrides:
        base = deep_merge(base, overrides)
    return config(base)


def site_page(*, jsonld: str = "", body: str = "", title: str = "Test") -> str:
    """An HTML page with an optional JSON-LD block."""
    block = (
        f'<script type="application/ld+json">{jsonld}</script>' if jsonld else ""
    )
    return (
        "<!doctype html><html><head>"
        f"<title>{title}</title>{block}"
        f"</head><body>{body}</body></html>"
    )


# --- round 026 P1: candidate-site discovery ---------------------------------


def sitediscovery_config(
    user_list: object = "",
    *,
    pages: list | None = None,
    overrides: dict | None = None,
    sources: list | None = None,
):
    """A config with candidate-site discovery enabled, ready for its tests.

    At least one finder must actually be configured -- the validator refuses a
    section that would look for candidates nowhere -- so the caller passes the
    operator-supplied list path and/or the saved pages it wants to exercise.
    """
    base = {
        "collect": {"fields": ["title", "address", "phone", "website"]},
        "sitediscovery": {
            "enabled": True,
            "sources": list(sources) if sources else ["user_list", "page_links", "search"],
            "pages_per_site": 1,
            "budget": {"max_requests": 50, "max_seconds": 120},
            "user_list": {"path": str(user_list)},
            "page_links": {"pages": list(pages or [])},
        },
    }
    if overrides:
        base = deep_merge(base, overrides)
    return config(base)


def write_site_list(path, rows) -> pathlib.Path:
    """Write an operator-supplied ``stable_key,name,url`` CSV."""
    import csv

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        writer = csv.writer(handle)
        writer.writerow(["stable_key", "name", "url"])
        for row in rows:
            writer.writerow(list(row))
    return pathlib.Path(path)


def discoverer(cfg, *, allow_private=True, **kwargs):
    """A :class:`SiteDiscoverer` wired for the loopback stub server."""
    from collector.sitediscovery import SiteDiscoverer

    kwargs.setdefault("fetcher", local_fetcher(allow_private=allow_private))
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("polite_delay_seconds", 0)
    return SiteDiscoverer(cfg, **kwargs)


#: The 顺企网 field selectors, as the screen's preset ships them. Kept here so a
#: test can exercise the offline importer against the real preset shape rather
#: than a simplified one of its own.
SHUNQIWANG_SELECTORS = {
    "title": "h1",
    "address": "#contact dl.codl dd:nth-of-type(1)",
    "phone": "#contact dl.codl dd:nth-of-type(2)",
    "description": "#aboutuscontent",
}


def write_saved_page(path, html: str, *, encoding: str = "utf-8") -> pathlib.Path:
    """Write a saved page to disk, the way a browser 'Save as' would."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(html.encode(encoding))
    return path


def local_pages_config(
    listing_path,
    *,
    origin_url: str = "https://www.11467.com/hangzhou/search/9092.htm",
    details_dir=None,
    details: list | None = None,
    selectors: dict | None = None,
    observed_at: str = "",
    max_detail_pages: int = 200,
    detail_pattern: str = r"/qiye/\d+\.htm",
    entry_extra: dict | None = None,
    overrides: dict | None = None,
):
    """A config whose only source is the offline saved-page importer."""
    entry: dict = {
        "name": "offline",
        "listing_path": str(listing_path),
        "origin_url": origin_url,
        "max_detail_pages": max_detail_pages,
    }
    if detail_pattern:
        entry["detail_pattern"] = detail_pattern
    entry["field_selectors"] = dict(selectors or SHUNQIWANG_SELECTORS)
    if details_dir is not None:
        entry["details_dir"] = str(details_dir)
    if details:
        entry["details"] = list(details)
    if observed_at:
        entry["observed_at"] = observed_at
    if entry_extra:
        # Merged into the entry rather than replacing it: ``overrides`` is a
        # deep merge and would swap the whole ``entries`` list, losing the path.
        entry.update(entry_extra)
    base = {
        "sources": ["local_pages"],
        "sources_map": {"local_pages": {"entries": [entry]}},
        "collect": {
            "mode": "fast",
            "fields": ["title", "address", "phone", "website", "category", "description"],
        },
    }
    if overrides:
        base = deep_merge(base, overrides)
    return config(base)


def local_pages_provider(cfg):
    from collector.sources import PROVIDER_CLASSES

    return PROVIDER_CLASSES["local_pages"](cfg)


def read_raw_envelopes(path) -> list[dict]:
    """Every envelope in a raw JSONL file, in order."""
    import json

    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                out.append(json.loads(line))
    return out
