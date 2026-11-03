"""Website enrichment: budgets, boundaries, conflicts and failure isolation."""

from __future__ import annotations

import json
import unittest

from collector import db, ingest
from collector.emailcheck import DNS_OK, DNS_TIMEOUT
from collector.enrich import (
    STATE_BOUNDARY_REJECTED,
    STATE_CONFIRMED_BY_WEBSITE,
    STATE_CONFLICTS_WITH_LISTING,
    STATE_SOURCE_CONFLICT,
    Enricher,
)

from .stub_site import Page, SiteServer
from .support import (
    FakeResolver,
    enrich_config,
    local_fetcher,
    raw_record,
    site_page,
    temp_dir,
)

LISTING_TITLE = "Praxis Dr. M\u00fcller"
LISTING_ADDRESS = "Musterstra\u00dfe 1, 10115 Berlin, DE"
LISTING_PHONE = "+49 30 12345678"

DENTIST_JSONLD = json.dumps(
    {
        "@context": "https://schema.org",
        "@type": "Dentist",
        "name": LISTING_TITLE,
        "email": "mailto:info@praxis-mueller.de",
        "telephone": LISTING_PHONE,
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "Musterstra\u00dfe 1",
            "postalCode": "10115",
            "addressLocality": "Berlin",
            "addressCountry": "DE",
        },
    }
)

HOME_LINKS = (
    '<a href="/kontakt">Kontakt</a>'
    '<a href="/standorte">Standorte</a>'
    '<a href="/impressum">Impressum</a>'
    '<a href="https://www.fremdportal.de/datenschutz">Datenschutz</a>'
)


class EnrichTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)
        self.cfg = enrich_config()
        self.run_id = db.create_run(
            self.conn,
            fingerprint=self.cfg.fingerprint(),
            config_json=self.cfg.canonical_json(),
            config_source="<test>",
            collector_version="0.2.0",
            schema_version=1,
            go_binary="stub",
            go_version=None,
            target_count=10,
        )
        self.raw_dir = self.tmp / "raw"
        self.raw_dir.mkdir()

    # -- seeding -------------------------------------------------------------

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
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=self.cfg)

    def enrich(self, *, resolver=None, cfg=None, allow_private=True, **kwargs):
        cfg = cfg or self.cfg
        enricher = Enricher(
            cfg,
            fetcher=local_fetcher(allow_private=allow_private),
            resolver=resolver if resolver is not None else FakeResolver(),
            sleep=lambda _seconds: None,
            polite_delay_seconds=0,
            **kwargs,
        )
        return enricher.enrich_run(self.conn, self.run_id)

    # -- assertions ----------------------------------------------------------

    def facts(self, field=None):
        rows = [dict(row) for row in db.iter_run_facts(self.conn, self.run_id)]
        if field is not None:
            rows = [row for row in rows if row["field"] == field]
        return rows

    def website_facts(self, field=None):
        return [row for row in self.facts(field) if row["provider"] == "website"]

    def errors(self):
        return [dict(row) for row in db.list_errors(self.conn, self.run_id)]


