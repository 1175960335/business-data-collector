"""021: the region/industry matching engine, its config section and its export.

The case matrix is the task book's own list (021 D.1/D.2): every region shape
and every industry shape a real directory row can take, decided by named rules
rather than by hand. Each test names the rule that must fire, so a behaviour
change has to be a deliberate change to a named rule, and a rule that stops
firing fails loudly instead of quietly letting a record through.

The rows quoted here are the real ones from the two b2b168 runs: the medical
supplier that lists "PET" as a fibre material, the PVC mill that lists
"宠物用革" among its leathers, the apparel company named after a county-level
city it no longer trades from.
"""

from __future__ import annotations

import unittest

from collector import db, export, matching
from collector.config import ConfigError, validate
from collector.sources.base import SourceRecord

from . import support

#: A Zhejiang/Hangzhou criterion: the one the 021 deliverable answers.
HANGZHOU = matching.RegionCriterion(province="浙江省", city="杭州市")
GONGSHU = matching.RegionCriterion(province="浙江省", city="杭州市", district="拱墅区")
#: The broad pet question and the narrow one. ``pet_store``'s own required
#: scope is applied by ``criteria_from_config`` (tested below); here it is
#: spelled out so the rule itself is what is under test.
PET = matching.IndustryCriterion(profile="pet_related")
PET_STORE = matching.IndustryCriterion(profile="pet_store", scope="store_service")


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


def region_verdict(address: str, title: str = "", criterion=HANGZHOU):
    return matching.match_region(evidence(title=title, address=address), criterion)


def industry_verdict(title: str, description: str = "", category: str = "", criterion=PET):
    return matching.match_industry(
        evidence(title=title, description=description, category=category), criterion
    )


