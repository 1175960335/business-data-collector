"""SQLite schema, de-duplication and resume bookkeeping."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
import unittest.mock

from collector import db
from collector.errors import SchemaError

from . import support


def new_run(conn, fingerprint: str = "fp", target: int = 10) -> int:
    return db.create_run(
        conn,
        fingerprint=fingerprint,
        config_json="{}",
        config_source="<test>",
        collector_version="0.0.0",
        schema_version=1,
        go_binary="stub",
        go_version=None,
        target_count=target,
    )


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)

    def test_schema_is_created_and_version_recorded(self):
        conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(conn.close)
        counts = db.table_counts(conn)
        for table in ("runs", "stages", "provider_tasks", "observations", "entities",
                      "entity_observations", "field_facts", "errors", "checkpoints",
                      "site_candidates"):
            self.assertIn(table, counts)
            self.assertEqual(counts[table], 0)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_opening_a_newer_database_fails_loudly(self):
        path = self.tmp / "newer.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version=99")
        conn.commit()
        conn.close()

        with self.assertRaises(SchemaError) as ctx:
            db.connect(path)
        self.assertIn("schema generation 99", str(ctx.exception))
        self.assertIn("refusing to read it", str(ctx.exception))

    def test_reopening_is_idempotent(self):
        path = self.tmp / "c.sqlite"
        first = db.connect(path)
        new_run(first, "fp-a")
        first.close()

        second = db.connect(path)
        self.addCleanup(second.close)
        self.assertEqual(db.table_counts(second)["runs"], 1)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)

    def test_latest_resumable_run_ignores_finished_runs(self):
        run_id = new_run(self.conn, "fp-a")
        self.assertIsNotNone(db.latest_resumable_run(self.conn, "fp-a"))

        db.finish_run(self.conn, run_id, "completed")
        self.assertIsNone(db.latest_resumable_run(self.conn, "fp-a"))
        self.assertIsNotNone(db.latest_run(self.conn, "fp-a"))

    def test_resumable_lookup_is_scoped_to_the_fingerprint(self):
        new_run(self.conn, "fp-a")
        self.assertIsNone(db.latest_resumable_run(self.conn, "fp-b"))

    def test_orphan_running_tasks_are_recovered(self):
        run_id = new_run(self.conn)
        task_id = db.upsert_provider_task(
            self.conn, run_id=run_id, provider="google_maps", stage="discover",
            label="t1", query="q", argv=["stub"], status="running",
        )
        self.assertEqual(db.recover_orphan_tasks(self.conn, run_id), 1)
        row = db.get_provider_task(self.conn, run_id, "t1")
        self.assertEqual(row["status"], "interrupted")
        self.assertIn("orchestrator process ended", row["error"])
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(db.recover_orphan_tasks(self.conn, run_id), 0)
        self.assertEqual(task_id, row["id"])


class ObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)
        self.run_id = new_run(self.conn)

    def _upsert(self, payload, query="q1"):
        from collector.util import canonical_json, content_hash

        return db.upsert_observation(
            self.conn,
            provider="google_maps",
            payload=payload,
            raw_json=canonical_json(payload),
            content_hash=content_hash(payload),
            mode="fast",
            query=query,
            source_url="https://example.invalid",
            scraped_at=None,
            record_schema="osint.record/1",
            missing=[],
            unknown=[],
            raw_path="raw.jsonl",
            raw_line=1,
            run_id=self.run_id,
        )

    def test_identical_payloads_are_stored_once_and_counted(self):
        payload = support.raw_record()
        first_id, created = self._upsert(payload)
        self.assertTrue(created)

        second_id, created = self._upsert(payload)
        self.assertFalse(created)
        self.assertEqual(first_id, second_id)

        row = self.conn.execute("SELECT * FROM observations").fetchone()
        self.assertEqual(row["seen_count"], 2)
        self.assertIsNotNone(row["last_seen_at"])

    def test_a_changed_payload_is_a_new_observation(self):
        self._upsert(support.raw_record(review_count=10))
        _, created = self._upsert(support.raw_record(review_count=11))
        self.assertTrue(created)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)


class EntityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)

    def _upsert(self, **overrides):
        values = {
            "stable_key": "data_id:x1",
            "id_kind": "data_id",
            "name": "Praxis",
            "name_norm": "praxis",
            "address": "Musterstra\u00dfe 1",
            "address_norm": "musterstra\u00dfe 1",
            "latitude": 52.52,
            "longitude": 13.40,
            "phone_norm": "+493012345",
            "website": "https://a.example.de",
            "domain": "a.example.de",
            "country": "DE",
        }
        values.update(overrides)
        return db.upsert_entity(self.conn, **values)

    def test_same_stable_key_is_one_entity(self):
        first = self._upsert()
        second = self._upsert()
        self.assertEqual(first, second)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1)

    def test_empty_values_do_not_overwrite_existing_evidence(self):
        self._upsert()
        self._upsert(name=None, address=None, website=None, domain=None, phone_norm=None,
                     latitude=None, longitude=None)

        row = self.conn.execute("SELECT * FROM entities").fetchone()
        self.assertEqual(row["name"], "Praxis")
        self.assertEqual(row["address"], "Musterstra\u00dfe 1")
        self.assertEqual(row["domain"], "a.example.de")
        self.assertEqual(row["latitude"], 52.52)

    def test_missing_values_are_filled_in_later(self):
        self._upsert(address=None, address_norm=None)
        self._upsert(address="Neue Stra\u00dfe 2", address_norm="neue stra\u00dfe 2")
        row = self.conn.execute("SELECT address FROM entities").fetchone()
        self.assertEqual(row["address"], "Neue Stra\u00dfe 2")


class FactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)
        self.run_id = new_run(self.conn)
        self.entity_id = db.upsert_entity(
            self.conn, stable_key="data_id:x1", id_kind="data_id", name="Praxis",
            name_norm="praxis", address=None, address_norm=None, latitude=None,
            longitude=None, phone_norm=None, website=None, domain=None, country="DE",
        )
        # Facts are scoped to a run through the entities it observed, so the
        # entity needs at least one observation in this run.
        from collector.util import canonical_json, content_hash

        payload = support.raw_record()
        self.observation_id, _ = db.upsert_observation(
            self.conn, provider="google_maps", payload=payload,
            raw_json=canonical_json(payload), content_hash=content_hash(payload),
            mode="fast", query="q", source_url=None, scraped_at=None,
            record_schema=None, missing=[], unknown=[], raw_path="raw.jsonl",
            raw_line=1, run_id=self.run_id,
        )
        db.link_entity_observation(
            self.conn, entity_id=self.entity_id, observation_id=self.observation_id,
            run_id=self.run_id, query="q",
        )

    def _insert(self, **overrides):
        values = {
            "entity_id": self.entity_id,
            "observation_id": self.observation_id,
            "run_id": self.run_id,
            "field": "phone",
            "raw_value": "+49 30 12345678",
            "normalized_value": "+493012345678",
            "provider": "google_maps",
            "source_url": "https://example.invalid",
            "query_context": "q",
            "scraped_at": None,
            "evidence_snippet": "+49 30 12345678",
            "evidence_ref": "raw.jsonl#L1",
            "extractor_version": "gmaps-entry/1",
            "validation_status": "valid",
            "validation_note": None,
            "observed_at": "2026-10-12T00:00:00.000000Z",
            "fact_key": "k1",
        }
        values.update(overrides)
        return db.insert_fact(self.conn, **values)

    def test_identical_facts_are_not_duplicated(self):
        self.assertTrue(self._insert())
        self.assertFalse(self._insert())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM field_facts").fetchone()[0], 1)

    def test_a_changed_value_becomes_a_new_fact(self):
        self._insert()
        self.assertTrue(self._insert(raw_value="+49 30 99999999", fact_key="k2"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM field_facts").fetchone()[0], 2)

    def test_latest_facts_picks_the_newest_observation(self):
        self._insert(fact_key="old", observed_at="2026-10-12T00:00:00.000000Z", raw_value="old")
        self._insert(fact_key="new", observed_at="2026-10-12T01:00:00.000000Z", raw_value="new")
        latest = db.latest_facts(self.conn, self.run_id)
        self.assertEqual(latest[(self.entity_id, "phone")]["raw_value"], "new")


class CheckpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)
        self.run_id = new_run(self.conn)

    def test_checkpoint_round_trip_and_overwrite(self):
        self.assertIsNone(db.get_checkpoint(self.conn, self.run_id, "ingest", "file:a"))
        db.set_checkpoint(self.conn, self.run_id, "ingest", "file:a", 7)
        self.assertEqual(db.get_checkpoint(self.conn, self.run_id, "ingest", "file:a"), 7)
        db.set_checkpoint(self.conn, self.run_id, "ingest", "file:a", 9)
        self.assertEqual(db.get_checkpoint(self.conn, self.run_id, "ingest", "file:a"), 9)
        self.assertEqual(len(db.list_checkpoints(self.conn, self.run_id, "ingest")), 1)

    def test_stage_records_timestamps(self):
        db.set_stage(self.conn, self.run_id, "discover", "running", touch_started=True)
        row = db.get_stage(self.conn, self.run_id, "discover")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNone(row["finished_at"])

        db.set_stage(self.conn, self.run_id, "discover", "completed", touch_finished=True)
        row = db.get_stage(self.conn, self.run_id, "discover")
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["finished_at"])


class ObservationProvenanceTest(unittest.TestCase):
    """Round 026 P0: how a record was obtained is stored, not inferred."""

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
            target_count=1,
        )

    def store(self, **overrides):
        from collector.util import canonical_json, content_hash

        payload = support.raw_record(**overrides.pop("payload", {}))
        kwargs = {
            "provider": "csv_list",
            "payload": payload,
            "raw_json": canonical_json(payload),
            "content_hash": content_hash(payload),
            "mode": "csv_file",
            "query": "q",
            "source_url": "/local/list.csv",
            "scraped_at": None,
            "record_schema": None,
            "missing": [],
            "unknown": [],
            "raw_path": "raw.jsonl",
            "raw_line": 1,
            "run_id": self.run_id,
        }
        kwargs.update(overrides)
        return db.upsert_observation(self.conn, **kwargs)

    def test_the_acquisition_method_and_page_are_stored(self):
        observation_id, created = self.store(
            acquired_via="import",
            origin="www.11467.com",
            source_page_url="https://www.11467.com/hangzhou/search/9092.htm",
            detail_url="https://www.11467.com/qiye/1.htm",
            snapshot_id="listing.html@abcdef12",
            observed_at="2026-10-13T21:11:21+08:00",
            input_path="/local/list.csv",
        )
        self.assertTrue(created)
        row = self.conn.execute(
            "SELECT * FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        self.assertEqual(row["acquired_via"], "import")
        self.assertEqual(row["origin"], "www.11467.com")
        self.assertEqual(
            row["source_page_url"], "https://www.11467.com/hangzhou/search/9092.htm"
        )
        self.assertEqual(row["detail_url"], "https://www.11467.com/qiye/1.htm")
        self.assertEqual(row["snapshot_id"], "listing.html@abcdef12")
        self.assertEqual(row["observed_at"], "2026-10-13T21:11:21+08:00")
        self.assertEqual(row["input_path"], "/local/list.csv")

    def test_an_unstated_detail_is_not_marked_as_fetched(self):
        observation_id, _ = self.store(acquired_via="import")
        row = self.conn.execute(
            "SELECT detail_fetched, detail_url, observed_at FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
        self.assertIsNone(row["detail_fetched"])
        self.assertIsNone(row["detail_url"])
        self.assertIsNone(row["observed_at"])

    def test_entity_provenance_reports_what_was_recorded(self):
        observation_id, _ = self.store(
            acquired_via="import",
            origin="www.11467.com",
            source_page_url="https://www.11467.com/hangzhou/search/9092.htm",
        )
        entity_id = db.upsert_entity(
            self.conn,
            stable_key="csv_list:csv_row:row-1",
            id_kind="csv_list:csv_row",
            name="杭州猫氧宠物服务有限公司",
            name_norm=None,
            address=None,
            address_norm=None,
            latitude=None,
            longitude=None,
            phone_norm=None,
            website=None,
            domain=None,
            country="CN",
        )
        db.link_entity_observation(
            self.conn,
            entity_id=entity_id,
            observation_id=observation_id,
            run_id=self.run_id,
            query="q",
        )
        prov = db.entity_provenance(self.conn, entity_id)
        self.assertEqual(prov["acquired_via"], ["import"])
        self.assertEqual(
            prov["source_page_urls"], ["https://www.11467.com/hangzhou/search/9092.htm"]
        )
        self.assertEqual(prov["origins"], ["www.11467.com"])
        self.assertFalse(prov["detail_fetched"])
        self.assertEqual(prov["detail_urls"], [])
        self.assertEqual(prov["observed_ats"], [])


class SiteCandidateTest(unittest.TestCase):
    """Round 026 P1: a proposed site and the verdict on it are both stored."""

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
            target_count=1,
        )
        self.entity_id = db.upsert_entity(
            self.conn,
            stable_key="place:1",
            id_kind="place_id",
            name="Praxis Dr. M\u00fcller",
            name_norm="praxis dr. m\u00fcller",
            address="Musterstra\u00dfe 1, 10115 Berlin",
            address_norm="musterstrasse 1 10115 berlin",
            latitude=None,
            longitude=None,
            phone_norm="+493012345678",
            website=None,
            domain=None,
            country="DE",
        )

    def record(self, **overrides):
        kwargs = {
            "run_id": self.run_id,
            "entity_id": self.entity_id,
            "url": "https://praxis-mueller.de/",
            "host": "praxis-mueller.de",
            "found_by": "user_list",
            "origin": "operator-supplied",
            "status": "pending",
            "reason": "only_the_name_agrees",
            "note": "name only",
            "evidence": {"page_name": "Praxis Dr. M\u00fcller"},
            "http_status": 200,
        }
        kwargs.update(overrides)
        return db.upsert_site_candidate(self.conn, **kwargs)

    def test_a_candidate_and_its_verdict_are_stored(self):
        self.assertTrue(self.record())
        row = self.conn.execute("SELECT * FROM site_candidates").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["found_by"], "user_list")
        self.assertEqual(row["origin"], "operator-supplied")
        self.assertEqual(row["http_status"], 200)
        self.assertEqual(row["host"], "praxis-mueller.de")
        # Both halves matter: what was proposed *and* why it was not accepted.
        self.assertEqual(json.loads(row["evidence_json"])["page_name"], "Praxis Dr. M\u00fcller")
        self.assertTrue(row["judged_at"])

    def test_re_judging_with_no_change_writes_nothing(self):
        self.assertTrue(self.record())
        # A resumed run must not re-count a verdict or re-bind a website.
        self.assertFalse(self.record())

    def test_re_judging_with_a_change_updates_the_one_row(self):
        self.record()
        self.assertTrue(self.record(status="confirmed", reason="confirmed_by_name_and_phone"))
        rows = list(db.iter_site_candidates(self.conn, self.run_id))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "confirmed")

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(ValueError):
            self.record(status="maybe")

    def test_the_counts_add_up_and_name_every_verdict(self):
        self.record(url="https://a.example/", status="confirmed")
        self.record(url="https://b.example/", status="pending")
        self.record(url="https://c.example/", status="rejected")
        self.record(url="https://d.example/", status="error")
        counts = db.site_candidate_counts(self.conn, self.run_id)
        # A verdict that never occurred is reported as zero rather than omitted.
        self.assertEqual(
            counts,
            {"candidates": 4, "confirmed": 1, "pending": 1, "rejected": 1, "errors": 1},
        )

    def test_counts_are_zero_on_an_empty_run(self):
        self.assertEqual(
            db.site_candidate_counts(self.conn, self.run_id),
            {"candidates": 0, "confirmed": 0, "pending": 0, "rejected": 0, "errors": 0},
        )

    def test_the_by_finder_breakdown_groups_by_who_proposed_it(self):
        self.record(url="https://a.example/", found_by="user_list", status="confirmed")
        self.record(url="https://b.example/", found_by="page_links", status="pending")
        self.record(url="https://c.example/", found_by="user_list", status="rejected")
        rows = {row["finder"]: row for row in db.site_candidate_finders(self.conn, self.run_id)}
        self.assertEqual(rows["user_list"]["candidates"], 2)
        self.assertEqual(rows["user_list"]["confirmed"], 1)
        self.assertEqual(rows["user_list"]["rejected"], 1)
        self.assertEqual(rows["page_links"]["candidates"], 1)
        # A finder that proposed nothing is absent, so "found none" and "was not
        # asked" stay distinguishable against the declared finder list.
        self.assertNotIn("search", rows)

    def test_the_website_is_filled_only_when_the_listing_left_it_empty(self):
        self.assertTrue(db.fill_entity_website(self.conn, self.entity_id, "https://praxis-mueller.de/"))
        row = self.conn.execute(
            "SELECT website, domain FROM entities WHERE id = ?", (self.entity_id,)
        ).fetchone()
        self.assertEqual(row["website"], "https://praxis-mueller.de/")
        self.assertEqual(row["domain"], "praxis-mueller.de")
        # A second, different inference never overrules the first.
        self.assertFalse(db.fill_entity_website(self.conn, self.entity_id, "https://other.example/"))
        row = self.conn.execute(
            "SELECT website FROM entities WHERE id = ?", (self.entity_id,)
        ).fetchone()
        self.assertEqual(row["website"], "https://praxis-mueller.de/")

    def test_a_listed_website_is_never_replaced_by_an_inference(self):
        self.conn.execute(
            "UPDATE entities SET website = ?, domain = ? WHERE id = ?",
            ("https://listed.example/", "listed.example", self.entity_id),
        )
        self.assertFalse(
            db.fill_entity_website(self.conn, self.entity_id, "https://inferred.example/")
        )
        row = self.conn.execute(
            "SELECT website FROM entities WHERE id = ?", (self.entity_id,)
        ).fetchone()
        self.assertEqual(row["website"], "https://listed.example/")

    def test_a_blank_website_counts_as_empty(self):
        self.conn.execute("UPDATE entities SET website = '' WHERE id = ?", (self.entity_id,))
        self.assertTrue(
            db.fill_entity_website(self.conn, self.entity_id, "https://inferred.example/")
        )


if __name__ == "__main__":
    unittest.main()


class ConnectRetryTest(unittest.TestCase):
    """026 P3.5: opening a shared database survives a transient refusal.

    The screen, the run it starts and any terminal command all open this file,
    and WAL lets them write at once. Right after a run is killed there is a
    window where an opener can be told the database is locked — or, as observed
    once in the bat end-to-end run, read-only — while the file itself is fine.
    Retrying is what keeps a half-second hiccup from taking a page down; a
    persistent refusal must still surface.
    """

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.path = self.tmp / "runs" / "collector.sqlite"

    def test_a_transient_refusal_is_retried_and_then_succeeds(self):
        from unittest import mock

        real = db.ensure_schema
        calls: list[int] = []

        def flaky(conn, generation=None):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return real(conn, generation)

        with mock.patch.object(db, "ensure_schema", flaky):
            conn = db.connect(self.path)
        try:
            self.assertEqual(len(calls), 2)
            # And the database really works afterwards.
            from collector.version import SCHEMA_VERSION

            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )
        finally:
            conn.close()

    def test_a_persistent_refusal_is_raised_not_swallowed(self):
        from unittest import mock

        calls: list[int] = []

        def always(conn, generation=None):
            calls.append(1)
            raise sqlite3.OperationalError("attempt to write a readonly database")

        with mock.patch.object(db, "ensure_schema", always):
            with self.assertRaises(sqlite3.OperationalError):
                db.connect(self.path)
        self.assertEqual(len(calls), db._OPEN_ATTEMPTS)

    def test_a_database_from_the_future_is_not_retried(self):
        from unittest import mock

        calls: list[int] = []

        def future(conn, generation=None):
            calls.append(1)
            raise SchemaError("database was written by schema generation 99")

        with mock.patch.object(db, "ensure_schema", future):
            with self.assertRaises(SchemaError):
                db.connect(self.path)
        # A refusal that is a fact about the file is answered at once: retrying
        # it would only make the message slower to arrive.
        self.assertEqual(len(calls), 1)

    def test_a_rejected_database_leaves_no_handle_behind(self):
        # Windows keeps the file locked while a handle is open, which is what
        # made the original error unactionable. The failed attempts must close.
        from unittest import mock

        def always(conn, generation=None):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        with mock.patch.object(db, "ensure_schema", always):
            with self.assertRaises(sqlite3.OperationalError):
                db.connect(self.path)
        # Being able to delete it is the observable form of "no handle left".
        self.path.unlink()


class StaleWriteLockTest(unittest.TestCase):
    """A write told "database is locked" waits instead of failing.

    052: the enrich stage's ``insert_fact`` was refused, and the handler that
    would have recorded the failure -- ``set_stage(..., "failed")`` -- was
    refused by the same lock, so the run ended with no terminal state at all.
    ``connect`` already sets ``busy_timeout`` to ten seconds, and ten seconds
    was not enough: that is the whole reason this class exists.

    The lock is taken by a real second connection and held for longer than the
    real busy timeout, so the only thing that can make the write succeed is the
    outer retry in ``db.write``. The retry budget itself is scaled down so the
    suite stays quick -- the *relation* is what matters (retry > busy_timeout),
    and it is asserted rather than assumed.

    Writing a hang here would be worse than the bug, so a cap is asserted on the
    total wait.
    """

    def setUp(self):
        self._holder_thread: threading.Thread | None = None
        self.tmp = support.temp_dir(self)
        self.path = self.tmp / "c.sqlite"
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.run_id = new_run(self.conn)
        # The scaled-down retry: still far longer than the 10 s busy timeout,
        # short enough that the suite pays for it once.
        self._pause = unittest.mock.patch.object(db, "_WRITE_PAUSE_SECONDS", 0.25)
        self._attempts = unittest.mock.patch.object(db, "_WRITE_ATTEMPTS", 120)
        self._pause.start()
        self._attempts.start()
        self.addCleanup(self._pause.stop)
        self.addCleanup(self._attempts.stop)

    def _hold_the_database(self, seconds: float) -> threading.Thread:
        """A real second writer, holding the database for ``seconds``."""
        path = self.path
        run_id = self.run_id

        def hold():
            # The connection is created *inside* the thread. ``sqlite3`` defaults
            # to ``check_same_thread=True``, so a connection opened in ``setUp``
            # and used here raises ``ProgrammingError`` on its first statement --
            # which is one way this test used to "prove nothing": the holder died
            # instantly and held nothing at all.
            holder = db.connect(path)
            try:
                # An INSERT opens the transaction implicitly (the same thing
                # ``db.write`` does), so the write lock is held until the commit.
                # An explicit ``BEGIN IMMEDIATE`` here collides with Python's own
                # transaction control and never takes the lock at all. ``stages``
                # is the table the 052 cascade was writing, so the lock is taken
                # on exactly the write that was refused.
                holder.execute(
                    "INSERT INTO stages (run_id, stage, status) VALUES (?, 'holder', 'working')",
                    (run_id,),
                )
                time.sleep(seconds)
                holder.commit()
            finally:
                holder.close()

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        # Only return once the holder actually owns the write lock: otherwise the
        # test could pass because the write arrived before the lock did.
        #
        # The probe deliberately does *not* use ``db.connect``: that sets
        # ``busy_timeout`` to ten seconds, and a probe that waits ten seconds
        # simply sits out the holder and then succeeds -- it can never observe
        # the refusal this class exists for. A near-zero busy timeout turns the
        # question into "does anyone hold the write lock right now", which is
        # exactly what needs proving, and answers it in milliseconds.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            probe = sqlite3.connect(str(self.path))
            try:
                probe.execute("PRAGMA busy_timeout=50")
                probe.execute(
                    "INSERT INTO stages (run_id, stage, status) VALUES (?, 'probe', 'working')",
                    (self.run_id,),
                )
                probe.rollback()  # got the lock: the holder has not started yet
            except sqlite3.OperationalError:
                break  # refused: the holder owns it
            finally:
                probe.close()
            time.sleep(0.02)
        else:
            self.fail("the holder never took the write lock, so this test proves nothing")

        # Registered *after* setUp's cleanups, and cleanups run in reverse
        # registration order, so the holder is joined (and its file handle, which
        # its own ``finally`` closes, released) before the temporary directory is
        # removed. Without this the holder outlives the test body on Windows and
        # the removal of ``c.sqlite`` is refused -- a cleanup error that has
        # nothing to do with what is being tested.
        self._holder_thread = thread
        self.addCleanup(self._join_holder)
        return thread

    def _join_holder(self):
        thread = getattr(self, "_holder_thread", None)
        if thread is not None:
            thread.join(60)
        self.assertFalse(
            bool(thread) and thread.is_alive(),
            "the holder thread never finished, so the lock it holds is a leak",
        )

    def test_a_write_waits_out_a_lock_held_past_the_busy_timeout(self):
        self._hold_the_database(12.0)  # > PRAGMA busy_timeout of 10 s
        started = time.monotonic()
        db.set_stage(self.conn, self.run_id, "discover", "completed", touch_finished=True)
        waited = time.monotonic() - started

        row = self.conn.execute(
            "SELECT status FROM stages WHERE run_id = ? AND stage = 'discover'", (self.run_id,)
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        # It waited rather than raising -- that is the whole point. The precise
        # bound proves it recovered without sitting through the whole budget.
        self.assertGreaterEqual(waited, 10.0)
        self.assertLess(waited, db._WRITE_ATTEMPTS * db._WRITE_PAUSE_SECONDS)

    def test_the_failure_recorder_survives_a_lock_so_a_terminal_state_lands(self):
        """The cascade: a stage unable to record its own failure.

        The lock is still held when the orchestrator's handler would mark the
        failure, so a terminal state is only reachable by waiting. If this
        regresses, the run is left claiming ``running`` forever -- the outcome
        the run state machine exists to prevent.
        """
        self._hold_the_database(12.0)
        # The orchestrator's handler, verbatim in shape.
        db.set_stage(self.conn, self.run_id, "enrich", "failed",
                     error="enrich: sqlite3.OperationalError: database is locked",
                     touch_finished=True)
        db.finish_run(self.conn, self.run_id, "failed", note="enrich refused")

        run = self.conn.execute("SELECT status FROM runs WHERE id = ?", (self.run_id,)).fetchone()
        stage = self.conn.execute(
            "SELECT status FROM stages WHERE run_id = ? AND stage = 'enrich'", (self.run_id,)
        ).fetchone()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(stage["status"], "failed")

    def test_a_refusal_that_is_not_about_locking_raises_at_once(self):
        # Retrying must not swallow a real failure: a missing column or a bad
        # path is a fact about the statement, and re-running it 16 times only
        # makes the message slower to arrive.
        with unittest.mock.patch.object(db, "_WRITE_PAUSE_SECONDS", 0.0):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                db.write(self.conn, "SELECT * FROM no_such_table")
        self.assertIn("no_such_table", str(caught.exception))


class ReadSnapshotTest(unittest.TestCase):
    """053: the iteration a stage writes inside is a snapshot, not a cursor.

    ``enrich_run`` walks the entity list of one connection while its loop body
    writes facts. When that list was a live cursor, the connection carried an
    open read snapshot for the whole stage; the screen commits on every poll,
    so from the first such commit every write in the stage was refused
    *immediately* -- ``SQLITE_BUSY_SNAPSHOT``, reported as "database is
    locked". Waiting cannot clear it (that error does not wait for
    ``busy_timeout``) and neither can ``write``'s retry (a rollback does not
    close a statement that is still being iterated), so the run died after the
    retry budget with a ``failed`` status and no terminal stage work done.

    A local reproduction script reproduces the mechanism against the
    real schema: lazy cursor -> 75 s of refusals, materialised list -> a write
    in 2 ms. These tests hold that contract in place.
    """

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.path = self.tmp / "c.sqlite"
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.run_id = new_run(self.conn)
        self._seed()

    def _seed(self):
        from collector.util import canonical_json, content_hash

        for name in ("Anna", "Berta", "Carla"):
            entity_id = db.upsert_entity(
                self.conn, stable_key=f"data_id:{name.lower()}", id_kind="data_id", name=name,
                name_norm=name.lower(), address=None, address_norm=None, latitude=None,
                longitude=None, phone_norm=None, website=None, domain=None, country="DE",
            )
            payload = support.raw_record()
            observation_id, _ = db.upsert_observation(
                self.conn, provider="google_maps", payload=payload,
                raw_json=canonical_json(payload), content_hash=content_hash(payload),
                mode="fast", query="q", source_url=None, scraped_at=None,
                record_schema=None, missing=[], unknown=[], raw_path="raw.jsonl",
                raw_line=1, run_id=self.run_id,
            )
            db.link_entity_observation(
                self.conn, entity_id=entity_id, observation_id=observation_id,
                run_id=self.run_id, query="q",
            )
            db.set_checkpoint(self.conn, self.run_id, "seed", name, entity_id)

    def _entities(self):
        return {
            str(checkpoint_key): int(value)
            for checkpoint_key, value in (
                (row["key"], row["value_json"]) for row in db.list_checkpoints(
                    self.conn, self.run_id, "seed"
                )
            )
        }

    def test_the_entity_list_is_materialised_before_any_caller_can_write(self):
        rows = db.iter_run_entities(self.conn, self.run_id)

        self.assertIsInstance(rows, list, "a live cursor here is the 053 outage")
        self.assertEqual(
            [row["name"] for row in rows], ["Anna", "Berta", "Carla"],
            "materialising must not lose or duplicate rows",
        )

        # The observable point, and the reason the class exists: a write that
        # arrives while another connection has committed still lands. With a
        # live cursor this exact write is the refused one -- the snapshot the
        # cursor holds predates the commit, so it is refused immediately.
        other = db.connect(self.path)
        try:
            other.execute("PRAGMA user_version=1")  # what the screen does per poll
            other.commit()
        finally:
            other.close()

        started = time.monotonic()
        db.insert_fact(
            self.conn,
            entity_id=self._entities()["Anna"], observation_id=None, run_id=self.run_id,
            field="phone", raw_value="+49 30 1", normalized_value="+49301",
            provider="google_maps", source_url=None, query_context="q", scraped_at=None,
            evidence_snippet=None, evidence_ref=None, extractor_version="test/1",
            validation_status="valid", validation_note=None,
            observed_at="2026-10-15T00:00:00.000000Z", fact_key="snap-1",
        )
        self.assertLess(time.monotonic() - started, 2.0, "a snapshot write must not wait")

    def test_the_candidate_list_is_materialised_the_same_way(self):
        entity_id = self._entities()["Anna"]
        db.upsert_site_candidate(
            self.conn, run_id=self.run_id, entity_id=entity_id, url="https://a.example.de/",
            host="a.example.de", found_by="test", status="confirmed",
        )
        rows = db.iter_site_candidates(self.conn, self.run_id)
        self.assertIsInstance(rows, list, "same hazard as the entity list")
        self.assertEqual([row["url"] for row in rows], ["https://a.example.de/"])

    def test_a_live_cursor_still_stalls_a_write_so_the_fix_cannot_be_undone_quietly(self):
        """Pins the mechanism, not the helper.

        This is SQLite's behaviour under WAL, asserted so that anyone reaching
        for a lazy cursor again meets a red test that explains why: an open
        read statement plus another connection's commit refuses the next write
        immediately, whatever the retry budget. If this test ever *stops*
        failing, the database build changed -- re-read ``iter_run_entities``
        rather than deleting the test.
        """
        cursor = self.conn.execute("SELECT id FROM entities")
        cursor.fetchone()  # a statement mid-scan: exactly the enrich shape
        other = db.connect(self.path)
        try:
            other.execute("PRAGMA user_version=1")
            other.commit()
        finally:
            other.close()

        # Scaled down: the point is the immediate refusal and the message, not
        # sitting through the real 80 s budget to see them.
        with unittest.mock.patch.object(db, "_WRITE_ATTEMPTS", 3), \
                unittest.mock.patch.object(db, "_WRITE_PAUSE_SECONDS", 0.01):
            started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError) as caught:
                db.insert_fact(
                    self.conn,
                    entity_id=self._entities()["Berta"], observation_id=None,
                    run_id=self.run_id, field="phone", raw_value="+49 30 2",
                    normalized_value="+49302", provider="google_maps", source_url=None,
                    query_context="q", scraped_at=None, evidence_snippet=None,
                    evidence_ref=None, extractor_version="test/1",
                    validation_status="valid", validation_note=None,
                    observed_at="2026-10-15T00:00:00.000000Z", fact_key="snap-2",
                )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0, "the refusal is immediate; waiting is not the problem")
        message = str(caught.exception)
        self.assertIn("database is locked", message)
        self.assertIn("stale read snapshot", message,
                      "the failure must name its own cause, as 053's bare message did not")
        cursor.close()
