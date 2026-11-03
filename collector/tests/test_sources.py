"""Stage E: the provider contract, shared facilities and the four sources.

Task book 004, stage E: "实现统一 provider 接口，至少接入：现有 Go Maps、
OpenStreetMap、用户 URL/CSV 列表、一个可配置企业目录" and "所有来源共享预算、
重试退避、域级限速、检查点；尊重 Retry-After，限制最大尝试次数".

These tests are deterministic. The two network providers are driven by a stub
fetcher, and the Google provider by the existing stub scraper, so nothing here
depends on the internet or on a live Overpass instance. That is deliberate: the
acceptance run exercises the real network, and it should be the only thing that
can fail for network reasons.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from collector.sources import (
    PROVIDER_CLASSES,
    PLANNING_ORDER,
    build_providers,
    manifest,
)
from collector.sources.base import (
    DomainRateLimiter,
    Provider,
    ProviderTask,
    RetryPolicy,
    RunBudget,
    RunContext,
    SourceRecord,
    read_envelope_count,
    require_records,
    write_envelopes,
)
from collector.webfetch import FetchResult

from . import support


class StubFetcher:
    """A scripted stand-in for :class:`collector.webfetch.Fetcher`.

    Providers only need ``fetch``; the rest of the stage D client is not what
    these tests are about. Recording the calls is what makes "each listing page
    is fetched exactly once" assertable, which is a politeness property the task
    book asks for and a wasted-budget bug is easy to introduce.
    """

    def __init__(self, pages=None, default=None):
        self.pages = dict(pages or {})
        self.default = default
        self.calls: list[str] = []

    def fetch(self, url, *, allowed_domains=None, accept_types=()):
        self.calls.append(url)
        if url in self.pages:
            entry = self.pages[url]
        elif self.default is not None:
            entry = self.default
        else:
            return FetchResult(
                requested_url=url, ok=False, status_code=404, error_kind="http_404",
                error_detail="not found",
            )
        if isinstance(entry, FetchResult):
            return entry
        body, content_type = entry
        return FetchResult(
            requested_url=url,
            final_url=url,
            ok=True,
            status_code=200,
            content_type=content_type,
            body=body,
            body_bytes=len(body.encode("utf-8")),
        )


def make_context(tmp, *, fetcher=None, budget=None, limiter=None, retry=None,
                 run_id=1, sleep=None, **kw):
    from unittest import mock

    return RunContext(
        run_id=run_id,
        run_dir=pathlib.Path(tmp) / f"run{run_id}",
        budget=budget if budget is not None else RunBudget(),
        rate_limiter=limiter if limiter is not None else DomainRateLimiter(0.0),
        retry=retry if retry is not None else RetryPolicy(),
        fetcher=fetcher if fetcher is not None else StubFetcher(),
        sleep=sleep if sleep is not None else (lambda _seconds: None),
        **kw,
    )


def source_config(overrides=None):
    """A config that lists extra sources, with their ``sources_map`` settings."""
    base = {"sources": ["csv_list"]}
    merged = dict(base)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return support.config(merged)


def write_csv(path, text):
    pathlib.Path(path).write_text(text, encoding="utf-8")
    return str(path)


def _query_string(query: str) -> str:
    import urllib.parse

    return urllib.parse.urlencode({"data": query})


# --- the contract -----------------------------------------------------------


class SourceRecordTest(unittest.TestCase):
    """The canonical payload, in the field names ingest already maps."""

    def test_field_names_match_the_collector(self):
        record = SourceRecord(
            title="Praxis Beispiel",
            address="Musterstra\u00dfe 1, 10115 Berlin",
            phone="+49 30 12345678",
            website="https://www.praxis.example.de",
            emails=("info@praxis.example.de",),
            latitude=52.52,
            longitude=13.40,
            category="Zahnarzt",
            source_id="node/123",
            source_id_kind="osm",
        )
        raw = record.as_raw()
        # ``web_site`` and not ``website``: ingest maps that key, and inventing a
        # different one would silently drop the field on the floor.
        self.assertEqual(raw["web_site"], "https://www.praxis.example.de")
        self.assertNotIn("website", raw)
        self.assertEqual(raw["title"], "Praxis Beispiel")
        self.assertEqual(raw["emails"], ["info@praxis.example.de"])
        self.assertEqual(raw["source_id"], "node/123")
        self.assertEqual(raw["source_id_kind"], "osm")

    def test_absent_fields_are_left_out_not_filled_with_blanks(self):
        raw = SourceRecord(title="Nur ein Name").as_raw()
        # An empty identity is omitted rather than written as "" — ingest treats
        # "no key" and "empty key" the same way, and an empty string would only
        # add noise. What matters is that no *content* field is guessed.
        self.assertEqual(raw, {"title": "Nur ein Name"})
        self.assertNotIn("address", raw)
        self.assertNotIn("phone", raw)
        self.assertNotIn("emails", raw)
        self.assertNotIn("native", raw)
        self.assertNotIn("extra", raw)

    def test_native_payload_is_kept_verbatim(self):
        native = {"tags": {"amenity": "dentist", "name": "X"}}
        record = SourceRecord(title="X", native=native)
        self.assertEqual(record.as_raw()["native"], native)


class EnvelopeTest(unittest.TestCase):
    """Every provider writes the same framing stage C already ingested."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def test_envelope_schema_and_provider_are_recorded(self):
        path = self.tmp / "raw.jsonl"
        written = write_envelopes(
            path,
            "openstreetmap",
            [SourceRecord(title="A", source_id="node/1", source_id_kind="osm")],
            query="amenity=dentist",
        )
        self.assertEqual(written, 1)
        (line,) = path.read_text(encoding="utf-8").splitlines()
        envelope = json.loads(line)
        self.assertEqual(envelope["schema"], "osint.record/1")
        self.assertEqual(envelope["provider"], "openstreetmap")
        self.assertEqual(envelope["context"]["query"], "amenity=dentist")
        self.assertEqual(envelope["raw"]["title"], "A")

    def test_caller_context_wins_over_the_defaults(self):
        path = self.tmp / "raw.jsonl"
        write_envelopes(
            path,
            "csv_list",
            [SourceRecord(title="A", source_id="row-1")],
            context={"source_url": "file:///list.csv", "csv_rows": 1},
        )
        envelope = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(envelope["context"]["source_url"], "file:///list.csv")
        self.assertEqual(envelope["context"]["csv_rows"], 1)
        self.assertEqual(envelope["context"]["mode"], "provider")

    def test_a_csv_row_can_never_claim_to_have_been_fetched_live(self):
        # 026 P0: the csv_list provider only reads a file, so a record that says
        # "live" is overstating what happened. The writer refuses to pass that
        # on, because a reader would take it for "this run visited the page".
        path = self.tmp / "raw.jsonl"
        write_envelopes(
            path,
            "csv_list",
            [SourceRecord(title="A", source_id="row-1", acquired_via="live")],
        )
        envelope = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(envelope["context"]["acquired_via"], "import")

    def test_an_import_does_not_stamp_the_import_moment_as_a_collection_time(self):
        # 034 第一批第 3 条：对只读文件的来源，"现在"是我们读文件的时刻，不是来源
        # 的采集时刻。盖上 scraped_at 会让导出把今晚的导入写成今晚采到的数据。
        path = self.tmp / "raw.jsonl"
        write_envelopes(
            path, "csv_list", [SourceRecord(title="A", source_id="row-1")]
        )
        envelope = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertNotIn("scraped_at", envelope["context"])

    def test_a_provider_that_fetched_does_stamp_when_it_fetched(self):
        # 只针对真取过字节的来源：那个时间确实是"这次跑拿到它的时刻"。
        path = self.tmp / "raw.jsonl"
        write_envelopes(
            path,
            "openstreetmap",
            [SourceRecord(title="A", source_id="node/1", source_id_kind="osm")],
        )
        envelope = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(envelope["context"]["scraped_at"])

    def test_a_stated_capture_time_travels_with_the_imported_row(self):
        path = self.tmp / "raw.jsonl"
        write_envelopes(
            path,
            "csv_list",
            [SourceRecord(title="A", source_id="row-1", observed_at="2026-10-13")],
        )
        envelope = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(envelope["context"]["observed_at"], "2026-10-13")
        self.assertNotIn("scraped_at", envelope["context"])

    def test_an_unknown_acquisition_method_falls_back_to_the_safe_reading(self):
        path = self.tmp / "raw.jsonl"
        write_envelopes(
            path,
            "openstreetmap",
            [SourceRecord(title="A", source_id="node/1", acquired_via="guessed")],
        )
        envelope = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(envelope["context"]["acquired_via"], "live")

    def test_record_provenance_reaches_every_envelope(self):
        path = self.tmp / "raw.jsonl"
        write_envelopes(
            path,
            "csv_list",
            [
                SourceRecord(
                    title="A",
                    source_id="row-1",
                    acquired_via="import",
                    origin="www.11467.com",
                    source_page_url="https://www.11467.com/hangzhou/search/9092.htm",
                    detail_url="https://www.11467.com/qiye/1.htm",
                    snapshot_id="listing.html@abcdef12",
                    observed_at="2026-10-13T21:11:21+08:00",
                )
            ],
        )
        context = json.loads(path.read_text(encoding="utf-8").splitlines()[0])["context"]
        self.assertEqual(context["acquired_via"], "import")
        self.assertEqual(context["origin"], "www.11467.com")
        self.assertEqual(context["source_page_url"], "https://www.11467.com/hangzhou/search/9092.htm")
        self.assertEqual(context["detail_url"], "https://www.11467.com/qiye/1.htm")
        self.assertEqual(context["snapshot_id"], "listing.html@abcdef12")
        self.assertEqual(context["observed_at"], "2026-10-13T21:11:21+08:00")

    def test_an_unvisited_detail_page_is_not_claimed_as_fetched(self):
        path = self.tmp / "raw.jsonl"
        write_envelopes(
            path,
            "csv_list",
            [SourceRecord(title="A", source_id="row-1", detail_url="https://x.example/1.htm")],
        )
        context = json.loads(path.read_text(encoding="utf-8").splitlines()[0])["context"]
        self.assertNotIn("detail_fetched", context)

    def test_a_record_with_no_known_time_carries_none(self):
        # 026 P0.2: the import moment must never stand in for the page's own
        # capture time. With nothing stated, the key is simply absent.
        path = self.tmp / "raw.jsonl"
        write_envelopes(
            path,
            "csv_list",
            [SourceRecord(title="A", source_id="row-1", acquired_via="import")],
        )
        context = json.loads(path.read_text(encoding="utf-8").splitlines()[0])["context"]
        self.assertNotIn("observed_at", context)
        self.assertNotIn("snapshot_id", context)

    def test_empty_input_writes_an_empty_file(self):
        path = self.tmp / "raw.jsonl"
        self.assertEqual(write_envelopes(path, "csv_list", []), 0)
        self.assertTrue(path.is_file())
        self.assertEqual(read_envelope_count(path), 0)

    def test_read_envelope_count_ignores_blank_lines(self):
        path = self.tmp / "raw.jsonl"
        path.write_text('{"a":1}\n\n{"b":2}\n', encoding="utf-8")
        self.assertEqual(read_envelope_count(path), 2)

    def test_require_records_tolerates_a_missing_file(self):
        # A provider that wrote nothing must not be an exception: the run counts
        # it as zero records and carries on with the other sources.
        self.assertEqual(require_records(None, label="x"), 0)
        self.assertEqual(require_records(self.tmp / "nope.jsonl", label="x"), 0)


