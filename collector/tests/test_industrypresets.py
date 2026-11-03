"""Round 026 P2.6: the industry presets are not a pet special case, and the
division table is the *full* set of a city's county-level divisions.

Two things are pinned here:

* **The pet vocabulary is not the only rule.** Profiles other than pet exist,
  each carries whether its word list was checked against real samples, and an
  operator's own exclusion words can override a keyword hit — so a
  "宠物食品包装机械" company is not swept into a pet list by the substring
  "宠物食品".
* **建德市 is part of 杭州市.** A county-level city or county belongs to its
  prefecture exactly as a district does; leaving 建德市/桐庐县/淳安县 out of the
  table did not make the verdict conservative, it made a real Hangzhou company
  come back as 待确认 with the reason "不在已知区县表中".
"""

from __future__ import annotations

import unittest

from collector import matching
from collector import local_ui
from collector.config import ConfigError, validate

from . import support


def evidence(**overrides) -> matching.RecordEvidence:
    base = {
        "title": "",
        "address": "",
        "description": "",
        "category": "",
        "source_url": "https://www.example.cn/detail.html",
    }
    base.update(overrides)
    return matching.RecordEvidence(**base)


HANGZHOU_CITY = matching.RegionCriterion(province="浙江省", city="杭州市")


class CountyLevelDivisionTest(unittest.TestCase):
    """The full county-level set of each listed city, not just its districts."""

    def test_a_county_level_city_of_hangzhou_belongs_to_hangzhou(self) -> None:
        verdict = matching.match_region(
            evidence(title="建德市某某宠物用品有限公司", address="浙江省杭州市建德市新安江街道 1 号"),
            HANGZHOU_CITY,
        )
        self.assertEqual(verdict.status, matching.STATUS_MATCH, verdict.reason)

    def test_a_county_of_hangzhou_belongs_to_hangzhou(self) -> None:
        for name in ("桐庐县", "淳安县"):
            verdict = matching.match_region(
                evidence(title=f"杭州某某宠物有限公司（{name}）", address=f"浙江省杭州市{name}某路 1 号"),
                HANGZHOU_CITY,
            )
            self.assertEqual(verdict.status, matching.STATUS_MATCH, f"{name}: {verdict.reason}")

    def test_the_hangzhou_table_holds_all_thirteen_divisions(self) -> None:
        divisions = matching.CITY_DISTRICTS["杭州"]
        self.assertEqual(len(divisions), 13)
        for name in ("建德市", "桐庐县", "淳安县"):
            self.assertIn(name, divisions)

    def test_a_division_of_another_city_is_still_not_hangzhou(self) -> None:
        # The fix must not have widened the rule into "any county-level name".
        verdict = matching.match_region(
            evidence(title="义乌市某某宠物用品有限公司", address="浙江省金华市义乌市某路 1 号"),
            HANGZHOU_CITY,
        )
        self.assertNotEqual(verdict.status, matching.STATUS_MATCH, verdict.reason)

    def test_an_unknown_division_is_still_not_assumed(self) -> None:
        verdict = matching.match_region(
            evidence(title="某某宠物有限公司", address="浙江省杭州市某某新区某路 1 号"),
            HANGZHOU_CITY,
        )
        self.assertNotEqual(verdict.status, matching.STATUS_MATCH, verdict.reason)