class RegionRuleTest(unittest.TestCase):
    """021 D.1 -- every region shape the task book lists."""

    def test_another_zhejiang_city_is_rejected(self):
        verdict = region_verdict("浙江省宁波市海曙区中山东路1号")
        self.assertEqual(verdict.status, matching.STATUS_REJECT)
        self.assertEqual(verdict.rule, "region.city_mismatch")
        self.assertEqual(verdict.matched_value, "宁波市")

    def test_out_of_province_is_rejected(self):
        verdict = region_verdict("安徽省合肥市新站区濉溪东路1号")
        self.assertEqual(verdict.status, matching.STATUS_REJECT)
        self.assertEqual(verdict.rule, "region.province_mismatch")

        # A second out-of-province row from the real run: Hubei, not Zhejiang.
        verdict = region_verdict("湖北省宜昌市夷陵区发展大道166号")
        self.assertEqual(verdict.rule, "region.province_mismatch")

    def test_clearly_in_the_target_city_matches(self):
        verdict = region_verdict("浙江省杭州市西湖区振华路298号")
        self.assertEqual(verdict.status, matching.STATUS_MATCH)
        self.assertEqual(verdict.rule, "region.city_match_with_district")

    def test_a_city_level_target_does_not_exclude_its_districts(self):
        # 萧山/余杭/滨江 are all Hangzhou; none may be dropped for not being the
        # city centre. This is the rule the three matched manufacturers rest on.
        for district in ("萧山区", "余杭区", "滨江区", "拱墅区", "上城区"):
            verdict = region_verdict(f"浙江省杭州市{district}某路1号")
            self.assertEqual(verdict.status, matching.STATUS_MATCH, district)
            self.assertEqual(verdict.rule, "region.city_match_with_district", district)

    def test_a_specified_district_target_is_judged_at_that_granularity(self):
        verdict = region_verdict("浙江省杭州市拱墅区信义坊99号", criterion=GONGSHU)
        self.assertEqual(verdict.status, matching.STATUS_MATCH)
        self.assertEqual(verdict.rule, "region.district_match")

        verdict = region_verdict("浙江省杭州市西湖区振华路298号", criterion=GONGSHU)
        self.assertEqual(verdict.status, matching.STATUS_REJECT)
        self.assertEqual(verdict.rule, "region.district_mismatch")

        verdict = region_verdict("浙江省杭州市春晓路527号", criterion=GONGSHU)
        self.assertEqual(verdict.status, matching.STATUS_UNKNOWN)
        self.assertEqual(verdict.rule, "region.district_absent")

    def test_a_bare_province_is_not_the_city(self):
        # "浙江" must never read as "杭州" -- the task book says so in as many
        # words. An address that stops at the province is a question.
        verdict = region_verdict("浙江省")
        self.assertEqual(verdict.status, matching.STATUS_UNKNOWN)
        self.assertEqual(verdict.rule, "region.province_only")

    def test_an_incomplete_address_is_left_for_review(self):
        verdict = region_verdict("九环路48号6号楼东二层")
        self.assertEqual(verdict.status, matching.STATUS_UNKNOWN)

    def test_a_historical_district_name_is_never_a_rejection(self):
        # 江干区 was withdrawn in April 2021; its territory is still Hangzhou.
        # Rejecting the record would drop a real Hangzhou company over a renaming.
        #
        # 026 P2.6: the verdict now separates 地理归属 (settled by the published
        # adjustment) from 现址新鲜度 (settled by nothing). For a *city-level*
        # target the first is decided by the mapping, so this is a match; the
        # old blanket 待确认 made a real Hangzhou company look unverified over a
        # renaming.
        verdict = region_verdict("浙江省杭州市江干区九环路48号")
        self.assertEqual(verdict.status, matching.STATUS_MATCH)
        self.assertEqual(verdict.rule, "region.historical_district_in_city")
        self.assertIn("2021年4月", verdict.reason)
        self.assertIn("上城区", verdict.reason)
        # ...and the freshness caveat is stated rather than hidden.
        self.assertIn("现址新鲜度未知", verdict.reason)

        # The suffix-less spelling the source actually used must behave the same.
        verdict = region_verdict("浙江省杭州江干区九环路48号")
        self.assertEqual(verdict.rule, "region.historical_district_in_city")

    def test_a_split_historical_district_is_not_decided_for_a_district_target(self):
        # 江干区's territory went to 上城区 *and* 钱塘区. Asking for one of them
        # cannot be answered from the old name alone.
        target = matching.RegionCriterion(province="浙江省", city="杭州市", district="上城区")
        verdict = matching.match_region(
            evidence(address="浙江省杭州市江干区九环路48号"), target
        )
        self.assertEqual(verdict.status, matching.STATUS_UNKNOWN)
        self.assertEqual(verdict.rule, "region.historical_district_split")
        self.assertIn("钱塘区", verdict.reason)

    def test_a_whole_historical_district_follows_its_successor(self):
        # 下城区 was merged into 拱墅区 in full, so asking for 拱墅区 is decidable.
        target = matching.RegionCriterion(province="浙江省", city="杭州市", district="拱墅区")
        verdict = matching.match_region(
            evidence(address="浙江省杭州市下城区体育场路1号"), target
        )
        self.assertEqual(verdict.status, matching.STATUS_MATCH)
        self.assertEqual(verdict.rule, "region.historical_district_mapped")

    def test_asking_for_the_withdrawn_name_itself_is_decided_by_the_mapping(self):
        target = matching.RegionCriterion(province="浙江省", city="杭州市", district="江干区")
        verdict = matching.match_region(
            evidence(address="浙江省杭州市江干区九环路48号"), target
        )
        self.assertEqual(verdict.status, matching.STATUS_MATCH)
        self.assertEqual(verdict.rule, "region.historical_district_mapped")

    def test_a_whole_historical_district_elsewhere_is_a_rejection(self):
        # The mapping is what makes "not here" decidable; without it this would
        # have to stay a question.
        target = matching.RegionCriterion(province="浙江省", city="杭州市", district="上城区")
        verdict = matching.match_region(
            evidence(address="浙江省杭州市下城区体育场路1号"), target
        )
        self.assertEqual(verdict.status, matching.STATUS_REJECT)
        self.assertEqual(verdict.rule, "region.historical_district_elsewhere")

    def test_the_historical_table_records_the_split_it_reports(self):
        # A successor list of one inside a table that claims a split (or the
        # other way round) is how the reason and the verdict drift apart.
        for name, entry in matching.HISTORICAL_DIVISIONS.items():
            self.assertIn(entry.successor, entry.successors, name)
            self.assertEqual(entry.split, len(entry.parts) > 1, name)

    def test_an_unknown_withdrawn_name_is_still_a_question(self):
        # No mapping, no decision: the conservative branch stays.
        self.assertFalse(matching.is_historical_division("某某区"))
        target = matching.RegionCriterion(province="浙江省", city="杭州市")
        verdict = matching.match_region(
            evidence(address="浙江省杭州市某某区某路1号"), target
        )
        self.assertNotEqual(verdict.status, matching.STATUS_MATCH)

    def test_a_name_that_disagrees_with_the_address_downgrades_only(self):
        # 桐乡市祺祥服饰 over a Hangzhou address: a branch or stale data, and
        # only a human can tell which. It must never turn into a pass.
        verdict = region_verdict(
            "浙江省杭州市拱墅区丰元国际大厦C座11楼", title="桐乡市祺祥服饰有限公司"
        )
        self.assertEqual(verdict.status, matching.STATUS_UNKNOWN)
        self.assertEqual(verdict.rule, "region.title_address_conflict")
        self.assertIn("桐乡市", verdict.reason)

    def test_a_name_that_agrees_with_the_address_adds_nothing(self):
        # A name is not evidence of location: it cannot *upgrade* a verdict.
        verdict = region_verdict(
            "安徽省合肥市新站区濉溪东路1号", title="合肥某某医疗器械有限公司"
        )
        self.assertEqual(verdict.status, matching.STATUS_REJECT)
        self.assertEqual(verdict.rule, "region.province_mismatch")

    def test_a_district_nobody_can_place_is_a_question(self):
        # 海宁市 is a county-level city under Jiaxing, and it is not in
        # Hangzhou's ten-district table: the rule refuses to guess either way.
        verdict = region_verdict("浙江省杭州市海宁市隆兴路1号")
        self.assertEqual(verdict.status, matching.STATUS_UNKNOWN)
        self.assertEqual(verdict.rule, "region.district_unplaced")

    def test_no_address_at_all_is_not_a_pass(self):
        verdict = region_verdict("")
        self.assertEqual(verdict.status, matching.STATUS_UNKNOWN)
        self.assertEqual(verdict.rule, "region.no_address")

    def test_an_unconfigured_dimension_decides_nothing(self):
        verdict = matching.match_region(
            evidence(address="Irgendwo in Berlin"), matching.RegionCriterion()
        )
        self.assertEqual(verdict.status, matching.STATUS_NOT_APPLICABLE)
        self.assertEqual(verdict.rule, "region.not_configured")

    def test_the_address_evidence_is_the_address_verbatim(self):
        address = "浙江省杭州市西湖区振华路298号西港新界西区3幢1001室"
        verdict = region_verdict(address)
        self.assertEqual(verdict.evidence[0].text, address)
        self.assertEqual(verdict.evidence[0].field, "address")