class FullEnrichmentTest(EnrichTestCase):
    def setUp(self):
        super().setUp()
        self.pages = {
            "/": Page(body=site_page(jsonld=DENTIST_JSONLD, body=HOME_LINKS, title=LISTING_TITLE)),
            "/kontakt": Page(
                body=site_page(
                    body='<a href="mailto:kontakt@praxis-mueller.de">Mail</a>'
                    '<a href="tel:+493012345678">Telefon</a>'
                )
            ),
            "/standorte": Page(body=site_page(body="<p>Standorte</p>")),
            "/impressum": Page(body=site_page(body="<p>Impressum</p>")),
        }

    def test_homepage_and_budgeted_same_site_pages_are_visited(self):
        with SiteServer(self.pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            stats = self.enrich(
                resolver=FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))})
            )
            self.assertEqual(stats.entities_with_website, 1)
            self.assertEqual(stats.sites_visited, 1)
            # homepage + /kontakt + /standorte, capped at pages_per_site = 3
            self.assertEqual(stats.pages_fetched, 3)
            self.assertNotIn("/impressum", site.hits)

    def test_every_value_records_the_page_it_came_from(self):
        with SiteServer(self.pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            self.enrich(
                resolver=FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))})
            )

            emails = self.website_facts("emails")
            self.assertTrue(emails)
            for row in emails:
                self.assertTrue(row["source_url"].startswith(site.url("/")))
                self.assertIn("#", row["evidence_ref"])
                self.assertTrue(row["evidence_snippet"])
                self.assertTrue(row["extractor_version"].startswith("website-"))

    def test_an_email_from_the_website_is_validated_against_dns(self):
        with SiteServer(self.pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            self.enrich(
                resolver=FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))})
            )
            row = self.website_facts("emails")[0]
            self.assertEqual(row["validation_state"], "mx_present")
            self.assertEqual(row["validation_status"], "valid")
            checks = json.loads(row["checks_json"])
            self.assertEqual(checks["mx_hosts"], ["mx.praxis-mueller.de"])
            self.assertIn("never sends mail", row["validation_note"])

    def test_an_agreeing_phone_is_recorded_as_a_confirmation(self):
        with SiteServer(self.pages) as site:
            self.seed(
                [
                    raw_record(
                        data_id="d1",
                        title=LISTING_TITLE,
                        phone=LISTING_PHONE,
                        web_site=site.url("/"),
                    )
                ]
            )
            stats = self.enrich(
                resolver=FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))})
            )
            self.assertGreaterEqual(stats.confirmations, 1)
            confirmed = [
                row for row in self.website_facts("phone") if row["validation_status"] == "confirmed"
            ]
            self.assertTrue(confirmed)
            self.assertEqual(confirmed[0]["validation_state"], STATE_CONFIRMED_BY_WEBSITE)

    def test_the_address_components_are_stored_as_validation_evidence(self):
        with SiteServer(self.pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            self.enrich(
                resolver=FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))})
            )
            row = self.website_facts("address")[0]
            checks = json.loads(row["checks_json"])
            self.assertEqual(checks["components"]["postal_code"], "10115")
            self.assertEqual(checks["components"]["city"], "Berlin")
            self.assertFalse(checks["postal_verified"])
            self.assertIn("postal_verified", checks)

    def test_off_site_links_are_never_fetched(self):
        with SiteServer(self.pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            self.enrich(
                resolver=FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))})
            )
            for path in site.hits:
                self.assertFalse(path.startswith("/datenschutz"))


class NoEmailTest(EnrichTestCase):
    def test_a_site_without_an_email_produces_no_email_fact_and_no_failure(self):
        no_email_jsonld = json.dumps(
            {
                "@type": "Dentist",
                "name": LISTING_TITLE,
                "telephone": LISTING_PHONE,
                "address": {"streetAddress": "Musterstra\u00dfe 1", "postalCode": "10115"},
            }
        )
        pages = {"/": Page(body=site_page(jsonld=no_email_jsonld, body="<p>Kein Kontakt</p>"))}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            stats = self.enrich(
                resolver=FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))})
            )
            self.assertEqual(stats.pages_failed, 0)
            # The listing supplies an email; the website contributes none, and
            # nothing is invented to fill the gap.
            self.assertEqual(self.website_facts("emails"), [])
            self.assertTrue(self.website_facts("phone"))


class BoundaryTest(EnrichTestCase):
    def test_a_private_address_in_the_listing_is_refused_and_recorded(self):
        with SiteServer({"/": Page(body=site_page())}) as site:
            self.seed(
                [
                    raw_record(data_id="bad", web_site="http://10.0.0.1/"),
                    raw_record(data_id="good", title=LISTING_TITLE, web_site=site.url("/")),
                ]
            )
            # Loopback stays reachable for the stub server; private ranges do not.
            stats = self.enrich(allow_private=False)
            self.assertEqual(stats.boundary_rejections, 1)
            # The other entity was still enriched: one bad site loses nothing.
            self.assertEqual(stats.entities_with_website, 2)

            kinds = {row["kind"] for row in self.errors()}
            self.assertIn("website_boundary_rejected", kinds)

            rejected = [
                row for row in self.website_facts("website")
                if row["validation_state"] == STATE_BOUNDARY_REJECTED
            ]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["validation_status"], "invalid")
            self.assertIn("private_address", rejected[0]["validation_note"])

    def test_a_loopback_website_in_the_listing_is_refused_by_the_default_policy(self):
        # This asserts the production default, not the test policy.
        from collector.webfetch import Fetcher, UrlPolicy

        self.seed([raw_record(data_id="bad", web_site="http://127.0.0.1:9/")])
        enricher = Enricher(
            self.cfg,
            fetcher=Fetcher(policy=UrlPolicy(), trust_env_proxy=False),
            resolver=FakeResolver(),
            sleep=lambda _seconds: None,
            polite_delay_seconds=0,
        )
        stats = enricher.enrich_run(self.conn, self.run_id)
        self.assertEqual(stats.boundary_rejections, 1)
        self.assertEqual(stats.pages_fetched, 0)

    def test_an_off_site_redirect_is_recorded_and_does_not_abort_the_run(self):
        pages = {"/": Page(status=302, location="http://example.com/elsewhere")}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", web_site=site.url("/"))])
            stats = self.enrich()
            self.assertEqual(stats.pages_failed, 1)
            kinds = {row["kind"] for row in self.errors()}
            self.assertIn("fetch_offsite_redirect", kinds)


