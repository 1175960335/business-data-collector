"""CSV / JSONL export."""

from __future__ import annotations

import csv
import json
import unittest

from collector import db, export, ingest, scoring

from . import support

EXPORT_FIELDS = [
    "entity_id",
    "stable_key",
    "title",
    "address",
    "phone",
    "website",
    "latitude",
    "longitude",
    "provider",
    "query",
    "source_url",
    "first_seen_at",
    "last_seen_at",
]


class EvidenceProvenanceTest(unittest.TestCase):
    """Round 026 P0: evidence is the page the facts were read from.

    The 024 code scraped an http(s) URL out of the record's stable key and cited
    it as the page the facts came from. For a csv_list run that pointed at a
    company detail page the run had never opened — a claim of a visit that never
    happened. These tests pin the corrected behaviour.
    """

    PROVENANCE = {
        "provider": "csv_list",
        "mode": "csv_file",
        "query": "11467-hangzhou-pet",
        "source_url": "/local/11467-hangzhou-pet-listing.csv",
        "input_path": "/local/11467-hangzhou-pet-listing.csv",
        "acquired_via": "import",
        "origin": "www.11467.com",
        "source_page_url": "https://www.11467.com/hangzhou/search/9092.htm",
        "snapshot_id": "11467-hangzhou-pet-listing.html@8636cd8f",
        "observed_at": "2026-10-13T21:11:21+08:00",
    }

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
            target_count=10,
        )
        self.raw_dir = self.tmp / "raw"
        self.raw_dir.mkdir()
        self.cfg = support.config(
            {
                "sources": ["csv_list"],
                "collect": {
                    "fields": ["title", "address", "description", "category", "phone", "website"]
                },
                "match": {
                    "region": {"province": "浙江省", "city": "杭州市"},
                    "industry": {"profile": "pet_related"},
                },
                "export": {"fields": ["entity_id", "stable_key", "title"], "formats": ["xlsx"]},
            }
        )

    def ingest_record(self, raw, context=None):
        envelope = {
            "schema": "osint.record/1",
            "provider": "csv_list",
            "context": {**self.PROVENANCE, **(context or {})},
            "raw": raw,
        }
        path = self.raw_dir / "listing-a1.jsonl"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        return ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=self.cfg)

    def only_evidence(self):
        _criteria, _matches, evidence = export.match_map(self.conn, self.run_id, self.cfg)
        self.assertEqual(len(evidence), 1)
        return next(iter(evidence.values()))

    def test_the_evidence_is_the_page_read_not_the_link_in_the_identifier(self):
        # The identifier *is* a detail-page URL, but the row's facts came from
        # the listing page. The listing page is the evidence, and nothing is
        # derived from the identifier: a URL that merely appears in an id is not
        # proof the page exists or was opened.
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "description": "宠物服务；宠物食品及用品零售。",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            }
        )
        evidence = self.only_evidence()
        self.assertEqual(
            evidence.source_url, "https://www.11467.com/hangzhou/search/9092.htm"
        )
        self.assertEqual(evidence.detail_url, "")
        self.assertFalse(evidence.detail_fetched)
        self.assertEqual(evidence.acquired_via, "import")
        self.assertEqual(evidence.origin, "www.11467.com")

    def test_a_declared_detail_link_is_reported_as_listed_but_unvisited(self):
        # When the provider *does* record the entity's own page, it is kept as a
        # lead and clearly labelled unvisited -- it is still not the evidence.
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "description": "宠物服务；宠物食品及用品零售。",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            },
            context={"detail_url": "https://www.11467.com/qiye/209193961.htm"},
        )
        evidence = self.only_evidence()
        self.assertEqual(
            evidence.source_url, "https://www.11467.com/hangzhou/search/9092.htm"
        )
        self.assertEqual(evidence.detail_url, "https://www.11467.com/qiye/209193961.htm")
        self.assertFalse(evidence.detail_fetched)

    def test_the_reader_cell_says_how_the_row_was_obtained(self):
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "description": "宠物服务；宠物食品及用品零售。",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            }
        )
        rows = export.reader_rows(
            self.conn,
            self.run_id,
            self.cfg,
            "zh",
            matches=export.match_map(self.conn, self.run_id, self.cfg)[1],
            record_evidence=export.match_map(self.conn, self.run_id, self.cfg)[2],
        )
        cell = rows[0]["match_evidence"]
        self.assertIn("https://www.11467.com/hangzhou/search/9092.htm", cell)
        self.assertIn("离线导入", cell)
        self.assertIn("未访问", cell)
        # The unvisited detail page must never be presented as the source.
        self.assertNotIn("（来源：https://www.11467.com/qiye/209193961.htm）", cell)

    def test_the_storage_time_never_poses_as_the_collection_time(self):
        # 034 第一批第 3 条：以前 scraped_at 空着时兜底用 first_seen_at，于是"今晚
        # 导入"被写成"今晚采集"。来源没给时间就必须说没给，不能拿入库时间填。
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            },
            context={"observed_at": None, "scraped_at": None},
        )
        rows = export.reader_rows(
            self.conn,
            self.run_id,
            self.cfg,
            "zh",
            matches=export.match_map(self.conn, self.run_id, self.cfg)[1],
            record_evidence=export.match_map(self.conn, self.run_id, self.cfg)[2],
        )
        self.assertIn("未给出时间", rows[0]["scraped_at"])

    def test_a_stated_source_time_is_shown_as_the_source_time(self):
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            },
            context={"observed_at": "2026-10-13", "scraped_at": None},
        )
        rows = export.reader_rows(
            self.conn,
            self.run_id,
            self.cfg,
            "zh",
            matches=export.match_map(self.conn, self.run_id, self.cfg)[1],
            record_evidence=export.match_map(self.conn, self.run_id, self.cfg)[2],
        )
        self.assertIn("2026-10-13", rows[0]["scraped_at"])
        self.assertIn("日期", rows[0]["scraped_at"])

    def test_a_purely_imported_row_does_not_show_the_import_moment(self):
        # 034 第一批第 3 条：026–033 写的导入行都带着一个 scraped_at（写信封时盖的
        # "现在"）。那是导入时刻，不是采集时刻。库里的值不动，但导出不把它当采集时间读。
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            },
            context={"observed_at": None, "scraped_at": "2026-10-14T18:00:00Z"},
        )
        rows = export.reader_rows(
            self.conn,
            self.run_id,
            self.cfg,
            "zh",
            matches=export.match_map(self.conn, self.run_id, self.cfg)[1],
            record_evidence=export.match_map(self.conn, self.run_id, self.cfg)[2],
        )
        self.assertIn("未给出时间", rows[0]["scraped_at"])
        self.assertNotIn("2026-10-14", rows[0]["scraped_at"])

    def test_a_row_that_also_has_a_live_part_keeps_its_fetch_time(self):
        # 混合行里确实有这次真取到的字节，那个时间是真的。
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            },
            context={
                "observed_at": None,
                "scraped_at": "2026-10-14T09:00:00Z",
                "acquired_via": "import, live",
            },
        )
        rows = export.reader_rows(
            self.conn,
            self.run_id,
            self.cfg,
            "zh",
            matches=export.match_map(self.conn, self.run_id, self.cfg)[1],
            record_evidence=export.match_map(self.conn, self.run_id, self.cfg)[2],
        )
        self.assertNotIn("未给出时间", rows[0]["scraped_at"])

    def test_a_live_row_stamps_when_this_run_fetched_it(self):
        # 真取过字节的行：那个时间就是"这次跑拿到它的时刻"，该显示。
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            },
            context={
                "observed_at": None,
                "scraped_at": "2026-10-14T09:00:00Z",
                "acquired_via": "live",
            },
        )
        rows = export.reader_rows(
            self.conn,
            self.run_id,
            self.cfg,
            "zh",
            matches=export.match_map(self.conn, self.run_id, self.cfg)[1],
            record_evidence=export.match_map(self.conn, self.run_id, self.cfg)[2],
        )
        self.assertNotIn("未给出时间", rows[0]["scraped_at"])
        self.assertTrue(rows[0]["scraped_at"])

    def test_a_fetched_detail_page_is_the_evidence_and_is_not_flagged_unvisited(self):
        # A directory record really was read from its detail page, so that page
        # is the evidence and there is nothing "unvisited" to warn about.
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "description": "宠物服务；宠物食品及用品零售。",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            },
            context={
                "acquired_via": "live",
                "source_page_url": "https://www.11467.com/qiye/209193961.htm",
                "detail_url": "https://www.11467.com/qiye/209193961.htm",
                "detail_fetched": True,
            },
        )
        evidence = self.only_evidence()
        self.assertEqual(
            evidence.source_url, "https://www.11467.com/qiye/209193961.htm"
        )
        self.assertEqual(evidence.detail_url, "")
        self.assertTrue(evidence.detail_fetched)

    def test_an_unknown_snapshot_time_is_left_unknown_not_invented(self):
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "description": "宠物服务；宠物食品及用品零售。",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            },
            context={"observed_at": "", "snapshot_id": ""},
        )
        evidence = self.only_evidence()
        self.assertEqual(evidence.observed_at, "")
        row = self.conn.execute(
            "SELECT observed_at, snapshot_id FROM observations"
        ).fetchone()
        self.assertIsNone(row["observed_at"])
        self.assertIsNone(row["snapshot_id"])

    def test_the_transport_path_is_not_the_evidence(self):
        # The CSV path is where the row was read *from*, not what it is about.
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "description": "宠物服务；宠物食品及用品零售。",
                "source_id": "row-1",
                "source_id_kind": "csv_row",
            }
        )
        evidence = self.only_evidence()
        self.assertTrue(evidence.source_url.startswith("https://"))
        self.assertNotIn(".csv", evidence.source_url)


