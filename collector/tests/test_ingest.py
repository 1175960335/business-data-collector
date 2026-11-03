"""Raw-file ingestion: parsing, de-duplication, evidence and missing values."""

from __future__ import annotations

import json
import unittest

from collector import db, ingest

from . import support


class IngestTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)
        self.run_id = db.create_run(
            self.conn,
            fingerprint="fp",
            config_json="{}",
            config_source="<test>",
            collector_version="0.0.0",
            schema_version=1,
            go_binary="stub",
            go_version=None,
            target_count=10,
        )
        self.raw_dir = self.tmp / "raw"
        self.raw_dir.mkdir()

    def write_raw(self, lines, name="discover-a1.jsonl") -> "object":
        path = self.raw_dir / name
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                if isinstance(line, str):
                    handle.write(line + "\n")
                else:
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        return path

    def ingest(self, cfg=None, path=None):
        cfg = cfg or support.config()
        target = path or self.raw_dir
        if target.is_dir():
            return ingest.ingest_directory(self.conn, target, run_id=self.run_id, cfg=cfg)
        return ingest.ingest_file(self.conn, target, run_id=self.run_id, cfg=cfg)


class ParsingTest(IngestTestCase):
    def test_enveloped_object_line(self):
        self.write_raw([support.envelope(support.raw_record())])
        stats = self.ingest()
        self.assertEqual(stats.observations_new, 1)
        self.assertEqual(stats.entities_new, 1)

        row = self.conn.execute("SELECT * FROM observations").fetchone()
        self.assertEqual(row["query"], "dentists in Berlin")
        self.assertEqual(row["mode"], "fast")
        self.assertEqual(row["record_schema"], "osint.record/1")

    def test_fast_mode_array_line_yields_one_observation_per_element(self):
        line = [support.envelope(support.raw_record(data_id="d1")),
                support.envelope(support.raw_record(data_id="d2"))]
        self.write_raw([line])
        stats = self.ingest()
        self.assertEqual(stats.observations_new, 2)
        self.assertEqual(stats.entities_new, 2)

    def test_plain_object_without_envelope_is_still_ingested(self):
        self.write_raw([support.raw_record()])
        stats = self.ingest()
        self.assertEqual(stats.observations_new, 1)
        row = self.conn.execute("SELECT * FROM observations").fetchone()
        self.assertIsNone(row["query"])
        self.assertIsNone(row["record_schema"])

    def test_malformed_line_is_counted_and_not_fatal(self):
        self.write_raw([support.envelope(support.raw_record()), '{"broken": '])
        stats = self.ingest()
        self.assertEqual(stats.malformed_lines, 1)
        self.assertEqual(stats.observations_new, 1)
        self.assertEqual(stats.malformed_examples, [("discover-a1.jsonl", 2)])

    def test_truncated_trailing_line_from_an_interrupted_run(self):
        path = self.raw_dir / "discover-a1.jsonl"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(support.envelope(support.raw_record())) + "\n")
            handle.write('{"schema": "osint.record/1", "conte')
        stats = self.ingest()
        self.assertEqual(stats.observations_new, 1)
        self.assertEqual(stats.malformed_lines, 1)

    def test_non_object_array_elements_are_skipped(self):
        self.write_raw([[support.envelope(support.raw_record()), 5, None]])
        stats = self.ingest()
        self.assertEqual(stats.skipped_elements, 2)
        self.assertEqual(stats.observations_new, 1)