class IndustryProfileRegistryTest(unittest.TestCase):
    def test_the_registry_is_not_pet_only(self) -> None:
        names = set(matching.INDUSTRY_PROFILES)
        self.assertGreaterEqual(len(names), 4)
        self.assertIn("food_service", names)
        self.assertIn("building_materials", names)

    def test_every_profile_says_whether_it_was_verified(self) -> None:
        for name, entry in matching.INDUSTRY_PROFILES.items():
            status = matching.industry_profile_status(name)
            self.assertTrue(status, name)
            if entry.verified:
                self.assertIn("已用真实样本验证", status, name)
            else:
                self.assertIn("尚未验证", status, name)

    def test_the_pet_vocabulary_is_the_verified_one(self) -> None:
        self.assertTrue(matching.INDUSTRY_PROFILES["pet_related"].verified)
        self.assertTrue(matching.INDUSTRY_PROFILES["pet_store"].verified)

    def test_the_new_profiles_say_they_are_unverified(self) -> None:
        self.assertFalse(matching.INDUSTRY_PROFILES["food_service"].verified)
        self.assertFalse(matching.INDUSTRY_PROFILES["building_materials"].verified)

    def test_an_unknown_profile_name_is_reported_as_an_unverified_rule(self) -> None:
        status = matching.industry_profile_status("pet_shop")
        self.assertIn("未验证规则", status)
        self.assertIn("pet_shop", status)

    def test_no_profile_at_all_is_not_an_unverified_rule(self) -> None:
        self.assertIn("未启用", matching.industry_profile_status(""))

    def test_a_config_typo_in_a_profile_name_is_refused(self) -> None:
        # Better than silently treating it as custom keywords: a named rule that
        # does not exist would otherwise decide nothing while looking configured.
        with self.assertRaises(ConfigError) as caught:
            validate(
                support.deep_merge(
                    support.BASE_CONFIG, {"match": {"industry": {"profile": "pet_shop"}}}
                ),
                source="<t>",
            )
        self.assertIn("pet_shop", str(caught.exception))


class ExclusionKeywordTest(unittest.TestCase):
    """An explicit exclusion overrules a keyword hit."""

    FOOD = matching.IndustryCriterion(profile="food_service")

    def test_a_packaging_machine_company_is_not_a_food_service_business(self) -> None:
        verdict = matching.match_industry(
            evidence(
                title="杭州某某食品机械有限公司",
                description="主要经营食品包装机械、餐饮设备的制造与销售。",
            ),
            self.FOOD,
        )
        self.assertEqual(verdict.status, matching.STATUS_REJECT, verdict.reason)
        self.assertEqual(verdict.rule, "industry.excluded")
        self.assertIn("食品机械", verdict.reason)

    def test_the_exclusion_beats_a_longer_keyword(self) -> None:
        # "食品" hits the vocabulary and "食品机械" hits the exclusion; the
        # exclusion wins, because the operator named that trade as out of scope.
        verdict = matching.match_industry(
            evidence(title="甲公司", description="主要经营食品机械的制造。"),
            self.FOOD,
        )
        self.assertEqual(verdict.status, matching.STATUS_REJECT, verdict.reason)
        self.assertEqual(verdict.rule, "industry.excluded")

    def test_an_operator_exclusion_is_honoured_too(self) -> None:
        criterion = matching.IndustryCriterion(
            profile="pet_related", exclude_keywords=("宠物殡葬",)
        )
        verdict = matching.match_industry(
            evidence(title="杭州某某宠物服务有限公司", description="主要经营宠物殡葬服务。"),
            criterion,
        )
        self.assertEqual(verdict.status, matching.STATUS_REJECT, verdict.reason)
        self.assertEqual(verdict.rule, "industry.excluded")

    def test_a_genuine_hit_still_matches(self) -> None:
        verdict = matching.match_industry(
            evidence(title="杭州某某餐饮管理有限公司", description="主要经营餐饮服务、团餐配送。"),
            self.FOOD,
        )
        self.assertEqual(verdict.status, matching.STATUS_MATCH, verdict.reason)

    def test_exclusions_are_read_from_the_config(self) -> None:
        cfg = support.config(
            {
                "match": {
                    "industry": {
                        "profile": "pet_related",
                        "exclude_keywords": ["宠物殡葬", "宠物托运"],
                    }
                }
            }
        )
        criteria = matching.criteria_from_config(cfg)
        self.assertEqual(
            criteria.industry.exclude_keywords, ("宠物殡葬", "宠物托运")
        )
        # The profile's own exclusions are added to the operator's, not replaced.
        self.assertIn("包装机械", criteria.industry.exclusions())

    def test_the_industry_report_line_states_the_vocabulary_status(self) -> None:
        criteria = matching.criteria_from_config(
            support.config({"match": {"industry": {"profile": "building_materials"}}})
        )
        text = criteria.describe()
        self.assertIn("建材五金", text)
        self.assertIn("尚未验证", text)

    def test_the_reason_lines_are_not_pet_specific(self) -> None:
        # The prose used to say "未见宠物相关业务" whatever the profile was.
        verdict = matching.match_industry(
            evidence(title="杭州某某五金有限公司", description="主要经营钢材与管件的批发。"),
            matching.IndustryCriterion(profile="food_service"),
        )
        self.assertNotIn("宠物", verdict.reason)


