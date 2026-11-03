"""Round 026 P2.1: the offline saved-page importer.

What these tests pin down is the honesty of the import, not that HTML parsing
works (the selector engine has its own tests):

* a row read from a saved file is stamped ``acquired_via="import"`` and never
  ``live`` — even if someone writes ``live`` into the file it came from;
* a detail link with no saved page becomes a listing-only row whose provenance
  says so, rather than a row that looks verified;
* no capture time is invented: ``observed_at`` is the operator's statement or
  absent, never the file's mtime;
* a page whose declared encoding is a lie is still read, and which encoding was
  used is recorded;
* nothing is fetched, so a run over saved pages touches no network at all.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from tests import support

from collector import db
from collector.errors import ConfigError
from collector.sources.localpages import LocalPagesProvider

LISTING_ORIGIN = "https://www.11467.com/hangzhou/search/9092.htm"


def listing_page(*links: tuple[str, str]) -> str:
    anchors = "".join(f'<a href="{href}">{text}</a>' for href, text in links)
    return f"<html><body>{anchors}</body></html>"


def detail_page(name: str, *, address: str = "", phone: str = "", about: str = "") -> str:
    contact = ""
    if address or phone:
        contact = (
            '<div id="contact"><dl class="codl">'
            f"<dd>{address}</dd><dd>{phone}</dd>"
            "</dl></div>"
        )
    return (
        f"<html><body><h1>{name}</h1>{contact}"
        f'<div id="aboutuscontent">{about}</div></body></html>'
    )


class LocalPagesProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = support.temp_dir(self)

    # -- configuration -----------------------------------------------------

    def test_it_is_not_configured_without_an_entry(self) -> None:
        cfg = support.config({"sources": ["local_pages"]})
        provider = LocalPagesProvider(cfg)
        self.assertFalse(provider.is_configured())
        self.assertIn("listing_path", provider.not_configured_reason())

    def test_a_listing_path_without_an_origin_url_is_refused(self) -> None:
        # The origin URL is what makes a relative href resolvable and the
        # evidence citable. Refused at validation, not at run time.
        with self.assertRaises(ConfigError) as caught:
            support.config(
                {
                    "sources": ["local_pages"],
                    "sources_map": {
                        "local_pages": {
                            "entries": [{"listing_path": str(self.tmp / "x.html")}]
                        }
                    },
                }
            )
        self.assertIn("origin_url", str(caught.exception))

    def test_an_origin_url_that_is_not_a_url_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            support.config(
                {
                    "sources": ["local_pages"],
                    "sources_map": {
                        "local_pages": {
                            "entries": [
                                {"listing_path": "x.html", "origin_url": "www.11467.com"}
                            ]
                        }
                    },
                }
            )
        self.assertIn("http", str(caught.exception))

    def test_an_unknown_entry_key_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            support.config(
                {
                    "sources": ["local_pages"],
                    "sources_map": {
                        "local_pages": {
                            "entries": [
                                {
                                    "listing_path": "x.html",
                                    "origin_url": LISTING_ORIGIN,
                                    "start_url": "https://example.com",
                                }
                            ]
                        }
                    },
                }
            )
        self.assertIn("start_url", str(caught.exception))

    def test_it_has_no_network_setting(self) -> None:
        # Offering a proxy for a provider that makes no request would be the
        # "configured but does nothing" failure.
        with self.assertRaises(ConfigError) as caught:
            support.config(
                {
                    "sources": ["local_pages"],
                    "sources_map": {
                        "local_pages": {
                            "network": {"mode": "direct"},
                            "entries": [
                                {"listing_path": "x.html", "origin_url": LISTING_ORIGIN}
                            ],
                        }
                    },
                }
            )
        self.assertIn("network", str(caught.exception))

    def test_the_note_names_a_listing_path_that_is_not_there(self) -> None:
        cfg = support.local_pages_config(self.tmp / "missing.html")
        note = support.local_pages_provider(cfg).configuration_note()
        self.assertIn("不存在", note)

    def test_a_complete_configuration_has_no_note(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html", listing_page(("/qiye/1.htm", "甲公司"))
        )
        cfg = support.local_pages_config(listing)
        self.assertEqual(support.local_pages_provider(cfg).configuration_note(), "")

    def test_settings_come_from_sources_map_only(self) -> None:
        # A run that lists the source but has no settings must report the
        # reason, not silently produce an empty file.
        cfg = support.config({"sources": ["local_pages"]})
        self.assertEqual(support.local_pages_provider(cfg).entries(), [])


class LocalPagesRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = support.temp_dir(self)
        self.runs = self.tmp / "runs"

    def _run(self, cfg):
        from collector.orchestrator import Orchestrator

        return Orchestrator(cfg, base_dir=self.tmp).run(resume=False)

    def _envelopes(self, result) -> list[dict]:
        path = pathlib.Path(result.run_dir) / "raw" / "import-pages-offline-a1.jsonl"
        return support.read_raw_envelopes(path)

    # -- reading saved pages ------------------------------------------------

    def test_a_detail_page_that_was_saved_is_read_with_the_presets(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html",
            listing_page(("/qiye/101.htm", "杭州萌宠用品有限公司")),
        )
        details = self.tmp / "detail"
        support.write_saved_page(
            details / "101.htm",
            detail_page(
                "杭州萌宠用品有限公司",
                address="杭州市西湖区文一路 1 号",
                phone="0571-88886666",
                about="主要经营宠物食品、宠物用品",
            ),
        )
        cfg = support.local_pages_config(listing, details_dir=details)
        result = self._run(cfg)
        self.assertEqual(result.collected, 1)

        envelope = self._envelopes(result)[0]
        self.assertEqual(envelope["context"]["acquired_via"], "import")
        self.assertTrue(envelope["context"]["detail_fetched"])
        self.assertEqual(envelope["raw"]["address"], "杭州市西湖区文一路 1 号")
        self.assertEqual(envelope["raw"]["phone"], "0571-88886666")
        self.assertIn("宠物食品", envelope["raw"]["description"])
        self.assertTrue(envelope["context"]["snapshot_id"].startswith("101.htm:"))

    def test_a_detail_page_that_was_not_saved_yields_a_listing_only_row(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html",
            listing_page(("/qiye/101.htm", "杭州萌宠用品有限公司")),
        )
        cfg = support.local_pages_config(listing, details_dir=self.tmp / "empty")
        result = self._run(cfg)

        envelope = self._envelopes(result)[0]
        self.assertEqual(envelope["raw"]["title"], "杭州萌宠用品有限公司")
        self.assertEqual(envelope["context"]["acquired_via"], "import")
        # The page was *not* read, so the flag that means "we visited it" is
        # absent. A reader must not be able to mistake this for a verified row.
        self.assertNotIn("detail_fetched", envelope["context"])
        self.assertEqual(envelope["context"]["source_page_url"], LISTING_ORIGIN)
        self.assertEqual(envelope["context"]["detail_url"], "https://www.11467.com/qiye/101.htm")
        self.assertTrue(envelope["raw"]["extra"]["listing_only"])
        self.assertIsNone(envelope["raw"].get("address"))

    def test_a_link_that_leaves_the_listing_host_is_not_a_candidate(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html",
            listing_page(
                ("/qiye/101.htm", "甲公司"),
                ("https://other.example.com/qiye/999.htm", "外部公司"),
            ),
        )
        cfg = support.local_pages_config(listing)
        self._run(cfg)
        titles = [e["raw"]["title"] for e in self._envelopes(self._run(cfg))]
        self.assertEqual(titles, ["甲公司"])

    def test_a_link_that_does_not_match_the_pattern_is_ignored(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html",
            listing_page(
                ("/qiye/101.htm", "甲公司"),
                ("/about.html", "关于我们"),
            ),
        )
        cfg = support.local_pages_config(listing)
        titles = [e["raw"]["title"] for e in self._envelopes(self._run(cfg))]
        self.assertEqual(titles, ["甲公司"])

    def test_an_explicit_url_to_path_map_is_honoured(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html",
            listing_page(("/qiye/101.htm", "甲公司")),
        )
        odd = support.write_saved_page(
            self.tmp / "somewhere-else" / "saved-page.html",
            detail_page("甲公司", phone="0571-11112222"),
        )
        cfg = support.local_pages_config(
            listing,
            details=[
                {"url": "https://www.11467.com/qiye/101.htm", "path": str(odd)}
            ],
        )
        envelope = self._envelopes(self._run(cfg))[0]
        self.assertEqual(envelope["raw"]["phone"], "0571-11112222")
        self.assertTrue(envelope["context"]["snapshot_id"].startswith("saved-page.html:"))

    def test_the_detail_folder_matches_on_the_url_last_segment(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html", listing_page(("/qiye/202.htm", "甲公司"))
        )
        details = self.tmp / "detail"
        # "Save page as" commonly writes ``202.html`` for ``202.htm``.
        support.write_saved_page(details / "202.html", detail_page("甲公司", phone="0571-33334444"))
        cfg = support.local_pages_config(listing, details_dir=details)
        self.assertEqual(self._envelopes(self._run(cfg))[0]["raw"]["phone"], "0571-33334444")

    # -- provenance ---------------------------------------------------------

    def test_the_capture_time_is_the_operators_statement_or_absent(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html", listing_page(("/qiye/101.htm", "甲公司"))
        )
        without = self._envelopes(self._run(support.local_pages_config(listing)))[0]
        self.assertNotIn("observed_at", without["context"])

        stated = self._envelopes(
            self._run(
                support.local_pages_config(listing, observed_at="2026-10-01T09:00:00+08:00")
            )
        )[0]
        self.assertEqual(stated["context"]["observed_at"], "2026-10-01T09:00:00+08:00")

    def test_the_file_mtime_is_never_used_as_the_capture_time(self) -> None:
        # The mtime is when the operator downloaded the page, not when the page
        # was published. Set it to something unmistakable and check it is absent.
        listing = support.write_saved_page(
            self.tmp / "listing.html", listing_page(("/qiye/101.htm", "甲公司"))
        )
        import os
        import time as _time

        stamp = _time.mktime((2019, 1, 2, 3, 4, 5, 0, 0, -1))
        os.utime(listing, (stamp, stamp))
        envelope = self._envelopes(self._run(support.local_pages_config(listing)))[0]
        self.assertNotIn("observed_at", envelope["context"])
        self.assertNotIn("2019", json.dumps(envelope, ensure_ascii=False))

    def test_the_envelope_context_says_the_run_imported_a_file(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html", listing_page(("/qiye/101.htm", "甲公司"))
        )
        envelope = self._envelopes(self._run(support.local_pages_config(listing)))[0]
        self.assertTrue(envelope["context"]["imported"])
        self.assertEqual(envelope["context"]["mode"], "local_pages")
        self.assertEqual(envelope["context"]["source_url"], LISTING_ORIGIN)
        self.assertEqual(envelope["context"]["origin"], "www.11467.com")

    def test_a_row_cannot_claim_to_have_been_fetched_live(self) -> None:
        # Even if a saved page is labelled with a live provenance block, the
        # importer's output stays ``import``.
        from collector.sources.base import SourceRecord, write_envelopes

        record = SourceRecord(title="甲公司", acquired_via="live")
        path = self.tmp / "forced.jsonl"
        write_envelopes(path, "local_pages", [record])
        envelope = support.read_raw_envelopes(path)[0]
        self.assertEqual(envelope["context"]["acquired_via"], "import")

    def test_a_saved_page_states_which_encoding_was_used(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html",
            listing_page(("/qiye/101.htm", "杭州甲公司")),
            encoding="gb18030",
        )
        envelope = self._envelopes(self._run(support.local_pages_config(listing)))[0]
        self.assertEqual(envelope["context"]["listing_encoding"], "gb18030")
        self.assertEqual(envelope["raw"]["title"], "杭州甲公司")

    def test_a_page_declaring_utf8_but_saved_as_gb18030_is_still_read(self) -> None:
        html = (
            '<html><head><meta charset="utf-8"></head><body>'
            '<a href="/qiye/101.htm">\u676d\u5dde\u5e02\u5b89\u535a\u516c\u53f8</a>'
            "</body></html>"
        )
        listing = support.write_saved_page(
            self.tmp / "listing.html", html, encoding="gb18030"
        )
        envelope = self._envelopes(self._run(support.local_pages_config(listing)))[0]
        self.assertEqual(envelope["raw"]["title"], "杭州市安博公司")

    # -- no network ----------------------------------------------------------

    def test_the_run_makes_no_request_at_all(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html",
            listing_page(("/qiye/101.htm", "甲公司"), ("/qiye/102.htm", "乙公司")),
        )
        # A fetcher that raises if it is ever used: the importer must not need
        # one for anything, not even to resolve a link.
        from collector.orchestrator import Orchestrator

        class ExplodingFetcher:
            def fetch(self, *_args, **_kwargs):
                raise AssertionError("local_pages must not fetch anything")

        orch = Orchestrator(support.local_pages_config(listing), base_dir=self.tmp)
        ctx = orch._run_context(1, self.runs / "1")
        ctx.fetcher = ExplodingFetcher()  # type: ignore[assignment]
        outcome = support.local_pages_provider(orch.cfg).run_task(
            support.local_pages_provider(orch.cfg).tasks()[0], ctx
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 2)

    # -- failing loudly ------------------------------------------------------

    def test_a_missing_listing_file_fails_with_the_path(self) -> None:
        cfg = support.local_pages_config(self.tmp / "gone.html")
        provider = support.local_pages_provider(cfg)
        from collector.orchestrator import Orchestrator

        orch = Orchestrator(cfg, base_dir=self.tmp)
        ctx = orch._run_context(1, self.runs / "1")
        outcome = provider.run_task(provider.tasks()[0], ctx)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "input_missing")
        self.assertIn("gone.html", outcome.error)

    def test_a_link_with_no_text_and_no_saved_page_writes_nothing(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html",
            listing_page(("/qiye/101.htm", "   "), ("/qiye/102.htm", "乙公司")),
        )
        cfg = support.local_pages_config(listing)
        envelope = self._envelopes(self._run(cfg))
        self.assertEqual([e["raw"]["title"] for e in envelope], ["乙公司"])

    def test_max_detail_pages_bounds_how_many_links_are_read(self) -> None:
        links = [(f"/qiye/{n}.htm", f"公司{n}") for n in range(1, 6)]
        listing = support.write_saved_page(self.tmp / "listing.html", listing_page(*links))
        cfg = support.local_pages_config(listing, max_detail_pages=2)
        self.assertEqual(len(self._envelopes(self._run(cfg))), 2)


#: The entry knobs the 顺企网 preset writes (collector/local_ui.py), stated here
#: so a test reads as "the shipped preset" rather than re-deriving the regexes.
ROW_ENTRY: dict = {
    "detail_url_pattern": r"^https?://[^/]+\.b2b\.11467\.com(?:/|$)?|/qiye/\d+\.htm",
    "detail_host_scope": "registrable_domain",
    "list_selector": "ul.companylist",
    "list_preset": "shunqiwang",
}


class ListingRowFieldsTest(unittest.TestCase):
    """Round 034: what the listing page itself states must survive the import.

    Every number here came from a local audit of the 顺企网 sample, run during
    development: 100 rows on the page, 98 of
    them under ``/qiye/``, 2 under ``*.b2b.11467.com``, and 1 whose address the
    page leaves as a dash. Before this batch, 1 of 100 rows kept its address.
    """

    def setUp(self) -> None:
        self.tmp = support.temp_dir(self)

    def _run(self, cfg):
        from collector.orchestrator import Orchestrator

        return Orchestrator(cfg, base_dir=self.tmp).run(resume=False)

    def _envelopes(self, result) -> list[dict]:
        path = pathlib.Path(result.run_dir) / "raw" / "import-pages-offline-a1.jsonl"
        return support.read_raw_envelopes(path)

    @staticmethod
    def listing_html(*rows: str) -> str:
        return (
            "<html><body><ul class='companylist'>"
            + "".join(f"<li>{row}</li>" for row in rows)
            + "</ul></body></html>"
        )

    @staticmethod
    def row(name: str, href: str, *, address: str, products: str) -> str:
        return (
            f'<div class="f_l"><h4><a href="{href}">{name}</a></h4>'
            f"<div>主营产品： {products}</div>"
            f"<div>地址： {address}</div></div>"
        )

    def _config(self, listing, **overrides):
        return support.local_pages_config(listing, entry_extra=ROW_ENTRY, **overrides)

    def test_a_row_whose_detail_page_was_never_saved_keeps_its_address(self) -> None:
        # The measured failure: this row used to arrive with a name and nothing
        # else, because only the anchor's text was read.
        listing = support.write_saved_page(
            self.tmp / "listing.html",
            self.listing_html(self.row("甲宠物店", "/qiye/101.htm",
                                       address="杭州市西湖区文一路 1 号",
                                       products="宠物美容")),
        )
        result = self._run(self._config(listing))
        envelope = self._envelopes(result)[0]
        self.assertEqual(envelope["raw"]["address"], "杭州市西湖区文一路 1 号")
        self.assertEqual(envelope["raw"]["description"], "宠物美容")
        # Still a listing-only row: the address came from the listing page and
        # the row must not claim its own page was read.
        self.assertNotIn("detail_fetched", envelope["context"])
        self.assertEqual(envelope["context"]["source_page_url"], LISTING_ORIGIN)

    def test_the_row_says_where_each_field_came_from(self) -> None:
        listing = support.write_saved_page(
            self.tmp / "listing.html",
            self.listing_html(self.row("甲宠物店", "/qiye/101.htm",
                                       address="杭州市西湖区文一路 1 号",
                                       products="宠物美容")),
        )
        envelope = self._envelopes(self._run(self._config(listing)))[0]
        sources = envelope["raw"]["native"]["field_sources"]
        self.assertEqual(sources["address"], "listing")
        self.assertEqual(sources["description"], "listing")

    def test_a_field_the_record_model_does_not_have_travels_with_the_row(self) -> None:
        # 成立时间 is not a record field; dropping it because of that would lose
        # part of what the page stated.
        html = self.listing_html(
            '<div class="f_l"><h4><a href="/qiye/101.htm">甲宠物店</a></h4>'
            "<div>地址： 文一路 1 号</div><span>成立时间：2021-05-17</span></div>"
        )
        listing = support.write_saved_page(self.tmp / "listing.html", html)
        envelope = self._envelopes(self._run(self._config(listing)))[0]
        self.assertEqual(
            envelope["raw"]["native"]["listing_fields"]["values"]["est_date"], "2021-05-17"
        )

    def test_a_dash_is_not_stored_as_an_address(self) -> None:
        html = self.listing_html(self.row("甲宠物店", "/qiye/101.htm",
                                          address="-", products="宠物美容"))
        listing = support.write_saved_page(self.tmp / "listing.html", html)
        envelope = self._envelopes(self._run(self._config(listing)))[0]
        self.assertIsNone(envelope["raw"].get("address"))
        # Said, not silently dropped: "the page left it blank" differs from
        # "we did not look".
        self.assertIn("address", envelope["raw"]["native"]["listing_fields"]["blank"])

    def test_a_business_hosted_on_a_subdomain_is_still_in_the_list(self) -> None:
        # 2 of the sample page's 100 rows link to https://<id>.b2b.11467.com,
        # whose path is "/". The exact-host rule dropped them.
        html = self.listing_html(
            self.row("甲宠物店", "/qiye/101.htm", address="文一路 1 号", products="宠物美容"),
            self.row("乙宠物店", "https://118560280.b2b.11467.com",
                     address="萧山区蜀山街道晨晖路 148 号", products="宠物无害化处理"),
        )
        listing = support.write_saved_page(self.tmp / "listing.html", html)
        result = self._run(self._config(listing))
        self.assertEqual(result.collected, 2)
        by_name = {e["raw"]["title"]: e["raw"].get("address") for e in self._envelopes(result)}
        self.assertEqual(by_name["乙宠物店"], "萧山区蜀山街道晨晖路 148 号")

    def test_the_pages_furniture_does_not_become_a_business(self) -> None:
        # Widening the host rule picks up 6 sub-domain links on the sample page
        # while only 2 are in the list; the other 4 are recommendations.
        # ``list_selector`` is what keeps them out.
        html = (
            "<html><body><ul class='companylist'><li>"
            + self.row("甲宠物店", "/qiye/101.htm", address="文一路 1 号", products="宠物美容")
            + "</li></ul><div class='side'><a href='https://95569003.b2b.11467.com/'>"
            "德国巴斯夫代理商</a></div></body></html>"
        )
        listing = support.write_saved_page(self.tmp / "listing.html", html)
        result = self._run(self._config(listing))
        titles = [e["raw"]["title"] for e in self._envelopes(result)]
        self.assertEqual(titles, ["甲宠物店"])

    def test_a_detail_pages_absence_does_not_erase_what_the_listing_said(self) -> None:
        # The saved detail page has no address selector match; the listing row
        # does. The row must end up with the listing's address, not with None.
        html = self.listing_html(self.row("甲宠物店", "/qiye/101.htm",
                                          address="杭州市西湖区文一路 1 号",
                                          products="宠物美容"))
        listing = support.write_saved_page(self.tmp / "listing.html", html)
        details = self.tmp / "detail"
        support.write_saved_page(details / "101.htm", detail_page("甲宠物店", about="宠物食品"))
        cfg = self._config(listing, details_dir=details)
        envelope = self._envelopes(self._run(cfg))[0]
        self.assertEqual(envelope["raw"]["address"], "杭州市西湖区文一路 1 号")
        self.assertEqual(envelope["raw"]["native"]["field_sources"]["address"], "listing")

    def test_a_detail_value_wins_and_the_listings_is_kept_when_they_differ(self) -> None:
        html = self.listing_html(self.row("甲宠物店", "/qiye/101.htm",
                                          address="列表页写的地址", products="宠物美容"))
        listing = support.write_saved_page(self.tmp / "listing.html", html)
        details = self.tmp / "detail"
        support.write_saved_page(
            details / "101.htm", detail_page("甲宠物店", address="详情页写的地址")
        )
        cfg = self._config(listing, details_dir=details)
        envelope = self._envelopes(self._run(cfg))[0]
        self.assertEqual(envelope["raw"]["address"], "详情页写的地址")
        conflict = envelope["raw"]["native"]["field_conflicts"]["address"]
        self.assertEqual(conflict["listing"], "列表页写的地址")

    def test_the_row_reading_can_be_turned_off_for_one_entry(self) -> None:
        html = self.listing_html(self.row("甲宠物店", "/qiye/101.htm",
                                          address="文一路 1 号", products="宠物美容"))
        listing = support.write_saved_page(self.tmp / "listing.html", html)
        cfg = support.local_pages_config(
            listing, entry_extra={"read_listing_fields": False}
        )
        envelope = self._envelopes(self._run(cfg))[0]
        self.assertIsNone(envelope["raw"].get("address"))

    def test_the_run_reports_how_many_rows_carried_fields(self) -> None:
        # 0 on a page that plainly has rows means the label set is wrong — a
        # config problem, not an empty page. Reported so it is visible.
        html = self.listing_html(self.row("甲宠物店", "/qiye/101.htm",
                                          address="文一路 1 号", products="宠物美容"))
        listing = support.write_saved_page(self.tmp / "listing.html", html)
        result = self._run(self._config(listing))
        import json

        path = pathlib.Path(result.run_dir) / "raw" / "import-pages-offline-a1.jsonl"
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(lines[0]["context"]["listing_rows_with_fields"], 1)


class LocalPagesStageWiringTest(unittest.TestCase):
    def test_the_provider_is_planned_and_has_no_account_requirement(self) -> None:
        from collector.sources import provider_names

        self.assertIn("local_pages", provider_names())

    def test_its_records_are_reachable_through_the_database(self) -> None:
        tmp = support.temp_dir(self)
        listing = support.write_saved_page(
            tmp / "listing.html",
            listing_page(("/qiye/101.htm", "杭州安博宠物用品有限公司")),
        )
        cfg = support.local_pages_config(listing)
        from collector.orchestrator import Orchestrator

        result = Orchestrator(cfg, base_dir=tmp).run(resume=False)
        conn = db.connect(pathlib.Path(result.database))
        try:
            names = [
                row["name"]
                for row in conn.execute(
                    "SELECT DISTINCT e.name FROM entities e "
                    "JOIN entity_observations eo ON eo.entity_id = e.id "
                    "WHERE eo.run_id = ?",
                    (result.run_id,),
                )
            ]
        finally:
            conn.close()
        self.assertIn("杭州安博宠物用品有限公司", names)

    def test_the_import_source_is_named_in_the_export_manifest(self) -> None:
        from collector.sources import manifest

        tmp = support.temp_dir(self)
        listing = support.write_saved_page(
            tmp / "listing.html", listing_page(("/qiye/101.htm", "甲公司"))
        )
        entries = {entry["name"]: entry for entry in manifest(support.local_pages_config(listing))}
        entry = entries["local_pages"]
        self.assertTrue(entry["planned"])
        self.assertFalse(entry["requires_account"])
        self.assertEqual(entry["origin"], "www.11467.com")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
