"""Cross-source entity merge (task book 004, stage E).

The task book is specific about how this must behave:

* "去重优先稳定源 ID，名称/电话/地址/地理位置为组合证据" — a stable provider id
  already keeps one provider's records together; across providers it is
  *evidence*, not proof;
* "区分公司与门店，不以域名或客服电话单独合并" — a shared domain or a shared
  switchboard number merges a whole chain. Both are refused as the sole reason;
* "冲突保留候选和合并原因" — the decision, its evidence and the losing candidate
  all survive.

So a merge needs **a name agreement plus at least one independent confirmations**
(phone, address or geography), and it is **refused** when both records carry
addresses that disagree: that is the signature of two branches of the same brand,
which the task book's acceptance list explicitly says must stay separate.

Nothing is overwritten. The merged entity keeps every field fact it had, and the
survivor keeps its own; the merge adds a row recording the decision. Reversing it
means deleting that row, not recovering lost data.

Candidate pairs are produced by *blocking* — exact phone, exact domain, exact
name, a name prefix, and rounded coordinates — rather than by comparing every
pair of entities. Blocking is a recall/compute trade-off, not a correctness one;
the pairs it does produce are judged by the same rule.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

from . import db
from .addresscheck import is_name_shaped, parse_address
from .identity import normalize_phone, normalize_text
from .phonecheck import check_phone

#: Components of the decision.
REASON_NAME = "name"
REASON_NAME_WEAK = "name_weak"
REASON_PHONE = "phone"
REASON_ADDRESS = "address"
REASON_GEO = "geography"

#: How close two coordinates must be to count as the same place.
#:
#: 120 m is a *positive* signal only. It is not used to *reject* a merge,
#: because OSM records a building's centroid while Google Maps records its
#: entrance: the same shop can legitimately be a few hundred metres apart in
#: the two sources, and that difference is a property of the data, not of the
#: two places being different.
DEFAULT_GEO_TOLERANCE_METERS = 120.0

#: Name agreement needed before anything else is considered.
DEFAULT_NAME_THRESHOLD = 0.82

#: Street-name agreement needed for two addresses to count as the same one.
DEFAULT_STREET_THRESHOLD = 0.85

#: Rounds coordinates to this many decimals when building geographic blocks.
#: 3 decimals is roughly 110 m, which matches the geo tolerance above.
_GEO_BLOCK_DECIMALS = 3

_DIGITS = re.compile(r"\D+")


#: How many kept-apart pairs are named in the report. Enough to show the
#: operator what was left in, small enough that a pathological page cannot turn
#: the checkpoint into a second copy of the database.
BRANCH_PAIR_REPORT_LIMIT = 20


@dataclass
class MergeStats:
    entities: int = 0
    pairs_considered: int = 0
    merged: int = 0
    refused_branch: int = 0
    refused_insufficient: int = 0
    already_recorded: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)
    #: The pairs the branch rule kept apart *while the name and the phone agreed*
    #: -- the ambiguous ones worth naming. A directory that lists one company
    #: twice (two detail pages, one phone, a typo apart in the street number)
    #: lands here, and so does a real second branch. The rule cannot tell them
    #: apart and must not merge on the name, so the rows stay separate and the
    #: reader is told which rows and why, instead of a duplicate appearing with
    #: no explanation. ``branch_pairs_total`` counts all of them; the list is
    #: capped at :data:`BRANCH_PAIR_REPORT_LIMIT`.
    branch_pairs: list[dict[str, str]] = field(default_factory=list)
    branch_pairs_total: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items()}


@dataclass
class Candidate:
    """One entity's merge-relevant values, resolved once."""

    entity_id: int
    stable_key: str
    id_kind: str
    provider: str
    name: str
    name_norm: str
    phone: str
    phone_e164: str
    address: str
    address_key: tuple[str, str, str]
    latitude: float | None
    longitude: float | None
    #: The coordinate reference system of ``latitude``/``longitude``. Two
    #: candidates whose coordinates are in different systems must never have
    #: their distance compared: WGS84 and GCJ-02 put the same place 300-500 m
    #: apart, which is larger than the geo tolerance and would read as "a
    #: different shop" — or, worse, a nearby pair could read as "the same one".
    crs: str | None
    domain: str
    fact_count: int
    completeness: int


