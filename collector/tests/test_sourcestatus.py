"""Round 026 P2.2/P2.3/P2.4: source status, cooldowns and how a run ended.

These tests pin down the honesty requirements of the task book:

* a site that answers 429 is recorded as *cooling down*, with the deadline the
  site itself asked for — not as "the directory had no businesses";
* a source that never ran shows no success time rather than a blank that reads
  as fine;
* ``target_count`` is a collection ceiling, so the run reports the collection
  count and the match verdicts separately instead of presenting one as the other;
* every stop has a reason, and the reason has a sentence.

The 429 path is exercised through a stub fetcher and a patched run context: no
test in this file sends a request anywhere.
"""

from __future__ import annotations

import pathlib
import unittest
from unittest import mock

from tests import support
from tests.test_sources import StubFetcher, make_context, write_csv

from collector import db
from collector.orchestrator import Orchestrator, RunResult
from collector.sources import directory
from collector.sources.directory import BusinessDirectoryProvider, _http_kind
from collector.webfetch import FetchResult


class HttpKindTest(unittest.TestCase):
    def test_a_rate_limit_is_its_own_kind(self) -> None:
        # The run treats 429 as a cooldown and 404 as a broken URL; folding both
        # into "http_error" would lose that.
        self.assertEqual(_http_kind(429), "http_429")
        self.assertEqual(_http_kind(404), "http_404")
        self.assertEqual(_http_kind(None, "connection_error"), "connection_error")
        self.assertEqual(_http_kind(None), "fetch_failed")


class DirectoryStopReasonTest(unittest.TestCase):
    """The listing walk says why it stopped, and a dead first page is a failure."""

    def setUp(self) -> None:
        self.tmp = support.temp_dir(self)

    def provider(self, entries, **settings) -> BusinessDirectoryProvider:
        cfg = support.config(
            {
                "sources": ["directory"],
                "sources_map": {"directory": {"entries": entries, **settings}},
            }
        )
        return BusinessDirectoryProvider(cfg)

    def _fail(self, status: int, kind: str, retry_after=None) -> FetchResult:
        return FetchResult(
            requested_url="https://example.com/list/",
            ok=False,
            status_code=status,
            error_kind=kind,
            error_detail="too many requests" if status == 429 else "not found",
            retry_after_seconds=retry_after,
        )

    def test_a_429_on_the_first_listing_page_is_a_failure_with_a_cooldown(self) -> None:
        provider = self.provider([{"name": "e", "start_url": "https://example.com/list/"}])
        (task,) = provider.tasks()
        fetcher = StubFetcher({"https://example.com/list/": self._fail(429, "http_429", 900)})
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "http_429")
        self.assertEqual(outcome.retry_after_seconds, 900)
        self.assertIn("900", outcome.error)

    def test_a_403_is_not_reported_as_a_rate_limit(self) -> None:
        provider = self.provider([{"name": "e", "start_url": "https://example.com/list/"}])
        (task,) = provider.tasks()
        fetcher = StubFetcher({"https://example.com/list/": self._fail(403, "http_403")})
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "http_403")
        self.assertIsNone(outcome.retry_after_seconds)

    def test_a_failure_on_a_later_page_is_recorded_as_the_stop_reason(self) -> None:
        # The first page yields rows; page 2 answers 429. The run keeps what it
        # got and says why it stopped, rather than reporting a clean finish.
        first = (
            '<html><body><a href="/c168-1.html">甲公司</a>'
            '<a rel="next" href="/list/2/">下一页</a></body></html>'
        )
        detail = "<html><body><ul class='company'><li>甲公司</li></ul></body></html>"
        provider = self.provider(
            [
                {
                    "name": "e",
                    "start_url": "https://example.com/list/",
                    "field_selectors": {"title": "ul.company li"},
                }
            ]
        )
        (task,) = provider.tasks()
        fetcher = StubFetcher(
            {
                "https://example.com/list/": (first, "text/html"),
                "https://example.com/c168-1.html": (detail, "text/html"),
                "https://example.com/list/2/": self._fail(429, "http_429", 60),
            }
        )
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))
        self.assertTrue(outcome.ok)
        envelopes = support.read_raw_envelopes(outcome.raw_path)
        self.assertEqual(envelopes[0]["context"]["listing_stop_reason"], "fetch_failed")
        self.assertEqual(envelopes[0]["context"]["listing_stop_status"], 429)

    def test_a_walk_that_ran_out_of_pages_says_so(self) -> None:
        page = '<html><body><a href="/c168-1.html">甲公司</a></body></html>'
        detail = "<html><body><ul class='company'><li>甲公司</li></ul></body></html>"
        provider = self.provider(
            [
                {
                    "name": "e",
                    "start_url": "https://example.com/list/",
                    "field_selectors": {"title": "ul.company li"},
                }
            ]
        )
        (task,) = provider.tasks()
        fetcher = StubFetcher(
            {
                "https://example.com/list/": (page, "text/html"),
                "https://example.com/c168-1.html": (detail, "text/html"),
            }
        )
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))
        envelope = support.read_raw_envelopes(outcome.raw_path)[0]
        # A page with rows but no "next" link is a different ending from a page
        # with no rows at all, and both are different from a failure.
        self.assertEqual(envelope["context"]["listing_stop_reason"], "no_next_link")

    def test_an_empty_listing_page_reports_that_there_were_no_rows(self) -> None:
        # The stop reason is read off the walk itself: with zero rows there is no
        # record to carry it, and "the site had no rows" is the fact that matters.
        provider = self.provider([{"name": "e", "start_url": "https://example.com/list/"}])
        fetcher = StubFetcher(
            {"https://example.com/list/": ("<html><body></body></html>", "text/html")}
        )
        walk = provider._walk_listings(
            make_context(self.tmp, fetcher=fetcher),
            "https://example.com/list/",
            fetcher=fetcher,
            detail_pattern=None,
            listing_selector=None,
            max_listing=5,
            stop_empty=1,
            stop_repeated=1,
            delay=0.0,
        )
        self.assertEqual(walk.stop["reason"], "no_more_rows")
        self.assertEqual(walk.listings, ["https://example.com/list/"])
        self.assertEqual(walk.details, [])