class IndustryRuleTest(unittest.TestCase):
    """021 D.2 -- every industry shape the task book lists."""

    def test_the_name_says_pet_but_the_business_is_other(self):
        # 爱尚's *name* is pet, but the profile block in this row describes
        # medical supports. The name alone must not carry the verdict.
        verdict = industry_verdict(
            "杭州爱尚宠物用品有限公司",
            "欢迎来到杭州爱尚宠物用品有限公司网站，具体地址是浙江省杭州江干区天成路92号。"
            "主要经营护腰带; 腰椎牵引器; 颈椎牵引器; 颈托。 未知。",
        )
        self.assertEqual(verdict.status, matching.STATUS_REJECT)
        self.assertEqual(verdict.rule, "industry.other_business")
        self.assertIn("名称虽含宠物", verdict.reason)

    def test_the_name_lacks_pet_but_the_business_is_pet(self):
        # The three real matches of this shape: a trading/e-commerce company
        # whose own product line is pet food and pet supplies.
        for title, business in (
            ("杭州秀游电子商务有限公司", "宠物用品,狗粮,狗链,狗窝"),
            ("杭州依曼进出口有限公司", "家纺,家居用品,家用纺织品,宠物用品"),
            ("杭州史尼芙贸易有限公司", "宠物服装,狗粮,零食,狗狗湿粮,猫猫罐头"),
        ):
            verdict = industry_verdict(
                title, f"欢迎来到{title}网站，联系人是方强。 主要经营{business}。 未知。"
            )
            self.assertEqual(verdict.status, matching.STATUS_MATCH, title)
            self.assertEqual(verdict.rule, "industry.business_match", title)

    def test_a_material_abbreviation_is_not_an_industry_hit(self):
        # 祥茂医疗 lists "PET" -- the fibre -- among its products. A bare
        # English "pet" in the vocabulary would hand a medical supplier to a
        # pet list, which is why PET_KEYWORDS does not carry it.
        verdict = industry_verdict(
            "杭州祥茂医疗用品有限公司", "主要经营PET,医用胶带、无纺布,急救包（箱）及其配件。"
        )
        self.assertEqual(verdict.status, matching.STATUS_REJECT)
        self.assertEqual(verdict.rule, "industry.other_business")
        # The reason may *name* the word it failed to find; it must not claim a hit.
        self.assertNotIn("命中", verdict.reason)
        self.assertEqual(verdict.matched_value, "")

    def test_the_name_inside_the_profile_intro_is_not_business_evidence(self):
        # b2b168's profile opens with "欢迎来到<name>网站". Without the
        # 主要经营 split, every company with 宠物 in its name would false-hit
        # on its own introduction.
        verdict = industry_verdict(
            "杭州利旺宠物用品有限公司",
            "欢迎来到杭州利旺宠物用品有限公司网站，具体地址是浙江省杭州余杭区星桥街道。"
            "主要经营挂钩,螺丝批插座,套装工具。 未知。",
        )
        self.assertEqual(verdict.status, matching.STATUS_REJECT)
        self.assertEqual(verdict.rule, "industry.other_business")

    def test_the_name_alone_is_not_a_pass(self):
        verdict = industry_verdict("杭州爱尚宠物用品有限公司")
        self.assertEqual(verdict.status, matching.STATUS_UNKNOWN)
        self.assertEqual(verdict.rule, "industry.name_only")

    def test_the_column_heading_alone_is_not_a_pass(self):
        # A directory's category column is a label, not the company's own words.
        verdict = industry_verdict("杭州某某贸易有限公司", category="宠物")
        self.assertEqual(verdict.status, matching.STATUS_UNKNOWN)
        self.assertEqual(verdict.rule, "industry.category_only")

    def test_the_name_and_the_classification_agreeing_is_enough(self):
        verdict = industry_verdict("杭州爱尚宠物用品有限公司", category="宠物用品")
        self.assertEqual(verdict.status, matching.STATUS_MATCH)
        self.assertEqual(verdict.rule, "industry.name_plus_category")

    def test_a_products_scope_is_recorded_and_kept_apart_from_a_store(self):
        # The mill whose leather list includes 宠物用革: pet-related under the
        # broad question, and its scope says it is goods, not a shop.
        verdict = industry_verdict(
            "浙江圣山塑胶有限公司",
            "主要经营PVC雨衣革; PVC涤纶布; 玩具革; 宠物用革; 服装革。",
        )
        self.assertEqual(verdict.status, matching.STATUS_MATCH)
        self.assertEqual(verdict.matched_value, "宠物")
        self.assertEqual(verdict.rule, "industry.business_match")

    def test_a_supplier_is_not_handed_over_as_a_store(self):
        # The narrow question ("线下宠物店与服务") must reject the supplier.
        verdict = industry_verdict(
            "杭州利旺宠物用品有限公司",
            "主要经营宠物窝,宠物衣服,狗链,狗碗,宠物玩具。",
            criterion=PET_STORE,
        )
        self.assertEqual(verdict.status, matching.STATUS_REJECT)
        self.assertEqual(verdict.rule, "industry.scope_mismatch")
        self.assertIn("线下门店", verdict.reason)

    def test_an_offline_store_matches_the_narrow_question(self):
        verdict = industry_verdict(
            "杭州市拱墅区佳友宠物店",
            "主要经营宠物犬销售,狗狗洗澡美容,狗狗寄养,狗狗婚配,宠物用品销售。",
            criterion=PET_STORE,
        )
        self.assertEqual(verdict.status, matching.STATUS_MATCH)
        self.assertEqual(verdict.rule, "industry.business_match")

    def test_no_evidence_either_way_is_not_a_pass(self):
        verdict = industry_verdict("杭州方祥医疗器械有限公司")
        self.assertEqual(verdict.status, matching.STATUS_UNKNOWN)
        self.assertEqual(verdict.rule, "industry.no_evidence")

    def test_an_unconfigured_dimension_decides_nothing(self):
        verdict = industry_verdict(
            " anything", description="主要经营宠物用品", criterion=matching.IndustryCriterion()
        )
        self.assertEqual(verdict.status, matching.STATUS_NOT_APPLICABLE)