# --- individual comparisons -------------------------------------------------


def name_similarity(a: str, b: str) -> float:
    """0..1 agreement between two names.

    Both directions are computed: ``Praxis Nord`` vs ``Zahnarztpraxis Nord``
    contains the shorter string, and containment of a *word* is weaker evidence
    than an overall match, so the score is the containment quotient rather than
    a plain 1.0. Names outside the Latin range are compared on the whole string,
    because splitting on whitespace means nothing there.
    """
    left = (a or "").strip().casefold()
    right = (b or "").strip().casefold()
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    ratio = SequenceMatcher(None, left, right).ratio()
    # Containment: the shorter is inside the longer.
    if left in right or right in left:
        shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
        if len(shorter) >= 3:
            containment = len(shorter) / len(longer)
            ratio = max(ratio, 0.6 + 0.4 * containment)

    # Token overlap for Latin names.
    left_tokens = {t for t in left.split() if len(t) >= 3}
    right_tokens = {t for t in right.split() if len(t) >= 3}
    if left_tokens and right_tokens:
        overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        ratio = max(ratio, overlap)
    return ratio


def phone_e164(value: str, country: str = "", rules: dict | None = None) -> str:
    """The E.164 form of a phone number, or '' when there is not one.

    A number without its own country code needs a region as *context*, and even
    then libphonenumber only accepts it as plausible, which is why the caller
    keeps the raw digits and compares them too.
    """
    text = (value or "").strip()
    if not text:
        return ""
    verdict = check_phone(text, region=country or None, rules=rules or None)
    return verdict.e164 or ""


def phones_agree(a: Candidate, b: Candidate) -> bool:
    """Two phone numbers refer to the same line, without guessing a country.

    E.164 is compared first because it is the only form where the country is
    part of the number. When one side has none, the raw digits are compared: a
    national number and its international form share their last nine digits, so
    "030 40367600" and "+49 30 40367600" still match.
    """
    if not a.phone or not b.phone:
        return False
    if a.phone_e164 and b.phone_e164:
        return a.phone_e164 == b.phone_e164
    left = _DIGITS.sub("", a.phone)
    right = _DIGITS.sub("", b.phone)
    if not left or not right:
        return False
    return left == right or left.endswith(right) or right.endswith(left)


_ABBREVIATION_SUFFIXES = (
    # German and European street-type abbreviations that a directory or a
    # second source writes in full. Without this, "Hauptstr. 5" and
    # "Hauptstrasse 5" look like two different streets, which is the branch
    # guard firing on a spelling difference rather than on a real second
    # location.
    ("str", "asse"),
    ("pl", "atz"),
    ("ch", "aussee"),
    ("gf", "ert"),
    ("hf", "hof"),
    ("mkt", "markt"),
)


def street_key(name: str) -> str:
    """A comparable form of a street name.

    Punctuation and spacing are removed (``Hauptstr.`` -> ``hauptstr``), the
    sharp s is folded to ``ss``, and a trailing abbreviation is expanded. The
    result keeps non-Latin scripts untouched, because folding them would be
    transliteration.
    """
    text = re.sub(r"[.\s]", "", (name or "").casefold()).replace("ß", "ss")
    if not text:
        return ""
    for short, completion in _ABBREVIATION_SUFFIXES:
        if text.endswith(short) and len(text) > len(short) + 2:
            return text + completion
    return text


