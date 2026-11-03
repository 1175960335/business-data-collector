"""Phone validation and address component parsing."""

from __future__ import annotations

import unittest

from collector.addresscheck import (
    STATE_COMPONENTS_PARSED,
    STATE_EMPTY,
    STATE_NAME_NOT_ADDRESS,
    is_name_shaped,
    parse_address,
)
from collector.phonecheck import (
    STATE_E164_VALID,
    STATE_NO_COUNTRY_CONTEXT,
    STATE_NATIONAL_VALID_REGION_CONTEXT,
    STATE_NOT_A_VALID_NUMBER,
    STATE_PARSE_ERROR,
    check_phone,
)


class PhoneTest(unittest.TestCase):
    def test_a_number_with_its_own_country_code_is_valid(self):
        verdict = check_phone("+49 30 12345678", region="DE")
        self.assertEqual(verdict.status, "valid")
        self.assertEqual(verdict.state, STATE_E164_VALID)
        self.assertEqual(verdict.e164, "+493012345678")
        self.assertEqual(verdict.region, "DE")
        self.assertEqual(verdict.number_type, "fixed_line_or_mobile")

    def test_the_raw_value_is_always_kept(self):
        verdict = check_phone("+49 (30) 1234-5678", region="DE")
        self.assertEqual(verdict.raw, "+49 (30) 1234-5678")
        self.assertEqual(verdict.e164, "+493012345678")

    def test_a_national_number_is_only_valid_relative_to_the_context(self):
        verdict = check_phone("030 12345678", region="DE")
        self.assertEqual(verdict.state, STATE_NATIONAL_VALID_REGION_CONTEXT)
        self.assertEqual(verdict.status, "uncertain")
        self.assertEqual(verdict.e164, "+493012345678")
        self.assertIn("DE", verdict.note)

    def test_a_national_number_without_a_context_is_not_guessed(self):
        verdict = check_phone("030 12345678")
        self.assertEqual(verdict.state, STATE_NO_COUNTRY_CONTEXT)
        self.assertEqual(verdict.status, "uncertain")
        self.assertIsNone(verdict.e164)

    def test_an_extension_is_extracted_and_kept(self):
        verdict = check_phone("+49 30 12345678 ext. 12", region="DE")
        self.assertEqual(verdict.extension, "12")
        self.assertEqual(verdict.e164, "+493012345678")
        self.assertIn("extension 12", verdict.note)

    def test_a_tel_url_prefix_is_stripped(self):
        verdict = check_phone("tel:+493012345678", region="DE")
        self.assertEqual(verdict.e164, "+493012345678")
        self.assertTrue(verdict.checks["stripped_tel_prefix"])

    def test_a_wrong_length_is_invalid(self):
        verdict = check_phone("12345", region="DE")
        self.assertEqual(verdict.state, STATE_NOT_A_VALID_NUMBER)
        self.assertEqual(verdict.status, "invalid")

    def test_unparsable_text_is_invalid(self):
        verdict = check_phone("keine Nummer", region="DE")
        self.assertEqual(verdict.state, STATE_PARSE_ERROR)
        self.assertEqual(verdict.status, "invalid")

    def test_an_empty_value_is_invalid(self):
        self.assertEqual(check_phone(None).status, "invalid")
        self.assertEqual(check_phone("").status, "invalid")

    def test_the_region_context_is_recorded_in_the_checks(self):
        verdict = check_phone("+49 30 12345678", region="DE")
        self.assertEqual(verdict.checks["parsed_with_region"], "(number's own country code)")
        verdict = check_phone("030 12345678", region="DE")
        self.assertEqual(verdict.checks["parsed_with_region"], "DE")

    def test_syntax_checking_can_be_switched_off(self):
        verdict = check_phone("030 12345678", region="DE", rules={"phone_syntax": False})
        self.assertEqual(verdict.status, "unverified")