class CombineRuleTest(unittest.TestCase):
    """021 A -- how the two dimensions become one verdict."""

    def combined(self, address: str, title: str, description: str):
        result = matching.match_record(
            evidence(title=title, address=address, description=description),
            matching.MatchCriteria(region=HANGZHOU, industry=PET),
        )
        return result

    def test_both_dimensions_matching_is_the_only_way_in(self):
        result = self.combined(
            "浙江省杭州市西湖区振华路298号",
            "杭州依曼进出口有限公司",
            "主要经营家纺,家居用品,宠物用品。",
        )
        self.assertEqual(result.status, matching.STATUS_MATCH)

    def test_one_explicit_mismatch_is_enough_to_reject(self):
        # Region fine, industry fine in name but the business is other trade.
        result = self.combined(
            "浙江省杭州市拱墅区德胜巷1号",
            "杭州碧蓝科技有限公司",
            "主要经营节能开水器; 饮水设备。",
        )
        self.assertEqual(result.status, matching.STATUS_REJECT)

        # Out of province even though the business is pet trade.
        result = self.combined(
            "安徽省合肥市濉溪东路1号", "合肥爱尚宠物用品有限公司", "主要经营狗粮,宠物用品。"
        )
        self.assertEqual(result.status, matching.STATUS_REJECT)

    def test_insufficient_evidence_stays_a_question(self):
        # An unplaceable division is still a question: there is no mapping to
        # decide it with, so the conservative branch holds.
        result = self.combined(
            "浙江省杭州市某某新区某路92号",
            "杭州爱尚宠物用品有限公司",
            "主要经营狗粮,宠物用品,猫粮。",
        )
        self.assertEqual(result.status, matching.STATUS_UNKNOWN)

    def test_a_renamed_district_is_a_question_no_longer(self):
        # Same record as before, but the address uses 江干区 -- withdrawn in
        # April 2021. 026 P2.6: the published adjustment settles 地理归属, so a
        # city-level target no longer needs a human for a renaming. (Under rule
        # version /1 this was 待确认, which is exactly the false negative this
        # fixes; the workbook states the rule version it was decided under.)
        result = self.combined(
            "浙江省杭州市江干区闸弄口天成路92号",
            "杭州爱尚宠物用品有限公司",
            "主要经营狗粮,宠物用品,猫粮。",
        )
        self.assertEqual(result.status, matching.STATUS_MATCH)
        self.assertEqual(result.region.rule, "region.historical_district_in_city")
        # The freshness caveat travels with the verdict rather than being lost.
        self.assertIn("现址新鲜度未知", result.region.reason)

    def test_nothing_configured_decides_nothing(self):
        result = matching.match_record(
            evidence(address="浙江省杭州市西湖区1号", description="主要经营宠物用品"),
            matching.MatchCriteria(),
        )
        self.assertEqual(result.status, matching.STATUS_NOT_APPLICABLE)

    def test_a_verdict_carries_its_rule_version_and_evidence(self):
        result = self.combined(
            "浙江省杭州市余杭区星桥街道星灵街10-1号",
            "杭州利旺宠物用品有限公司",
            "主要经营宠物窝,宠物衣服,狗链。",
        )
        payload = result.as_dict()
        self.assertEqual(payload["rule_version"], matching.MATCH_RULE_VERSION)
        self.assertTrue(payload["judged_at"])
        texts = [item["text"] for item in payload["industry"]["evidence"]]
        self.assertTrue(any("宠物窝" in text for text in texts))
        self.assertTrue(payload["region"]["evidence"])