def _house_number_in_text(text: str, postal_code: str) -> str:
    """A house number the parser did not attribute, or "".

    :func:`~collector.addresscheck.parse_address` reads the *first*
    comma-separated part as the street, so an address that leads with an
    annotation (``Parkplatz über Rochstraße zu erreichen, Karl-Liebknecht-Str.
    13, 10178 Berlin``) or carries a building name (``Friedrichstr, Atrium 60,
    10117 Berlin``) can lose its house number completely. An empty house number
    is then read as "no conflict" by :func:`address_agrees`, which is how two
    *different* shops on the same street with the same postcode were merged as
    one in a real run. Recovering the number restores the branch guard.

    Only the parts *before* the postcode are scanned — scanning the whole
    string would pick up the postcode itself as the number. The recovered value
    is only ever compared for equality, so this can make a merge less likely,
    never more.
    """
    parts = [part.strip() for part in (text or "").split(",") if part.strip()]
    before = [part for part in parts if not (postal_code and postal_code in part)] or parts
    for part in before:
        for match in re.finditer(r"\d{1,4}[a-zA-Z]?(?:-\d{1,4})?", part):
            value = match.group(0).casefold()
            if value.isdigit() and len(value) == 5:
                continue  # a five-digit token is a postcode, not a house number
            return value
    return ""


def address_components(value: str, country: str = "") -> tuple[str, str, str]:
    """(house_number, street_key, postal_code) for an address, normalised."""
    if not (value or "").strip():
        return ("", "", "")
    verdict = parse_address(value, country=country or None)
    components = verdict.components or {}
    house = (components.get("house_number") or "").casefold()
    street = street_key(components.get("street_name") or components.get("street") or "")
    postal = (components.get("postal_code") or "").casefold()
    if not house:
        house = _house_number_in_text(str(value), postal)
    return (house, street, postal)


def address_agrees(a: tuple[str, str, str], b: tuple[str, str, str]) -> bool:
    """Same address, tolerating spelling differences in the street name."""
    a_house, a_street, a_post = a
    b_house, b_street, b_post = b
    if not any(a) or not any(b):
        return False
    if a_post and b_post and a_post != b_post:
        return False
    if a_house and b_house and a_house != b_house:
        return False
    if a_street and b_street:
        if a_street == b_street:
            return True
        return SequenceMatcher(None, a_street, b_street).ratio() >= DEFAULT_STREET_THRESHOLD
    # Only one side names a street; agreeing on the house number is enough.
    if a_house and b_house:
        return True
    return bool(a_post and a_post == b_post)


def addresses_differ(a: tuple[str, str, str], b: tuple[str, str, str]) -> bool:
    """Both records have addresses and they are not the same one.

    This is the branch guard: a brand with two locations produces two rows, and
    the task book's acceptance list requires them to stay separate. It fires on
    a different house number, a different postcode, or a materially different
    street — not on an abbreviation, which :func:`street_key` already folded.
    """
    if not any(a) or not any(b):
        return False
    a_house, a_street, a_post = a
    b_house, b_street, b_post = b
    if a_house and b_house and a_house != b_house:
        return True
    if a_post and b_post and a_post != b_post:
        return True
    if a_street and b_street and a_street != b_street:
        if SequenceMatcher(None, a_street, b_street).ratio() < DEFAULT_STREET_THRESHOLD:
            return True
    return False


def geo_distance_meters(
    a_lat: float | None, a_lon: float | None, b_lat: float | None, b_lon: float | None
) -> float | None:
    if None in (a_lat, a_lon, b_lat, b_lon):
        return None
    radius = 6_371_008.8  # IUGG mean Earth radius in metres
    lat1, lon1, lat2, lon2 = map(math.radians, (a_lat, a_lon, b_lat, b_lon))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


# --- the decision -----------------------------------------------------------


def names_plausibly_same(a: str, b: str) -> bool:
    """A weaker name signal than a whole-string match, but not none.

    True when the two names share a word of at least three characters, or one
    is contained in the other. This is the floor under the combined-evidence
    escape in :func:`same_store`: "Praxis Dr. M\u00fcller" and
    "Zahnarztpraxis M\u00fcller" share "m\u00fcller", while "Praxis Nord" and
    "Vermittlung 24" share nothing and stay apart.
    """
    left = _tokens(a)
    right = _tokens(b)
    if not left or not right:
        return False
    if left & right:
        return True
    low, high = (a.casefold(), b.casefold()) if len(a) <= len(b) else (b.casefold(), a.casefold())
    return len(low) >= 3 and low in high


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^\w]+", (value or "").casefold())
        if len(token) >= 3
    }


