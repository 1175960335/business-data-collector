"""The HTML tree and the CSS subset the directory provider is configured with.

Stage D found a real defect caused by a *streaming* parser that threw the tree
away: an unclosed ``<a>`` lost its links. The directory provider needs the
opposite question answered — "the element matching ``.adr`` inside this detail
page" — so ``selector.py`` builds a tree and matches a small CSS subset against
it.

The important part of these tests is not that ``.adr`` matches. It is that a
selector the engine cannot evaluate is **refused at compile time**. A selector
that silently matches nothing turns a typo in the config into "the directory
returned no businesses", which is indistinguishable from the truth, and that is
exactly the "configured but does nothing" failure the task book warns about.
"""

from __future__ import annotations

import unittest

from collector.selector import (
    Element,
    SelectorError,
    compile_selector,
    parse_html,
    select,
)


class TreeTest(unittest.TestCase):
    """Parsing markup into a tree that survives broken pages."""

    def test_text_is_whitespace_collapsed(self):
        root = parse_html("<p>  Musterstra\u00dfe\n   1  </p>")
        (paragraph,) = root.elements()
        self.assertEqual(paragraph.text(), "Musterstra\u00dfe 1")

    def test_nested_text_is_joined_with_a_single_space(self):
        root = parse_html("<div>a <span>b</span> <span>c</span></div>")
        (division,) = select(root, "div")
        self.assertEqual(division.text(), "a b c")

    def test_attributes_classes_and_id(self):
        root = parse_html('<div id="main" class="adr address" data-x="1">t</div>')
        (division,) = root.elements()
        self.assertEqual(division.id(), "main")
        self.assertEqual(division.classes(), {"adr", "address"})
        self.assertEqual(division.attr("data-x"), "1")
        self.assertIsNone(division.attr("data-y"))

    def test_ancestors_are_walked_upwards(self):
        root = parse_html("<div><section><p>t</p></section></div>")
        paragraph = list(root.elements())[-1]
        self.assertEqual([node.tag for node in paragraph.ancestors()], ["section", "div", "#root"])

    def test_void_elements_do_not_nest(self):
        root = parse_html("<div><img src='a.png'><p>after</p></div>")
        division = list(root.elements())[0]
        self.assertEqual([child.tag for child in division.children if isinstance(child, Element)], ["img", "p"])

    def test_implied_end_tags_close_the_previous_sibling(self):
        # Without this, every <p> nests inside the first one and a descendant
        # selector fires on the whole document.
        root = parse_html("<div><p>one<p>two<p>three</div>")
        division = list(root.elements())[0]
        paragraphs = [child for child in division.children if isinstance(child, Element)]
        self.assertEqual([child.text() for child in paragraphs], ["one", "two", "three"])

    def test_a_nested_list_keeps_its_structure(self):
        # Regression: the auto-close rule used to scan the whole stack, so the
        # inner <li> closed the *outer* one through the nested <ul>. The inner
        # list came out empty and "inner" became a sibling of "outer".
        root = parse_html("<ul><li>outer<ul><li>inner</li></ul></li></ul>")
        outer = select(root, "ul > li")[0]
        self.assertEqual(outer.text(), "outer inner")
        nested = select(root, "li > ul > li")
        self.assertEqual([element.text() for element in nested], ["inner"])

    def test_a_nested_list_is_reachable_by_child_combinator(self):
        html = "<ul class='listings'><li><ul><li><a href='/a'>a</a></li></ul></li></ul>"
        links = select(parse_html(html), "ul.listings > li > ul > li > a")
        self.assertEqual([element.text() for element in links], ["a"])

    def test_repeated_cells_and_rows_unnest(self):
        root = parse_html("<table><tr><td>a<td>b<tr><td>c</table>")
        self.assertEqual(len(select(root, "table > tr")), 2)
        self.assertEqual(len(select(root, "tr > td")), 3)

    def test_repeated_definition_terms_unnest(self):
        root = parse_html("<dl><dt>one<dd>a<dt>two<dd>b</dl>")
        self.assertEqual([element.text() for element in select(root, "dt")], ["one", "two"])
        self.assertEqual([element.text() for element in select(root, "dd")], ["a", "b"])

    def test_a_stray_end_tag_is_ignored_not_fatal(self):
        root = parse_html("</div><p>t</p></span>")
        self.assertEqual([element.tag for element in root.elements()], ["p"])

    def test_unclosed_anchors_all_surface_in_document_order(self):
        # The stage D streaming parser lost the second link here. A tree that
        # keeps both is what the directory provider relies on.
        root = parse_html("<body><a href='/one'>1<a href='/two'>2")
        anchors = [element for element in root.elements() if element.tag == "a"]
        self.assertEqual([element.attr("href") for element in anchors], ["/one", "/two"])

    def test_html_round_trips_the_element(self):
        root = parse_html('<a href="/x" class="y">link</a>')
        (anchor,) = root.elements()
        self.assertEqual(anchor.html(), '<a href="/x" class="y">link</a>')

    def test_character_references_are_decoded(self):
        root = parse_html("<p>caf&eacute; &amp; bar</p>")
        (paragraph,) = root.elements()
        self.assertEqual(paragraph.text(), "caf\u00e9 & bar")

    def test_empty_markup_is_an_empty_tree_not_an_error(self):
        self.assertEqual(list(parse_html("").elements()), [])