class IdentityTest(IngestTestCase):
    def test_record_without_identity_is_reported_not_merged(self):
        raw = support.raw_record()
        for key in ("place_id", "cid", "data_id", "link"):
            raw.pop(key, None)
        self.write_raw([support.envelope(raw)])
        stats = self.ingest()

        self.assertEqual(stats.missing_identity, 1)
        self.assertEqual(stats.entities_new, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 0)

        error = self.conn.execute("SELECT * FROM errors").fetchone()
        self.assertEqual(error["kind"], "missing_identity")
        context = json.loads(error["context_json"])
        self.assertEqual(context["title"], "Praxis Beispiel")
        # NFKC folding turns '\u00df' into 'ss', so both spellings dedupe together.
        self.assertEqual(context["fallback_key"], "praxis beispiel|musterstrasse 1, 10115 berlin")

    def test_place_id_wins_over_weaker_identifiers(self):
        raw = support.raw_record(place_id="ChIJ-place", cid="123", data_id="d1", link="https://l")
        self.write_raw([support.envelope(raw)])
        self.ingest()
        row = self.conn.execute("SELECT * FROM entities").fetchone()
        self.assertEqual(row["stable_key"], "place_id:ChIJ-place")
        self.assertEqual(row["id_kind"], "place_id")

    def test_same_business_twice_is_one_entity(self):
        self.write_raw([support.envelope(support.raw_record())], "a.jsonl")
        self.write_raw([support.envelope(support.raw_record())], "b.jsonl")
        stats = self.ingest()
        self.assertEqual(stats.entities_new, 1)
        self.assertEqual(stats.entities_seen, 1)
        self.assertEqual(db.count_run_entities(self.conn, self.run_id), 1)

    def test_the_envelope_top_level_provider_is_honoured(self):
        # Regression found by the stage E acceptance run: every real provider
        # writes "provider" at the envelope's top level, and ingest read only
        # context["provider"] — so OpenStreetMap records resolved as
        # google_maps and borrowed Google's identity namespace.
        raw = support.raw_record()
        raw.pop("data_id")
        raw["source_id"] = "node/1"
        raw["source_id_kind"] = "osm"
        envelope = support.envelope(raw)
        del envelope["context"]["provider"]
        envelope["provider"] = "openstreetmap"
        self.write_raw([envelope])
        self.ingest()

        row = self.conn.execute("SELECT * FROM entities").fetchone()
        self.assertEqual(row["id_kind"], "openstreetmap:osm")
        self.assertEqual(row["stable_key"], "openstreetmap:osm:node/1")


class FieldFactTest(IngestTestCase):
    def facts(self, field: str):
        return list(
            self.conn.execute(
                "SELECT * FROM field_facts WHERE field = ? ORDER BY id", (field,)
            )
        )

    def test_facts_carry_provenance_and_evidence(self):
        self.write_raw([support.envelope(support.raw_record())])
        self.ingest()
        fact = self.facts("phone")[0]
        self.assertEqual(fact["raw_value"], "+49 30 12345678")
        self.assertEqual(fact["normalized_value"], "+493012345678")
        self.assertEqual(fact["provider"], "google_maps")
        self.assertEqual(fact["query_context"], "dentists in Berlin")
        self.assertEqual(fact["evidence_ref"], "discover-a1.jsonl#L1")
        self.assertEqual(fact["extractor_version"], "gmaps-entry/1")
        self.assertEqual(fact["validation_status"], "valid")

    def test_unicode_survives(self):
        raw = support.raw_record(title="Zahn\u00e4rzte Feuerlandh\u00f6fe \u2013 Dr. Vo\u00df")
        self.write_raw([support.envelope(raw)])
        self.ingest()
        row = self.conn.execute("SELECT name, name_norm FROM entities").fetchone()
        self.assertEqual(row["name"], "Zahn\u00e4rzte Feuerlandh\u00f6fe \u2013 Dr. Vo\u00df")
        self.assertIn("zahn\u00e4rzte", row["name_norm"])

    def test_missing_field_is_recorded_with_a_reason(self):
        raw = support.raw_record()
        raw.pop("phone")
        self.write_raw([support.envelope(raw)])
        self.ingest()
        fact = self.facts("phone")[0]
        self.assertIsNone(fact["raw_value"])
        self.assertIsNone(fact["normalized_value"])
        self.assertEqual(fact["validation_status"], "missing")
        self.assertIn("absent or empty", fact["validation_note"])

    def test_email_validation_statuses(self):
        raw = support.raw_record(emails=["gut@example.de", "kaputt@@example.de", "user@domain.com"])
        cfg = support.config({"collect": {"fields": ["title", "emails"]}})
        self.write_raw([support.envelope(raw)])
        self.ingest(cfg)

        statuses = {row["raw_value"]: row["validation_status"] for row in self.facts("emails")}
        self.assertEqual(statuses["gut@example.de"], "valid")
        self.assertEqual(statuses["kaputt@@example.de"], "invalid")
        self.assertEqual(statuses["user@domain.com"], "uncertain")
        self.assertIsNone(
            [row for row in self.facts("emails") if row["raw_value"] == "kaputt@@example.de"][0][
                "normalized_value"
            ]
        )

    def test_short_phone_is_invalid(self):
        self.write_raw([support.envelope(support.raw_record(phone="12345"))])
        self.ingest()
        self.assertEqual(self.facts("phone")[0]["validation_status"], "invalid")

    def test_phone_without_country_prefix_is_uncertain_only_when_asked(self):
        raw = support.raw_record(phone="030 1234567")
        path = self.write_raw([support.envelope(raw)], "a.jsonl")
        self.ingest()
        self.assertEqual(self.facts("phone")[0]["validation_status"], "valid")

        self.conn.execute("DELETE FROM field_facts")
        self.conn.commit()
        cfg = support.config({"validation": {"rules": {"require_country_phone": True}}})
        ingest.ingest_file(self.conn, path, run_id=self.run_id, cfg=cfg)
        self.assertEqual(self.facts("phone")[0]["validation_status"], "uncertain")

    def test_field_not_requested_produces_no_fact(self):
        cfg = support.config({"collect": {"fields": ["title"]},
                              "export": {"fields": ["entity_id", "title"]}})
        self.write_raw([support.envelope(support.raw_record())])
        self.ingest(cfg)
        self.assertEqual(self.facts("phone"), [])
        self.assertTrue(self.facts("title"))