class DnsStateTest(EnrichTestCase):
    def _run_with_resolver(self, resolver):
        pages = {"/": Page(body=site_page(jsonld=DENTIST_JSONLD))}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            return self.enrich(resolver=resolver)

    def test_a_dns_timeout_is_stored_as_its_own_state(self):
        self._run_with_resolver(FakeResolver(mx={"praxis-mueller.de": (DNS_TIMEOUT, ())}))
        row = self.website_facts("emails")[0]
        self.assertEqual(row["validation_state"], "dns_timeout")
        self.assertEqual(row["validation_status"], "uncertain")

    def test_a_null_mx_is_stored_as_its_own_state(self):
        self._run_with_resolver(FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("",))}))
        row = self.website_facts("emails")[0]
        self.assertEqual(row["validation_state"], "null_mx")
        self.assertEqual(row["validation_status"], "invalid")


class ConflictTest(EnrichTestCase):
    def test_a_name_disagreement_is_uncertain_and_does_not_overwrite_the_listing(self):
        jsonld = json.dumps(
            {"@type": "Dentist", "name": "Zahnarztpraxis am Alex", "telephone": "+49 30 99999999"}
        )
        pages = {"/": Page(body=site_page(jsonld=jsonld, title="Zahnarztpraxis am Alex"))}
        with SiteServer(pages) as site:
            self.seed(
                [
                    raw_record(
                        data_id="d1",
                        title="Praxis Dr. Ganz Anders",
                        phone=LISTING_PHONE,
                        web_site=site.url("/"),
                    )
                ]
            )
            stats = self.enrich()

        self.assertGreaterEqual(stats.identity_conflicts, 1)
        conflicts = [
            row for row in self.website_facts()
            if row["validation_state"] == STATE_CONFLICTS_WITH_LISTING
        ]
        self.assertTrue(conflicts)
        for row in conflicts:
            self.assertEqual(row["validation_status"], "uncertain")
            checks = json.loads(row["checks_json"])
            self.assertIn("listing_value", checks)
            self.assertIn("website_value", checks)

        # The listing's own fact is untouched and still the first one written.
        listing_titles = [
            row for row in self.facts("title") if row["provider"] == "google_maps"
        ]
        self.assertEqual(listing_titles[0]["raw_value"], "Praxis Dr. Ganz Anders")

    def test_a_phone_written_two_ways_is_not_a_conflict(self):
        jsonld = json.dumps({"@type": "Dentist", "telephone": "+49 30 12345678"})
        pages = {"/": Page(body=site_page(jsonld=jsonld))}
        with SiteServer(pages) as site:
            # The listing writes the national form; the site writes E.164.
            self.seed(
                [
                    raw_record(
                        data_id="d1",
                        title=LISTING_TITLE,
                        phone="030 12345678",
                        web_site=site.url("/"),
                    )
                ]
            )
            stats = self.enrich()
        self.assertEqual(stats.identity_conflicts, 0)
        self.assertGreaterEqual(stats.confirmations, 1)

    def test_an_address_that_only_omits_the_country_is_not_a_conflict(self):
        jsonld = json.dumps(
            {
                "@type": "Dentist",
                "address": {
                    "streetAddress": "Musterstra\u00dfe 1",
                    "postalCode": "10115",
                    "addressLocality": "Berlin",
                },
            }
        )
        pages = {"/": Page(body=site_page(jsonld=jsonld))}
        with SiteServer(pages) as site:
            self.seed(
                [
                    raw_record(
                        data_id="d1",
                        title=LISTING_TITLE,
                        address=LISTING_ADDRESS,
                        web_site=site.url("/"),
                    )
                ]
            )
            stats = self.enrich()
        self.assertEqual(stats.identity_conflicts, 0)


