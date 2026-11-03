"""Cross-source entity merge (task book 004, stage E).

The acceptance list for stage E is specific:

* "同一门店跨源合并" — one store seen by two sources is one row;
* "同品牌不同地址保留" — two branches of one brand stay two rows;
* "非拉丁名称不被清空" — a non-Latin name survives, untransliterated;
* "冲突保留候选和合并原因" — the decision, its evidence and the losing
  candidate all survive, and the merge is reversible.

The rules that make those true live in ``merge.py`` and are tested here against
a real SQLite database, because the merge is a database operation: it writes
``entity_merges`` rows that the export relies on, and a test that only checked
the comparison helpers would not notice a broken join.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from collector import db, ingest, merge
from collector.merge import (
    Candidate,
    address_agrees,
    address_components,
    addresses_differ,
    collect_candidates,
    geo_distance_meters,
    name_similarity,
    phone_e164,
    phones_agree,
    same_store,
    street_key,
)

from . import support

#: The three providers used across the acceptance cases.
GOOGLE = "google_maps"
OSM = "openstreetmap"
CSV = "csv_list"


def candidate(
    entity_id,
    *,
    name="",
    phone="",
    address="",
    latitude=None,
    longitude=None,
    crs=None,
    domain="",
    id_kind="place_id",
):
    return Candidate(
        entity_id=entity_id,
        stable_key=f"{id_kind}:{entity_id}",
        id_kind=id_kind,
        provider="",
        name=name,
        name_norm=merge.normalize_text(name),
        phone=phone,
        phone_e164="",
        address=address,
        address_key=("", "", ""),
        latitude=latitude,
        longitude=longitude,
        crs=crs,
        domain=domain,
        fact_count=0,
        completeness=0,
    )


def ready(*candidates, country="DE"):
    """Resolve the derived fields the decision reads, as the stage does."""
    return collect_candidates(
        [
            {
                "id": c.entity_id,
                "stable_key": c.stable_key,
                "id_kind": c.id_kind,
                "name": c.name,
                "phone_norm": c.phone,
                "address": c.address,
                "latitude": c.latitude,
                "longitude": c.longitude,
                "crs": c.crs,
                "domain": c.domain,
            }
            for c in candidates
        ],
        country=country,
    )


# --- the individual comparisons ---------------------------------------------


class NameSimilarityTest(unittest.TestCase):
    def test_identical_names_agree_completely(self):
        self.assertEqual(name_similarity("Praxis Nord", "Praxis Nord"), 1.0)

    def test_a_prefix_of_the_same_name_still_agrees(self):
        # "Zahnarztpraxis Nord" contains "Praxis Nord": containment of a word is
        # weaker than a whole-string match, so the score is not a plain 1.0.
        score = name_similarity("Praxis Nord", "Zahnarztpraxis Nord")
        self.assertGreaterEqual(score, 0.82)

    def test_unrelated_names_do_not_agree(self):
        self.assertLess(name_similarity("Praxis Nord", "Baeckerei Sonne"), 0.82)

    def test_case_and_punctuation_do_not_matter(self):
        # Case folding is applied; the umlaut is *not* folded, because that
        # would be transliteration. The two spellings are therefore close but
        # not identical, which is why the name threshold is a threshold.
        score = name_similarity("Praxis Dr. M\u00fcller", "PRAXIS DR. M\u00dcLLER")
        self.assertEqual(score, 1.0)
        self.assertGreater(
            name_similarity("Praxis Dr. M\u00fcller", "praxis dr mueller"), 0.8
        )

    def test_an_empty_name_never_agrees(self):
        self.assertEqual(name_similarity("", "Praxis Nord"), 0.0)
        self.assertEqual(name_similarity(None, None), 0.0)


class StreetKeyTest(unittest.TestCase):
    """Abbreviations must not look like different streets."""

    def test_the_german_abbreviations_expand(self):
        # Without this, the branch guard fires on a spelling difference rather
        # than on a real second location.
        self.assertEqual(street_key("Hauptstr."), street_key("Hauptstrasse"))
        self.assertEqual(street_key("Hauptstra\u00dfe"), street_key("Hauptstrasse"))
        self.assertEqual(street_key("Hauptpl."), street_key("Hauptplatz"))
        self.assertEqual(street_key("Bahnhofstr."), street_key("Bahnhofstrasse"))

    def test_punctuation_and_spacing_are_removed(self):
        self.assertEqual(street_key("Muster Str. 1"), street_key("Musterstr1"))

    def test_a_non_latin_script_is_left_alone(self):
        # Folding it would be transliteration, which the task book forbids.
        self.assertEqual(street_key("\u56fd\u5e86\u8def88\u53f7"), "\u56fd\u5e86\u8def88\u53f7")

    def test_a_short_word_is_not_given_a_suffix(self):
        self.assertEqual(street_key("str"), "str")


class PhoneAgreementTest(unittest.TestCase):
    def test_the_same_number_in_two_formats_agrees(self):
        (a, b) = ready(
            candidate(1, phone="030 40367600"),
            candidate(2, phone="+49 30 40367600"),
        )
        self.assertTrue(phones_agree(a, b))

    def test_a_shared_switchboard_does_not_by_itself_merge(self):
        # Same name, same head-office number, different branch: the task book
        # says a customer-service line alone must not merge a whole chain.
        (a, b) = ready(
            candidate(1, name="Praxis Nord", phone="+49 30 9999999",
                      address="Hauptstr. 5, 10115 Berlin"),
            candidate(2, name="Praxis Nord", phone="+49 30 9999999",
                      address="S\u00fcdstr. 9, 10243 Berlin"),
        )
        ok, evidence, _score = same_store(a, b)
        self.assertFalse(ok)
        self.assertEqual(evidence, [merge.REASON_ADDRESS])

    def test_two_different_numbers_do_not_agree(self):
        (a, b) = ready(
            candidate(1, phone="+49 30 1111111"),
            candidate(2, phone="+49 30 2222222"),
        )
        self.assertFalse(phones_agree(a, b))

    def test_a_missing_number_never_agrees(self):
        (a, b) = ready(candidate(1), candidate(2, phone="+49 30 1111111"))
        self.assertFalse(phones_agree(a, b))

    def test_e164_is_preferred_when_both_sides_have_one(self):
        self.assertEqual(
            phone_e164("+49 30 40367600", "DE"), "+493040367600"
        )


class AddressAgreementTest(unittest.TestCase):
    def test_the_same_address_agrees_across_two_spellings(self):
        (a, b) = ready(
            candidate(1, address="Hauptstr. 5, 10115 Berlin"),
            candidate(2, address="Hauptstra\u00dfe 5, 10115 Berlin"),
        )
        self.assertTrue(address_agrees(a.address_key, b.address_key))
        self.assertFalse(addresses_differ(a.address_key, b.address_key))

    def test_a_different_house_number_is_a_different_address(self):
        (a, b) = ready(
            candidate(1, address="Hauptstr. 5, 10115 Berlin"),
            candidate(2, address="Hauptstr. 7, 10115 Berlin"),
        )
        self.assertFalse(address_agrees(a.address_key, b.address_key))
        self.assertTrue(addresses_differ(a.address_key, b.address_key))

    def test_a_different_postcode_is_a_different_address(self):
        (a, b) = ready(
            candidate(1, address="Hauptstr. 5, 10115 Berlin"),
            candidate(2, address="Hauptstr. 5, 10243 Berlin"),
        )
        self.assertTrue(addresses_differ(a.address_key, b.address_key))

    def test_a_materially_different_street_is_a_different_address(self):
        (a, b) = ready(
            candidate(1, address="Hauptstr. 5, 10115 Berlin"),
            candidate(2, address="Gartenweg 5, 10115 Berlin"),
        )
        self.assertTrue(addresses_differ(a.address_key, b.address_key))

    def test_an_empty_address_neither_agrees_nor_conflicts(self):
        self.assertFalse(address_agrees(("", "", ""), ("5", "hauptstrasse", "10115")))
        self.assertFalse(addresses_differ(("", "", ""), ("5", "hauptstrasse", "10115")))

    def test_a_house_number_the_parser_missed_is_still_compared(self):
        # "Friedrichstr, Atrium 60, 10117 Berlin" names the street first and
        # puts the number in a later comma part. The parser reads only the
        # first part as the street, so the number used to be dropped entirely
        # and an empty number was read as "no conflict" — which merged two
        # *different* shops on one street. This is the real false merge found
        # on live data: REWE Friedrichstraße 111 against REWE Atrium 60.
        (a, b) = ready(
            candidate(1, address="Friedrichstraße 111, 10117 Berlin, Germany"),
            candidate(2, address="Friedrichstr, Atrium 60, 10117 Berlin, Germany"),
        )
        self.assertFalse(address_agrees(a.address_key, b.address_key))
        self.assertTrue(addresses_differ(a.address_key, b.address_key))

    def test_an_annotated_address_keeps_its_house_number(self):
        (a,) = ready(
            candidate(
                1,
                address=(
                    "Parkplatz über Rochstraße zu erreichen, "
                    "Karl-Liebknecht-Str. 13, 10178 Berlin, Germany"
                ),
            ),
        )
        # The street is misread (the parser takes the leading annotation), but
        # the house number is recovered so the guard can still compare it.
        self.assertEqual(a.address_key[0], "13")

    def test_an_address_with_no_house_number_still_has_none(self):
        # The recovery must not invent a number out of the postcode.
        self.assertEqual(
            address_components("Karl-Liebknecht-Straße, 10178 Berlin", "DE"),
            ("", "karl-liebknecht-strasse", "10178"),
        )

    def test_a_name_shaped_address_is_not_an_address_key(self):
        # 050 P0: the house number *inside a company's own name* must not become
        # the independent confirmation. Two records whose address slot holds
        # their own name would otherwise agree on that number and be merged on
        # the strength of the name, twice.
        (a, b) = ready(
            candidate(1, name="Hansa 12 GmbH", address="Hansa 12 GmbH"),
            candidate(2, name="Hansa 12 GmbH", address="Hansa 12 GmbH"),
        )
        self.assertEqual(a.address_key, ("", "", ""))
        self.assertEqual(b.address_key, ("", "", ""))
        self.assertFalse(address_agrees(a.address_key, b.address_key))

    def test_a_name_shaped_address_does_not_merge_two_records(self):
        # Same name, no phone, and an "address" that is the name on both sides:
        # nothing independent agrees, so nothing merges.
        (a, b) = ready(
            candidate(1, name="Hansa 12 GmbH", address="Hansa 12 GmbH"),
            candidate(2, name="Hansa 12 GmbH", address="Hansa 12 GmbH"),
        )
        merge_ok, evidence, _score = same_store(a, b, country="DE")
        self.assertFalse(merge_ok)
        self.assertEqual(evidence, [])

    def test_a_real_address_on_both_sides_still_merges(self):
        (a, b) = ready(
            candidate(1, name="Hansa 12 GmbH", address="Hafenstraße 12, 20359 Hamburg"),
            candidate(2, name="Hansa 12 GmbH", address="Hafenstraße 12, 20359 Hamburg"),
        )
        self.assertTrue(any(a.address_key))
        merge_ok, evidence, _score = same_store(a, b, country="DE")
        self.assertTrue(merge_ok)
        self.assertIn("address", evidence)


class GeoDistanceTest(unittest.TestCase):
    def test_the_same_place_is_zero_metres_apart(self):
        self.assertEqual(
            geo_distance_meters(52.52, 13.40, 52.52, 13.40), 0.0
        )

    def test_a_known_distance_is_metered(self):
        # 52.52/13.40 to 52.521/13.401 is roughly 130-150 m in Berlin.
        distance = geo_distance_meters(52.52, 13.40, 52.521, 13.401)
        self.assertGreater(distance, 100.0)
        self.assertLess(distance, 200.0)

    def test_a_missing_coordinate_cannot_be_metered(self):
        self.assertIsNone(geo_distance_meters(None, 13.40, 52.52, 13.40))


class CoordinateSystemTest(unittest.TestCase):
    """Task book 019 D: "跨坐标系不一致的坐标不得直接比较距离".

    A Chinese map provider returns GCJ-02 (Amap) or BD-09 (Baidu) while Google
    Maps and OSM return WGS84; the same shop is 300-500 m apart between them,
    which is far wider than the 120 m tolerance. Comparing anyway would not be
    "a weaker signal" — it would be a wrong one.
    """

    def test_the_same_coordinates_in_different_systems_do_not_agree(self):
        a = ready(candidate(1, name="宠物医院", latitude=30.2741, longitude=120.1551,
                            crs="WGS84"))
        b = ready(candidate(2, name="宠物医院", latitude=30.2741, longitude=120.1551,
                            crs="GCJ-02"))
        agrees, evidence, _ = same_store(a[0], b[0])
        self.assertFalse(agrees)
        self.assertNotIn("geography", evidence)

    def test_the_same_coordinates_in_one_system_do_agree(self):
        a = ready(candidate(1, name="Pet Clinic", latitude=30.2741, longitude=120.1551,
                            crs="WGS84"))
        b = ready(candidate(2, name="Pet Clinic", latitude=30.2741, longitude=120.1551,
                            crs="WGS84"))
        agrees, evidence, _ = same_store(a[0], b[0])
        self.assertTrue(agrees)
        self.assertIn("geography", evidence)

    def test_an_undeclared_system_is_not_treated_as_a_conflict(self):
        # Sources that publish no coordinates at all declare nothing, and two
        # of those are simply not comparable by distance — not in conflict.
        a = ready(candidate(1, name="Pet Clinic", latitude=30.2741, longitude=120.1551))
        b = ready(candidate(2, name="Pet Clinic", latitude=30.2741, longitude=120.1551))
        agrees, evidence, _ = same_store(a[0], b[0])
        self.assertTrue(agrees)
        self.assertIn("geography", evidence)

    def test_different_systems_are_not_even_blocked_together(self):
        rows = [
            {"id": 1, "stable_key": "a:1", "id_kind": "place_id", "name": "宠物医院",
             "latitude": 30.2741, "longitude": 120.1551, "crs": "WGS84"},
            {"id": 2, "stable_key": "b:2", "id_kind": "place_id", "name": "宠物医院",
             "latitude": 30.2741, "longitude": 120.1551, "crs": "GCJ-02"},
        ]
        candidates = collect_candidates(rows, country="CN")
        self.assertEqual(candidates[0].crs, "WGS84")
        self.assertEqual(candidates[1].crs, "GCJ-02")


# --- the decision itself ----------------------------------------------------


class SameStoreTest(unittest.TestCase):
    """Task book: "同一门店跨源合并" / "同品牌不同地址保留"."""

    def decide(self, left, right, **kw):
        (a, b) = ready(left, right)
        return same_store(a, b, **kw)

    def test_one_store_seen_by_two_sources_is_merged(self):
        # The exact case stage E exists for: Google says one thing, OSM another,
        # and both are true of the same shop.
        ok, evidence, score = self.decide(
            candidate(1, name="Praxis Dr. M\u00fcller",
                      phone="+49 30 40367600",
                      address="Hauptstr. 5, 10115 Berlin",
                      latitude=52.5201, longitude=13.4052),
            candidate(2, name="Zahnarztpraxis M\u00fcller",
                      phone="030 40367600",
                      address="Hauptstra\u00dfe 5, 10115 Berlin",
                      latitude=52.5202, longitude=13.4053),
        )
        self.assertTrue(ok)
        self.assertIn(merge.REASON_PHONE, evidence)
        self.assertIn(merge.REASON_ADDRESS, evidence)
        self.assertIn(merge.REASON_GEO, evidence)
        self.assertGreater(score, 1.0)

    def test_two_branches_of_one_brand_stay_separate(self):
        # Same name, same switchboard, two different addresses: the signature
        # of a chain, which the task book requires to stay two rows.
        ok, evidence, _score = self.decide(
            candidate(1, name="Praxis Nord", phone="+49 30 9999999",
                      address="Hauptstr. 5, 10115 Berlin"),
            candidate(2, name="Praxis Nord", phone="+49 30 9999999",
                      address="S\u00fcdstr. 9, 10243 Berlin"),
        )
        self.assertFalse(ok)
        self.assertEqual(evidence, [merge.REASON_ADDRESS])

    def test_a_name_on_its_own_never_merges(self):
        ok, evidence, _score = self.decide(
            candidate(1, name="Praxis Nord", address="Hauptstr. 5, 10115 Berlin"),
            candidate(2, name="Praxis Nord", address="Hauptstr. 5, 10115 Berlin"),
        )
        # Two records with the same name and address but no other evidence are
        # not merged on the name alone: the phone and the address both agree,
        # so this one *does* merge — the pure-name case is below.
        self.assertTrue(ok)

        ok, _evidence, _score = self.decide(
            candidate(1, name="Praxis Nord"),
            candidate(2, name="Praxis Nord"),
        )
        self.assertFalse(ok)

    def test_a_record_missing_phone_and_address_is_not_force_merged(self):
        # 034 第一批第 4 条："对缺失电话/地址的记录不强行合并"。一条只有名字的记录
        # 和一条信息完整的记录同名，也不能据此合并——缺字段不是"一致"。
        ok, evidence, _score = self.decide(
            candidate(1, name="Praxis Nord", phone="+49 30 40367600",
                      address="Hauptstr. 5, 10115 Berlin"),
            candidate(2, name="Praxis Nord"),
        )
        self.assertFalse(ok)
        self.assertEqual(evidence, [])

    def test_a_shared_domain_does_not_by_itself_merge(self):
        # A chain's sites share one registrable domain; the collector must not
        # collapse the branches because of it.
        ok, _evidence, _score = self.decide(
            candidate(1, name="Praxis Nord", domain="praxis-nord.de",
                      address="Hauptstr. 5, 10115 Berlin"),
            candidate(2, name="Praxis Nord", domain="praxis-nord.de",
                      address="S\u00fcdstr. 9, 10243 Berlin"),
        )
        self.assertFalse(ok)

    def test_a_non_latin_name_is_not_blanked_or_transliterated(self):
        # "非拉丁名称不被清空": a Japanese clinic seen by Google and by OSM.
        ok, _evidence, _score = self.decide(
            candidate(1, name="\u6f22\u65b9\u30af\u30ea\u30cb\u30c3\u30af\u5802\u6797",
                      phone="+81 3 12345678",
                      address="\u6771\u4eac\u90fd\u6e2f\u533a\u8336\u4e95\u4f4d1-2-3"),
            candidate(2, name="\u6f22\u65b9\u30af\u30ea\u30cb\u30c3\u30af\u5802\u6797",
                      phone="03 12345678",
                      address="\u6771\u4eac\u90fd\u6e2f\u533a\u8336\u4e95\u4f4d1-2-3"),
            country="JP",
        )
        self.assertTrue(ok)

    def test_a_different_name_with_the_same_phone_is_not_merged(self):
        # A phone shared by a call centre and the practice it serves is not
        # enough; the name has to agree too.
        ok, _evidence, _score = self.decide(
            candidate(1, name="Praxis Nord", phone="+49 30 40367600",
                      address="Hauptstr. 5, 10115 Berlin"),
            candidate(2, name="Vermittlung 24", phone="+49 30 40367600",
                      address="Hauptstr. 5, 10115 Berlin"),
        )
        self.assertFalse(ok)

    def test_the_branch_guard_wins_over_agreeing_coordinates(self):
        # A mistyped coordinate must not override an explicit address conflict:
        # the address is the stronger evidence for "two locations".
        ok, evidence, _score = self.decide(
            candidate(1, name="Praxis Nord", address="Hauptstr. 5, 10115 Berlin",
                      latitude=52.5201, longitude=13.4052),
            candidate(2, name="Praxis Nord", address="Hauptstr. 5, 99999 Berlin",
                      latitude=52.5201, longitude=13.4052),
        )
        self.assertFalse(ok)
        self.assertEqual(evidence, [merge.REASON_ADDRESS])

    def test_a_coordinate_difference_does_not_undo_agreeing_evidence(self):
        # OSM records a building's centroid and Google Maps records the
        # entrance; the same shop can be a few hundred metres apart in the two
        # sources. Geography is a positive signal only, never a veto — the
        # address and the E.164 number are what decide.
        ok, evidence, _score = self.decide(
            candidate(1, name="B\u00e4ckerei Sonne", phone="+49 30 1111111",
                      address="Hauptstr. 5, 10115 Berlin",
                      latitude=52.5200, longitude=13.4050),
            candidate(2, name="Baeckerei Sonne", phone="030 1111111",
                      address="Hauptstra\u00dfe 5, 10115 Berlin",
                      latitude=52.5210, longitude=13.4080),
        )
        self.assertTrue(ok)
        self.assertIn(merge.REASON_PHONE, evidence)
        self.assertIn(merge.REASON_ADDRESS, evidence)

    def test_coordinates_alone_are_not_enough_without_a_name(self):
        # Two businesses in the same building share the entrance point.
        ok, _evidence, _score = self.decide(
            candidate(1, name="Praxis Nord", address="Hauptstr. 5, 10115 Berlin",
                      latitude=52.52, longitude=13.40),
            candidate(2, name="Augenarzt S\u00fcd", address="Hauptstr. 5, 10115 Berlin",
                      latitude=52.52, longitude=13.40),
        )
        self.assertFalse(ok)


class IdentityNamespaceTest(unittest.TestCase):
    """A non-Google provider must not borrow a Google-only id.

    Regression found by the stage E acceptance script: a record carrying both
    ``data_id`` and ``source_id`` resolved to ``data_id:...`` whatever the
    provider said, so an OpenStreetMap record collided with an unrelated
    Google record and was silently deduped against it at ingest time — the
    merge stage then had nothing left to do.
    """

    def test_a_non_google_provider_resolves_only_its_own_source_id(self):
        from collector.identity import resolve_stable_key

        kind, key = resolve_stable_key(
            {"data_id": "br-1", "source_id": "node/33301", "source_id_kind": "osm"},
            "openstreetmap",
        )
        self.assertEqual(kind, "openstreetmap:osm")
        self.assertEqual(key, "openstreetmap:osm:node/33301")

    def test_google_keeps_its_historical_keys(self):
        from collector.identity import resolve_stable_key

        kind, key = resolve_stable_key(
            {"data_id": "0x0:0x1", "source_id": "node/33301"}, "google_maps",
        )
        self.assertEqual((kind, key), ("data_id", "data_id:0x0:0x1"))

    def test_a_record_with_no_identity_still_reports_missing(self):
        from collector.identity import resolve_stable_key

        self.assertIsNone(resolve_stable_key({"title": "no ids here"}, "openstreetmap"))


# --- the stage against a real database --------------------------------------


def raw(**overrides):
    base = {
        "title": "Praxis Beispiel",
        "address": "Musterstra\u00dfe 1, 10115 Berlin",
        "phone": "+49 30 12345678",
        "web_site": "https://www.praxis.example.de",
        "latitude": 52.52,
        "longitude": 13.40,
        "category": "Zahnarzt",
    }
    base.update(overrides)
    return base


class MergeRunTest(unittest.TestCase):
    """The merge is a database operation, so it is tested as one."""

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

    def write(self, provider, records, name=None):
        path = self.raw_dir / (name or f"{provider}-a1.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                envelope = support.envelope(record, provider=provider)
                handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                handle.write("\n")
        return path

    def ingest_all(self):
        return ingest.ingest_directory(
            self.conn, self.raw_dir, run_id=self.run_id, cfg=support.config()
        )

    def entity_ids(self):
        return {
            int(row["id"])
            for row in self.conn.execute("SELECT id FROM entities")
        }

    def test_one_store_from_two_sources_becomes_one_row(self):
        # The acceptance case, end to end through ingest and merge.
        self.write(
            GOOGLE,
            [raw(title="Praxis Dr. M\u00fcller", data_id="g1",
                 address="Hauptstr. 5, 10115 Berlin")],
        )
        self.write(
            OSM,
            [raw(title="Zahnarztpraxis M\u00fcller",
                 address="Hauptstra\u00dfe 5, 10115 Berlin",
                 latitude=52.5202, longitude=13.4053,
                 source_id="node/4711", source_id_kind="osm")],
        )
        self.ingest_all()
        self.assertEqual(db.table_counts(self.conn)["entities"], 2)

        stats = merge.merge_run(self.conn, self.run_id, support.config())

        self.assertEqual(stats.entities, 2)
        self.assertEqual(stats.merged, 1)
        self.assertEqual(db.count_run_stores(self.conn, self.run_id), 1)
        self.assertEqual(db.count_merges(self.conn), 1)

    def test_two_branches_stay_two_rows(self):
        self.write(
            GOOGLE,
            [
                raw(title="Praxis Nord", data_id="b1",
                    address="Hauptstr. 5, 10115 Berlin"),
                raw(title="Praxis Nord", data_id="b2",
                    address="S\u00fcdstr. 9, 10243 Berlin"),
            ],
        )
        self.ingest_all()
        stats = merge.merge_run(self.conn, self.run_id, support.config())

        self.assertEqual(stats.merged, 0)
        self.assertGreaterEqual(stats.refused_branch, 1)
        self.assertEqual(db.count_run_stores(self.conn, self.run_id), 2)

    def test_a_company_listed_twice_is_named_when_it_is_kept_apart(self):
        """056: the same business, two detail pages, one phone.

        A directory that lists one company twice produces two records whose
        name and phone agree and whose addresses differ by a house-number typo.
        The branch rule must keep them apart -- a shared switchboard is exactly
        what two branches look like -- so the rows stay two, and the pair is
        named in the stats so the deliverable can explain the duplicate instead
        of leaving the reader to notice it.
        """
        self.write(
            GOOGLE,
            [
                raw(title="杭州方祥医疗器械有限公司",
                    data_id="c10352910", phone="571 85181608",
                    address="浙江省杭州市江干区九环路48号"),
                raw(title="杭州方祥医疗器械有限公司",
                    data_id="c10356845", phone="571 85181608",
                    address="浙江省杭州市江干区九环路4号"),
            ],
        )
        self.ingest_all()
        stats = merge.merge_run(self.conn, self.run_id, support.config())

        self.assertEqual(stats.merged, 0)
        self.assertEqual(stats.refused_branch, 1)
        self.assertEqual(db.count_run_stores(self.conn, self.run_id), 2)
        # Named, not just counted.
        self.assertEqual(stats.branch_pairs_total, 1)
        self.assertEqual(len(stats.branch_pairs), 1)
        pair = stats.branch_pairs[0]
        self.assertEqual(pair["name"], "杭州方祥医疗器械有限公司")
        self.assertTrue(pair["phone"])
        self.assertNotEqual(pair["address_a"], pair["address_b"])
        self.assertTrue(pair["source_a"] and pair["source_b"])
        self.assertNotEqual(pair["source_a"], pair["source_b"])

    def test_a_kept_apart_pair_without_a_shared_phone_is_not_named(self):
        # Same trading name, different numbers, different streets: an ordinary
        # refusal. Naming every one of these would bury the pair that matters.
        self.write(
            GOOGLE,
            [
                raw(title="Praxis Nord", data_id="n1", phone="+49 30 1111111",
                    address="Hauptstr. 5, 10115 Berlin"),
                raw(title="Praxis Nord", data_id="n2", phone="+49 30 2222222",
                    address="Südstr. 9, 10243 Berlin"),
            ],
        )
        self.ingest_all()
        stats = merge.merge_run(self.conn, self.run_id, support.config())

        self.assertGreaterEqual(stats.refused_branch, 1)
        self.assertEqual(stats.branch_pairs_total, 0)
        self.assertEqual(stats.branch_pairs, [])

    def test_a_non_latin_name_survives_untransliterated(self):
        self.write(
            GOOGLE,
            [raw(title="\u6f22\u65b9\u30af\u30ea\u30cb\u30c3\u30af\u5802\u6797",
                 data_id="j1", address="\u8336\u4e95\u4f4d1-2-3")],
        )
        self.write(
            OSM,
            [raw(title="\u6f22\u65b9\u30af\u30ea\u30cb\u30c3\u30af\u5802\u6797",
                 address="\u8336\u4e95\u4f4d1-2-3",
                 latitude=35.6, longitude=139.7,
                 source_id="node/88", source_id_kind="osm")],
        )
        self.ingest_all()
        stats = merge.merge_run(self.conn, self.run_id, support.config())

        self.assertEqual(stats.merged, 1)
        self.assertEqual(db.count_run_stores(self.conn, self.run_id), 1)
        names = [
            row[0]
            for row in self.conn.execute("SELECT name FROM entities")
        ]
        # The name is stored exactly as the sources spelled it.
        self.assertIn("\u6f22\u65b9\u30af\u30ea\u30cb\u30c3\u30af\u5802\u6797", names)

    def test_the_losing_candidate_and_the_reason_are_kept(self):
        self.write(GOOGLE, [raw(data_id="c1", address="Hauptstr. 5, 10115 Berlin")])
        self.write(OSM, [raw(address="Hauptstra\u00dfe 5, 10115 Berlin",
                             latitude=52.5202, longitude=13.4053,
                             source_id="way/22", source_id_kind="osm")])
        self.ingest_all()
        merge.merge_run(self.conn, self.run_id, support.config())

        merges = list(db.iter_merges(self.conn))
        self.assertEqual(len(merges), 1)
        entry = merges[0]
        self.assertIn(merge.REASON_ADDRESS, entry["reason"])
        evidence = json.loads(entry["evidence_json"])
        self.assertIn("survivor", evidence)
        self.assertIn("candidate", evidence)
        self.assertIn("distance_meters", evidence)
        self.assertIsNotNone(entry["score"])

    def test_the_merge_is_reversible_by_deleting_one_row(self):
        self.write(GOOGLE, [raw(data_id="r1", address="Hauptstr. 5, 10115 Berlin")])
        self.write(OSM, [raw(address="Hauptstra\u00dfe 5, 10115 Berlin",
                             latitude=52.5202, longitude=13.4053,
                             source_id="way/22", source_id_kind="osm")])
        self.ingest_all()
        merge.merge_run(self.conn, self.run_id, support.config())
        canonical = db.canonical_map(self.conn)
        self.assertTrue(canonical)

        self.conn.execute("DELETE FROM entity_merges")
        self.conn.commit()

        self.assertEqual(db.canonical_map(self.conn), {})
        # Nothing was overwritten: both entities still exist with their facts.
        self.assertEqual(db.table_counts(self.conn)["entities"], 2)

    def test_rerunning_the_merge_is_idempotent(self):
        self.write(GOOGLE, [raw(data_id="i1", address="Hauptstr. 5, 10115 Berlin")])
        self.write(OSM, [raw(address="Hauptstra\u00dfe 5, 10115 Berlin",
                             latitude=52.5202, longitude=13.4053,
                             source_id="way/22", source_id_kind="osm")])
        self.ingest_all()
        first = merge.merge_run(self.conn, self.run_id, support.config())
        second = merge.merge_run(self.conn, self.run_id, support.config())

        self.assertEqual(first.merged, 1)
        self.assertEqual(second.merged, 0)
        self.assertGreaterEqual(second.already_recorded, 1)
        self.assertEqual(db.count_merges(self.conn), 1)

    def test_three_sources_of_one_store_produce_one_group(self):
        # Union-find: A~B and B~C must give one group with one survivor, not a
        # chain of pairwise merges that could contradict each other.
        self.write(GOOGLE, [raw(data_id="t1", address="Hauptstr. 5, 10115 Berlin")])
        self.write(OSM, [raw(address="Hauptstra\u00dfe 5, 10115 Berlin",
                             latitude=52.5202, longitude=13.4053,
                             source_id="way/22", source_id_kind="osm")])
        self.write(CSV, [raw(address="Hauptstr. 5, 10115 Berlin",
                             latitude=52.5201, longitude=13.4052,
                             source_id="row-3", source_id_kind="csv_row")])
        self.ingest_all()
        self.assertEqual(db.table_counts(self.conn)["entities"], 3)

        stats = merge.merge_run(self.conn, self.run_id, support.config())

        self.assertEqual(stats.merged, 2)
        self.assertEqual(db.count_run_stores(self.conn, self.run_id), 1)
        self.assertEqual(db.count_merges(self.conn), 2)

    def test_an_empty_run_is_not_an_error(self):
        stats = merge.merge_run(self.conn, self.run_id, support.config())
        self.assertEqual(stats.entities, 0)
        self.assertEqual(stats.merged, 0)
        self.assertIn("no entities", " ".join(stats.notes))
