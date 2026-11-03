"""HTML / JSON-LD extraction."""

from __future__ import annotations

import unittest

from collector.htmlextract import (
    LINK_ABOUT,
    LINK_CONTACT,
    LINK_IMPRINT,
    LINK_LOCATIONS,
    address_from_jsonld,
    classify_link,
    extract_page,
    find_link_candidates,
    find_outbound_links,
    iter_jsonld_nodes,
)

BASE = "https://www.praxis-mueller.de/"

FULL_PAGE = """
<!doctype html><html lang="de"><head>
<title>Praxis Dr. M\u00fcller</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Dentist","name":"Praxis Dr. M\u00fcller",
 "email":"mailto:info@praxis-mueller.de","telephone":"+49 30 12345678",
 "url":"https://www.praxis-mueller.de",
 "address":{"@type":"PostalAddress","streetAddress":"Musterstra\u00dfe 1",
            "postalCode":"10115","addressLocality":"Berlin","addressCountry":"DE"},
 "location":[{"@type":"Place","name":"Filiale Prenzlauer Berg",
              "telephone":"+49 30 87654321",
              "address":{"@type":"PostalAddress","streetAddress":"Kastanienallee 5",
                         "postalCode":"10435","addressLocality":"Berlin"}}]}
</script>
</head><body>
<a href="mailto:kontakt@praxis-mueller.de?subject=Anfrage">Kontakt aufnehmen</a>
<a href="tel:+493012345678">030 12345678</a>
<a href="/kontakt">Kontakt</a>
<a href="/ueber-uns">\u00dcber uns</a>
<a href="https://www.praxis-mueller.de/standorte">Unsere Standorte</a>
<a href="/impressum">Impressum</a>
<a href="https://www.fremdportal.de/datenschutz">Externer Datenschutz</a>
</body></html>
"""


class JsonLdTest(unittest.TestCase):
    def test_organisation_values_are_extracted_with_their_page(self):
        page = extract_page(BASE, FULL_PAGE)
        self.assertEqual(page.jsonld_blocks, 1)
        self.assertEqual(page.title, "Praxis Dr. M\u00fcller")

        emails = [value.raw_value for value in page.of_field("emails")]
        self.assertIn("info@praxis-mueller.de", emails)  # the mailto: prefix is stripped

        phones = [value.raw_value for value in page.of_field("phone")]
        self.assertIn("+49 30 12345678", phones)

        addresses = [value.raw_value for value in page.of_field("address")]
        self.assertIn("Musterstra\u00dfe 1, 10115 Berlin, DE", addresses)

    def test_every_value_carries_its_source_and_evidence(self):
        page = extract_page(BASE, FULL_PAGE)
        for value in page.values:
            with self.subTest(field=value.field):
                self.assertIn(value.source, ("jsonld", "mailto", "tel"))
                self.assertTrue(value.evidence)

    def test_multiple_locations_are_all_kept(self):
        page = extract_page(BASE, FULL_PAGE)
        addresses = [value.raw_value for value in page.of_field("address")]
        self.assertEqual(len(addresses), 2)
        self.assertIn("Kastanienallee 5, 10435 Berlin", addresses)
        roles = [value.context.get("role") for value in page.of_field("address")]
        self.assertIn("location", roles)

    def test_a_broken_jsonld_block_is_counted_and_does_not_stop_the_rest(self):
        html = (
            '<script type="application/ld+json">{ not json }</script>'
            '<a href="mailto:a@b.de">mail</a>'
        )
        page = extract_page(BASE, html)
        self.assertEqual(page.jsonld_errors, 1)
        self.assertEqual([value.raw_value for value in page.of_field("emails")], ["a@b.de"])

    def test_graph_shaped_documents_are_walked(self):
        html = (
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@graph":['
            '{"@type":"WebSite","name":"ignored"},'
            '{"@type":"LocalBusiness","name":"Gefunden","email":"a@b.de"}]}'
            "</script>"
        )
        page = extract_page(BASE, html)
        names = [value.raw_value for value in page.of_field("title")]
        self.assertEqual(names, ["Gefunden"])

    def test_non_organisation_nodes_are_ignored(self):
        html = (
            '<script type="application/ld+json">'
            '{"@type":"BreadcrumbList","itemListElement":[{"name":"Start"}]}'
            "</script>"
        )
        page = extract_page(BASE, html)
        self.assertEqual(page.values, [])

    def test_iter_nodes_handles_lists_and_graphs(self):
        payload = [{"@type": "A"}, {"@graph": [{"@type": "B"}, {"@type": "C"}]}]
        types = sorted(
            node.get("@type") for node in iter_jsonld_nodes(payload) if "@type" in node
        )
        self.assertEqual(types, ["A", "B", "C"])

    def test_address_rendering_keeps_components_the_source_gave(self):
        self.assertEqual(
            address_from_jsonld(
                {"streetAddress": "Weg 1", "postalCode": "10115", "addressLocality": "Berlin"}
            ),
            "Weg 1, 10115 Berlin",
        )
        self.assertEqual(address_from_jsonld("Free text 5, Ort"), "Free text 5, Ort")
        self.assertIsNone(address_from_jsonld({}))