class DirectoryPresetVerificationTest(unittest.TestCase):
    """034 第三批 3.2: 在线目录 / 离线已验证 / 实时受限 不许混写。

    "已验证" 单独出现会被读成"实时跑也会成功"，而顺企网不是这样：它的离线样本
    验证过，实时路径返回 429。所以离线和实时是两个字段，各有各的证据，谁也不能
    代表谁。同一张表同时供界面和导出读取，两边不可能说不一样的话。
    """

    def test_offline_and_live_states_are_separate_fields(self):
        for key, row in directory.PRESET_VERIFICATION.items():
            self.assertIn(row["offline"], ("verified", "unknown"), key)
            self.assertIn(row["live"], ("verified", "rate_limited", "unknown"), key)
            self.assertTrue(row["evidence"], key)

    def test_shunqiwang_is_verified_offline_and_rate_limited_live(self):
        # The distinction the task book named: 八方曾在线采集，顺企网离线已验证
        # 而实时受 429。两者不能写成同一个词。
        row = directory.PRESET_VERIFICATION["shunqiwang"]
        self.assertEqual(row["offline"], "verified")
        self.assertEqual(row["live"], "rate_limited")
        self.assertIn("429", row["evidence"])
        self.assertNotEqual(row, directory.PRESET_VERIFICATION["b2b168"])

    def test_b2b168_is_verified_on_both_paths(self):
        row = directory.PRESET_VERIFICATION["b2b168"]
        self.assertEqual(row["offline"], "verified")
        # A live run was actually done against this site, so it may not be
        # written as if only saved pages had been read.
        self.assertEqual(row["live"], "verified")

    def test_a_preset_with_no_evidence_of_either_says_so(self):
        row = directory.PRESET_VERIFICATION["generic"]
        self.assertEqual(row["offline"], "unknown")
        self.assertEqual(row["live"], "unknown")

    def test_the_label_names_both_states(self):
        self.assertIn("离线", directory.verification_label("shunqiwang"))
        self.assertIn("受限", directory.verification_label("shunqiwang"))
        self.assertIn("实时", directory.verification_label("b2b168"))
        # An unknown preset is not dressed up as verified.
        self.assertEqual(directory.verification_label("nope"), "未验证")

    def test_the_screen_and_the_export_read_the_same_table(self):
        from collector import local_ui

        for key in ("b2b168", "shunqiwang", "generic"):
            self.assertEqual(
                local_ui.DIRECTORY_PRESETS[key]["live_status"],
                directory.verification_label(key),
            )


class SourceStatusDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = support.temp_dir(self)
        self.conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(self.conn.close)

    def test_a_fresh_source_has_no_status_at_all(self) -> None:
        self.assertEqual(db.list_source_status(self.conn), [])

    def test_a_success_records_the_time_and_the_network_used(self) -> None:
        db.note_source_success(
            self.conn, "directory", network_mode="direct", proxy="", at="2026-10-13T02:00:00+00:00"
        )
        row = db.get_source_status(self.conn, "directory")
        self.assertEqual(row["last_success_at"], "2026-10-13T02:00:00+00:00")
        self.assertEqual(row["network_mode"], "direct")
        self.assertIsNone(row["cooldown_until"])

    def test_a_rate_limit_sets_the_cooldown_the_site_asked_for(self) -> None:
        deadline = db.note_source_failure(
            self.conn,
            "directory",
            error="429",
            error_kind="http_429",
            retry_after_seconds=900,
            at="2026-10-13T02:00:00+00:00",
        )
        self.assertEqual(deadline, "2026-10-13T02:15:00+00:00")
        row = db.get_source_status(self.conn, "directory")
        self.assertEqual(row["cooldown_seconds"], 900)
        self.assertEqual(row["last_error_kind"], "http_429")

    def test_a_plain_error_does_not_set_a_cooldown(self) -> None:
        db.note_source_failure(
            self.conn, "directory", error="404", error_kind="http_404", at="2026-10-13T02:00:00+00:00"
        )
        row = db.get_source_status(self.conn, "directory")
        self.assertIsNone(row["cooldown_until"])
        self.assertEqual(row["last_error_kind"], "http_404")

    def test_a_later_success_clears_the_cooldown(self) -> None:
        db.note_source_failure(
            self.conn,
            "directory",
            error="429",
            error_kind="http_429",
            retry_after_seconds=900,
            at="2026-10-13T02:00:00+00:00",
        )
        db.note_source_success(self.conn, "directory", at="2026-10-13T02:20:00+00:00")
        row = db.get_source_status(self.conn, "directory")
        self.assertIsNone(row["cooldown_until"])
        # The error is kept as history: "it worked again" does not erase the fact
        # that it once rate-limited us.
        self.assertEqual(row["last_error_kind"], "http_429")

    def test_cooling_down_is_decided_against_the_clock_not_stored(self) -> None:
        # A stored boolean would be wrong the moment the deadline passed.
        db.note_source_failure(
            self.conn,
            "s",
            error="429",
            error_kind="http_429",
            retry_after_seconds=60,
            at="2020-01-01T00:00:00+00:00",
        )
        (entry,) = db.list_source_status(self.conn)
        self.assertFalse(entry["cooling_down"])

    def test_the_cooldown_is_per_source_not_per_run(self) -> None:
        db.note_source_failure(
            self.conn, "a", error="429", error_kind="http_429", retry_after_seconds=60
        )
        db.note_source_success(self.conn, "b")
        statuses = {entry["source"]: entry for entry in db.list_source_status(self.conn)}
        self.assertEqual(sorted(statuses), ["a", "b"])
        self.assertIsNotNone(statuses["a"]["cooldown_until"])
        self.assertIsNone(statuses["b"]["cooldown_until"])

    def test_a_blank_network_mode_does_not_erase_what_a_success_recorded(self) -> None:
        db.note_source_success(self.conn, "s", network_mode="direct")
        db.note_source_failure(self.conn, "s", error="boom", error_kind="connection_error")
        row = db.get_source_status(self.conn, "s")
        self.assertEqual(row["network_mode"], "direct")