# --- shared budget, politeness and retry -----------------------------------


class RunBudgetTest(unittest.TestCase):
    """One run-wide budget, shared by every source."""

    def test_no_limit_means_no_reason(self):
        budget = RunBudget(max_requests=0, max_seconds=0)
        self.assertIsNone(budget.reason())
        self.assertIsNone(budget.remaining_seconds())

    def test_request_budget_exhausts(self):
        budget = RunBudget(max_requests=2)
        self.assertIsNone(budget.reason())
        budget.spend()
        self.assertIsNone(budget.reason())
        budget.spend()
        self.assertIn("request budget exhausted", budget.reason())

    def test_time_budget_exhausts(self):
        import time as _time

        budget = RunBudget(max_seconds=3)
        budget.started_at = _time.monotonic()  # start the clock now
        self.assertIsNone(budget.reason())
        # Move the start far enough back that the budget is gone. Rewinding
        # ``started_at`` is how a test simulates 10 seconds passing without
        # sleeping for real.
        budget.started_at -= 10.0
        self.assertIn("time budget exhausted", budget.reason())

    def test_remaining_seconds_shrinks_and_floors_at_zero(self):
        budget = RunBudget(max_seconds=100)
        budget.started_at -= 30.0
        self.assertAlmostEqual(budget.remaining_seconds(), 70.0, delta=1.0)
        budget.started_at -= 500.0
        self.assertEqual(budget.remaining_seconds(), 0.0)

    def test_spend_and_note_skip_are_counted(self):
        budget = RunBudget()
        budget.spend(3)
        budget.note_skip()
        budget.note_skip()
        as_dict = budget.as_dict()
        self.assertEqual(as_dict["used_requests"], 3)
        self.assertEqual(as_dict["skipped"], 2)
        self.assertEqual(as_dict["max_requests"], 0)


class DomainRateLimiterTest(unittest.TestCase):
    """A minimum interval per registrable domain."""

    def test_a_zero_interval_never_waits(self):
        limiter = DomainRateLimiter(0.0, sleep=lambda _s: None)
        for _ in range(5):
            limiter.wait("example.com")
        self.assertEqual(limiter.waits, 0)

    def test_the_first_request_to_a_domain_is_immediate(self):
        slept: list[float] = []
        limiter = DomainRateLimiter(1.0, sleep=slept.append)
        limiter.wait("example.com")
        self.assertEqual(slept, [])

    def test_a_second_request_within_the_interval_waits(self):
        now = [100.0]
        slept: list[float] = []
        limiter = DomainRateLimiter(
            1.0, sleep=slept.append, clock=lambda: now[0]
        )
        limiter.wait("example.com")
        now[0] = 100.4  # 0.4s later, interval is 1.0s
        limiter.wait("example.com")
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 0.6, places=6)

    def test_a_request_after_the_interval_does_not_wait(self):
        now = [100.0]
        slept: list[float] = []
        limiter = DomainRateLimiter(1.0, sleep=slept.append, clock=lambda: now[0])
        limiter.wait("example.com")
        now[0] = 101.5
        limiter.wait("example.com")
        self.assertEqual(slept, [])

    def test_domains_are_limited_independently(self):
        now = [100.0]
        slept: list[float] = []
        limiter = DomainRateLimiter(1.0, sleep=slept.append, clock=lambda: now[0])
        limiter.wait("a.example.com")
        limiter.wait("b.example.org")
        self.assertEqual(slept, [])

    def test_an_empty_domain_is_not_rate_limited(self):
        limiter = DomainRateLimiter(1.0, sleep=lambda _s: None)
        limiter.wait("")
        self.assertEqual(limiter.waits, 0)


class RetryPolicyTest(unittest.TestCase):
    """Backoff, honouring Retry-After, and a hard attempt ceiling."""

    def test_exponential_backoff(self):
        policy = RetryPolicy(backoff_seconds=1.0, backoff_multiplier=2.0)
        self.assertEqual(policy.delay_for(1), 1.0)
        self.assertEqual(policy.delay_for(2), 2.0)
        self.assertEqual(policy.delay_for(3), 4.0)

    def test_backoff_is_capped(self):
        policy = RetryPolicy(backoff_seconds=1.0, max_backoff_seconds=5.0)
        self.assertEqual(policy.delay_for(10), 5.0)

    def test_retry_after_within_the_cap_is_obeyed(self):
        policy = RetryPolicy(max_retry_wait_seconds=5)
        self.assertEqual(policy.delay_for(1, retry_after=3), 3.0)

    def test_a_long_retry_after_is_not_obeyed_beyond_the_backoff_cap(self):
        # A server asking for an hour is *reported*, not obeyed: spending the
        # run's budget that way is the caller's decision.
        policy = RetryPolicy(max_retry_wait_seconds=5, max_backoff_seconds=30.0)
        self.assertEqual(policy.delay_for(1, retry_after=3600), 30.0)

    def test_retry_after_can_be_ignored(self):
        policy = RetryPolicy(backoff_seconds=1.0, honor_retry_after=False)
        self.assertEqual(policy.delay_for(1, retry_after=3), 1.0)

    def test_max_attempts_bounds_the_total_tries(self):
        self.assertEqual(RetryPolicy(max_attempts=3).max_attempts, 3)