class SourceConflictTest(EnrichTestCase):
    """The page contradicting itself: the anchor links one value, prints another.

    Field report (045): imovis.de linked ``tel:+4940237751001`` and printed
    ``+49 40 182 11 6600``. Both numbers are well-formed German numbers, so
    format validation of either one says "valid" and the disagreement vanished
    from the export. It must not: they are two different numbers.
    """

    def facts_for(self, field, entity_id=None):
        rows = self.website_facts(field)
        if entity_id is not None:
            rows = [row for row in rows if row["entity_id"] == entity_id]
        return rows

    def test_a_tel_anchor_that_prints_a_different_number_is_uncertain(self):
        body = '<a href="tel:+4940237751001">+49 40 182 11 6600</a>'
        pages = {"/": Page(body=site_page(body=body))}
        with SiteServer(pages) as site:
            self.seed(
                [
                    raw_record(
                        data_id="d1",
                        title="Imovis GmbH",
                        # No listing phone: nothing to compare against, so only
                        # the page's own self-contradiction can be reported.
                        web_site=site.url("/"),
                    )
                ]
            )
            stats = self.enrich()

        self.assertGreaterEqual(stats.source_conflicts, 1)
        phones = self.facts_for("phone")
        self.assertTrue(phones, "the phone must still be recorded, not dropped")
        conflicted = [row for row in phones if row["validation_state"] == STATE_SOURCE_CONFLICT]
        self.assertTrue(conflicted)
        for row in conflicted:
            self.assertEqual(row["validation_status"], "uncertain")
            # The format result is kept, but it no longer decides the verdict.
            checks = json.loads(row["checks_json"])
            self.assertIs(checks["source_conflict"], True)
            self.assertEqual(checks["href_value"], "+4940237751001")
            self.assertEqual(checks["display_text"], "+49 40 182 11 6600")
            self.assertEqual(checks["href_normalized"], "+4940237751001")
            self.assertEqual(checks["display_normalized"], "+4940182116600")
            self.assertNotEqual(checks["href_normalized"], checks["display_normalized"])

    def test_the_same_number_written_two_ways_is_not_a_source_conflict(self):
        body = '<a href="tel:+493012345678">030 12345678</a>'
        pages = {"/": Page(body=site_page(body=body))}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title="Praxis", web_site=site.url("/"))])
            stats = self.enrich()
        self.assertEqual(stats.source_conflicts, 0)
        self.assertFalse(
            [row for row in self.facts_for("phone") if row["validation_state"] == STATE_SOURCE_CONFLICT]
        )

    def test_a_label_is_not_a_competing_value(self):
        for label in ("Anrufen", "Kontakt", "Schreiben Sie uns", "Tel."):
            with self.subTest(label=label):
                body = f'<a href="tel:+493012345678">{label}</a>'
                pages = {"/": Page(body=site_page(body=body))}
                with SiteServer(pages) as site:
                    self.seed([raw_record(data_id="d1", title="Praxis", web_site=site.url("/"))])
                    stats = self.enrich()
                self.assertEqual(stats.source_conflicts, 0)
                phones = [row for row in self.facts_for("phone")]
                self.assertTrue(phones)
                self.assertNotEqual(phones[0]["validation_state"], STATE_SOURCE_CONFLICT)

    def test_a_mailto_that_prints_a_different_address_is_uncertain(self):
        body = '<a href="mailto:vertrieb@imovis.de">info@imovis.de</a>'
        pages = {"/": Page(body=site_page(body=body))}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title="Imovis GmbH", web_site=site.url("/"))])
            stats = self.enrich()
        self.assertGreaterEqual(stats.source_conflicts, 1)
        emails = self.facts_for("emails")
        conflicted = [row for row in emails if row["validation_state"] == STATE_SOURCE_CONFLICT]
        self.assertTrue(conflicted)
        checks = json.loads(conflicted[0]["checks_json"])
        self.assertEqual(checks["href_value"], "vertrieb@imovis.de")
        self.assertEqual(checks["display_normalized"], "info@imovis.de")

    def test_a_mailto_showing_a_label_is_not_a_conflict(self):
        body = '<a href="mailto:vertrieb@imovis.de">Schreiben Sie uns</a>'
        pages = {"/": Page(body=site_page(body=body))}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title="Imovis GmbH", web_site=site.url("/"))])
            stats = self.enrich()
        self.assertEqual(stats.source_conflicts, 0)

    def test_two_sites_do_not_have_their_conflicts_cross_bound(self):
        one = {"/": Page(body=site_page(body='<a href="tel:+4940237751001">+49 40 182 11 6600</a>'))}
        two = {"/": Page(body=site_page(body='<a href="tel:+493012345678">+49 30 9999 8888</a>'))}
        with SiteServer(one) as site_one, SiteServer(two) as site_two:
            self.seed(
                [
                    raw_record(data_id="d1", title="Imovis GmbH", web_site=site_one.url("/")),
                    raw_record(data_id="d2", title="Praxis", web_site=site_two.url("/")),
                ]
            )
            self.enrich()

        by_entity = {}
        for row in self.facts_for("phone"):
            by_entity.setdefault(row["entity_id"], []).append(row)
        self.assertEqual(len(by_entity), 2)
        # Each entity keeps its own pair; an id from one site must never appear
        # on the other entity's fact.
        seen_values = set()
        for rows in by_entity.values():
            for row in rows:
                checks = json.loads(row["checks_json"])
                if not checks.get("source_conflict"):
                    continue
                self.assertNotIn(checks["href_value"], seen_values)
                seen_values.add(checks["href_value"])
        self.assertEqual(
            seen_values, {"+4940237751001", "+493012345678"},
            "each entity's conflicting value must stay on its own fact",
        )


