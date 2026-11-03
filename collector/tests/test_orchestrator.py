"""Orchestration: planning, resume, the stop target and failure reporting."""

from __future__ import annotations

import json
import pathlib
import unittest

from collector import db
from collector.errors import ResumeError
from collector.orchestrator import Orchestrator

from . import support
from .stub_site import Page, SiteServer


def fresh(overrides: dict | None = None):
    return support.config(overrides)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)

    def test_fast_mode_argv_has_no_depth_and_no_email_flag(self):
        orch = Orchestrator(fresh(), base_dir=self.tmp, adapter=support.stub_adapter())
        plan = orch.plan()
        argv = plan["tasks"][0]["argv"]
        self.assertIn("-fast-mode", argv)
        self.assertNotIn("-depth", argv)
        self.assertNotIn("-email", argv)
        self.assertIn("-json-records", argv)
        # Fast mode refuses to start without a search centre.
        self.assertEqual(argv[argv.index("-geo") + 1], "52.52,13.405")
        self.assertEqual(plan["fetch_flags"], [])

    def test_email_flag_is_added_only_when_emails_are_requested(self):
        cfg = fresh({"collect": {"fields": ["title", "emails"]}})
        orch = Orchestrator(cfg, base_dir=self.tmp, adapter=support.stub_adapter())
        plan = orch.plan()
        self.assertIn("-email", plan["tasks"][0]["argv"])

    def test_browser_mode_with_bbox_passes_grid_arguments(self):
        cfg = fresh({
            "collect": {"mode": "browser", "max_depth": 3},
            "location": {"bbox": [52.515, 13.385, 52.535, 13.415]},
        })
        orch = Orchestrator(cfg, base_dir=self.tmp, adapter=support.stub_adapter())
        argv = orch.plan()["tasks"][0]["argv"]
        self.assertNotIn("-fast-mode", argv)
        self.assertEqual(argv[argv.index("-depth") + 1], "3")
        self.assertEqual(argv[argv.index("-grid-bbox") + 1], "52.515,13.385,52.535,13.415")
        self.assertEqual(argv[argv.index("-grid-cell") + 1], "1.0")

    def test_arguments_are_passed_as_a_list_not_a_shell_string(self):
        cfg = fresh({"location": {"query": 'dentists "in" Berlin & Co'}})
        orch = Orchestrator(cfg, base_dir=self.tmp, adapter=support.stub_adapter())
        plan = orch.plan()
        self.assertIsInstance(plan["tasks"][0]["argv"], list)
        # The query is not an argv element: it is written to the queries file,
        # so no shell quoting is involved anywhere.
        self.assertNotIn('dentists "in" Berlin & Co', plan["tasks"][0]["argv"])

    def test_one_task_per_query(self):
        cfg = fresh({"location": {"query": None, "queries": ["a in Berlin", "b in Berlin"]}})
        orch = Orchestrator(cfg, base_dir=self.tmp, adapter=support.stub_adapter())
        labels = [task["label"] for task in orch.plan()["tasks"]]
        self.assertEqual(labels, ["discover-a-in-Berlin", "discover-b-in-Berlin"])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)

    def orchestrator(self, overrides=None, env=None):
        cfg = fresh(overrides)
        return cfg, Orchestrator(cfg, base_dir=self.tmp, adapter=support.stub_adapter(env))

    def test_google_rows_are_recorded_as_live_not_unknown(self):
        # 061: the stub writes the same envelope shape the Go scraper writes --
        # provider, mode, query, source_url, scraped_at and no provenance --
        # and those rows were published as "获取方式：未知" although this run
        # had just fetched them. The orchestrator now states the method for the
        # provider that produced the file.
        _, orch = self.orchestrator()
        result = orch.run()

        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT acquired_via FROM observations").fetchall()
        self.assertTrue(rows)
        self.assertEqual({row["acquired_via"] for row in rows}, {"live"})

    def test_full_run_writes_raw_database_and_exports(self):
        cfg, orch = self.orchestrator({"runtime": {"concurrency": 1}})
        result = orch.run()

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.collected, 3)
        self.assertEqual(result.shortfall, 47)

        raw_dir = pathlib.Path(result.run_dir) / "raw"
        self.assertEqual(len(list(raw_dir.glob("*.jsonl"))), 1)
        for path in result.exports.values():
            self.assertTrue(pathlib.Path(path).is_file(), path)

        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        self.assertEqual(db.table_counts(conn)["entities"], 3)
        self.assertEqual(db.table_counts(conn)["observations"], 3)
        self.assertGreater(db.table_counts(conn)["field_facts"], 0)
        self.assertEqual(db.get_stage(conn, result.run_id, "discover")["status"], "completed")
        self.assertEqual(db.get_stage(conn, result.run_id, "ingest")["status"], "completed")
        self.assertEqual(db.get_stage(conn, result.run_id, "export")["status"], "completed")

    def test_export_columns_follow_the_configuration(self):
        _, orch = self.orchestrator()
        result = orch.run()
        header = pathlib.Path(result.exports["csv"]).read_text(encoding="utf-8-sig").splitlines()[0]
        self.assertTrue(header.startswith("entity_id,stable_key,title,address,phone,website"))
        self.assertNotIn("emails", header)

    def test_repeat_run_does_not_duplicate_entities_and_keeps_new_evidence(self):
        _, first = self.orchestrator()
        first_result = first.run()

        _, second = self.orchestrator()
        second_result = second.run()

        self.assertNotEqual(first_result.run_id, second_result.run_id)
        self.assertEqual(second_result.status, "completed")

        conn = db.connect(pathlib.Path(second_result.database))
        self.addCleanup(conn.close)
        counts = db.table_counts(conn)
        self.assertEqual(counts["runs"], 2)
        # Same payloads: no new observations, no new entities.
        self.assertEqual(counts["entities"], 3)
        self.assertEqual(counts["observations"], 3)
        self.assertEqual(second_result.ingest_stats["entities_new"], 0)
        self.assertEqual(second_result.ingest_stats["observations_new"], 0)
        self.assertEqual(second_result.ingest_stats["observations_repeated"], 3)
        # The second run still sees all three businesses.
        self.assertEqual(second_result.collected, 3)

    def test_changed_evidence_from_the_provider_is_added(self):
        _, first = self.orchestrator()
        first.run()

        _, second = self.orchestrator(env={"STUB_VARIANT": "-v2"})
        result = second.run()

        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        # The stub's variant changes every payload, so the businesses are the
        # same but the evidence is new.
        self.assertEqual(db.table_counts(conn)["entities"], 3)
        self.assertEqual(result.ingest_stats["entities_new"], 0)
        self.assertEqual(result.ingest_stats["observations_new"], 3)

    def test_failed_task_marks_the_run_partial_and_records_the_reason(self):
        cfg, orch = self.orchestrator(env={"STUB_EXIT": "7"})
        result = orch.run()

        self.assertEqual(result.status, "failed")
        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        errors = db.list_errors(conn, result.run_id)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["kind"], "go_failure")
        self.assertIn("exit code 7", errors[0]["message"])
        task = db.list_provider_tasks(conn, result.run_id)[0]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["exit_code"], 7)

    def test_partial_status_when_one_of_two_queries_fails(self):
        overrides = {
            "location": {"query": None, "queries": ["a in Berlin", "b in Berlin"]},
            "target_count": 100,
        }
        _, orch = self.orchestrator(overrides, env={"STUB_FAIL_QUERY": "a in Berlin"})
        result = orch.run()

        # One query failed, the other still produced results: the run is partial,
        # not failed, and the failure keeps its reason.
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.collected, 3)
        self.assertIn("1 failed task(s)", result.stages["discover"])

        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        errors = db.list_errors(conn, result.run_id)
        self.assertEqual(len(errors), 1)
        self.assertIn("a in Berlin", errors[0]["message"])

    def test_target_count_skips_the_remaining_queries(self):
        overrides = {
            "location": {"query": None, "queries": ["a in Berlin", "b in Berlin"]},
            "target_count": 2,
        }
        _, orch = self.orchestrator(overrides)
        result = orch.run()

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.collected, 3)
        self.assertIn("target_count 2 reached", result.stages["discover"])

        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        statuses = {row["label"]: row["status"] for row in db.list_provider_tasks(conn, result.run_id)}
        self.assertEqual(statuses["discover-a-in-Berlin"], "completed")
        self.assertEqual(statuses["discover-b-in-Berlin"], "skipped")
        self.assertEqual(len(list((pathlib.Path(result.run_dir) / "raw").glob("*.jsonl"))), 1)

    def test_missing_scraper_binary_is_reported_not_crashed(self):
        cfg = fresh()
        orch = Orchestrator(cfg, base_dir=self.tmp, go_binary=self.tmp / "does-not-exist.exe")
        result = orch.run()
        self.assertEqual(result.status, "failed")
        self.assertTrue(any("not found" in note for note in result.notes))

    def test_shortfall_is_reported_when_the_sources_are_not_enough(self):
        _, orch = self.orchestrator({"target_count": 100})
        result = orch.run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.collected, 3)
        self.assertEqual(result.shortfall, 97)
        self.assertTrue(any("shortfall 97" in note for note in result.notes))


