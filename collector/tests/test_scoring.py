"""Stage F scoring: five published dimensions, computed from evidence only."""

from __future__ import annotations

import datetime as dt
import json
import unittest

from collector import db, ingest, merge, scoring
from collector.scoring import DIMENSION_WEIGHTS, SCORER_VERSION

from . import support


def fact_row(**overrides):
    """A field_facts-shaped row. sqlite3.Row is duck-typed by name access."""
    base = {
        "entity_id": 1,
        "field": "title",
        "raw_value": "Praxis Beispiel",
        "normalized_value": "praxis beispiel",
        "provider": "google_maps",
        "source_url": "https://www.google.com/maps/place/x",
        "query_context": "dentists in Berlin",
        "scraped_at": "2026-10-01T10:00:00+00:00",
        "evidence_snippet": "title from listing card",
        "evidence_ref": "discover-a1.jsonl:2",
        "extractor_version": "go-1.0",
        "validation_status": "valid",
        "validation_note": None,
        "validation_state": "valid",
        "observed_at": "2026-10-01T10:00:00+00:00",
    }
    base.update(overrides)
    return base


NOW = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=dt.timezone.utc)


class CompletenessTest(unittest.TestCase):
    def test_counts_requested_fields_only(self):
        facts = [fact_row(field="title"), fact_row(field="phone", raw_value="+49 30 1")]
        dimension = scoring._completeness(facts, ["title", "address", "phone"])
        self.assertAlmostEqual(dimension.value, 2 / 3)
        self.assertIn("address", dimension.reason)

    def test_an_empty_value_does_not_count_as_filled(self):
        facts = [fact_row(field="title", raw_value="   ", normalized_value="")]
        dimension = scoring._completeness(facts, ["title"])
        self.assertEqual(dimension.value, 0.0)
        self.assertIn("missing: title", dimension.reason)

    def test_nothing_requested_is_complete(self):
        dimension = scoring._completeness([], [])
        self.assertEqual(dimension.value, 1.0)
        self.assertIn("no fields requested", dimension.reason)


class ValidationTest(unittest.TestCase):
    def test_uncertain_counts_half_and_invalid_zero(self):
        facts = [
            fact_row(field="a", validation_status="valid"),
            fact_row(field="b", validation_status="uncertain"),
            fact_row(field="c", validation_status="invalid"),
            fact_row(field="d", validation_status="missing"),
        ]
        dimension = scoring._validation(facts)
        self.assertAlmostEqual(dimension.value, 1.5 / 3)
        self.assertIn("1 valid, 1 uncertain, 1 invalid", dimension.reason)

    def test_no_judged_facts_reports_instead_of_zero_confidence(self):
        dimension = scoring._validation([fact_row(validation_status="missing")])
        self.assertEqual(dimension.value, 0.0)
        self.assertIn("no validated facts", dimension.reason)


class EvidenceTest(unittest.TestCase):
    def test_share_of_facts_with_evidence(self):
        facts = [fact_row(), fact_row(evidence_snippet=None, evidence_ref=None)]
        dimension = scoring._evidence(facts)
        self.assertAlmostEqual(dimension.value, 0.5)
        self.assertIn("1/2", dimension.reason)


class FreshnessTest(unittest.TestCase):
    def test_buckets_are_published(self):
        # The newest of observed_at / scraped_at wins, so the variant stamp is
        # written into both slots.
        cases = [
            ("2026-10-09T00:00:00+00:00", 1.0),   # 3.5 days
            ("2026-09-20T00:00:00+00:00", 0.8),   # 22 days
            ("2026-08-20T00:00:00+00:00", 0.5),   # 53 days
            ("2026-02-01T00:00:00+00:00", 0.3),   # 253 days
            ("2024-01-01T00:00:00+00:00", 0.1),   # over a year
        ]
        for stamp, expected in cases:
            with self.subTest(stamp=stamp):
                fact = fact_row(observed_at=stamp, scraped_at=stamp)
                dimension = scoring._freshness([fact], NOW)
                self.assertAlmostEqual(dimension.value, expected)

    def test_scraped_at_is_the_fallback_when_observed_at_is_absent(self):
        fact = fact_row(observed_at=None, scraped_at="2026-10-01T00:00:00+00:00")
        dimension = scoring._freshness([fact], NOW)
        self.assertAlmostEqual(dimension.value, 0.8)  # 11.5 days
        self.assertIn("last observed", dimension.reason)

    def test_no_timestamp_is_not_guessed(self):
        dimension = scoring._freshness([fact_row(scraped_at=None, observed_at=None)], NOW)
        self.assertEqual(dimension.value, 0.0)
        self.assertIn("no observed_at", dimension.reason)


