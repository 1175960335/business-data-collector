"""Stage F: explainable scoring.

The task book requires the five dimensions to be shown **separately** —
完整度 / 验证结果 / 证据可信度 / 时效性 / 冲突 — and says a composite is
only allowed if its rule is public and versioned, and never to be presented
as accuracy or sales value. So:

* every dimension is a number in ``[0.0, 1.0]`` **plus a human-readable
  reason** naming the facts it was computed from;
* the composite is a published weighted mean; the weights and the
  ``SCORER_VERSION`` constant below *are* the rule, and both are exported
  next to the score;
* a dimension with nothing to measure reports the reason (``no facts``,
  ``no timestamps``, ...) instead of a confident zero.

None of these numbers is an accuracy: an entity with no website scores low
on completeness because the *configuration asked* for a website, not
because it is a bad lead.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

#: Version of the scoring rule. Any change to a dimension definition, a
#: bucket boundary or a weight must bump this string and be described in the
#: changelog below, because a stored score is only meaningful together with
#: the rule that produced it.
SCORER_VERSION = "2026-10-12.1"

#: Rule changelog (append-only). One line per SCORER_VERSION.
CHANGELOG: tuple[str, ...] = (
    "2026-10-12.1: first published rule. Weights: completeness .35, "
    "validation .25, evidence .20, freshness .10, conflict .10.",
)

#: Composite weights. They are public on purpose: a hidden weight is an
#: unexplainable score.
DIMENSION_WEIGHTS: dict[str, float] = {
    "completeness": 0.35,
    "validation": 0.25,
    "evidence": 0.20,
    "freshness": 0.10,
    "conflict": 0.10,
}

#: Freshness buckets: (max age in days inclusive, value). A store observed
#: within the last week scores 1.0; a store last seen over a year ago 0.1.
FRESHNESS_BUCKETS: tuple[tuple[float, float], ...] = (
    (7.0, 1.0),
    (30.0, 0.8),
    (90.0, 0.5),
    (365.0, 0.3),
)
FRESHNESS_FLOOR = 0.1

#: Conflict penalties per offending fact. Both are minuted in the reason.
UNCERTAIN_PENALTY = 0.25
INVALID_PENALTY = 0.15


@dataclass(frozen=True)
class Dimension:
    name: str
    value: float
    reason: str


@dataclass(frozen=True)
class EntityScore:
    """One entity's five dimensions plus the published composite."""

    entity_id: int
    stable_key: str | None
    dimensions: tuple[Dimension, ...]
    composite: float
    scorer_version: str = SCORER_VERSION

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "entity_id": self.entity_id,
            "stable_key": self.stable_key,
            "composite": round(self.composite, 4),
            "scorer_version": self.scorer_version,
        }
        for dimension in self.dimensions:
            out[f"score_{dimension.name}"] = round(dimension.value, 4)
            out[f"score_{dimension.name}_reason"] = dimension.reason
        return out


def _parse_timestamp(text: Any) -> dt.datetime | None:
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _completeness(facts: list[Any], requested: Iterable[str]) -> Dimension:
    """How many of the *requested* fields carry a value.

    The denominator is the user's ``collect.fields``, not everything the
    schema could hold: asking for six fields and getting four is 0.67, and
    that is the honest number.
    """
    wanted = [name for name in requested if name]
    if not wanted:
        return Dimension("completeness", 1.0, "no fields requested by the config")
    filled: set[str] = set()
    for row in facts:
        if _fact_value(row):
            filled.add(str(row["field"]))
    missing = [name for name in wanted if name not in filled]
    value = (len(wanted) - len(missing)) / len(wanted)
    reason = f"{len(wanted) - len(missing)}/{len(wanted)} requested fields have a value"
    if missing:
        reason += "; missing: " + ", ".join(sorted(missing))
    return Dimension("completeness", value, reason)


def _fact_value(row: Any) -> str | None:
    text = row["raw_value"] or row["normalized_value"]
    if text is None:
        return None
    text = str(text).strip()
    return text or None


def _validation(facts: list[Any]) -> Dimension:
    """Share of present facts that validated cleanly.

    ``missing`` is not counted against validation — an absent field is a
    completeness question, not a wrong answer. ``uncertain`` counts half:
    the D stage's website-vs-listing conflicts land here by design.
    """
    counts = {"valid": 0, "uncertain": 0, "invalid": 0}
    for row in facts:
        status = str(row["validation_status"] or "")
        if status in counts:
            counts[status] += 1
    judged = counts["valid"] + counts["uncertain"] + counts["invalid"]
    if judged == 0:
        return Dimension("validation", 0.0, "no validated facts to judge")
    value = (counts["valid"] + 0.5 * counts["uncertain"]) / judged
    reason = (
        f"{counts['valid']} valid, {counts['uncertain']} uncertain, "
        f"{counts['invalid']} invalid of {judged} checked fact(s)"
    )
    return Dimension("validation", value, reason)


def _evidence(facts: list[Any]) -> Dimension:
    """Share of facts that carry a pointer to their evidence."""
    with_evidence = 0
    for row in facts:
        snippet = row["evidence_snippet"]
        ref = row["evidence_ref"]
        if (snippet and str(snippet).strip()) or (ref and str(ref).strip()):
            with_evidence += 1
    if not facts:
        return Dimension("evidence", 0.0, "no facts to back with evidence")
    value = with_evidence / len(facts)
    return Dimension(
        "evidence", value, f"{with_evidence}/{len(facts)} fact(s) carry evidence_snippet or evidence_ref"
    )