class SourceStatusWiringTest(unittest.TestCase):
    """The orchestrator's own recording, driven through a patched run context."""

    def setUp(self) -> None:
        self.tmp = support.temp_dir(self)

    def _orchestrator(self, fetcher, **config_overrides):
        cfg = support.config(
            {
                "sources": ["directory"],
                "sources_map": {
                    "directory": {
                        "entries": [{"name": "e", "start_url": "https://example.com/list/"}]
                    }
                },
                **config_overrides,
            }
        )
        orch = Orchestrator(cfg, base_dir=self.tmp)
        context = make_context(self.tmp, fetcher=fetcher)
        patcher = mock.patch.object(Orchestrator, "_run_context", lambda *_a, **_k: context)
        patcher.start()
        self.addCleanup(patcher.stop)
        return orch

    def test_a_rate_limited_task_records_a_cooldown_and_a_reason(self) -> None:
        fetcher = StubFetcher(
            {
                "https://example.com/list/": FetchResult(
                    requested_url="https://example.com/list/",
                    ok=False,
                    status_code=429,
                    error_kind="http_429",
                    error_detail="slow down",
                    retry_after_seconds=900,
                )
            }
        )
        result = self._orchestrator(fetcher).run(resume=False)
        conn = db.connect(pathlib.Path(result.database))
        try:
            (entry,) = db.list_source_status(conn)
        finally:
            conn.close()
        self.assertEqual(entry["source"], "directory")
        self.assertEqual(entry["last_error_kind"], "http_429")
        self.assertIsNotNone(entry["cooldown_until"])
        self.assertEqual(entry["network_mode"], "inherit")

    def test_a_successful_task_records_the_success_time(self) -> None:
        page = "<html><body>no links here</body></html>"
        fetcher = StubFetcher({"https://example.com/list/": (page, "text/html")})
        result = self._orchestrator(fetcher).run(resume=False)
        conn = db.connect(pathlib.Path(result.database))
        try:
            (entry,) = db.list_source_status(conn)
        finally:
            conn.close()
        self.assertIsNotNone(entry["last_success_at"])
        self.assertIsNone(entry["last_error_kind"])


class StopReasonTest(unittest.TestCase):
    """Every ending says why, and the sentence exists for the code."""

    def setUp(self) -> None:
        self.tmp = support.temp_dir(self)

    def _run(self, **overrides) -> RunResult:
        rows = (
            "title,address\n"
            "杭州甲公司,杭州市西湖区文一路 1 号\n"
            "杭州乙公司,杭州市上城区解放路 2 号\n"
        )
        path = write_csv(self.tmp / "list.csv", rows)
        cfg = support.config(
            {
                "sources": ["csv_list"],
                "sources_map": {"csv_list": {"path": path}},
                **overrides,
            }
        )
        return Orchestrator(cfg, base_dir=self.tmp).run(resume=False)

    def test_hitting_the_target_says_so(self) -> None:
        result = self._run(target_count=2)
        self.assertEqual(result.stop_reason, "target_reached")
        self.assertEqual(result.stop_note, "已达到目标条数")

    def test_running_out_of_sources_says_so(self) -> None:
        result = self._run(target_count=99)
        self.assertEqual(result.stop_reason, "sources_exhausted")
        self.assertIn("来源", result.stop_note)

    def test_every_code_has_a_sentence(self) -> None:
        for code, note in Orchestrator.STOP_NOTES.items():
            self.assertTrue(note, code)

    def test_the_reason_is_persisted_for_the_screen(self) -> None:
        result = self._run(target_count=1)
        conn = db.connect(pathlib.Path(result.database))
        try:
            stored = db.get_checkpoint(conn, result.run_id, "run", "summary")
        finally:
            conn.close()
        self.assertEqual(stored["stop_reason"], result.stop_reason)
        self.assertEqual(stored["stop_note"], result.stop_note)
        self.assertIn("collected", stored)


