"""Chinese administrative divisions (task book 019 A, round 034 item 3.5).

Two things are being pinned down here, and they are different kinds of claim:

* **the level of a place.** 建德 is a 县级市 administered by 杭州市 -- not a
  district of it, not a prefecture of its own. Saying "建德, 浙江" without 杭州
  is the wrong city, which is why the parent is part of the answer.
* **a name that used to be a place.** 下城区, 江干区 and 富阳市 are still typed
  by people and are no longer divisions. Recognising them but *not* mapping
  them is the honest answer: 富阳市 was a county-level city and is now a
  district, so silently answering "富阳区" would quietly change the level.

The county-level table is transcribed from a published code list, so a test
re-derives every row's level from its code rather than trusting the hand entry.
"""

from __future__ import annotations

import unittest

from collector import local_ui, regions
from collector.errors import CollectorError

from . import support


class DivisionLookupTest(unittest.TestCase):
    def lookup(self, text: str) -> dict | None:
        return regions.lookup_cn_division(text)

    def test_a_county_level_city_is_resolved_with_its_prefecture(self):
        # The one the task book named. The parent is the point: without it a
        # search for 建德 is not anchored in 杭州市.
        hit = self.lookup("建德")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["level"], "county_city")
        self.assertEqual(hit["code"], "330182")
        self.assertEqual(hit["city_name"], "杭州市")
        self.assertEqual(hit["province_name"], "浙江省")
        # With the suffix too, since people type both.
        self.assertEqual(self.lookup("建德市")["code"], "330182")

    def test_a_county_and_a_district_are_different_levels(self):
        self.assertEqual(self.lookup("桐庐县")["level"], "county")
        self.assertEqual(self.lookup("淳安县")["level"], "county")
        self.assertEqual(self.lookup("西湖区")["level"], "district")
        self.assertEqual(self.lookup("临平区")["level"], "district")
        for name in ("桐庐县", "淳安县", "西湖区"):
            self.assertEqual(self.lookup(name)["city_name"], "杭州市")

    def test_a_city_resolves_whether_or_not_the_suffix_is_typed(self):
        # The regression: the stored name carries its suffix, so the caller's
        # stripped form was compared against the wrong string and 杭州 -- the
        # single most likely thing a person types -- resolved to nothing while
        # the docstring promised it worked.
        self.assertEqual(self.lookup("杭州")["code"], "3301")
        self.assertEqual(self.lookup("杭州市")["code"], "3301")
        self.assertEqual(self.lookup("杭州市")["level"], "city")

    def test_a_province_still_resolves(self):
        self.assertEqual(self.lookup("浙江省")["level"], "province")
        self.assertEqual(self.lookup("浙江")["code"], "33")

    def test_a_place_the_data_does_not_carry_is_none_not_a_guess(self):
        self.assertIsNone(self.lookup("某个不存在的县"))
        self.assertIsNone(self.lookup(""))

    # -- former names --------------------------------------------------------

    def test_an_old_district_name_is_recognised_but_not_mapped(self):
        for name in ("下城区", "江干区"):
            hit = self.lookup(name)
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit["level"], "former")
            # Not a place: no parent city is invented for it.
            self.assertNotIn("city_name", hit)
            self.assertTrue(hit["note"])

    def test_a_city_that_became_a_district_is_not_answered_as_the_district(self):
        # 富阳市 (县级市) -> 富阳区 (市辖区). The name is the same word; the level
        # is not. Answering 富阳区 would be answering a different question.
        hit = self.lookup("富阳市")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["level"], "former")
        self.assertIn("富阳区", hit["note"])
        # And the current name is still resolvable on its own terms.
        self.assertEqual(self.lookup("富阳区")["level"], "district")

    # -- the transcribed table -----------------------------------------------

    def test_every_hand_entered_level_agrees_with_its_code(self):
        for row in regions.CN_COUNTIES:
            self.assertEqual(
                regions.county_level_code_to_level(str(row["code"])),
                row["level"],
                f"{row['name']} ({row['code']}) is entered as {row['level']!r}",
            )

    def test_every_hand_entered_parent_is_a_real_city(self):
        codes = {str(row["code"]) for row in regions.CN_CITIES}
        for row in regions.CN_COUNTIES:
            self.assertIn(str(row["cityCode"]), codes, row["name"])

    def test_the_coverage_boundary_is_stated_next_to_the_data(self):
        # A reader has to be able to see that this table is not the country.
        self.assertTrue(regions.COUNTY_LEVEL_COVERAGE_NOTE)
        self.assertTrue(regions.COUNTY_LEVEL_SOURCE_URL.startswith("https://"))
        self.assertIn("杭州市", regions.COUNTY_LEVEL_COVERAGE_NOTE)