def same_store(
    a: Candidate,
    b: Candidate,
    *,
    country: str = "",
    name_threshold: float = DEFAULT_NAME_THRESHOLD,
    geo_tolerance_meters: float = DEFAULT_GEO_TOLERANCE_METERS,
) -> tuple[bool, list[str], float]:
    """Decide whether two candidate rows are the same store.

    Returns (merge, evidence, score). ``evidence`` names what agreed, which is
    what lands in the merge reason; the score only orders decisions and is never
    presented as a probability.

    A name on its own never merges, and neither does any single confirmation.
    The name gate is relaxed only when the *strong* confirmations agree — the
    E.164 number and the parsed address — because that combination is what
    separates a source's naming convention (a practice's legal name against its
    trading name) from two genuinely different businesses. Geography is
    deliberately **not** part of that escape: OSM records a building's centroid
    and Google Maps the entrance, so the two can legitimately be a few hundred
    metres apart for the same shop, and treating that as agreement would make
    the tolerance a lottery. Geography still counts on its own as evidence when
    it does agree. Even under the escape the names must still share a word, and
    the weaker name is recorded in the evidence so the decision stays reviewable.
    """
    name = name_similarity(a.name, b.name)
    if name < name_threshold and not (
        phones_agree(a, b) and address_agrees(a.address_key, b.address_key)
        and names_plausibly_same(a.name, b.name)
    ):
        return False, [], 0.0

    evidence: list[str] = []
    score = name

    if phones_agree(a, b):
        evidence.append(REASON_PHONE)
        score += 0.4 if (a.phone_e164 and b.phone_e164) else 0.3

    if address_agrees(a.address_key, b.address_key):
        evidence.append(REASON_ADDRESS)
        score += 0.3

    if _geo_agrees(a, b, geo_tolerance_meters):
        evidence.append(REASON_GEO)
        score += 0.3

    if name < name_threshold:
        # The name was carried by the confirmations, not by its own score.
        evidence.append(REASON_NAME_WEAK)

    if not evidence:
        return False, [], 0.0

    if addresses_differ(a.address_key, b.address_key):
        # Same name, maybe the same phone, different street: two locations of
        # the same brand, which must stay separate.
        return False, [REASON_ADDRESS], 0.0

    return True, evidence, round(score, 4)


def _geo_agrees(
    a: Candidate, b: Candidate, geo_tolerance_meters: float
) -> bool:
    if not _same_crs(a, b):
        # Comparing across coordinate systems is not a weaker signal, it is a
        # wrong one, so it is refused rather than tolerated. The pair can still
        # be merged on name, phone, address or domain.
        return False
    distance = geo_distance_meters(a.latitude, a.longitude, b.latitude, b.longitude)
    return distance is not None and distance <= geo_tolerance_meters


def _same_crs(a: Candidate, b: Candidate) -> bool:
    """Are these two coordinates in the same reference system?

    An unknown system ("") is treated as compatible: it means the provider
    declared none, which is the case for every source that publishes no
    coordinates at all. Two *different* named systems never match.
    """
    return (a.crs or "") == (b.crs or "")


# --- candidates -------------------------------------------------------------


def collect_candidates(rows: Iterable[Any], *, country: str = "", rules: dict | None = None) -> list[Candidate]:
    out: list[Candidate] = []
    for row in rows:
        name = (row["name"] or "") if "name" in row.keys() else ""
        out.append(
            Candidate(
                entity_id=int(row["id"]),
                stable_key=row["stable_key"],
                id_kind=row["id_kind"],
                provider="",
                name=name,
                name_norm=normalize_text(name),
                phone=(row["phone_norm"] or "") if "phone_norm" in row.keys() else "",
                phone_e164="",
                address=(row["address"] or "") if "address" in row.keys() else "",
                address_key=("", "", ""),
                latitude=row["latitude"] if "latitude" in row.keys() else None,
                longitude=row["longitude"] if "longitude" in row.keys() else None,
                crs=(row["crs"] or None) if "crs" in row.keys() else None,
                domain=(row["domain"] or "") if "domain" in row.keys() else "",
                fact_count=0,
                completeness=0,
            )
        )
    return _enrich(out, country=country, rules=rules)