class MatchConfigTest(unittest.TestCase):
    """021 A/D.4 -- the ``match`` section is explainable and changes nothing else."""

    def validate_with(self, match_node):
        # The shared base config, so only the match section differs.
        return validate(support.deep_merge(support.BASE_CONFIG, {"match": match_node}))

    def test_the_profile_carries_its_own_scope(self):
        # Naming pet_store is asking the narrow question; the criterion gets
        # the profile's required scope without the operator repeating it.
        data = self.validate_with(
            {"region": {"province": "浙江省", "city": "杭州市"}, "industry": {"profile": "pet_store"}}
        )
        cfg = support.config({"match": data["match"]})
        criteria = matching.criteria_from_config(cfg)
        self.assertEqual(criteria.industry.profile, "pet_store")
        self.assertEqual(criteria.industry.scope, "store_service")

    def test_an_unknown_dimension_key_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            self.validate_with({"region": {"province": "浙江省"}, "industry": {}, "scope": "x"})
        self.assertIn("match", str(caught.exception))

    def test_an_unknown_profile_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            self.validate_with({"industry": {"profile": "dogs"}})
        self.assertIn("match.industry.profile", str(caught.exception))

    def test_an_unknown_scope_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            self.validate_with({"industry": {"profile": "pet_related", "scope": "online"}})
        self.assertIn("match.industry.scope", str(caught.exception))

    def test_a_match_section_with_no_condition_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            self.validate_with({})
        self.assertIn("no condition configured", str(caught.exception))

    def test_an_unconfigured_dimension_reports_not_applicable(self):
        # Region only: the industry dimension decides nothing rather than
        # inventing a condition.
        data = self.validate_with({"region": {"province": "浙江省", "city": "杭州市"}})
        cfg = support.config({"match": data["match"]})
        criteria = matching.criteria_from_config(cfg)
        self.assertTrue(criteria.region.configured)
        self.assertFalse(criteria.industry.configured)
        verdict = matching.match_industry(
            evidence(title="杭州依曼进出口有限公司", description="主要经营宠物用品"), criteria.industry
        )
        self.assertEqual(verdict.status, matching.STATUS_NOT_APPLICABLE)

    def test_the_match_section_does_not_change_the_fingerprint(self):
        # ``match`` is a reporting question, not a collection one: the same run
        # keeps its fingerprint, so a historical run's checkpoints are reused
        # and its data is not recollected to answer a report question.
        without = support.config({"export": {"fields": ["title"], "formats": ["csv"]}})
        with_match = support.config(
            {
                "match": {
                    "region": {"province": "浙江省", "city": "杭州市"},
                    "industry": {"profile": "pet_related"},
                },
                "export": {"fields": ["title"], "formats": ["csv"]},
            }
        )
        self.assertEqual(without.fingerprint(), with_match.fingerprint())
        self.assertIsNone(without.match_data)
        self.assertEqual(with_match.match_data["industry"]["profile"], "pet_related")

    def test_a_config_without_match_decides_nothing(self):
        cfg = support.config({})
        self.assertIsNone(matching.criteria_from_config(cfg))


