"""Extract contact data from a business's own web pages.

Task book 004, stage D:

    "首页 + 有预算的同域 Contact/About/Locations 页面；支持 mailto/tel 和
     JSON-LD；每字段保留真实来源页。"

Two things this module deliberately does *not* do:

* it never invents a value — every field carries the page it came from and a
  snippet of the evidence;
* it never decides whether a value is *good*. Validity is judged later, in
  :mod:`collector.emailcheck`, :mod:`collector.phonecheck` and
  :mod:`collector.addresscheck`. Keeping extraction and judgement apart is what
  lets the report say "this value came from the JSON-LD block of /kontakt, and
  its MX lookup timed out" instead of collapsing both into one opaque verdict.

For a ``mailto:``/``tel:`` anchor the two things the element states are kept
apart: ``href_value`` is what the link targets, ``display_text`` is what the
page shows. Whether the two *disagree* is a judgement about normalised values,
so it belongs downstream; extraction records both so the question can be asked
at all. Collapsing them into one string is what lets a page that links one
number and prints another look like a healthy contact.

Sources, in the order they are trusted
--------------------------------------
``jsonld``   ``<script type="application/ld+json">`` — structured data the site
             publishes about itself. Best attribution: it is tied to an
             organisation, not to a stray page element.
``mailto``   ``<a href="mailto:…">``
``tel``      ``<a href="tel:…">``

Plain page text is *not* scanned for addresses. Stage B measured what that
costs: the existing Go extractor returns every address on the page, including
those of the data-protection authority and of job portals linked from the
footer. Requiring a ``mailto:``/``tel:`` link or a JSON-LD block is what keeps
an extracted value attributable to the business.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterator

from .webfetch import registrable_domain

#: Page kinds worth spending the per-site budget on, most useful first.
LINK_CONTACT = "contact"
LINK_LOCATIONS = "locations"
LINK_ABOUT = "about"
LINK_IMPRINT = "imprint"
#: A link that leaves the page's own site. Not a page to fetch next -- stage D
#: stays inside the site it was given -- but a business's own site is advertised
#: on exactly such a link, so candidate-site discovery reads these.
LINK_OUTBOUND = "outbound"

#: Path/anchor fragments that identify a page kind. Matched case-insensitively
#: against the URL path and the anchor text, so ``/Kontakt`` and
#: ``/ueber-uns`` are both found.
LINK_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        LINK_CONTACT,
        (
            "kontakt", "contact", "contacto", "contatti", "contactez", "contact-us",
            "contactus", "contact-us", "contattaci", "contacteer", "yhteystiedot",
            "ansprechpartner", "kundenservice",
        ),
    ),
    (
        LINK_LOCATIONS,
        (
            "standort", "standorte", "standorten", "filialen", "filiale",
            "niederlassung", "niederlassungen", "locations", "location",
            "branches", "branch", "our-locations", "sites", "praxen", "praxis-standorte",
        ),
    ),
    (
        LINK_ABOUT,
        (
            "about", "ueber", "uber", "über", "about-us", "aboutus", "sobre",
            "a-propos", "chi-siamo", "team", "wir-ueber-uns", "unternehmen",
            "company", "nosotros",
        ),
    ),
    (
        LINK_IMPRINT,
        (
            "impressum", "imprint", "legal-notice", "legalnotice", "mentions-legales",
            "aviso-legal", "imprint-notice",
        ),
    ),
)

#: ``@type`` values that describe the business itself.
ORGANISATION_TYPES = frozenset(
    {
        "organization", "localbusiness", "corporation", "ngo", "educationalorganization",
        "medicalorganization", "dentist", "physician", "medicalclinic", "hospital",
        "store", "restaurant", "professionalservice", "legalservice", "financialservice",
        "homeandconstructionbusiness", "healthandbeautysalon", "autodealer",
        "generalcontractor", "hairsalon", "daycare", "veterinarycare", "attorney",
        "accountingservice", "realestateagent", "lodgingbusiness", "hotel",
        "sportsactivitylocation", "exercisegym", "person", "plumber", "electrician",
        "roofingcontractor", "movingcompany", "locksmith", "travelagency",
        "childcare", "educationalorganization", "softwareapplication",
    }
)

_EMAIL_IN_HREF = re.compile(r"^mailto:", re.IGNORECASE)
_TEL_IN_HREF = re.compile(r"^tel:", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")

#: Tags that implicitly end an open ``<a>``. Real pages leave anchors unclosed
#: (``<a href="/kontakt">Kontakt<div>…``), and ``html.parser`` reports tags
#: literally rather than applying the HTML5 tree-construction rules, so without
#: this the link would be dropped. Inline tags are deliberately absent: an
#: anchor may legitimately wrap a ``<span>`` and its text must survive.
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
        "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)


@dataclass
class ExtractedValue:
    """One value found on one page, with the evidence that produced it."""

    field: str
    raw_value: str
    source: str
    evidence: str
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "raw_value": self.raw_value,
            "source": self.source,
            "evidence": self.evidence,
            "context": dict(self.context),
        }


@dataclass
class LinkCandidate:
    """A same-site page worth fetching next."""

    url: str
    kind: str
    text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "kind": self.kind, "text": self.text}


@dataclass
class PageExtraction:
    """Everything one page yielded."""

    url: str
    title: str = ""
    values: list[ExtractedValue] = field(default_factory=list)
    links: list[LinkCandidate] = field(default_factory=list)
    #: Links that leave the page's own site, in the order they appear. Kept
    #: apart from ``links`` because the two answer different questions: ``links``
    #: is "which pages of this site should be read next", ``outbound`` is "which
    #: other sites does this page point at". Only the second can be a candidate
    #: for a business's own website.
    outbound: list[LinkCandidate] = field(default_factory=list)
    jsonld_blocks: int = 0
    jsonld_errors: int = 0
    parse_error: str = ""

    def of_field(self, name: str) -> list[ExtractedValue]:
        return [value for value in self.values if value.field == name]

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "jsonld_blocks": self.jsonld_blocks,
            "jsonld_errors": self.jsonld_errors,
            "parse_error": self.parse_error,
            "values": [value.as_dict() for value in self.values],
            "links": [link.as_dict() for link in self.links],
            "outbound": [link.as_dict() for link in self.outbound],
        }


class _PageParser(HTMLParser):
    """Collects anchors, the title and JSON-LD blocks in one pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.anchors: list[tuple[str, str]] = []
        self.jsonld_blocks: list[str] = []
        self._in_title = False
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._script_is_jsonld = False
        self._script_parts: list[str] = []

    # -- tags ---------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): (value or "") for name, value in attrs}
        if tag == "a":
            # Nested anchors are invalid; treat the new one as a fresh link.
            self._flush_anchor()
            self._anchor_href = attributes.get("href", "")
            self._anchor_text = []
        elif tag == "title":
            self._in_title = True
        elif tag == "script":
            script_type = (attributes.get("type") or "").lower().strip()
            self._script_is_jsonld = script_type == "application/ld+json"
            self._script_parts = []
        elif tag in _BLOCK_TAGS:
            self._flush_anchor()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            attributes = {name.lower(): (value or "") for name, value in attrs}
            href = attributes.get("href", "")
            if href:
                self.anchors.append((href, ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "a":
            self._flush_anchor()
        elif tag == "script":
            if self._script_is_jsonld:
                self.jsonld_blocks.append("".join(self._script_parts))
            self._script_is_jsonld = False
            self._script_parts = []
        elif tag in _BLOCK_TAGS:
            self._flush_anchor()

    def _flush_anchor(self) -> None:
        if self._anchor_href is not None:
            text = _collapse("".join(self._anchor_text))
            self.anchors.append((self._anchor_href, text))
        self._anchor_href = None
        self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if self._script_is_jsonld:
            self._script_parts.append(data)


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


# --- JSON-LD ----------------------------------------------------------------


def iter_jsonld_nodes(payload: Any) -> Iterator[dict]:
    """Walk a JSON-LD document and yield every object node.

    Handles the three shapes seen in the wild: a bare object, a list of objects,
    and an object whose ``@graph`` holds the list.
    """
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        graph = node.get("@graph")
        if graph is not None:
            stack.append(graph)
            # A node may carry both @graph and its own properties.
            if len(node) == 1:
                continue
        yield node


def _node_types(node: dict) -> list[str]:
    value = node.get("@type")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def _is_organisation(node: dict) -> bool:
    return any(item.lower() in ORGANISATION_TYPES for item in _node_types(node))


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        # e.g. {"@type": "Country", "name": "DE"} or {"@id": "..."}
        for key in ("name", "@id", "value"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


def address_from_jsonld(value: Any) -> str | None:
    """Render a schema.org PostalAddress as one raw string.

    The raw string is kept as the source wrote it wherever possible; components
    are only assembled when the source published them separately, and the
    assembly is recorded as such by the caller.
    """
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, list):
        for item in value:
            text = address_from_jsonld(item)
            if text:
                return text
        return None
    if not isinstance(value, dict):
        return None

    street = _text(value.get("streetAddress"))
    postal = _text(value.get("postalCode"))
    locality = _text(value.get("addressLocality"))
    region = _text(value.get("addressRegion"))
    country = _text(value.get("addressCountry"))

    parts: list[str] = []
    if street:
        parts.append(street)
    city_line = " ".join(part for part in (postal, locality) if part)
    if city_line:
        parts.append(city_line)
    if region:
        parts.append(region)
    if country:
        parts.append(country)

    if parts:
        return ", ".join(parts)
    # A node with only @id/name is still a usable hint.
    return _text(value)


def _jsonld_values(url: str, block: str, index: int) -> tuple[list[ExtractedValue], int]:
    """Extract from one JSON-LD block. Returns (values, error_count)."""
    try:
        payload = json.loads(block)
    except (json.JSONDecodeError, ValueError):
        return [], 1

    values: list[ExtractedValue] = []
    for node in iter_jsonld_nodes(payload):
        if not _is_organisation(node):
            continue
        types = _node_types(node)
        context = {"jsonld_type": ", ".join(types) or "unknown", "block": index}

        name = _text(node.get("name"))
        if name:
            values.append(
                ExtractedValue(
                    field="title",
                    raw_value=name,
                    source="jsonld",
                    evidence=_json_snippet(block, "name", name),
                    context=context,
                )
            )

        email = _text(node.get("email"))
        if email:
            email = email.replace("mailto:", "").strip()
            values.append(
                ExtractedValue(
                    field="emails",
                    raw_value=email,
                    source="jsonld",
                    evidence=_json_snippet(block, "email", email),
                    context=context,
                )
            )

        telephone = _text(node.get("telephone"))
        if telephone:
            values.append(
                ExtractedValue(
                    field="phone",
                    raw_value=telephone,
                    source="jsonld",
                    evidence=_json_snippet(block, "telephone", telephone),
                    context=context,
                )
            )

        address = address_from_jsonld(node.get("address"))
        if address:
            values.append(
                ExtractedValue(
                    field="address",
                    raw_value=address,
                    source="jsonld",
                    evidence=_json_snippet(block, "address", address),
                    context=context,
                )
            )

        website = _text(node.get("url"))
        if website:
            values.append(
                ExtractedValue(
                    field="website",
                    raw_value=website,
                    source="jsonld",
                    evidence=_json_snippet(block, "url", website),
                    context=context,
                )
            )

        # schema.org uses `location` for a second branch and `department` for a
        # sub-unit; both mean "another place this business operates".
        for key in ("location", "department", "branch"):
            nested = node.get(key)
            for child in nested if isinstance(nested, list) else [nested]:
                if not isinstance(child, dict):
                    continue
                child_address = address_from_jsonld(child.get("address"))
                child_name = _text(child.get("name"))
                child_context = dict(context)
                child_context["location_of"] = name or types[0] if types else ""
                if child_address:
                    values.append(
                        ExtractedValue(
                            field="address",
                            raw_value=child_address,
                            source="jsonld",
                            evidence=_json_snippet(block, key, child_address),
                            context=dict(child_context, role="location"),
                        )
                    )
                if child_name:
                    values.append(
                        ExtractedValue(
                            field="title",
                            raw_value=child_name,
                            source="jsonld",
                            evidence=_json_snippet(block, key, child_name),
                            context=dict(child_context, role="location"),
                        )
                    )
                child_phone = _text(child.get("telephone"))
                if child_phone:
                    values.append(
                        ExtractedValue(
                            field="phone",
                            raw_value=child_phone,
                            source="jsonld",
                            evidence=_json_snippet(block, key, child_phone),
                            context=dict(child_context, role="location"),
                        )
                    )

    return values, 0


def _json_snippet(block: str, key: str, value: str, width: int = 160) -> str:
    """A short, locatable fragment of the JSON-LD block that held the value."""
    needle = f'"{key}"'
    position = block.find(needle)
    if position < 0:
        return _collapse(value)[:width]
    fragment = block[position : position + width]
    return _collapse(fragment)


# --- links ------------------------------------------------------------------


def classify_link(path: str, text: str) -> str | None:
    """The page kind a link points at, or None when it is not interesting."""
    haystack = f"{path} {text}".lower()
    for kind, keywords in LINK_KEYWORDS:
        for keyword in keywords:
            if keyword in haystack:
                return kind
    return None


def find_link_candidates(
    base_url: str,
    anchors: list[tuple[str, str]],
    *,
    max_links: int = 40,
) -> list[LinkCandidate]:
    """Same-site pages worth fetching, best first, de-duplicated.

    Off-site anchors are dropped here rather than fetched and rejected later:
    the site boundary is a property of the link, and the redirect check in
    :mod:`collector.webfetch` is the second line of defence for the case where a
    same-site URL redirects away.
    """
    base_site = registrable_domain(urllib.parse.urlsplit(base_url).hostname or "")
    seen: dict[str, LinkCandidate] = {}
    order = {LINK_CONTACT: 0, LINK_LOCATIONS: 1, LINK_ABOUT: 2, LINK_IMPRINT: 3}

    for href, text in anchors:
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        host = (parts.hostname or "").lower()
        if not host or registrable_domain(host) != base_site:
            continue

        # Query strings and fragments do not change which page this is.
        clean = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        kind = classify_link(parts.path, text)
        if kind is None:
            continue

        existing = seen.get(clean)
        if existing is not None:
            if order[kind] < order[existing.kind]:
                existing.kind = kind
            continue
        seen[clean] = LinkCandidate(url=clean, kind=kind, text=text)

    candidates = sorted(
        seen.values(),
        key=lambda item: (order[item.kind], len(item.url), item.url),
    )
    return candidates[:max_links]


def find_outbound_links(
    base_url: str,
    anchors: list[tuple[str, str]],
    *,
    max_links: int = 40,
) -> list[LinkCandidate]:
    """Links that leave the page's own site, in order, de-duplicated.

    The mirror image of :func:`find_link_candidates`: same-site links are the
    ones *here* that are dropped. Nothing is filtered by page kind -- whether an
    off-site link is the business's own website is a question about ownership,
    and that decision belongs to the caller, not to the parser.

    Only ``http``/``https`` links are kept: a ``mailto:`` or ``tel:`` anchor is
    contact data, which :func:`_contact_values` already reads, and a ``data:`` or
    ``javascript:`` target is not a site at all.
    """
    base_site = registrable_domain(urllib.parse.urlsplit(base_url).hostname or "")
    seen: dict[str, LinkCandidate] = {}

    for href, text in anchors:
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        host = (parts.hostname or "").lower()
        if not host:
            continue
        if base_site and registrable_domain(host) == base_site:
            continue
        # Not a page of the site being read, so it is kept whole: its query
        # string may matter (a redirector, a tracking link) and the ownership
        # rule reads the host, not a canonicalised path.
        clean = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, parts.query, "")
        )
        if clean in seen:
            continue
        seen[clean] = LinkCandidate(url=clean, kind=LINK_OUTBOUND, text=text)

    return list(seen.values())[:max_links]