class RunContextTest(unittest.TestCase):
    """Paths, budget withdrawal and politeness, shared by every provider."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def test_raw_path_is_per_attempt_so_a_retry_does_not_overwrite(self):
        ctx = make_context(self.tmp)
        self.assertEqual(
            ctx.raw_path("discover-x", 1).name, "discover-x-a1.jsonl"
        )
        self.assertEqual(
            ctx.raw_path("discover-x", 2).name, "discover-x-a2.jsonl"
        )
        self.assertNotEqual(ctx.raw_path("discover-x", 1),
                            ctx.raw_path("discover-x", 2))

    def test_log_paths_name_their_stream(self):
        ctx = make_context(self.tmp)
        self.assertEqual(ctx.log_path("t", 1, "stdout").name, "t-a1.stdout.log")
        self.assertEqual(ctx.log_path("t", 1, "stderr").name, "t-a1.stderr.log")

    def test_withdraw_task_timeout_uses_the_fallback_when_unbounded(self):
        ctx = make_context(self.tmp)
        self.assertEqual(ctx.withdraw_task_timeout(60), 60)

    def test_withdraw_task_timeout_is_capped_by_the_remaining_time(self):
        import time as _time

        budget = RunBudget(max_seconds=100)
        budget.started_at = _time.monotonic() - 90.0  # 10s left of 100
        ctx = make_context(self.tmp, budget=budget)
        timeout = ctx.withdraw_task_timeout(60)
        self.assertGreaterEqual(timeout, 1)
        self.assertLessEqual(timeout, 11)

    def test_be_polite_delegates_to_the_rate_limiter(self):
        slept: list[float] = []
        now = [0.0]
        limiter = DomainRateLimiter(1.0, sleep=slept.append, clock=lambda: now[0])
        ctx = make_context(self.tmp, limiter=limiter)
        ctx.be_polite("example.com")
        now[0] = 0.2
        ctx.be_polite("example.com")
        self.assertEqual(len(slept), 1)

    def test_spend_reaches_the_shared_budget(self):
        budget = RunBudget(max_requests=2)
        ctx = make_context(self.tmp, budget=budget)
        ctx.spend()
        self.assertIsNone(ctx.budget_reason())
        ctx.spend()
        self.assertIn("request budget", ctx.budget_reason())

    def test_adapters_are_seeded_by_the_orchestrator(self):
        ctx = make_context(self.tmp, adapters={"google_maps": "the-stub"})
        self.assertEqual(ctx.adapters["google_maps"], "the-stub")


# --- the provider registry --------------------------------------------------


class ProviderRegistryTest(unittest.TestCase):
    """Every source describes itself well enough to publish a manifest."""

    def test_the_sources_are_registered_in_planning_order(self):
        # Round 026 P2.1 added ``local_pages`` (saved HTML read offline), last
        # because it never touches the network.
        self.assertEqual(
            PLANNING_ORDER,
            ("google_maps", "openstreetmap", "csv_list", "directory", "local_pages"),
        )
        self.assertEqual(
            sorted(PROVIDER_CLASSES),
            ["csv_list", "directory", "google_maps", "local_pages", "openstreetmap"],
        )

    def test_every_provider_declares_its_licence_and_attribution(self):
        # The task book asks for "来源和许可证说明齐全": a source whose licence
        # is blank is a manifest that cannot be published.
        for name, cls in PROVIDER_CLASSES.items():
            with self.subTest(provider=name):
                self.assertTrue(issubclass(cls, Provider))
                self.assertTrue(cls.name, "name is empty")
                self.assertTrue(cls.title, "title is empty")
                self.assertTrue(cls.license, "license is empty")
                self.assertTrue(cls.attribution, "attribution is empty")

    def test_the_directory_provider_records_where_its_design_came_from(self):
        # Business-Directory-Scraper has no LICENSE, so borrowing its layout is
        # allowed but must be declared, and the code must not have been copied.
        entry = PROVIDER_CLASSES["directory"](support.config()).describe()
        self.assertIn("no LICENSE", entry["provenance"])
        self.assertIn("no code was copied", entry["provenance"])

    def test_the_openstreetmap_provider_states_share_alike(self):
        entry = PROVIDER_CLASSES["openstreetmap"](support.config()).describe()
        self.assertIn("ODbL", entry["license"])
        self.assertIn("share-alike", entry["attribution"])

    def test_a_provider_cannot_be_instantiated_without_the_contract(self):
        with self.assertRaises(TypeError):
            Provider(cfg=None)  # type: ignore[abstract]

    def test_build_providers_only_builds_what_is_configured(self):
        cfg = source_config()
        providers = build_providers(cfg)
        self.assertEqual([provider.name for provider in providers], ["csv_list"])

    def test_manifest_marks_unconfigured_sources_without_hiding_them(self):
        cfg = source_config()
        entries = {entry["name"]: entry for entry in manifest(cfg)}
        self.assertEqual(entries["csv_list"]["planned"], True)
        # Not configured, but still listed *with the reason* — never silently
        # dropped, which is what makes "配了却不生效" visible.
        self.assertEqual(entries["openstreetmap"]["planned"], False)
        self.assertFalse(entries["openstreetmap"]["configured"])
        self.assertIn("not listed in sources", entries["openstreetmap"]["not_configured_reason"])


# --- csv_list: the deterministic control source -----------------------------


class CsvListProviderTest(unittest.TestCase):
    """The one source that touches no network, and therefore the control case."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def csv(self, text, name="list.csv"):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def provider(self, path, **settings):
        return self.provider_for(path, **settings)

    def provider_for(self, path, **settings):
        from collector.sources.csvlist import CsvListProvider

        cfg = source_config(
            {
                "sources": ["csv_list"],
                "sources_map": {"csv_list": {"path": path, **settings}},
            }
        )
        return CsvListProvider(cfg)

    def test_a_file_is_read_and_written_as_envelopes(self):
        path = self.csv(
            "name,address,phone,website,emails\n"
            "Praxis Nord,Musterstr. 1,+49 30 111,https://nord.example.de,a@n.de;b@n.de\n"
            "Praxis Sud,Sudstr. 2,+49 30 222,https://sud.example.de,c@sud.de\n"
        )
        provider = self.provider(path)
        (task,) = provider.tasks()
        ctx = make_context(self.tmp, fetcher=StubFetcher())
        outcome = provider.run_task(task, ctx)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 2)
        self.assertEqual(outcome.raw_path, ctx.raw_path(task.label, 1))
        envelopes = [
            json.loads(line)
            for line in outcome.raw_path.read_text(encoding="utf-8").splitlines()
        ]
        for envelope in envelopes:
            self.assertEqual(envelope["schema"], "osint.record/1")
            self.assertEqual(envelope["provider"], "csv_list")
        first = envelopes[0]["raw"]
        self.assertEqual(first["title"], "Praxis Nord")
        self.assertEqual(first["address"], "Musterstr. 1")
        self.assertEqual(first["emails"], ["a@n.de", "b@n.de"])
        self.assertEqual(first["source_id"], "row-1")
        self.assertEqual(first["source_id_kind"], "csv_row")

    def test_german_and_english_headers_are_both_recognised(self):
        # A file an operator actually has rarely uses the canonical names.
        path = self.csv(
            "Firma,Anschrift,Telefon,Webseite,E-Mail,Kategorie\n"
            "Zahnarztpraxis M\u00fcller,Hauptstr. 5,030 40367600,https://pm.example.de,x@pm.de,Zahnarzt\n"
        )
        (task,) = self.provider(path).tasks()
        outcome = self.provider(path).run_task(task, make_context(self.tmp))
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["title"], "Zahnarztpraxis M\u00fcller")
        self.assertEqual(raw["address"], "Hauptstr. 5")
        self.assertEqual(raw["phone"], "030 40367600")
        self.assertEqual(raw["web_site"], "https://pm.example.de")
        self.assertEqual(raw["category"], "Zahnarzt")

    def test_the_business_scope_column_is_carried_as_description(self):
        # 024: the industry match (021 A) reads `description`. A CSV that
        # carries the business's own scope must be able to prove an industry
        # with it, not fall back to the company name alone.
        path = self.csv(
            "name,address,description\n"
            "某某宠物店,浙江省杭州市西湖区某路1号,宠物服务；宠物食品及用品零售\n"
        )
        (task,) = self.provider(path).tasks()
        outcome = self.provider(path).run_task(task, make_context(self.tmp))
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["description"], "宠物服务；宠物食品及用品零售")

    def test_a_chinese_scope_header_maps_to_description(self):
        # A Chinese directory's own export spells the column 主营产品.
        path = self.csv("name,address,主营产品\n某某宠物店,杭州市西湖区,宠物用品批发\n")
        (task,) = self.provider(path).tasks()
        outcome = self.provider(path).run_task(task, make_context(self.tmp))
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["description"], "宠物用品批发")

    def test_native_keeps_the_operators_own_header_spelling(self):
        path = self.csv(
            "Firma,Anschrift\nPraxis Nord,Musterstr. 1\n"
        )
        (task,) = self.provider(path).tasks()
        outcome = self.provider(path).run_task(task, make_context(self.tmp))
        native = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]["native"]
        self.assertEqual(native, {"Firma": "Praxis Nord", "Anschrift": "Musterstr. 1"})

    def test_a_row_without_a_name_is_skipped_not_invented(self):
        # No title means nothing to attribute. Naming it after its category is
        # exactly what the task book forbids.
        path = self.csv(
            "name,category\nPraxis Nord,Zahnarzt\n,Zahnarzt\n"
        )
        (task,) = self.provider(path).tasks()
        outcome = self.provider(path).run_task(task, make_context(self.tmp))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 1)

    def test_an_operator_column_mapping_wins_over_a_guess(self):
        path = self.csv(
            "betrieb,strasse\nPraxis Nord,Musterstr. 1\n"
        )
        provider = self.provider(path, columns={"strasse": "address"})
        (task,) = provider.tasks()
        outcome = provider.run_task(task, make_context(self.tmp))
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["title"], "Praxis Nord")
        self.assertEqual(raw["address"], "Musterstr. 1")

    def test_a_configured_id_column_is_used_for_the_source_id(self):
        path = self.csv(
            "id,name\nexternal-42,Praxis Nord\n"
        )
        (task,) = self.provider(path, id_column="ID").tasks()
        outcome = self.provider(path, id_column="ID").run_task(
            task, make_context(self.tmp)
        )
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["source_id"], "external-42")

    def test_an_unknown_id_column_falls_back_to_the_row_number(self):
        path = self.csv(
            "name\nPraxis Nord\n"
        )
        (task,) = self.provider(path, id_column="nope").tasks()
        outcome = self.provider(path, id_column="nope").run_task(
            task, make_context(self.tmp)
        )
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["source_id"], "row-1")

    def test_a_missing_file_is_a_failure_not_a_crash(self):
        provider = self.provider(self.tmp / "never-written.csv")
        (task,) = provider.tasks()
        outcome = provider.run_task(task, make_context(self.tmp))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "missing_file")

    def test_an_exhausted_budget_skips_instead_of_reading(self):
        path = self.csv("name\nPraxis Nord\n")
        provider = self.provider(path)
        (task,) = provider.tasks()
        budget = RunBudget(max_requests=0)
        budget.spend(1)  # already used
        budget.max_requests = 1
        ctx = make_context(self.tmp, budget=budget)
        outcome = provider.run_task(task, ctx)
        self.assertFalse(outcome.ok)
        self.assertIn("budget", outcome.skipped_reason)

    def test_tasks_describe_the_file_without_touching_it(self):
        path = self.csv("name\nx\n")
        (task,) = self.provider(path).tasks()
        self.assertEqual(task.query, path)
        self.assertEqual(task.targets, (path,))
        self.assertEqual(task.as_dict()["query"], path)

    def test_a_single_column_file_does_not_crash_the_provider(self):
        # Sniffer raises on a file with no delimiter to find. That must be a
        # zero-row source, not an exception that kills the run.
        path = self.csv("name\nPraxis Nord\nPraxis Sud\n")
        (task,) = self.provider(path).tasks()
        outcome = self.provider(path).run_task(task, make_context(self.tmp))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 2)

    def test_a_semicolon_delimited_file_is_sniffed(self):
        path = self.csv("name;address\nPraxis Nord;Musterstr. 1\n")
        (task,) = self.provider(path).tasks()
        outcome = self.provider(path).run_task(task, make_context(self.tmp))
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["address"], "Musterstr. 1")

    def test_a_tab_delimited_file_is_sniffed(self):
        path = self.csv("name\taddress\nPraxis Nord\tMusterstr. 1\n")
        (task,) = self.provider(path).tasks()
        outcome = self.provider(path).run_task(task, make_context(self.tmp))
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["address"], "Musterstr. 1")

    def test_an_explicit_tab_setting_overrides_sniffing(self):
        path = self.csv("name\taddress\nPraxis Nord\tMusterstr. 1\n")
        provider = self.provider(path, delimiter="tab")
        (task,) = provider.tasks()
        outcome = provider.run_task(task, make_context(self.tmp))
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["address"], "Musterstr. 1")

    def test_a_utf8_bom_does_not_end_up_in_the_first_column_name(self):
        path = self.tmp / "bom.csv"
        path.write_bytes(
            "\ufeffname,address\nPraxis Nord,Musterstr. 1\n".encode("utf-8")
        )
        (task,) = self.provider(str(path)).tasks()
        outcome = self.provider(str(path)).run_task(task, make_context(self.tmp))
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["title"], "Praxis Nord")

    def test_not_configured_states_which_setting_is_missing(self):
        from collector.sources.csvlist import CsvListProvider

        cfg = support.config({"sources": ["google_maps"]})
        provider = CsvListProvider(cfg)
        self.assertFalse(provider.is_configured())
        self.assertIn("not listed in sources", provider.availability()[1])

    # -- provenance (round 026 P0) ------------------------------------------

    def test_the_import_is_stamped_as_an_import_not_a_visit(self):
        # Without a provenance block a csv_list record still defaults to
        # "import": the provider reads a file, so no record may read as a page
        # this run fetched.
        path = self.csv("name\nPraxis Nord\n")
        (task,) = self.provider(path).tasks()
        outcome = self.provider(path).run_task(task, make_context(self.tmp))
        context = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["context"]
        self.assertEqual(context["acquired_via"], "import")
        self.assertEqual(context["input_path"], path)

    def test_the_provenance_block_names_the_site_and_the_page_read(self):
        path = self.csv("name,source_url\nPraxis Nord,https://www.11467.com/qiye/1.htm\n")
        provider = self.provider(
            path,
            id_column="source_url",
            provenance={
                "acquired_via": "import",
                "origin": "www.11467.com",
                "source_page_url": "https://www.11467.com/hangzhou/search/9092.htm",
                "snapshot_id": "listing.html@abcdef12",
                "observed_at": "2026-10-13T21:11:21+08:00",
                "columns": {"source_url": "detail_url"},
            },
        )
        (task,) = provider.tasks()
        outcome = provider.run_task(task, make_context(self.tmp))
        context = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["context"]
        self.assertEqual(context["acquired_via"], "import")
        self.assertEqual(context["origin"], "www.11467.com")
        self.assertEqual(
            context["source_page_url"], "https://www.11467.com/hangzhou/search/9092.htm"
        )
        self.assertEqual(context["snapshot_id"], "listing.html@abcdef12")
        self.assertEqual(context["observed_at"], "2026-10-13T21:11:21+08:00")
        # The row's own link is a lead, never the page the facts were read from.
        self.assertEqual(context["detail_url"], "https://www.11467.com/qiye/1.htm")
        self.assertNotIn("detail_fetched", context)

    def test_a_per_row_column_overrides_the_file_wide_provenance(self):
        # A file that mixes two sites stamps each row; a row without the column
        # keeps the file-wide default.
        path = self.csv(
            "name,page\n"
            "Praxis Nord,https://a.example/list\n"
            "Praxis Sud,\n"
        )
        provider = self.provider(
            path,
            provenance={
                "source_page_url": "https://default.example/list",
                "columns": {"page": "source_page_url"},
            },
        )
        (task,) = provider.tasks()
        outcome = provider.run_task(task, make_context(self.tmp))
        lines = outcome.raw_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            json.loads(lines[0])["context"]["source_page_url"], "https://a.example/list"
        )
        self.assertEqual(
            json.loads(lines[1])["context"]["source_page_url"],
            "https://default.example/list",
        )

    def test_an_unknown_acquisition_method_in_the_config_is_refused(self):
        path = self.csv("name\nPraxis Nord\n")
        with self.assertRaises(Exception) as caught:
            self.provider(path, provenance={"acquired_via": "scraped"})
        self.assertIn("acquired_via", str(caught.exception))

    def test_an_unknown_provenance_column_target_is_refused(self):
        path = self.csv("name\nPraxis Nord\n")
        with self.assertRaises(Exception) as caught:
            self.provider(path, provenance={"columns": {"name": "not_a_key"}})
        self.assertIn("provenance.columns", str(caught.exception))