class PageCacheTest(EnrichTestCase):
    """045.3: a page discovery already read is not read again by enrichment."""

    def test_a_page_already_read_this_run_is_not_requested_again(self):
        from collector.webfetch import FetchResult, PageCache

        jsonld = json.dumps({"@type": "Dentist", "telephone": LISTING_PHONE})
        body = site_page(jsonld=jsonld, title=LISTING_TITLE)
        pages = {"/": Page(body=body)}
        with SiteServer(pages) as site:
            self.seed(
                [
                    raw_record(
                        data_id="d1",
                        title=LISTING_TITLE,
                        phone=LISTING_PHONE,
                        web_site=site.url("/"),
                    )
                ]
            )
            cache = PageCache()
            cache.put(
                FetchResult(
                    requested_url=site.url("/"),
                    final_url=site.url("/"),
                    ok=True,
                    status_code=200,
                    body=body,
                )
            )
            stats = self.enrich(page_cache=cache)
            hits = site.hits

        self.assertEqual(hits, [], "the cached page must not be requested again")
        self.assertGreaterEqual(stats.pages_from_cache, 1)
        # The cached body is still parsed: reuse must not cost an answer.
        self.assertTrue(self.website_facts("phone"))


class QueryContextTest(EnrichTestCase):
    """045: a website fact must not inherit the listing's search term.

    The German-company batch carried ``query_context="dentists in Berlin"`` on
    every website fact because the enrichment copied the listing's ``query``
    onto facts the website produced. A website visit has no search term; the
    listing's own facts keep theirs.
    """

    def test_a_website_fact_carries_no_search_query(self):
        jsonld = json.dumps({"@type": "Dentist", "telephone": LISTING_PHONE})
        pages = {"/": Page(body=site_page(jsonld=jsonld))}
        with SiteServer(pages) as site:
            self.seed(
                [
                    raw_record(
                        data_id="d1",
                        title=LISTING_TITLE,
                        phone=LISTING_PHONE,
                        web_site=site.url("/"),
                    )
                ]
            )
            self.enrich()

        website = self.website_facts()
        self.assertTrue(website)
        for row in website:
            self.assertIsNone(
                row["query_context"],
                "a website fact has no search query to carry",
            )
        listing = [row for row in self.facts() if row["provider"] == "google_maps"]
        self.assertTrue(listing)
        for row in listing:
            self.assertEqual(row["query_context"], "dentists in Berlin")