# --- entry point ------------------------------------------------------------


def extract_page(url: str, html: str) -> PageExtraction:
    """Extract contact values and follow-up links from one HTML page.

    Never raises on malformed markup: a page that cannot be parsed yields an
    extraction with ``parse_error`` set, so one broken site cannot abort a batch.
    """
    result = PageExtraction(url=url)

    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - a broken page must not stop the run
        result.parse_error = f"{type(exc).__name__}: {exc}"

    result.title = _collapse("".join(parser.title_parts))

    # JSON-LD first: it is the best-attributed source, and the order of
    # `result.values` reflects that.
    for index, block in enumerate(parser.jsonld_blocks, start=1):
        result.jsonld_blocks += 1
        values, errors = _jsonld_values(url, block, index)
        result.jsonld_errors += errors
        result.values.extend(values)

    for href, text in parser.anchors:
        # The anchor carries *two* statements about the contact: what the link
        # targets (``href``) and what the page shows a human (its text). They
        # are recorded separately -- ``href_value`` and ``display_text`` --
        # because on a real site they can disagree, and collapsing them into
        # one string is what hides that. Whether they actually *conflict* is a
        # judgement (it needs number/address normalisation), so it is left to
        # :mod:`collector.enrich`; extraction only keeps both, verbatim.
        display = _collapse(text)
        if _EMAIL_IN_HREF.match(href):
            address = href[len("mailto:") :].split("?", 1)[0].strip()
            # A mailto: may list several recipients separated by commas.
            for part in address.split(","):
                part = part.strip()
                if not part:
                    continue
                result.values.append(
                    ExtractedValue(
                        field="emails",
                        raw_value=part,
                        source="mailto",
                        evidence=display or f"mailto:{part}",
                        context={"href": href, "href_value": part, "display_text": display},
                    )
                )
        elif _TEL_IN_HREF.match(href):
            number = urllib.parse.unquote(href[len("tel:") :]).strip()
            if number:
                result.values.append(
                    ExtractedValue(
                        field="phone",
                        raw_value=number,
                        source="tel",
                        evidence=display or f"tel:{number}",
                        context={"href": href, "href_value": number, "display_text": display},
                    )
                )

    result.links = find_link_candidates(url, parser.anchors)
    result.outbound = find_outbound_links(url, parser.anchors)
    return result