# --- openstreetmap: the second real discovery source ------------------------


OVERPASS_PAYLOAD = {
    "elements": [
        {
            "type": "node",
            "id": 123456,
            "lat": 52.5201,
            "lon": 13.4052,
            "tags": {
                "amenity": "dentist",
                "name": "Praxis Dr. M\u00fcller",
                "addr:street": "Hauptstra\u00dfe",
                "addr:housenumber": "5",
                "addr:postcode": "10115",
                "addr:city": "Berlin",
                "phone": "+49 30 40367600",
                "website": "https://praxis-mueller.example.de",
                "email": "info@praxis-mueller.example.de",
            },
        },
        {
            # A way has no lat/lon of its own; Overpass answers "out center".
            "type": "way",
            "id": 777,
            "center": {"lat": 52.51, "lon": 13.40},
            "tags": {"shop": "bakery", "name": "B\u00e4ckerei Sonne"},
        },
        {
            # No name and no id: nothing to attribute, and inventing a name
            # from the category is forbidden.
            "type": "node",
            "id": 999,
            "tags": {"amenity": "cafe"},
        },
    ]
}


class OpenStreetMapProviderTest(unittest.TestCase):
    """Discovery through Overpass: no account, no key, ODbL 1.0 data."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def provider(self, **settings):
        from collector.sources.openstreetmap import OpenStreetMapProvider

        cfg = support.config(
            {
                "sources": ["openstreetmap"],
                "sources_map": {"openstreetmap": settings},
            }
        )
        return OpenStreetMapProvider(cfg)

    def endpoint(self):
        from collector.sources.openstreetmap import OVERPASS_ENDPOINTS

        return OVERPASS_ENDPOINTS[0]

    def test_it_needs_a_place_and_says_so(self):
        from collector.config import Config, validate

        # Browser mode is the one that does not itself demand geo, so this is
        # the only way to reach the "configured but location-less" state: the
        # config layer is happy, and it is the *provider* that refuses to guess
        # a place. deep_merge keeps collect.fields intact, which a plain dict
        # replacement would wipe out.
        raw = support.deep_merge(
            support.BASE_CONFIG,
            {
                "collect": {"mode": "browser"},
                "sources": ["openstreetmap"],
            },
        )
        # Replace, do not merge: leaving the base config's ``geo`` in place
        # would quietly configure the place this test is checking is absent.
        raw["location"] = {"query": "x", "country": "DE", "city": "Berlin"}
        cfg = Config(validate(raw, source="<test>"), source="<test>")
        provider = self.provider()
        provider.cfg = cfg
        self.assertFalse(provider.is_configured())
        self.assertIn("bbox or location.geo", provider.availability()[1])

    def test_the_query_is_a_real_overpass_query(self):
        (task,) = self.provider().tasks()
        self.assertIn("nwr", task.query)
        self.assertIn('["amenity"="dentist"]', task.query)
        self.assertIn("[out:json]", task.query)
        self.assertIn("out center tags;", task.query)

    def test_a_configured_query_override_is_used_verbatim(self):
        (task,) = self.provider(query="[out:json];node[shop];out;").tasks()
        self.assertEqual(task.query, "[out:json];node[shop];out;")

    def test_the_area_clause_uses_a_bbox_when_one_is_configured(self):
        # A bbox cannot be combined with fast mode: the Go scraper rejects
        # -fast-mode with -grid-bbox. The config enforces it, so this test says
        # so rather than pretending a grid is a fast-mode option.
        cfg = support.config(
            {
                "collect": {"mode": "browser"},
                "sources": ["openstreetmap"],
                "location": {
                    "query": "x",
                    "country": "DE",
                    "bbox": [52.50, 13.30, 52.55, 13.45],
                },
            }
        )
        from collector.sources.openstreetmap import OpenStreetMapProvider

        self.assertEqual(
            OpenStreetMapProvider(cfg)._area_clause(), "52.5,13.3,52.55,13.45"
        )

    def test_the_area_clause_falls_back_to_around(self):
        self.assertIn("around:3000,", self.provider()._area_clause())

    def test_a_configured_radius_is_used(self):
        self.assertIn("around:500,", self.provider(radius_meters=500)._area_clause())

    def test_one_task_per_run_not_one_per_element(self):
        tasks = self.provider().tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].targets, (self.endpoint(),))

    def test_elements_become_canonical_records(self):
        provider = self.provider()
        (task,) = provider.tasks()
        fetcher = StubFetcher(
            {f"{self.endpoint()}?{_query_string(task.query)}": ("", "application/json")}
        )
        fetcher.pages[f"{self.endpoint()}?{_query_string(task.query)}"] = (
            json.dumps(OVERPASS_PAYLOAD),
            "application/json",
        )
        ctx = make_context(self.tmp, fetcher=fetcher)
        outcome = provider.run_task(task, ctx)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 2)  # the nameless one is unusable
        envelopes = [
            json.loads(line)
            for line in outcome.raw_path.read_text(encoding="utf-8").splitlines()
        ]
        first = envelopes[0]
        self.assertEqual(first["provider"], "openstreetmap")
        self.assertEqual(first["context"]["osm_elements"], 3)
        self.assertEqual(first["context"]["osm_unusable"], 1)

        raw = first["raw"]
        self.assertEqual(raw["title"], "Praxis Dr. M\u00fcller")
        self.assertEqual(raw["source_id"], "node/123456")
        self.assertEqual(raw["source_id_kind"], "osm")
        self.assertEqual(raw["latitude"], 52.5201)
        self.assertEqual(raw["longitude"], 13.4052)
        # ``native`` is the tag dict itself, so the evidence is what the source
        # actually said, unmodified.
        self.assertEqual(raw["native"]["amenity"], "dentist")
        self.assertEqual(raw["native"]["addr:city"], "Berlin")

    def test_a_way_uses_its_centre_for_coordinates(self):
        provider = self.provider()
        records, unusable = provider._elements(OVERPASS_PAYLOAD)
        bakery = [r for r in records if r.title.startswith("B\u00e4ckerei")][0]
        self.assertEqual(bakery.source_id, "way/777")
        self.assertEqual((bakery.latitude, bakery.longitude), (52.51, 13.40))
        self.assertEqual(unusable, 1)

    def test_a_payload_that_is_not_json_is_reported_not_crashed(self):
        provider = self.provider()
        (task,) = provider.tasks()
        fetcher = StubFetcher(
            {
                f"{self.endpoint()}?{_query_string(task.query)}": (
                    "<html>gateway error</html>",
                    "text/html",
                )
            }
        )
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "bad_payload")

    def test_a_failed_fetch_keeps_the_providers_error_kind(self):
        provider = self.provider()
        (task,) = provider.tasks()
        fetcher = StubFetcher(
            {
                f"{self.endpoint()}?{_query_string(task.query)}": FetchResult(
                    requested_url=self.endpoint(),
                    ok=False,
                    status_code=429,
                    error_kind="http_429",
                    error_detail="too many requests",
                    retry_after_seconds=3600,
                )
            }
        )
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "http_429")
        self.assertIn("too many requests", outcome.error)

    def test_an_empty_result_set_is_a_success_with_zero_records(self):
        # An honest empty search must not be an error: a small town may simply
        # have no dentist tagged in OSM yet.
        provider = self.provider()
        (task,) = provider.tasks()
        fetcher = StubFetcher(
            {
                f"{self.endpoint()}?{_query_string(task.query)}": (
                    json.dumps({"elements": []}),
                    "application/json",
                )
            }
        )
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 0)

    def test_an_exhausted_budget_skips_the_query(self):
        provider = self.provider()
        (task,) = provider.tasks()
        budget = RunBudget(max_requests=1)
        budget.spend(1)
        fetcher = StubFetcher()
        outcome = provider.run_task(task, make_context(self.tmp, budget=budget, fetcher=fetcher))
        self.assertFalse(outcome.ok)
        self.assertIn("budget", outcome.skipped_reason)
        self.assertEqual(fetcher.calls, [])  # nothing was requested

    def test_the_run_waits_for_the_endpoint_domain(self):
        provider = self.provider()
        (task,) = provider.tasks()
        slept: list[float] = []
        now = [0.0]
        limiter = DomainRateLimiter(1.0, sleep=slept.append, clock=lambda: now[0])
        fetcher = StubFetcher(
            {
                f"{self.endpoint()}?{_query_string(task.query)}": (
                    json.dumps({"elements": []}),
                    "application/json",
                )
            }
        )
        ctx = make_context(self.tmp, fetcher=fetcher, limiter=limiter)
        provider.run_task(task, ctx)
        now[0] = 0.2
        # A second task against the same endpoint must be slowed down.
        (task2,) = provider.tasks()
        task2.attempt = 2
        provider.run_task(task2, ctx)
        self.assertEqual(limiter.waits, 1)

    def test_not_configured_states_which_setting_is_missing(self):
        from collector.sources.openstreetmap import OpenStreetMapProvider

        cfg = support.config({"sources": ["google_maps"]})
        provider = OpenStreetMapProvider(cfg)
        self.assertFalse(provider.is_configured())
        self.assertIn("not listed in sources", provider.availability()[1])


# --- directory: a configurable business directory ---------------------------


def listing_page(*links, next_href=None, extra_body=""):
    parts = list(links)
    if next_href:
        parts.append(f"<a rel='next' href='{next_href}'>next</a>")
    return (
        "<!doctype html><html><body>"
        + "".join(f"<a href='{href}'>{text}</a>" for href, text in
                  [(link, text) for link, text in
                   [(l, l.rsplit("/", 1)[-1]) for l in links]])
        + "".join(f"<a rel='next' href='{next_href}'>next</a>" for _ in [0] if next_href)
        + extra_body
        + "</body></html>"
    )


class DirectoryProviderTest(unittest.TestCase):
    """A start URL, a detail pattern, per-field selectors and stop conditions."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def provider_for(self, entries, **settings):
        from collector.sources.directory import BusinessDirectoryProvider

        cfg = support.config(
            {
                "sources": ["directory"],
                "sources_map": {
                    "directory": {"entries": entries, **settings},
                },
            }
        )
        return BusinessDirectoryProvider(cfg)

    def test_it_needs_an_entry_with_a_start_url(self):
        from collector.config import Config

        cfg = Config(
            {"sources": ["directory"], "sources_map": {"directory": {"entries": []}}},
            source="<test>",
        )
        from collector.sources.directory import BusinessDirectoryProvider

        provider = BusinessDirectoryProvider(cfg)
        self.assertFalse(provider.is_configured())
        self.assertIn("entries", provider.availability()[1])

        # An entry without a usable start_url is not an entry at all.
        provider = BusinessDirectoryProvider(
            Config(
                {
                    "sources": ["directory"],
                    "sources_map": {"directory": {"entries": [{"name": "x"}]}},
                },
                source="<test>",
            )
        )
        self.assertFalse(provider.is_configured())

    def test_one_task_per_entry(self):
        provider = self.provider_for(
            [
                {"name": "one", "start_url": "https://dir1.example.de/"},
                {"name": "two", "start_url": "https://dir2.example.de/"},
            ]
        )
        tasks = provider.tasks()
        self.assertEqual([task.label for task in tasks],
                         ["discover-dir-one", "discover-dir-two"])
        self.assertEqual(tasks[0].targets, ("https://dir1.example.de/",))

    def test_a_detail_page_yields_a_record_with_its_links(self):
        listing = (
            "<a href='/firmen/praxis-nord'>Praxis Nord</a>"
            "<a href='https://elsewhere.example/outbound'>out</a>"
        )
        detail = (
            "<h1>Praxis Nord</h1>"
            "<div class='adr'>Musterstra\u00dfe 1, 10115 Berlin</div>"
            "<a href='mailto:info@praxis-nord.example'>mail</a>"
            "<a href='tel:+4930123456'>call</a>"
            "<a href='https://www.praxis-nord.example'>homepage</a>"
        )
        # The directory and the business live on *different registrable domains*:
        # a link that stays on the directory is not the business's website.
        pages = {
            "https://dir.example/": (listing, "text/html"),
            "https://dir.example/firmen/praxis-nord": (detail, "text/html"),
        }
        provider = self.provider_for(
            [
                {
                    "name": "demo",
                    "start_url": "https://dir.example/",
                    "detail_pattern": "/firmen/",
                    "field_selectors": {
                        "title": "h1",
                        "address": ".adr",
                    },
                }
            ]
        )
        (task,) = provider.tasks()
        fetcher = StubFetcher(pages)
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 1)
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["title"], "Praxis Nord")
        self.assertEqual(raw["address"], "Musterstra\u00dfe 1, 10115 Berlin")
        self.assertEqual(raw["emails"], ["info@praxis-nord.example"])
        self.assertEqual(raw["phone"], "+4930123456")
        # The directory's own domain is not the business's website.
        self.assertEqual(raw["web_site"], "https://www.praxis-nord.example")
        self.assertEqual(raw["source_id_kind"], "directory_url")
        # The outbound link is not followed: a listing's detail links stay on
        # the directory.
        self.assertNotIn("https://elsewhere.example/outbound", fetcher.calls)

    def test_the_map_and_route_controls_are_not_part_of_the_address(self):
        # The shape dasoertliche.de really serves: the address text, then two
        # in-page anchors for the map and the route planner, inside the same
        # container. Reading the whole subtree appends both to every address.
        listing = "<a href='/Themen/Praxis-Nord-Berlin'>Praxis Nord</a>"
        detail = (
            "<h1>Praxis Nord</h1>"
            "<div class='det_address'>"
            "Kurf\u00fcrstendamm&nbsp;56<br/>"
            "10707 Berlin"
            "<a href='#det_mapanchor' class='det_tomap noprint' title='Zum Kartenausschnitt'"
            " rel='nofollow'>Zum Kartenausschnitt</a>"
            "<a href='#det_mapanchor' id='route' title='Routenplaner'"
            " rel='nofollow'>Routenplaner</a>"
            "</div>"
        )
        pages = {
            "https://dir.example/": (listing, "text/html"),
            "https://dir.example/Themen/Praxis-Nord-Berlin": (detail, "text/html"),
        }
        provider = self.provider_for(
            [
                {
                    "name": "demo",
                    "start_url": "https://dir.example/",
                    "detail_pattern": "^/Themen/[^/]+$",
                    "field_selectors": {"title": "h1", "address": ".det_address"},
                }
            ]
        )
        (task,) = provider.tasks()
        outcome = provider.run_task(
            task, make_context(self.tmp, fetcher=StubFetcher(pages))
        )

        self.assertTrue(outcome.ok)
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["address"], "Kurf\u00fcrstendamm 56 10707 Berlin")

    def test_a_linked_street_name_is_still_kept(self):
        # The narrowness matters: a directory that links the street name must
        # not lose the street. Only in-page anchors, noprint controls and known
        # action phrases are dropped.
        listing = "<a href='/firmen/praxis-nord'>Praxis Nord</a>"
        detail = (
            "<h1>Praxis Nord</h1>"
            "<div class='adr'>"
            "<a href='https://www.openstreetmap.org/way/1'>Musterstra\u00dfe 1</a>"
            ", 10115 Berlin"
            "</div>"
        )
        pages = {
            "https://dir.example/": (listing, "text/html"),
            "https://dir.example/firmen/praxis-nord": (detail, "text/html"),
        }
        provider = self.provider_for(
            [
                {
                    "name": "demo",
                    "start_url": "https://dir.example/",
                    "detail_pattern": "/firmen/",
                    "field_selectors": {"title": "h1", "address": ".adr"},
                }
            ]
        )
        (task,) = provider.tasks()
        outcome = provider.run_task(
            task, make_context(self.tmp, fetcher=StubFetcher(pages))
        )

        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["address"], "Musterstra\u00dfe 1 , 10115 Berlin")

    def test_a_street_whose_name_contains_an_action_word_is_kept(self):
        # "Routenplanerweg" contains "Routenplaner" but is not the phrase, and a
        # span is not a control, so neither is touched.
        listing = "<a href='/firmen/praxis-nord'>Praxis Nord</a>"
        detail = (
            "<h1>Praxis Nord</h1>"
            "<div class='adr'>Routenplanerweg 7, 10115 Berlin <span>Anfahrt</span></div>"
        )
        pages = {
            "https://dir.example/": (listing, "text/html"),
            "https://dir.example/firmen/praxis-nord": (detail, "text/html"),
        }
        provider = self.provider_for(
            [
                {
                    "name": "demo",
                    "start_url": "https://dir.example/",
                    "detail_pattern": "/firmen/",
                    "field_selectors": {"title": "h1", "address": ".adr"},
                }
            ]
        )
        (task,) = provider.tasks()
        outcome = provider.run_task(
            task, make_context(self.tmp, fetcher=StubFetcher(pages))
        )

        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["address"], "Routenplanerweg 7, 10115 Berlin Anfahrt")

    def test_a_route_button_whose_text_is_an_action_phrase_is_dropped(self):
        # Not every directory uses an anchor: a <button> labelled with the action
        # is the same page chrome, and the phrase list catches it.
        listing = "<a href='/firmen/praxis-nord'>Praxis Nord</a>"
        detail = (
            "<h1>Praxis Nord</h1>"
            "<div class='adr'>Musterstra\u00dfe 1, 10115 Berlin"
            "<button type='button'>Route planen</button></div>"
        )
        pages = {
            "https://dir.example/": (listing, "text/html"),
            "https://dir.example/firmen/praxis-nord": (detail, "text/html"),
        }
        provider = self.provider_for(
            [
                {
                    "name": "demo",
                    "start_url": "https://dir.example/",
                    "detail_pattern": "/firmen/",
                    "field_selectors": {"title": "h1", "address": ".adr"},
                }
            ]
        )
        (task,) = provider.tasks()
        outcome = provider.run_task(
            task, make_context(self.tmp, fetcher=StubFetcher(pages))
        )

        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["address"], "Musterstra\u00dfe 1, 10115 Berlin")

    def test_an_operator_phone_selector_wins_over_the_link_label(self):
        # Selecting a[href^='tel:'] and taking its text would give "call"; the
        # default therefore has no phone selector at all. An operator who points
        # one at a real number element gets that number.
        detail = (
            "<h1>Praxis Nord</h1>"
            "<span class='tel'>+49 30 123456</span>"
            "<a href='tel:+4930999999'>call</a>"
        )
        pages = {
            "https://dir.example/": ("<a href='/f/p1'>p1</a>", "text/html"),
            "https://dir.example/f/p1": (detail, "text/html"),
        }
        provider = self.provider_for(
            [
                {
                    "name": "demo",
                    "start_url": "https://dir.example/",
                    "detail_pattern": "/f/",
                    "field_selectors": {"title": "h1", "phone": ".tel"},
                }
            ]
        )
        (task,) = provider.tasks()
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=StubFetcher(pages)))
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8"))["raw"]
        self.assertEqual(raw["phone"], "+49 30 123456")

    def test_pagination_is_followed_and_each_page_is_fetched_once(self):
        pages = {
            "https://dir.example/": (
                "<a href='/f/p1'>p1</a><a rel='next' href='/p/2'>2</a>",
                "text/html",
            ),
            "https://dir.example/p/2": (
                "<a href='/f/p2'>p2</a><a rel='next' href='/p/3'>3</a>",
                "text/html",
            ),
            "https://dir.example/p/3": ("<a href='/f/p3'>p3</a>", "text/html"),
            **{
                f"https://dir.example/f/p{n}": (f"<h1>Betrieb {n}</h1>", "text/html")
                for n in (1, 2, 3)
            },
        }
        provider = self.provider_for(
            [
                {
                    "name": "demo",
                    "start_url": "https://dir.example/",
                    "detail_pattern": "/f/",
                }
            ]
        )
        (task,) = provider.tasks()
        fetcher = StubFetcher(pages)
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 3)
        # Every listing page exactly once, and no detail page twice.
        listings = [url for url in fetcher.calls if "/p/" in url or url.endswith(".example/")]
        self.assertEqual(sorted(listings), sorted({
            "https://dir.example/",
            "https://dir.example/p/2",
            "https://dir.example/p/3",
        }))
        self.assertEqual(len(fetcher.calls), len(set(fetcher.calls)))

    def test_a_repeated_page_stops_the_walk(self):
        # The "next" link points at a URL not yet read, but it serves the same
        # rows, so continuing would spend the run's budget re-collecting links.
        same_rows = "<a href='/f/p1'>p1</a>"
        pages = {
            "https://dir.example/": (same_rows + "<a rel='next' href='/p/2'>2</a>", "text/html"),
            "https://dir.example/p/2": (same_rows + "<a rel='next' href='/p/3'>3</a>", "text/html"),
            "https://dir.example/p/3": (same_rows, "text/html"),
            "https://dir.example/f/p1": ("<h1>Betrieb 1</h1>", "text/html"),
        }
        provider = self.provider_for(
            [{"name": "demo", "start_url": "https://dir.example/",
              "detail_pattern": "/f/", "stop_after_repeated_pages": 1}]
        )
        (task,) = provider.tasks()
        fetcher = StubFetcher(pages)
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))

        self.assertTrue(outcome.ok)
        self.assertNotIn("https://dir.example/p/3", fetcher.calls)
        self.assertEqual(outcome.records, 1)

    def test_a_self_referencing_next_link_stops_the_walk(self):
        pages = {
            "https://dir.example/": (
                "<a href='/f/p1'>p1</a><a rel='next' href='/'>again</a>",
                "text/html",
            ),
            "https://dir.example/f/p1": ("<h1>Betrieb 1</h1>", "text/html"),
        }
        provider = self.provider_for(
            [{"name": "demo", "start_url": "https://dir.example/",
              "detail_pattern": "/f/"}]
        )
        (task,) = provider.tasks()
        fetcher = StubFetcher(pages)
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))
        self.assertTrue(outcome.ok)
        self.assertEqual(fetcher.calls.count("https://dir.example/"), 1)

    def test_a_head_level_link_rel_next_is_followed(self):
        # Real directories publish the next-page hint as <link rel="next"> in
        # <head>, not as an anchor in the body. Reading only anchors makes a
        # multi-page directory look like a single page and silently drops
        # every listing after the first.
        pages = {
            "https://dir.example/": (
                "<!doctype html><html><head>"
                "<link rel='next' href='/p/2'></head><body>"
                "<a href='/f/p1'>p1</a></body></html>",
                "text/html",
            ),
            "https://dir.example/p/2": (
                "<!doctype html><html><body><a href='/f/p2'>p2</a></body></html>",
                "text/html",
            ),
            "https://dir.example/f/p1": ("<h1>Betrieb 1</h1>", "text/html"),
            "https://dir.example/f/p2": ("<h1>Betrieb 2</h1>", "text/html"),
        }
        provider = self.provider_for(
            [{"name": "demo", "start_url": "https://dir.example/",
              "detail_pattern": "/f/"}]
        )
        (task,) = provider.tasks()
        fetcher = StubFetcher(pages)
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))

        self.assertTrue(outcome.ok)
        self.assertIn("https://dir.example/p/2", fetcher.calls)
        self.assertEqual(outcome.records, 2)

    def test_an_empty_listing_page_stops_after_the_configured_count(self):
        # ``detail_pattern`` keeps the pagination links out of the detail set,
        # which is what makes "empty" mean "no businesses on this page".
        pages = {
            "https://dir.example/": ("<a rel='next' href='/p/2'>2</a>", "text/html"),
            "https://dir.example/p/2": ("<a rel='next' href='/p/3'>3</a>", "text/html"),
            "https://dir.example/p/3": ("<a rel='next' href='/p/4'>4</a>", "text/html"),
        }
        provider = self.provider_for(
            [{"name": "demo", "start_url": "https://dir.example/",
              "detail_pattern": "/f/", "stop_after_empty_pages": 2}]
        )
        (task,) = provider.tasks()
        fetcher = StubFetcher(pages)
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 0)
        # Two pages read: the first, and the second empty one that reached the
        # configured count. The third is never fetched — that is the stop.
        self.assertNotIn("https://dir.example/p/3", fetcher.calls)
        self.assertEqual(len(fetcher.calls), 2)

    def test_an_unsupported_selector_is_refused_at_config_time(self):
        # Better than a runtime failure: a typo never reaches a run at all, so
        # it cannot masquerade as "the directory has no businesses".
        from collector.errors import ConfigError

        with self.assertRaises(ConfigError) as caught:
            self.provider_for(
                [
                    {
                        "name": "demo",
                        "start_url": "https://dir.example/",
                        "detail_pattern": "/f/",
                        "field_selectors": {"title": "h1", "phone": "a:contains('x')"},
                    }
                ]
            )
        self.assertIn("does not support", str(caught.exception))

    def test_a_valid_selector_config_passes_validation(self):
        provider = self.provider_for(
            [
                {
                    "name": "demo",
                    "start_url": "https://dir.example/",
                    "detail_pattern": "/f/",
                    "field_selectors": {
                        "title": "h1",
                        "phone": "a[href^='tel:']",
                        "address": "[class~='adr']",
                    },
                }
            ]
        )
        self.assertTrue(provider.is_configured())

    def test_a_detail_page_without_a_name_is_dropped_not_invented(self):
        pages = {
            "https://dir.example/": ("<a href='/f/p1'>p1</a>", "text/html"),
            "https://dir.example/f/p1": ("<div class='adr'>Musterstr. 1</div>", "text/html"),
        }
        provider = self.provider_for(
            [{"name": "demo", "start_url": "https://dir.example/",
              "detail_pattern": "/f/"}]
        )
        (task,) = provider.tasks()
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=StubFetcher(pages)))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 0)

    def test_the_detail_page_limit_is_respected(self):
        pages = {
            "https://dir.example/": (
                "".join(f"<a href='/f/p{n}'>p{n}</a>" for n in range(1, 6)),
                "text/html",
            ),
            **{
                f"https://dir.example/f/p{n}": (f"<h1>Betrieb {n}</h1>", "text/html")
                for n in range(1, 6)
            },
        }
        provider = self.provider_for(
            [{"name": "demo", "start_url": "https://dir.example/",
              "detail_pattern": "/f/", "max_detail_pages": 2}]
        )
        (task,) = provider.tasks()
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=StubFetcher(pages)))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 2)

    def test_one_task_spends_one_request_not_one_per_page(self):
        # max_requests counts *tasks*, uniformly across providers. Counting the
        # pages as well spent the budget twice over and starved the walk.
        pages = {
            "https://dir.example/": (
                "".join(f"<a href='/f/p{n}'>p{n}</a>" for n in range(1, 6)),
                "text/html",
            ),
            **{
                f"https://dir.example/f/p{n}": (f"<h1>Betrieb {n}</h1>", "text/html")
                for n in range(1, 6)
            },
        }
        provider = self.provider_for(
            [{"name": "demo", "start_url": "https://dir.example/",
              "detail_pattern": "/f/", "max_detail_pages": 3}]
        )
        (task,) = provider.tasks()
        budget = RunBudget(max_requests=1)
        fetcher = StubFetcher(pages)
        outcome = provider.run_task(
            task, make_context(self.tmp, budget=budget, fetcher=fetcher)
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(budget.used_requests, 1)
        self.assertEqual(outcome.records, 3)  # bounded by max_detail_pages
        self.assertEqual(len(fetcher.calls), 4)

    def test_an_exhausted_budget_skips_the_task_before_it_fetches(self):
        pages = {"https://dir.example/": ("<a href='/f/p1'>p1</a>", "text/html")}
        provider = self.provider_for(
            [{"name": "demo", "start_url": "https://dir.example/",
              "detail_pattern": "/f/"}]
        )
        (task,) = provider.tasks()
        budget = RunBudget(max_requests=1)
        budget.spend(1)
        fetcher = StubFetcher(pages)
        outcome = provider.run_task(
            task, make_context(self.tmp, budget=budget, fetcher=fetcher)
        )
        self.assertFalse(outcome.ok)
        self.assertIn("budget", outcome.skipped_reason)
        self.assertEqual(fetcher.calls, [])

    def test_a_website_field_is_read_through_a_selector_list(self):
        # The adapter's own default website selector is a comma list. It has to
        # resolve to a union: stage 019 found that while it was refused, every
        # row fell back to a page heuristic and carried the directory's own
        # installer as its "website".
        listing = ("<a href='/c168-1.html'>one</a>")
        detail = (
            "<h1>Firma</h1>"
            "<a href='https://firma.example/'>Firma Website</a>"
        )
        pages = {
            "https://dir.example/": (listing, "text/html"),
            "https://dir.example/c168-1.html": (detail, "text/html"),
        }
        provider = self.provider_for(
            [{"name": "demo", "start_url": "https://dir.example/",
              "detail_pattern": "/c168-",
              "field_selectors": {
                  "title": "h1",
                  "website": "a[href*='website='], a[href*='url=']",
              }}]
        )
        (task,) = provider.tasks()
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=StubFetcher(pages)))
        self.assertEqual(outcome.records, 1)

    def test_the_directories_own_download_is_not_a_business_website(self):
        from collector.selector import parse_html
        from collector.sources.directory import _own_website

        # b2b168.com links its desktop client from a sibling domain; measured
        # on 17/17 real rows, every one came back with this as its "website".
        root = parse_html(
            "<a href='https://tz-admin.b2b168.net/soft/app/%E5%AE%A2%E6%9C%8D.exe'>"
            "\u5ba2\u670d</a>"
        )
        website = _own_website(
            root, "https://www.b2b168.com/c168-1.html", "https://www.b2b168.com/x/"
        )
        self.assertIsNone(website)

    def test_a_download_link_on_another_host_is_not_a_website(self):
        from collector.selector import parse_html
        from collector.sources.directory import _own_website

        root = parse_html("<a href='https://cdn.example.net/setup.exe'>setup</a>")
        website = _own_website(
            root, "https://dir.example/c168-1.html", "https://dir.example/x/"
        )
        self.assertIsNone(website)

    def test_a_real_external_site_is_still_kept(self):
        from collector.selector import parse_html
        from collector.sources.directory import _own_website

        root = parse_html("<a href='https://firma.example/'>Website</a>")
        website = _own_website(
            root, "https://dir.example/c168-1.html", "https://dir.example/x/"
        )
        self.assertEqual(website, "https://firma.example/")

    def test_a_government_portal_is_not_a_business_website(self):
        from collector.selector import parse_html
        from collector.sources.directory import _own_website

        # The only external link on a b2b168.com detail page is the MIIT filing
        # portal, which Chinese sites must link in the footer.
        root = parse_html(
            "<a href='https://beian.miit.gov.cn/'>\u7ca4ICP\u590710089450\u53f7-8</a>"
        )
        website = _own_website(
            root, "https://www.b2b168.com/c168-1.html", "https://www.b2b168.com/x/"
        )
        self.assertIsNone(website)


