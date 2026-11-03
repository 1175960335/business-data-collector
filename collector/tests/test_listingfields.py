"""Round 034, first batch: the fields a *listing row* carries.

The measured failure these tests exist to prevent (a local listing-field
audit run during development): the 顺企网 sample listing page states an address and a
product line inside each of its 100 rows, and the product kept them for exactly
one row — the only row whose detail page happened to be saved. The page was never
the problem; the path was.

The safety property that matters more than coverage: **a value may only come from
inside the row that names the business.** Every test named ``*neighbour*`` or
``*wrapper*`` is an attempt to make the module read a value that belongs to
somebody else, and each one must fail to do so.
"""

from __future__ import annotations

import unittest

from collector.listingfields import (
    LIST_PRESETS,
    PLACEHOLDER_VALUES,
    RowFields,
    as_native,
    fields_for_anchor,
    merge_listing_and_detail,
    preset_labels,
    preset_names,
    read_listing_row,
    resolve_preset,
)
from collector.selector import parse_html

SHUNQI_LI = (
    '<div class="f_l"><h4><a href="//www.11467.com/qiye/1.htm">甲宠物店</a></h4>'
    "<div>主营产品： 宠物美容、宠物寄养</div>"
    "<div>地址： 杭州市西湖区文一路 1 号</div>"
    "<span>成立时间：2021-05-17</span></div>"
)


def rows(html: str, *, selector: str = "li"):
    """Every ``selector`` container in a page, as parsed elements."""
    from collector.selector import select

    return select(parse_html(html), selector)


def one_row(html: str):
    found = rows(html)
    assert len(found) == 1, f"expected one row, got {len(found)}"
    return found[0]


class PresetTest(unittest.TestCase):
    def test_a_verified_site_has_its_own_preset(self) -> None:
        self.assertIn("shunqiwang", preset_names())

    def test_an_unknown_preset_falls_back_to_generic(self) -> None:
        # A config written for a newer collector must still run; the fallback
        # says out loud that it will often find nothing.
        self.assertEqual(resolve_preset("no-such-site")["label"], LIST_PRESETS["generic"]["label"])

    def test_a_preset_states_what_was_verified(self) -> None:
        for name, preset in LIST_PRESETS.items():
            self.assertTrue(preset.get("label"), f"{name} 没有名称")
            self.assertTrue(preset.get("note"), f"{name} 没有说明")
            self.assertTrue(preset.get("labels"), f"{name} 没有标签")

    def test_the_shunqiwang_labels_are_the_ones_the_page_writes(self) -> None:
        self.assertEqual(preset_labels("shunqiwang")["address"], ("地址",))
        self.assertEqual(preset_labels("shunqiwang")["description"], ("主营产品",))


class RowReadingTest(unittest.TestCase):
    def test_a_row_states_its_address_and_product_line(self) -> None:
        fields = read_listing_row(one_row(f"<li>{SHUNQI_LI}</li>"), labels=preset_labels("shunqiwang"))
        self.assertEqual(fields.get("address"), "杭州市西湖区文一路 1 号")
        self.assertEqual(fields.get("description"), "宠物美容、宠物寄养")

    def test_a_field_the_record_model_does_not_have_is_still_kept(self) -> None:
        # 成立时间 is not a record field. "The page had it" is not a reason to
        # drop it: it goes into native so a reader can still see the whole row.
        fields = read_listing_row(one_row(f"<li>{SHUNQI_LI}</li>"), labels=preset_labels("shunqiwang"))
        self.assertEqual(fields.get("est_date"), "2021-05-17")

    def test_a_value_stops_at_the_next_label(self) -> None:
        # Otherwise one row's address would swallow the row's founding date.
        fields = read_listing_row(one_row(f"<li>{SHUNQI_LI}</li>"), labels=preset_labels("shunqiwang"))
        self.assertNotIn("成立时间", fields.get("address") or "")

    def test_the_longest_label_wins(self) -> None:
        # With both "主营产品" and "主营" configured, the short one matches
        # inside the long one and would split the row in the wrong place.
        html = "<li><a href='/q/1.htm'>甲公司</a>主营产品： 宠物食品主营： 无</li>"
        fields = read_listing_row(
            one_row(html),
            labels={"description": ("主营产品", "主营"), "address": ("地址",)},
        )
        self.assertEqual(fields.get("description"), "宠物食品主营： 无")

    def test_a_row_with_no_labels_yields_no_values(self) -> None:
        # Not a failure and not a reason to harvest body text: the page
        # supported a name and nothing else.
        fields = read_listing_row(one_row("<li><a href='/q/1.htm'>甲公司</a></li>"))
        self.assertEqual(fields.values, {})

    def test_the_business_name_is_not_read_back_as_a_value(self) -> None:
        html = "<li><a href='/q/1.htm'>杭州市西湖区</a><div>主营产品： 宠物</div></li>"
        fields = read_listing_row(one_row(html), labels=preset_labels("shunqiwang"))
        self.assertEqual(fields.get("address"), None)