def _freshness(facts: list[Any], now: dt.datetime | None) -> Dimension:
    """How recently the store was observed, bucketed, never guessed."""
    now = now or dt.datetime.now(dt.timezone.utc)
    newest: dt.datetime | None = None
    for row in facts:
        for key in ("observed_at", "scraped_at"):
            stamp = _parse_timestamp(row[key])
            if stamp is not None and (newest is None or stamp > newest):
                newest = stamp
    if newest is None:
        return Dimension("freshness", 0.0, "no observed_at or scraped_at on any fact")
    age_days = max((now - newest).total_seconds() / 86400.0, 0.0)
    value = FRESHNESS_FLOOR
    for max_days, bucket_value in FRESHNESS_BUCKETS:
        if age_days <= max_days:
            value = bucket_value
            break
    return Dimension(
        "freshness", value, f"last observed {age_days:.1f} day(s) ago ({newest.date().isoformat()})"
    )


def _conflict(facts: list[Any]) -> Dimension:
    """One minus the conflict penalty. Conflicts are counted, not hidden.

    Each ``uncertain`` fact costs ``UNCERTAIN_PENALTY`` and each ``invalid``
    one ``INVALID_PENALTY``; the reason names every offending field so a
    reader can see what disagreed.
    """
    uncertain_fields: list[str] = []
    invalid_fields: list[str] = []
    for row in facts:
        status = str(row["validation_status"] or "")
        field_name = str(row["field"])
        if status == "uncertain" and field_name not in uncertain_fields:
            uncertain_fields.append(field_name)
        elif status == "invalid" and field_name not in invalid_fields:
            invalid_fields.append(field_name)
    penalty = UNCERTAIN_PENALTY * len(uncertain_fields) + INVALID_PENALTY * len(invalid_fields)
    value = max(1.0 - penalty, 0.0)
    if not uncertain_fields and not invalid_fields:
        return Dimension("conflict", 1.0, "no conflicting or invalid facts")
    reason = (
        f"{len(uncertain_fields)} uncertain field(s)"
        + (": " + ", ".join(sorted(uncertain_fields)) if uncertain_fields else "")
        + f"; {len(invalid_fields)} invalid field(s)"
        + (": " + ", ".join(sorted(invalid_fields)) if invalid_fields else "")
    )
    return Dimension("conflict", value, reason)


def score_entity(
    entity_id: int,
    facts: list[Any],
    *,
    requested_fields: Iterable[str],
    stable_key: str | None = None,
    now: dt.datetime | None = None,
) -> EntityScore:
    """Score one entity (one group of merged stores) from its field facts.

    ``facts`` must be every fact of every entity in the group — the caller
    owns the grouping, so a merged store is scored on all its sources'
    evidence rather than the survivor's row alone.
    """
    dimensions = (
        _completeness(facts, requested_fields),
        _validation(facts),
        _evidence(facts),
        _freshness(facts, now),
        _conflict(facts),
    )
    composite = 0.0
    for dimension in dimensions:
        composite += DIMENSION_WEIGHTS[dimension.name] * dimension.value
    return EntityScore(
        entity_id=entity_id,
        stable_key=stable_key,
        dimensions=dimensions,
        composite=max(0.0, min(1.0, composite)),
    )


def score_run(
    conn: sqlite3.Connection,
    run_id: int,
    cfg,
    *,
    now: dt.datetime | None = None,
) -> dict[int, EntityScore]:
    """Score every store of a run, keyed by the entity id the export shows.

    The grouping mirrors ``export.entity_rows``: entities merged into a
    survivor are scored together under the survivor's id, so the score
    describes the row a reader will actually see.
    """
    from . import db

    requested = list(cfg.collect_fields)
    canonical = db.canonical_map(conn)

    facts_by_entity: dict[int, list[Any]] = {}
    for row in db.iter_run_facts(conn, run_id):
        facts_by_entity.setdefault(int(row["entity_id"]), []).append(row)

    scores: dict[int, EntityScore] = {}
    rows_by_id = {int(entity["id"]): entity for entity in db.iter_run_entities(conn, run_id)}
    for entity_id, entity in rows_by_id.items():
        survivor = canonical.get(entity_id, entity_id)
        if survivor in scores:
            continue  # a merged member is scored with its survivor, not twice
        group_facts: list[Any] = []
        for member_id, rows in facts_by_entity.items():
            if canonical.get(member_id, member_id) == survivor:
                group_facts.extend(rows)
        # The stable key shown is the survivor's, even when a member was met
        # first — the export shows the survivor's row, so must the score.
        scores[survivor] = score_entity(
            survivor,
            group_facts,
            requested_fields=requested,
            stable_key=rows_by_id[survivor]["stable_key"],
            now=now,
        )
    return scores


__all__ = [
    "CHANGELOG",
    "DIMENSION_WEIGHTS",
    "Dimension",
    "EntityScore",
    "FRESHNESS_BUCKETS",
    "SCORER_VERSION",
    "score_entity",
    "score_run",
]