class RunSummaryTest(unittest.TestCase):
    """The collected-vs-matched split of 026 P2.4."""

    def setUp(self) -> None:
        self.tmp = support.temp_dir(self)

    def test_the_summary_separates_collected_from_matched(self) -> None:
        from collector import export

        cfg = support.config({"target_count": 50})
        conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(conn.close)
        run_id = db.create_run(
            conn,
            fingerprint="x",
            config_json=cfg.canonical_json(),
            config_source="<t>",
            collector_version="0",
            schema_version=1,
            go_binary="",
            go_version="",
            target_count=50,
        )
        summary = export.run_summary(conn, run_id, cfg)
        self.assertEqual(summary["collected"], 0)
        self.assertEqual(summary["target_count"], 50)
        self.assertEqual(summary["shortfall"], 50)
        # No ``match`` section in this config, so no group counts are invented.
        self.assertIsNone(summary["matched"])

    def test_a_matching_config_gets_group_counts(self) -> None:
        from collector import export

        cfg = support.config(
            {
                "target_count": 10,
                "match": {"region": {"city": "杭州"}, "industry": {"profile": "pet_related"}},
            }
        )
        conn = db.connect(self.tmp / "c.sqlite")
        self.addCleanup(conn.close)
        run_id = db.create_run(
            conn,
            fingerprint="y",
            config_json=cfg.canonical_json(),
            config_source="<t>",
            collector_version="0",
            schema_version=1,
            go_binary="",
            go_version="",
            target_count=10,
        )
        summary = export.run_summary(conn, run_id, cfg)
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(summary["unknown"], 0)
        self.assertEqual(summary["rejected"], 0)