class AddressTest(unittest.TestCase):
    def test_components_are_derived_and_the_raw_text_is_kept(self):
        raw = "Musterstra\u00dfe 1, 10115 Berlin, DE"
        verdict = parse_address(raw, country="DE")
        self.assertEqual(verdict.raw, raw)
        self.assertEqual(verdict.state, STATE_COMPONENTS_PARSED)
        self.assertEqual(verdict.components["postal_code"], "10115")
        self.assertEqual(verdict.components["city"], "Berlin")
        self.assertEqual(verdict.components["country"], "DE")
        self.assertEqual(verdict.components["street"], "Musterstra\u00dfe 1")
        self.assertEqual(verdict.components["house_number"], "1")
        self.assertEqual(verdict.components["street_name"], "Musterstra\u00dfe")

    def test_location_validation_is_separate_from_postal_reality(self):
        verdict = parse_address("Musterstra\u00dfe 1, 10115 Berlin, DE", country="DE")
        self.assertTrue(verdict.location_validated)
        # Postal reality is never claimed.
        self.assertFalse(verdict.postal_verified)
        self.assertIn("does not consult a postal address database", verdict.postal_note)

    def test_a_foreign_country_is_flagged_not_corrected(self):
        verdict = parse_address("10 Downing St, London SW1A 2AA, United Kingdom", country="DE")
        self.assertFalse(verdict.location_validated)
        self.assertIn("GB", verdict.location_note)
        # The raw value was not rewritten to fit the configured country.
        self.assertEqual(verdict.raw, "10 Downing St, London SW1A 2AA, United Kingdom")

    def test_an_address_without_a_country_leaves_location_undetermined(self):
        verdict = parse_address("Musterstra\u00dfe 1, 10115 Berlin", country="DE")
        self.assertIsNone(verdict.location_validated)
        self.assertIn("undetermined", verdict.location_note)

    def test_unicode_is_preserved_and_scripted(self):
        verdict = parse_address("\u5317\u4eac\u5e02\u671d\u9633\u533a\u5efa\u56fd\u8def88\u53f7", country="CN")
        self.assertEqual(verdict.raw, "\u5317\u4eac\u5e02\u671d\u9633\u533a\u5efa\u56fd\u8def88\u53f7")
        self.assertTrue(verdict.has_non_ascii)
        self.assertIn("han", verdict.scripts)
        self.assertTrue(verdict.unicode_ok)

    def test_cyrillic_and_greek_addresses_survive(self):
        for raw, script in (
            ("\u0443\u043b. \u0422\u0432\u0435\u0440\u0441\u043a\u0430\u044f 1, \u041c\u043e\u0441\u043a\u0432\u0430", "cyrillic"),
            ("\u039f\u03b4\u03cc\u03c2 \u0395\u03c1\u03bc\u03bf\u03cd 5, \u0391\u03b8\u03ae\u03bd\u03b1", "greek"),
        ):
            with self.subTest(raw=raw):
                verdict = parse_address(raw)
                self.assertEqual(verdict.raw, raw)
                self.assertIn(script, verdict.scripts)

    def test_mojibake_is_flagged(self):
        verdict = parse_address("Musterstra\u00dfe 1, 10115 Berl\u00c3\u00bcn", country="DE")
        self.assertTrue(verdict.mojibake_suspected)

    def test_a_us_state_is_not_mistaken_for_a_city(self):
        verdict = parse_address("123 Main St, Brooklyn, NY 11201, USA", country="DE")
        self.assertEqual(verdict.components["city"], "Brooklyn")
        self.assertEqual(verdict.components["region"], "NY")
        self.assertEqual(verdict.components["country"], "US")

    def test_a_polish_postal_code_is_recognised(self):
        verdict = parse_address("ul. Marsza\u0142kowska 10, 00-590 Warszawa, PL", country="DE")
        self.assertEqual(verdict.components["postal_code"], "00-590")
        self.assertEqual(verdict.components["city"], "Warszawa")
        self.assertEqual(verdict.components["country"], "PL")

    def test_a_dutch_postal_code_is_recognised(self):
        verdict = parse_address("Keizersgracht 123, 1015 CJ Amsterdam, NL", country="DE")
        self.assertEqual(verdict.components["postal_code"], "1015 CJ")
        self.assertEqual(verdict.components["city"], "Amsterdam")

    def test_an_empty_address_is_handled(self):
        verdict = parse_address("", country="DE")
        self.assertEqual(verdict.state, STATE_EMPTY)
        self.assertFalse(verdict.postal_verified)

    def test_an_unparsable_address_keeps_the_raw_value(self):
        verdict = parse_address("irgendwo im Nirgendwo", country="DE")
        self.assertEqual(verdict.raw, "irgendwo im Nirgendwo")
        self.assertEqual(verdict.components.get("city"), "irgendwo im Nirgendwo")