class SupportedSelectorTest(unittest.TestCase):
    """The subset a directory configuration realistically uses."""

    def selection(self, html: str, css: str) -> list[str]:
        return [element.text() for element in select(parse_html(html), css)]

    def test_tag(self):
        self.assertEqual(self.selection("<h1>Title</h1><p>Body</p>", "h1"), ["Title"])

    def test_universal_matches_every_element(self):
        root = parse_html("<div><p>a</p></div>")
        self.assertEqual(len(compile_selector("*").select(root)), 2)

    def test_id(self):
        self.assertEqual(self.selection('<p id="a">x</p><p id="b">y</p>', "#a"), ["x"])

    def test_class_requires_the_class_to_be_present(self):
        self.assertEqual(self.selection('<p class="adr">x</p><p class="name">y</p>', ".adr"), ["x"])

    def test_compound_class_needs_both(self):
        self.assertEqual(
            self.selection('<p class="adr address">x</p><p class="adr">y</p>', ".adr.address"),
            ["x"],
        )

    def test_tag_with_id_and_class(self):
        html = '<div><p class="x" id="only">t</p><span class="x">s</span></div>'
        self.assertEqual(self.selection(html, "p.x#only"), ["t"])

    def test_attribute_presence(self):
        self.assertEqual(
            self.selection("<a href='/x'>x</a><a>y</a>", "a[href]"), ["x"]
        )

    def test_attribute_equals(self):
        self.assertEqual(
            self.selection("<a rel='next'>n</a><a rel='prev'>p</a>", "a[rel='next']"), ["n"]
        )

    def test_attribute_equals_unquoted_value(self):
        self.assertEqual(
            self.selection("<div data-k='adr'>x</div>", "[data-k=adr]"), ["x"]
        )

    def test_attribute_prefix_is_what_maketel_and_mailto_links_reachable(self):
        html = (
            "<a href='mailto:info@example.de'>m</a>"
            "<a href='tel:+4930123456'>t</a>"
            "<a href='/website=x'>w</a>"
        )
        self.assertEqual(self.selection(html, "a[href^='mailto:']"), ["m"])
        self.assertEqual(self.selection(html, "a[href^='tel:']"), ["t"])
        self.assertEqual(self.selection(html, "a[href*='website=']"), ["w"])

    def test_attribute_substring_and_suffix(self):
        html = "<a href='https://x.example.de/a'>x</a><a href='https://y.example'>y</a>"
        self.assertEqual(self.selection(html, "a[href*='example.de']"), ["x"])
        self.assertEqual(self.selection(html, "a[href$='.example']"), ["y"])

    def test_attribute_word_match(self):
        self.assertEqual(
            self.selection("<div class='adr other'>x</div>", "[class~='adr']"), ["x"]
        )

    def test_descendant_combinator(self):
        html = "<div class='wrap'><div class='inner'><p>t</p></div></div><p>outer</p>"
        self.assertEqual(self.selection(html, "div.wrap p"), ["t"])

    def test_child_combinator_needs_a_direct_parent(self):
        html = "<div class='wrap'><div class='inner'><p>t</p></div></div>"
        self.assertEqual(self.selection(html, "div.wrap > p"), [])
        self.assertEqual(self.selection(html, "div.inner > p"), ["t"])

    def test_three_steps_of_child_combinator(self):
        html = "<ul><li><ul><li>deep</li></ul></li></ul>"
        self.assertEqual(self.selection(html, "ul > li > ul > li"), ["deep"])

    def test_matches_are_in_document_order(self):
        html = "<div><p>1</p><p>2</p><p>3</p></div>"
        self.assertEqual(self.selection(html, "p"), ["1", "2", "3"])

    def test_select_is_a_convenience_for_compile_then_run(self):
        root = parse_html("<p>t</p>")
        self.assertEqual(select(root, "p"), compile_selector("p").select(root))