class DiscoveredWebsiteEvidenceTest(EvidenceProvenanceTest):
    """Round 026 P1: a website this run *found* is reported with its basis.

    A value the run inferred must not be indistinguishable from one the source
    published. The website column can only hold a URL, so the reason has to live
    in the evidence cell -- otherwise a reader cannot tell a listed site from a
    discovered one, nor see what the discovery rested on.
    """

    def add_site_fact(self, entity_id, *, provider, url, note="", source_url=""):
        from collector.util import content_hash

        return db.insert_fact(
            self.conn,
            entity_id=entity_id,
            observation_id=None,
            run_id=self.run_id,
            field="website",
            raw_value=url,
            normalized_value=None,
            provider=provider,
            source_url=source_url or url,
            query_context=None,
            scraped_at=None,
            evidence_snippet=None,
            evidence_ref=url,
            extractor_version="test",
            validation_status="confirmed",
            validation_note=note,
            observed_at="2026-10-13T21:11:21+08:00",
            fact_key=content_hash([entity_id, "website", url, provider]),
        )

    def cell(self) -> str:
        from collector import export

        _criteria, matches, evidence = export.match_map(self.conn, self.run_id, self.cfg)
        rows = export.reader_rows(
            self.conn, self.run_id, self.cfg, "zh", matches=matches, record_evidence=evidence
        )
        return rows[0]["match_evidence"]

    def test_a_discovered_site_states_why_it_is_there(self):
        from collector.sitediscovery import PROVIDER

        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "description": "宠物服务；宠物食品及用品零售。",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            }
        )
        entity_id = int(
            self.conn.execute("SELECT id FROM entities").fetchone()["id"]
        )
        self.add_site_fact(
            entity_id,
            provider=PROVIDER,
            url="https://maoyang-pet.example/",
            note="页面名称与名录一致，且地址一致",
            source_url="https://maoyang-pet.example/",
        )
        cell = self.cell()
        self.assertIn("官网（本批发现）：https://maoyang-pet.example/", cell)
        self.assertIn("判定依据：页面名称与名录一致，且地址一致", cell)
        # The page the verdict came from is named, so it can be re-checked.
        self.assertIn("候选页：https://maoyang-pet.example/", cell)

    def test_a_listed_site_is_not_labelled_as_found(self):
        # Stage D writes a ``website`` fact too, but it records that a *listed*
        # site was visited. That is not an inference and must not be dressed up
        # as one.
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "description": "宠物服务；宠物食品及用品零售。",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
                "web_site": "https://listed.example/",
            }
        )
        entity_id = int(self.conn.execute("SELECT id FROM entities").fetchone()["id"])
        self.add_site_fact(
            entity_id,
            provider="website",
            url="https://listed.example/",
            note="网站与名录一致",
        )
        self.assertNotIn("本批发现", self.cell())

    def test_a_record_with_no_discovered_site_gets_no_line(self):
        self.ingest_record(
            {
                "title": "杭州猫氧宠物服务有限公司",
                "address": "浙江省杭州市滨江区长河街道长江路448号长河商厦",
                "description": "宠物服务；宠物食品及用品零售。",
                "source_id": "https://www.11467.com/qiye/209193961.htm",
                "source_id_kind": "csv_row",
            }
        )
        self.assertNotIn("本批发现", self.cell())


class ExportTestCase(unittest.TestCase):
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
            target_count=10,
        )
        self.cfg = support.config({"export": {"fields": EXPORT_FIELDS}})
        self.raw_dir = self.tmp / "raw"
        self.raw_dir.mkdir()
        self.out_dir = self.tmp / "export"

    def seed(self, records, name="a.jsonl"):
        path = self.raw_dir / name
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(support.envelope(record), ensure_ascii=False) + "\n")
        return path

    def ingest(self, records, name="a.jsonl"):
        self.seed(records, name)
        return ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=self.cfg)


class CsvTest(ExportTestCase):
    def test_header_matches_the_requested_columns_in_order(self):
        self.ingest([support.raw_record()])
        export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        with open(self.out_dir / "entities.csv", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], EXPORT_FIELDS)

    def test_values_unicode_and_missing_cells(self):
        self.ingest([
            support.raw_record(
                data_id="d1",
                title="Zahn\u00e4rzte Feuerlandh\u00f6fe \u2013 Dr. Vo\u00df",
                address="M\u00fcnzstra\u00dfe 12, 10178 Berlin",
            ),
            support.raw_record(data_id="d2", title="Praxis Ohne Kontakt", phone=None, web_site=None),
        ])
        export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        with open(self.out_dir / "entities.csv", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 2)
        by_title = {row["title"]: row for row in rows}
        self.assertIn("Zahn\u00e4rzte Feuerlandh\u00f6fe \u2013 Dr. Vo\u00df", by_title)
        self.assertEqual(
            by_title["Zahn\u00e4rzte Feuerlandh\u00f6fe \u2013 Dr. Vo\u00df"]["address"],
            "M\u00fcnzstra\u00dfe 12, 10178 Berlin",
        )
        # A missing value is an empty cell, never the string "None".
        self.assertEqual(by_title["Praxis Ohne Kontakt"]["phone"], "")
        self.assertEqual(by_title["Praxis Ohne Kontakt"]["website"], "")

    def test_csv_has_a_bom_so_excel_reads_utf8(self):
        self.ingest([support.raw_record()])
        export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        self.assertTrue((self.out_dir / "entities.csv").read_bytes().startswith(b"\xef\xbb\xbf"))