class SourceDescriptionTest(unittest.TestCase):
    """021 A -- the description reaches the database as the source's own words."""

    def test_the_description_is_kept_verbatim_in_the_payload(self):
        text = "欢迎来到杭州利旺宠物用品有限公司网站。 主要经营宠物窝,宠物衣服,狗链。"
        record = SourceRecord(
            title="杭州利旺宠物用品有限公司",
            address="浙江省杭州市余杭区星桥街道星灵街10-1号",
            source_id="https://www.b2b168.com/c168-13877823.html",
            description=text,
        )
        raw = record.as_raw()
        self.assertEqual(raw["description"], text)

    def test_an_absent_description_stays_absent(self):
        record = SourceRecord(
            title="无简介公司",
            address="浙江省杭州市西湖区1号",
            source_id="https://www.example.cn/x.html",
        )
        self.assertNotIn("description", record.as_raw())


MATCH_EXPORT_FIELDS = ["entity_id", "stable_key", "title", "address", "phone", "website"]

#: Three rows with the shapes the deliverable is judged on: two pet traders in
#: Hangzhou (one of them under a district renamed in 2021, which rule version /2
#: matches by geographic attribution), and a medical supplier whose name says
#: nothing about pets (no match).
MATCH_ROWS = [
    support.raw_record(
        data_id="m1",
        title="杭州依曼进出口有限公司",
        address="浙江省杭州市西湖区振华路298号",
        description="欢迎来到杭州依曼进出口有限公司网站。 主要经营家纺,家居用品,宠物用品。",
    ),
    support.raw_record(
        data_id="m2",
        title="杭州爱尚宠物用品有限公司",
        address="浙江省杭州江干区闸弄口天成路92号",
        description="欢迎来到杭州爱尚宠物用品有限公司网站。 主要经营狗粮,宠物用品,猫粮。",
    ),
    support.raw_record(
        data_id="m3",
        title="杭州方祥医疗器械有限公司",
        address="浙江省杭州市江干区九环路48号",
        description="欢迎来到杭州方祥医疗器械有限公司网站。 主要经营护腰带; 腰椎牵引器。",
    ),
]