class RenderTest(unittest.TestCase):
    """The two screen blocks, as pure functions."""

    def test_a_source_that_never_ran_does_not_look_healthy(self) -> None:
        from collector.local_ui import _source_status_block

        block = _source_status_block([])
        self.assertIn("还没有任何来源跑过", block)
        self.assertIn("TUN", block)

    def test_the_network_mode_is_spelled_out(self) -> None:
        from collector.local_ui import _source_status_block

        block = _source_status_block(
            [
                {
                    "source": "directory",
                    "network_mode": "direct",
                    "proxy": "",
                    "last_success_at": "2026-10-13T02:00:00+00:00",
                    "last_error": "",
                    "last_error_at": None,
                    "last_error_kind": "",
                    "cooldown_until": None,
                    "cooldown_seconds": None,
                    "cooling_down": False,
                }
            ]
        )
        self.assertIn("直连", block)
        self.assertIn("忽略 runtime.proxy", block)
        self.assertIn("2026-10-13 02:00:00", block)

    def test_a_cooldown_is_shown_with_its_deadline(self) -> None:
        from collector.local_ui import _source_status_block

        block = _source_status_block(
            [
                {
                    "source": "directory",
                    "network_mode": "inherit",
                    "proxy": "",
                    "last_success_at": None,
                    "last_error": "http_429 too many requests",
                    "last_error_at": "2026-10-13T02:00:00+00:00",
                    "last_error_kind": "http_429",
                    "cooldown_until": "2026-10-13T02:15:00+00:00",
                    "cooldown_seconds": 900,
                    "cooling_down": True,
                }
            ]
        )
        self.assertIn("冷却中", block)
        self.assertIn("2026-10-13 02:15:00", block)
        self.assertIn("900", block)

    def test_a_finished_cooldown_is_not_shown_as_active(self) -> None:
        from collector.local_ui import _source_status_block

        block = _source_status_block(
            [
                {
                    "source": "directory",
                    "network_mode": "inherit",
                    "proxy": "",
                    "last_success_at": None,
                    "last_error": "429",
                    "last_error_at": "2026-10-13T02:00:00+00:00",
                    "last_error_kind": "http_429",
                    "cooldown_until": "2026-10-13T02:15:00+00:00",
                    "cooldown_seconds": 900,
                    "cooling_down": False,
                }
            ]
        )
        self.assertNotIn("冷却中", block)
        self.assertIn("已结束", block)

    def test_the_summary_keeps_the_two_numbers_apart(self) -> None:
        from collector.local_ui import _run_summary_block

        block = _run_summary_block(
            {"candidates": 15, "collected": 12, "with_contact": 7,
             "target_count": 30, "shortfall": 18,
             "matched": 4, "unknown": 3, "rejected": 5},
            stop_reason="sources_exhausted",
            stop_note="来源已给不出更多不重复的商家，未达到目标条数",
        )
        # 034 3.6: 候选 and 最终商家 are printed as two rows, because a run that
        # yielded 15 records and 12 businesses is not a run that lost 3.
        self.assertIn("候选记录数（合并前）", block)
        self.assertIn("最终商家数（合并后）", block)
        self.assertIn(">15<", block)
        self.assertIn(">12<", block)
        self.assertIn("7 / 12", block)
        self.assertIn("4 / 3 / 5", block)
        self.assertIn("18", block)
        self.assertIn("来源已给不出更多", block)

    def test_a_run_without_matching_says_so_instead_of_showing_zero(self) -> None:
        from collector.local_ui import _run_summary_block

        block = _run_summary_block(
            {"collected": 3, "target_count": 3, "shortfall": 0,
             "matched": None, "unknown": None, "rejected": None},
            stop_reason="target_reached",
            stop_note="",
        )
        self.assertIn("未启用分组", block)
        self.assertIn("已达到目标条数", block)

    def test_a_run_with_no_reason_yet_does_not_print_a_bare_code(self) -> None:
        from collector.local_ui import _run_summary_block

        block = _run_summary_block(
            {"collected": 0, "target_count": 5, "shortfall": 5,
             "matched": None, "unknown": None, "rejected": None},
            stop_reason="",
            stop_note="",
        )
        self.assertIn("尚未结束", block)


class PresetLabelTest(unittest.TestCase):
    """026 P2.2: the label states the offline/live split, honestly."""

    def test_the_shunqiwang_preset_says_live_collection_is_limited(self) -> None:
        from collector.local_ui import DIRECTORY_PRESETS

        label = DIRECTORY_PRESETS["shunqiwang"]["label"]
        self.assertIn("离线样本已验证", label)
        self.assertIn("实时采集受限", label)

    def test_every_preset_states_its_live_status(self) -> None:
        from collector.local_ui import DIRECTORY_PRESETS

        for key, entry in DIRECTORY_PRESETS.items():
            self.assertTrue(entry.get("live_status"), key)


class AreaCodeOnlyPhoneTest(unittest.TestCase):
    """026 P2.2: a bare area code is not a business telephone number.

    This already held before the round — it is pinned here because the task book
    names it, and a regression would otherwise be silent (the value would reach
    the export as a "phone").
    """

    def test_a_bare_area_code_is_not_a_valid_business_phone(self) -> None:
        from collector.phonecheck import STATUS_INVALID, check_phone

        for value in ("(0571)", "0571", "0571-", "  (0571)  "):
            verdict = check_phone(value, region="CN")
            self.assertEqual(verdict.status, STATUS_INVALID, value)
            self.assertIsNone(verdict.e164, value)

    def test_a_full_hangzhou_number_is_still_recognised(self) -> None:
        from collector.phonecheck import STATUS_VALID, STATUS_UNCERTAIN, check_phone

        verdict = check_phone("0571-88886666", region="CN")
        self.assertIn(verdict.status, (STATUS_VALID, STATUS_UNCERTAIN))
        self.assertEqual(verdict.e164, "+8657188886666")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