# --- directory: the container the businesses are listed in -------------------


class DirectoryListScopeTest(unittest.TestCase):
    """059: ``list_selector`` must confine the live harvest, not just the import.

    The offline importer has honoured this key since round 034; the live walk
    ignored it, so writing it down did nothing and no one was told. The failure
    it leaves open is the one 059 asked about: a business link that sits in the
    page's furniture rather than in the list becoming a row. Every test here is
    offline and needs no network.
    """

    #: One page: two companies in the list, one in a recommendation strip.
    PAGE = (
        "<html><body>"
        "<div class='list-right'><div class='cations'><ul class='list'>"
        "<li><div class='biaoti'><a href='/c168-1.html'>Listed One</a></div></li>"
        "<li><div class='biaoti'><a href='/c168-2.html'>Listed Two</a></div></li>"
        "</ul></div></div>"
        "<div class='tuijian'><a href='/c168-3.html'>Recommended Three</a></div>"
        "</body></html>"
    )

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def harvest(self, entry):
        """Run one directory entry over the page; return (calls, context)."""
        from collector.sources.directory import BusinessDirectoryProvider

        cfg = support.config(
            {
                "sources": ["directory"],
                "sources_map": {"directory": {"entries": [entry]}},
            }
        )
        provider = BusinessDirectoryProvider(cfg)
        (task,) = provider.tasks()
        pages = {
            "https://dir.example/": (self.PAGE, "text/html"),
            "https://dir.example/c168-1.html": ("<h1>Listed One</h1>", "text/html"),
            "https://dir.example/c168-2.html": ("<h1>Listed Two</h1>", "text/html"),
            "https://dir.example/c168-3.html": ("<h1>Recommended Three</h1>", "text/html"),
        }
        fetcher = StubFetcher(pages)
        outcome = provider.run_task(task, make_context(self.tmp, fetcher=fetcher))
        self.assertTrue(outcome.ok)
        # The raw file is JSONL -- one envelope per record.
        first = outcome.raw_path.read_text(encoding="utf-8").splitlines()[0]
        return fetcher.calls, json.loads(first)["context"]

    def test_the_recommendation_strip_is_not_harvested_when_a_container_is_named(self):
        calls, context = self.harvest({
            "name": "demo",
            "start_url": "https://dir.example/",
            "detail_pattern": r"/c168-\d+\.html",
            "list_selector": "ul.list",
        })
        fetched = [u for u in calls if "/c168-" in u]
        self.assertEqual(len(fetched), 2)
        self.assertNotIn("https://dir.example/c168-3.html", fetched)
        self.assertEqual(context["listing_scope"], "restricted")

    def test_without_a_container_the_harvest_is_page_wide_and_says_so(self):
        calls, context = self.harvest({
            "name": "demo",
            "start_url": "https://dir.example/",
            "detail_pattern": r"/c168-\d+\.html",
        })
        fetched = [u for u in calls if "/c168-" in u]
        self.assertEqual(len(fetched), 3)
        self.assertEqual(context["listing_scope"], "not_configured")

    def test_a_container_that_matches_nothing_is_reported_not_fatal(self):
        _calls, context = self.harvest({
            "name": "demo",
            "start_url": "https://dir.example/",
            "detail_pattern": r"/c168-\d+\.html",
            "list_selector": "ul.there-is-no-such-list",
        })
        self.assertEqual(context["listing_scope"], "unrestricted_no_match")
        self.assertIn("没匹配到任何容器", context["listing_scope_note"])
        # The honest outcome is a wider harvest with a sentence, not a dead run.
        self.assertEqual(context["detail_urls_seen"], 3)

    def test_a_container_the_engine_refuses_is_reported_not_fatal(self):
        _calls, context = self.harvest({
            "name": "demo",
            "start_url": "https://dir.example/",
            "detail_pattern": r"/c168-\d+\.html",
            "list_selector": "ul[[[broken",
        })
        self.assertEqual(context["listing_scope"], "unrestricted_selector_error")
        self.assertIn("无法求值", context["listing_scope_note"])
        self.assertEqual(context["detail_urls_seen"], 3)

    def test_the_saved_b2b168_page_yields_the_same_links_with_and_without_it(self):
        # The point of naming a container is to stop *future* furniture from
        # becoming rows -- not to change what this page yields today.
        from collector.selector import parse_html, select
        from collector.sources.directory import detail_anchor_elements

        path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "verification" / "cn" / "b2b168-hangzhou-chongwu.html"
        )
        if not path.is_file():
            self.skipTest(f"样本页不在：{path}")
        root = parse_html(path.read_text(encoding="utf-8", errors="replace"))
        base = "https://www.b2b168.com/zhejiang-hangzhou-chongwu/"
        pattern = __import__("re").compile(r"/c168-\d+\.html")
        wide = {u for u, _t, _e in detail_anchor_elements(root, base, pattern)}
        narrow = {
            u for u, _t, _e in detail_anchor_elements(
                root, base, pattern, within=select(root, "ul.list")
            )
        }
        self.assertEqual(len(wide), 23)
        self.assertEqual(narrow, wide)