class MailtoTelTest(unittest.TestCase):
    def test_mailto_and_tel_links_are_extracted(self):
        page = extract_page(BASE, FULL_PAGE)
        mailto = [value for value in page.of_field("emails") if value.source == "mailto"]
        self.assertEqual([value.raw_value for value in mailto], ["kontakt@praxis-mueller.de"])
        tel = [value for value in page.of_field("phone") if value.source == "tel"]
        self.assertEqual([value.raw_value for value in tel], ["+493012345678"])

    def test_a_mailto_with_several_recipients_is_split(self):
        page = extract_page(BASE, '<a href="mailto:a@x.de,b@y.de">beide</a>')
        self.assertEqual(
            sorted(value.raw_value for value in page.of_field("emails")), ["a@x.de", "b@y.de"]
        )

    def test_the_query_string_of_a_mailto_is_dropped(self):
        page = extract_page(BASE, '<a href="mailto:a@x.de?subject=Hi">x</a>')
        self.assertEqual([value.raw_value for value in page.of_field("emails")], ["a@x.de"])

    def test_a_tel_url_is_url_decoded(self):
        page = extract_page(BASE, '<a href="tel:+49%2030%2012345678">x</a>')
        self.assertEqual([value.raw_value for value in page.of_field("phone")], ["+49 30 12345678"])

    def test_plain_text_addresses_are_not_harvested(self):
        # Stage B measured that free-text harvesting returns the data-protection
        # authority's address. Only attributable sources are used.
        page = extract_page(BASE, "<p>Schreiben Sie an info@irgendwo.de</p>")
        self.assertEqual(page.of_field("emails"), [])

    def test_the_link_target_and_the_visible_text_are_kept_apart(self):
        # Field report: one anchor linked tel:+4940237751001 and printed
        # "+49 40 182 11 6600". Both statements are the element's, so both are
        # kept; deciding they disagree needs number normalisation, which is a
        # judgement and lives downstream.
        page = extract_page(BASE, '<a href="tel:+4940237751001">+49 40 182 11 6600</a>')
        phone = page.of_field("phone")[0]
        self.assertEqual(phone.raw_value, "+4940237751001")
        self.assertEqual(phone.context["href_value"], "+4940237751001")
        self.assertEqual(phone.context["display_text"], "+49 40 182 11 6600")
        self.assertEqual(phone.evidence, "+49 40 182 11 6600")

    def test_a_label_in_the_visible_text_is_recorded_as_such(self):
        page = extract_page(BASE, '<a href="tel:+493012345678">Anrufen</a>')
        phone = page.of_field("phone")[0]
        self.assertEqual(phone.raw_value, "+493012345678")
        self.assertEqual(phone.context["display_text"], "Anrufen")

    def test_a_mailto_records_the_shown_address_separately(self):
        page = extract_page(BASE, '<a href="mailto:a@x.de">b@y.de</a>')
        email = page.of_field("emails")[0]
        self.assertEqual(email.raw_value, "a@x.de")
        self.assertEqual(email.context["href_value"], "a@x.de")
        self.assertEqual(email.context["display_text"], "b@y.de")


class LinkTest(unittest.TestCase):
    def test_page_kinds_are_classified(self):
        self.assertEqual(classify_link("/kontakt", "Kontakt"), LINK_CONTACT)
        self.assertEqual(classify_link("/ueber-uns", "\u00dcber uns"), LINK_ABOUT)
        self.assertEqual(classify_link("/standorte", "Standorte"), LINK_LOCATIONS)
        self.assertEqual(classify_link("/impressum", "Impressum"), LINK_IMPRINT)
        self.assertIsNone(classify_link("/produkte", "Produkte"))

    def test_anchor_text_is_used_when_the_path_is_uninformative(self):
        self.assertEqual(classify_link("/p/12", "Kontakt zu uns"), LINK_CONTACT)

    def test_candidates_are_same_site_only_and_ordered(self):
        page = extract_page(BASE, FULL_PAGE)
        urls = [link.url for link in page.links]
        self.assertEqual(
            urls,
            [
                "https://www.praxis-mueller.de/kontakt",
                "https://www.praxis-mueller.de/standorte",
                "https://www.praxis-mueller.de/ueber-uns",
                "https://www.praxis-mueller.de/impressum",
            ],
        )
        for url in urls:
            self.assertNotIn("fremdportal", url)

    def test_duplicate_links_collapse_and_query_strings_are_dropped(self):
        anchors = [
            ("/kontakt", "Kontakt"),
            ("/kontakt?lang=de", "Kontakt 2"),
            ("https://www.praxis-mueller.de/kontakt#form", "Kontakt 3"),
        ]
        candidates = find_link_candidates(BASE, anchors)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].url, "https://www.praxis-mueller.de/kontakt")

    def test_special_hrefs_are_ignored(self):
        anchors = [
            ("#top", "oben"),
            ("javascript:void(0)", "kontakt"),
            ("mailto:a@b.de", "contact"),
            ("tel:+49", "contact"),
        ]
        self.assertEqual(find_link_candidates(BASE, anchors), [])

    def test_a_maximum_can_be_applied(self):
        anchors = [(f"/kontakt-{index}", "Kontakt") for index in range(20)]
        self.assertEqual(len(find_link_candidates(BASE, anchors, max_links=5)), 5)