class IdempotencyTest(IngestTestCase):
    def test_reingesting_the_same_file_adds_nothing(self):
        self.write_raw([support.envelope(support.raw_record())])
        first = self.ingest()
        self.assertEqual(first.observations_new, 1)
        self.assertGreater(first.facts_new, 0)

        second = ingest.ingest_file(
            self.conn, self.raw_dir / "discover-a1.jsonl", run_id=self.run_id, cfg=support.config()
        )
        self.assertEqual(second.observations_new, 0)
        self.assertEqual(second.observations_repeated, 1)
        self.assertEqual(second.facts_new, 0)
        self.assertEqual(second.entities_new, 0)
        self.assertEqual(second.entities_seen, 1)

    def test_directory_ingest_skips_already_processed_lines(self):
        self.write_raw([support.envelope(support.raw_record())])
        self.ingest()
        again = self.ingest()
        self.assertEqual(again.lines, 0)
        self.assertEqual(again.observations_new, 0)

    def test_new_evidence_for_a_known_business_is_added(self):
        cfg = support.config({"collect": {"fields": ["title", "address", "review_count"]}})
        self.write_raw([support.envelope(support.raw_record(review_count=10))], "a.jsonl")
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)
        self.write_raw([support.envelope(support.raw_record(review_count=99))], "b.jsonl")
        stats = ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        self.assertEqual(stats.entities_new, 0)
        self.assertEqual(stats.entities_seen, 1)
        self.assertEqual(stats.observations_new, 1)

        values = [row["raw_value"] for row in self.conn.execute(
            "SELECT raw_value FROM field_facts WHERE field = 'review_count' ORDER BY id"
        )]
        self.assertEqual(values, ["10", "99"])