class MultipleLocationTest(EnrichTestCase):
    def test_two_locations_are_both_kept(self):
        jsonld = json.dumps(
            {
                "@type": "Dentist",
                "name": LISTING_TITLE,
                "address": {
                    "streetAddress": "Musterstra\u00dfe 1",
                    "postalCode": "10115",
                    "addressLocality": "Berlin",
                },
                "location": [
                    {
                        "@type": "Place",
                        "name": "Filiale Prenzlauer Berg",
                        "address": {
                            "streetAddress": "Kastanienallee 5",
                            "postalCode": "10435",
                            "addressLocality": "Berlin",
                        },
                    },
                    {
                        "@type": "Place",
                        "name": "Filiale Kreuzberg",
                        "address": {
                            "streetAddress": "Bergmannstra\u00dfe 9",
                            "postalCode": "10961",
                            "addressLocality": "Berlin",
                        },
                    },
                ],
            }
        )
        pages = {"/": Page(body=site_page(jsonld=jsonld))}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            self.enrich()

        raw_values = {row["raw_value"] for row in self.website_facts("address")}
        self.assertIn("Kastanienallee 5, 10435 Berlin", raw_values)
        self.assertIn("Bergmannstra\u00dfe 9, 10961 Berlin", raw_values)

    def test_a_shared_company_site_serves_each_listing_separately(self):
        # Two listings, one site: both are enriched, and neither overwrites the
        # other because facts are attached to the entity, not to the domain.
        pages = {"/": Page(body=site_page(jsonld=DENTIST_JSONLD))}
        with SiteServer(pages) as site:
            self.seed(
                [
                    raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/")),
                    raw_record(data_id="d2", title="Zweite Praxis", web_site=site.url("/")),
                ]
            )
            stats = self.enrich(
                resolver=FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))})
            )
        self.assertEqual(stats.entities_with_website, 2)
        self.assertEqual(stats.sites_visited, 2)


class UnicodeAddressTest(EnrichTestCase):
    def test_a_non_latin_address_is_stored_verbatim(self):
        cjk = "\u5317\u4eac\u5e02\u671d\u9633\u533a\u5efa\u56fd\u8def88\u53f7"
        jsonld = json.dumps(
            {
                "@type": "Dentist",
                "name": LISTING_TITLE,
                "address": {"@type": "PostalAddress", "streetAddress": cjk, "addressCountry": "CN"},
            }
        )
        pages = {"/": Page(body=site_page(jsonld=jsonld))}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            self.enrich()
        row = self.website_facts("address")[0]
        self.assertIn(cjk, row["raw_value"])
        checks = json.loads(row["checks_json"])
        self.assertTrue(checks["unicode_ok"])
        self.assertIn("han", checks["scripts"])


class FailureIsolationTest(EnrichTestCase):
    def test_a_broken_page_and_broken_jsonld_do_not_lose_the_batch(self):
        pages = {
            "/": Page(
                body=site_page(
                    jsonld="{ this is not json }",
                    body='<a href="/kontakt">Kontakt</a><a href="/standorte">Standorte</a>'
                    '<a href="mailto:a@praxis-mueller.de">Mail</a>',
                )
            ),
            "/kontakt": Page(status=500, body="boom"),
            "/standorte": Page(body=site_page(jsonld=DENTIST_JSONLD)),
        }
        with SiteServer(pages) as site:
            self.seed(
                [
                    raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/")),
                    raw_record(data_id="d2", title="Zweite Praxis", web_site="http://10.0.0.1/"),
                ]
            )
            stats = self.enrich(
                resolver=FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))}),
                allow_private=False,
            )

        self.assertEqual(stats.pages_failed, 1)
        kinds = {row["kind"] for row in self.errors()}
        self.assertIn("jsonld_parse_error", kinds)
        self.assertIn("fetch_http_error", kinds)
        # Facts were still produced from the pages that did work.
        self.assertTrue(self.website_facts("emails"))
        self.assertTrue(self.website_facts("address"))

    def test_an_unreachable_site_records_an_error_without_raising(self):
        # Port 9 (discard) on loopback: reachable per policy, nothing listening.
        self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site="http://127.0.0.1:9/")])
        enricher = Enricher(
            self.cfg,
            fetcher=local_fetcher(timeout_seconds=2),
            resolver=FakeResolver(),
            sleep=lambda _seconds: None,
            polite_delay_seconds=0,
        )
        stats = enricher.enrich_run(self.conn, self.run_id)
        self.assertEqual(stats.pages_failed, 1)
        self.assertTrue(self.errors())