class VocabularySingleSourceTest(unittest.TestCase):
    """The validator reads the vocabulary list; it does not keep its own copy.

    This is the failure that made the whole feature pet-only for a round: the
    matcher gained an industry, the validator still refused it, and the config
    could never name the new question.
    """

    def test_the_config_validator_accepts_every_registry_profile(self) -> None:
        for name in matching.INDUSTRY_PROFILES:
            with self.subTest(profile=name):
                validate(
                    support.deep_merge(
                        support.BASE_CONFIG, {"match": {"industry": {"profile": name}}}
                    ),
                    source="<t>",
                )

    def test_the_validators_list_is_the_registry_itself(self) -> None:
        from collector import config as config_module

        self.assertEqual(
            tuple(config_module.INDUSTRY_PROFILE_NAMES), tuple(matching.INDUSTRY_PROFILES)
        )


class MatchScreenWiringTest(unittest.TestCase):
    """026 P2.6, screen half: an operator can see and type the exclusion list."""

    def test_the_match_block_offers_every_published_profile(self) -> None:
        block = local_ui._domestic_block()
        for name in matching.INDUSTRY_PROFILES:
            self.assertIn(f'value="{name}"', block, name)

    def test_the_match_block_states_the_vocabulary_status(self) -> None:
        block = local_ui._domestic_block()
        self.assertIn("match-vocab-status", block)
        self.assertIn("词表已用真实样本验证", block)
        self.assertIn("词表尚未验证", block)

    def test_the_match_block_has_an_exclusion_field(self) -> None:
        block = local_ui._domestic_block()
        self.assertIn('name="match_exclude_keywords"', block)
        # The form key must be declared, or the field is dropped before the
        # config is built and the typed words never reach the matcher.
        self.assertIn("match_exclude_keywords", local_ui.FORM_FIELDS)

    def test_the_labels_come_from_the_registry(self) -> None:
        labels = dict(local_ui.MATCH_PROFILE_LABELS)
        self.assertIn("", labels)  # "do not match" is still offered
        for name, entry in matching.INDUSTRY_PROFILES.items():
            self.assertIn(name, labels, name)
            self.assertIn(entry.label_zh, labels[name], name)


class ExclusionReachesTheConfigTest(unittest.TestCase):
    """The typed exclusion words survive the screen -> config -> matcher path."""

    def _one(self, **values):
        def one(key: str) -> str:
            return str(values.get(key, ""))

        return one

    def test_the_screen_writes_the_exclusion_list(self) -> None:
        node = local_ui._match_node(
            self._one(
                match_profile="building_materials",
                match_exclude_keywords="工程施工，设计院",
            )
        )
        self.assertEqual(
            node["industry"]["exclude_keywords"], ["工程施工", "设计院"]
        )

    def test_an_exclusion_only_run_is_refused_not_silently_ignored(self) -> None:
        # Typing only exclusions is a question the matcher cannot answer: there
        # is no positive hit for them to remove. The screen still writes the
        # section, and the loader refuses it by name -- better an error that
        # names the field than a filter that quietly does nothing.
        node = local_ui._match_node(self._one(match_exclude_keywords="建筑"))
        self.assertIsNotNone(node)
        with self.assertRaises(ConfigError) as caught:
            validate(support.deep_merge(support.BASE_CONFIG, {"match": node}), source="<t>")
        self.assertIn("exclude_keywords", str(caught.exception))

    def test_an_exclusion_alongside_a_profile_is_accepted(self) -> None:
        node = local_ui._match_node(
            self._one(match_profile="food_service", match_exclude_keywords="食品机械")
        )
        out = validate(support.deep_merge(support.BASE_CONFIG, {"match": node}), source="<t>")
        self.assertEqual(out["match"]["industry"]["exclude_keywords"], ["食品机械"])

    def test_no_matching_at_all_still_writes_nothing(self) -> None:
        self.assertIsNone(local_ui._match_node(self._one()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