class ChinesePhoneTest(unittest.TestCase):
    """The phone shapes a Chinese directory actually publishes (stage 019)."""

    def test_a_chinese_extension_marker_is_understood_and_kept(self):
        # libphonenumber does not know 转/分机; before this was normalised the
        # whole value came back as a parse error, losing a real number.
        verdict = check_phone("0571-88889999转802", region="CN")
        self.assertEqual(verdict.extension, "802")
        self.assertEqual(verdict.raw, "0571-88889999转802")
        self.assertNotEqual(verdict.state, STATE_PARSE_ERROR)

    def test_a_landline_without_its_country_code_is_not_called_verified(self):
        verdict = check_phone("0574 63323957", region="CN")
        self.assertEqual(verdict.state, STATE_NATIONAL_VALID_REGION_CONTEXT)
        self.assertEqual(verdict.status, "uncertain")

    def test_a_mobile_is_read_but_the_country_stays_unconfirmed(self):
        verdict = check_phone("13777374645", region="CN")
        self.assertEqual(verdict.e164, "+8613777374645")
        self.assertEqual(verdict.state, STATE_NATIONAL_VALID_REGION_CONTEXT)

    def test_an_international_chinese_number_is_valid(self):
        verdict = check_phone("+86 138 0013 8000", region="CN")
        self.assertEqual(verdict.state, STATE_E164_VALID)
        self.assertEqual(verdict.status, "valid")

    def test_a_chinese_number_without_a_region_is_not_guessed(self):
        verdict = check_phone("13777374645")
        self.assertEqual(verdict.state, STATE_NO_COUNTRY_CONTEXT)