class PlaceholderTest(unittest.TestCase):
    def test_a_dash_is_not_an_address(self) -> None:
        # 顺企网 writes 「地址： -」 for a company whose address it does not hold.
        # Storing the dash would export a value the source never stated.
        html = "<li><a href='/q/1.htm'>甲公司</a><div>地址： -</div></li>"
        fields = read_listing_row(one_row(html), labels=preset_labels("shunqiwang"))
        self.assertIsNone(fields.get("address"))
        self.assertIn("address", fields.blank)

    def test_nothing_here_in_every_spelling_the_pages_use(self) -> None:
        for word in ("-", "暂无", "未公开", "不详", "N/A"):
            with self.subTest(word=word):
                html = f"<li><a href='/q/1.htm'>甲公司</a><div>地址： {word}</div></li>"
                fields = read_listing_row(one_row(html), labels=preset_labels("shunqiwang"))
                self.assertIsNone(fields.get("address"), f"{word!r} 被当成了地址")

    def test_a_real_value_that_merely_starts_with_a_dash_is_kept(self) -> None:
        # Over-eager stripping would eat a genuine address.
        html = "<li><a href='/q/1.htm'>甲公司</a><div>地址： -1 号楼</div></li>"
        fields = read_listing_row(one_row(html), labels=preset_labels("shunqiwang"))
        self.assertEqual(fields.get("address"), "-1 号楼")

    def test_the_placeholder_set_is_visible_to_a_config_author(self) -> None:
        self.assertIn("-", PLACEHOLDER_VALUES)
        self.assertIn("暂无", PLACEHOLDER_VALUES)


class TruncationTest(unittest.TestCase):
    def test_a_value_the_source_cut_short_is_marked_not_repaired(self) -> None:
        html = "<li><a href='/q/1.htm'>甲公司</a><div>主营产品： ...宠物服务...</div></li>"
        fields = read_listing_row(one_row(html), labels=preset_labels("shunqiwang"))
        self.assertIn("description", fields.truncated)
        # Kept verbatim: a repaired description would be our words.
        self.assertIn("...", fields.get("description") or "")

    def test_an_untruncated_value_is_not_marked(self) -> None:
        html = "<li><a href='/q/1.htm'>甲公司</a><div>主营产品： 宠物服务</div></li>"
        fields = read_listing_row(one_row(html), labels=preset_labels("shunqiwang"))
        self.assertEqual(fields.truncated, frozenset())