# --- google_maps: the Go scraper, wrapped -----------------------------------


class GoogleMapsProviderTest(unittest.TestCase):
    """The stage A-D source, reached through the same provider contract."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def test_one_task_per_query_with_a_slug_label(self):
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config(
            {
                "location": {
                    "query": None,
                    "queries": ["dentists in Berlin", "dentists in Hamburg"],
                }
            }
        )
        provider = GoogleMapsProvider(cfg)
        tasks = provider.tasks()
        self.assertEqual(
            [task.label for task in tasks],
            ["discover-dentists-in-Berlin", "discover-dentists-in-Hamburg"],
        )
        self.assertEqual(tasks[0].query, "dentists in Berlin")
        self.assertEqual(tasks[0].targets, ("dentists in Berlin",))

    def test_it_declares_that_the_data_is_googles_not_the_tools(self):
        from collector.sources.google_maps import GoogleMapsProvider

        entry = GoogleMapsProvider(support.config()).describe()
        self.assertIn("MIT", entry["license"])
        # The tool's licence is not the data's: redistribution is explicitly
        # out of scope rather than left for the operator to assume.
        self.assertIn("redistribution is not covered", entry["attribution"])
        self.assertIn("Google Maps", entry["attribution"])
        self.assertFalse(entry["requires_account"])

    def test_the_orchestrators_adapter_is_reused_not_rebuilt(self):
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config()
        provider = GoogleMapsProvider(cfg)
        marker = object()
        ctx = make_context(self.tmp, adapters={"google_maps": marker})
        # A stub adapter keeps a run started with a stub using that stub.
        self.assertIs(provider.adapter(ctx), marker)

    def test_the_query_is_handed_over_a_file_not_the_command_line(self):
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config()
        provider = GoogleMapsProvider(cfg)
        (task,) = provider.tasks()
        ctx = make_context(self.tmp, adapters={"google_maps": support.stub_adapter()})
        outcome = provider.run_task(task, ctx)

        self.assertTrue(outcome.ok)
        query_file = ctx.run_dir / "queries" / f"{task.label}.txt"
        self.assertEqual(query_file.read_text(encoding="utf-8").strip(), task.query)
        # The raw envelopes carry the query, so nothing is embedded in argv.
        raw = json.loads(outcome.raw_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(raw["context"]["query"], task.query)
        self.assertEqual(raw["schema"], "osint.record/1")

    def test_a_failing_scraper_is_reported_with_its_exit_code(self):
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config()
        provider = GoogleMapsProvider(cfg)
        (task,) = provider.tasks()
        ctx = make_context(
            self.tmp, adapters={"google_maps": support.stub_adapter({"STUB_EXIT": "7"})}
        )
        outcome = provider.run_task(task, ctx)

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "go_failure")
        self.assertIn("exit code 7", outcome.error)
        self.assertEqual(outcome.exit_code, 7)

    def test_a_timeout_is_reported_as_go_timeout(self):
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config()
        provider = GoogleMapsProvider(cfg)
        (task,) = provider.tasks()
        # A one-second budget leaves no room for a 20-second stub.
        budget = RunBudget(max_seconds=1)
        ctx = make_context(
            self.tmp,
            budget=budget,
            adapters={"google_maps": support.stub_adapter({"STUB_SLEEP": "20"})},
        )
        outcome = provider.run_task(task, ctx)

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "go_timeout")
        self.assertIn("timeout", outcome.error)

    def test_a_scraper_that_exits_zero_with_every_job_failed_is_not_success(self):
        # Real failure mode: the proxy refuses the connection, the scraper logs
        # "job finished" with status failed for every job, writes a zero-byte
        # result file and still exits 0. Counting that as success makes the run
        # report "completed" with a source silently missing.
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config()
        provider = GoogleMapsProvider(cfg)
        (task,) = provider.tasks()
        ctx = make_context(
            self.tmp,
            adapters={"google_maps": support.stub_adapter({"STUB_JOB_STATUS": "failed"})},
        )
        outcome = provider.run_task(task, ctx)

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "all_jobs_failed")
        self.assertIn("all 1 job(s) failed", outcome.error)

    def test_an_empty_search_that_really_ran_is_still_success(self):
        # The counter-case: no records, but the job itself succeeded. That is a
        # legitimate empty result and must not be turned into a failure.
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config()
        provider = GoogleMapsProvider(cfg)
        (task,) = provider.tasks()
        ctx = make_context(
            self.tmp,
            adapters={
                "google_maps": support.stub_adapter(
                    {"STUB_JOB_STATUS": "success", "STUB_RECORDS": "0"}
                )
            },
        )
        outcome = provider.run_task(task, ctx)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 0)

    def test_one_failed_job_among_several_is_not_a_total_failure(self):
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config()
        provider = GoogleMapsProvider(cfg)
        (task,) = provider.tasks()
        ctx = make_context(
            self.tmp,
            adapters={"google_maps": support.stub_adapter({"STUB_JOB_STATUS": "failed,success"})},
        )
        outcome = provider.run_task(task, ctx)

        self.assertTrue(outcome.ok)

    def test_a_stub_with_no_job_log_is_not_treated_as_a_failure(self):
        # Abstain rather than mis-report: no job lines means the check cannot
        # tell, and the previous behaviour (exit code decides) must stand.
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config()
        provider = GoogleMapsProvider(cfg)
        (task,) = provider.tasks()
        ctx = make_context(self.tmp, adapters={"google_maps": support.stub_adapter()})
        outcome = provider.run_task(task, ctx)

        self.assertTrue(outcome.ok)

    def test_the_raw_file_has_one_envelope_per_scraped_record(self):
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config()
        provider = GoogleMapsProvider(cfg)
        (task,) = provider.tasks()
        ctx = make_context(
            self.tmp,
            adapters={"google_maps": support.stub_adapter({"STUB_RECORDS": "5"})},
        )
        outcome = provider.run_task(task, ctx)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.records, 5)
        self.assertEqual(
            read_envelope_count(outcome.raw_path), 5
        )

    def test_the_raw_file_is_per_attempt_so_a_retry_keeps_the_first(self):
        from collector.sources.google_maps import GoogleMapsProvider

        cfg = support.config()
        provider = GoogleMapsProvider(cfg)
        (task,) = provider.tasks()
        ctx = make_context(self.tmp, adapters={"google_maps": support.stub_adapter()})
        first = provider.run_task(task, ctx)

        task.attempt = 2
        second = provider.run_task(task, ctx)

        self.assertNotEqual(first.raw_path, second.raw_path)
        self.assertTrue(first.raw_path.is_file())
        self.assertTrue(second.raw_path.is_file())