class BudgetTest(EnrichTestCase):
    def test_pages_per_site_caps_the_visit(self):
        pages = {
            "/": Page(body=site_page(body=HOME_LINKS)),
            "/kontakt": Page(body=site_page(body="<p>k</p>")),
            "/standorte": Page(body=site_page(body="<p>s</p>")),
            "/impressum": Page(body=site_page(body="<p>i</p>")),
        }
        cfg = enrich_config({"enrich": {"pages_per_site": 1}})
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            stats = self.enrich(cfg=cfg)
        self.assertEqual(stats.pages_fetched, 1)
        self.assertEqual(site.hits, ["/"])

    def test_the_request_budget_stops_further_sites(self):
        pages = {"/": Page(body=site_page(jsonld=DENTIST_JSONLD))}
        cfg = enrich_config({"enrich": {"pages_per_site": 1, "budget": {"max_requests": 1}}})
        with SiteServer(pages) as site:
            self.seed(
                [
                    raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/")),
                    raw_record(data_id="d2", title="Zweite Praxis", web_site=site.url("/")),
                ]
            )
            stats = self.enrich(cfg=cfg)
        self.assertEqual(stats.pages_fetched, 1)
        self.assertEqual(stats.pages_skipped_budget, 1)
        self.assertIn("request budget exhausted", stats.budget_reason)

    def test_a_second_pass_adds_nothing_because_of_the_checkpoint(self):
        pages = {"/": Page(body=site_page(jsonld=DENTIST_JSONLD))}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            resolver = FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))})
            first = self.enrich(resolver=resolver)
            facts_after_first = len(self.facts())
            second = self.enrich(resolver=resolver)
        self.assertGreater(first.facts_written, 0)
        self.assertEqual(second.facts_written, 0)
        self.assertEqual(len(self.facts()), facts_after_first)


class FieldGatingTest(EnrichTestCase):
    def test_a_field_that_was_not_requested_is_not_stored(self):
        cfg = enrich_config({"enrich": {"fields": ["title"], "pages_per_site": 1}})
        pages = {"/": Page(body=site_page(jsonld=DENTIST_JSONLD))}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            self.enrich(cfg=cfg, resolver=FakeResolver(mx={"praxis-mueller.de": (DNS_OK, ("mx.mx",))}))
        self.assertEqual(self.website_facts("emails"), [])
        self.assertEqual(self.website_facts("phone"), [])
        self.assertTrue(self.website_facts("title"))

    def test_a_conflict_is_still_recorded_for_a_field_that_was_not_requested(self):
        # The conflict is a data-quality signal about a field the user collects,
        # so it is recorded even when the field is not part of enrich.fields.
        cfg = enrich_config({"enrich": {"fields": ["emails"], "pages_per_site": 1}})
        jsonld = json.dumps({"@type": "Dentist", "name": "Voellig anderer Name"})
        pages = {"/": Page(body=site_page(jsonld=jsonld))}
        with SiteServer(pages) as site:
            self.seed([raw_record(data_id="d1", title=LISTING_TITLE, web_site=site.url("/"))])
            stats = self.enrich(cfg=cfg)
        self.assertEqual(stats.identity_conflicts, 1)
        conflicts = [
            row for row in self.website_facts("title")
            if row["validation_state"] == STATE_CONFLICTS_WITH_LISTING
        ]
        self.assertEqual(len(conflicts), 1)


class NoWebsiteTest(EnrichTestCase):
    def test_an_entity_without_a_website_is_left_alone(self):
        self.seed([raw_record(data_id="d1", web_site=None)])
        stats = self.enrich()
        self.assertEqual(stats.entities_considered, 1)
        self.assertEqual(stats.entities_with_website, 0)
        self.assertEqual(stats.pages_fetched, 0)


if __name__ == "__main__":
    unittest.main()