class AbsentDefaultTest(IngestTestCase):
    """A provider's "did not fetch it" placeholder is stored as missing.

    The Go scraper parses a Google review count into an ``int`` that defaults to
    0 and only writes it to its own database when it is ``> 0`` — its fast mode
    does not fetch a count at all. Recording that 0 as a *valid* review count is
    the "把缺失默认0当已核实" failure the task book names.
    """

    def fact(self, field="review_count"):
        return self.conn.execute(
            "SELECT raw_value, validation_status, validation_note FROM field_facts "
            "WHERE field = ?",
            (field,),
        ).fetchone()

    def test_a_google_zero_is_stored_as_missing_but_keeps_the_raw_text(self):
        cfg = support.config({"collect": {"fields": ["title", "review_count"]}})
        self.write_raw([support.envelope(support.raw_record(review_count=0))])
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        row = self.fact()
        self.assertEqual(row["validation_status"], "missing")
        # The raw text is kept so the evidence is still inspectable.
        self.assertEqual(row["raw_value"], "0")
        self.assertIn("absent-default", row["validation_note"])

    def test_a_real_count_is_unaffected(self):
        cfg = support.config({"collect": {"fields": ["title", "review_count"]}})
        self.write_raw([support.envelope(support.raw_record(review_count=7))])
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        row = self.fact()
        self.assertEqual(row["validation_status"], "valid")
        self.assertEqual(row["raw_value"], "7")

    def test_a_zero_from_another_source_is_a_real_value(self):
        # The rule is keyed on the exact provider: a source that does measure a
        # count is not second-guessed.
        cfg = support.config({"collect": {"fields": ["title", "review_count"]}})
        self.write_raw(
            [
                support.envelope(
                    support.raw_record(data_id=None, source_id="node/1", review_count=0),
                    provider="openstreetmap",
                )
            ]
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        row = self.fact()
        self.assertEqual(row["validation_status"], "valid")
        self.assertEqual(row["raw_value"], "0")


class NameShapedAddressIngestTest(IngestTestCase):
    """048: the address slot holding the business's own name is not an address.

    Wikidata's P6375 for Q30256951 is "geomer GmbH". Storing that as the entity's
    address would make the ownership judgement treat the *name* as a second,
    independent signal -- which is exactly the conjunction that rule exists to
    enforce. The raw text is kept; the claim that it is an address is not.
    """

    def entity(self):
        return self.conn.execute("SELECT name, address, address_norm FROM entities").fetchone()

    def address_fact(self):
        return self.conn.execute(
            "SELECT raw_value, normalized_value, validation_status, validation_note "
            "FROM field_facts WHERE field = 'address'"
        ).fetchone()

    def test_the_name_is_not_stored_as_the_entity_address(self):
        cfg = support.config({"collect": {"fields": ["title", "address"]}})
        self.write_raw(
            [
                support.envelope(
                    support.raw_record(title="geomer GmbH", address="geomer GmbH")
                )
            ]
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        entity = self.entity()
        self.assertEqual(entity["name"], "geomer GmbH")
        self.assertIsNone(entity["address"])
        self.assertIsNone(entity["address_norm"])

    def test_the_fact_keeps_the_raw_text_and_says_why_it_is_missing(self):
        cfg = support.config({"collect": {"fields": ["title", "address"]}})
        self.write_raw(
            [
                support.envelope(
                    support.raw_record(title="geomer GmbH", address="geomer GmbH")
                )
            ]
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        fact = self.address_fact()
        # 050: 待确认, not 缺失 and not 冲突. The source *did* write a value; what
        # it wrote is not an address, which is a different statement from "the
        # source said nothing" and a different one again from "two values
        # disagree".
        self.assertEqual(fact["validation_status"], "uncertain")
        self.assertEqual(fact["raw_value"], "geomer GmbH")
        self.assertIsNone(fact["normalized_value"])
        self.assertIn("企业名称", fact["validation_note"])

    def test_the_state_names_the_reason(self):
        cfg = support.config({"collect": {"fields": ["title", "address"]}})
        self.write_raw(
            [
                support.envelope(
                    support.raw_record(title="geomer GmbH", address="geomer GmbH")
                )
            ]
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        state = self.conn.execute(
            "SELECT validation_state FROM field_facts WHERE field = 'address'"
        ).fetchone()["validation_state"]
        self.assertEqual(state, "name_not_an_address")

    def test_a_digit_in_the_company_name_does_not_save_it(self):
        # 050 P0: the 048 digit gate is gone, so "Hansa 12 GmbH" in the address
        # slot of "Hansa 12 GmbH" is caught here too.
        cfg = support.config({"collect": {"fields": ["title", "address"]}})
        self.write_raw(
            [
                support.envelope(
                    support.raw_record(title="Hansa 12 GmbH", address="Hansa 12 GmbH")
                )
            ]
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        self.assertIsNone(self.entity()["address"])
        fact = self.address_fact()
        self.assertEqual(fact["validation_status"], "uncertain")
        self.assertEqual(fact["raw_value"], "Hansa 12 GmbH")

    def test_a_digitless_address_the_source_stated_is_kept(self):
        # 050: no blanket "no house number means bad address". This address is
        # nobody's name, so it is stored as the source stated it.
        cfg = support.config({"collect": {"fields": ["title", "address"]}})
        self.write_raw(
            [
                support.envelope(
                    support.raw_record(
                        title="Metallbau Windeck GmbH", address="Rietzer Berg, Kloster Lehnin"
                    )
                )
            ]
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        self.assertEqual(self.entity()["address"], "Rietzer Berg, Kloster Lehnin")
        self.assertEqual(self.address_fact()["validation_status"], "unverified")

    def test_the_raw_file_still_carries_the_source_value(self):
        # The evidence is the source's own statement: "the source wrote this".
        # It stays verbatim in the raw file and in the observation payload.
        cfg = support.config({"collect": {"fields": ["title", "address"]}})
        self.write_raw(
            [
                support.envelope(
                    support.raw_record(title="geomer GmbH", address="geomer GmbH")
                )
            ]
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        row = self.conn.execute("SELECT raw_json FROM observations").fetchone()
        self.assertIn("geomer GmbH", row["raw_json"])

    def test_a_real_address_is_stored_unchanged(self):
        cfg = support.config({"collect": {"fields": ["title", "address"]}})
        self.write_raw(
            [
                support.envelope(
                    support.raw_record(
                        title="Praxis M\u00fcller",
                        address="Musterstra\u00dfe 1, 10115 Berlin",
                    )
                )
            ]
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        self.assertEqual(self.entity()["address"], "Musterstra\u00dfe 1, 10115 Berlin")
        self.assertEqual(self.address_fact()["validation_status"], "unverified")

    def test_a_company_named_after_its_street_keeps_its_address(self):
        # The strict rule needs a house number to be absent *and* the value to
        # equal the name; a real address is not caught just because the two look
        # similar.
        cfg = support.config({"collect": {"fields": ["title", "address"]}})
        self.write_raw(
            [
                support.envelope(
                    support.raw_record(title="Rietzer Berg GmbH", address="Rietzer Berg 29")
                )
            ]
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

        self.assertEqual(self.entity()["address"], "Rietzer Berg 29")


class Provenance061Test(IngestTestCase):
    """061: how a row says it was obtained, and what stays unknown.

    The Go scraper writes its own envelope and states no provenance, so every
    Google row of a live run used to land with ``acquired_via`` NULL and the
    workbook printed "获取方式：未知 19" for a batch that had just been fetched.
    The method is now supplied by the caller that knows the provider -- and
    never guessed from the payload, which is what 026 P0 forbids.
    """

    def observation(self):
        return self.conn.execute("SELECT * FROM observations ORDER BY id LIMIT 1").fetchone()

    def test_a_file_the_orchestrator_attributes_to_a_live_provider_is_live(self):
        self.write_raw([support.envelope(support.raw_record())])
        ingest.ingest_one(
            self.conn,
            self.raw_dir / "discover-a1.jsonl",
            run_id=self.run_id,
            cfg=support.config(),
            default_acquired_via="live",
        )
        self.assertEqual(self.observation()["acquired_via"], "live")

    def test_a_stated_method_is_never_overwritten(self):
        self.write_raw(
            [
                support.envelope(
                    support.raw_record(), provenance={"acquired_via": "cache"}
                )
            ]
        )
        ingest.ingest_one(
            self.conn,
            self.raw_dir / "discover-a1.jsonl",
            run_id=self.run_id,
            cfg=support.config(),
            default_acquired_via="live",
        )
        self.assertEqual(self.observation()["acquired_via"], "cache")

    def test_a_file_with_nobody_to_attribute_it_to_stays_unknown(self):
        # The guard 026 P0 exists for: a raw file handed over from an older run
        # is not evidence that *this* run visited anything.
        self.write_raw([support.envelope(support.raw_record())])
        self.ingest()
        self.assertIsNone(self.observation()["acquired_via"])

    def test_the_scrape_time_is_not_turned_into_the_source_time(self):
        # The envelope's own stamp is not the collection time for an imported
        # row (034 第一批第 3 条); only a stated ``observed_at`` is.
        self.write_raw(
            [support.envelope(support.raw_record(), scraped_at="2026-10-16T12:10:08Z")]
        )
        self.ingest()
        self.assertIsNone(self.observation()["observed_at"])


class CategoryFallback061Test(IngestTestCase):
    """061: Google leaves ``category`` empty and puts the categories in a list.

    Twenty of twenty rows in a real run came back with no category while the
    payload said "Dog day care center", "Dog walker", "Dog trainer" -- the one
    piece of evidence the industry judgement is supposed to be based on.
    """

    def category_fact(self):
        return self.conn.execute(
            "SELECT * FROM field_facts WHERE field = 'category'"
        ).fetchone()

    def test_the_list_is_used_when_the_singular_key_is_blank(self):
        raw = support.raw_record(category="")
        raw["categories"] = ["Dog day care center", "Dog walker"]
        cfg = support.config({"collect": {"fields": ["title", "category"]}})
        self.write_raw([support.envelope(raw)])
        self.ingest(cfg)

        fact = self.category_fact()
        self.assertEqual(fact["raw_value"], "Dog day care center; Dog walker")
        # A free-text field is never "validated", only present: the point of the
        # fix is that it stops being recorded as missing.
        self.assertEqual(fact["validation_status"], "unverified")

    def test_a_stated_singular_category_wins(self):
        raw = support.raw_record(category="Zahnarzt")
        raw["categories"] = ["Dentist"]
        cfg = support.config({"collect": {"fields": ["title", "category"]}})
        self.write_raw([support.envelope(raw)])
        self.ingest(cfg)
        self.assertEqual(self.category_fact()["raw_value"], "Zahnarzt")

    def test_no_category_anywhere_is_still_missing(self):
        raw = support.raw_record(category="")
        cfg = support.config({"collect": {"fields": ["title", "category"]}})
        self.write_raw([support.envelope(raw)])
        self.ingest(cfg)

        fact = self.category_fact()
        self.assertIsNone(fact["raw_value"])
        self.assertEqual(fact["validation_status"], "missing")


if __name__ == "__main__":
    unittest.main()
