"""Candidate official-site discovery (task book 026 P1).

The acceptance questions this module answers:

* "名录没给官网时，怎么找到它自己的站" -- the candidate finders, each declaring
  where its candidates come from;
* "凭什么认定这个站是这家企业的" -- the ownership rule, which needs the page's own
  name *and* an independent signal, and never accepts a directory or a namesake;
* "未经确认怎么办" -- a name-only match is stored as ``pending`` and writes no
  fact, so a lead never becomes a claim;
* "找不到时说的是找不到还是没有搜" -- an unavailable finder is reported as
  unavailable, and ``search`` is declared rather than faked.

Everything runs against a loopback stub site and local files: no external host
is contacted, so a rate-limited directory cannot make these tests flaky.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from collector import db, ingest
from collector.identity import normalize_text
from collector.orchestrator import Orchestrator, RunResult
from collector.sitediscovery import (
    PROVIDER as SITE_DISCOVERY_PROVIDER,
    REASON_DIRECTORY_HOST,
    REASON_NAME_MISMATCH,
    REASON_NO_NAME_ON_PAGE,
    REASON_ONLY_NAME,
    REUSED_NOTE,
    STATUS_CONFIRMED,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_REJECTED,
    _days_ago_iso,
    _plus_seconds_iso,
    _sha16,
    is_directory_host,
    judge_ownership,
    WikidataFinder,
    WikidataHit,
    WikidataUnavailable,
)

from . import support
from .stub_site import Page, SiteServer

LISTING_TITLE = "Praxis Dr. M\u00fcller"
LISTING_ADDRESS = "Musterstra\u00dfe 1, 10115 Berlin, DE"
LISTING_PHONE = "+49 30 12345678"

#: The three values a candidate page must state to be confirmed, in the shape
#: ``extract_page`` reads them (JSON-LD ``name``/``telephone``/``address``).
AGREEING_JSONLD = json.dumps(
    {
        "@type": "Dentist",
        "name": LISTING_TITLE,
        "telephone": LISTING_PHONE,
        "address": {
            "streetAddress": "Musterstra\u00dfe 1",
            "postalCode": "10115",
            "addressLocality": "Berlin",
        },
    }
)


def ownership(page_url: str, *, page_name=LISTING_TITLE, page_phone="", page_address="", **kwargs):
    return judge_ownership(
        entity_name=kwargs.pop("entity_name", LISTING_TITLE),
        listing_phone=kwargs.pop("listing_phone", LISTING_PHONE),
        listing_address=kwargs.pop("listing_address", LISTING_ADDRESS),
        page_url=page_url,
        page_values={"title": page_name, "phone": page_phone, "address": page_address},
        country=kwargs.pop("country", "DE"),
        rules=kwargs.pop("rules", {}),
    )


class OwnershipTest(unittest.TestCase):
    """The decision, without a database or a socket in sight."""

    def test_a_directory_host_is_never_an_official_site(self):
        # The name and the phone agree exactly -- and it is still refused,
        # because a listing site is where the business is *advertised*.
        verdict = ownership(
            "https://www.11467.com/qiye/209193961.htm",
            page_phone=LISTING_PHONE,
            page_address=LISTING_ADDRESS,
        )
        self.assertEqual(verdict.status, STATUS_REJECTED)
        self.assertEqual(verdict.reason, REASON_DIRECTORY_HOST)
        self.assertFalse(verdict.confirmed)

    def test_a_subdomain_of_a_directory_is_refused_too(self):
        verdict = ownership(
            "http://hangzhou.b2b168.com/company/1.html",
            page_phone=LISTING_PHONE,
            page_address=LISTING_ADDRESS,
        )
        self.assertEqual(verdict.reason, REASON_DIRECTORY_HOST)

    def test_the_name_alone_is_not_an_identity(self):
        verdict = ownership("https://praxis-mueller.de/")
        self.assertEqual(verdict.status, STATUS_PENDING)
        self.assertEqual(verdict.reason, REASON_ONLY_NAME)
        self.assertFalse(verdict.confirmed)

    def test_the_name_and_the_phone_confirm_the_site(self):
        verdict = ownership("https://praxis-mueller.de/", page_phone=LISTING_PHONE)
        self.assertEqual(verdict.status, STATUS_CONFIRMED)
        self.assertTrue(verdict.confirmed)
        self.assertEqual(verdict.reason, "confirmed_by_name_and_phone")
        self.assertTrue(verdict.evidence["phone_agrees"])

    def test_the_name_and_the_address_confirm_the_site(self):
        verdict = ownership("https://praxis-mueller.de/", page_address=LISTING_ADDRESS)
        self.assertEqual(verdict.status, STATUS_CONFIRMED)
        self.assertEqual(verdict.reason, "confirmed_by_name_and_address")
        self.assertTrue(verdict.evidence["address_agrees"])

    def test_a_different_phone_does_not_confirm_but_leaves_it_pending(self):
        # Name agrees, the phone is a different one: no independent signal, so
        # the outcome is the same as supplying none at all.
        verdict = ownership("https://praxis-mueller.de/", page_phone="+49 30 99999999")
        self.assertEqual(verdict.status, STATUS_PENDING)

    def test_a_namesake_elsewhere_is_refused(self):
        verdict = ownership(
            "https://mueller-dental.com/",
            page_name="Zahnarztpraxis Dr. Schmidt",
            page_phone=LISTING_PHONE,
        )
        self.assertEqual(verdict.status, STATUS_REJECTED)
        self.assertEqual(verdict.reason, REASON_NAME_MISMATCH)

    def test_a_page_with_no_readable_name_is_refused(self):
        verdict = ownership("https://praxis-mueller.de/", page_name="")
        self.assertEqual(verdict.status, STATUS_REJECTED)
        self.assertEqual(verdict.reason, REASON_NO_NAME_ON_PAGE)

    def test_the_wrong_country_phone_agreement_is_not_a_confirmation(self):
        # The listing's number parses as a German one; the page states a US
        # number that happens to share its digits. E.164 comparison must not
        # treat that as the same subscriber.
        verdict = ownership(
            "https://praxis-mueller.de/",
            page_phone="+1 30 12345678",
            listing_phone="+49 30 12345678",
        )
        self.assertEqual(verdict.status, STATUS_PENDING)

    def test_the_deny_list_matches_hosts_and_subdomains_but_not_lookalikes(self):
        self.assertTrue(is_directory_host("b2b168.com"))
        self.assertTrue(is_directory_host("hangzhou.b2b168.com"))
        self.assertTrue(is_directory_host("WWW.11467.COM"))
        self.assertFalse(is_directory_host("notb2b168.com"))
        self.assertFalse(is_directory_host("b2b168.com.evil.example"))
        self.assertFalse(is_directory_host("praxis-mueller.de"))
        # An empty host cannot be judged, so it must not be treated as a company.
        self.assertTrue(is_directory_host(""))


class NameShapedListingAddressTest(unittest.TestCase):
    """048: a listing address that is the business's own name is not a signal.

    This is the false-positive boundary the fix is for. The rule needs the name
    **and** one independent signal; when the listing's address *is* the name and
    the page repeats the name in its address field, the "independent" signal is
    the name agreeing with itself, and the site gets confirmed on nothing.
    """

    NAME = "geomer GmbH"

    def test_the_name_in_the_address_slot_does_not_confirm_a_site(self):
        # What the old code saw: two copies of the name agree as addresses, so
        # the "independent" signal was there and the site was confirmed. Shown
        # here so the boundary is demonstrated, not merely asserted.
        from collector.enrich import values_agree

        self.assertTrue(values_agree("address", self.NAME, self.NAME, country="DE", rules={}))

        verdict = ownership(
            "https://www.geomer.de/",
            entity_name=self.NAME,
            listing_phone="",
            listing_address=self.NAME,
            page_name=self.NAME,
            page_address=self.NAME,
        )
        self.assertEqual(verdict.status, STATUS_PENDING)
        self.assertEqual(verdict.reason, REASON_ONLY_NAME)
        self.assertFalse(verdict.confirmed)
        self.assertFalse(verdict.evidence["address_agrees"])

    def test_the_ignored_value_and_the_reason_are_recorded(self):
        verdict = ownership(
            "https://www.geomer.de/",
            entity_name=self.NAME,
            listing_phone="",
            listing_address="geomer GmbH",
            page_name=self.NAME,
            page_address="geomer GmbH",
        )
        ignored = verdict.evidence["listing_address_ignored"]
        self.assertEqual(ignored["value"], "geomer GmbH")
        self.assertIn("企业名称", ignored["why"])

    def test_a_real_listing_address_still_confirms(self):
        # The guard must not cost the legitimate path: this is the same shape as
        # the Imovis confirmation, with an address that really is an address.
        verdict = ownership(
            "https://www.geomer.de/",
            entity_name=self.NAME,
            listing_phone="",
            listing_address="Musterstra\u00dfe 1, 10115 Berlin",
            page_name=self.NAME,
            page_address="Musterstra\u00dfe 1, 10115 Berlin",
        )
        self.assertEqual(verdict.status, STATUS_CONFIRMED)
        self.assertTrue(verdict.evidence["address_agrees"])
        self.assertNotIn("listing_address_ignored", verdict.evidence)

    def test_a_name_shaped_listing_address_leaves_the_phone_path_open(self):
        # 048.2: a missing address must not take the phone signal with it.
        verdict = ownership(
            "https://www.geomer.de/",
            entity_name=self.NAME,
            listing_phone="+49 6221 89458-0",
            listing_address=self.NAME,
            page_name=self.NAME,
            page_address="",
            page_phone="+49 6221 89458-0",
        )
        self.assertEqual(verdict.status, STATUS_CONFIRMED)
        self.assertEqual(verdict.reason, "confirmed_by_name_and_phone")
        self.assertIn("listing_address_ignored", verdict.evidence)


class PageSideNameShapedAddressTest(unittest.TestCase):
    """050 P0: the mirror guard, on the page's own address field.

    Guarding only the listing side left this half open. A page that writes its
    own name into its address field states no address, and if the listing also
    happened to carry a name-shaped value the two names agreed with each other
    as addresses. This is the shape 049 found on real pages.
    """

    NAME = "geomer GmbH"

    def test_the_page_repeating_its_own_name_is_not_a_signal(self):
        verdict = ownership(
            "https://www.geomer.de/",
            entity_name=self.NAME,
            listing_phone="",
            listing_address="Musterstra\u00dfe 1, 10115 Berlin",
            page_name=self.NAME,
            page_address=self.NAME,
        )
        self.assertEqual(verdict.status, STATUS_PENDING)
        self.assertEqual(verdict.reason, REASON_ONLY_NAME)
        self.assertFalse(verdict.evidence["address_agrees"])

    def test_the_ignored_page_value_and_the_reason_are_recorded(self):
        verdict = ownership(
            "https://www.geomer.de/",
            entity_name=self.NAME,
            listing_phone="",
            listing_address="Musterstra\u00dfe 1, 10115 Berlin",
            page_name="geomer GmbH",
            page_address="geomer GmbH",
        )
        ignored = verdict.evidence["page_address_ignored"]
        self.assertEqual(ignored["value"], "geomer GmbH")
        self.assertIn("名称", ignored["why"])

    def test_a_real_page_address_still_confirms(self):
        verdict = ownership(
            "https://www.geomer.de/",
            entity_name=self.NAME,
            listing_phone="",
            listing_address="Musterstra\u00dfe 1, 10115 Berlin",
            page_name=self.NAME,
            page_address="Musterstra\u00dfe 1, 10115 Berlin",
        )
        self.assertEqual(verdict.status, STATUS_CONFIRMED)
        self.assertTrue(verdict.evidence["address_agrees"])
        self.assertNotIn("page_address_ignored", verdict.evidence)

    def test_a_digit_in_the_company_name_cannot_confirm_a_site(self):
        # 049's own admission: the digit gate let a company called "Hansa 12
        # GmbH" through, and the name then stood in for the second signal.
        verdict = ownership(
            "https://www.hansa12.de/",
            entity_name="Hansa 12 GmbH",
            listing_phone="",
            listing_address="Hansa 12 GmbH",
            page_name="Hansa 12 GmbH",
            page_address="Hansa 12 GmbH",
        )
        self.assertEqual(verdict.status, STATUS_PENDING)
        self.assertEqual(verdict.reason, REASON_ONLY_NAME)
        self.assertFalse(verdict.confirmed)
        self.assertIn("listing_address_ignored", verdict.evidence)
        self.assertIn("page_address_ignored", verdict.evidence)

    def test_the_phone_still_confirms_a_digit_named_company(self):
        verdict = ownership(
            "https://www.hansa12.de/",
            entity_name="Hansa 12 GmbH",
            listing_phone="+49 40 1234567",
            listing_address="Hansa 12 GmbH",
            page_name="Hansa 12 GmbH",
            page_address="",
            page_phone="+49 40 1234567",
        )
        self.assertEqual(verdict.status, STATUS_CONFIRMED)
        self.assertEqual(verdict.reason, "confirmed_by_name_and_phone")


class FinderTest(unittest.TestCase):
    """Where candidates come from, and what each finder admits about itself."""

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)
        self.run_id = db.create_run(
            self.conn,
            fingerprint="fp",
            config_json="{}",
            config_source="<test>",
            collector_version="0",
            schema_version=1,
            go_binary="stub",
            go_version=None,
            target_count=10,
        )
        self.raw_dir = self.tmp / "raw"
        self.raw_dir.mkdir()
        self.site = None

    def seed(self, records, name="a.jsonl"):
        path = self.raw_dir / name
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(
                    json.dumps(
                        {
                            "schema": "osint.record/1",
                            "context": {
                                "provider": "google_maps",
                                "mode": "fast",
                                "query": "dentists in Berlin",
                                "source_url": "https://www.google.com/maps/search/x",
                            },
                            "raw": record,
                            "missing": [],
                            "unknown": [],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        cfg = support.sitediscovery_config(self.tmp / "sites.csv")
        if records:
            ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)
        return cfg

    def indexes(self):
        """The same two lookups ``SiteDiscoverer`` builds, so a finder is tested
        through the interface it is actually called with."""
        entities = list(db.iter_run_entities(self.conn, self.run_id))
        by_key = {str(e["stable_key"]): int(e["id"]) for e in entities}
        by_name = {
            normalize_text(e["name"]): int(e["id"]) for e in entities if e["name"]
        }
        return entities, by_key, by_name

    def finder(self, cfg, name):
        return next(f for f in support.discoverer(cfg).finders() if f.name == name)

    # -- user-supplied list --------------------------------------------------

    def test_the_operator_list_maps_a_row_by_name_or_by_key(self):
        site_csv = support.write_site_list(
            self.tmp / "sites.csv",
            [
                ("", LISTING_TITLE, "https://praxis-mueller.de/"),
                ("google_maps:0xfixture:0x2", "Ignored, key does not match", "https://x.example/"),
                ("", "Someone Else", "https://elsewhere.example/"),
            ],
        )
        cfg = support.sitediscovery_config(site_csv)
        self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")])

        entities, by_key, by_name = self.indexes()
        proposals = self.finder(cfg, "user_list").find(entities, by_key, by_name)

        # Only the row this run knows about produces a candidate: an unknown key
        # and an unknown name are skipped rather than invented into an entity.
        self.assertEqual(len(proposals), 1)
        _, candidate = proposals[0]
        self.assertEqual(candidate.url, "https://praxis-mueller.de/")
        self.assertEqual(candidate.found_by, "user_list")
        self.assertEqual(candidate.origin, "operator-supplied")

    def test_a_missing_operator_file_is_not_configured_rather_than_empty(self):
        cfg = support.sitediscovery_config(self.tmp / "does-not-exist.csv")
        finder = self.finder(cfg, "user_list")
        # The path is set, so the finder is "configured" -- and then honestly
        # finds nothing because the file is not there.
        self.assertTrue(finder.is_configured())
        self.assertEqual(finder.find([], {}, {}), [])

    # -- links on pages already obtained -------------------------------------

    def test_page_links_takes_only_links_that_leave_the_page(self):
        html = support.site_page(
            body=(
                '<a href="/kontakt">Kontakt</a>'
                '<a href="https://www.11467.com/qiye/209193961.htm">directory</a>'
                '<a href="https://praxis-mueller.de/">own site</a>'
            ),
            title="Praxis im Verzeichnis",
        )
        saved = self.tmp / "listing.html"
        saved.write_text(html, encoding="utf-8")

        cfg = support.sitediscovery_config(
            "",
            pages=[
                {
                    "entity": LISTING_TITLE,
                    "path": str(saved),
                    # The page this was saved from is a *different* directory:
                    # the link to 11467 is therefore off-site and is proposed,
                    # while ``/kontakt`` on the page's own host is not.
                    "page": "https://www.b2b168.com/company/1.html",
                }
            ],
        )
        self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")])

        entities, by_key, by_name = self.indexes()
        urls = [c.url for _, c in self.finder(cfg, "page_links").find(entities, by_key, by_name)]
        # The page's own host is excluded; both off-site links are proposed and
        # the *ownership* rule then decides which one is the business's own.
        self.assertEqual(len(urls), 2)
        self.assertNotIn("kontakt", " ".join(urls))
        self.assertIn("https://praxis-mueller.de/", urls)
        self.assertIn("https://www.11467.com/qiye/209193961.htm", urls)
        # The candidate carries where it came from, so a reviewer can see it.
        candidates = [c for _, c in self.finder(cfg, "page_links").find(entities, by_key, by_name)]
        self.assertEqual({c.origin for c in candidates}, {"https://www.b2b168.com/company/1.html"})

    # -- the finder that is not available ------------------------------------

    def test_the_search_finder_declares_itself_unavailable_and_proposes_nothing(self):
        cfg = support.sitediscovery_config(self.tmp / "sites.csv")
        search = self.finder(cfg, "search")

        self.assertFalse(search.is_configured())
        self.assertIn("paid quota", search.not_configured_reason())
        # It never fabricates a result: "no search service" is not "no site".
        self.assertEqual(search.find([], {}, {}), [])

    def test_a_missing_manifest_is_configured_but_warns(self):
        # Three distinct states, and the report must not collapse them: nothing
        # asked for, asked for but missing, asked for and found none.
        cfg = support.sitediscovery_config(
            "", overrides={"sitediscovery": {"page_links": {"manifest": str(self.tmp / "nope.csv")}}}
        )
        finder = self.finder(cfg, "page_links")
        self.assertTrue(finder.is_configured())
        self.assertIn("不存在", finder.configuration_note())
        self.assertEqual(finder.pages(), [])

    def test_a_manifest_with_no_usable_row_warns_differently(self):
        manifest = self.tmp / "empty.csv"
        manifest.write_text("note\nnothing here\n", encoding="utf-8")
        cfg = support.sitediscovery_config(
            "", overrides={"sitediscovery": {"page_links": {"manifest": str(manifest)}}}
        )
        finder = self.finder(cfg, "page_links")
        self.assertEqual(finder.pages(), [])
        self.assertIn("没有可用行", finder.configuration_note())

    def test_the_manifest_reports_every_finder_including_the_absent_one(self):
        cfg = support.sitediscovery_config(self.tmp / "sites.csv")
        manifest = {row["name"]: row for row in support.discoverer(cfg).manifests()}

        self.assertEqual(
            set(manifest), {"user_list", "page_links", "wikidata", "search"}
        )
        self.assertFalse(manifest["search"]["configured"])
        self.assertTrue(manifest["search"]["not_configured_reason"])
        # The account-free lookup is declared too, and says so: a run that never
        # turns it on must still be able to see that it exists.
        self.assertFalse(manifest["wikidata"]["configured"])
        self.assertFalse(manifest["wikidata"]["requires_account"])
        self.assertIn("did not search", manifest["user_list"]["provenance"])

    # -- the account-free structured lookup (034 第二批 2.3) -----------------

    def wikidata_config(self, **settings):
        return support.sitediscovery_config(
            self.tmp / "sites.csv",
            sources=["user_list", "page_links", "wikidata", "search"],
            overrides={"sitediscovery": {"wikidata": {"enabled": True, **settings}}},
        )

    def test_the_query_binds_names_first_and_matches_labels_exactly(self):
        # A FILTER over STR(?label) would scan every item that has a website and
        # risk the endpoint's 60-second query timeout. VALUES binds the constant
        # so the lookup uses an index -- and it is still an exact match, because
        # a near-match would be a guess presented as a fact.
        finder = self.finder(self.wikidata_config(), "wikidata")
        query = finder._sparql(["Praxis Müller", 'Say "Hi"'])
        self.assertIn('"Praxis Müller"@zh', query)
        self.assertIn('"Praxis Müller"@en', query)
        self.assertIn('"Say \\"Hi\\""@zh', query)
        self.assertIn("VALUES ?name", query)
        self.assertIn("wdt:P856", query)
        self.assertNotIn("FILTER", query)

    def test_the_endpoint_answer_is_read_into_candidates(self):
        body = json.dumps(
            {
                "results": {
                    "bindings": [
                        {"name": {"value": "Praxis Müller"},
                         "website": {"value": "https://praxis-mueller.de/"},
                         "item": {"value": "http://www.wikidata.org/entity/Q123"},
                         "type": {"value": "http://www.wikidata.org/entity/Q783794"}},
                        # One item can come back once per P31: still one hit.
                        {"name": {"value": "Praxis Müller"},
                         "website": {"value": "https://praxis-mueller.de/"},
                         "item": {"value": "http://www.wikidata.org/entity/Q123"},
                         "type": {"value": "http://www.wikidata.org/entity/Q4830453"}},
                        # A non-http value is not a website and must be dropped
                        # rather than proposed as one.
                        {"name": {"value": "Other"}, "website": {"value": "ftp://x/"}},
                    ]
                }
            }
        )
        self.assertEqual(
            WikidataFinder._parse(body),
            [
                WikidataHit(
                    name="Praxis Müller",
                    url="https://praxis-mueller.de/",
                    item="Q123",
                    types=("Q4830453", "Q783794"),
                )
            ],
        )

    def test_a_name_match_is_not_an_identity(self):
        # 042: "Wikipedia" matches the encyclopedia item exactly, and that item
        # has an official website. A string match is not a business, so the hit
        # is dropped -- and the drop is reported with the type it had.
        cfg = self.wikidata_config()
        finder = self.finder(cfg, "wikidata")
        finder._query = lambda names: [
            WikidataHit(name=names[0], url="https://wikipedia.readthedocs.io/en/latest/",
                        item="Q52", types=("Q3305213",)),
            WikidataHit(name=names[0], url="https://praxis-mueller.de/",
                        item="Q123", types=("Q783794",)),
        ]
        found = finder.find([{"id": 1, "name": "Praxis Müller"}], {}, {})

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1].url, "https://praxis-mueller.de/")
        self.assertIn("Q123", found[0][1].note)
        self.assertTrue(any("非企业/组织条目" in note for note in finder.runtime_notes))
        self.assertTrue(any("Q3305213" in note for note in finder.runtime_notes))

    def test_an_untyped_item_is_dropped_and_said_to_be_untyped(self):
        cfg = self.wikidata_config()
        finder = self.finder(cfg, "wikidata")
        finder._query = lambda names: [
            WikidataHit(name=names[0], url="https://example.org/", item="Q999"),
        ]
        self.assertEqual(finder.find([{"id": 1, "name": "Praxis Müller"}], {}, {}), [])
        self.assertTrue(any("未声明类型" in note for note in finder.runtime_notes))

    def test_the_type_check_can_be_switched_off_and_says_so_in_the_note(self):
        # The operator's call, not the code's: some seeds are schools or clinics
        # and are not in the business class list.
        cfg = self.wikidata_config(require_org_type=False)
        finder = self.finder(cfg, "wikidata")
        finder._query = lambda names: [
            WikidataHit(name=names[0], url="https://example.org/", item="Q999",
                        types=("Q3914",)),
        ]
        found = finder.find([{"id": 1, "name": "Praxis Müller"}], {}, {})
        self.assertEqual(len(found), 1)
        self.assertIn("Q3914", found[0][1].note)

    def test_an_unreachable_endpoint_is_reported_not_swallowed(self):
        cfg = self.wikidata_config(endpoint="http://127.0.0.1:1/sparql", timeout_seconds=1)
        finder = self.finder(cfg, "wikidata")
        self.assertEqual(finder.find([{"id": 1, "name": "Praxis Müller"}], {}, {}), [])
        self.assertTrue(finder.runtime_notes)
        # And it says so without also claiming a clean measurement.
        self.assertTrue(any("查询未能完成" in note for note in finder.runtime_notes))
        self.assertFalse(any("个有官网声明" in note for note in finder.runtime_notes))

    def test_the_lookup_is_bounded_and_the_shortfall_is_reported(self):
        # One query, two names: the third name is not asked about, and that is
        # said out loud rather than silently dropped.
        cfg = self.wikidata_config(names_per_query=2, max_queries=1)
        finder = self.finder(cfg, "wikidata")
        entities = [{"id": index, "name": f"Firma {index}"} for index in range(3)]

        calls: list[list[str]] = []

        def fake(_names):
            raise WikidataUnavailable("offline in this test")

        finder._query = lambda names: (calls.append(list(names)), fake(names))[1]
        finder.find(entities, {}, {})

        self.assertEqual(len(calls), 1)
        # As spelled by the listing, not case-folded: see
        # ``test_the_query_carries_the_spelling_the_listing_used``.
        self.assertEqual(calls[0], ["Firma 0", "Firma 1"])
        self.assertTrue(any("只查询了前 2/3" in note for note in finder.runtime_notes))

    def test_the_query_carries_the_spelling_the_listing_used(self):
        # 042: the lookup used to send the case-folded name. No label with a
        # capital letter in it can match a folded literal, so every "0 of N
        # answered" was measuring our own lower-casing. Both spellings go now.
        cfg = self.wikidata_config(languages=["de"])
        finder = self.finder(cfg, "wikidata")
        query = finder._sparql(["Römisch-Germanisches Museum"])
        self.assertIn('"Römisch-Germanisches Museum"@de', query)
        self.assertIn('"römisch-germanisches museum"@de', query)

    def test_the_answer_is_matched_back_to_the_business_regardless_of_case(self):
        cfg = self.wikidata_config()
        finder = self.finder(cfg, "wikidata")
        finder._query = lambda names: [
            WikidataHit(name=names[0], url="https://praxis-mueller.de/",
                        item="Q123", types=("Q783794",))
        ]
        found = finder.find([{"id": 7, "name": "Praxis Dr. Müller"}], {}, {})
        self.assertEqual([entity_id for entity_id, _ in found], [7])


class DiscoveryRunTest(unittest.TestCase):
    """The whole stage against a loopback stub site and a real database."""

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)
        self.run_id = db.create_run(
            self.conn,
            fingerprint="fp",
            config_json="{}",
            config_source="<test>",
            collector_version="0",
            schema_version=1,
            go_binary="stub",
            go_version=None,
            target_count=10,
        )
        self.raw_dir = self.tmp / "raw"
        self.raw_dir.mkdir()

    # -- fixtures ------------------------------------------------------------

    def site_csv(self, rows, name="sites.csv") -> pathlib.Path:
        return support.write_site_list(self.tmp / name, rows)

    def seed(self, records, cfg, name="a.jsonl"):
        path = self.raw_dir / name
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(
                    json.dumps(
                        {
                            "schema": "osint.record/1",
                            "context": {
                                "provider": "google_maps",
                                "mode": "fast",
                                "query": "dentists in Berlin",
                                "source_url": "https://www.google.com/maps/search/x",
                            },
                            "raw": record,
                            "missing": [],
                            "unknown": [],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=cfg)

    def run_stage(self, cfg, *, allow_private=True, **kwargs):
        discoverer = support.discoverer(cfg, allow_private=allow_private, **kwargs)
        return discoverer, discoverer.discover_run(self.conn, self.run_id)

    # -- assertions ----------------------------------------------------------

    def candidates(self, status=None):
        return [
            dict(row)
            for row in db.iter_site_candidates(self.conn, self.run_id, status=status)
        ]

    def facts(self, field=None):
        rows = [dict(row) for row in db.iter_run_facts(self.conn, self.run_id)]
        return rows if field is None else [r for r in rows if r["field"] == field]

    # -- confirmed -----------------------------------------------------------

    def test_a_confirmed_site_becomes_a_website_fact_and_fills_the_entity(self):
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, stats = self.run_stage(cfg)

        self.assertEqual(stats.candidates_found, 1)
        self.assertEqual(stats.confirmed, 1)
        self.assertEqual(stats.pages_fetched, 1)

        row = self.candidates()[0]
        self.assertEqual(row["status"], STATUS_CONFIRMED)
        self.assertEqual(row["found_by"], "user_list")
        self.assertEqual(row["http_status"], 200)
        self.assertEqual(row["reason"], "confirmed_by_name_and_phone")

        facts = [f for f in self.facts("website") if f["provider"] == SITE_DISCOVERY_PROVIDER]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["validation_status"], "confirmed")
        self.assertEqual(facts[0]["validation_state"], "site_confirmed_by_own_page")
        self.assertEqual(facts[0]["source_url"], site.url("/"))
        # The evidence behind the verdict is stored, not just the verdict.
        self.assertTrue(json.loads(facts[0]["checks_json"])["phone_agrees"])

        entity = self.conn.execute(
            "SELECT website, domain FROM entities WHERE id = ?", (row["entity_id"],)
        ).fetchone()
        self.assertEqual(entity["website"], site.url("/"))
        self.assertEqual(entity["domain"], "127.0.0.1")

    def test_a_listing_that_already_named_a_site_is_not_second_guessed(self):
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self.seed(
                [
                    support.raw_record(
                        title=LISTING_TITLE, web_site="https://listed.example/", data_id="d1"
                    )
                ],
                cfg,
            )
            _, stats = self.run_stage(cfg)

        # Not fetched at all: the listed site is the operator's data and a
        # discovered one is an inference, so the inference does not run.
        self.assertEqual(site.hits, [])
        self.assertEqual(stats.pages_fetched, 0)
        self.assertEqual(stats.confirmed, 0)
        row = self.candidates()[0]
        self.assertEqual(row["status"], STATUS_REJECTED)
        self.assertEqual(row["reason"], "listing_already_has_a_site")

        entity = self.conn.execute(
            "SELECT website FROM entities WHERE id = ?", (row["entity_id"],)
        ).fetchone()
        self.assertEqual(entity["website"], "https://listed.example/")
        self.assertEqual(
            [f for f in self.facts("website") if f["provider"] == SITE_DISCOVERY_PROVIDER], []
        )

    # -- not confirmed -------------------------------------------------------

    def test_a_name_only_match_stays_pending_and_writes_no_website_fact(self):
        # The page states the right name and nothing independent.
        pages = {"/": Page(body=support.site_page(title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, stats = self.run_stage(cfg)

        self.assertEqual(stats.pending, 1)
        self.assertEqual(stats.confirmed, 0)
        row = self.candidates()[0]
        self.assertEqual(row["status"], STATUS_PENDING)
        self.assertEqual(row["reason"], REASON_ONLY_NAME)
        self.assertTrue(row["note"])
        # A lead is stored, but nothing downstream may read it as a fact.
        self.assertEqual(
            [f for f in self.facts("website") if f["provider"] == SITE_DISCOVERY_PROVIDER], []
        )
        entity = self.conn.execute(
            "SELECT website FROM entities WHERE id = ?", (row["entity_id"],)
        ).fetchone()
        self.assertFalse(entity["website"])

    # -- 045: the home page is not the only page that can answer -----------

    def test_the_contact_page_supplies_the_signal_the_home_page_lacks(self):
        # German businesses put the address and phone in the Impressum, so the
        # home page often states only the name. Reading it is the same question,
        # asked of the page the site answers it on.
        home = support.site_page(
            title=LISTING_TITLE,
            body='<a href="/kontakt">Kontakt</a><a href="/impressum">Impressum</a>',
        )
        pages = {
            "/": Page(body=home),
            # The contact page states the name and nothing independent, so it
            # does not answer the question either; the Impressum does.
            "/kontakt": Page(body=support.site_page(title=LISTING_TITLE)),
            "/impressum": Page(body=support.site_page(jsonld=AGREEING_JSONLD)),
        }
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(
                csv, overrides={"sitediscovery": {"pages_per_site": 3}}
            )
            self.seed(
                [support.raw_record(title=LISTING_TITLE, phone=LISTING_PHONE, web_site="", data_id="d1")],
                cfg,
            )
            _, stats = self.run_stage(cfg)
            hits = site.hits

        self.assertEqual(stats.confirmed, 1)
        self.assertEqual(stats.pending, 0)
        # Contact pages are read in the reader's priority order, within budget.
        self.assertIn("/kontakt", hits)
        self.assertIn("/impressum", hits)
        row = self.candidates()[0]
        self.assertEqual(row["status"], STATUS_CONFIRMED)
        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["pages_budget"], 3)
        self.assertEqual(len(evidence["pages_visited"]), 3)
        # The page that actually carried the confirmation is named, not just the
        # site: "confirmed by the Impressum" is checkable, "confirmed" is not.
        self.assertTrue(evidence["page_values_from"]["phone"].endswith("/impressum"))
        self.assertTrue(evidence["address_agrees"] or evidence["phone_agrees"])

    def test_the_page_budget_still_caps_the_reads(self):
        # pages_per_site counts the home page, so 1 means no follow-up at all.
        home = support.site_page(
            title=LISTING_TITLE, body='<a href="/impressum">Impressum</a>'
        )
        pages = {
            "/": Page(body=home),
            "/impressum": Page(body=support.site_page(jsonld=AGREEING_JSONLD)),
        }
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)  # pages_per_site stays 1
            self.seed(
                [support.raw_record(title=LISTING_TITLE, phone=LISTING_PHONE, web_site="", data_id="d1")],
                cfg,
            )
            _, stats = self.run_stage(cfg)
            hits = site.hits

        self.assertEqual(stats.pending, 1)
        self.assertEqual(stats.confirmed, 0)
        self.assertNotIn("/impressum", hits)
        self.assertEqual(json.loads(self.candidates()[0]["evidence_json"])["pages_visited"], [site.url("/")])

    def test_a_contact_page_that_states_a_different_phone_does_not_confirm(self):
        # The conjunction is not weakened by looking at more pages: the name
        # *and* one independent signal are still required.
        other = json.dumps(
            {"@type": "Dentist", "name": LISTING_TITLE, "telephone": "+49 30 99999999"}
        )
        home = support.site_page(title=LISTING_TITLE, body='<a href="/kontakt">Kontakt</a>')
        pages = {"/": Page(body=home), "/kontakt": Page(body=support.site_page(jsonld=other))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(
                csv, overrides={"sitediscovery": {"pages_per_site": 2}}
            )
            self.seed(
                [support.raw_record(title=LISTING_TITLE, phone=LISTING_PHONE, web_site="", data_id="d1")],
                cfg,
            )
            _, stats = self.run_stage(cfg)

        self.assertEqual(stats.confirmed, 0)
        self.assertEqual(stats.pending, 1)
        self.assertEqual(self.candidates()[0]["reason"], REASON_ONLY_NAME)

    def test_an_off_site_contact_link_is_not_followed(self):
        home = support.site_page(
            title=LISTING_TITLE,
            body='<a href="https://portal.example.org/impressum">Impressum</a>',
        )
        pages = {"/": Page(body=home)}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(
                csv, overrides={"sitediscovery": {"pages_per_site": 3}}
            )
            self.seed(
                [support.raw_record(title=LISTING_TITLE, phone=LISTING_PHONE, web_site="", data_id="d1")],
                cfg,
            )
            _, stats = self.run_stage(cfg)
            hits = site.hits

        # Only the home page was read; the stage never leaves the site.
        self.assertEqual(hits, ["/"])
        evidence = json.loads(self.candidates()[0]["evidence_json"])
        self.assertEqual(evidence["pages_visited"], [site.url("/")])
        self.assertEqual(stats.pending, 1)

    def test_a_contact_page_the_run_already_read_is_not_fetched_again(self):
        # 045.3: discovery reads the contact page, enrichment then reads the same
        # page. A shared cache turns the second read into a lookup.
        from collector.webfetch import FetchResult, PageCache

        home = support.site_page(
            title=LISTING_TITLE, body='<a href="/impressum">Impressum</a>'
        )
        impressum = support.site_page(jsonld=AGREEING_JSONLD)
        pages = {"/": Page(body=home), "/impressum": Page(body=impressum)}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(
                csv, overrides={"sitediscovery": {"pages_per_site": 2}}
            )
            self.seed(
                [support.raw_record(title=LISTING_TITLE, phone=LISTING_PHONE, web_site="", data_id="d1")],
                cfg,
            )
            cache = PageCache()
            cache.put(
                FetchResult(
                    requested_url=site.url("/impressum"),
                    final_url=site.url("/impressum"),
                    ok=True,
                    status_code=200,
                    body=impressum,
                )
            )
            _, stats = self.run_stage(cfg, page_cache=cache)
            hits = site.hits

        self.assertEqual(stats.confirmed, 1)
        self.assertNotIn("/impressum", hits, "a cached page must not be re-requested")
        self.assertEqual(hits, ["/"])
        self.assertGreaterEqual(cache.hits, 1)


    def test_every_verdict_records_the_pages_it_actually_read(self):
        pages = {"/": Page(body=support.site_page(title="Someone Else Entirely"))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, _stats = self.run_stage(cfg)

        evidence = json.loads(self.candidates()[0]["evidence_json"])
        self.assertEqual(evidence["pages_visited"], [site.url("/")])
        self.assertEqual(evidence["pages_budget"], 1)


    def test_a_namesake_page_is_rejected_and_writes_nothing(self):
        pages = {
            "/": Page(
                body=support.site_page(
                    jsonld=json.dumps(
                        {
                            "@type": "Dentist",
                            "name": "Zahnarztpraxis Dr. Schmidt",
                            "telephone": LISTING_PHONE,
                        }
                    ),
                    title="Zahnarztpraxis Dr. Schmidt",
                )
            )
        }
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, stats = self.run_stage(cfg)

        self.assertEqual(stats.rejected, 1)
        row = self.candidates()[0]
        self.assertEqual(row["reason"], REASON_NAME_MISMATCH)
        self.assertEqual(
            [f for f in self.facts("website") if f["provider"] == SITE_DISCOVERY_PROVIDER], []
        )

    def test_a_directory_url_is_rejected_without_being_fetched(self):
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            # A directory host in the operator's own list: the deny list decides
            # before any request, which is also why nothing is fetched -- and why
            # a rate-limited directory is never contacted for a verdict that is
            # already known.
            csv = self.site_csv(
                [("", LISTING_TITLE, "https://www.11467.com/qiye/209193961.htm")]
            )
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, stats = self.run_stage(cfg)

        self.assertEqual(site.hits, [])
        self.assertEqual(stats.pages_fetched, 0)
        self.assertEqual(stats.pages_failed, 0)
        self.assertEqual(stats.rejected, 1)
        self.assertEqual(self.candidates()[0]["reason"], REASON_DIRECTORY_HOST)
        self.assertEqual(self.candidates()[0]["found_by"], "user_list")

    # -- errors and boundaries -----------------------------------------------

    def test_a_private_address_candidate_is_refused_by_the_boundary_policy(self):
        csv = self.site_csv([("", LISTING_TITLE, "http://10.0.0.5/")])
        cfg = support.sitediscovery_config(csv)
        self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
        # Loopback stays reachable for the stub server; private ranges do not --
        # the same boundary production uses.
        _, stats = self.run_stage(cfg, allow_private=False)

        self.assertEqual(stats.boundary_rejections, 1)
        self.assertEqual(stats.pages_fetched, 0)
        row = self.candidates()[0]
        self.assertEqual(row["status"], STATUS_REJECTED)
        self.assertEqual(row["reason"], "url_boundary_rejected")
        errors = [dict(e) for e in db.list_errors(self.conn, self.run_id)]
        self.assertIn("candidate_boundary_rejected", {e["kind"] for e in errors})

    def test_an_unreachable_candidate_is_an_error_not_a_rejection(self):
        pages = {"/other": Page(body="hi")}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/missing"))])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, stats = self.run_stage(cfg)

        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.pages_failed, 1)
        row = self.candidates()[0]
        self.assertEqual(row["status"], STATUS_ERROR)
        self.assertEqual(row["http_status"], 404)
        # "Could not look" must never be stored as "this is not their site".
        self.assertNotIn(row["status"], (STATUS_REJECTED, STATUS_CONFIRMED))

    # -- budget and resume --------------------------------------------------

    def test_the_request_budget_stops_early_and_says_why(self):
        pages = {
            "/a": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE)),
            "/b": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE)),
        }
        with SiteServer(pages) as site:
            csv = self.site_csv(
                [
                    ("", LISTING_TITLE, site.url("/a")),
                    ("", LISTING_TITLE, site.url("/b")),
                ]
            )
            cfg = support.sitediscovery_config(
                csv, overrides={"sitediscovery": {"budget": {"max_requests": 1}}}
            )
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, stats = self.run_stage(cfg)

        self.assertEqual(stats.pages_fetched, 1)
        self.assertEqual(stats.candidates_skipped_budget, 1)
        self.assertIn("request budget exhausted", stats.budget_reason)
        # The un-attempted candidate is absent, not silently marked rejected.
        self.assertEqual(len(self.candidates()), 1)

    def test_a_second_pass_does_not_re_judge_a_candidate(self):
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, first = self.run_stage(cfg)
            hits_after_first = list(site.hits)
            _, second = self.run_stage(cfg)

        self.assertEqual(first.confirmed, 1)
        self.assertEqual(second.pages_fetched, 0)
        self.assertEqual(second.confirmed, 0)
        self.assertEqual(second.candidates_skipped_seen, 1)
        self.assertEqual(site.hits, hits_after_first)
        self.assertEqual(len(self.candidates()), 1)
        self.assertEqual(
            len([f for f in self.facts("website") if f["provider"] == SITE_DISCOVERY_PROVIDER]), 1
        )

    # -- de-duplication after a merge (034 第一批第 4 条) --------------------

    def _two_records_of_one_business(self, cfg):
        """Two records the merge stage folded into one, as entity ids."""
        self.seed(
            [
                support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1"),
                support.raw_record(title=LISTING_TITLE, web_site="", data_id="d2",
                                   phone="+49 30 1234567"),
            ],
            cfg,
        )
        ids = [
            int(row["id"]) for row in self.conn.execute("SELECT id FROM entities ORDER BY id")
        ]
        self.assertEqual(len(ids), 2)
        db.upsert_merge(self.conn, canonical_entity_id=ids[0], entity_id=ids[1],
                        reason="same_name_and_phone", run_id=self.run_id)
        return ids

    def test_a_merged_business_is_discovered_once_not_once_per_record(self):
        # The stage used to walk every entity linked to the run, merged-away
        # members included, so a business two records described cost two checks.
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self._two_records_of_one_business(cfg)
            _, stats = self.run_stage(cfg)

            # One business, one request. Two would mean the de-duplication is
            # not working, and on a real run that is the request budget gone.
            self.assertEqual(stats.entities_considered, 1)
            self.assertEqual(stats.entities_merged_away, 1)
            self.assertEqual(stats.pages_fetched, 1)
            self.assertEqual(len(site.hits), 1)
        self.assertEqual(stats.confirmed, 1)

    def test_the_same_candidate_from_two_records_collapses(self):
        # Two records of one business each propose the same URL: that is one
        # candidate, and the duplicate is counted so the saving is visible.
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv(
                [("", LISTING_TITLE, site.url("/")), ("", LISTING_TITLE, site.url("/"))]
            )
            cfg = support.sitediscovery_config(csv)
            self._two_records_of_one_business(cfg)
            _, stats = self.run_stage(cfg)
            self.assertEqual(stats.candidates_found, 1)
            self.assertEqual(stats.candidates_deduped, 1)
            self.assertEqual(stats.pages_fetched, 1)

    def test_a_merge_after_the_first_pass_does_not_cost_a_second_request(self):
        # The checkpoint is keyed by the business, not by the record. Two records
        # are each checked once while they are separate; after they are merged,
        # both of their candidates fold onto the same key and the next pass asks
        # for nothing.
        other = "Zahnarztpraxis Zweit"
        pages = {"/": Page(body=support.site_page(title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv(
                [("", LISTING_TITLE, site.url("/")), ("", other, site.url("/"))]
            )
            cfg = support.sitediscovery_config(csv)
            self.seed(
                [
                    support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1"),
                    support.raw_record(title=other, web_site="", data_id="d2"),
                ],
                cfg,
            )
            _, first = self.run_stage(cfg)
            ids = [int(r["id"]) for r in self.conn.execute("SELECT id FROM entities ORDER BY id")]
            hits_after_first = list(site.hits)
            db.upsert_merge(self.conn, canonical_entity_id=ids[0], entity_id=ids[1],
                            reason="same_phone", run_id=self.run_id)
            _, second = self.run_stage(cfg)

            self.assertEqual(first.pages_fetched, 2)  # two records, not yet merged
            self.assertEqual(second.pages_fetched, 0)
            self.assertEqual(site.hits, hits_after_first)
            self.assertEqual(second.candidates_found, 1)
            self.assertEqual(second.candidates_deduped, 1)
            self.assertEqual(second.candidates_skipped_seen, 1)

    # -- a failed attempt is not an answer (042) -----------------------------
    #
    # The checkpoint used to be the bare value True, so "a previous pass judged
    # this" and "a previous pass judged this *successfully*" were the same thing:
    # a fetch that failed was skipped for good, and the record said nothing about
    # what the verdict was allowed to use.

    def _clock(self):
        """A clock the test moves by hand.

        The cooldown is real time; a test that had to wait ten minutes would
        either be slow or would quietly never exercise the wait.
        """
        self.moment = {"now": "2026-10-14T00:00:00.000000Z"}
        return lambda: self.moment["now"]

    def _advance(self, seconds: int) -> None:
        self.moment["now"] = _plus_seconds_iso(self.moment["now"], seconds)

    def _checkpoint(self, entity_id: int, url: str):
        return db.get_checkpoint(self.conn, self.run_id, "sitediscovery", f"c:{entity_id}:{url}")

    def test_a_failed_fetch_is_retried_and_then_given_up(self):
        pages = {"/other": Page(body="hi")}
        with SiteServer(pages) as site:
            url = site.url("/missing")
            csv = self.site_csv([("", LISTING_TITLE, url)])
            cfg = support.sitediscovery_config(
                csv,
                overrides={"sitediscovery": {"retry": {"max_attempts": 3, "cooldown_seconds": 0}}},
            )
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            now_iso = self._clock()

            _, first = self.run_stage(cfg, now_iso=now_iso)
            self.assertEqual(first.errors, 1)
            _, second = self.run_stage(cfg, now_iso=now_iso)
            _, third = self.run_stage(cfg, now_iso=now_iso)
            _, fourth = self.run_stage(cfg, now_iso=now_iso)

        self.assertEqual(second.candidates_retried, 1)
        self.assertEqual(third.candidates_retried, 1)
        # Bounded: three attempts were allowed and all three happened, so the
        # fourth pass asks nothing and says which reason stopped it.
        self.assertEqual(fourth.candidates_failed_given_up, 1)
        self.assertEqual(fourth.errors, 0)
        self.assertEqual(fourth.pages_fetched, 0)
        entity_id = self.candidates()[0]["entity_id"]
        self.assertEqual(self._checkpoint(entity_id, url)["attempts"], 3)
        # "Could not look" is still not "this is not their site".
        self.assertEqual(self.candidates()[0]["status"], STATUS_ERROR)

    def test_a_failure_inside_the_cooldown_is_not_re_asked(self):
        pages = {"/other": Page(body="hi")}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/missing"))])
            cfg = support.sitediscovery_config(
                csv,
                overrides={"sitediscovery": {"retry": {"max_attempts": 5, "cooldown_seconds": 600}}},
            )
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            now_iso = self._clock()
            _, first = self.run_stage(cfg, now_iso=now_iso)
            self.assertEqual(first.errors, 1)

            _, too_soon = self.run_stage(cfg, now_iso=now_iso)
            self.assertEqual(too_soon.candidates_skipped_cooling, 1)
            self.assertEqual(too_soon.errors, 0)
            self.assertEqual(too_soon.pages_fetched, 0)

            self._advance(600)
            _, later = self.run_stage(cfg, now_iso=now_iso)
        self.assertEqual(later.candidates_retried, 1)

    def test_a_retry_that_gets_through_confirms_the_site(self):
        # The point of retrying: a host that was down once is not a business
        # without a website.
        with SiteServer({}) as site:
            url = site.url("/own")
            csv = self.site_csv([("", LISTING_TITLE, url)])
            cfg = support.sitediscovery_config(
                csv,
                overrides={"sitediscovery": {"retry": {"max_attempts": 2, "cooldown_seconds": 0}}},
            )
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            now_iso = self._clock()
            _, first = self.run_stage(cfg, now_iso=now_iso)

            site.pages["/own"] = Page(
                body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE)
            )
            _, second = self.run_stage(cfg, now_iso=now_iso)

        self.assertEqual(first.errors, 1)
        self.assertEqual(second.candidates_retried, 1)
        self.assertEqual(second.confirmed, 1)
        self.assertEqual(self.candidates()[0]["status"], STATUS_CONFIRMED)
        self.assertEqual(
            len([f for f in self.facts("website") if f["provider"] == SITE_DISCOVERY_PROVIDER]), 1
        )

    def test_new_evidence_re_opens_a_verdict_taken_without_it(self):
        # The first pass cannot compare an address it does not have, so it can
        # only say "the name agrees". The listing later gains the address: the
        # verdict that never saw it must not stand.
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            # Neither an address nor a phone: the page states both, but there is
            # nothing to compare them against.
            self.conn.execute(
                "UPDATE entities SET address = NULL, address_norm = NULL, phone_norm = NULL"
            )
            self.conn.commit()
            _, first = self.run_stage(cfg)
            self.assertEqual(first.pending, 1)

            self.conn.execute(
                "UPDATE entities SET address = ?, address_norm = ?, phone_norm = ?",
                (LISTING_ADDRESS, normalize_text(LISTING_ADDRESS), LISTING_PHONE),
            )
            self.conn.commit()
            _, second = self.run_stage(cfg)

        self.assertEqual(second.candidates_rejudged, 1)
        self.assertEqual(second.confirmed, 1)
        self.assertEqual(self.candidates()[0]["status"], STATUS_CONFIRMED)

    def test_a_verdict_taken_under_other_rules_is_re_judged(self):
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, first = self.run_stage(cfg)
            self.assertEqual(first.confirmed, 1)

            # As if an older build had written it and the rules then changed.
            row = self.candidates()[0]
            state = self._checkpoint(row["entity_id"], row["url"])
            state["rules"] = "site-ownership/0"
            db.set_checkpoint(
                self.conn, self.run_id, "sitediscovery",
                f"c:{row['entity_id']}:{row['url']}", state,
            )
            _, second = self.run_stage(cfg)

        self.assertEqual(second.candidates_rejudged, 1)
        self.assertEqual(second.pages_fetched, 1)
        # Revisiting it reaches the same answer rather than overturning it.
        self.assertEqual(self.candidates()[0]["status"], STATUS_CONFIRMED)

    def test_a_legacy_checkpoint_is_read_from_the_verdict_it_wrote(self):
        # Pre-042 stored the bare value True. It is adopted from the verdict row
        # instead of being guessed at, so a confirmed verdict still costs nothing.
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, first = self.run_stage(cfg)
            row = self.candidates()[0]
            db.set_checkpoint(
                self.conn, self.run_id, "sitediscovery",
                f"c:{row['entity_id']}:{row['url']}", True,
            )
            _, second = self.run_stage(cfg)

        self.assertEqual(first.confirmed, 1)
        self.assertEqual(second.pages_fetched, 0)
        self.assertEqual(second.candidates_legacy_adopted, 1)
        self.assertEqual(self.candidates()[0]["status"], STATUS_CONFIRMED)
        # Adopted, not merely tolerated: it is now written in the new shape, so
        # the next pass reads it directly.
        state = self._checkpoint(row["entity_id"], row["url"])
        self.assertEqual(state["status"], STATUS_CONFIRMED)
        self.assertTrue(state["rules"])
        self.assertTrue(state["fingerprint"])

    def test_a_legacy_checkpoint_on_a_failure_is_retried_not_skipped(self):
        pages = {"/other": Page(body="hi")}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/missing"))])
            cfg = support.sitediscovery_config(
                csv,
                overrides={"sitediscovery": {"retry": {"max_attempts": 2, "cooldown_seconds": 0}}},
            )
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, first = self.run_stage(cfg)
            row = self.candidates()[0]
            db.set_checkpoint(
                self.conn, self.run_id, "sitediscovery",
                f"c:{row['entity_id']}:{row['url']}", True,
            )
            _, second = self.run_stage(cfg)

        self.assertEqual(first.errors, 1)
        # A legacy value that means "judged" must never become "skip forever".
        self.assertEqual(second.candidates_retried, 1)
        self.assertEqual(second.errors, 1)

    def test_a_legacy_checkpoint_with_no_verdict_row_is_judged_once(self):
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            url = site.url("/")
            csv = self.site_csv([("", LISTING_TITLE, url)])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, first = self.run_stage(cfg)
            row = self.candidates()[0]
            self.conn.execute("DELETE FROM site_candidates")
            self.conn.commit()
            db.set_checkpoint(
                self.conn, self.run_id, "sitediscovery",
                f"c:{row['entity_id']}:{url}", True,
            )
            _, second = self.run_stage(cfg)

        self.assertEqual(second.pages_fetched, 1)
        # The website fact from the first pass is still the only one -- a second
        # identical fact would be double-counting, not a second confirmation.
        self.assertEqual(
            len([f for f in self.facts("website") if f["provider"] == SITE_DISCOVERY_PROVIDER]), 1
        )
        self.assertEqual(self.candidates()[0]["status"], STATUS_CONFIRMED)
        self.assertEqual(self._checkpoint(row["entity_id"], url)["attempts"], 1)

    def test_the_verdict_row_says_what_it_depended_on(self):
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            _, stats = self.run_stage(cfg)

        row = self.candidates()[0]
        judgement = json.loads(row["evidence_json"])["_judgement"]
        self.assertEqual(judgement["rules"], stats.rules_id)
        self.assertTrue(judgement["evidence"])
        self.assertEqual(judgement["attempts"], 1)
        self.assertEqual(judgement["basis"], "new")
        # The fingerprint is over what the verdict was allowed to use, so a
        # different listing evidence is a different fingerprint.
        self.assertNotEqual(judgement["evidence"], _sha16([]))

    def test_a_confirmed_site_is_bound_to_the_business_it_was_judged_for(self):
        # Two businesses, two candidates: the confirmation belongs to the one
        # whose page agreed, not to whichever record was walked last.
        other = "Zahnarztpraxis Andere"
        pages = {
            "/own": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE)),
            "/namesake": Page(body=support.site_page(title="Jemand Anderes")),
        }
        with SiteServer(pages) as site:
            csv = self.site_csv(
                [("", LISTING_TITLE, site.url("/own")), ("", other, site.url("/namesake"))]
            )
            cfg = support.sitediscovery_config(csv)
            self.seed(
                [
                    support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1"),
                    support.raw_record(title=other, web_site="", data_id="d2"),
                ],
                cfg,
            )
            _, stats = self.run_stage(cfg)

        self.assertEqual(stats.confirmed, 1)
        self.assertEqual(stats.rejected, 1)
        ids = {
            row["name"]: int(row["id"])
            for row in self.conn.execute("SELECT id, name FROM entities")
        }
        own_id = ids[LISTING_TITLE]
        facts = [f for f in self.facts("website") if f["provider"] == SITE_DISCOVERY_PROVIDER]
        self.assertEqual(len(facts), 1)
        self.assertEqual(int(facts[0]["entity_id"]), own_id)
        website_of_other = self.conn.execute(
            "SELECT website FROM entities WHERE id = ?", (ids[other],)
        ).fetchone()["website"]
        self.assertFalse(website_of_other)

    # -- reusing a verdict across runs (034 第一批第 4 条) --------------------

    def new_run(self, fingerprint="fp-2") -> int:
        """A second run in the same database, as a re-run of the same job.

        The entities are keyed by ``stable_key``, which is unique across the
        whole database, so re-ingesting the same business lands on the *same*
        entity row -- which is what makes a cross-run verdict reusable at all.
        """
        return db.create_run(
            self.conn,
            fingerprint=fingerprint,
            config_json="{}",
            config_source="<test>",
            collector_version="0",
            schema_version=1,
            go_binary="stub",
            go_version=None,
            target_count=10,
        )

    def seed_into(self, run_id: int, cfg):
        """Link the already-ingested evidence to another run."""
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=run_id, cfg=cfg)

    def test_a_confirmed_verdict_inside_the_window_is_reused_not_re_asked(self):
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(
                csv, overrides={"sitediscovery": {"reuse_within_days": 7}}
            )
            records = [support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")]
            self.seed(records, cfg)
            _, first = self.run_stage(cfg)
            hits_after_first = list(site.hits)

            second_run = self.new_run()
            self.seed_into(second_run, cfg)
            discoverer = support.discoverer(cfg)
            second = discoverer.discover_run(self.conn, second_run)

        self.assertEqual(first.confirmed, 1)
        # The whole point: the second run spends nothing on a question the
        # database can already answer.
        self.assertEqual(second.pages_fetched, 0)
        self.assertEqual(second.candidates_reused, 1)
        self.assertEqual(site.hits, hits_after_first)

        rows = [
            dict(row)
            for row in db.iter_site_candidates(self.conn, second_run, status=STATUS_CONFIRMED)
        ]
        self.assertEqual(len(rows), 1)
        # A reused verdict still says it was reused, so the export cannot present
        # it as a check that happened in this run.
        self.assertIn(REUSED_NOTE.strip(), rows[0]["note"] or "")
        self.assertEqual(rows[0]["http_status"], 200)

    def test_with_no_window_a_new_run_re_asks(self):
        # Default is 0 days: a new run re-judges. The first verdict here is
        # *pending*, so nothing was bound to the business and the second run has
        # a real question left to answer.
        pages = {"/": Page(body=support.site_page(title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(csv)
            records = [support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")]
            self.seed(records, cfg)
            _, first = self.run_stage(cfg)
            hits_after_first = list(site.hits)

            second_run = self.new_run()
            self.seed_into(second_run, cfg)
            second = support.discoverer(cfg).discover_run(self.conn, second_run)

        self.assertEqual(first.pending, 1)
        self.assertEqual(second.candidates_reused, 0)
        self.assertEqual(second.pages_fetched, 1)
        self.assertEqual(len(site.hits), len(hits_after_first) + 1)

    def test_a_window_does_not_resurrect_a_rejection(self):
        # Only a *confirmed* verdict is reusable. A rejection is re-judged: a
        # site that was wrong last week can be right today, and a business that
        # had no site can put one up.
        pages = {"/": Page(body=support.site_page(title="Jemand Anderes"))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(
                csv, overrides={"sitediscovery": {"reuse_within_days": 7}}
            )
            records = [support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")]
            self.seed(records, cfg)
            _, first = self.run_stage(cfg)
            hits_after_first = list(site.hits)

            second_run = self.new_run()
            self.seed_into(second_run, cfg)
            second = support.discoverer(cfg).discover_run(self.conn, second_run)

        self.assertEqual(first.rejected, 1)
        self.assertEqual(second.candidates_reused, 0)
        self.assertEqual(second.pages_fetched, 1)
        self.assertEqual(len(site.hits), len(hits_after_first) + 1)

    def test_a_verdict_outside_the_window_is_not_reused(self):
        # The window is the expiry. Backdating the verdict outside it must put
        # the candidate back on the "ask again" side of the line.
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(
                csv, overrides={"sitediscovery": {"reuse_within_days": 7}}
            )
            records = [support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")]
            self.seed(records, cfg)
            _, first = self.run_stage(cfg)

            stale = _days_ago_iso(30)
            self.conn.execute("UPDATE site_candidates SET judged_at = ?", (stale,))
            self.conn.commit()
            self.assertIsNone(
                db.recent_site_verdict(
                    self.conn,
                    entity_id=self._only_entity_id(),
                    url=site.url("/"),
                    newer_than=_days_ago_iso(7),
                )
            )

            second_run = self.new_run()
            self.seed_into(second_run, cfg)
            second = support.discoverer(cfg).discover_run(self.conn, second_run)

        self.assertEqual(first.confirmed, 1)
        self.assertEqual(second.candidates_reused, 0)
        for row in db.iter_site_candidates(self.conn, second_run):
            self.assertNotIn(REUSED_NOTE.strip(), dict(row)["note"] or "")

    def test_a_verdict_is_reused_for_its_own_business_only(self):
        # The same URL proposed for two different businesses is two different
        # questions; the cache is scoped to the business, not to the URL.
        url = "https://confirmed.example/"
        entity = self._one_entity()
        db.upsert_site_candidate(
            self.conn, run_id=self.run_id, entity_id=entity, url=url, host="confirmed.example",
            found_by="user_list", origin="operator", status=STATUS_CONFIRMED,
            reason="confirmed_by_name_and_phone", note="first verdict", evidence={},
            http_status=200,
        )
        other = db.upsert_entity(
            self.conn,
            stable_key="k-other", id_kind="gmaps", name="Andere Praxis",
            name_norm=normalize_text("Andere Praxis"), address=None, address_norm=None,
            latitude=None, longitude=None, phone_norm=None, website=None, domain=None,
            country="DE",
        )
        self.assertIsNotNone(
            db.recent_site_verdict(
                self.conn, entity_id=entity, url=url, newer_than=_days_ago_iso(7)
            )
        )
        self.assertIsNone(
            db.recent_site_verdict(
                self.conn, entity_id=other, url=url, newer_than=_days_ago_iso(7)
            )
        )

    def test_a_pending_verdict_is_never_reused(self):
        url = "https://maybe.example/"
        entity = self._one_entity()
        db.upsert_site_candidate(
            self.conn, run_id=self.run_id, entity_id=entity, url=url, host="maybe.example",
            found_by="user_list", origin="operator", status=STATUS_PENDING,
            reason=REASON_ONLY_NAME, note="", evidence={}, http_status=200,
        )
        self.assertIsNone(
            db.recent_site_verdict(
                self.conn, entity_id=entity, url=url, newer_than=_days_ago_iso(7)
            )
        )

    def _one_entity(self) -> int:
        """One business in the database, without running a stage."""
        self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")],
                  support.config())
        rows = [int(r["id"]) for r in self.conn.execute("SELECT id FROM entities ORDER BY id")]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _only_entity_id(self) -> int:
        rows = [int(r["id"]) for r in self.conn.execute("SELECT id FROM entities ORDER BY id")]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_a_structured_lookup_hit_still_has_to_pass_the_ownership_check(self):
        # A structured statement that "this is their website" is a lead, not a
        # fact: the page itself still has to say so. This page says otherwise.
        pages = {"/": Page(body=support.site_page(title="Jemand Anderes"))}
        with SiteServer(pages) as site:
            csv = self.site_csv([("", LISTING_TITLE, site.url("/"))])
            cfg = support.sitediscovery_config(
                csv,
                sources=["user_list", "page_links", "wikidata", "search"],
                overrides={"sitediscovery": {"wikidata": {"enabled": True}}},
            )
            finder = next(
                f for f in support.discoverer(cfg).finders() if f.name == "wikidata"
            )
            finder._query = lambda names: [
                WikidataHit(name=names[0], url=site.url("/"), item="Q1", types=("Q783794",))
            ]

            discoverer = support.discoverer(cfg)
            discoverer.finders = lambda: [finder]  # one source, so the origin is unambiguous
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            stats = discoverer.discover_run(self.conn, self.run_id)

        self.assertEqual(stats.candidates_found, 1)
        self.assertEqual(stats.rejected, 1)
        rows = [dict(row) for row in db.iter_site_candidates(self.conn, self.run_id)]
        self.assertEqual(rows[0]["found_by"], "wikidata")
        self.assertEqual(rows[0]["origin"], "structured-open-data")
        # Nothing was written: an unconfirmed candidate is not a website.
        self.assertEqual(
            [f for f in self.facts("website") if f["provider"] == SITE_DISCOVERY_PROVIDER], []
        )

    # -- reporting -----------------------------------------------------------

    def test_a_named_file_that_is_not_there_is_reported_not_hidden(self):
        # The distinction the task book insists on: zero candidates because
        # nothing was asked for, because the file is missing, or because it was
        # asked and found none. The run's report must name which one it is.
        cfg = support.sitediscovery_config(self.tmp / "missing-sites.csv")
        self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
        discoverer, stats = self.run_stage(cfg)

        self.assertEqual(stats.candidates_found, 0)
        self.assertTrue(stats.finder_notes)
        self.assertTrue(any("不存在" in note for note in stats.finder_notes))
        # Configured-but-missing is not "not configured": the operator did name
        # a file, and saying otherwise would send them looking in the wrong place.
        self.assertFalse(
            any(note.startswith("user_list:") for note in stats.unavailable_finders),
            stats.unavailable_finders,
        )

    def test_the_summary_counts_every_verdict_exactly_once(self):
        pages = {"/good": Page(body=support.site_page(title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = self.site_csv(
                [
                    ("", LISTING_TITLE, site.url("/good")),
                    ("", LISTING_TITLE, "https://www.11467.com/qiye/1.htm"),
                    ("", LISTING_TITLE, "http://10.0.0.5/"),
                ]
            )
            cfg = support.sitediscovery_config(csv)
            self.seed([support.raw_record(title=LISTING_TITLE, web_site="", data_id="d1")], cfg)
            discoverer, stats = self.run_stage(cfg, allow_private=False)
            summary = discoverer.summary(self.conn, self.run_id)

        self.assertEqual(summary["candidates"], 3)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["rejected"], 2)
        self.assertEqual(summary["confirmed"], 0)
        self.assertEqual(summary["errors"], 0)
        # Exactly one page was read: the two refused candidates cost nothing.
        self.assertEqual(stats.pages_fetched, 1)
        self.assertEqual(stats.rejected, summary["rejected"])
        self.assertEqual(stats.pending, summary["pending"])
        self.assertEqual(
            summary["candidates"],
            summary["confirmed"] + summary["pending"] + summary["rejected"] + summary["errors"],
        )
        by_finder = {row["finder"]: row for row in summary["by_finder"]}
        self.assertEqual(by_finder["user_list"]["candidates"], 3)

        # The stage's own report names the finder that could not look.
        self.assertTrue(stats.unavailable_finders)
        self.assertTrue(any("search" in item for item in stats.unavailable_finders))


class StageWiringTest(unittest.TestCase):
    """The stage runs inside a real orchestrated run, between merge and enrich."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def fixture(self, website: str, path="fixture.json") -> pathlib.Path:
        target = self.tmp / path
        target.write_text(
            json.dumps(
                [
                    {
                        "title": LISTING_TITLE,
                        "address": LISTING_ADDRESS,
                        "phone": LISTING_PHONE,
                        "web_site": website,
                        "data_id": "0xfixture:0x1",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return target

    def test_a_run_discovers_the_site_and_hands_it_to_the_enricher(self):
        from collector.enrich import Enricher

        pages = {
            "/": Page(
                body=support.site_page(
                    jsonld=AGREEING_JSONLD,
                    body='<a href="/kontakt">Kontakt</a>',
                    title=LISTING_TITLE,
                )
            ),
            "/kontakt": Page(body=support.site_page(title=LISTING_TITLE)),
        }
        with SiteServer(pages) as site:
            csv = support.write_site_list(
                self.tmp / "sites.csv", [("", LISTING_TITLE, site.url("/"))]
            )
            cfg = support.sitediscovery_config(
                csv,
                overrides={
                    "sitediscovery": {"pages_per_site": 1},
                    "enrich": {
                        "enabled": True,
                        "fields": ["title", "phone", "address", "website"],
                        "pages_per_site": 1,
                    },
                },
            )
            orch = Orchestrator(
                cfg,
                base_dir=self.tmp,
                adapter=support.stub_adapter(
                    {"STUB_FIXTURE": str(self.fixture("", "no-site.json"))}
                ),
                discoverer=support.discoverer(cfg),
                enricher=Enricher(
                    cfg,
                    fetcher=support.local_fetcher(),
                    resolver=support.FakeResolver(),
                    sleep=lambda _seconds: None,
                    polite_delay_seconds=0,
                ),
            )
            result = orch.run()

        self.assertEqual(result.status, "completed")
        self.assertIn("completed", result.stages["sitediscovery"])
        self.assertEqual(result.sitediscovery_stats["confirmed"], 1)
        # The run's own report must not read as if a whole-web search happened.
        self.assertEqual(result.sitediscovery_stats["by_finder"][0]["finder"], "user_list")
        self.assertTrue(result.sitediscovery_stats["unavailable_finders"])

        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        self.assertEqual(
            db.get_stage(conn, result.run_id, "sitediscovery")["status"], "completed"
        )
        # The discovered site travelled the ordinary path, so the export shows it.
        facts = [
            json.loads(line)
            for line in pathlib.Path(result.exports["field_facts"])
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        website_facts = [
            row
            for row in facts
            if row["field"] == "website" and row["provider"] == SITE_DISCOVERY_PROVIDER
        ]
        self.assertEqual(len(website_facts), 1)
        self.assertTrue(website_facts[0]["source_url"].startswith(site.url("/")))

    def test_a_resumed_run_does_not_pay_for_the_same_candidate_twice(self):
        # 034 第一批第 5 条: "恢复" is a condition change too, and the correct
        # update is *no* update -- the verdicts are already there. A resume that
        # re-asked would spend the budget a second time on questions the run has
        # already answered, and on a rate-limited site that is the difference
        # between finishing and getting refused.
        pages = {"/": Page(body=support.site_page(jsonld=AGREEING_JSONLD, title=LISTING_TITLE))}
        with SiteServer(pages) as site:
            csv = support.write_site_list(
                self.tmp / "sites.csv", [("", LISTING_TITLE, site.url("/"))]
            )
            cfg = support.sitediscovery_config(csv)
            first = Orchestrator(
                cfg,
                base_dir=self.tmp,
                adapter=support.stub_adapter(
                    {"STUB_FIXTURE": str(self.fixture("", "no-site.json"))}
                ),
                discoverer=support.discoverer(cfg),
            ).run()
            hits_after_first = list(site.hits)

            # Reopen the stage the way a resume of an interrupted run finds it.
            conn = db.connect(pathlib.Path(first.database))
            self.addCleanup(conn.close)
            db.set_stage(conn, first.run_id, "sitediscovery", "pending")
            again = Orchestrator(
                cfg,
                base_dir=self.tmp,
                adapter=support.stub_adapter(
                    {"STUB_FIXTURE": str(self.fixture("", "no-site.json"))}
                ),
                discoverer=support.discoverer(cfg),
            )
            second_run = RunResult(
                run_id=first.run_id, fingerprint=first.fingerprint, status="running",
                target_count=first.target_count, collected=first.collected, shortfall=0,
                resumed=True, run_dir=first.run_dir, database=first.database,
            )
            status = again._stage_sitediscovery(conn, first.run_id, second_run)

        self.assertEqual(first.sitediscovery_stats["confirmed"], 1)
        # The stage reports itself done without asking anything again.
        self.assertIn("completed", status)
        self.assertEqual(site.hits, hits_after_first)
        # Nothing new was decided, so nothing new is stored.
        self.assertEqual(db.site_candidate_counts(conn, first.run_id)["confirmed"], 1)
        self.assertEqual(second_run.sitediscovery_stats["pages_fetched"], 0)
        self.assertEqual(second_run.sitediscovery_stats["candidates_skipped_seen"], 1)
        # The verdict is still *reported*, because the run's picture has not
        # changed -- "nothing was asked again" and "the answer disappeared"
        # would be two very different resumes.
        self.assertEqual(second_run.sitediscovery_stats["confirmed"], 1)

    def test_a_disabled_section_leaves_the_run_without_a_candidate_row(self):
        cfg = support.config()
        orch = Orchestrator(
            cfg,
            base_dir=self.tmp,
            adapter=support.stub_adapter({"STUB_FIXTURE": str(self.fixture("", "s2.json"))}),
        )
        result = orch.run()

        self.assertIn("skipped", result.stages["sitediscovery"])
        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        self.assertEqual(db.site_candidate_counts(conn, result.run_id)["candidates"], 0)
        self.assertEqual(result.sitediscovery_stats, {})


if __name__ == "__main__":
    unittest.main()