class ChineseAddressTest(unittest.TestCase):
    """Chinese addresses are split into levels, never rewritten (stage 019)."""

    def test_the_levels_are_split_in_order(self):
        verdict = parse_address("江苏省苏州市吴中区木渎镇金山路1号", country="CN")
        self.assertEqual(verdict.components["province"], "江苏省")
        self.assertEqual(verdict.components["city"], "苏州市")
        self.assertEqual(verdict.components["district"], "吴中区")
        self.assertEqual(verdict.components["town"], "木渎镇")
        self.assertEqual(verdict.components["road"], "金山路")
        self.assertEqual(verdict.components["house_number"], "1号")
        self.assertTrue(verdict.location_validated)

    def test_a_repeated_upper_level_does_not_swallow_the_road(self):
        # The shape b2b168.com really serves: the province is written twice, so
        # a single anchored pass over the remainder loses 九环路 entirely.
        raw = "浙江省杭州江干区浙江 杭州市江干区九环路48号6号楼东二层"
        verdict = parse_address(raw, country="CN")
        self.assertEqual(verdict.components["province"], "浙江省")
        self.assertEqual(verdict.components["city"], "杭州")
        self.assertEqual(verdict.components["district"], "江干区")
        self.assertEqual(verdict.components["road"], "九环路")
        self.assertEqual(verdict.components["house_number"], "48号")
        self.assertEqual(verdict.components["detail"], "6号楼东二层")
        self.assertEqual(verdict.raw, raw)

    def test_a_repeated_level_does_not_widen_the_town(self):
        verdict = parse_address(
            "浙江省杭州富阳区杭州富阳区富春街道孙权路231号528室", country="CN"
        )
        self.assertEqual(verdict.components["town"], "富春街道")
        self.assertEqual(verdict.components["road"], "孙权路")

    def test_a_suffixless_city_is_recorded_as_written(self):
        verdict = parse_address("浙江省杭州富阳区富春街道孙权路231号", country="CN")
        self.assertEqual(verdict.components["city"], "杭州")
        self.assertEqual(verdict.components["city_suffix_absent"], "1")

    def test_a_municipality_is_the_province_and_the_district_is_not_a_city(self):
        verdict = parse_address("北京市朝阳区建国路88号", country="CN")
        self.assertEqual(verdict.components["province"], "北京市")
        self.assertEqual(verdict.components["district"], "朝阳区")
        self.assertNotIn("city", verdict.components)

    def test_a_county_level_city_is_kept_as_the_district(self):
        verdict = parse_address("浙江省宁波余姚市浙江 余姚市 姚北开发区", country="CN")
        self.assertEqual(verdict.components["city"], "宁波")
        self.assertEqual(verdict.components["district"], "余姚市")
        # Text that is neither a level nor a street is kept verbatim.
        self.assertEqual(verdict.components["detail"], "姚北开发区")

    def test_the_raw_text_is_never_rewritten(self):
        raw = "浙江省宁波鄞州区宁波市鄞州区高桥镇秀丰村（高桥工业区）"
        verdict = parse_address(raw, country="CN")
        self.assertEqual(verdict.raw, raw)

    def test_a_western_address_is_not_read_as_chinese(self):
        verdict = parse_address("Musterstra\u00dfe 1, 10115 Berlin", country="DE")
        self.assertNotIn("province", verdict.components)
        self.assertEqual(verdict.components.get("postal_code"), "10115")


class NameShapedAddressTest(unittest.TestCase):
    """048: a source that writes the business's name into the address slot.

    Wikidata's P6375 for Q30256951 is literally "geomer GmbH". Two copies of the
    name are not a name plus an independent signal, and the collector must not
    let that pass for an address.
    """

    def test_a_name_written_into_the_address_slot_is_recognised(self):
        self.assertTrue(is_name_shaped("geomer GmbH", "geomer GmbH"))

    def test_a_legal_form_does_not_hide_it(self):
        for value, name in (
            ("geomer GmbH", "geomer"),
            ("ACME Aktiengesellschaft", "ACME AG"),
            ("Beispiel & Co. KG", "Beispiel"),
            ("Muster Limited", "Muster Ltd."),
        ):
            with self.subTest(value=value, name=name):
                self.assertTrue(is_name_shaped(value, name))

    def test_a_branch_suffix_does_not_hide_it(self):
        # The item's *second* P6375 value: still no digit, still starts with the
        # full name, so what follows is a qualifier rather than a street.
        self.assertTrue(
            is_name_shaped("geomer GmbH Niederlassung Hessen", "geomer GmbH")
        )

    def test_a_real_address_is_left_alone(self):
        self.assertFalse(is_name_shaped("Musterstra\u00dfe 1, 10115 Berlin", "Praxis M\u00fcller"))
        # A house number is what makes an address an address.
        self.assertFalse(is_name_shaped("geomer GmbH Stra\u00dfe 5", "geomer GmbH"))

    def test_an_address_without_a_house_number_is_not_a_name(self):
        # Plenty of real listings carry no house number; they are not addresses
        # that merely *look* like names, and only equality is treated as one.
        self.assertFalse(is_name_shaped("Rietzer Berg, Kloster Lehnin", "Metallbau Windeck GmbH"))

    def test_an_empty_side_is_never_a_match(self):
        self.assertFalse(is_name_shaped("", "geomer GmbH"))
        self.assertFalse(is_name_shaped("geomer GmbH", ""))
        self.assertFalse(is_name_shaped(None, None))

    def test_parse_address_reports_it_as_not_an_address(self):
        verdict = parse_address("geomer GmbH", country="DE", name="geomer GmbH")
        self.assertEqual(verdict.state, STATE_NAME_NOT_ADDRESS)
        # No components means every comparison that reads them stops seeing an
        # address -- the value is classified, not dropped.
        self.assertEqual(verdict.components, {})
        self.assertEqual(verdict.match_key, "")
        self.assertEqual(verdict.raw, "geomer GmbH")
        self.assertIn("not_an_address", verdict.checks)
        self.assertFalse(verdict.postal_verified)

    def test_without_a_name_the_parser_behaves_as_before(self):
        verdict = parse_address("geomer GmbH", country="DE")
        self.assertNotEqual(verdict.state, STATE_NAME_NOT_ADDRESS)

    def test_a_normal_address_is_unaffected_by_the_name_argument(self):
        verdict = parse_address(
            "Musterstra\u00dfe 1, 10115 Berlin, DE", country="DE", name="Praxis M\u00fcller"
        )
        self.assertEqual(verdict.state, STATE_COMPONENTS_PARSED)
        self.assertEqual(verdict.components["house_number"], "1")