class NthOfTypeTest(unittest.TestCase):
    """Stage 019: positional selection, because real directories need it.

    Chinese business directories publish the contact block as
    ``<dl><dt>公司地址：</dt><dd>…</dd><dt>固定电话：</dt><dd>…</dd></dl>`` — every
    value is a bare ``<dd>`` with no distinguishing attribute, so without a
    positional selector those pages cannot be read at all.
    """

    #: The shape b2b168.com uses, copied from a real page.
    CONTACT = (
        "<dl class='codl'>"
        "<dt>公司地址：</dt><dd>杭州市西湖区文三路 100 号</dd>"
        "<dt>固定电话：</dt><dd>057188889999</dd>"
        "<dt>联系人：</dt><dd>张三先生（）</dd>"
        "<dt>移动电话：</dt><dd>13800000000</dd>"
        "<dt>邮政编码：</dt><dd>310012</dd>"
        "<dt>传真号码：</dt><dd></dd>"
        "</dl>"
    )

    def texts(self, html: str, css: str) -> list[str]:
        return [element.text() for element in select(parse_html(html), css)]

    def test_the_first_and_third_values_are_addressable(self):
        self.assertEqual(self.texts(self.CONTACT, "dl.codl dd:nth-of-type(1)"),
                         ["杭州市西湖区文三路 100 号"])
        self.assertEqual(self.texts(self.CONTACT, "dl.codl dd:nth-of-type(2)"),
                         ["057188889999"])
        self.assertEqual(self.texts(self.CONTACT, "dl.codl dd:nth-of-type(4)"),
                         ["13800000000"])

    def test_it_counts_the_type_not_all_children(self):
        # ``dd:nth-of-type(2)`` is the second <dd> even though a <dt> sits
        # between the first and the second. Counting all children would return
        # the third element and silently read the wrong field.
        self.assertEqual(self.texts(self.CONTACT, "dd:nth-of-type(2)"), ["057188889999"])

    def test_an_index_past_the_end_matches_nothing_rather_than_wrapping(self):
        self.assertEqual(self.texts(self.CONTACT, "dl.codl dd:nth-of-type(9)"), [])

    def test_spaces_inside_the_parentheses_are_tolerated(self):
        self.assertEqual(self.texts(self.CONTACT, "dl.codl dd:nth-of-type( 1 )"),
                         ["杭州市西湖区文三路 100 号"])

    def test_it_composes_with_a_descendant_step(self):
        html = "<div class='a'><dl class='codl'><dd>outer</dd></dl></div>"
        self.assertEqual(self.texts(html, "div.a dl.codl dd:nth-of-type(1)"), ["outer"])

    def test_it_composes_with_a_child_step(self):
        self.assertEqual(self.texts(self.CONTACT, "dl.codl > dd:nth-of-type(1)"),
                         ["杭州市西湖区文三路 100 号"])

    def test_an_empty_dd_is_still_a_position(self):
        # The fax row is empty but still occupies position 6; if empty elements
        # were skipped the postcode would move and the fields would shift.
        self.assertEqual(self.texts(self.CONTACT, "dl.codl dd:nth-of-type(5)"), ["310012"])
        self.assertEqual(self.texts(self.CONTACT, "dl.codl dd:nth-of-type(6)"), [""])

    def test_zero_is_refused(self):
        with self.assertRaises(SelectorError) as caught:
            compile_selector("dd:nth-of-type(0)")
        self.assertIn("counts from 1", str(caught.exception))

    def test_a_repeated_nth_of_type_is_refused(self):
        with self.assertRaises(SelectorError):
            compile_selector("dd:nth-of-type(1):nth-of-type(2)")

    def test_other_pseudo_classes_are_still_refused(self):
        for css in ("dd:first-child", "dd:last-of-type", "dd:nth-child(1)"):
            with self.subTest(css=css), self.assertRaises(SelectorError):
                compile_selector(css)


