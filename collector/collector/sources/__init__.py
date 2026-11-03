"""Discovery providers and the source manifest.

Task book 004, stage E: "实现统一 provider 接口，至少接入：现有 Go Maps、
OpenStreetMap、用户 URL/CSV 列表、一个可配置企业目录。搜索 provider 设计成
可替换接口，能用现有无需新账号的来源则实现并验证；否则明确标为未配置，
不伪装已完成。"

Four providers ship:

* ``google_maps``   — the Go scraper (stage A-D, already validated);
* ``openstreetmap`` — Overpass API, no account, ODbL;
* ``csv_list``      — a file the operator already has, fully deterministic;
* ``directory``     — a hand-configured business directory with page selectors.

Round 026 P2 adds a fifth:

* ``local_pages``   — HTML pages the operator saved earlier, read offline.

A provider that needs credentials is *declared* to need them
(:attr:`Provider.requires_account`) and never asked for keys: the task book
allows paid Places/search/social APIs only as optional and forbids spending
money on them. Every provider describes its licence and attribution, so the run
can publish a manifest instead of leaving the operator to guess what they are
allowed to do with the output.
"""

from __future__ import annotations

from typing import Any

from .base import Provider
from .csvlist import CsvListProvider
from .directory import BusinessDirectoryProvider
from .google_maps import GoogleMapsProvider
from .localpages import LocalPagesProvider
from .openstreetmap import OpenStreetMapProvider

#: Provider name -> class. Order is the order used in the manifest.
PROVIDER_CLASSES: dict[str, type[Provider]] = {
    GoogleMapsProvider.name: GoogleMapsProvider,
    OpenStreetMapProvider.name: OpenStreetMapProvider,
    CsvListProvider.name: CsvListProvider,
    BusinessDirectoryProvider.name: BusinessDirectoryProvider,
    LocalPagesProvider.name: LocalPagesProvider,
}

#: The order providers are planned in. Google Maps first because it is the
#: validated source; the rest follow in decreasing determinism, with the two
#: offline file readers last because they never touch the network.
PLANNING_ORDER = ("google_maps", "openstreetmap", "csv_list", "directory", "local_pages")


def provider_names() -> tuple[str, ...]:
    return tuple(PROVIDER_CLASSES)


def coordinate_system_of(provider: str) -> str:
    """The CRS a provider's coordinates are in, or "" when it has none.

    Used by the ingest stage to stamp records whose payload does not carry the
    system itself — the Go scraper writes plain JSONL, so nothing in the file
    says its latitude is WGS84. A provider that publishes no coordinates (the
    ``directory`` adapter, ``csv_list``) returns "", and the entity's ``crs``
    stays empty rather than being filled with a guess.
    """
    cls = PROVIDER_CLASSES.get(provider)
    return getattr(cls, "coordinate_system", "") if cls is not None else ""


def build_providers(cfg) -> list[Provider]:
    """Providers for the configured sources, in planning order.

    Only sources the configuration actually lists are built. An unlisted
    provider is not silently skipped: it appears in :func:`manifest` as
    "not configured" with the reason, which is what keeps "配了却不生效" from
    ever looking like success.
    """
    requested = cfg.data.get("sources") or []
    out: list[Provider] = []
    for name in PLANNING_ORDER:
        if name not in requested:
            continue
        cls = PROVIDER_CLASSES.get(name)
        if cls is None:
            continue
        out.append(cls(cfg))
    return out


def manifest(cfg) -> list[dict[str, Any]]:
    """Every provider's licence, attribution and configuration state."""
    out: list[dict[str, Any]] = []
    for name in PLANNING_ORDER:
        cls = PROVIDER_CLASSES.get(name)
        if cls is None:
            continue
        provider = cls(cfg)
        entry = provider.describe()
        entry["planned"] = name in (cfg.data.get("sources") or [])
        # Round 026 P0: when the operator told a provider where its rows came
        # from, say so here. ``csv_list`` is the *input channel* (a local file);
        # the ``origin`` is the site the data was exported from. A reader must
        # be able to tell the two apart, or "the source is a CSV" hides a real
        # site behind a transport detail.
        entry["origin"] = _configured_origin(cfg, provider)
        out.append(entry)
    return out


def _configured_origin(cfg, provider) -> str:
    """The site a provider's rows came from, as the operator configured it."""
    settings = (cfg.data.get("sources_map") or {}).get(provider.name) or {}
    if not isinstance(settings, dict):
        return ""
    provenance = settings.get("provenance")
    if isinstance(provenance, dict):
        origin = str(provenance.get("origin") or "").strip()
        if origin:
            return origin
    # A directory names its site in the start URL rather than a provenance block.
    entries = settings.get("entries")
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        start = str(entries[0].get("start_url") or "").strip()
        if start:
            from urllib.parse import urlsplit

            return urlsplit(start).hostname or ""
        # The offline importer names it in ``origin_url``: the address the page
        # was saved from, which is the same thing, said for a file.
        origin = str(entries[0].get("origin_url") or "").strip()
        if origin:
            from urllib.parse import urlsplit

            return urlsplit(origin).hostname or ""
    return ""


__all__ = [
    "PLANNING_ORDER",
    "PROVIDER_CLASSES",
    "Provider",
    "build_providers",
    "coordinate_system_of",
    "manifest",
    "provider_names",
]