class JsonlTest(ExportTestCase):
    def test_one_object_per_line_and_identity_agrees_with_csv(self):
        self.ingest([support.raw_record(data_id="d1"), support.raw_record(data_id="d2")])
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        with open(paths["jsonl"], encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        with open(paths["csv"], encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(len(records), len(rows))
        self.assertEqual(
            sorted(record["stable_key"] for record in records),
            sorted(row["stable_key"] for row in rows),
        )

    def test_field_facts_file_keeps_superseded_values(self):
        cfg = support.config(
            {"collect": {"fields": ["title", "review_count"]}, "export": {"fields": EXPORT_FIELDS}}
        )
        self.cfg = cfg
        self.ingest([support.raw_record(review_count=10)])
        self.ingest([support.raw_record(review_count=99)], name="b.jsonl")
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        with open(paths["field_facts"], encoding="utf-8") as handle:
            facts = [json.loads(line) for line in handle if line.strip()]

        ratings = [f for f in facts if f["field"] == "review_count"]
        self.assertEqual(sorted(f["raw_value"] for f in ratings), ["10", "99"])
        for fact in facts:
            self.assertIn("evidence_ref", fact)
            self.assertIn("validation_status", fact)
            self.assertIn("extractor_version", fact)

    def test_errors_file_records_unattributable_records(self):
        raw = support.raw_record()
        for key in ("place_id", "cid", "data_id", "link"):
            raw.pop(key, None)
        self.ingest([raw])
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        with open(paths["errors"], encoding="utf-8") as handle:
            errors = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["kind"], "missing_identity")
        self.assertEqual(errors[0]["stage"], "ingest")


class StageDFactViewTest(ExportTestCase):
    """How the entity view behaves once a second source exists (stage D)."""

    def add_website_fact(self, *, field, raw, normalized, status, state="from_website"):
        entity_id = int(
            self.conn.execute("SELECT id FROM entities ORDER BY id LIMIT 1").fetchone()["id"]
        )
        return db.insert_fact(
            self.conn,
            entity_id=entity_id,
            observation_id=None,
            run_id=self.run_id,
            field=field,
            raw_value=raw,
            normalized_value=normalized,
            provider="website",
            source_url="https://www.praxis.example.de/kontakt",
            query_context="dentists in Berlin",
            scraped_at="2026-10-12T00:00:00.000000Z",
            evidence_snippet=raw,
            evidence_ref="https://www.praxis.example.de/kontakt#jsonld[1]",
            extractor_version="website-jsonld/1",
            validation_status=status,
            validation_note="note",
            validation_state=state,
            checks_json='{"mx_hosts": ["mx.praxis.example.de"]}',
            observed_at="2026-10-12T00:00:01.000000Z",
            fact_key=f"test:{field}:{raw}",
        )

    def row_for(self, title):
        self.ingest([support.raw_record(data_id="d1", title=title, phone="+49 30 12345678")])
        export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        with open(self.out_dir / "entities.csv", encoding="utf-8-sig", newline="") as handle:
            return next(row for row in csv.DictReader(handle) if row["title"].startswith(title))

    def test_the_listing_spelling_survives_a_confirmed_website_value(self):
        # Same phone written two ways: the listing's form is what the column shows.
        row = self.row_for("Praxis Beispiel")
        self.add_website_fact(
            field="phone",
            raw="+49 30 12345678",
            normalized="+493012345678",
            status="confirmed",
            state="confirmed_by_website",
        )
        export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        with open(self.out_dir / "entities.csv", encoding="utf-8-sig", newline="") as handle:
            row = next(iter(csv.DictReader(handle)))
        self.assertEqual(row["phone"], "+49 30 12345678")

    def test_an_invalid_value_is_not_offered_as_the_entity_value(self):
        self.row_for("Praxis Beispiel")
        self.add_website_fact(
            field="phone",
            raw="0000000",
            normalized=None,
            status="invalid",
            state="not_a_valid_number",
        )
        export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        with open(self.out_dir / "entities.csv", encoding="utf-8-sig", newline="") as handle:
            row = next(iter(csv.DictReader(handle)))
        self.assertNotIn("0000000", row["phone"])

    def test_a_genuinely_different_value_is_shown_alongside_the_original(self):
        self.row_for("Praxis Beispiel")
        self.add_website_fact(
            field="phone",
            raw="+49 30 99999999",
            normalized="+493099999999",
            status="uncertain",
            state="conflicts_with_listing",
        )
        export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        with open(self.out_dir / "entities.csv", encoding="utf-8-sig", newline="") as handle:
            row = next(iter(csv.DictReader(handle)))
        self.assertIn("+49 30 12345678", row["phone"])
        self.assertIn("+49 30 99999999", row["phone"])

    def test_the_facts_file_carries_the_state_and_the_checks(self):
        self.row_for("Praxis Beispiel")
        self.add_website_fact(
            field="emails",
            raw="info@praxis.example.de",
            normalized="info@praxis.example.de",
            status="valid",
            state="mx_present",
        )
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        with open(paths["field_facts"], encoding="utf-8") as handle:
            facts = [json.loads(line) for line in handle if line.strip()]
        website = [row for row in facts if row["provider"] == "website"]
        self.assertEqual(len(website), 1)
        self.assertEqual(website[0]["validation_state"], "mx_present")
        self.assertEqual(website[0]["checks"]["mx_hosts"], ["mx.praxis.example.de"])
        # Listing facts carry their own state, so "valid" is never ambiguous.
        listing_phones = [
            row for row in facts if row["field"] == "phone" and row["provider"] == "google_maps"
        ]
        self.assertEqual(listing_phones[0]["validation_state"], "listing_phone_syntax_ok")


class XlsxTest(ExportTestCase):
    """Stage F workbook: the task book's reopen-and-check list."""

    LONG = "x" * 5000  # Excel's limit is 32767; this is a legal long value

    def export_xlsx(self, records, **config_overrides):
        overrides = {
            "collect": {"fields": list(support.BASE_CONFIG["collect"]["fields"])},
            "export": {
                "fields": list(EXPORT_FIELDS) + ["score_composite", "score_completeness"],
                "formats": ["csv", "jsonl", "xlsx"],
            },
        }
        self._merge(overrides, config_overrides)
        self.cfg = support.config(overrides)
        self.ingest(records)
        return export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

    def _merge(self, target, overrides):
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._merge(target[key], value)
            else:
                target[key] = value

    def workbook(self, path):
        import openpyxl

        return openpyxl.load_workbook(path)

    def test_record_and_evidence_counts_agree_across_formats(self):
        paths = self.export_xlsx(
            [support.raw_record(data_id="d1"), support.raw_record(data_id="d2")]
        )

        book = self.workbook(paths["xlsx"])
        with open(paths["csv"], encoding="utf-8-sig", newline="") as handle:
            csv_rows = list(csv.DictReader(handle))
        with open(paths["field_facts"], encoding="utf-8") as handle:
            fact_rows = [json.loads(line) for line in handle if line.strip()]

        records = book["记录"]
        evidence = book["字段证据"]
        # header + one row per store, in both directions
        self.assertEqual(records.max_row - 1, len(csv_rows))
        self.assertEqual(evidence.max_row - 1, len(fact_rows))
        # the entity id is kept so evidence can be joined back
        self.assertEqual(records.cell(row=1, column=1).value, "entity_id")
        ids = {records.cell(row=r, column=1).value for r in range(2, records.max_row + 1)}
        self.assertEqual(ids, {str(row["entity_id"]) for row in csv_rows})

    def test_unicode_missing_values_and_long_text_survive_a_reopen(self):
        paths = self.export_xlsx(
            [
                support.raw_record(
                    data_id="uni-1",
                    title="\u6f22\u65b9\u30af\u30ea\u30cb\u30c3\u30af\u5802\u6797",
                    phone="+49 30 1234567",
                    address=self.LONG + ", 10115 Berlin",
                ),
                support.raw_record(data_id="uni-2", web_site=None),
            ]
        )
        book = self.workbook(paths["xlsx"])
        records = book["记录"]
        header = [records.cell(row=1, column=c).value for c in range(1, records.max_column + 1)]
        row_of = {
            records.cell(row=r, column=header.index("stable_key") + 1).value: r
            for r in range(2, records.max_row + 1)
        }

        def cell(data_id, column):
            return records.cell(row=row_of[f"data_id:{data_id}"], column=header.index(column) + 1).value

        self.assertEqual(cell("uni-1", "title"), "\u6f22\u65b9\u30af\u30ea\u30cb\u30c3\u30af\u5802\u6797")
        self.assertEqual(cell("uni-1", "address"), self.LONG + ", 10115 Berlin")
        self.assertEqual(cell("uni-1", "phone"), "+49 30 1234567")
        # A field the source simply did not carry is an empty cell, never the
        # string "None" and never a zero.
        self.assertIsNone(cell("uni-2", "website"))

    def test_formula_shaped_strings_stay_text(self):
        paths = self.export_xlsx(
            [
                support.raw_record(data_id="formula-1", title="=SUM(A1:A9)+1"),
                support.raw_record(data_id="formula-2", address="- 1st Street, 10115 Berlin"),
            ]
        )
        book = self.workbook(paths["xlsx"])
        records = book["记录"]
        header = [records.cell(row=1, column=c).value for c in range(1, records.max_column + 1)]
        title_column = header.index("title") + 1
        address_column = header.index("address") + 1

        titles = {
            records.cell(row=r, column=title_column).value for r in range(2, records.max_row + 1)
        }
        self.assertIn("=SUM(A1:A9)+1", titles)

        addresses = {
            records.cell(row=r, column=address_column).value for r in range(2, records.max_row + 1)
        }
        self.assertIn("- 1st Street, 10115 Berlin", addresses)
        for r in range(2, records.max_row + 1):
            cell = records.cell(row=r, column=title_column)
            if cell.value == "=SUM(A1:A9)+1":
                self.assertEqual(cell.data_type, "s")

    def test_phone_column_keeps_text_semantics(self):
        paths = self.export_xlsx([support.raw_record(data_id="txt-1", phone="+49 30 1234567")])
        book = self.workbook(paths["xlsx"])
        records = book["记录"]
        header = [records.cell(row=1, column=c).value for c in range(1, records.max_column + 1)]
        cell = records.cell(row=2, column=header.index("phone") + 1)
        self.assertEqual(cell.number_format, "@")
        self.assertEqual(cell.data_type, "s")
        self.assertEqual(cell.value, "+49 30 1234567")

    def test_over_the_excel_limit_is_reported_not_clipped(self):
        from openpyxl import Workbook

        cell = Workbook().active.cell(row=1, column=1)
        with self.assertRaises(export.ExportError):
            export._set_text(cell, "y" * (export.CELL_MAX_CHARS + 1))

    def test_a_control_character_cannot_kill_the_workbook(self):
        # openpyxl raises IllegalCharacterError on these, so one stray NUL from
        # a scraped page used to abort the whole export. They are removed, and
        # the overview declares that removal.
        from openpyxl import Workbook

        cell = Workbook().active.cell(row=1, column=1)
        export._set_text(cell, "杭州\x00某某\x0b宠物店")
        self.assertEqual(cell.value, "杭州某某宠物店")

    def test_the_workbook_declares_what_it_does_to_external_text(self):
        # A reader must be able to tell that cell text was adjusted; otherwise
        # the CSV's apostrophe looks like part of the source's own wording.
        for code in ("zh", "en", "bilingual"):
            with self.subTest(lang=code):
                spec = export.LANG_SPECS[code]
                self.assertIn("limits.cell.value", spec.texts)
                self.assertTrue(spec.texts["limits.cell.value"].strip())

    def test_failures_and_conflicts_sheets_list_both(self):
        self.ingest([support.raw_record(data_id="bad-1", phone="12")])  # invalid: few digits
        entity_id = self.conn.execute("SELECT id FROM entities").fetchone()[0]
        db.insert_fact(
            self.conn,
            entity_id=entity_id,
            observation_id=None,
            run_id=self.run_id,
            field="website",
            raw_value="https://other.example.de",
            normalized_value="https://other.example.de",
            provider="enrich",
            source_url="https://www.praxis.example.de",
            query_context=None,
            scraped_at=None,
            evidence_snippet=None,
            evidence_ref=None,
            extractor_version="stage-d",
            validation_status="uncertain",
            validation_note="the website states a different domain than the listing",
            validation_state="website_conflict",
            observed_at="2026-10-01T10:00:00+00:00",
            fact_key="test-conflict-1",
        )
        cfg = support.config({"export": {"formats": ["xlsx"], "fields": EXPORT_FIELDS}})
        self.cfg = cfg
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        book = self.workbook(paths["xlsx"])
        problems = book["失败与冲突"]
        texts = [
            problems.cell(row=r, column=c).value
            for r in range(1, problems.max_row + 1)
            for c in range(1, problems.max_column + 1)
            if problems.cell(row=r, column=c).value is not None
        ]
        self.assertTrue(any("invalid" in t for t in texts), texts)
        self.assertTrue(any("website_conflict" in t for t in texts), texts)

    def test_a_source_conflict_shows_both_values_in_the_technical_workbook(self):
        # 048.4: the note says both halves of the page's self-contradiction are
        # kept. Saying that while only printing one of them sent the reader to a
        # JSONL file to find the other, so the technical sheet carries both.
        self.ingest([support.raw_record(data_id="conf-1")])
        entity_id = self.conn.execute("SELECT id FROM entities").fetchone()[0]
        db.insert_fact(
            self.conn,
            entity_id=entity_id,
            observation_id=None,
            run_id=self.run_id,
            field="phone",
            raw_value="+4940237751001",
            normalized_value="+4940237751001",
            provider="website",
            source_url="https://imovis.de/",
            query_context=None,
            scraped_at=None,
            evidence_snippet=None,
            evidence_ref=None,
            extractor_version="stage-d",
            validation_status="uncertain",
            validation_note="页面自相矛盾：链接目标与可见文本是两个不同的值",
            validation_state="source_conflict",
            checks_json=json.dumps(
                {
                    "source_conflict": True,
                    "href_value": "+4940237751001",
                    "display_text": "+49 40 182 11 6600",
                }
            ),
            observed_at="2026-10-14T00:00:00+00:00",
            fact_key="test-source-conflict-1",
        )
        cfg = support.config({"export": {"formats": ["xlsx"], "fields": EXPORT_FIELDS}})
        self.cfg = cfg
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        book = self.workbook(paths["xlsx"])
        problems = book["失败与冲突"]
        texts = [
            str(problems.cell(row=r, column=c).value)
            for r in range(1, problems.max_row + 1)
            for c in range(1, problems.max_column + 1)
            if problems.cell(row=r, column=c).value is not None
        ]
        both = [t for t in texts if "+49 40 182 11 6600" in t]
        self.assertTrue(both, texts)
        self.assertIn("+4940237751001", both[0])
        self.assertIn("链接目标", both[0])
        self.assertIn("页面显示", both[0])

    def test_an_ordinary_uncertain_fact_leaves_the_conflict_column_empty(self):
        self.ingest([support.raw_record(data_id="unc-1", phone="12")])
        cfg = support.config({"export": {"formats": ["xlsx"], "fields": EXPORT_FIELDS}})
        paths = export.export_run(self.conn, self.run_id, cfg, self.out_dir)

        book = self.workbook(paths["xlsx"])
        problems = book["失败与冲突"]
        header_row = 1
        for row in range(1, problems.max_row + 1):
            if problems.cell(row=row, column=1).value == "entity_id":
                header_row = row
                break
        column = list(
            problems.cell(row=header_row, column=c).value
            for c in range(1, problems.max_column + 1)
        ).index("conflicting_values") + 1
        values = [
            problems.cell(row=row, column=column).value
            for row in range(header_row + 1, problems.max_row + 1)
            if problems.cell(row=row, column=1).value is not None
        ]
        self.assertTrue(values)
        self.assertTrue(all(not value for value in values), values)

    def test_the_scoring_rule_ships_with_the_workbook(self):
        paths = self.export_xlsx([support.raw_record(data_id="s1")])
        book = self.workbook(paths["xlsx"])
        self.assertIn("评分说明", book.sheetnames)
        rule = book["评分说明"]
        texts = [str(rule.cell(row=r, column=1).value) for r in range(1, rule.max_row + 1)]
        self.assertTrue(any(scoring.SCORER_VERSION in t for t in texts))
        self.assertTrue(any("不是准确率" in t for t in texts))


class AbsentDefaultTest(ExportTestCase):
    """A provider's absent-default 0 is not a value and must not be exported."""

    def config(self):
        return support.config(
            {
                "collect": {"fields": ["title", "review_count"]},
                "export": {
                    "fields": ["entity_id", "title", "review_count", "provider"],
                    "formats": ["csv"],
                },
            }
        )

    def test_a_google_zero_review_count_is_not_exported_as_a_value(self):
        self.cfg = self.config()
        self.ingest([support.raw_record(data_id="rc-1", review_count=0)])
        export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        with open(self.out_dir / "entities.csv", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        # google_maps fast mode does not fetch the count; the 0 is its absent
        # marker, so the export shows nothing rather than a verified zero.
        self.assertEqual(rows[0]["review_count"], "")

    def write_envelope(self, envelope: dict, name: str = "a.jsonl"):
        """Write a pre-built envelope verbatim (``ingest`` would re-wrap it)."""
        path = self.raw_dir / name
        path.write_text(json.dumps(envelope, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def test_a_genuine_zero_from_another_source_is_kept(self):
        self.cfg = self.config()
        self.write_envelope(
            support.envelope(
                support.raw_record(data_id=None, source_id="node/1", review_count=0),
                provider="openstreetmap",
            )
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=self.cfg)
        export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        with open(self.out_dir / "entities.csv", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["review_count"], "0")

    def test_a_real_count_still_survives(self):
        self.cfg = self.config()
        self.ingest([support.raw_record(data_id="rc-9", review_count=9)])
        export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        with open(self.out_dir / "entities.csv", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["review_count"], "9")


class VerificationWordingTest(ExportTestCase):
    """051 A3: 冲突 and 待核实 are different statements.

    ``uncertain`` used to be one bucket, and everything in it was reported with
    the word 冲突. A reader who trusted that count went looking for two values
    that disagree -- and for the commonest uncertain fact there is (an address
    slot holding the business's own name) there is no disagreement to find.
    """

    def verification_cell(self):
        self.cfg = support.config(
            {
                "collect": {"fields": ["title", "address"]},
                "export": {
                    "fields": EXPORT_FIELDS,
                    "formats": ["xlsx"],
                    "languages": ["zh"],
                },
            }
        )
        self.ingest(
            [support.raw_record(data_id="v1", title="geomer GmbH", address="geomer GmbH")]
        )
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        import openpyxl

        sheet = openpyxl.load_workbook(paths["xlsx_zh"])["商家名单"]
        headers = [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]
        return str(sheet.cell(row=2, column=headers.index("核实情况") + 1).value)

    def test_a_name_shaped_address_reads_as_needs_review_not_conflict(self):
        cell = self.verification_cell()
        self.assertIn("待核实", cell)
        self.assertNotIn("冲突", cell)

    def test_the_english_workbook_says_the_same_thing_in_its_own_words(self):
        self.cfg = support.config(
            {
                "collect": {"fields": ["title", "address"]},
                "export": {
                    "fields": EXPORT_FIELDS,
                    "formats": ["xlsx"],
                    "languages": ["en"],
                },
            }
        )
        self.ingest(
            [support.raw_record(data_id="v1", title="geomer GmbH", address="geomer GmbH")]
        )
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        import openpyxl

        sheet = openpyxl.load_workbook(paths["xlsx_en"])["Business list"]
        headers = [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]
        cell = str(sheet.cell(row=2, column=headers.index("Verification") + 1).value)
        self.assertIn("need review", cell)
        self.assertNotIn("conflict", cell)


class BilingualXlsxTest(ExportTestCase):
    """The reader-facing workbook: Chinese first, honest about what is missing."""

    def bilingual(self, records, **collect_overrides):
        collect = {"fields": ["title", "address", "phone", "website", "emails", "rating", "review_count"]}
        collect.update(collect_overrides)
        self.cfg = support.config(
            {
                "collect": collect,
                "export": {"fields": EXPORT_FIELDS, "formats": ["xlsx"]},
            }
        )
        self.ingest(records)
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        import openpyxl

        return openpyxl.load_workbook(paths["xlsx_bilingual"])

    def test_the_reader_workbook_is_written_next_to_the_technical_one(self):
        paths = self.export_run_xlsx()
        self.assertIn("xlsx", paths)
        self.assertIn("xlsx_bilingual", paths)
        self.assertEqual(paths["xlsx_bilingual"].name, "entities_bilingual.xlsx")
        self.assertTrue(paths["xlsx_bilingual"].is_file())

    def export_run_xlsx(self):
        self.cfg = support.config(
            {"export": {"fields": EXPORT_FIELDS, "formats": ["xlsx"]}}
        )
        self.ingest([support.raw_record()])
        return export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

    def test_the_main_table_is_chinese_first_and_english_second(self):
        book = self.bilingual([support.raw_record()])
        sheet = book["商家名单"]
        headers = [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]
        self.assertEqual(headers[0], "序号 / No.")
        self.assertTrue(any(h == "商家名称 / Business name" for h in headers))
        self.assertTrue(any(h == "地址 / Address" for h in headers))
        # The task book forbids a technical id / raw json in the main table.
        self.assertNotIn("stable_key", headers)
        self.assertNotIn("raw", headers)

    def test_an_empty_optional_column_is_dropped_and_listed_in_the_overview(self):
        # No record carries an email, so the email column must not appear; the
        # overview has to say it was not obtained rather than leave it a mystery.
        book = self.bilingual([support.raw_record(emails=None)])
        sheet = book["商家名单"]
        headers = [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]
        self.assertFalse(any("邮箱" in h for h in headers))
        overview = book["概览"]
        text = "\n".join(
            str(overview.cell(row=r, column=c).value)
            for r in range(1, overview.max_row + 1)
            for c in range(1, overview.max_column + 1)
            if overview.cell(row=r, column=c).value is not None
        )
        self.assertIn("邮箱", text)
        self.assertIn("未获取项目", text)

    def test_missing_values_show_the_bilingual_placeholder(self):
        book = self.bilingual([support.raw_record(web_site=None, phone=None)])
        sheet = book["商家名单"]
        headers = [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]
        values = [
            sheet.cell(row=2, column=c).value for c in range(1, sheet.max_column + 1)
        ]
        row = dict(zip(headers, values))
        self.assertEqual(row["电话 / Phone"], "未获取 / Not available")

    def test_phone_stays_text_and_website_is_a_hyperlink(self):
        book = self.bilingual([support.raw_record()])
        sheet = book["商家名单"]
        headers = [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]
        phone_col = headers.index("电话 / Phone") + 1
        site_col = headers.index("官网 / Website") + 1
        self.assertEqual(sheet.cell(row=2, column=phone_col).number_format, "@")
        self.assertEqual(sheet.cell(row=2, column=phone_col).value, "+49 30 12345678")
        self.assertIsNotNone(sheet.cell(row=2, column=site_col).hyperlink)

    def test_times_are_shown_in_beijing_time_with_the_zone_stated(self):
        self.cfg = support.config(
            {"export": {"fields": EXPORT_FIELDS, "formats": ["xlsx"]}}
        )
        # Pin the source's own scrape time so the conversion is checkable.
        path = self.raw_dir / "a.jsonl"
        path.write_text(
            json.dumps(
                support.envelope(
                    support.raw_record(), scraped_at="2026-10-12T15:53:43.6542002Z"
                ),
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        ingest.ingest_directory(self.conn, self.raw_dir, run_id=self.run_id, cfg=self.cfg)
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        import openpyxl

        sheet = openpyxl.load_workbook(paths["xlsx_bilingual"])["商家名单"]
        headers = [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]
        time_col = headers.index("采集时间 / Collected at") + 1
        value = str(sheet.cell(row=2, column=time_col).value)
        self.assertIn("UTC+8", value)
        # 2026-10-12T15:53Z is 23:53 in Beijing, not 15:53.
        self.assertIn("23:53", value)

    def test_a_formula_shaped_value_stays_text_in_the_bilingual_sheet(self):
        book = self.bilingual([support.raw_record(data_id="f1", title="=HYPERLINK(1,2)")])
        sheet = book["商家名单"]
        headers = [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]
        cell = sheet.cell(row=2, column=headers.index("商家名称 / Business name") + 1)
        self.assertEqual(cell.value, "=HYPERLINK(1,2)")
        self.assertEqual(cell.data_type, "s")

    def test_the_overview_states_the_counting_rule_and_the_limits(self):
        book = self.bilingual([support.raw_record(data_id="o1")])
        overview = book["概览"]
        text = "\n".join(
            str(overview.cell(row=r, column=c).value)
            for r in range(1, overview.max_row + 1)
            for c in range(1, overview.max_column + 1)
            if overview.cell(row=r, column=c).value is not None
        )
        self.assertIn("入库记录", text)      # records -> entities -> merges -> stores
        self.assertIn("未逐家人工核实", text)  # the honest verification statement
        self.assertIn("评论数", text)        # the review_count caveat
        self.assertIn("不代表商家质量", text)  # score is not quality

    def test_the_source_key_sheet_carries_licences(self):
        book = self.bilingual([support.raw_record()])
        self.assertIn("来源与字段说明", book.sheetnames)
        sheet = book["来源与字段说明"]
        text = "\n".join(
            str(sheet.cell(row=r, column=c).value)
            for r in range(1, sheet.max_row + 1)
            for c in range(1, sheet.max_column + 1)
            if sheet.cell(row=r, column=c).value is not None
        )
        self.assertIn("ODbL", text)


class LanguageContractTest(ExportTestCase):
    """Stage 019: zh / en / bilingual are the same data with different words.

    The task book's language requirement is explicitly *not* a translation
    feature: the language option controls headers, sheet names, notes and the
    missing-value hint. Names, addresses and source text must come out of all
    three workbooks byte-for-byte identical, and no language may turn external
    text into a live Excel formula.
    """

    #: The record used for every comparison. The phone starts with ``+`` and the
    #: title starts with ``=``; both are the shapes that break a careless
    #: exporter (a number-formatted phone loses the plus, a formula-shaped title
    #: executes).
    RECORD = {
        "data_id": "lang-1",
        "title": "=SUM(1,1)",
        "address": "Friedrichstraße 12, 10117 Berlin",
        "phone": "+49 30 123456",
        "web_site": "https://example.com/",
        "review_count": 17,
    }

    def books(self, languages, records=None):
        """Export the same records once per requested language."""
        self.cfg = support.config({
            "collect": {"fields": ["title", "address", "phone", "website", "review_count"]},
            "export": {"fields": EXPORT_FIELDS, "formats": ["xlsx"],
                       "languages": list(languages)},
        })
        self.ingest(records if records is not None else [support.raw_record(**self.RECORD)])
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        import openpyxl

        return {
            lang: openpyxl.load_workbook(paths[f"xlsx_{lang}"])
            for lang in languages
        }

    def table(self, book, lang):
        """``{header: value}`` for the first data row of a language's list."""
        spec = export.LANG_SPECS[lang]
        sheet = book[spec.sheets[0]]
        headers = [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]
        values = [sheet.cell(row=2, column=c).value for c in range(1, sheet.max_column + 1)]
        return dict(zip(headers, values))

    def header_of(self, lang, key):
        return dict((k, h) for k, h, _w, _a in export.LANG_SPECS[lang].columns)[key]

    def test_every_language_writes_its_own_file_and_sheet_names(self):
        books = self.books(["zh", "en", "bilingual"])
        self.assertEqual(export.LANG_SPECS["zh"].filename, "entities_zh.xlsx")
        self.assertEqual(export.LANG_SPECS["en"].filename, "entities_en.xlsx")
        self.assertEqual(export.LANG_SPECS["bilingual"].filename, "entities_bilingual.xlsx")
        self.assertEqual(books["zh"].sheetnames, ["商家名单", "概览", "来源与字段说明"])
        self.assertEqual(books["en"].sheetnames,
                         ["Business list", "Overview", "Sources and fields"])
        self.assertEqual(books["bilingual"].sheetnames, ["商家名单", "概览", "来源与字段说明"])
        for lang, path_name in (("zh", "entities_zh.xlsx"), ("en", "entities_en.xlsx"),
                                ("bilingual", "entities_bilingual.xlsx")):
            with self.subTest(lang=lang):
                self.assertTrue((self.out_dir / path_name).is_file())

    def test_headers_are_monolingual_where_the_language_is_monolingual(self):
        books = self.books(["zh", "en", "bilingual"])
        zh_header = self.header_of("zh", "title")
        en_header = self.header_of("en", "title")
        self.assertNotIn("/", zh_header)   # 中文版只有中文
        self.assertNotIn("商家", en_header)  # 英文版只有英文
        self.assertIn("/", self.header_of("bilingual", "title"))
        self.assertIn(zh_header, self.table(books["zh"], "zh"))
        self.assertIn(en_header, self.table(books["en"], "en"))

    def test_the_record_count_and_the_source_text_are_identical_across_languages(self):
        records = [
            support.raw_record(**self.RECORD),
            support.raw_record(data_id="lang-2", title="Zahnarzt am Markt",
                               address="Marktplatz 3, 10115 Berlin", phone="030 987654"),
        ]
        books = self.books(["zh", "en", "bilingual"], records)
        titles = {}
        for lang, book in books.items():
            spec = export.LANG_SPECS[lang]
            sheet = book[spec.sheets[0]]
            body = [
                sheet.cell(row=r, column=2).value
                for r in range(2, sheet.max_row + 1)
                if sheet.cell(row=r, column=1).value is not None
            ]
            titles[lang] = body
        self.assertEqual(titles["zh"], titles["en"])
        self.assertEqual(titles["zh"], titles["bilingual"])
        # The raw text is carried through untouched — no translation, no
        # re-ordering, no normalisation.
        self.assertEqual(titles["zh"], ["=SUM(1,1)", "Zahnarzt am Markt"])

    def test_the_phone_keeps_its_plus_and_stays_text_in_every_language(self):
        books = self.books(["zh", "en", "bilingual"])
        for lang, book in books.items():
            with self.subTest(lang=lang):
                spec = export.LANG_SPECS[lang]
                sheet = book[spec.sheets[0]]
                headers = [
                    sheet.cell(row=1, column=c).value
                    for c in range(1, sheet.max_column + 1)
                ]
                column = headers.index(self.header_of(lang, "phone")) + 1
                cell = sheet.cell(row=2, column=column)
                self.assertEqual(cell.value, "+49 30 123456")
                self.assertEqual(cell.data_type, "s")

    def test_external_text_never_becomes_a_formula_in_any_language(self):
        books = self.books(["zh", "en", "bilingual"])
        for lang, book in books.items():
            with self.subTest(lang=lang):
                spec = export.LANG_SPECS[lang]
                sheet = book[spec.sheets[0]]
                headers = [
                    sheet.cell(row=1, column=c).value
                    for c in range(1, sheet.max_column + 1)
                ]
                column = headers.index(self.header_of(lang, "title")) + 1
                cell = sheet.cell(row=2, column=column)
                self.assertEqual(cell.value, "=SUM(1,1)")
                self.assertEqual(cell.data_type, "s")

    def test_each_language_uses_its_own_missing_value_word(self):
        books = self.books(["zh", "en", "bilingual"], [support.raw_record(phone=None, web_site=None)])
        self.assertEqual(self.table(books["zh"], "zh")[self.header_of("zh", "phone")], "未获取")
        self.assertEqual(self.table(books["en"], "en")[self.header_of("en", "phone")],
                         "Not available")
        self.assertEqual(self.table(books["bilingual"], "bilingual")[
            self.header_of("bilingual", "phone")], "未获取 / Not available")

    def test_the_english_workbook_says_the_option_only_controls_wording(self):
        # The task book allows a Chinese address in the English workbook, but
        # requires the file to say so rather than imply it was translated.
        books = self.books(["en"], [support.raw_record(address="杭州市西湖区文三路 100 号")])
        overview = books["en"]["Overview"]
        text = "\n".join(
            str(overview.cell(row=r, column=c).value)
            for r in range(1, overview.max_row + 1)
            for c in range(1, overview.max_column + 1)
            if overview.cell(row=r, column=c).value is not None
        )
        self.assertIn("Language option", text)
        self.assertIn("headers and explanatory notes only", text)
        # ...and the address really is the untranslated original, in the list.
        self.assertEqual(
            self.table(books["en"], "en")[self.header_of("en", "address")],
            "杭州市西湖区文三路 100 号",
        )

    def test_the_region_row_reaches_every_language(self):
        books = self.books(["zh", "en", "bilingual"])
        for lang, book in books.items():
            with self.subTest(lang=lang):
                overview = book[export.LANG_SPECS[lang].sheets[1]]
                text = "\n".join(
                    str(overview.cell(row=r, column=c).value)
                    for r in range(1, overview.max_row + 1)
                    for c in range(1, overview.max_column + 1)
                    if overview.cell(row=r, column=c).value is not None
                )
                # Each language carries its own label, and the ISO code always
                # travels with it so the wording never has to be trusted.
                self.assertIn(export.LANG_SPECS[lang].texts["cond.region.label"], text)
                self.assertIn("DE", text)

    def test_the_english_region_row_is_english(self):
        books = self.books(["zh", "en"])
        self.assertIn("Germany (DE)", self._overview_text(books["en"], "en"))
        self.assertIn("德国（DE）", self._overview_text(books["zh"], "zh"))

    def test_source_names_follow_the_language_of_the_sheet(self):
        # A source label is our own wording, not source text, so the English
        # sheet must not be left with a Chinese provider name.
        books = self.books(["zh", "en", "bilingual"])
        self.assertIn("Google Maps", self._overview_text(books["en"], "en"))
        self.assertNotIn("Google 地图", self._overview_text(books["en"], "en"))
        self.assertIn("Google 地图", self._overview_text(books["zh"], "zh"))
        bilingual = self._overview_text(books["bilingual"], "bilingual")
        self.assertIn("Google 地图", bilingual)
        self.assertIn("Google Maps", bilingual)

    def _overview_text(self, book, lang):
        overview = book[export.LANG_SPECS[lang].sheets[1]]
        return "\n".join(
            str(overview.cell(row=r, column=c).value)
            for r in range(1, overview.max_row + 1)
            for c in range(1, overview.max_column + 1)
            if overview.cell(row=r, column=c).value is not None
        )

    def test_asking_for_one_language_writes_only_that_file(self):
        self.books(["zh"])
        self.assertTrue((self.out_dir / "entities_zh.xlsx").is_file())
        self.assertFalse((self.out_dir / "entities_en.xlsx").exists())
        self.assertFalse((self.out_dir / "entities_bilingual.xlsx").exists())


class SiteCandidateExportTest(ExportTestCase):
    """The candidate verdicts leave the database (034 第二批 2.1/2.2).

    Until 034 the ``site_candidates`` table was written and never read back
    out, so a run that judged forty URLs and rejected all of them produced an
    export indistinguishable from a run that never looked.
    """

    def seed_candidate(self, title, url, *, found_by, origin, status, reason="", note="",
                       evidence=None):
        # A file per business: re-ingesting the same file name is a no-op, so
        # two candidates under one name would silently become one entity.
        self.ingest([support.raw_record(data_id="d-" + title, title=title)],
                    name=f"{title}.jsonl")
        entity_id = int(
            self.conn.execute("SELECT MAX(id) AS id FROM entities").fetchone()["id"]
        )
        db.upsert_site_candidate(
            self.conn, run_id=self.run_id, entity_id=entity_id, url=url,
            host="example.org", found_by=found_by, origin=origin, status=status,
            reason=reason, note=note, evidence=evidence or {}, http_status=200,
        )
        return entity_id

    def test_every_candidate_is_exported_with_where_it_came_from(self):
        self.seed_candidate("A", "https://a.example/", found_by="user_list",
                            origin="operator-supplied", status="confirmed",
                            reason="confirmed_by_name_and_phone")
        self.seed_candidate("B", "https://b.example/", found_by="page_links",
                            origin="https://dir.example/1.htm", status="rejected",
                            reason="directory_or_platform_host")
        self.seed_candidate("C", "https://c.example/", found_by="wikidata",
                            origin="structured-open-data", status="pending",
                            reason="only_the_name_agrees")
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

        with open(paths["site_candidates_csv"], encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(set(by_name), {"A", "B", "C"})
        # The three origins stay apart: an operator's file, a link on a saved
        # page, and a programmatic lookup are different weights of evidence.
        self.assertEqual(by_name["A"]["found_by_label"], "人工提供的清单")
        self.assertEqual(by_name["B"]["found_by_label"], "已有页面上的外链")
        self.assertEqual(by_name["C"]["found_by_label"], "程序查询公开结构化数据（Wikidata）")
        self.assertIn("已有页面外链", by_name["B"]["origin_label"])
        self.assertEqual(by_name["C"]["origin_label"], "公开结构化数据（程序）")
        # And the verdict, with the reason, not just the verdict.
        self.assertEqual(by_name["B"]["status"], "rejected")
        self.assertEqual(by_name["B"]["reason"], "directory_or_platform_host")

    def test_an_old_row_shows_the_audit_keys_blank_rather_than_invented(self):
        # 042 added the rules id and the evidence fingerprint. A verdict written
        # before that has neither, and guessing one would be worse than a blank.
        self.seed_candidate("D", "https://d.example/", found_by="user_list",
                            origin="operator-supplied", status="confirmed")
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        with open(paths["site_candidates_csv"], encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["judged_rules"], "")
        self.assertEqual(rows[0]["judged_evidence"], "")

    def test_the_verdict_says_which_rules_and_which_evidence_it_used(self):
        self.seed_candidate(
            "E", "https://e.example/", found_by="wikidata",
            origin="structured-open-data", status="confirmed",
            evidence={
                "host": "e.example",
                "_judgement": {"rules": "site-ownership/1:abc", "evidence": "def", "attempts": 2},
            },
        )
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        with open(paths["site_candidates_csv"], encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["judged_rules"], "site-ownership/1:abc")
        self.assertEqual(rows[0]["judged_evidence"], "def")
        self.assertEqual(rows[0]["attempts"], "2")

    def test_an_empty_run_still_writes_the_file_with_its_header(self):
        # A missing file would read as "this feature is off"; an empty one with
        # a header reads as "nothing was judged", which is the true statement.
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        with open(paths["site_candidates_csv"], encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "entity_id")

    def test_the_manifest_says_what_could_and_could_not_look(self):
        # "0 candidates" is two different facts depending on whether anything
        # was able to ask. The manifest carries the finders either way.
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        payload = json.loads(paths["sources_manifest"].read_text(encoding="utf-8"))
        self.assertIn("site_discovery", payload)
        self.assertFalse(payload["site_discovery"]["enabled"])
        self.assertEqual(payload["site_discovery"]["candidates"], 0)


class CsvCellSafetyTest(ExportTestCase):
    """026 P3.4: the CSV is a spreadsheet too, and has no cell type to lean on."""

    def csv_rows(self, records):
        self.cfg = support.config({
            "collect": {"fields": ["title", "address", "phone", "website"]},
            "export": {"fields": EXPORT_FIELDS, "formats": ["csv", "jsonl"]},
        })
        self.ingest(records)
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        with open(paths["csv"], encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        with open(paths["jsonl"], encoding="utf-8") as handle:
            raw = [json.loads(line) for line in handle if line.strip()]
        return rows, raw

    def test_a_formula_shaped_value_is_neutralised_in_the_csv(self):
        rows, raw = self.csv_rows([support.raw_record(data_id="f1", title="=SUM(A1:A9)+1")])
        self.assertEqual(rows[0]["title"], "'=SUM(A1:A9)+1")
        # ...and the lossless copy keeps the original, so nothing is destroyed.
        self.assertEqual(raw[0]["title"], "=SUM(A1:A9)+1")

    def test_a_real_phone_number_is_left_alone(self):
        # The first version of this rule prefixed every value starting with "+"
        # and put an apostrophe in front of every international phone number.
        rows, _raw = self.csv_rows(
            [support.raw_record(data_id="p1", phone="+49 30 12345678",
                               address="- 1st Street, 10115 Berlin")]
        )
        self.assertEqual(rows[0]["phone"], "+49 30 12345678")
        self.assertEqual(rows[0]["address"], "- 1st Street, 10115 Berlin")

    def test_a_formula_with_a_leading_sign_is_still_neutralised(self):
        rows, _raw = self.csv_rows(
            [
                support.raw_record(data_id="f2", title="-1+1"),
                support.raw_record(data_id="f3", title="+1+1"),
            ]
        )
        self.assertEqual({row["title"] for row in rows}, {"'-1+1", "'+1+1"})

    def test_ordinary_chinese_text_is_untouched(self):
        rows, _raw = self.csv_rows(
            [support.raw_record(data_id="c1", title="杭州爱尚宠物用品有限公司")]
        )
        self.assertEqual(rows[0]["title"], "杭州爱尚宠物用品有限公司")


class RunSummaryCountsTest(ExportTestCase):
    """034 第三批 3.6: 候选 / 最终商家 / 有联系方式，三个数不许合成一个。

    "采集到 98 条" 和 "98 家可联系的企业" 是两个交付物。把它们写成一个数，
    就是把"抓到了"冒充成"用得上"。
    """

    def seed_business(self, title, data_id, *, phone=None):
        self.ingest(
            [support.raw_record(data_id=data_id, title=title, phone=phone)],
            name=f"{data_id}.jsonl",
        )

    def test_candidates_and_survivors_are_counted_separately(self):
        self.seed_business("A", "d1", phone="+49 30 12345678")
        self.seed_business("B", "d2", phone="+49 30 87654321")
        summary = export.run_summary(self.conn, self.run_id, self.cfg)
        self.assertEqual(summary["candidates"], 2)
        self.assertEqual(summary["collected"], 2)
        self.assertEqual(summary["with_contact"], 2)

    def test_a_merged_duplicate_is_one_business_not_two(self):
        # Two records, one business: 候选 stays 2 and 最终商家 becomes 1. A card
        # that printed only one of them would read as if 1 record were missing.
        self.seed_business("A", "d1", phone="+49 30 12345678")
        self.seed_business("A", "d2", phone="+49 30 12345678")
        ids = [int(r["id"]) for r in self.conn.execute("SELECT id FROM entities ORDER BY id")]
        self.assertEqual(len(ids), 2)
        db.upsert_merge(self.conn, canonical_entity_id=ids[0], entity_id=ids[1],
                        reason="same_name_and_phone", run_id=self.run_id)

        summary = export.run_summary(self.conn, self.run_id, self.cfg)
        self.assertEqual(summary["candidates"], 2)
        self.assertEqual(summary["collected"], 1)
        self.assertEqual(summary["with_contact"], 1)

    def test_a_business_without_a_phone_or_email_is_not_counted_as_contactable(self):
        self.seed_business("A", "d1", phone=None)
        self.seed_business("B", "d2", phone="+49 30 87654321")
        summary = export.run_summary(self.conn, self.run_id, self.cfg)
        self.assertEqual(summary["collected"], 2)
        self.assertEqual(summary["with_contact"], 1)

    def test_an_invalid_phone_does_not_make_a_business_contactable(self):
        # A number that failed validation is not a way to reach anyone, however
        # many characters it has.
        self.seed_business("A", "d1", phone="000")
        entity_id = int(
            self.conn.execute("SELECT MAX(id) AS id FROM entities").fetchone()["id"]
        )
        self.conn.execute(
            "UPDATE field_facts SET validation_status = 'invalid' "
            "WHERE entity_id = ? AND field = 'phone'",
            (entity_id,),
        )
        self.conn.commit()
        self.assertEqual(export.run_summary(self.conn, self.run_id, self.cfg)["with_contact"], 0)


class AcquisitionSummaryTest(ExportTestCase):
    """026 P3.1: the first screen states how the rows were *really* obtained."""

    PROVENANCE = {
        "provider": "directory",
        "acquired_via": "live",
        "source_page_url": "https://www.b2b168.com/zhejiang-hangzhou-chongwu/",
        "detail_url": "https://www.b2b168.com/c168-10352910.html",
        "detail_fetched": True,
        "origin": "www.b2b168.com",
        "observed_at": "2026-10-13T13:44:00+08:00",
    }

    def seed_with(self, records, provenance, scraped_at=None):
        path = self.raw_dir / "p.jsonl"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                # A directory record identifies itself by the detail URL it was
                # read from; without it the record has no identity and nothing
                # reaches the export at all.
                record = dict(record)
                record.setdefault("source_id", provenance.get("detail_url", ""))
                handle.write(
                    json.dumps(
                        support.envelope(
                            record, provenance=provenance, scraped_at=scraped_at
                        ),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        return ingest.ingest_directory(
            self.conn, self.raw_dir, run_id=self.run_id, cfg=self.cfg
        )

    def lines(self, lang="zh"):
        summary = export.acquisition_summary(self.conn, self.run_id, self.cfg)
        return summary, dict(export._acquisition_lines(summary, export.LANG_SPECS[lang]))

    def test_a_live_record_is_reported_as_live(self):
        self.seed_with([support.raw_record(data_id="l1")], self.PROVENANCE)
        summary, lines = self.lines()
        self.assertEqual(summary["modes"], {"live": 1})
        self.assertIn("实时抓取", lines["获取方式"])

    def test_an_imported_row_is_never_reported_as_live(self):
        # The whole point of P2.1/P3.1: an import cannot read as a visit.
        self.seed_with(
            [support.raw_record(data_id="i1")],
            dict(
                self.PROVENANCE,
                acquired_via="import",
                snapshot_id="page.html@0123456789abcdef",
                input_path="/local/page.html",
                detail_fetched=False,
            ),
        )
        summary, lines = self.lines()
        self.assertEqual(summary["modes"], {"import": 1})
        self.assertIn("离线导入", lines["获取方式"])
        self.assertNotIn("实时抓取", lines["获取方式"])

    def test_a_row_with_no_provenance_says_unknown_not_live(self):
        # Most historical rows predate the provenance work. Guessing "live" for
        # them would be exactly the claim 026 P0 forbids.
        self.ingest([support.raw_record(data_id="n1")])
        summary, lines = self.lines()
        self.assertEqual(summary["modes"], {"unknown": 1})
        self.assertIn("未知", lines["获取方式"])

    def test_a_live_row_without_a_stated_time_shows_when_it_was_fetched(self):
        # 061: the per-row cell already did this; the overview next to it said
        # "来源未给出时间" for the very same rows.
        self.seed_with(
            [support.raw_record(data_id="t3")],
            {k: v for k, v in self.PROVENANCE.items() if k != "observed_at"},
            scraped_at="2026-10-16T12:10:08Z",
        )
        _summary, lines = self.lines()
        self.assertNotEqual(lines["来源时间"], "来源未给出时间")
        self.assertIn("2026-10-16", lines["来源时间"])

    def test_an_imported_row_still_does_not_show_the_import_moment(self):
        # The middle case must not become a loophole: an import's scraped_at is
        # the moment somebody imported the file, not the moment data was taken.
        self.seed_with(
            [support.raw_record(data_id="t4")],
            dict(
                {k: v for k, v in self.PROVENANCE.items() if k != "observed_at"},
                acquired_via="import",
            ),
            scraped_at="2026-10-16T12:10:08Z",
        )
        _summary, lines = self.lines()
        self.assertEqual(lines["来源时间"], "来源未给出时间")

    def test_the_source_time_is_the_sources_own_statement(self):
        self.seed_with([support.raw_record(data_id="t1")], self.PROVENANCE)
        _summary, lines = self.lines()
        self.assertEqual(lines["来源时间"], "2026-10-13 13:44 (UTC+8)")

    def test_a_row_without_a_source_time_says_so(self):
        self.seed_with(
            [support.raw_record(data_id="t2")],
            {k: v for k, v in self.PROVENANCE.items() if k != "observed_at"},
        )
        _summary, lines = self.lines()
        self.assertEqual(lines["来源时间"], "来源未给出时间")

    def test_the_english_workbook_says_the_same_thing_in_english(self):
        self.seed_with([support.raw_record(data_id="e1")], self.PROVENANCE)
        _summary, lines = self.lines("en")
        self.assertIn("live fetch", lines["Method"])
        # A single timestamp keeps the language-neutral (UTC+8) suffix the rest
        # of the workbook uses; only a *range* spells the zone out.
        self.assertEqual(lines["Source time"], "2026-10-13 13:44 (UTC+8)")

    def test_a_time_range_names_the_zone_in_the_readers_language(self):
        summary = {
            "modes": {"live": 2},
            "observed_from": "2026-10-13T01:00:00+08:00",
            "observed_to": "2026-10-13T02:00:00+08:00",
            "observed_count": 2,
            "origins": [],
            "source_pages": 0,
            "rows": 2,
        }
        zh = dict(export._acquisition_lines(summary, export.LANG_SPECS["zh"]))
        en = dict(export._acquisition_lines(summary, export.LANG_SPECS["en"]))
        self.assertIn("北京时间", zh["来源时间"])
        self.assertIn("Beijing time", en["Source time"])

    def test_a_date_only_statement_is_shown_as_a_date_not_as_missing(self):
        # Found by the 026 P3.5 bat end-to-end run: the offline importer's "when
        # was this page captured" field is answered with a date far more often
        # than with a timestamp, and a date used to render as "来源未给出时间"
        # -- telling the reader the source said nothing when the operator had
        # said exactly what they knew.
        self.seed_with(
            [support.raw_record(data_id="d1")],
            dict(self.PROVENANCE, observed_at="2026-10-13"),
        )
        summary, lines = self.lines()
        self.assertEqual(summary["observed_from"], "2026-10-13")
        self.assertIn("2026-10-13", lines["来源时间"])
        self.assertIn("只给到日期", lines["来源时间"])
        self.assertNotIn("未给出时间", lines["来源时间"])

    def test_a_date_never_gains_a_clock_or_a_zone_it_did_not_have(self):
        # Passing the date through the Beijing-time renderer would invent
        # 08:00 (UTC+8) out of nothing.
        self.seed_with(
            [support.raw_record(data_id="d2")],
            dict(self.PROVENANCE, observed_at="2026-10-13"),
        )
        _summary, lines = self.lines()
        self.assertNotIn("UTC+8", lines["来源时间"])
        self.assertNotIn("00:00", lines["来源时间"])
        self.assertNotIn("08:00", lines["来源时间"])

    def test_two_dates_read_as_a_range_of_dates(self):
        summary = {
            "modes": {"import": 2},
            "observed_from": "2026-10-01",
            "observed_to": "2026-10-13",
            "observed_count": 2,
            "origins": [],
            "source_pages": 0,
            "rows": 2,
        }
        zh = dict(export._acquisition_lines(summary, export.LANG_SPECS["zh"]))
        self.assertIn("2026-10-01", zh["来源时间"])
        self.assertIn("2026-10-13", zh["来源时间"])
        self.assertIn("日期", zh["来源时间"])
        # A date range must not borrow the instant-range sentence, which claims
        # Beijing time for both ends.
        self.assertNotIn("北京时间", zh["来源时间"])

    def test_the_english_word_for_a_date_only_statement(self):
        self.seed_with(
            [support.raw_record(data_id="d3")],
            dict(self.PROVENANCE, observed_at="2026-10-13"),
        )
        _summary, lines = self.lines("en")
        self.assertIn("2026-10-13", lines["Source time"])
        self.assertIn("date only", lines["Source time"])

    def test_an_unreadable_time_is_not_dressed_up_as_one(self):
        # Anything that is neither an instant nor a date stays "not stated":
        # guessing at "2026-10-13-ish" would be worse than saying nothing.
        self.seed_with(
            [support.raw_record(data_id="d4")],
            dict(self.PROVENANCE, observed_at="10月13日"),
        )
        _summary, lines = self.lines()
        self.assertEqual(lines["来源时间"], "来源未给出时间")

    def test_the_page_helper_returns_exactly_the_workbook_lines(self):
        # 026 P3.5: the run page and the workbook both print this section, so
        # they must not be two translations of one fact.
        self.seed_with([support.raw_record(data_id="h1")], self.PROVENANCE)
        summary = export.acquisition_summary(self.conn, self.run_id, self.cfg)
        self.assertEqual(
            export.acquisition_lines(self.conn, self.run_id, self.cfg, "zh"),
            export._acquisition_lines(summary, export.LANG_SPECS["zh"]),
        )

    def test_the_section_reaches_the_overview_sheet(self):
        import openpyxl

        self.seed_with([support.raw_record(data_id="o1")], self.PROVENANCE)
        cfg = support.config({"export": {"fields": EXPORT_FIELDS, "formats": ["xlsx"],
                                        "languages": ["zh"]}})
        paths = export.export_run(self.conn, self.run_id, cfg, self.out_dir)
        sheet = openpyxl.load_workbook(paths["xlsx_zh"])["概览"]
        text = "\n".join(
            str(sheet.cell(row=r, column=c).value)
            for r in range(1, sheet.max_row + 1)
            for c in (1, 2)
            if sheet.cell(row=r, column=c).value is not None
        )
        self.assertIn("本批实际获取方式", text)
        self.assertIn("实时抓取", text)
        self.assertIn("来源时间", text)


class KeptApartPairTest(ExportTestCase):
    """056: a duplicate kept as two rows is explained, not left to be noticed.

    The merge stage refuses to combine two records that share a name and a phone
    but sit at different addresses -- the branch rule. When that happens the
    reader sees the same company twice, so the delivery has to say which rows
    are the pair and why they were not merged.
    """

    NAME = "\u676d\u5dde\u65b9\u7965\u533b\u7597\u5668\u68b0\u6709\u9650\u516c\u53f8"

    def setUp(self):
        super().setUp()
        self.cfg = support.config(
            {
                "collect": {"fields": ["title", "address", "phone"]},
                "export": {
                    "fields": EXPORT_FIELDS,
                    "formats": ["xlsx"],
                    "languages": ["zh", "en"],
                },
            }
        )

    def two_rows_one_phone(self):
        self.ingest([
            support.raw_record(
                data_id="c10352910", title=self.NAME, phone="571 85181608",
                address="\u6d59\u6c5f\u7701\u676d\u5dde\u5e02\u6c5f\u5e72\u533a\u4e5d\u73af\u8def48\u53f7",
            ),
            support.raw_record(
                data_id="c10356845", title=self.NAME, phone="571 85181608",
                address="\u6d59\u6c5f\u7701\u676d\u5dde\u5e02\u6c5f\u5e72\u533a\u4e5d\u73af\u8def4\u53f7",
            ),
        ])
        from collector import merge

        merge.merge_run(self.conn, self.run_id, self.cfg)
        return export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)

    def test_the_reader_overview_names_the_pair(self):
        import openpyxl

        paths = self.two_rows_one_phone()
        sheet = openpyxl.load_workbook(paths["xlsx_zh"])["\u6982\u89c8"]
        rows = [
            (sheet.cell(row=r, column=1).value, sheet.cell(row=r, column=2).value)
            for r in range(1, sheet.max_row + 1)
        ]
        # 059: the label names a *conflict*, and the sentence must not claim
        # what the records cannot show -- "two branches" is a guess, so the
        # line says "suspected" and "not confirmed" instead.
        line = [value for label, value in rows if label == "\u540c\u540d\u540c\u7535\u8bdd\u3001\u5730\u5740\u51b2\u7a81"]
        self.assertEqual(len(line), 1, rows)
        self.assertIn("1 \u5bf9", str(line[0]))
        self.assertIn("\u672a\u81ea\u52a8\u5408\u5e76", str(line[0]))
        self.assertIn("\u672a\u786e\u8ba4", str(line[0]))
        self.assertNotIn("\u95e8\u5e97/\u5206\u652f", str(line[0]))

    def test_the_overview_line_is_absent_when_no_pair_was_kept_apart(self):
        # A line every batch carries regardless of what happened is boilerplate;
        # this one is a fact about this batch.
        self.ingest([support.raw_record()])
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        import openpyxl

        sheet = openpyxl.load_workbook(paths["xlsx_zh"])["\u6982\u89c8"]
        labels = [sheet.cell(row=r, column=1).value for r in range(1, sheet.max_row + 1)]
        self.assertNotIn("\u540c\u540d\u540c\u7535\u8bdd\u3001\u5730\u5740\u51b2\u7a81", labels)

    def test_the_technical_file_lists_both_halves_of_the_pair(self):
        import openpyxl

        paths = self.two_rows_one_phone()
        sheet = openpyxl.load_workbook(paths["xlsx"])["\u5931\u8d25\u4e0e\u51b2\u7a81"]
        text = "\n".join(
            str(cell)
            for row in sheet.iter_rows(values_only=True)
            for cell in row if cell is not None
        )
        self.assertIn("\u540c\u540d\u540c\u7535\u8bdd\u4f46\u5730\u5740\u51b2\u7a81", text)
        self.assertIn("\u672a\u786e\u8ba4", text)
        # Both street numbers, and both records' keys, are in the sheet.
        self.assertIn("\u4e5d\u73af\u8def48\u53f7", text)
        self.assertIn("\u4e5d\u73af\u8def4\u53f7", text)
        self.assertIn("c10352910", text)
        self.assertIn("c10356845", text)

    def test_the_english_workbook_says_it_in_english(self):
        import openpyxl

        paths = self.two_rows_one_phone()
        sheet = openpyxl.load_workbook(paths["xlsx_en"])["Overview"]
        text = "\n".join(
            str(sheet.cell(row=r, column=2).value)
            for r in range(1, sheet.max_row + 1)
        )
        self.assertIn("two rows", text)
        self.assertIn("Not merged", text)
        # 059: the English line carries the same hedge -- "suspected",
        # "not confirmed" -- rather than asserting two branches.
        self.assertIn("not confirmed", text)


class SingleSourceCounting061Test(ExportTestCase):
    """061: one source cannot produce a *cross-source* merge.

    A real single-source run folded two rows of the same Google result set into
    one -- the source had listed one business twice -- and the overview printed
    it as "跨源合并 1", which credits a second source that never took part.
    """

    def setUp(self):
        super().setUp()
        self.cfg = support.config(
            {
                "collect": {"fields": ["title", "address", "phone", "review_count"]},
                "export": {
                    "fields": EXPORT_FIELDS,
                    "formats": ["xlsx"],
                    "languages": ["zh", "en"],
                },
            }
        )

    def overview_text(self, paths, sheet_name):
        import openpyxl

        sheet = openpyxl.load_workbook(paths["xlsx_zh"])[sheet_name]
        return "\n".join(
            str(sheet.cell(row=r, column=c).value)
            for r in range(1, sheet.max_row + 1)
            for c in range(1, sheet.max_column + 1)
            if sheet.cell(row=r, column=c).value is not None
        )

    def test_a_single_source_run_does_not_claim_a_cross_source_merge(self):
        self.ingest([support.raw_record(data_id="o1")])
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        text = self.overview_text(paths, "\u6982\u89c8")
        self.assertIn("\u540c\u4e00\u6765\u6e90\u5185\u5408\u5e76", text)
        self.assertNotIn("\u8de8\u6e90\u5408\u5e76", text)

    def test_the_review_note_no_longer_says_fast_mode_never_returns_counts(self):
        # A real fast-mode run returned 344, 37, 71 ... for 19 of 19 rows; the
        # old sentence told the reader those were placeholders.
        self.ingest([support.raw_record(data_id="o1", review_count=344)])
        paths = export.export_run(self.conn, self.run_id, self.cfg, self.out_dir)
        text = self.overview_text(paths, "\u6982\u89c8")
        self.assertIn("\u8bc4\u8bba\u6570", text)
        self.assertNotIn("\u5feb\u901f\u6a21\u5f0f\u4e0d\u8fd4\u56de\u8bc4\u8bba\u6570", text)


if __name__ == "__main__":
    unittest.main()
