"""Configuration loading, validation and fingerprinting.

Task book 004, stage C: "配置至少包含：industry、location/country、可选 bbox、
target_count、sources、collect/enrich/export 字段、每站页数、并发、超时、
请求/时间预算、验证规则、输出路径。未知或不合法配置明确报错。"

Every rejection names the exact config path that is wrong (``collect.fields[2]``),
so the file can be fixed without reading this loader.

The accepted formats are YAML (``.yaml``/``.yml``, needs PyYAML), TOML
(``.toml``, stdlib from 3.11) and JSON (``.json``, stdlib).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

from . import regions
from .errors import ConfigError
from .matching import INDUSTRY_PROFILES as _INDUSTRY_PROFILES

# --- vocabularies -----------------------------------------------------------

SUPPORTED_SOURCES = ("google_maps", "openstreetmap", "csv_list", "directory", "local_pages")
SUPPORTED_MODES = ("fast", "browser")
SUPPORTED_FORMATS = ("csv", "jsonl", "xlsx")

#: Reader-facing export languages. ``bilingual`` is the combined sheet the
#: stage 017 workbook already produced and keeps its file name; ``zh`` and
#: ``en`` are the single-language variants. Several may be selected at once.
SUPPORTED_LANGUAGES = ("zh", "en", "bilingual")

#: What a config that does not mention ``languages`` produces. This is the
#: historical behaviour (the bilingual workbook, and only it), kept so a run
#: exported before languages existed re-exports to exactly the same file set.
DEFAULT_LANGUAGES = ("bilingual",)

#: Per-source network routing. ``inherit`` uses ``runtime.proxy`` and the
#: system environment; ``direct`` deliberately ignores both; ``proxy`` names
#: one proxy for that source only.
NETWORK_MODES = ("inherit", "direct", "proxy")

#: Per-provider settings keys. ``sources`` keeps its list form (a list of the
#: sources to use) so existing configs are untouched; anything provider-specific
#: lives in ``sources_map.<name>`` and is still checked for unknown keys here, so
#: a typo in a directory entry is an error rather than a silently ignored line.
PROVIDER_SETTINGS_KEYS: dict[str, tuple[str, ...]] = {
    # ``network`` is accepted by every provider: it is the one setting that
    # applies to all of them (see NETWORK_MODES).
    "google_maps": ("network",),
    "openstreetmap": ("endpoint", "radius_meters", "timeout_seconds", "query", "network"),
    "csv_list": ("path", "id_column", "columns", "delimiter", "provenance", "network"),
    "directory": (
        "entries",
        "delay_seconds",
        "max_listing_pages",
        "max_detail_pages",
        "stop_after_empty_pages",
        "stop_after_repeated_pages",
        "network",
    ),
    # Round 026 P2: the offline importer. No ``network`` key on purpose -- this
    # provider makes no request, so offering a proxy setting for it would be the
    # "configured but does nothing" failure the task book warns about.
    "local_pages": ("entries", "max_detail_pages"),
}

#: Per-entry keys for the configurable business directory.
DIRECTORY_ENTRY_KEYS = (
    "name",
    "start_url",
    "detail_pattern",
    #: 034/059: the container the businesses are listed in -- keeps the page's
    #: furniture out of the harvest. The offline importer has honoured it since
    #: 034; the live walk now reads the same key.
    "list_selector",
    "field_selectors",
    "pagination",
    "delay_seconds",
    "max_listing_pages",
    "max_detail_pages",
    "stop_after_empty_pages",
    "stop_after_repeated_pages",
)

#: Per-entry keys for the offline saved-page importer. ``listing_path`` and
#: ``origin_url`` are required together: the file without the address it was
#: saved from cannot resolve a relative link or cite itself honestly.
LOCAL_PAGES_ENTRY_KEYS = (
    "name",
    "listing_path",
    "origin_url",
    "detail_pattern",
    #: 034: a directory may host business pages under a sub-domain whose path is
    #: ``/``, so the URL-level pattern is the only way to recognise them.
    "detail_url_pattern",
    #: ``same_host`` (default, historical) or ``registrable_domain``.
    "detail_host_scope",
    #: The container the businesses are listed in. Keeps the page's furniture
    #: out of the harvest when a host rule is widened.
    "list_selector",
    #: Label set for the listing rows' own fields (listingfields.LIST_PRESETS).
    "list_preset",
    #: False turns the row reading off for one entry without touching the rest.
    "read_listing_fields",
    "field_selectors",
    "details",
    "details_dir",
    "observed_at",
    "max_detail_pages",
)

#: Providers that read a file and nothing else. Their records are forced to
#: ``acquired_via="import"`` when the envelope is written, so a mislabelled row
#: can never be published as "this run visited that page".
#:
#: 061: the Go scraper writes its own envelope and never states a method, so
#: the ingest path needs the same vocabulary the providers use. It lives here
#: because both sides already read this module, and two definitions of "which
#: providers can be live" would drift.
NEVER_LIVE_PROVIDERS = frozenset({"csv_list", "local_pages"})


#: Config field name -> key in the raw provider payload.
COLLECT_FIELDS: dict[str, str] = {
    "title": "title",
    "address": "address",
    "complete_address": "complete_address",
    "phone": "phone",
    "website": "web_site",
    "emails": "emails",
    "latitude": "latitude",
    "longitude": "longitude",
    #: The coordinate reference system the two above are in. Exported next to
    #: them because a bare latitude is ambiguous between WGS84, GCJ-02 and
    #: BD-09, which differ by hundreds of metres for the same place.
    "crs": "crs",
    "category": "category",
    "categories": "categories",
    "rating": "review_rating",
    "review_count": "review_count",
    "price_range": "price_range",
    "open_hours": "open_hours",
    "description": "description",
    "plus_code": "plus_code",
    "timezone": "timezone",
    "place_id": "place_id",
    "cid": "cid",
    "data_id": "data_id",
    "link": "link",
    #: A provider's own stable id. Stage E's non-Google sources fill this with
    #: whatever they use as a primary key (an OSM element ref, a CSV row id, a
    #: directory detail URL), so the entity keeps a traceable identity.
    "source_id": "source_id",
    "thumbnail": "thumbnail",
    "images": "images",
    "about": "about",
    "owner": "owner",
    "menu": "menu",
    "order_online": "order_online",
    "reservations": "reservations",
    "status": "status",
    "reviews_link": "reviews_link",
    "user_reviews": "user_reviews",
    "user_reviews_extended": "user_reviews_extended",
    "street_view_url": "street_view_url",
}

#: Fields that cost an extra fetch in the Go scraper. Requesting none of the
#: fields in a group means the matching flag must not be passed at all: the task
#: book requires that disabling a field "限制相关额外抓取，不能只隐藏导出列".
FIELD_FETCH_TRIGGERS: dict[str, tuple[str, ...]] = {
    "-email": ("emails",),
    "-extra-reviews": ("user_reviews_extended",),
}

#: Export columns that are not taken from the provider payload.
CONTEXT_COLUMNS = (
    "entity_id",
    "stable_key",
    "id_kind",
    "provider",
    #: Which sources describe this store. A row merged from two providers shows
    #: both, so a collapsed lead list still says what it is made of.
    "sources",
    "query",
    "source_url",
    "scraped_at",
    "first_seen_at",
    "last_seen_at",
)

#: Fields website enrichment can actually produce. Stage D extracts from
#: mailto/tel links and JSON-LD blocks, which carry exactly these; listing a
#: field here that the extractor cannot fill would be the "configured but does
#: nothing" failure the task book warns about.
ENRICHABLE_FIELDS = ("title", "emails", "phone", "address", "website")

#: Stage F score columns. Each listed dimension is exported as its value and
#: a ``_reason`` column; ``score_composite`` is the published weighted mean
#: (see collector/scoring.py for the rule and its version). The task book:
#: "若提供综合分，公开规则且版本化；不得把分数解释成准确率或销售价值" — so
#: the rule ships with the data, and no column is named "accuracy".
SCORE_COLUMNS = tuple(
    name
    for dimension in ("completeness", "validation", "evidence", "freshness", "conflict")
    for name in (f"score_{dimension}", f"score_{dimension}_reason")
) + ("score_composite", "score_scorer_version")

#: Every name ``export.fields`` may contain.
EXPORT_FIELDS = tuple(COLLECT_FIELDS) + CONTEXT_COLUMNS + SCORE_COLUMNS

#: Sections that only affect delivery, not collection. Changing them must not
#: invalidate a checkpoint (see :meth:`Config.fingerprint`).
#:
#: ``match`` is here for the same reason as ``export``: the matching criteria
#: decide how a *report* is read, not what is collected. Re-deciding an old run
#: against different criteria must not re-scrape it, and must not make it look
#: like a new run.
NON_COLLECTION_SECTIONS = ("export", "output", "match")

#: Named industry vocabularies a ``match.industry`` section may ask for. The
#: words live with the rule (see :mod:`collector.matching`), not with the run,
#: so a verdict can always be traced back to the vocabulary that produced it.
#:
#: 026 P2.6: the list is *read from* :data:`collector.matching.INDUSTRY_PROFILES`
#: rather than written out again here. A second hard-coded copy is exactly how a
#: matcher ends up pet-only: the registry gains an industry and the validator
#: keeps refusing it. One source of truth, so the two can never disagree.
INDUSTRY_PROFILE_NAMES = tuple(_INDUSTRY_PROFILES)

#: Scope a criterion may require. Empty accepts every pet-related business;
#: ``store_service`` asks only for offline stores and services, which is a
#: different question from "pet-related businesses" (task book 021 A).
INDUSTRY_SCOPES = ("", "store_service")


def _path(*parts: object) -> str:
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out or "<root>"


# --- primitive checks -------------------------------------------------------


def _mapping(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected a mapping, got {type(value).__name__}")
    return value


def _reject_unknown(section: dict, allowed: set[str], path: str) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) {unknown}; allowed: {sorted(allowed)}"
        )


def _str(value: Any, path: str, *, required: bool = True, default: str = "") -> str:
    """An optional string may be absent, null or blank; all three mean 'unset'."""
    if value is None or (not required and isinstance(value, str) and not value.strip()):
        if required:
            raise ConfigError(f"{path}: required, but missing")
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: expected a non-empty string, got {value!r}")
    return value.strip()


def _int(
    value: Any,
    path: str,
    *,
    minimum: int | None = None,
    default: int | None = None,
) -> int:
    if value is None:
        if default is None:
            raise ConfigError(f"{path}: required, but missing")
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}: expected an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{path}: must be >= {minimum}, got {value}")
    return value


def _float(
    value: Any,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    default: float | None = None,
) -> float:
    if value is None:
        if default is None:
            raise ConfigError(f"{path}: required, but missing")
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}: expected a number, got {value!r}")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"{path}: must be >= {minimum}, got {number}")
    if maximum is not None and number > maximum:
        raise ConfigError(f"{path}: must be <= {maximum}, got {number}")
    return number


def _bool(value: Any, path: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{path}: expected true or false, got {value!r}")
    return value


def _str_list(
    value: Any,
    path: str,
    *,
    allowed: tuple[str, ...] | None = None,
    required: bool = False,
    default: tuple[str, ...] = (),
) -> list[str]:
    if value is None:
        if required:
            raise ConfigError(f"{path}: required, but missing")
        return list(default)
    if not isinstance(value, list):
        raise ConfigError(f"{path}: expected a list, got {type(value).__name__}")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{_path(path, index)}: expected a non-empty string, got {item!r}")
        item = item.strip()
        if allowed is not None and item not in allowed:
            raise ConfigError(
                f"{_path(path, index)}: unknown value {item!r}; allowed: {sorted(allowed)}"
            )
        if item in out:
            raise ConfigError(f"{_path(path, index)}: duplicate value {item!r}")
        out.append(item)
    return out


# --- loading ----------------------------------------------------------------


def _import_yaml():
    try:
        import yaml  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
        raise ConfigError(
            "YAML configs need PyYAML, which is not installed. "
            "Run: .venv/Scripts/python.exe -m pip install -r requirements.txt "
            "(or use a .toml/.json config instead)"
        ) from exc
    return yaml


def load_config_file(path: str | pathlib.Path) -> dict:
    """Read a config file into a plain dict. Format is chosen by extension."""
    file = pathlib.Path(path)
    if not file.is_file():
        raise ConfigError(f"config file not found: {file}")

    suffix = file.suffix.lower()
    text = file.read_text(encoding="utf-8")

    try:
        if suffix in (".yaml", ".yml"):
            data = _import_yaml().safe_load(text)
        elif suffix == ".toml":
            import tomllib

            data = tomllib.loads(text)
        elif suffix == ".json":
            data = json.loads(text)
        else:
            raise ConfigError(
                f"unsupported config extension {suffix!r}: use .yaml, .toml or .json"
            )
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise ConfigError(f"{file}: could not parse: {exc}") from exc

    if data is None:
        raise ConfigError(f"{file}: file is empty")
    if not isinstance(data, dict):
        raise ConfigError(f"{file}: top level must be a mapping, got {type(data).__name__}")
    return data


# --- validation -------------------------------------------------------------


def _validate_location(raw: Any) -> dict:
    node = _mapping(raw, "location")
    _reject_unknown(
        node,
        {
            "query",
            "queries",
            "country",
            # ``country_input`` is derived (see below) but it is part of the
            # normalised form, so validate() must accept its own output when a
            # config is read back — re-export and resume both round-trip through
            # here. It is pruned from collection_view() so ``中国`` and ``CN``
            # still fingerprint identically.
            "country_input",
            "city",
            "bbox",
            "geo",
            "province",
            "district",
        },
        "location",
    )

    typed_country = _str(node.get("country"), "location.country")
    # A country may be written as a code (``DE``), a Chinese name (``中国``) or an
    # English name (``Germany``). The resolved code is what everything else uses;
    # an unrecognised value is refused with the text that was typed rather than
    # silently mapped to a country.
    country = regions.normalize_country(typed_country)
    if not country or len(country) != 2 or not country.isalpha():
        raise ConfigError(
            f"location.country: expected a two-letter ISO-3166 code or a known "
            f"country name (e.g. CN, 中国, Germany), got {typed_country!r}"
        )

    single = node.get("query")
    many = node.get("queries")
    if single is not None and many is not None:
        # The normalised form this function returns keeps both — ``query``
        # for the human, ``queries`` for the machinery — and it must be able
        # to round-trip through validate() as a loadable config. Accept the
        # pair exactly when they agree; a hand-written file that disagrees is
        # still the ambiguity it is refused as.
        derived = [str(item).strip() for item in many] if isinstance(many, list) else []
        if derived != [str(single).strip()]:
            raise ConfigError("location: provide either 'query' or 'queries', not both")
    if many is not None:
        queries = _str_list(many, "location.queries", required=True)
    elif single is not None:
        queries = [_str(single, "location.query")]
    else:
        raise ConfigError("location.query: required (or provide location.queries)")

    bbox = node.get("bbox")
    if bbox is None:
        parsed_bbox = None
    else:
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ConfigError(
                "location.bbox: expected [minLat, minLon, maxLat, maxLon], "
                f"got {bbox!r}"
            )
        values = [
            _float(item, _path("location.bbox", index), minimum=-180.0, maximum=180.0)
            for index, item in enumerate(bbox)
        ]
        min_lat, min_lon, max_lat, max_lon = values
        if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
            raise ConfigError(f"location.bbox: latitude out of range: {values!r}")
        if min_lat >= max_lat:
            raise ConfigError(
                f"location.bbox: minLat must be < maxLat, got {min_lat} >= {max_lat}"
            )
        if min_lon >= max_lon:
            raise ConfigError(
                f"location.bbox: minLon must be < maxLon, got {min_lon} >= {max_lon}"
            )
        parsed_bbox = [min_lat, min_lon, max_lat, max_lon]

    province = _str(node.get("province"), "location.province", required=False, default="")
    district = _str(node.get("district"), "location.district", required=False, default="")

    return {
        "query": queries[0],
        "queries": queries,
        "country": country,
        #: Kept so a config that said ``中国`` still reads back the way it was
        #: written; the code above is what the machinery uses.
        "country_input": typed_country,
        "city": _str(node.get("city"), "location.city", required=False, default=""),
        "province": province,
        "district": district,
        "bbox": parsed_bbox,
        "geo": _validate_geo(node.get("geo")),
    }


def _validate_geo(raw: Any) -> list[float] | None:
    """The search centre as [lat, lon].

    Fast mode refuses to start without it ("geo coordinates are required in fast
    mode" — measured), so it is required there rather than discovered at run
    time.
    """
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 2:
        raise ConfigError(f"location.geo: expected [lat, lon], got {raw!r}")
    lat = _float(raw[0], "location.geo[0]", minimum=-90.0, maximum=90.0)
    lon = _float(raw[1], "location.geo[1]", minimum=-180.0, maximum=180.0)
    return [lat, lon]


def _validate_budget(raw: Any, path: str) -> dict:
    node = _mapping(raw, path)
    _reject_unknown(node, {"max_requests", "max_seconds"}, path)
    return {
        "max_requests": _int(node.get("max_requests"), f"{path}.max_requests", minimum=0, default=0),
        "max_seconds": _int(node.get("max_seconds"), f"{path}.max_seconds", minimum=0, default=0),
    }


def _validate_collect(raw: Any) -> dict:
    node = _mapping(raw, "collect")
    _reject_unknown(
        node,
        {
            "mode",
            "fields",
            "max_depth",
            "zoom",
            "language",
            "radius_meters",
            "pages_per_site",
            "grid_cell_km",
        },
        "collect",
    )

    mode = _str(node.get("mode"), "collect.mode")
    if mode not in SUPPORTED_MODES:
        raise ConfigError(
            f"collect.mode: unknown mode {mode!r}; allowed: {list(SUPPORTED_MODES)}"
        )

    fields = _str_list(
        node.get("fields"),
        "collect.fields",
        allowed=tuple(COLLECT_FIELDS),
        required=True,
    )
    if "title" not in fields:
        raise ConfigError(
            "collect.fields: 'title' is required; a record without a name cannot be "
            "attributed to a business"
        )

    max_depth = _int(node.get("max_depth"), "collect.max_depth", minimum=1, default=1)
    if mode == "fast" and max_depth != 1:
        raise ConfigError(
            "collect.max_depth: fast mode has no paging support, so max_depth must be 1 "
            "(stage B verified -depth is a no-op in fast mode)"
        )

    grid_cell_km = _float(
        node.get("grid_cell_km"), "collect.grid_cell_km", minimum=0.05, maximum=50.0, default=1.0
    )

    return {
        "mode": mode,
        "fields": fields,
        "max_depth": max_depth,
        "zoom": _int(node.get("zoom"), "collect.zoom", minimum=0, default=15),
        "language": _str(node.get("language"), "collect.language", required=False, default="en"),
        "radius_meters": _int(
            node.get("radius_meters"), "collect.radius_meters", minimum=1, default=10000
        ),
        "pages_per_site": _int(
            node.get("pages_per_site"), "collect.pages_per_site", minimum=0, default=0
        ),
        "grid_cell_km": grid_cell_km,
    }


def _validate_enrich(raw: Any) -> dict:
    node = _mapping(raw, "enrich")
    _reject_unknown(node, {"enabled", "fields", "pages_per_site", "budget"}, "enrich")

    enabled = _bool(node.get("enabled"), "enrich.enabled", default=False)
    fields = _str_list(
        node.get("fields"),
        "enrich.fields",
        allowed=ENRICHABLE_FIELDS,
        default=(),
    )
    pages_per_site = _int(
        node.get("pages_per_site"), "enrich.pages_per_site", minimum=0, default=0
    )

    if not enabled and fields:
        raise ConfigError(
            "enrich.fields: listed fields but enrich.enabled is false; "
            "either enable enrichment or remove the field list"
        )
    if enabled and not fields:
        raise ConfigError(
            "enrich.fields: enrichment is enabled but no field is requested, so "
            f"it would visit every site and store nothing; allowed: {list(ENRICHABLE_FIELDS)}"
        )
    if enabled and pages_per_site < 1:
        raise ConfigError(
            "enrich.pages_per_site: must be >= 1 when enrich.enabled is true "
            "(the homepage counts as one page)"
        )

    return {
        "enabled": enabled,
        "fields": fields,
        "pages_per_site": pages_per_site,
        "budget": _validate_budget(node.get("budget", {}), "enrich.budget"),
    }


def _validate_export(raw: Any) -> dict:
    node = _mapping(raw, "export")
    _reject_unknown(node, {"fields", "formats", "directory", "languages"}, "export")
    return {
        "fields": _str_list(
            node.get("fields"), "export.fields", allowed=EXPORT_FIELDS, required=True
        ),
        "formats": _str_list(
            node.get("formats"),
            "export.formats",
            allowed=SUPPORTED_FORMATS,
            default=("csv", "jsonl"),
        ),
        "directory": _str(
            node.get("directory"), "export.directory", required=False, default="export"
        ),
        # Absent means the historical behaviour: the bilingual workbook and only
        # it, so re-exporting a run that predates languages produces exactly the
        # file set it produced before. The screen writes this key explicitly, so
        # a new run gets its region's languages.
        "languages": _str_list(
            node.get("languages"),
            "export.languages",
            allowed=SUPPORTED_LANGUAGES,
            default=DEFAULT_LANGUAGES,
        ),
    }


def _validate_match(raw: Any) -> dict:
    """``match``: the region and industry questions a report must answer.

    Both dimensions are optional, and an unconfigured dimension decides nothing
    rather than being invented: the export says "不适用" and never fills a
    condition in for the operator. A ``match`` section that carries no condition
    at all is refused, because it would look like matching while deciding
    nothing.

    The criteria do not change what is collected -- ``match`` is one of the
    :data:`NON_COLLECTION_SECTIONS` -- so a historical run keeps its fingerprint
    and its checkpoints no matter how the matching is written.
    """
    node = _mapping(raw, "match")
    _reject_unknown(node, {"region", "industry"}, "match")

    region_node = node.get("region") or {}
    region = _mapping(region_node, "match.region")
    _reject_unknown(region, {"province", "city", "district"}, "match.region")

    industry_node = node.get("industry") or {}
    industry = _mapping(industry_node, "match.industry")
    _reject_unknown(
        industry, {"profile", "scope", "keywords", "exclude_keywords"}, "match.industry"
    )

    profile = _str(industry.get("profile"), "match.industry.profile", required=False, default="")
    if profile and profile not in INDUSTRY_PROFILE_NAMES:
        raise ConfigError(
            f"match.industry.profile: unknown vocabulary {profile!r}; "
            f"allowed: {list(INDUSTRY_PROFILE_NAMES)}"
        )

    scope = _str(industry.get("scope"), "match.industry.scope", required=False, default="")
    if scope not in INDUSTRY_SCOPES:
        raise ConfigError(
            f"match.industry.scope: unknown scope {scope!r}; "
            f"allowed: {sorted(name for name in INDUSTRY_SCOPES if name)}"
        )

    keywords = _str_list(industry.get("keywords"), "match.industry.keywords", default=())
    # 026 P2.6: words that disqualify a record even when a keyword hits. Kept
    # separate from ``keywords`` because they make the opposite claim: the
    # caller is saying "this trade is out", not "forgot to name it".
    exclude_keywords = _str_list(
        industry.get("exclude_keywords"),
        "match.industry.exclude_keywords",
        default=(),
    )

    out = {
        "region": {
            "province": _str(region.get("province"), "match.region.province", required=False),
            "city": _str(region.get("city"), "match.region.city", required=False),
            "district": _str(region.get("district"), "match.region.district", required=False),
        },
        "industry": {
            "profile": profile,
            "scope": scope,
            "keywords": keywords,
            "exclude_keywords": exclude_keywords,
        },
    }
    if exclude_keywords and not (profile or keywords):
        # A region-only match with an exclusion list reads as "everything in
        # this city except these trades", but there is no positive industry
        # question, so the exclusions would never be evaluated. Refused rather
        # than ignored: a silently dropped filter is worse than a loud error.
        raise ConfigError(
            "match.industry.exclude_keywords: exclusions narrow an industry "
            "question and cannot stand alone. Add match.industry.profile or "
            "match.industry.keywords, or drop the exclusion list."
        )
    if not any(out["region"].values()) and not (profile or keywords):
        raise ConfigError(
            "match: no condition configured. Set match.region (province/city/district) "
            "and/or match.industry (profile or keywords), or remove the match section "
            "entirely -- an empty one decides nothing. "
            "match.industry.exclude_keywords narrows a question, it is not one: "
            "a criterion made only of exclusions would leave every record 待确认."
        )
    return out


def _validate_sitediscovery_pages(raw: Any) -> list[dict]:
    """The saved pages a ``page_links`` finder reads outbound links from.

    Each entry pairs the business it belongs to with the HTML file the operator
    saved. The page's own URL is optional: without it the local file name is
    used as the page label, and the finder then only has the file's own domain
    to compare against -- which for a saved directory page would be empty, so a
    page URL is all but required for the link to be judged at all.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(
            f"sitediscovery.page_links.pages: expected a list of "
            f"{{entity, path, page}} entries, got {type(raw).__name__}"
        )
    out: list[dict] = []
    for index, item in enumerate(raw):
        node = _mapping(item, f"sitediscovery.page_links.pages[{index}]")
        _reject_unknown(
            node, {"entity", "name", "stable_key", "path", "page", "url"},
            f"sitediscovery.page_links.pages[{index}]",
        )
        target = _str(
            node.get("entity") or node.get("stable_key") or node.get("name"),
            f"sitediscovery.page_links.pages[{index}].entity",
            required=True,
        )
        path = _str(
            node.get("path"),
            f"sitediscovery.page_links.pages[{index}].path",
            required=True,
        )
        page = _str(
            node.get("page") or node.get("url"),
            f"sitediscovery.page_links.pages[{index}].page",
            required=False,
            default="",
        )
        out.append({"entity": target, "path": path, "page": page})
    return out


def _validate_sitediscovery(raw: Any) -> dict:
    """``sitediscovery``: how a business's own site may be *found*.

    The stage D enricher only ever visits a site the listing already named. This
    section fills the blank before it: a confirmed candidate becomes an ordinary
    ``website`` fact, which is what makes the whole downstream chain -- enrich,
    merge, match, export -- work without a single change.

    Unlike ``match``, this section decides *what is collected*, so it takes part
    in the fingerprint. A section that carries nothing is pruned back by
    :meth:`Config.collection_view`, so a configuration written before this
    section existed still fingerprints to the value it had then.
    """
    from .sitediscovery import (  # local: no cycle
        DEFAULT_PAGES_PER_SITE,
        DEFAULT_RETRY_COOLDOWN_SECONDS,
        DEFAULT_RETRY_MAX_ATTEMPTS,
        FINDER_NAMES,
    )
    node = _mapping(raw, "sitediscovery")
    _reject_unknown(
        node,
        {"enabled", "sources", "budget", "pages_per_site", "reuse_within_days",
         "retry", "user_list", "page_links", "search", "wikidata"},
        "sitediscovery",
    )

    enabled = _bool(node.get("enabled"), "sitediscovery.enabled", default=False)
    sources = _str_list(
        node.get("sources"), "sitediscovery.sources", allowed=FINDER_NAMES, default=()
    )
    pages_per_site = _int(
        node.get("pages_per_site"),
        "sitediscovery.pages_per_site",
        minimum=1,
        default=DEFAULT_PAGES_PER_SITE,
    )

    user_list = _mapping(node.get("user_list") or {}, "sitediscovery.user_list")
    _reject_unknown(user_list, {"path"}, "sitediscovery.user_list")
    user_path = _str(
        user_list.get("path"), "sitediscovery.user_list.path", required=False, default=""
    )

    page_links = _mapping(node.get("page_links") or {}, "sitediscovery.page_links")
    _reject_unknown(page_links, {"pages", "manifest"}, "sitediscovery.page_links")
    pages = _validate_sitediscovery_pages(page_links.get("pages"))
    #: A CSV the screen writes, so the saved pages are one file to pick rather
    #: than a nested list to hand-edit; the collector reads it with the same
    #: reader the screen wrote it against.
    manifest = _str(
        page_links.get("manifest"), "sitediscovery.page_links.manifest",
        required=False, default="",
    )

    search = _mapping(node.get("search") or {}, "sitediscovery.search")
    _reject_unknown(search, {"endpoint", "api_key_env"}, "sitediscovery.search")
    endpoint = _str(
        search.get("endpoint"), "sitediscovery.search.endpoint", required=False, default=""
    )
    api_key_env = _str(
        search.get("api_key_env"), "sitediscovery.search.api_key_env", required=False, default=""
    )

    #: 034 第二批 2.3: the one account-free programmatic lookup that exists.
    #: Free, no key, no registration -- but it answers only for businesses
    #: Wikidata has a structured statement about, so it is opt-in and its
    #: coverage is reported per run rather than assumed.
    wikidata = _mapping(node.get("wikidata") or {}, "sitediscovery.wikidata")
    _reject_unknown(
        wikidata,
        {"enabled", "endpoint", "user_agent", "timeout_seconds", "names_per_query",
         "max_queries", "languages", "require_org_type", "org_classes"},
        "sitediscovery.wikidata",
    )
    wikidata_enabled = _bool(wikidata.get("enabled"), "sitediscovery.wikidata.enabled",
                             default=False)
    #: 042: a name is not an identity, so an item must be declared an
    #: organisation before its website is proposed. The classes are overridable
    #: because "what counts as a business here" is the operator's call, but the
    #: check being *on* is not.
    wikidata_require_org_type = _bool(
        wikidata.get("require_org_type"), "sitediscovery.wikidata.require_org_type",
        default=True,
    )
    wikidata_org_classes = _str_list(
        wikidata.get("org_classes"), "sitediscovery.wikidata.org_classes",
        default=(),
    )

    #: 042: what happens to a verdict whose fetch *failed*. A failure is not an
    #: answer, so it is retried -- but bounded per candidate and only after a
    #: cooldown, because a host that refused once should not be asked again at
    #: once by a resumed run. The node is only emitted when the operator actually
    #: wrote it: leaving it out must not change the fingerprint of a
    #: configuration that predates it.
    retry_given = node.get("retry")
    retry = _mapping(retry_given or {}, "sitediscovery.retry")
    _reject_unknown(retry, {"max_attempts", "cooldown_seconds"}, "sitediscovery.retry")
    retry_max_attempts = _int(
        retry.get("max_attempts"), "sitediscovery.retry.max_attempts",
        minimum=1, default=DEFAULT_RETRY_MAX_ATTEMPTS,
    )
    retry_cooldown = _int(
        retry.get("cooldown_seconds"), "sitediscovery.retry.cooldown_seconds",
        minimum=0, default=DEFAULT_RETRY_COOLDOWN_SECONDS,
    )

    configured = {
        "user_list": bool(user_path),
        "page_links": bool(pages or manifest),
        "search": bool(endpoint and api_key_env),
        "wikidata": wikidata_enabled,
    }

    if not enabled:
        stated = [name for name, present in configured.items() if present]
        if sources or stated:
            raise ConfigError(
                "sitediscovery: settings given but sitediscovery.enabled is false "
                f"(sources={list(sources)}, configured={stated}); either enable it "
                "or remove the settings"
            )
    else:
        if not sources:
            raise ConfigError(
                "sitediscovery.sources: discovery is enabled but names no source, "
                f"so it would look for candidates nowhere; allowed: {list(FINDER_NAMES)}"
            )
        # ``search`` is allowed to be listed while unconfigured: its absence is
        # reported instead of pretending a search happened. But a run that can
        # *only* search would silently find nothing, which is the failure the
        # task book forbids, so that combination is refused up front.
        if not any(configured[name] for name in sources):
            raise ConfigError(
                "sitediscovery: discovery is enabled and names sources "
                f"{list(sources)}, but none of them is configured "
                f"({configured}); set sitediscovery.user_list.path or "
                "sitediscovery.page_links.pages, or drop the section"
            )

    out = {
        "enabled": enabled,
        "sources": sources,
        "pages_per_site": pages_per_site,
        #: 034: 0 = 每次重问（034 之前的默认行为）；>0 = 在这个天数内复用「已确认」判定。
        "reuse_within_days": _int(
            node.get("reuse_within_days"), "sitediscovery.reuse_within_days",
            minimum=0, default=0,
        ),
        "budget": _validate_budget(node.get("budget", {}), "sitediscovery.budget"),
        "user_list": {"path": user_path},
        "page_links": {"pages": pages, "manifest": manifest},
        "search": {"endpoint": endpoint, "api_key_env": api_key_env},
        "wikidata": {
            **wikidata,
            "enabled": wikidata_enabled,
            "require_org_type": wikidata_require_org_type,
            "org_classes": list(wikidata_org_classes),
        },
    }
    if retry_given is not None:
        out["retry"] = {
            "max_attempts": retry_max_attempts,
            "cooldown_seconds": retry_cooldown,
        }
    return out


def _validate_runtime(raw: Any) -> dict:
    node = _mapping(raw, "runtime")
    _reject_unknown(
        node,
        {
            "concurrency",
            "timeout_seconds",
            "inactivity_seconds",
            "proxy",
            "go_binary",
            "budget",
        },
        "runtime",
    )
    return {
        "concurrency": _int(node.get("concurrency"), "runtime.concurrency", minimum=1, default=1),
        "timeout_seconds": _int(
            node.get("timeout_seconds"), "runtime.timeout_seconds", minimum=1, default=600
        ),
        "inactivity_seconds": _int(
            node.get("inactivity_seconds"), "runtime.inactivity_seconds", minimum=1, default=120
        ),
        "proxy": _str(node.get("proxy"), "runtime.proxy", required=False, default=""),
        "go_binary": _str(node.get("go_binary"), "runtime.go_binary", required=False, default=""),
        "budget": _validate_budget(node.get("budget", {}), "runtime.budget"),
    }


def _validate_validation(raw: Any) -> dict:
    node = _mapping(raw, "validation")
    _reject_unknown(node, {"rules"}, "validation")
    rules_node = _mapping(node.get("rules", {}), "validation.rules")
    allowed = {"require_identity", "require_country_phone", "email_syntax", "phone_syntax"}
    _reject_unknown(rules_node, allowed, "validation.rules")
    return {
        "rules": {
            "require_identity": _bool(
                rules_node.get("require_identity"), "validation.rules.require_identity", default=True
            ),
            "require_country_phone": _bool(
                rules_node.get("require_country_phone"),
                "validation.rules.require_country_phone",
                default=False,
            ),
            "email_syntax": _bool(
                rules_node.get("email_syntax"), "validation.rules.email_syntax", default=True
            ),
            "phone_syntax": _bool(
                rules_node.get("phone_syntax"), "validation.rules.phone_syntax", default=True
            ),
        }
    }


def _validate_sources_map(raw: Any) -> dict:
    """``sources_map``: per-provider settings, one mapping per source.

    Structure is checked here (known source, mapping value, allowed keys) while
    the *meaning* of a value belongs to the provider that reads it. That split
    keeps an unknown key an error without this module having to know what
    ``radius_meters`` means to Overpass.
    """
    node = _mapping(raw, "sources_map")
    _reject_unknown(node, set(PROVIDER_SETTINGS_KEYS), "sources_map")

    out: dict[str, dict] = {}
    for name, value in node.items():
        settings = _mapping(value, _path("sources_map", name))
        allowed = set(PROVIDER_SETTINGS_KEYS.get(name, ()))
        _reject_unknown(settings, allowed, _path("sources_map", name))
        out[name] = dict(settings)

    if "csv_list" in out:
        out["csv_list"] = _validate_csv_list(out["csv_list"])
    if "directory" in out:
        out["directory"] = _validate_directory(out["directory"])
    if "local_pages" in out:
        out["local_pages"] = _validate_local_pages(out["local_pages"])
    for name, settings in list(out.items()):
        if "network" in settings:
            settings["network"] = _validate_network(
                settings["network"], _path("sources_map", name, "network")
            )
    return out


def _validate_network(raw: Any, path: str) -> dict:
    """``sources_map.<name>.network``: how this one source reaches the network.

    The precedence is explicit rather than emergent:

    * ``inherit`` (the default) — use ``runtime.proxy`` when set, and otherwise
      let the system environment decide, exactly as every run did before this
      key existed;
    * ``direct`` — no proxy at all, **and** the environment is ignored, so a
      machine-wide ``HTTP_PROXY`` cannot quietly route a source the operator
      asked to go direct;
    * ``proxy`` — one proxy for this source only, and again the environment is
      ignored so the choice is unambiguous.

    ``runtime.proxy`` itself is untouched, so an existing overseas configuration
    keeps working byte for byte.
    """
    node = _mapping(raw, path)
    _reject_unknown(node, {"mode", "proxy"}, path)

    mode = _str(node.get("mode"), f"{path}.mode", required=False, default="inherit")
    if mode not in NETWORK_MODES:
        raise ConfigError(
            f"{path}.mode: unknown mode {mode!r}; allowed: {list(NETWORK_MODES)}"
        )
    proxy = _str(node.get("proxy"), f"{path}.proxy", required=False, default="")

    if mode == "proxy" and not proxy:
        raise ConfigError(
            f"{path}.proxy: required when mode is 'proxy' "
            "(e.g. http://127.0.0.1:7890), otherwise the source would silently "
            "inherit the global proxy instead"
        )
    if mode != "proxy" and proxy:
        raise ConfigError(
            f"{path}.proxy: only allowed when mode is 'proxy', but mode is {mode!r}; "
            "set mode: proxy to use it"
        )
    if proxy and "://" not in proxy:
        raise ConfigError(
            f"{path}.proxy: expected a scheme-qualified URL (http://host:port), got {proxy!r}"
        )
    return {"mode": mode, "proxy": proxy}


def _validate_csv_list(node: dict) -> dict:
    path = _path("sources_map", "csv_list")
    columns = node.get("columns")
    if columns is not None:
        mapping = _mapping(columns, f"{path}.columns")
        from .sources.csvlist import FIELDS as CSV_FIELDS

        for target in mapping.values():
            if target not in CSV_FIELDS:
                raise ConfigError(
                    f"{path}.columns: unknown target {target!r}; "
                    f"allowed: {sorted(CSV_FIELDS)}"
                )
    provenance = node.get("provenance")
    if provenance is not None:
        node["provenance"] = _validate_csv_provenance(provenance, path)
    return node


#: Keys the ``provenance`` block accepts. ``columns`` is the per-row mapping and
#: is checked separately; the rest are the file-wide statements.
CSV_PROVENANCE_KEYS = (
    "acquired_via",
    "origin",
    "source_page_url",
    "snapshot_id",
    "observed_at",
    "columns",
)


def _validate_csv_provenance(raw: Any, path: str) -> dict:
    """``sources_map.csv_list.provenance``: where the rows came from.

    Round 026 P0: an imported file has to be able to *state* its provenance, so
    evidence cites the page the data was read from instead of whatever the row's
    identifier happens to look like. Structure is checked here; the provider
    reads the meaning.
    """
    node = _mapping(raw, f"{path}.provenance")
    _reject_unknown(node, set(CSV_PROVENANCE_KEYS), f"{path}.provenance")

    from .sources.base import ACQUIRED_VIA, PROVENANCE_KEYS

    acquired = node.get("acquired_via")
    if acquired is not None:
        value = _str(acquired, f"{path}.provenance.acquired_via")
        if value not in ACQUIRED_VIA:
            raise ConfigError(
                f"{path}.provenance.acquired_via: {value!r} is not one of "
                f"{sorted(ACQUIRED_VIA)}"
            )

    columns = node.get("columns")
    if columns is not None:
        mapping = _mapping(columns, f"{path}.provenance.columns")
        # ``detail_fetched`` is a boolean the provider decides, never a column.
        allowed = {key for key in PROVENANCE_KEYS if key != "detail_fetched"}
        for target in mapping.values():
            if target not in allowed:
                raise ConfigError(
                    f"{path}.provenance.columns: unknown target {target!r}; "
                    f"allowed: {sorted(allowed)}"
                )
    return node


def _validate_directory(node: dict) -> dict:
    path = _path("sources_map", "directory")
    entries = node.get("entries")
    if entries is None:
        raise ConfigError(
            f"{path}.entries: required; a directory source needs at least one "
            "entry with a start_url"
        )
    if not isinstance(entries, list) or not entries:
        raise ConfigError(
            f"{path}.entries: expected a non-empty list of entries, got {entries!r}"
        )

    for index, entry in enumerate(entries):
        entry_path = f"{path}.entries[{index}]"
        mapping = _mapping(entry, entry_path)
        _reject_unknown(mapping, set(DIRECTORY_ENTRY_KEYS), entry_path)
        start_url = _str(mapping.get("start_url"), f"{entry_path}.start_url")
        if "://" not in start_url:
            raise ConfigError(
                f"{entry_path}.start_url: expected an absolute http(s) URL, got {start_url!r}"
            )
        selectors = mapping.get("field_selectors")
        if selectors is not None:
            selector_map = _mapping(selectors, f"{entry_path}.field_selectors")
            for key, value in selector_map.items():
                if value is None or str(value).strip() == "":
                    continue
                # Compiled here as well as by the provider, so a selector that
                # cannot be evaluated is a config error rather than a run-time
                # surprise that looks like "no businesses found".
                from .selector import SelectorError, compile_selector

                try:
                    compile_selector(str(value))
                except SelectorError as exc:
                    raise ConfigError(
                        f"{entry_path}.field_selectors.{key}: {exc}"
                    ) from exc
        pagination = mapping.get("pagination")
        if pagination is not None:
            from .selector import SelectorError, compile_selector

            selector = (
                pagination.strip()
                if isinstance(pagination, str)
                else str((_mapping(pagination, f"{entry_path}.pagination") or {}).get("selector") or "").strip()
            )
            if selector:
                try:
                    compile_selector(selector)
                except SelectorError as exc:
                    raise ConfigError(f"{entry_path}.pagination: {exc}") from exc
    return node


def _validate_local_pages(node: dict) -> dict:
    """``sources_map.local_pages``: saved pages the operator points at.

    The file *existing* is not checked here. A config is a description that may
    be written on one machine and run on another, and a saved page's path is a
    local fact, not part of the run's meaning — the same reason ``csv_list``
    does not check its path either. What *is* checked is that the description is
    complete: a listing path with no origin URL cannot resolve a relative link
    or say where the data came from, so it is refused at validation time rather
    than producing an empty run later.
    """
    path = _path("sources_map", "local_pages")
    entries = node.get("entries")
    if entries is None:
        raise ConfigError(
            f"{path}.entries: required; local_pages needs at least one entry with "
            "a listing_path and its origin_url"
        )
    if not isinstance(entries, list) or not entries:
        raise ConfigError(
            f"{path}.entries: expected a non-empty list of entries, got {entries!r}"
        )

    for index, entry in enumerate(entries):
        entry_path = f"{path}.entries[{index}]"
        mapping = _mapping(entry, entry_path)
        _reject_unknown(mapping, set(LOCAL_PAGES_ENTRY_KEYS), entry_path)
        listing = _str(
            mapping.get("listing_path"), f"{entry_path}.listing_path", required=True
        )
        if not listing.strip():
            raise ConfigError(f"{entry_path}.listing_path: must not be blank")
        origin = _str(
            mapping.get("origin_url"), f"{entry_path}.origin_url", required=True
        )
        if "://" not in origin:
            raise ConfigError(
                f"{entry_path}.origin_url: expected the absolute http(s) URL the page "
                f"was saved from, got {origin!r}"
            )
        selectors = mapping.get("field_selectors")
        if selectors is not None:
            selector_map = _mapping(selectors, f"{entry_path}.field_selectors")
            for key, value in selector_map.items():
                if value is None or str(value).strip() == "":
                    continue
                from .selector import SelectorError, compile_selector

                try:
                    compile_selector(str(value))
                except SelectorError as exc:
                    raise ConfigError(f"{entry_path}.field_selectors.{key}: {exc}") from exc
        pattern = mapping.get("detail_pattern")
        if pattern is not None and str(pattern).strip():
            import re as _re

            try:
                _re.compile(str(pattern))
            except _re.error as exc:
                raise ConfigError(f"{entry_path}.detail_pattern: {exc}") from exc
        details = mapping.get("details")
        if details is not None:
            if not isinstance(details, list):
                raise ConfigError(
                    f"{entry_path}.details: expected a list of {{url, path}} entries, "
                    f"got {details!r}"
                )
            for detail_index, detail in enumerate(details):
                detail_path = f"{entry_path}.details[{detail_index}]"
                detail_map = _mapping(detail, detail_path)
                _reject_unknown(detail_map, {"url", "path"}, detail_path)
                _str(detail_map.get("url"), f"{detail_path}.url", required=True)
                _str(detail_map.get("path"), f"{detail_path}.path", required=True)
        details_dir = mapping.get("details_dir")
        if details_dir is not None:
            _str(details_dir, f"{entry_path}.details_dir", required=False)
        observed_at = mapping.get("observed_at")
        if observed_at is not None:
            _str(observed_at, f"{entry_path}.observed_at", required=False)
        if mapping.get("max_detail_pages") is not None:
            _int(mapping.get("max_detail_pages"), f"{entry_path}.max_detail_pages")
    return node


def _validate_output(raw: Any) -> dict:
    node = _mapping(raw, "output")
    _reject_unknown(node, {"directory", "database", "raw_directory"}, "output")
    return {
        "directory": _str(node.get("directory"), "output.directory", required=False, default="runs"),
        "database": _str(
            node.get("database"), "output.database", required=False, default="collector.sqlite"
        ),
        "raw_directory": _str(
            node.get("raw_directory"), "output.raw_directory", required=False, default="raw"
        ),
    }


TOP_LEVEL_KEYS = {
    "name",
    "industry",
    "location",
    "target_count",
    "sources",
    "sources_map",
    "collect",
    "enrich",
    "export",
    "match",
    "sitediscovery",
    "runtime",
    "validation",
    "output",
}


def validate(raw: dict, source: str = "<memory>") -> dict:
    """Validate a raw config dict and return the normalised form.

    Raises :class:`ConfigError` naming the first offending path.
    """
    root = _mapping(raw, "<root>")
    # ``source_path`` is a runtime stamp this very function adds to its
    # output; the normalised form must therefore be loadable again (stage F
    # writes configs back out), while a hand-written file gains nothing from
    # setting it.
    _reject_unknown(root, set(TOP_LEVEL_KEYS) | {"source_path"}, "<root>")

    # ``industry`` describes the *search scope* of a run. A batch whose records
    # come from a named seed list rather than an industry search has no
    # industry, and 045 is explicit that such a batch must not be given a
    # fabricated one ("用户不提供行业的批次不要杜撰行业"). Blank is therefore a
    # legal value and stays blank all the way to the export; a config that does
    # describe a search still states its industry as before.
    industry = _str(root.get("industry"), "industry", required=False, default="")
    location = _validate_location(root.get("location"))

    target_count = _int(root.get("target_count"), "target_count", minimum=1)

    sources = _str_list(
        root.get("sources"), "sources", allowed=SUPPORTED_SOURCES, required=True
    )
    # A provider's own settings only make sense when that provider is listed.
    sources_map = _validate_sources_map(root.get("sources_map", {}))
    orphans = sorted(set(sources_map) - set(sources))
    if orphans:
        raise ConfigError(
            f"sources_map: settings for {orphans}, but sources is {sources}; "
            "either add the source to sources or remove its settings"
        )

    collect = _validate_collect(root.get("collect"))
    enrich = _validate_enrich(root.get("enrich", {}))
    export = _validate_export(root.get("export"))
    match = _validate_match(root["match"]) if "match" in root and root["match"] is not None else None
    # An absent (or explicitly null) section is the historical configuration:
    # discovery was not a thing, so nothing is looked for.
    sitediscovery = _validate_sitediscovery(
        root["sitediscovery"] if root.get("sitediscovery") is not None else {}
    )
    runtime = _validate_runtime(root.get("runtime", {}))
    validation = _validate_validation(root.get("validation", {}))
    output = _validate_output(root.get("output", {}))

    if collect["mode"] == "fast" and location["bbox"] is not None:
        raise ConfigError(
            "location.bbox: fast mode cannot be combined with a bbox "
            "(the Go scraper rejects -fast-mode with -grid-bbox)"
        )

    if collect["mode"] == "fast" and location["geo"] is None:
        raise ConfigError(
            "location.geo: fast mode needs a search centre, e.g. geo: [52.52, 13.405] "
            "(the Go scraper refuses to start fast mode without -geo)"
        )

    return {
        "name": _str(root.get("name"), "name", required=False, default=""),
        "source_path": source,
        "industry": industry,
        "location": location,
        "target_count": target_count,
        "sources": sources,
        "sources_map": sources_map,
        "collect": collect,
        "enrich": enrich,
        "export": export,
        #: ``None`` when the config carries no ``match`` section: nothing is
        #: matched, and an old run re-exports to the workbook it always had.
        "match": match,
        #: Always present so the screen can render the switch; pruned back to
        #: nothing by :meth:`Config.collection_view` when it is switched off.
        "sitediscovery": sitediscovery,
        "runtime": runtime,
        "validation": validation,
        "output": output,
    }


class Config:
    """A validated configuration.

    ``data`` is the normalised dict from :func:`validate`. It is kept as a plain
    dict so it can be stored verbatim in the database and compared across runs.
    """

    def __init__(self, data: dict, source: str = "<memory>"):
        self.data = data
        self.source = source

    # -- convenience accessors ------------------------------------------------

    @property
    def industry(self) -> str:
        return self.data["industry"]

    @property
    def query(self) -> str:
        return self.data["location"]["query"]

    @property
    def queries(self) -> list[str]:
        """Every search text for this run, in order."""
        return list(self.data["location"]["queries"])

    @property
    def country(self) -> str:
        return self.data["location"]["country"]

    @property
    def province(self) -> str:
        return self.data["location"].get("province", "")

    @property
    def district(self) -> str:
        return self.data["location"].get("district", "")

    @property
    def city(self) -> str:
        return self.data["location"].get("city", "")

    @property
    def region_label(self) -> str:
        """The region in the reader's words (中国 / 德国 / …)."""
        return regions.region_label(self.country)

    def region_facts(self) -> dict:
        """Region, division-data provenance and city-centre caveat, for reports."""
        return regions.region_facts(self.country)

    def region_warnings(self) -> list[str]:
        """Region/source combinations the user must see, not discover later."""
        return regions.source_warnings(self.country, self.sources_list)

    @property
    def bbox(self) -> list[float] | None:
        return self.data["location"]["bbox"]

    @property
    def geo(self) -> list[float] | None:
        return self.data["location"]["geo"]

    @property
    def target_count(self) -> int:
        return self.data["target_count"]

    @property
    def sources_list(self) -> list[str]:
        """The sources this run uses, in configuration order."""
        return list(self.data["sources"])

    @property
    def sources_map(self) -> dict:
        """Per-provider settings, keyed by source name. Never None."""
        return dict(self.data.get("sources_map") or {})

    @property
    def mode(self) -> str:
        return self.data["collect"]["mode"]

    @property
    def collect_fields(self) -> list[str]:
        return list(self.data["collect"]["fields"])

    @property
    def export_fields(self) -> list[str]:
        return list(self.data["export"]["fields"])

    @property
    def formats(self) -> list[str]:
        return list(self.data["export"]["formats"])

    @property
    def languages(self) -> list[str]:
        """Reader-facing export languages, in the order they were configured."""
        return list(self.data["export"].get("languages") or DEFAULT_LANGUAGES)

    # -- matching (task book 021) ----------------------------------------------

    @property
    def match_data(self) -> dict | None:
        """The raw ``match`` section, or None when the config carries none."""
        node = self.data.get("match")
        return node if isinstance(node, dict) else None

    # -- per-source network routing (stage 019 A4) ---------------------------

    def source_network(self, name: str) -> dict:
        """``{"mode", "proxy"}`` for one source. Defaults to ``inherit``."""
        settings = (self.data.get("sources_map") or {}).get(name) or {}
        node = settings.get("network") if isinstance(settings, dict) else None
        if not isinstance(node, dict):
            return {"mode": "inherit", "proxy": ""}
        return {
            "mode": str(node.get("mode") or "inherit"),
            "proxy": str(node.get("proxy") or ""),
        }

    def source_proxy(self, name: str) -> str:
        """The proxy this source must use; '' means "no proxy"."""
        network = self.source_network(name)
        if network["mode"] == "direct":
            return ""
        if network["mode"] == "proxy":
            return network["proxy"]
        return self.proxy

    def source_trust_env_proxy(self, name: str) -> bool:
        """Whether the system environment may supply a proxy for this source.

        Only ``inherit`` leaves that decision to the environment. ``direct``
        and ``proxy`` both pin it, which is what makes "明确直连" mean what it
        says even on a machine with ``HTTP_PROXY`` set.
        """
        return self.source_network(name)["mode"] == "inherit"

    def source_networks(self) -> dict[str, dict]:
        """Every configured source's routing, for the run context."""
        return {name: self.source_network(name) for name in self.sources_list}

    # -- candidate-site discovery (round 026 P1) ------------------------------

    @property
    def sitediscovery(self) -> dict:
        """The normalised section. Always present; empty when unconfigured."""
        return dict(self.data.get("sitediscovery") or {})

    @property
    def sitediscovery_enabled(self) -> bool:
        return bool(self.sitediscovery.get("enabled"))

    @property
    def sitediscovery_sources(self) -> list[str]:
        return list(self.sitediscovery.get("sources") or [])

    @property
    def sitediscovery_pages_per_site(self) -> int:
        return int(self.sitediscovery.get("pages_per_site") or 1)

    @property
    def sitediscovery_request_budget(self) -> int:
        return int((self.sitediscovery.get("budget") or {}).get("max_requests") or 0)

    @property
    def sitediscovery_reuse_days(self) -> int:
        """How long a *confirmed* site verdict may be reused without a request.

        0 (the default, and every config written before round 034) means a new
        run re-asks every time. That is the safe direction: a site that changed
        has to be able to show it. Turning it on is the operator's call, and the
        window is theirs to name.
        """
        return int(self.sitediscovery.get("reuse_within_days") or 0)

    @property
    def sitediscovery_retry_max_attempts(self) -> int:
        """How many times one candidate may be judged before a failure stands.

        Counts the first attempt, so the default of 2 means one retry. Bounded
        because "it failed" must not become "ask again forever", and per
        candidate because one dead host must not consume another's budget.
        """
        from .sitediscovery import DEFAULT_RETRY_MAX_ATTEMPTS  # local: no cycle

        return int(
            (self.sitediscovery.get("retry") or {}).get("max_attempts")
            or DEFAULT_RETRY_MAX_ATTEMPTS
        )

    @property
    def sitediscovery_retry_cooldown_seconds(self) -> int:
        """How long a failed candidate waits before it may be asked again."""
        from .sitediscovery import DEFAULT_RETRY_COOLDOWN_SECONDS  # local: no cycle

        value = (self.sitediscovery.get("retry") or {}).get("cooldown_seconds")
        return DEFAULT_RETRY_COOLDOWN_SECONDS if value is None else int(value)

    @property
    def sitediscovery_seconds_budget(self) -> int:
        return int((self.sitediscovery.get("budget") or {}).get("max_seconds") or 0)

    # -- enrichment (stage D) -------------------------------------------------

    @property
    def enrich_enabled(self) -> bool:
        return bool(self.data["enrich"]["enabled"])
    @property
    def enrich_fields(self) -> list[str]:
        return list(self.data["enrich"]["fields"])

    @property
    def enrich_pages_per_site(self) -> int:
        return int(self.data["enrich"]["pages_per_site"])

    @property
    def enrich_request_budget(self) -> int:
        """Fetch cap for enrichment; falls back to the run-wide budget."""
        own = int(self.data["enrich"]["budget"]["max_requests"])
        return own if own else int(self.data["runtime"]["budget"]["max_requests"])

    @property
    def enrich_time_budget_seconds(self) -> int:
        """Wall-clock cap for enrichment; falls back to the run-wide budget."""
        own = int(self.data["enrich"]["budget"]["max_seconds"])
        return own if own else int(self.data["runtime"]["budget"]["max_seconds"])

    @property
    def proxy(self) -> str:
        return self.data["runtime"]["proxy"]

    @property
    def go_binary(self) -> str:
        return self.data["runtime"]["go_binary"]

    @property
    def concurrency(self) -> int:
        return self.data["runtime"]["concurrency"]

    @property
    def timeout_seconds(self) -> int:
        return self.data["runtime"]["timeout_seconds"]

    @property
    def inactivity_seconds(self) -> int:
        return self.data["runtime"]["inactivity_seconds"]

    @property
    def request_budget(self) -> int:
        """Maximum provider tasks for the whole run; 0 means no extra limit."""
        return int(self.data["runtime"]["budget"]["max_requests"])

    @property
    def time_budget_seconds(self) -> int:
        """Maximum wall clock for discovery; 0 means no extra limit."""
        return int(self.data["runtime"]["budget"]["max_seconds"])

    @property
    def output_directory(self) -> str:
        return self.data["output"]["directory"]

    @property
    def database_name(self) -> str:
        return self.data["output"]["database"]

    @property
    def raw_directory_name(self) -> str:
        return self.data["output"]["raw_directory"]

    @property
    def export_directory_name(self) -> str:
        return self.data["export"]["directory"]

    def rules(self) -> dict:
        return dict(self.data["validation"]["rules"])

    # -- identity -------------------------------------------------------------

    #: Keys inside ``location`` that describe *how the region was typed* rather
    #: than what is collected. ``country_input`` is the text the operator wrote
    #: (``中国``); the resolved ``country`` code is what the run actually uses,
    #: so ``中国`` and ``CN`` must not fingerprint differently.
    _LOCATION_DISPLAY_ONLY = ("country_input",)

    def collection_view(self) -> dict:
        """The subset of the config that changes what is collected.

        ``export`` and ``output`` are excluded: changing an export column list or
        an output directory does not invalidate already-collected evidence, so it
        must not force a re-scrape.

        Stage 019 added optional keys to ``location`` (``province``,
        ``district``) and to every source (``network``). Both are pruned back to
        their absent form when they carry no information, so a configuration
        written before those keys existed fingerprints to **exactly** the same
        value it did then. That is what keeps a historical run resumable and
        re-exportable instead of silently becoming a "new" run.
        """
        view = {
            key: value
            for key, value in self.data.items()
            if key not in NON_COLLECTION_SECTIONS and key != "source_path"
        }

        location = dict(view.get("location") or {})
        for key in self._LOCATION_DISPLAY_ONLY:
            location.pop(key, None)
        for key in ("province", "district"):
            if not location.get(key):
                location.pop(key, None)
        view["location"] = location

        sources_map: dict[str, dict] = {}
        for name, settings in (view.get("sources_map") or {}).items():
            entry = dict(settings) if isinstance(settings, dict) else {}
            network = entry.get("network")
            if isinstance(network, dict) and (
                str(network.get("mode") or "inherit") == "inherit"
                and not network.get("proxy")
            ):
                entry.pop("network", None)
            if entry:
                sources_map[name] = entry
        view["sources_map"] = sources_map

        # A switched-off candidate-site section changes nothing about what is
        # collected, so it is pruned back to its absent form: a configuration
        # written before the section existed keeps the fingerprint it had, and
        # switching discovery off does not look like a new run. Validation
        # already refuses to carry settings while the switch is off, so
        # "disabled" really does mean "contributes nothing".
        sitediscovery = view.get("sitediscovery")
        if isinstance(sitediscovery, dict) and not sitediscovery.get("enabled"):
            view.pop("sitediscovery", None)

        return view

    def fingerprint(self) -> str:
        """Stable sha256 over the collection-relevant config."""
        payload = json.dumps(
            self.collection_view(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def short_fingerprint(self) -> str:
        return self.fingerprint()[:12]

    def canonical_json(self) -> str:
        return json.dumps(self.data, sort_keys=True, ensure_ascii=False, indent=2)

    def fetch_flags(self) -> list[str]:
        """Go flags implied by the requested fields.

        A field that costs an extra fetch contributes its flag only when it is
        actually requested, so disabling it removes the fetch instead of merely
        hiding the column.
        """
        requested = set(self.collect_fields)
        flags: list[str] = []
        for flag, trigger_fields in FIELD_FETCH_TRIGGERS.items():
            if requested.intersection(trigger_fields):
                flags.append(flag)
        return flags


def load(path: str | pathlib.Path) -> Config:
    """Load and validate a config file."""
    raw = load_config_file(path)
    return Config(validate(raw, source=str(path)), source=str(path))