class RowIsolationTest(unittest.TestCase):
    def test_a_row_holding_two_businesses_is_refused(self) -> None:
        # Reading it would put the neighbour's address on this record.
        html = (
            "<li><a href='/qiye/1.htm'>甲公司</a><div>地址： 甲地址</div>"
            "<a href='/qiye/2.htm'>乙公司</a><div>地址： 乙地址</div></li>"
        )
        self.assertIsNone(fields_for_anchor(
            _first_anchor(html), detail_pattern=_qiye(), preset="shunqiwang"))

    def test_a_wrapper_around_two_rows_is_not_read_as_one_row(self) -> None:
        html = (
            "<div><a href='/qiye/1.htm'>甲公司</a><div>地址： 甲地址</div>"
            "<a href='/qiye/2.htm'>乙公司</a><div>地址： 乙地址</div></div>"
        )
        self.assertIsNone(fields_for_anchor(
            _first_anchor(html), detail_pattern=_qiye(), preset="shunqiwang"))

    def test_a_neighbours_address_never_reaches_this_record(self) -> None:
        html = (
            "<ul><li><a href='/qiye/1.htm'>甲公司</a><div>地址： 甲地址</div></li>"
            "<li><a href='/qiye/2.htm'>乙公司</a><div>地址： 乙地址</div></li></ul>"
        )
        fields = fields_for_anchor(_nth_anchor(html, 1), detail_pattern=_qiye(), preset="shunqiwang")
        assert fields is not None
        self.assertEqual(fields.get("address"), "乙地址")
        self.assertNotIn("甲地址", fields.get("address") or "")

    def test_the_page_root_is_never_a_row(self) -> None:
        # Without that bound, a page whose list is not marked up as a list
        # resolves every anchor to body and every field is the page's.
        html = "<html><body><a href='/qiye/1.htm'>甲公司</a><div>地址： 甲地址</div></body></html>"
        self.assertIsNone(fields_for_anchor(
            _first_anchor(html), detail_pattern=_qiye(), preset="shunqiwang"))

    def test_a_link_with_nothing_labelled_around_it_yields_no_values(self) -> None:
        # A lone <div> around one business link is accepted as a row, and
        # reading it finds no labels — so it contributes a name and nothing
        # else, exactly as before this module existed. The multi-link guard is
        # what keeps such a container from being a wrapper around several
        # businesses; a single unnamed block cannot put a wrong value anywhere.
        html = "<html><body><div><a href='/qiye/1.htm'>甲公司</a></div></body></html>"
        fields = fields_for_anchor(_first_anchor(html), detail_pattern=_qiye(), preset="shunqiwang")
        assert fields is not None
        self.assertEqual(fields.values, {})


class NativeBlockTest(unittest.TestCase):
    def test_the_whole_row_travels_with_the_record(self) -> None:
        fields = read_listing_row(one_row(f"<li>{SHUNQI_LI}</li>"), labels=preset_labels("shunqiwang"))
        block = as_native(fields)
        assert block is not None
        self.assertEqual(block["values"]["address"], "杭州市西湖区文一路 1 号")
        self.assertIn("主营产品", block["labels"])
        self.assertEqual(block["values"]["est_date"], "2021-05-17")

    def test_a_row_that_could_not_be_isolated_has_no_block(self) -> None:
        self.assertIsNone(as_native(RowFields(row_isolated=False)))
        self.assertIsNone(as_native(None))


class MergeTest(unittest.TestCase):
    def test_a_detail_page_absence_does_not_erase_the_listing(self) -> None:
        # The exact failure that lost 97 addresses: build_detail_record returns
        # None for a selector that did not match, and that used to reach the
        # record as "no address known".
        values, decisions = merge_listing_and_detail(
            {"address": None, "description": None},
            RowFields(values={"address": "列表页写的地址"}),
        )
        self.assertEqual(values["address"], "列表页写的地址")
        self.assertEqual(decisions["address"].source, "listing")

    def test_a_detail_value_wins_but_the_listings_survives_when_they_differ(self) -> None:
        values, decisions = merge_listing_and_detail(
            {"address": "详情页写的地址"},
            RowFields(values={"address": "列表页写的地址"}),
        )
        self.assertEqual(values["address"], "详情页写的地址")
        self.assertEqual(decisions["address"].source, "detail")
        self.assertEqual(decisions["address"].conflict,
                         {"listing": "列表页写的地址", "detail": "详情页写的地址"})

    def test_agreeing_sides_are_not_reported_as_a_conflict(self) -> None:
        values, decisions = merge_listing_and_detail(
            {"address": "同一地址。"}, RowFields(values={"address": "同一地址"})
        )
        self.assertEqual(decisions["address"].source, "both")
        self.assertIsNone(decisions["address"].conflict)

    def test_a_field_neither_side_stated_is_none_not_guessed(self) -> None:
        _values, decisions = merge_listing_and_detail({}, RowFields())
        self.assertEqual(decisions["phone"].source, "none")
        self.assertIsNone(decisions["phone"].value)

    def test_a_listing_row_given_as_a_plain_mapping_is_accepted(self) -> None:
        values, _decisions = merge_listing_and_detail({}, {"description": "宠物"})
        self.assertEqual(values["description"], "宠物")


# --- helpers ----------------------------------------------------------------


def _qiye():
    import re

    return re.compile(r"/qiye/\d+\.htm")


def _anchors(html: str):
    return [e for e in parse_html(html).elements() if e.tag == "a"]


def _first_anchor(html: str):
    return _anchors(html)[0]


def _nth_anchor(html: str, index: int):
    return _anchors(html)[index]


if __name__ == "__main__":
    unittest.main()