def _enrich(candidates: list[Candidate], *, country: str, rules: dict | None) -> list[Candidate]:
    for candidate in candidates:
        if candidate.phone:
            candidate.phone_e164 = phone_e164(candidate.phone, country, rules)
        # 050: an address slot holding the business's own name is not an
        # address, so it must not produce an address key. Without this the house
        # number inside a company's own name ("Hansa 12 GmbH") matches another
        # record's copy of the same name, and that match is then counted as the
        # independent confirmation -- the name agreeing with itself, twice over.
        if candidate.address and not is_name_shaped(candidate.address, candidate.name):
            candidate.address_key = address_components(candidate.address, country)
    return candidates


def _completeness(row) -> int:
    """How many identity-relevant fields this entity actually has."""
    score = 0
    for key in ("name", "address", "phone_norm", "website", "domain"):
        value = row[key] if key in row.keys() else None
        if value:
            score += 1
    if row["latitude"] is not None and row["longitude"] is not None:
        score += 1
    return score


def _blocks(candidates: list[Candidate]) -> list[tuple[int, int]]:
    """Candidate pairs, produced by blocking.

    Five blocks, because relying on one would miss the cross-source case the
    stage exists for: a store recorded twice with different phones and a
    mistyped name still shares its coordinates and its street.
    """
    by_phone: dict[str, list[int]] = {}
    by_domain: dict[str, list[int]] = {}
    by_name: dict[str, list[int]] = {}
    by_prefix: dict[str, list[int]] = {}
    by_geo: dict[str, list[int]] = {}

    for index, candidate in enumerate(candidates):
        if candidate.phone_e164:
            by_phone.setdefault(candidate.phone_e164, []).append(index)
        digits = _DIGITS.sub("", candidate.phone or "")
        if len(digits) >= 7:
            by_phone.setdefault(f"digits:{digits[-9:]}", []).append(index)
        if candidate.domain:
            by_domain.setdefault(candidate.domain, []).append(index)
        if candidate.name_norm:
            by_name.setdefault(candidate.name_norm, []).append(index)
            prefix = _name_prefix(candidate.name_norm)
            if prefix:
                by_prefix.setdefault(prefix, []).append(index)
        if candidate.latitude is not None and candidate.longitude is not None:
            # The CRS is part of the block key: two records in different
            # systems must not even become a candidate pair, or the distance
            # check would be asked to answer a question it cannot.
            key = (
                f"{candidate.crs or ''}:"
                f"{round(candidate.latitude, _GEO_BLOCK_DECIMALS)}:"
                f"{round(candidate.longitude, _GEO_BLOCK_DECIMALS)}"
            )
            by_geo.setdefault(key, []).append(index)

    pairs: set[tuple[int, int]] = set()
    for block in list(by_phone.values()) + list(by_domain.values()) + list(by_name.values()) + list(by_prefix.values()) + list(by_geo.values()):
        if len(block) < 2:
            continue
        for a in block:
            for b in block:
                if a == b:
                    continue
                pairs.add((min(a, b), max(a, b)))
    return sorted(pairs)


_NAME_STOPWORDS = {"der", "die", "das", "the", "and", "&", "co", "ltd", "gmbh", "kg", "inc", "llc", "ug"}


def _name_prefix(name_norm: str) -> str:
    """A blocking prefix: the first significant token.

    Latin names start with the trading name ("praxis nordlicht"), so the first
    token discriminates. For scripts without spaces the prefix is simply the
    first characters, which still groups same-name records.
    """
    text = (name_norm or "").strip()
    if not text:
        return ""
    tokens = [t for t in re.split(r"\s+", text) if t and t not in _NAME_STOPWORDS]
    if not tokens:
        return text[:8]
    return tokens[0][:12] if " " in text else text[:8]


# --- the stage --------------------------------------------------------------