class DigitInTheCompanyNameTest(unittest.TestCase):
    """050 P0: a company name that contains a house number.

    Round 048's guard refused to judge any value carrying a digit, on the
    reasoning that a real street address has one. A company called ``Hansa 12
    GmbH`` walks straight through that gate, and its own name in the address slot
    then agrees with itself as the "independent" signal -- a false confirmation.
    """

    def test_a_digit_in_the_name_does_not_open_the_gate(self):
        self.assertTrue(is_name_shaped("Hansa 12 GmbH", "Hansa 12 GmbH"))

    def test_a_digit_in_the_name_is_not_proof_of_an_address(self):
        # The name is the same value, legal form or not.
        self.assertTrue(is_name_shaped("Bahn 7 Logistik", "Bahn 7 Logistik GmbH"))
        self.assertTrue(is_name_shaped("Bahn 7 Logistik GmbH", "Bahn 7 Logistik"))

    def test_the_verdict_names_the_reason(self):
        verdict = parse_address("Hansa 12 GmbH", country="DE", name="Hansa 12 GmbH")
        self.assertEqual(verdict.state, STATE_NAME_NOT_ADDRESS)
        self.assertEqual(verdict.components, {})
        self.assertEqual(verdict.match_key, "")
        self.assertEqual(verdict.raw, "Hansa 12 GmbH")

    def test_a_house_number_in_the_value_is_address_content(self):
        # ``geomer GmbH Straße 5``: the name plus a street. It carries its own
        # address content, so it is kept -- the name is not proof of anything.
        self.assertFalse(is_name_shaped("geomer GmbH Stra\u00dfe 5", "geomer GmbH"))

    def test_the_name_plus_a_real_address_is_kept(self):
        self.assertFalse(
            is_name_shaped("M\u00fcller GmbH, Hauptstr. 1, 10115 Berlin", "M\u00fcller GmbH")
        )

    def test_an_address_without_a_house_number_is_still_not_a_name(self):
        # 050: no blanket "no house number means bad address" rule. The value is
        # only withheld when it *is* the name.
        self.assertFalse(is_name_shaped("Rietzer Berg, Kloster Lehnin", "Metallbau Windeck GmbH"))
        verdict = parse_address(
            "Rietzer Berg, Kloster Lehnin", country="DE", name="Metallbau Windeck GmbH"
        )
        self.assertNotEqual(verdict.state, STATE_NAME_NOT_ADDRESS)

    def test_a_business_named_after_its_own_address_is_withheld(self):
        # The documented cost of the rule. It is a missed address, which is
        # recoverable by a human; a confirmation on no evidence is not.
        self.assertTrue(is_name_shaped("Am Hafen 1", "Am Hafen 1 GmbH"))
        verdict = parse_address("Am Hafen 1", country="DE", name="Am Hafen 1 GmbH")
        self.assertEqual(verdict.state, STATE_NAME_NOT_ADDRESS)
        self.assertEqual(verdict.raw, "Am Hafen 1")


if __name__ == "__main__":
    unittest.main()