class ConflictTest(unittest.TestCase):
    def test_penalties_name_the_fields(self):
        facts = [
            fact_row(field="phone", validation_status="uncertain"),
            fact_row(field="website", validation_status="invalid"),
        ]
        dimension = scoring._conflict(facts)
        self.assertAlmostEqual(dimension.value, 1.0 - 0.25 - 0.15)
        self.assertIn("phone", dimension.reason)
        self.assertIn("website", dimension.reason)

    def test_clean_store_scores_one(self):
        dimension = scoring._conflict([fact_row(), fact_row(field="phone")])
        self.assertEqual(dimension.value, 1.0)
        self.assertIn("no conflicting", dimension.reason)

    def test_many_conflicts_floor_at_zero(self):
        facts = [fact_row(field=f"f{i}", validation_status="uncertain") for i in range(9)]
        self.assertEqual(scoring._conflict(facts).value, 0.0)


class CompositeTest(unittest.TestCase):
    def test_weights_are_public_and_sum_to_one(self):
        self.assertAlmostEqual(sum(DIMENSION_WEIGHTS.values()), 1.0)

    def test_composite_is_the_published_mean(self):
        facts = [
            fact_row(field="title"),
            fact_row(field="phone", raw_value="+49 30 1234567"),
        ]
        score = scoring.score_entity(
            1, facts, requested_fields=["title", "phone"], stable_key="data_id:x", now=NOW
        )
        expected = sum(DIMENSION_WEIGHTS[d.name] * d.value for d in score.dimensions)
        self.assertAlmostEqual(score.composite, expected)
        payload = score.as_dict()
        self.assertEqual(payload["scorer_version"], SCORER_VERSION)
        self.assertEqual(payload["stable_key"], "data_id:x")
        for name in DIMENSION_WEIGHTS:
            self.assertIn(f"score_{name}", payload)
            self.assertIn(f"score_{name}_reason", payload)


class ScoreRunTest(unittest.TestCase):
    """The scoring must see the same merged groups the export shows."""

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)
        self.run_id = db.create_run(
            self.conn,
            fingerprint="fp", config_json="{}", config_source="<test>",
            collector_version="0.0.0", schema_version=1, go_binary="stub",
            go_version=None, target_count=10,
        )
        self.cfg = support.config()

    def ingest_records(self, provider, records, name):
        path = self.tmp / "raw" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                envelope = support.envelope(record, provider=provider)
                handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        return path

    def test_a_merged_store_is_scored_once_over_both_sources(self):
        self.ingest_records(
            "google_maps",
            [{"title": "Praxis Dr. M\u00fcller", "address": "Hauptstr. 5, 10115 Berlin",
              "phone": "+49 30 40367600", "data_id": "s1", "latitude": 52.52,
              "longitude": 13.405}],
            "google.jsonl",
        )
        self.ingest_records(
            "openstreetmap",
            [{"title": "Zahnarztpraxis M\u00fcller", "address": "Hauptstra\u00dfe 5, 10115 Berlin",
              "phone": "030 40367600", "source_id": "node/1", "source_id_kind": "osm",
              "latitude": 52.5205, "longitude": 13.4055}],
            "osm.jsonl",
        )
        ingest.ingest_directory(self.conn, self.tmp / "raw", run_id=self.run_id, cfg=self.cfg)
        stats = merge.merge_run(self.conn, self.run_id, self.cfg)
        self.assertEqual(stats.merged, 1)

        scores = scoring.score_run(self.conn, self.run_id, self.cfg, now=NOW)
        self.assertEqual(len(scores), 1)
        (score,) = scores.values()
        # Both sources describe the store, so the evidence dimension sees
        # facts from two providers and the completeness counts every field
        # either source supplied.
        evidence = next(d for d in score.dimensions if d.name == "evidence")
        self.assertGreater(evidence.value, 0.0)
        self.assertEqual(score.scorer_version, SCORER_VERSION)


if __name__ == "__main__":
    unittest.main()