class UnsupportedSelectorTest(unittest.TestCase):
    """Anything outside the subset is refused, loudly.

    Each of these used to be a plausible config value that would have matched
    nothing and reported "no businesses found".
    """

    def assert_refused(self, css: str):
        with self.assertRaises(SelectorError) as caught:
            compile_selector(css)
        return str(caught.exception)

    def test_empty_selector(self):
        self.assertIn("empty", self.assert_refused("   "))

    def test_selector_that_names_nothing(self):
        self.assertIn("names nothing", self.assert_refused(">"))

    def test_pseudo_class(self):
        self.assertIn("':'", self.assert_refused("a:hover"))

    def test_sibling_combinators(self):
        self.assertIn("'+'", self.assert_refused("a + p"))
        self.assertIn("'~'", self.assert_refused("a ~ p"))

    def test_function_pseudo_class(self):
        # ':' is reported because it is reached first; refusing for any of the
        # unsupported characters is what matters, not which one is named.
        self.assertIn("':'", self.assert_refused("p:not(.x)"))

    def test_a_colon_inside_an_attribute_value_is_allowed(self):
        # The reason the syntax check strips brackets first: ':' is only a
        # problem as *structure*, never as data.
        selector = compile_selector("a[href^='mailto:']")
        self.assertEqual(len(selector.steps), 1)

    def test_a_plus_inside_an_attribute_value_is_allowed(self):
        selector = compile_selector("a[href^='tel:+49']")
        self.assertEqual(len(selector.steps), 1)

    def test_bad_id_is_reported(self):
        with self.assertRaises(SelectorError):
            compile_selector("p#")

    def test_bad_attribute_is_reported(self):
        with self.assertRaises(SelectorError):
            compile_selector("a[href")

    def test_star_must_stand_alone(self):
        with self.assertRaises(SelectorError):
            compile_selector("p*x")


class SelectorListTest(unittest.TestCase):
    """A comma-separated selector is a union, not a syntax error.

    The directory adapter's *own default* ``website`` selector is
    ``a[href*='website='], a[href*='url=']``. Before this was a real union the
    whole selector was refused, the field fell back to a page heuristic, and
    every b2b168.com row came back carrying the site's desktop-client installer
    as its "website" (stage 019 measured 17/17).
    """

    def test_a_list_matches_the_union_in_document_order(self):
        root = parse_html("<p>a</p><span>b</span><em>c</em>")
        matched = select(root, "p, em")
        self.assertEqual([element.tag for element in matched], ["p", "em"])

    def test_an_element_matching_both_sides_appears_once(self):
        root = parse_html('<a class="x" href="#1">a</a><a href="#2">b</a>')
        matched = select(root, "a, .x")
        self.assertEqual(len(matched), 2)

    def test_a_single_selector_is_still_a_plain_selector(self):
        selector = compile_selector("a")
        self.assertEqual(len(selector.steps), 1)

    def test_a_comma_inside_an_attribute_value_is_not_a_separator(self):
        root = parse_html('<a href="mailto:a@b.c,x@y.z">m</a>')
        matched = select(root, "a[href*='a@b.c,x@y.z']")
        self.assertEqual(len(matched), 1)

    def test_blank_parts_are_ignored(self):
        root = parse_html("<p>a</p>")
        self.assertEqual(len(select(root, " p , ")), 1)

    def test_an_empty_selector_list_is_still_empty(self):
        with self.assertRaises(SelectorError):
            compile_selector(" , ")

    def test_one_bad_part_refuses_the_whole_list(self):
        # A list must not become "the good half silently works": a typo in any
        # member is a configuration error the operator has to see.
        with self.assertRaises(SelectorError):
            compile_selector("p, a[href")

    def test_the_directories_default_website_selector_compiles(self):
        selector = compile_selector("a[href*='website='], a[href*='url=']")
        self.assertEqual(len(selector.selectors), 2)