def _note_branch_pair(stats: MergeStats, a: Candidate, b: Candidate) -> None:
    """Name a kept-apart pair when the name *and* the phone agree.

    The branch rule refuses a merge when two records share a name and some
    signal but sit at different parsed addresses. Most of those refusals are
    ordinary and need no comment -- two records with the same trading name and
    nothing else in common. The ambiguous ones are the pairs whose switchboard
    number is identical: a directory that lists one company twice, or a real
    second branch. The rule must not merge them (a shared switchboard is exactly
    what two branches look like), so the rows stay separate and *this* is what
    keeps them from being a silent duplicate in the deliverable.
    """
    if not phones_agree(a, b):
        return
    stats.branch_pairs_total += 1
    if len(stats.branch_pairs) >= BRANCH_PAIR_REPORT_LIMIT:
        return
    stats.branch_pairs.append(
        {
            "name": a.name or b.name or "",
            "phone": a.phone or b.phone or "",
            "address_a": a.address or "",
            "address_b": b.address or "",
            "source_a": a.stable_key,
            "source_b": b.stable_key,
        }
    )


def merge_run(
    conn,
    run_id: int,
    cfg,
    *,
    country: str = "",
) -> MergeStats:
    """Merge cross-source duplicates among the entities a run has seen."""
    stats = MergeStats()
    rows = list(db.iter_run_entities(conn, run_id))
    stats.entities = len(rows)
    if not rows:
        stats.notes.append("no entities to merge")
        return stats

    rules = cfg.rules() if cfg is not None else {}
    country = country or (cfg.country if cfg is not None else "")
    candidates = collect_candidates(rows, country=country, rules=rules)

    providers = _provider_by_entity(conn, run_id)
    facts = _fact_counts(conn, run_id)
    for candidate in candidates:
        candidate.provider = providers.get(candidate.entity_id, "")
        candidate.fact_count = facts.get(candidate.entity_id, 0)
        candidate.completeness = _completeness_of(rows, candidate.entity_id)

    pairs = _blocks(candidates)
    stats.pairs_considered = len(pairs)

    # Union-find keeps merged groups consistent when three sources describe one
    # store: A~B and B~C put A, B and C into a single group with one survivor,
    # instead of a chain of pairwise merges that could contradict each other.
    parent = list(range(len(candidates)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    groups: dict[int, list[int]] = {}
    for left, right in pairs:
        ok, evidence, score = same_store(
            candidates[left], candidates[right], country=country
        )
        if not ok:
            if evidence == [REASON_ADDRESS]:
                stats.refused_branch += 1
                _note_branch_pair(stats, candidates[left], candidates[right])
            else:
                stats.refused_insufficient += 1
            continue

        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            stats.already_recorded += 1
            continue
        parent[max(left_root, right_root)] = min(left_root, right_root)

    for index in range(len(candidates)):
        groups.setdefault(find(index), []).append(index)

    for members in groups.values():
        if len(members) < 2:
            continue
        survivor = _survivor(candidates, members)
        for member in members:
            if member == survivor:
                continue
            evidence = _merged_evidence(candidates, survivor, member)
            if db.upsert_merge(
                conn,
                canonical_entity_id=candidates[survivor].entity_id,
                entity_id=candidates[member].entity_id,
                reason="+".join(evidence["evidence"]),
                evidence=evidence,
                score=evidence["score"],
                run_id=run_id,
            ):
                stats.merged += 1
            else:
                stats.already_recorded += 1

    clusters = len(groups)
    stats.notes.append(
        f"{stats.entities} entity(ies) reduced to {clusters} store(s) by "
        f"{stats.merged} merge(s)"
    )
    # The stage records its own ledger, so the numbers and the kept-apart pairs
    # travel with the run whoever called the merge -- the orchestrator, a
    # re-export path, or a script. The export reads them from here; a caller
    # that also wants ``finished_at`` on the row writes its own checkpoint with
    # the same values, which is idempotent.
    db.set_checkpoint(
        conn,
        run_id,
        "merge",
        "stats",
        {
            "entities": stats.entities,
            "pairs_considered": stats.pairs_considered,
            "merged": stats.merged,
            "refused_branch": stats.refused_branch,
            "refused_insufficient": stats.refused_insufficient,
            "branch_pairs": list(stats.branch_pairs),
            "branch_pairs_total": stats.branch_pairs_total,
            "notes": list(stats.notes),
        },
    )
    return stats


def _completeness_of(rows: list[Any], entity_id: int) -> int:
    for row in rows:
        if int(row["id"]) == entity_id:
            return _completeness(row)
    return 0


def _survivor(candidates: list[Candidate], members: list[int]) -> int:
    """Which member of a merged group survives.

    Deterministic and evidence-based: the most complete record wins, then the
    one with the most field facts, then the lowest entity id. Never the name's
    alphabetical order, because that would let a coincidence decide which
    spelling the export shows.
    """
    return max(
        members,
        key=lambda index: (
            candidates[index].completeness,
            candidates[index].fact_count,
            -candidates[index].entity_id,
        ),
    )


def _merged_evidence(candidates: list[Candidate], survivor: int, loser: int) -> dict:
    winner, candidate = candidates[survivor], candidates[loser]
    evidence = _evidence_for(winner, candidate)
    evidence["survivor"] = {
        "entity_id": winner.entity_id,
        "stable_key": winner.stable_key,
        "id_kind": winner.id_kind,
    }
    evidence["candidate"] = {
        "entity_id": candidate.entity_id,
        "stable_key": candidate.stable_key,
        "id_kind": candidate.id_kind,
        "name": candidate.name,
    }
    return evidence


def _evidence_for(survivor: Candidate, loser: Candidate) -> dict:
    """Why these two are the same store, computed against each other."""
    evidence: list[str] = []
    if phones_agree(survivor, loser):
        evidence.append(REASON_PHONE)
    if address_agrees(survivor.address_key, loser.address_key):
        evidence.append(REASON_ADDRESS)
    distance = _comparable_distance(survivor, loser)
    if distance is not None and distance <= DEFAULT_GEO_TOLERANCE_METERS:
        evidence.append(REASON_GEO)
    return {
        "evidence": evidence,
        "score": round(
            name_similarity(survivor.name, loser.name)
            + 0.3 * len(evidence)
            + (distance is not None and distance <= DEFAULT_GEO_TOLERANCE_METERS and 0.1 or 0.0),
            4,
        ),
        "name_similarity": round(name_similarity(survivor.name, loser.name), 4),
        "distance_meters": None if distance is None else round(distance, 1),
        "crs": survivor.crs or loser.crs or None,
    }


def _comparable_distance(a: Candidate, b: Candidate) -> float | None:
    """The distance in metres, or None when the two must not be compared.

    None means "no usable distance evidence" — either a coordinate is missing
    or the two are in different reference systems, in which case a number would
    be misleading rather than merely imprecise.
    """
    if not _same_crs(a, b):
        return None
    return geo_distance_meters(a.latitude, a.longitude, b.latitude, b.longitude)


def _provider_by_entity(conn, run_id: int) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in conn.execute(
        """
        SELECT DISTINCT ff.entity_id AS entity_id, ff.provider AS provider
          FROM field_facts ff
          JOIN entity_observations eo ON eo.entity_id = ff.entity_id
         WHERE eo.run_id = ?
        """,
        (run_id,),
    ):
        out[int(row["entity_id"])] = str(row["provider"])
    return out


def _fact_counts(conn, run_id: int) -> dict[int, int]:
    out: dict[int, int] = {}
    for row in conn.execute(
        """
        SELECT ff.entity_id AS entity_id, COUNT(*) AS n
          FROM field_facts ff
          JOIN entity_observations eo ON eo.entity_id = ff.entity_id
         WHERE eo.run_id = ?
         GROUP BY ff.entity_id
        """,
        (run_id,),
    ):
        out[int(row["entity_id"])] = int(row["n"])
    return out


__all__ = [
    "Candidate",
    "MergeStats",
    "address_agrees",
    "addresses_differ",
    "collect_candidates",
    "merge_run",
    "name_similarity",
    "phone_e164",
    "phones_agree",
    "same_store",
]