class ReaderWorkbookMatchTest(unittest.TestCase):
    """021 B/D -- the reader workbook splits the three groups, honestly."""

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
            target_count=25,
        )
        self.raw_dir = self.tmp / "raw"
        self.raw_dir.mkdir()
        self.out_dir = self.tmp / "export"
        self.match_cfg = support.config(
            {
                "collect": {"fields": ["title", "address", "phone", "website", "description"]},
                "match": {
                    "region": {"province": "浙江省", "city": "杭州市"},
                    "industry": {"profile": "pet_related"},
                },
                "export": {"fields": MATCH_EXPORT_FIELDS, "formats": ["xlsx"]},
            }
        )
        self.plain_cfg = support.config(
            {
                "collect": {"fields": ["title", "address", "phone", "website", "description"]},
                "export": {"fields": MATCH_EXPORT_FIELDS, "formats": ["xlsx"]},
            }
        )

    def seed(self, records=MATCH_ROWS):
        with open(self.raw_dir / "a.jsonl", "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(
                    __import__("json").dumps(support.envelope(record), ensure_ascii=False) + "\n"
                )
        from collector import ingest

        return ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=self.match_cfg)

    def book(self, cfg=None, records=MATCH_ROWS):
        self.seed(records)
        paths = export.export_run(self.conn, self.run_id, cfg or self.match_cfg, self.out_dir)
        import openpyxl

        return paths, openpyxl.load_workbook(paths["xlsx_bilingual"])

    def test_a_config_with_match_writes_three_group_sheets(self):
        _paths, workbook = self.book()
        self.assertEqual(
            workbook.sheetnames,
            ["符合名单 · Matched", "待确认 · Needs review", "不符合 · No match", "概览", "来源与字段说明"],
        )

    def test_a_config_without_match_writes_the_historical_three_sheets(self):
        _paths, workbook = self.book(self.plain_cfg)
        self.assertEqual(workbook.sheetnames, ["商家名单", "概览", "来源与字段说明"])
        sheet = workbook["商家名单"]
        headers = [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]
        self.assertFalse(any("匹配" in str(h) or "Verdict" in str(h) for h in headers))

    def test_the_three_groups_are_exclusive_and_cover_every_row(self):
        _paths, workbook = self.book()
        groups = {}
        # An empty group carries a placeholder sentence instead of rows; it is
        # not a record and must not be counted as one.
        placeholder = "本组没有记录。 / No records in this group."
        for name, status in (
            ("符合名单 · Matched", "match"),
            ("待确认 · Needs review", "unknown"),
            ("不符合 · No match", "reject"),
        ):
            sheet = workbook[name]
            headers = [c.value for c in sheet[1]]
            rows = [
                dict(zip(headers, [c.value for c in row]))
                for row in sheet.iter_rows(min_row=2)
                if row[0].value is not None and row[0].value != placeholder
            ]
            groups[status] = rows
        total = sum(len(rows) for rows in groups.values())
        self.assertEqual(total, 3)
        names = [row["商家名称 / Business name"] for rows in groups.values() for row in rows]
        self.assertEqual(len(names), len(set(names)))
        # m2 sits in 江干区 -- withdrawn in 2021. Under rule version /2 the
        # published adjustment decides 地理归属, so a city-level target matches
        # it; only the freshness of the address is unverified, which the reason
        # states. (Under /1 this row was the class's 待确认 example.)
        self.assertEqual(
            [r["商家名称 / Business name"] for r in groups["match"]],
            ["杭州依曼进出口有限公司", "杭州爱尚宠物用品有限公司"],
        )
        self.assertEqual([r["商家名称 / Business name"] for r in groups["unknown"]], [])
        self.assertEqual([r["商家名称 / Business name"] for r in groups["reject"]],
                         ["杭州方祥医疗器械有限公司"])

    def test_the_overview_states_the_counts_the_criteria_and_the_rule(self):
        _paths, workbook = self.book()
        overview = workbook["概览"]
        text = "\n".join(
            str(overview.cell(row=r, column=c).value)
            for r in range(1, overview.max_row + 1)
            for c in range(1, overview.max_column + 1)
            if overview.cell(row=r, column=c).value is not None
        )
        self.assertIn("浙江省杭州市", text)
        self.assertIn("宠物相关企业", text)
        self.assertIn(matching.MATCH_RULE_VERSION, text)
        self.assertIn("三组互斥", text)
        # The collected-versus-matched relationship must be stated, so reaching
        # the target count can never be mistaken for satisfying the question.
        self.assertIn("只约束采集数量，不等于符合数量", text)
        # 026 P2.6: and the vocabulary's own verified state, so a count of 符合
        # reads next to the rule that produced it.
        self.assertIn("行业词表状态", text)
        self.assertIn("词表已用真实样本验证", text)

    def test_an_unverified_vocabulary_is_flagged_in_the_overview(self):
        # The point of the status line: a word list nobody checked must not read
        # like an instrument reading.
        cfg = support.config(
            {
                "collect": {"fields": ["title", "address", "phone", "website", "description"]},
                "match": {
                    "region": {"province": "浙江省", "city": "杭州市"},
                    "industry": {"profile": "building_materials"},
                },
                "export": {"fields": MATCH_EXPORT_FIELDS, "formats": ["xlsx"]},
            }
        )
        _paths, workbook = self.book(cfg=cfg)
        overview = workbook["概览"]
        text = "\n".join(
            str(overview.cell(row=r, column=c).value)
            for r in range(1, overview.max_row + 1)
            for c in range(1, overview.max_column + 1)
            if overview.cell(row=r, column=c).value is not None
        )
        self.assertIn("建材五金", text)
        self.assertIn("词表尚未验证", text)
        # Unless the overview names the operator's own exclusions, a reader
        # cannot re-derive a verdict that an exclusion decided.
        self.assertIn("排除词", text)

    def test_an_empty_group_says_so_instead_of_looking_unexported(self):
        # One record, and it matches: the 不符合 sheet has to say "none", and
        # the overview has to say the matched group is the whole batch -- not
        # stay silent, which reads as "this sheet was never written".
        records = [
            support.raw_record(
                data_id="only",
                title="杭州依曼进出口有限公司",
                address="浙江省杭州市西湖区振华路298号",
                description="主要经营宠物用品。",
            )
        ]
        _paths, workbook = self.book(records=records)
        empty_text = "本组没有记录。 / No records in this group."
        sheet = workbook["不符合 · No match"]
        self.assertEqual(sheet.cell(row=2, column=1).value, empty_text)
        overview = workbook["概览"]
        text = "\n".join(
            str(overview.cell(row=r, column=c).value)
            for r in range(1, overview.max_row + 1)
            for c in range(1, overview.max_column + 1)
            if overview.cell(row=r, column=c).value is not None
        )
        # The zero group is stated, and the "no match at all" statement is not
        # printed when there *is* a match.
        self.assertIn("不符合（明确不在条件内） / No match\n0", text)
        self.assertNotIn("符合名单为空", text)

    def test_a_record_the_matcher_did_not_see_is_not_a_silent_pass(self):
        # A row missing from the verdict map must come out 待确认 with the
        # reason attached, never as an empty cell a reader takes for a pass.
        self.seed()
        rows = export.reader_rows(self.conn, self.run_id, self.match_cfg, "zh", matches={})
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["match_status"], "待确认")
            self.assertIn("没有参与匹配", row["match_reason"])
            self.assertEqual(row["match_region"], "未判定")

    def test_the_verdict_is_recomputed_at_every_export(self):
        # 021 D.5: nothing is cached. Narrowing the criteria to one district
        # between two exports of the same run empties the matched group.
        self.seed()
        first = export.export_run(self.conn, self.run_id, self.match_cfg, self.out_dir)
        import openpyxl

        workbook = openpyxl.load_workbook(first["xlsx_bilingual"])
        matched = workbook["符合名单 · Matched"]
        self.assertEqual(matched.cell(row=2, column=2).value, "杭州依曼进出口有限公司")

        other = support.config(
            {
                "collect": {"fields": ["title", "address", "phone", "website", "description"]},
                "match": {
                    "region": {"province": "浙江省", "city": "杭州市", "district": "拱墅区"},
                    "industry": {"profile": "pet_related"},
                },
                "export": {"fields": MATCH_EXPORT_FIELDS, "formats": ["xlsx"]},
            }
        )
        second = export.export_run(self.conn, self.run_id, other, self.out_dir)
        workbook = openpyxl.load_workbook(second["xlsx_bilingual"])
        matched = workbook["符合名单 · Matched"]
        self.assertEqual(matched.cell(row=2, column=1).value, "本组没有记录。 / No records in this group.")
        overview = workbook["概览"]
        text = "\n".join(
            str(overview.cell(row=r, column=c).value)
            for r in range(1, overview.max_row + 1)
            for c in range(1, overview.max_column + 1)
            if overview.cell(row=r, column=c).value is not None
        )
        self.assertIn("符合名单为空", text)

    def test_no_verdict_cell_is_a_formula(self):
        # External-text safety: a verdict is prose, and an Excel formula is a
        # way for a cell to execute something. None may appear.
        _paths, workbook = self.book()
        for name in workbook.sheetnames:
            for row in workbook[name].iter_rows():
                for cell in row:
                    self.assertFalse(
                        isinstance(cell.value, str) and cell.value.startswith("="), (name, cell.value)
                    )

    def test_the_key_sheet_carries_the_rule_the_verdicts_rest_on(self):
        _paths, workbook = self.book()
        key = workbook["来源与字段说明"]
        text = "\n".join(
            str(key.cell(row=r, column=c).value)
            for r in range(1, key.max_row + 1)
            for c in range(1, key.max_column + 1)
            if key.cell(row=r, column=c).value is not None
        )
        self.assertIn(matching.MATCH_RULE_VERSION, text)
        self.assertIn("地区判定依据", text)
        self.assertIn("行业判定依据", text)
        self.assertIn("匹配不等于核实", text)

    def test_the_raw_records_and_facts_are_untouched_by_matching(self):
        # 021 B: matching reports, it never rewrites. The raw JSONL and the
        # facts still carry what the source published.
        self.seed()
        before = self.conn.execute(
            "SELECT field, raw_value FROM field_facts ORDER BY entity_id, field"
        ).fetchall()
        export.export_run(self.conn, self.run_id, self.match_cfg, self.out_dir)
        after = self.conn.execute(
            "SELECT field, raw_value FROM field_facts ORDER BY entity_id, field"
        ).fetchall()
        self.assertEqual(before, after)