class ScreenCentreTest(unittest.TestCase):
    """What the screen says when it cannot resolve a centre."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def centre_error(self, city: str, geo: str = "") -> str:
        with self.assertRaises(CollectorError) as ctx:
            local_ui.resolve_location(
                {"city": [city], "country": ["中国"], "geo": [geo]}
            )
        return str(ctx.exception)

    def test_a_county_level_city_says_which_city_it_belongs_to(self):
        message = self.centre_error("建德")
        self.assertIn("杭州市", message)
        self.assertIn("330182", message)
        # And it still refuses to substitute another city's coordinates.
        self.assertIn("不会默默改用其他城市", message)

    def test_an_old_district_name_is_refused_as_an_old_name(self):
        self.assertIn("旧区名", self.centre_error("下城区"))

    def test_an_explicit_coordinate_beats_the_question(self):
        # The way out is the same as it always was, and it wins.
        self.assertEqual(
            local_ui.resolve_location(
                {"city": ["建德"], "country": ["中国"], "geo": ["29.48,119.28"]}
            ),
            (29.48, 119.28),
        )

    def test_an_unknown_place_asks_for_coordinates(self):
        message = self.centre_error("某个不存在的县")
        self.assertIn("纬度,经度", message)
        self.assertIn("不会默默改用其他城市", message)

    def test_bare_fuyang_resolves_to_the_division_that_exists(self):
        # 富阳市 is gone; 富阳区 exists. The stem alone must land on the one that
        # is real, not on the old name.
        self.assertEqual(regions.lookup_cn_division("富阳")["name"], "富阳区")


class CountryResolutionTest(unittest.TestCase):
    """054: resolving a country is a typing convenience, not a data conversion.

    The screen's own placeholder promised "填 中国 / CN / Germany 均可", and the
    table only knew some of the spellings it implied. The bug class is a
    *locale* deciding whether a country resolves at all, so these pin the three
    spellings that must all reach the same code, plus the honest "" for text
    that is not a country at all -- which is what lets the caller refuse with
    the person's own words instead of inventing one.
    """

    def test_the_three_spellings_agree(self):
        for text in ("意大利", "Italy", "italia", "IT", "it", " 意大利 "):
            self.assertEqual(regions.normalize_country(text), "IT", text)

    def test_china_still_resolves_under_every_name_it_had(self):
        for text in ("中国", "CN", "China", "china", "中华人民共和国"):
            self.assertEqual(regions.normalize_country(text), "CN", text)

    def test_a_country_that_was_missing_now_resolves(self):
        # These were absent from the 24-entry table, so the same locale problem
        # reached every one of them.
        for text, code in (
            ("西班牙", "ES"),
            ("Portugal", "PT"),
            ("比利时", "BE"),
            ("Canada", "CA"),
            ("澳大利亚", "AU"),
            ("新加坡", "SG"),
            ("日本", "JP"),
            ("德国", "DE"),
            ("美国", "US"),
        ):
            self.assertEqual(regions.normalize_country(text), code, text)

    def test_a_non_country_returns_empty_and_is_not_guessed(self):
        # An empty answer is the point: it makes the caller say "I don't know
        # what you mean by this", instead of picking a country for the person.
        for text in ("", "   ", "不是国家", "Milan"):
            self.assertEqual(regions.normalize_country(text), "", repr(text))

    def test_every_resolvable_country_gets_a_profile(self):
        # 054 found 意大利 refused at the alias table; this keeps the *next*
        # country from being half-added (label present, profile missing).
        for code in regions.REGION_LABELS:
            self.assertIn(code, regions.REGION_PROFILES, code)


class CityAliasTest(unittest.TestCase):
    """054: a city alias is pure spelling, and it belongs to one country.

    Two rules, and both are load-bearing:

    * **spelling only** -- 米兰 / Milan / Milano are the same city, and the
      *typed* text survives into the config and the export;
    * **country-bound** -- Milan is not usable in Germany. Without the second
      rule a stale country box would silently retarget the search, which is
      exactly the "don't quietly move the user" line in the task book.
    """

    def test_the_three_spellings_reach_one_centre(self):
        centre = local_ui.resolve_location({"city": ["米兰"], "country": ["意大利"]})
        self.assertEqual(centre, (45.4642, 9.19))
        for text in ("Milan", "milano", "Milano", " 米兰 "):
            self.assertEqual(
                local_ui.resolve_location({"city": [text], "country": ["IT"]}),
                centre,
                text,
            )

    def test_the_typed_text_is_what_is_kept(self):
        # Not the canonical key: the person's own words go into the config.
        self.assertEqual(regions.resolve_city("米兰", "IT"), "milano")
        self.assertEqual(regions.resolve_city("Milan", "IT"), "milano")
        # An unknown city is returned untouched rather than dropped.
        self.assertEqual(regions.resolve_city("Sondrio", "IT"), "Sondrio")

    def test_a_city_is_not_usable_outside_its_own_country(self):
        self.assertEqual(regions.resolve_city("Milan", "DE"), "Milan")
        self.assertIsNone(regions.city_centre("Milan", "DE"))
        self.assertIsNone(regions.city_centre("柏林", "CN"))
        # ... and it is usable in its own country, spelled either way.
        self.assertIsNotNone(regions.city_centre("Berlin", "DE"))
        self.assertIsNotNone(regions.city_centre("柏林", "DE"))

    def test_other_countries_work_through_the_same_path(self):
        for text, country in (("柏林", "DE"), ("慕尼黑", "DE"), ("武汉", "CN")):
            self.assertIsNotNone(
                local_ui.resolve_location({"city": [text], "country": [country]}),
                f"{text}/{country}",
            )


class CoordinateNecessityTest(unittest.TestCase):
    """054: ask for a coordinate only when a chosen source reads one.

    A CSV file, a saved page and a directory category page each already say
    where they are; demanding a centre for them blocked a user who had no use
    for it. The rule is derived from the sources, not from a flag.
    """

    def test_only_the_map_sources_need_a_centre(self):
        self.assertTrue(local_ui.needs_geo(["google_maps"]))
        self.assertTrue(local_ui.needs_geo(["openstreetmap"]))
        self.assertTrue(local_ui.needs_geo(["google_maps", "csv_list"]))
        for offline in (["csv_list"], ["local_pages"], ["directory"], [], ["sitediscovery"]):
            self.assertFalse(local_ui.needs_geo(offline), offline)

    def test_an_unlisted_city_is_not_an_error_when_no_map_is_read(self):
        # The exact 054 symptom: an offline import of an unlisted city was
        # blocked by a coordinate nothing was going to use.
        self.assertEqual(
            local_ui.resolve_location(
                {"city": ["Sondrio"], "country": ["IT"]}, sources=["csv_list"]
            ),
            (0.0, 0.0),
        )

    def test_the_same_city_is_still_a_question_for_a_map_run(self):
        with self.assertRaises(CollectorError):
            local_ui.resolve_location(
                {"city": ["Sondrio"], "country": ["IT"]}, sources=["google_maps"]
            )

    def test_zero_zero_is_reachable_only_through_the_no_map_path(self):
        # Guards against the shortcut becoming a real centre: with a map source
        # chosen, 0,0 is never returned for an unlisted city.
        with self.assertRaises(CollectorError):
            local_ui.resolve_location(
                {"city": ["Sondrio"], "country": ["IT"]},
                sources=["google_maps", "csv_list"],
            )


class EmptyCityInProvinceBoxTest(unittest.TestCase):
    """054 item 4: a city typed into the province box is reported, not moved."""

    def message(self, city: str, province: str, country: str = "IT") -> str:
        with self.assertRaises(CollectorError) as ctx:
            local_ui.resolve_location(
                {"city": [city], "province": [province], "country": [country]},
                sources=["google_maps"],
            )
        return str(ctx.exception)

    def test_a_city_in_the_province_box_is_named_and_left_alone(self):
        text = self.message("", "米兰")
        self.assertIn("米兰", text)
        self.assertIn("不会替你搬动", text)

    def test_an_empty_city_with_no_clue_says_the_city_is_missing(self):
        text = self.message("", "")
        self.assertIn("「城市」一栏是空的", text)
        # It must not claim the province box holds a city: with that box empty
        # too there is nothing to point at, and a false "you put the city one
        # box up" sends the person to fix the wrong field.
        self.assertNotIn("「省/直辖市」一栏填的是城市名", text)
        # 米兰/柏林 appear here only as *examples* of what to type, which is
        # the documented behaviour -- not as a guess at what was meant.
        self.assertIn("例如", text)

    def test_a_province_that_is_a_real_province_is_not_reported_as_a_city(self):
        # 浙江省 is a province, so the message must be about the missing city,
        # not about a misplaced city name -- a false "you put the city one box
        # up" would send the person to fix the wrong field.
        text = self.message("", "浙江省", "CN")
        self.assertIn("「城市」一栏是空的", text)
        self.assertNotIn("「省/直辖市」一栏填的是城市名", text)


if __name__ == "__main__":
    unittest.main()