class OutboundLinkTest(unittest.TestCase):
    """Off-site links: where a business's own site is advertised.

    ``links`` answers "which pages of this site should be read next"; these
    answer "which other sites does this page point at". Candidate-site discovery
    needs the second, and the two must not be conflated -- a directory page's
    same-site navigation is not evidence of anything.
    """

    def test_only_off_site_links_are_returned(self):
        page = extract_page(BASE, FULL_PAGE)
        self.assertEqual(
            [link.url for link in page.outbound], ["https://www.fremdportal.de/datenschutz"]
        )
        # The same-site navigation that ``links`` carries is not repeated here.
        self.assertEqual(
            [link.url for link in page.links],
            [
                "https://www.praxis-mueller.de/kontakt",
                "https://www.praxis-mueller.de/standorte",
                "https://www.praxis-mueller.de/ueber-uns",
                "https://www.praxis-mueller.de/impressum",
            ],
        )

    def test_the_page_s_own_site_is_excluded_including_other_subdomains(self):
        anchors = [
            ("https://praxis-mueller.de/x", "same site, no www"),
            ("https://shop.praxis-mueller.de/x", "same site, other subdomain"),
            ("https://notpraxis-mueller.de/x", "a different registrable domain"),
        ]
        urls = [link.url for link in find_outbound_links(BASE, anchors)]
        self.assertEqual(urls, ["https://notpraxis-mueller.de/x"])

    def test_link_kind_is_not_filtered_so_ownership_can_decide(self):
        # "/produkte" is not a page kind stage D would follow, but it is still a
        # link to somewhere else, and that is all this finder claims.
        anchors = [("https://elsewhere.example/produkte", "Produkte")]
        self.assertEqual(
            [link.url for link in find_outbound_links(BASE, anchors)],
            ["https://elsewhere.example/produkte"],
        )

    def test_contact_and_script_targets_are_not_sites(self):
        anchors = [
            ("mailto:a@b.de", "mail"),
            ("tel:+4930", "call"),
            ("javascript:void(0)", "js"),
            ("#top", "top"),
            ("data:text/html,x", "data"),
            ("/kontakt", "same site"),
        ]
        self.assertEqual(find_outbound_links(BASE, anchors), [])

    def test_duplicates_collapse_but_the_query_string_is_kept(self):
        # Unlike a same-site page, an off-site target is kept whole: its query
        # string can be the only thing that differs.
        anchors = [
            ("https://elsewhere.example/a", "one"),
            ("https://elsewhere.example/a", "one again"),
            ("https://elsewhere.example/a?ref=1", "tracked"),
        ]
        urls = [link.url for link in find_outbound_links(BASE, anchors)]
        self.assertEqual(
            urls, ["https://elsewhere.example/a", "https://elsewhere.example/a?ref=1"]
        )

    def test_a_maximum_can_be_applied(self):
        anchors = [(f"https://elsewhere.example/{index}", "x") for index in range(20)]
        self.assertEqual(len(find_outbound_links(BASE, anchors, max_links=5)), 5)

    def test_a_page_with_no_off_site_links_yields_none(self):
        page = extract_page(BASE, "<html><body><a href='/kontakt'>Kontakt</a></body></html>")
        self.assertEqual(page.outbound, [])


class RobustnessTest(unittest.TestCase):
    def test_malformed_markup_does_not_raise(self):
        page = extract_page(BASE, "<html><body><a href='/kontakt'>Kontakt<div></body>")
        self.assertEqual(page.parse_error, "")
        self.assertTrue(page.links)

    def test_an_empty_page_is_handled(self):
        page = extract_page(BASE, "")
        self.assertEqual(page.values, [])
        self.assertEqual(page.title, "")


if __name__ == "__main__":
    unittest.main()