class BudgetTest(unittest.TestCase):
    """``runtime.budget`` must limit work, not merely be stored."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def orchestrator(self, overrides=None, env=None):
        return Orchestrator(fresh(overrides), base_dir=self.tmp,
                            adapter=support.stub_adapter(env))

    def test_request_budget_limits_the_number_of_tasks(self):
        overrides = {
            "location": {"query": None, "queries": ["a in Berlin", "b in Berlin", "c in Berlin"]},
            "target_count": 100,
            "runtime": {"budget": {"max_requests": 1, "max_seconds": 0}},
        }
        result = self.orchestrator(overrides).run()

        self.assertEqual(result.status, "completed")
        self.assertIn("request budget exhausted", result.stages["discover"])

        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        statuses = {row["label"]: row["status"] for row in db.list_provider_tasks(conn, result.run_id)}
        self.assertEqual(statuses["discover-a-in-Berlin"], "completed")
        self.assertEqual(statuses["discover-b-in-Berlin"], "skipped")
        self.assertEqual(statuses["discover-c-in-Berlin"], "skipped")
        self.assertEqual(len(list((pathlib.Path(result.run_dir) / "raw").glob("*.jsonl"))), 1)

    def test_no_budget_means_no_extra_limit(self):
        overrides = {
            "location": {"query": None, "queries": ["a in Berlin", "b in Berlin"]},
            "target_count": 100,
        }
        result = self.orchestrator(overrides).run()
        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        statuses = [row["status"] for row in db.list_provider_tasks(conn, result.run_id)]
        self.assertEqual(statuses, ["completed", "completed"])

    def test_time_budget_caps_the_task_timeout(self):
        overrides = {"runtime": {"timeout_seconds": 60, "budget": {"max_seconds": 1}}}
        result = self.orchestrator(overrides, env={"STUB_SLEEP": "20"}).run()

        self.assertEqual(result.status, "failed")
        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        errors = db.list_errors(conn, result.run_id)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["kind"], "go_timeout")
        self.assertIn("timeout", errors[0]["message"])
        task = db.list_provider_tasks(conn, result.run_id)[0]
        self.assertEqual(task["status"], "failed")

    def test_time_budget_skips_queries_once_it_is_spent(self):
        overrides = {
            "location": {"query": None, "queries": ["a in Berlin", "b in Berlin"]},
            "target_count": 100,
            # A one-second budget caps the first task's timeout at one second, so
            # by the time it is killed the budget is gone and the second query
            # must be skipped rather than started.
            "runtime": {"timeout_seconds": 60, "budget": {"max_seconds": 1}},
        }
        result = self.orchestrator(overrides, env={"STUB_SLEEP": "20"}).run()

        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        statuses = {row["label"]: row["status"] for row in db.list_provider_tasks(conn, result.run_id)}
        self.assertEqual(statuses["discover-a-in-Berlin"], "failed")
        self.assertEqual(statuses["discover-b-in-Berlin"], "skipped")
        self.assertIn("time budget exhausted", result.stages["discover"])


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)

    def orchestrator(self, overrides=None, env=None):
        cfg = fresh(overrides)
        return Orchestrator(cfg, base_dir=self.tmp, adapter=support.stub_adapter(env))

    def test_resume_after_an_orphaned_task_loses_nothing_and_duplicates_nothing(self):
        # First attempt fails, leaving a run that is not finished.
        failing = self.orchestrator(env={"STUB_EXIT": "1"})
        first = failing.run()
        self.assertEqual(first.status, "failed")

        # Mimic what an abrupt kill leaves behind: the run and its task are still
        # marked running because the process never got to write a terminal state.
        conn = db.connect(pathlib.Path(first.database))
        conn.execute("UPDATE runs SET status = 'running' WHERE id = ?", (first.run_id,))
        conn.execute(
            "UPDATE provider_tasks SET status = 'running' WHERE run_id = ?", (first.run_id,)
        )
        conn.commit()
        conn.close()

        resumed = self.orchestrator()
        result = resumed.run(resume=True)

        self.assertTrue(result.resumed)
        self.assertEqual(result.run_id, first.run_id)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.collected, 3)

        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        # Exactly one run, three businesses, three raw payloads: the retry added
        # no duplicates and lost nothing.
        self.assertEqual(db.table_counts(conn)["runs"], 1)
        self.assertEqual(db.table_counts(conn)["entities"], 3)
        self.assertEqual(db.table_counts(conn)["observations"], 3)
        # The failed attempt's output is preserved next to the successful one.
        self.assertEqual(len(list((pathlib.Path(result.run_dir) / "raw").glob("*.jsonl"))), 2)
        task = db.list_provider_tasks(conn, result.run_id)[0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["attempt"], 2)

    def test_a_resume_that_ingests_more_makes_the_merge_run_again(self):
        # The merge folds duplicates among the entities it can see. If a later
        # resume ingests more — a source that failed first and was retried on
        # the next attempt — the stage's "completed" mark must not skip it, or
        # the new records never meet the old ones and cross-source duplicates
        # are silently missed. Observed on a real run: a retried Google task
        # added 20 entities after the merge had run over 139 OSM ones, and the
        # run reported zero cross-source merges.
        from collector import ingest as ingest_module
        from collector.ingest import IngestStats
        from collector.orchestrator import RunResult

        first = self.orchestrator().run()
        conn = db.connect(pathlib.Path(first.database))
        self.addCleanup(conn.close)
        self.assertEqual(db.get_stage(conn, first.run_id, "merge")["status"], "completed")

        # Reopen the ingest stage, as the retry path leaves it.
        db.set_stage(conn, first.run_id, "ingest", "pending")
        result = RunResult(
            run_id=first.run_id,
            fingerprint=first.fingerprint,
            status="running",
            target_count=first.target_count,
            collected=first.collected,
            shortfall=0,
            resumed=True,
            run_dir=first.run_dir,
            database=first.database,
        )

        original = ingest_module.ingest_directory
        ingest_module.ingest_directory = lambda *a, **k: IngestStats(
            observations_new=2, entities_new=2
        )
        try:
            self.orchestrator()._stage_ingest(conn, first.run_id, result)
        finally:
            ingest_module.ingest_directory = original

        # No longer "completed", so the merge stage runs again on this pass.
        self.assertNotEqual(db.get_stage(conn, first.run_id, "merge")["status"], "completed")

    def test_a_resume_that_ingests_nothing_leaves_the_merge_alone(self):
        # The counter-case: re-running the merge on every resume would be waste.
        from collector import ingest as ingest_module
        from collector.ingest import IngestStats
        from collector.orchestrator import RunResult

        first = self.orchestrator().run()
        conn = db.connect(pathlib.Path(first.database))
        self.addCleanup(conn.close)

        db.set_stage(conn, first.run_id, "ingest", "pending")
        result = RunResult(
            run_id=first.run_id,
            fingerprint=first.fingerprint,
            status="running",
            target_count=first.target_count,
            collected=first.collected,
            shortfall=0,
            resumed=True,
            run_dir=first.run_dir,
            database=first.database,
        )
        original = ingest_module.ingest_directory
        ingest_module.ingest_directory = lambda *a, **k: IngestStats()
        try:
            self.orchestrator()._stage_ingest(conn, first.run_id, result)
        finally:
            ingest_module.ingest_directory = original

        self.assertEqual(db.get_stage(conn, first.run_id, "merge")["status"], "completed")

    def test_resume_is_reported_in_the_notes(self):
        self.orchestrator(env={"STUB_EXIT": "1"}).run()
        result = self.orchestrator().run(resume=True)
        self.assertTrue(any("resumed run" in note for note in result.notes))

    def test_no_resume_refuses_to_continue_an_unfinished_run(self):
        first = self.orchestrator(env={"STUB_EXIT": "1"}).run()
        with self.assertRaises(ResumeError) as ctx:
            self.orchestrator().run(resume=False)
        self.assertIn(f"run {first.run_id}", str(ctx.exception))
        self.assertIn("unfinished", str(ctx.exception))

    def test_resuming_a_run_with_a_different_fingerprint_is_refused(self):
        first = self.orchestrator().run()
        changed = self.orchestrator({"target_count": 99})

        conn = db.connect(pathlib.Path(first.database))
        self.addCleanup(conn.close)
        with self.assertRaises(ResumeError) as ctx:
            changed.resume_run(conn, first.run_id)
        self.assertIn("different configuration", str(ctx.exception))
        self.assertIn("mix incompatible evidence", str(ctx.exception))

    def test_a_changed_config_starts_a_new_run_instead_of_reusing_checkpoints(self):
        first = self.orchestrator().run()
        second = self.orchestrator({"target_count": 99}).run()

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertFalse(second.resumed)
        conn = db.connect(pathlib.Path(second.database))
        self.addCleanup(conn.close)
        self.assertEqual(db.table_counts(conn)["runs"], 2)

    def test_a_completed_run_is_not_resumed(self):
        first = self.orchestrator().run()
        second = self.orchestrator().run()
        self.assertFalse(second.resumed)
        self.assertNotEqual(first.run_id, second.run_id)


class EnrichStageTest(unittest.TestCase):
    """The stage D wiring: stub scraper -> ingest -> enrich -> export."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def fixture(self, website: str) -> pathlib.Path:
        path = self.tmp / "fixture.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "title": "Praxis Dr. M\u00fcller",
                        "address": "Musterstra\u00dfe 1, 10115 Berlin, DE",
                        "phone": "+49 30 12345678",
                        "web_site": website,
                        "data_id": "0xfixture:0x1",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_the_stage_list_includes_enrich(self):
        from collector.orchestrator import STAGES

        # ``sitediscovery`` sits between merge and enrich: after merge so the
        # run's cross-source picture exists, before enrich so a site it confirms
        # is one the enricher then visits.
        self.assertEqual(
            STAGES, ("discover", "ingest", "merge", "sitediscovery", "enrich", "export")
        )

    def test_the_stage_is_skipped_when_discovery_is_disabled(self):
        cfg = fresh()
        orch = Orchestrator(cfg, base_dir=self.tmp, adapter=support.stub_adapter())
        result = orch.run()

        self.assertIn("skipped", result.stages["sitediscovery"])
        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        self.assertEqual(
            db.get_stage(conn, result.run_id, "sitediscovery")["status"], "skipped"
        )
        # Nothing was looked for, so no candidate row of any kind exists.
        self.assertEqual(db.site_candidate_counts(conn, result.run_id)["candidates"], 0)

    def test_the_stage_is_skipped_when_enrichment_is_disabled(self):
        cfg = fresh()
        orch = Orchestrator(cfg, base_dir=self.tmp, adapter=support.stub_adapter())
        result = orch.run()

        self.assertIn("skipped", result.stages["enrich"])
        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        self.assertEqual(db.get_stage(conn, result.run_id, "enrich")["status"], "skipped")

    def test_a_run_with_enrichment_visits_the_site_and_exports_what_it_said(self):
        from collector.emailcheck import DNS_OK
        from collector.enrich import Enricher

        jsonld = json.dumps(
            {
                "@type": "Dentist",
                "name": "Praxis Dr. M\u00fcller",
                "email": "info@praxis-mueller.de",
                "address": {
                    "streetAddress": "Musterstra\u00dfe 1",
                    "postalCode": "10115",
                    "addressLocality": "Berlin",
                },
            }
        )
        pages = {"/": Page(body=support.site_page(jsonld=jsonld))}

        with SiteServer(pages) as site:
            cfg = support.enrich_config({"enrich": {"pages_per_site": 1}})
            enricher = Enricher(
                cfg,
                fetcher=support.local_fetcher(),
                resolver=support.FakeResolver(
                    mx={"praxis-mueller.de": (DNS_OK, ("mx.praxis-mueller.de",))}
                ),
                sleep=lambda _seconds: None,
                polite_delay_seconds=0,
            )
            orch = Orchestrator(
                cfg,
                base_dir=self.tmp,
                adapter=support.stub_adapter({"STUB_FIXTURE": str(self.fixture(site.url("/")))}),
                enricher=enricher,
            )
            result = orch.run()

        self.assertEqual(result.status, "completed")
        self.assertIn("completed", result.stages["enrich"])
        self.assertGreater(result.enrich_stats["facts_written"], 0)

        conn = db.connect(pathlib.Path(result.database))
        self.addCleanup(conn.close)
        self.assertEqual(db.get_stage(conn, result.run_id, "enrich")["status"], "completed")

        facts = [
            json.loads(line)
            for line in pathlib.Path(result.exports["field_facts"])
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        website_facts = [row for row in facts if row["provider"] == "website"]
        self.assertTrue(website_facts)
        self.assertEqual(
            {row["validation_state"] for row in website_facts if row["field"] == "emails"},
            {"mx_present"},
        )
        # The export carries the validation state and the evidence checks.
        email_fact = next(row for row in website_facts if row["field"] == "emails")
        self.assertEqual(email_fact["checks"]["mx_hosts"], ["mx.praxis-mueller.de"])
        self.assertTrue(email_fact["source_url"].startswith(site.url("/")))


if __name__ == "__main__":
    unittest.main()
