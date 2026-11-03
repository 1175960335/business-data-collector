"""The local control screen: config building, the run manager, the pages."""

from __future__ import annotations

import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from collector import local_ui
from collector.errors import CollectorError

from . import support


def form(**overrides):
    base = {
        "industry": ["dentists"],
        "city": ["Berlin"],
        "country": ["DE"],
        "query": [""],
        "target_count": ["12"],
        "mode": ["fast"],
        "formats": ["csv,jsonl,xlsx"],
        "enrich": ["off"],
    }
    base.update(overrides)
    return base


class FileSizeTest(unittest.TestCase):
    """The download list's size column. 28 KB is not 0.0 KB."""

    def setUp(self):
        self.tmp = support.temp_dir(self)

    def size(self, name, byte_count):
        path = self.tmp / name
        path.write_bytes(b"x" * byte_count)
        return local_ui._fmt_size(path)

    def test_a_kilobyte_file_is_not_rounded_to_zero(self):
        # The regression: the old code divided by 1024 twice, so 28084 bytes
        # came out as "0.0 KB".
        self.assertEqual(self.size("a.xlsx", 28084), "27.4 KB")

    def test_bytes_megabytes_and_gigabytes_read_sensibly(self):
        self.assertEqual(self.size("b.bin", 512), "512 B")
        self.assertEqual(self.size("c.bin", 2048), "2.0 KB")
        self.assertEqual(self.size("d.bin", 5 * 1024 * 1024), "5.0 MB")

    def test_a_missing_file_is_a_question_mark_not_a_crash(self):
        self.assertEqual(local_ui._fmt_size(self.tmp / "nope"), "?")


class ExportRoleTest(unittest.TestCase):
    """The download list must say which file is the one to read."""

    def test_the_bilingual_file_is_recommended_and_ordered_first(self):
        label, _note, recommended = local_ui._file_role("entities_bilingual.xlsx")
        self.assertTrue(recommended)
        self.assertIn("推荐阅读", label)

    def test_the_technical_files_are_labelled_as_technical(self):
        for name in ("entities.xlsx", "entities.csv", "field_facts.jsonl"):
            label, _note, recommended = local_ui._file_role(name)
            self.assertIn("技术版", label)
            self.assertFalse(recommended)

    def test_the_recommended_file_sorts_to_the_top(self):
        import pathlib

        files = [
            pathlib.Path("entities.xlsx"),
            pathlib.Path("entities_bilingual.xlsx"),
            pathlib.Path("entities.csv"),
        ]
        ordered = local_ui._ordered_exports(files)
        self.assertEqual(ordered[0].name, "entities_bilingual.xlsx")

    def test_the_language_files_are_named_so_the_language_is_obvious(self):
        # Stage 019: the download names must tell the three languages apart,
        # and each readable one must be marked recommended.
        for name, keyword in (
            ("entities_zh.xlsx", "中文"),
            ("entities_en.xlsx", "英文"),
            ("entities_bilingual.xlsx", "双语"),
        ):
            with self.subTest(name=name):
                label, note, recommended = local_ui._file_role(name)
                self.assertIn(keyword, label)
                self.assertTrue(recommended)
                self.assertTrue(note)

    def test_a_chinese_run_shows_the_chinese_file_first(self):
        import pathlib

        ordered = local_ui._ordered_exports(
            [pathlib.Path("entities.xlsx"), pathlib.Path("entities_zh.xlsx")]
        )
        self.assertEqual(ordered[0].name, "entities_zh.xlsx")


class ConfigBuildingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.base = self.tmp / "project"

    def test_the_form_becomes_a_valid_config(self):
        cfg = local_ui.build_config(form(), base_dir=self.base, name="ui-test")
        self.assertEqual(cfg.industry, "dentists")
        self.assertEqual(cfg.query, "dentists in Berlin")
        self.assertEqual(cfg.target_count, 12)
        self.assertEqual(cfg.mode, "fast")
        self.assertFalse(cfg.enrich_enabled)
        self.assertEqual(cfg.formats, ["csv", "jsonl", "xlsx"])
        # Both stage E sources and the score columns are part of a daily run.
        self.assertEqual(cfg.sources_list, ["google_maps", "openstreetmap"])
        self.assertIn("score_composite", cfg.export_fields)

    def test_defaults_fill_a_blank_form(self):
        cfg = local_ui.build_config(form(industry=[""], query=[""], target_count=["abc"]),
                                    base_dir=self.base, name="blank")
        self.assertEqual(cfg.industry, "dentists")
        self.assertEqual(cfg.target_count, 30)
        self.assertEqual(cfg.query, "dentists in Berlin")

    def test_enrich_can_be_switched_on_from_the_screen(self):
        cfg = local_ui.build_config(form(enrich=["on"]), base_dir=self.base, name="enrich")
        self.assertTrue(cfg.enrich_enabled)
        self.assertEqual(cfg.enrich_fields, ["title", "emails", "phone", "address", "website"])

    def test_an_unknown_mode_falls_back_instead_of_failing_the_run(self):
        cfg = local_ui.build_config(form(mode=["wat"]), base_dir=self.base, name="mode")
        self.assertEqual(cfg.mode, "fast")

    def test_the_written_config_is_a_real_file_that_loads(self):
        cfg = local_ui.build_config(form(), base_dir=self.base, name="written")
        path = local_ui.write_config(cfg, self.tmp / "generated" / "written.yaml")
        self.assertTrue(path.is_file())
        from collector.config import load

        loaded = load(path)
        self.assertEqual(loaded.query, cfg.query)
        self.assertEqual(loaded.fingerprint(), cfg.fingerprint())

    def test_a_form_cannot_smuggle_in_unknown_config(self):
        # Only the documented fields are read; anything else in the form is
        # ignored, which keeps a stale bookmarked form from breaking a run.
        cfg = local_ui.build_config(
            {**form(), "evil": ["rm -rf"]}, base_dir=self.base, name="no-smuggling"
        )
        self.assertNotIn("evil", json.dumps(cfg.data))


class SiteDiscoveryFormTest(unittest.TestCase):
    """026 P1: the discovery switch is usable from the screen, not YAML-only."""

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.base = self.tmp / "project"
        self.sites = support.write_site_list(
            self.tmp / "sites.csv", [("", "Praxis Beispiel", "https://praxis.example/")]
        )
        manifest = self.tmp / "pages.csv"
        manifest.write_text(
            "name,path,page\n"
            f"Praxis Beispiel,{self.tmp / 'listing.html'},https://www.11467.com/qiye/1.htm\n",
            encoding="utf-8",
        )
        self.pages = manifest

    def build(self, **overrides):
        return local_ui.build_config(
            form(**overrides), base_dir=self.base, name="discovery"
        )

    def test_off_by_default_so_the_config_is_unchanged(self):
        cfg = self.build()
        self.assertFalse(cfg.sitediscovery_enabled)
        self.assertNotIn("sitediscovery", cfg.collection_view())
        # Switching it off leaves the fingerprint exactly as it was, so a run
        # started from the screen is still resumable by a build that predates
        # the feature.
        plain = local_ui.build_config(
            form(sitediscovery=["off"]), base_dir=self.base, name="discovery"
        )
        self.assertEqual(cfg.fingerprint(), plain.fingerprint())

    def test_switching_it_on_reads_both_files(self):
        cfg = self.build(
            sitediscovery=["on"],
            sitediscovery_sites=[str(self.sites)],
            sitediscovery_pages=[str(self.pages)],
            sitediscovery_budget=["25"],
        )
        self.assertTrue(cfg.sitediscovery_enabled)
        self.assertEqual(cfg.sitediscovery_request_budget, 25)
        self.assertEqual(cfg.sitediscovery["user_list"]["path"], str(self.sites))
        self.assertEqual(cfg.sitediscovery["page_links"]["manifest"], str(self.pages))
        # ``search`` is listed even though it is unavailable, so the run's report
        # states that no search happened instead of leaving it to be inferred.
        self.assertIn("search", cfg.sitediscovery_sources)

    def test_the_manifest_file_is_read_into_page_entries(self):
        from collector.sitediscovery import read_pages_manifest

        rows = read_pages_manifest(self.pages)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity"], "Praxis Beispiel")
        self.assertEqual(rows[0]["page"], "https://www.11467.com/qiye/1.htm")

    def test_the_saved_pages_reach_the_finder(self):
        cfg = self.build(
            sitediscovery=["on"], sitediscovery_pages=[str(self.pages)]
        )
        finder = next(f for f in support.discoverer(cfg).finders() if f.name == "page_links")
        self.assertTrue(finder.is_configured())
        self.assertEqual(len(finder.pages()), 1)

    def test_turning_it_on_without_a_file_is_refused_with_the_field_named(self):
        with self.assertRaises(CollectorError) as ctx:
            self.build(sitediscovery=["on"])
        message = str(ctx.exception)
        self.assertIn("已知官网清单 CSV", message)
        self.assertIn("已保存页面清单 CSV", message)
        # The refusal has to say what it is *not*: a search.
        self.assertIn("不会去全网搜索", message)

    def test_a_path_that_does_not_exist_is_refused_not_silently_empty(self):
        with self.assertRaises(CollectorError) as ctx:
            self.build(sitediscovery=["on"], sitediscovery_sites=[str(self.tmp / "nope.csv")])
        self.assertIn("找不到这个文件", str(ctx.exception))

    def test_a_non_integer_budget_is_refused(self):
        with self.assertRaises(CollectorError) as ctx:
            self.build(
                sitediscovery=["on"],
                sitediscovery_sites=[str(self.sites)],
                sitediscovery_budget=["many"],
            )
        self.assertIn("整数", str(ctx.exception))

    def test_zero_means_unlimited(self):
        cfg = self.build(
            sitediscovery=["on"], sitediscovery_sites=[str(self.sites)], sitediscovery_budget=["0"]
        )
        self.assertEqual(cfg.sitediscovery_request_budget, 0)

    def test_no_reuse_window_means_every_run_re_asks(self):
        # The default, and the behaviour of every configuration written before
        # 034: a blank field must not quietly start caching verdicts.
        cfg = self.build(sitediscovery=["on"], sitediscovery_sites=[str(self.sites)])
        self.assertEqual(cfg.sitediscovery_reuse_days, 0)
        explicit = self.build(
            sitediscovery=["on"], sitediscovery_sites=[str(self.sites)],
            sitediscovery_reuse_days=["0"],
        )
        self.assertEqual(explicit.sitediscovery_reuse_days, 0)

    def test_a_reuse_window_from_the_screen_reaches_the_config(self):
        cfg = self.build(
            sitediscovery=["on"], sitediscovery_sites=[str(self.sites)],
            sitediscovery_reuse_days=["7"],
        )
        self.assertEqual(cfg.sitediscovery_reuse_days, 7)

    def test_a_bad_reuse_window_is_refused_with_the_field_named(self):
        for bad in ("many", "-1"):
            with self.assertRaises(CollectorError) as ctx:
                self.build(
                    sitediscovery=["on"], sitediscovery_sites=[str(self.sites)],
                    sitediscovery_reuse_days=[bad],
                )
            self.assertIn("已确认判定可复用天数", str(ctx.exception))

    def test_the_reuse_field_is_on_the_screen(self):
        # Same rule as the other discovery fields: an input the operator cannot
        # see is a YAML-only setting and does not count as a screen entry.
        self.assertIn('name="sitediscovery_reuse_days"', local_ui._discovery_block())
        self.assertIn("复用", local_ui._discovery_block())

    def test_the_account_free_lookup_is_off_unless_ticked(self):
        # Free and account-free, but it reaches a public service and covers
        # almost no small business, so it is opt-in and off by default.
        cfg = self.build(sitediscovery=["on"], sitediscovery_sites=[str(self.sites)])
        self.assertFalse(cfg.sitediscovery["wikidata"]["enabled"])
        self.assertIn("wikidata", cfg.sitediscovery_sources)

        on = self.build(
            sitediscovery=["on"], sitediscovery_sites=[str(self.sites)],
            sitediscovery_wikidata=["on"],
        )
        self.assertTrue(on.sitediscovery["wikidata"]["enabled"])

    def test_the_lookup_switch_is_on_the_screen_and_says_what_it_is_not(self):
        block = local_ui._discovery_block()
        self.assertIn('name="sitediscovery_wikidata"', block)
        # It must not read as a whole-web search: that still needs a paid key.
        self.assertIn("不是", block)
        self.assertIn("Wikidata", block)


class RegionAndLanguageFormTest(unittest.TestCase):
    """Stage 019: a China run must not quietly use the overseas defaults."""

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.base = self.tmp / "project"

    def build(self, **overrides):
        # A China form defaults to the domestic source, and that source is
        # configured per site: the list URL is part of the form from 021 on.
        overrides.setdefault("directory_url", ["https://www.example.cn/zhejiang-hangzhou-list/"])
        return local_ui.build_config(form(**overrides), base_dir=self.base, name="region")

    def test_china_uses_the_domestic_source_and_chinese_defaults(self):
        cfg = self.build(industry=["宠物服务"], city=["杭州"], country=["中国"])
        # The default is the directory adapter, never Google/OSM.
        self.assertEqual(cfg.sources_list, ["directory"])
        self.assertEqual(cfg.languages, ["zh"])
        self.assertEqual(cfg.country, "CN")
        # The query is written the way a Chinese search is written.
        self.assertEqual(cfg.query, "杭州宠物服务")

    def test_the_overseas_defaults_are_unchanged(self):
        cfg = self.build()
        self.assertEqual(cfg.sources_list, ["google_maps", "openstreetmap"])
        self.assertEqual(cfg.languages, ["en", "bilingual"])
        self.assertEqual(cfg.query, "dentists in Berlin")

    def test_the_screen_choice_beats_the_region_default(self):
        cfg = self.build(country=["中国"], city=["杭州"],
                         sources=["directory", "google_maps"], languages=["zh", "en"])
        self.assertEqual(cfg.sources_list, ["directory", "google_maps"])
        self.assertEqual(cfg.languages, ["zh", "en"])

    def test_an_unimplemented_source_is_dropped_not_fatal(self):
        # A stale bookmark naming a source this build does not have must not
        # break the run, and must not smuggle anything in either.
        cfg = self.build(sources=["telepathy", "google_maps"])
        self.assertEqual(cfg.sources_list, ["google_maps"])

    def test_an_unknown_region_is_refused_with_the_typed_text(self):
        with self.assertRaises(CollectorError) as ctx:
            self.build(country=["Narnia"])
        message = str(ctx.exception)
        self.assertIn("不认识的国家/地区", message)
        self.assertIn("Narnia", message)

    def test_the_region_fields_reach_the_config(self):
        cfg = self.build(country=["中国"], city=["杭州"], province=["浙江省"],
                         district=["西湖区"])
        self.assertEqual(cfg.province, "浙江省")
        self.assertEqual(cfg.district, "西湖区")

    def test_a_china_run_warns_when_only_foreign_sources_are_chosen(self):
        # Choosing Google for a China run is allowed, but it must be visible.
        cfg = self.build(country=["中国"], city=["杭州"], sources=["google_maps"])
        warnings = cfg.region_warnings()
        self.assertTrue(warnings)
        self.assertIn("不是国内方案", " ".join(warnings))

    def test_a_healthy_china_run_has_no_warning(self):
        self.assertEqual(self.build(country=["中国"], city=["杭州"]).region_warnings(), [])


class LocationResolutionTest(unittest.TestCase):
    """An unknown city must be asked about, never silently replaced."""

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.base = self.tmp / "project"

    def resolve(self, **overrides):
        return local_ui.resolve_location(form(**overrides))

    def test_a_known_city_resolves_from_the_table(self):
        self.assertEqual(self.resolve(city=["Berlin"], country=["DE"]), (52.52, 13.405))
        self.assertEqual(self.resolve(city=["上海"], country=["CN"]), (31.2304, 121.4737))

    def test_an_explicit_coordinate_wins(self):
        # The screen asked for coordinates; they must not be overridden by
        # whatever the city field says.
        self.assertEqual(self.resolve(geo=["31.2,121.5"], city=["Nowhere"], country=["XX"]),
                         (31.2, 121.5))

    def test_an_unknown_city_is_an_error_not_a_default(self):
        with self.assertRaises(CollectorError) as ctx:
            self.resolve(city=["Atlantis"], country=["XX"])
        self.assertIn("不会默默改用", str(ctx.exception))

    def test_a_malformed_coordinate_is_an_error(self):
        for bad in ("52.52", "north,east", "999,999"):
            with self.subTest(value=bad), self.assertRaises(CollectorError):
                self.resolve(geo=[bad])

    def test_the_config_carries_the_resolved_centre(self):
        cfg = local_ui.build_config(
            form(city=["Hamburg"], country=["DE"]), base_dir=self.base, name="hamburg"
        )
        self.assertEqual(cfg.data["location"]["geo"], [53.5511, 9.9937])
        self.assertEqual(cfg.query, "dentists in Hamburg")


class RunManagerTest(unittest.TestCase):
    def test_start_refuses_a_second_concurrent_run(self):
        manager = local_ui.RunManager()
        import subprocess

        started = []

        class FakePopen:
            pid = 4242

            def __init__(self, *args, **kwargs):
                started.append(args)
            def poll(self):
                return None

        original = subprocess.Popen
        subprocess.Popen = FakePopen  # type: ignore[assignment]
        try:
            manager.start(pathlib.Path("cfg.yaml"), pathlib.Path("base"))
            with self.assertRaises(RuntimeError):
                manager.start(pathlib.Path("cfg2.yaml"), pathlib.Path("base"))
            self.assertTrue(manager.is_running())
        finally:
            subprocess.Popen = original  # type: ignore[assignment]

    def test_stop_takes_the_process_tree_down(self):
        manager = local_ui.RunManager()
        calls = []

        class FakePopen:
            pid = 4242

            def poll(self):
                return None
            def wait(self, timeout=None):
                calls.append(("wait", timeout))
                return 0

        manager._current = FakePopen()
        manager._config = "cfg.yaml"
        original_run = local_ui.subprocess.run
        local_ui.subprocess.run = lambda argv, **kw: calls.append(("taskkill", argv))  # type: ignore[assignment]
        try:
            self.assertTrue(manager.stop())
        finally:
            local_ui.subprocess.run = original_run  # type: ignore[assignment]
        self.assertFalse(manager.is_running())
        self.assertEqual(calls[0][0], "taskkill")
        self.assertIn("/T", calls[0][1])

    def test_stop_without_a_run_is_a_no_op(self):
        self.assertFalse(local_ui.RunManager().stop())


class PageSmokeTest(unittest.TestCase):
    """A real server on a real port: the pages render, the guard rails hold."""

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls._handle = tempfile.TemporaryDirectory()
        cls.base = pathlib.Path(cls._handle.name) / "project"
        (cls.base / "runs").mkdir(parents=True)
        cls.manager = local_ui.RunManager()
        handler = local_ui.make_handler(cls.base, cls.manager)
        from http.server import ThreadingHTTPServer

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._handle.cleanup()

    def get(self, path: str):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def post(self, path: str, fields: dict | None = None):
        import http.client

        # A tick group sends one entry per tick, so a list value must become
        # repeated keys rather than a Python repr of the list.
        pairs = [
            (key, item)
            for key, value in (fields or {}).items()
            for item in (value if isinstance(value, list) else [value])
        ]
        body = urllib.parse.urlencode(pairs)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(
                "POST", path, body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            response.read()
            return response.status, response.getheader("Location") or ""
        finally:
            connection.close()

    def test_home_page_renders_the_screen(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("线索采集控制台", body)
        self.assertIn("开始采集", body)

    def test_an_unknown_city_is_not_started(self):
        # POSTing a city with no centre must not start a run and must show
        # the reason — the old bug silently reused Berlin.
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            body = urllib.parse.urlencode(
                {"industry": "dentists", "city": "Atlantis", "country": "XX"}
            )
            connection.request(
                "POST", "/runs", body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 303)
            location = response.getheader("Location") or ""
            self.assertIn("error=", location)
        finally:
            connection.close()

        status, body = self.get("/" + location if location.startswith("/") else "/")
        self.assertEqual(status, 200)
        self.assertIn("无法开始", body)
        self.assertIn("不会默默改用", body)
        self.assertFalse(self.manager.is_running())

    def test_home_page_offers_a_coordinate_field(self):
        _status, body = self.get("/")
        self.assertIn('name="geo"', body)

    def test_home_page_offers_the_region_and_language_fields(self):
        # Stage 019: a person must be able to pick the region, the sources and
        # the export languages from the screen.
        _status, body = self.get("/")
        for field in ('name="country"', 'name="province"', 'name="district"'):
            self.assertIn(field, body)
        # The tick groups submit one entry per tick, so the name appears on
        # every checkbox of the group.
        self.assertIn('name="sources"', body)
        self.assertIn('name="languages"', body)
        for value in ('value="directory"', 'value="zh"', 'value="bilingual"'):
            self.assertIn(value, body)
        self.assertIn("中国", body)

    def test_the_checkbox_rows_do_not_break_the_mobile_width(self):
        # The task book's mobile check: the new controls must not be squeezed
        # into something unusable. A checkbox that inherits ``width: 100%``
        # from the text inputs would push its label off the row, so the tick
        # style must reset the width and the row grid must be free to wrap.
        _status, body = self.get("/")
        self.assertIn(".tick input { width: auto", body)
        self.assertIn("repeat(auto-fit, minmax(180px, 1fr))", body)

    def test_a_china_run_says_google_is_not_the_domestic_option(self):
        # The warning is derived from the *stored* config, so it survives a
        # reload and is what the run actually did.
        from collector import db

        cfg = support.config({
            "location": {"query": "宠物服务 in 杭州", "country": "中国",
                         "city": "杭州", "geo": [30.2741, 120.1551]},
            "sources": ["google_maps"],
        })
        stored = {k: v for k, v in cfg.data.items() if k != "source_path"}

        conn = db.connect(self.base / "runs" / "collector.sqlite")
        try:
            run_id = db.create_run(
                conn, fingerprint="h" * 12, config_json=json.dumps(stored),
                config_source="<test>", collector_version="test", schema_version=1,
                go_binary="", go_version=None, target_count=5,
            )
        finally:
            conn.close()

        status, body = self.get(f"/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertIn("地区与来源提示", body)
        self.assertIn("不是国内方案", body)

    def test_an_unreadable_stored_config_does_not_crash_the_page(self):
        # Older rows carry an empty config blob; the region note must be
        # skipped rather than taking the download page down with it.
        from collector import db

        conn = db.connect(self.base / "runs" / "collector.sqlite")
        try:
            run_id = db.create_run(
                conn, fingerprint="i" * 12, config_json="{}",
                config_source="<test>", collector_version="test", schema_version=1,
                go_binary="", go_version=None, target_count=5,
            )
        finally:
            conn.close()

        status, body = self.get(f"/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertNotIn("地区与来源提示", body)

    def test_an_unknown_run_is_a_404_not_a_crash(self):
        status, _body = self.get("/runs/999")
        self.assertEqual(status, 404)

    def test_an_existing_run_detail_page_renders(self):
        # The detail page reads the run's stage rows, and ``stages`` has no
        # ``note`` column — so reading one crashed the page for *every*
        # existing run, including the page that carries the download links.
        # A stage row whose ``error`` is NULL is exactly the crashing case.
        from collector import db

        conn = db.connect(self.base / "runs" / "collector.sqlite")
        try:
            run_id = db.create_run(
                conn,
                fingerprint="f" * 12,
                config_json="{}",
                config_source="<test>",
                collector_version="test",
                schema_version=1,
                go_binary="",
                go_version=None,
                target_count=5,
            )
            db.set_stage(conn, run_id, "discover", "completed")
            db.set_stage(conn, run_id, "ingest", "completed", error="boom")
        finally:
            conn.close()

        # The page also lists the run's export files, each with a download
        # link. Put one there so that path is exercised too.
        export_dir = self.base / "runs" / ("f" * 12) / f"run-{run_id}" / "export"
        export_dir.mkdir(parents=True, exist_ok=True)
        (export_dir / "entities.csv").write_text("a,b\n1,2\n", encoding="utf-8")

        status, body = self.get(f"/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertIn("discover", body)
        self.assertIn("boom", body)
        self.assertIn(f"/runs/{run_id}/download?file=entities.csv", body)

    def test_a_path_traversal_filename_is_refused(self):
        status, body = self.get("/runs/1/download?file=..\\..\\collector.sqlite")
        self.assertEqual(status, 400)

    def test_a_bind_address_warning_is_printed_for_non_loopback(self):
        # The warning is the documented behaviour of --host; the default must
        # never be the dangerous one.
        import inspect

        source = inspect.getsource(local_ui.serve)
        self.assertIn('host != "127.0.0.1"', source)
        self.assertIn("WARNING", source)

    def test_the_status_api_lists_runs_as_json(self):
        status, body = self.get("/api/runs")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("runs", payload)
        self.assertIn("running", payload)

    def test_the_home_page_polls_status_but_never_posts(self):
        # The auto-refresh must be able to update a badge and nothing else: no
        # page reload (which would wipe a half-filled form) and no request that
        # could start, stop or resubmit a run.
        _status, body = self.get("/")
        self.assertIn("/api/runs", body)
        self.assertIn("setInterval", body)
        self.assertNotIn("location.reload", body)
        self.assertNotIn("method: 'POST'", body)
        self.assertNotIn('method: "POST"', body)

    def test_the_detail_page_offers_a_reexport_button(self):
        from collector import db

        conn = db.connect(self.base / "runs" / "collector.sqlite")
        try:
            run_id = db.create_run(
                conn,
                fingerprint="g" * 12,
                config_json="{}",
                config_source="<test>",
                collector_version="test",
                schema_version=1,
                go_binary="",
                go_version=None,
                target_count=5,
            )
        finally:
            conn.close()

        status, body = self.get(f"/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertIn(f"/runs/{run_id}/reexport", body)
        self.assertIn("重新导出", body)

    def test_reexport_rewrites_the_files_without_collecting(self):
        # A finished run can have its downloads regenerated — for example to add
        # the bilingual workbook — with no scraper involved. The raw file must
        # be untouched and the bilingual workbook must appear.
        from collector import db, ingest

        cfg = local_ui.build_config(form(), base_dir=self.base, name="reexport")
        conn = db.connect(self.base / "runs" / "collector.sqlite")
        try:
            run_id = db.create_run(
                conn,
                fingerprint=cfg.fingerprint(),
                config_json=cfg.canonical_json(),
                config_source="<test>",
                collector_version="test",
                schema_version=1,
                go_binary="",
                go_version=None,
                target_count=cfg.target_count,
            )
            raw_dir = self.base / "runs" / cfg.short_fingerprint() / f"run-{run_id}" / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / "discover-a1.jsonl"
            raw_path.write_text(
                json.dumps(support.envelope(support.raw_record()), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            ingest.ingest_directory(conn, raw_dir, run_id=run_id, cfg=cfg)
        finally:
            conn.close()

        before = raw_path.read_text(encoding="utf-8")
        status, location = self.post(f"/runs/{run_id}/reexport")
        self.assertEqual(status, 303)
        self.assertEqual(location, f"/runs/{run_id}")

        export_dir = self.base / "runs" / cfg.short_fingerprint() / f"run-{run_id}" / "export"
        self.assertTrue((export_dir / "entities.xlsx").is_file())
        self.assertTrue((export_dir / "entities_bilingual.xlsx").is_file())
        self.assertEqual(raw_path.read_text(encoding="utf-8"), before)

    def test_reexport_can_pick_a_language_without_recollecting(self):
        # The task book's "re-export a chosen language only": the same stored
        # data, no network, raw facts untouched — just the requested workbook.
        from collector import db, ingest

        cfg = local_ui.build_config(form(), base_dir=self.base, name="relang")
        conn = db.connect(self.base / "runs" / "collector.sqlite")
        try:
            run_id = db.create_run(
                conn, fingerprint=cfg.fingerprint(), config_json=cfg.canonical_json(),
                config_source="<test>", collector_version="test", schema_version=1,
                go_binary="", go_version=None, target_count=cfg.target_count,
            )
            raw_dir = self.base / "runs" / cfg.short_fingerprint() / f"run-{run_id}" / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / "discover-a1.jsonl"
            raw_path.write_text(
                json.dumps(support.envelope(support.raw_record()), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            ingest.ingest_directory(conn, raw_dir, run_id=run_id, cfg=cfg)
        finally:
            conn.close()

        before = raw_path.read_text(encoding="utf-8")
        status, _location = self.post(f"/runs/{run_id}/reexport", {"languages": ["zh"]})
        self.assertEqual(status, 303)

        export_dir = self.base / "runs" / cfg.short_fingerprint() / f"run-{run_id}" / "export"
        self.assertTrue((export_dir / "entities_zh.xlsx").is_file())
        # No collection happened and the raw evidence is byte-identical.
        self.assertEqual(raw_path.read_text(encoding="utf-8"), before)

    def test_reexport_of_an_unknown_run_is_a_404(self):
        status, _body = self.get("/runs/424242/reexport")
        self.assertEqual(status, 404)
        status, _location = self.post("/runs/424242/reexport")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()


class DomesticDirectoryFormTest(unittest.TestCase):
    """021 D.7: the domestic source must be usable from the screen alone."""

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.base = self.tmp / "project"

    def build(self, **overrides):
        overrides.setdefault("country", ["中国"])
        overrides.setdefault("city", ["杭州"])
        overrides.setdefault("province", ["浙江省"])
        overrides.setdefault("industry", ["宠物服务"])
        return local_ui.build_config(form(**overrides), base_dir=self.base, name="domestic")

    def test_the_form_carries_the_directory_entry(self):
        cfg = self.build(
            sources=["directory"],
            directory_url=["https://www.b2b168.com/zhejiang-hangzhou-chongwu/"],
        )
        entries = cfg.data["sources_map"]["directory"]["entries"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["start_url"], "https://www.b2b168.com/zhejiang-hangzhou-chongwu/")
        # The verified preset's selectors, written out where they can be read.
        self.assertEqual(entry["field_selectors"]["description"], "ul.Cgsjj")
        self.assertEqual(entry["field_selectors"]["address"], "dl.codl dd:nth-of-type(1)")
        self.assertIn("detail_pattern", entry)
        # Politeness limits travel with the entry, so a first look is bounded.
        self.assertEqual(entry["max_listing_pages"], 1)
        self.assertGreaterEqual(entry["delay_seconds"], 1.0)

    def test_the_shunqiwang_preset_carries_its_own_selectors(self):
        # 024: the second domestic source (顺企网) must be selectable and must
        # ship its own detail-page selectors, not fall back to b2b168's.
        cfg = self.build(
            sources=["directory"],
            directory_url=["https://www.11467.com/hangzhou/search/9092.htm"],
            directory_preset=["shunqiwang"],
        )
        entry = cfg.data["sources_map"]["directory"]["entries"][0]
        self.assertEqual(entry["detail_pattern"], r"/qiye/\d+\.htm")
        self.assertEqual(
            entry["field_selectors"]["address"], "#contact dl.codl dd:nth-of-type(1)"
        )
        self.assertEqual(entry["field_selectors"]["description"], "#aboutuscontent")
        # 顺企网 rate-limits harder than b2b168, so this preset asks for a
        # slower pace than the shared 1.5s default.
        self.assertGreaterEqual(entry["delay_seconds"], 3.0)

    def test_a_directory_run_without_a_list_url_is_refused_by_name(self):
        with self.assertRaises(CollectorError) as ctx:
            self.build(sources=["directory"])
        message = str(ctx.exception)
        self.assertIn("企业目录", message)
        self.assertIn("目录列表页地址", message)

    def test_a_url_without_a_scheme_is_refused(self):
        with self.assertRaises(CollectorError) as ctx:
            self.build(sources=["directory"], directory_url=["www.b2b168.com/list/"])
        self.assertIn("http://", str(ctx.exception))

    def test_an_overseas_form_writes_no_directory_settings(self):
        cfg = self.build(country=["DE"], city=["Berlin"], sources=["google_maps"])
        self.assertEqual(cfg.data.get("sources_map"), {})
        # The overseas flow is untouched: no description field is requested.
        self.assertNotIn("description", cfg.collect_fields)

    def test_the_matching_question_can_be_asked_on_the_screen(self):
        cfg = self.build(
            sources=["directory"],
            directory_url=["https://www.example.cn/list/"],
            match_profile=["pet_related"],
        )
        self.assertEqual(cfg.match_data["industry"]["profile"], "pet_related")
        self.assertEqual(cfg.match_data["region"]["province"], "浙江省")
        self.assertEqual(cfg.match_data["region"]["city"], "杭州")
        # Matching must not change what is collected.
        self.assertEqual(cfg.sources_list, ["directory"])
        self.assertIn("description", cfg.collect_fields)

    def test_leaving_matching_off_writes_no_match_section(self):
        cfg = self.build(
            sources=["directory"], directory_url=["https://www.example.cn/list/"]
        )
        self.assertIsNone(cfg.match_data)

    def test_extra_keywords_reach_the_config(self):
        cfg = self.build(
            sources=["directory"],
            directory_url=["https://www.example.cn/list/"],
            match_profile=["pet_related"],
            match_keywords=["水族，鸟食"],
        )
        self.assertEqual(cfg.match_data["industry"]["keywords"], ["水族", "鸟食"])

    def test_the_written_yaml_is_inspectable_and_reloadable(self):
        cfg = self.build(
            sources=["directory"],
            directory_url=["https://www.b2b168.com/zhejiang-hangzhou-chongwu/"],
            match_profile=["pet_store"],
        )
        path = local_ui.write_config(cfg, self.tmp / "generated" / "domestic.yaml")
        text = path.read_text(encoding="utf-8")
        # A reader of the generated file sees the same knobs the screen showed.
        self.assertIn("sources_map", text)
        self.assertIn("ul.Cgsjj", text)
        self.assertIn("match:", text)
        self.assertIn("pet_store", text)
        from collector.config import load

        loaded = load(path)
        self.assertEqual(loaded.match_data["industry"]["profile"], "pet_store")
        self.assertEqual(
            loaded.data["sources_map"]["directory"]["entries"][0]["start_url"],
            "https://www.b2b168.com/zhejiang-hangzhou-chongwu/",
        )
        self.assertEqual(loaded.fingerprint(), cfg.fingerprint())

    def test_the_screen_render_carries_the_domestic_fields(self):
        # The rendered page must contain the new inputs, or the entry is
        # YAML-only and does not count as a screen entry (021 D.7).
        page = local_ui.PAGE.format(
            version="test",
            industry="", city="", country="DE", query="", province="", district="",
            target_count="30", formats="csv,jsonl,xlsx", geo="",
            sources_boxes="", languages_boxes="",
            fast_selected="", browser_selected="", enrich_off=" selected", enrich_on="",
            domestic_block=local_ui._domestic_block(),
            discovery_block=local_ui._discovery_block(),
            offline_block=local_ui._offline_block(),
            csv_block=local_ui._csv_block(),
            status_block=local_ui._source_status_block([]),
            start_disabled="", running_note="", region_note="", table="", error_banner="", preflight_banner="", confirm_field="", empty_note="",
        )
        self.assertIn('name="directory_url"', page)
        self.assertIn('name="directory_preset"', page)
        self.assertIn('name="match_profile"', page)
        self.assertIn('name="match_keywords"', page)
        self.assertIn("八方资源网", page)

    def test_the_screen_render_carries_the_discovery_fields(self):
        # 026 P1: same rule as the domestic block -- if the inputs are not on
        # the rendered page, the feature is YAML-only and does not count.
        page = local_ui.PAGE.format(
            version="test",
            industry="", city="", country="DE", query="", province="", district="",
            target_count="30", formats="csv,jsonl,xlsx", geo="",
            sources_boxes="", languages_boxes="",
            fast_selected="", browser_selected="", enrich_off=" selected", enrich_on="",
            domestic_block="",
            discovery_block=local_ui._discovery_block(),
            offline_block="",
            csv_block="",
            status_block="",
            start_disabled="", running_note="", region_note="", table="", error_banner="", preflight_banner="", confirm_field="", empty_note="",
        )
        self.assertIn('name="sitediscovery"', page)
        self.assertIn('name="sitediscovery_sites"', page)
        self.assertIn('name="sitediscovery_pages"', page)
        self.assertIn('name="sitediscovery_budget"', page)
        # The block must say what it does *not* do, so nobody reads the switch as
        # a web search.
        self.assertIn("本地没有搜索服务", page)

    def test_the_screen_render_carries_the_offline_import_fields(self):
        # 026 P2.1: same rule again -- an input that is not on the rendered page
        # is a YAML-only feature and does not count as a screen entry.
        page = local_ui.PAGE.format(
            version="test",
            industry="", city="", country="DE", query="", province="", district="",
            target_count="30", formats="csv,jsonl,xlsx", geo="",
            sources_boxes="", languages_boxes="",
            fast_selected="", browser_selected="", enrich_off=" selected", enrich_on="",
            domestic_block="",
            discovery_block="",
            offline_block=local_ui._offline_block(),
            csv_block=local_ui._csv_block(),
            status_block=local_ui._source_status_block(
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
            ),
            start_disabled="", running_note="", region_note="", table="", error_banner="", preflight_banner="", confirm_field="", empty_note="",
        )
        self.assertIn('name="local_pages"', page)
        self.assertIn('name="local_listing_path"', page)
        self.assertIn('name="local_origin_url"', page)
        self.assertIn('name="local_details"', page)
        self.assertIn('name="local_preset"', page)
        # 034 第一批第 2 条 reached the screen: without this input the row-field
        # fix is a code change nobody can turn off or inspect.
        self.assertIn('name="local_listing_fields"', page)
        # Both blocks must be honest about what they cannot do.
        self.assertIn("这里不发任何网络请求", page)
        # And the hint must no longer claim the pre-034 behaviour: a row without
        # a saved detail page keeps the fields the listing row itself stated.
        self.assertNotIn("只会写出名字", page)
        self.assertIn("只增补、不覆盖", page)
        # The per-source panel is on the same screen, with the network mode and
        # the last success spelled out rather than left blank.
        self.assertIn("来源状态", page)
        self.assertIn("最近成功", page)
        self.assertIn("2026-10-13 02:00:00", page)
        self.assertIn("TUN", page)


class OfflineImportConfigTest(unittest.TestCase):
    """034 第三批 3.1: the screen's offline fields reach the generated config.

    The row-reading fix lives in the provider, but a screen that omits the key
    would ship the old behaviour no matter what the provider can do. These are
    the keys the saved-page import depends on, checked at the point where a
    person's choices become YAML.
    """

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.listing = self.tmp / "listing.html"
        self.listing.write_text(
            "<html><body><ul class='companylist'><li><h4><a href='/qiye/1.htm'>A</a></h4>"
            "<div>地址： 某地</div></li></ul></body></html>",
            encoding="utf-8",
        )

    def build(self, **overrides):
        form_values = {
            "industry": ["宠物服务"], "city": ["杭州"], "country": ["中国"],
            "province": ["浙江省"], "geo": ["30.2741,120.1551"], "query": [""],
            "target_count": ["10"], "mode": ["fast"], "formats": ["csv"],
            "enrich": ["off"], "sources": ["local_pages"], "local_pages": ["on"],
            "local_listing_path": [str(self.listing)],
            "local_origin_url": ["https://www.11467.com/hangzhou/search/9092.htm"],
            "local_preset": ["shunqiwang"],
        }
        form_values.update(overrides)
        cfg = local_ui.build_config(form_values, base_dir=self.tmp, name="ui-offline")
        entries = (cfg.data.get("sources_map") or {}).get("local_pages", {}).get("entries") or []
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_the_preset_travels_with_the_entry(self):
        entry = self.build()
        # Facts about the site, not choices a person is equipped to make: they
        # land in the YAML verbatim so what ran is what a reader can inspect.
        self.assertEqual(entry["list_preset"], "shunqiwang")
        self.assertEqual(entry["list_selector"], "ul.companylist")
        self.assertEqual(entry["detail_host_scope"], "registrable_domain")
        self.assertTrue(entry["detail_url_pattern"])

    def test_keeping_the_row_fields_is_the_default(self):
        # Default on: a blank form must not silently reproduce the pre-034 loss.
        self.assertTrue(self.build()["read_listing_fields"])

    def test_the_switch_can_be_turned_off_and_says_so_in_the_yaml(self):
        # Off is how a reader reproduces the old behaviour on purpose, so the
        # value has to be written explicitly -- "missing" would be read as True.
        self.assertFalse(self.build(local_listing_fields=["off"])["read_listing_fields"])

    def test_a_stated_time_is_the_operators_own_words(self):
        entry = self.build(local_observed_at=["2026-10-13"])
        self.assertEqual(entry["observed_at"], "2026-10-13")

    def test_a_missing_listing_file_is_refused_with_the_field_named(self):
        with self.assertRaises(CollectorError) as ctx:
            self.build(local_listing_path=[str(self.tmp / "nope.html")])
        self.assertIn("已存列表页文件", str(ctx.exception))


class CsvListEntryTest(unittest.TestCase):
    """050 P1: the CSV list is configurable from the screen, not by hand.

    Four things have to stay apart, and each has a test that pins it: the input
    file, the original source (name and site), the time the source stated, and
    the entry channel. A test that only checked the path would let the other
    three collapse into one wrong sentence again.
    """

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.csv = self.tmp / "list.csv"
        self.csv.write_text(
            "company_id,Firma,Anschrift,Telefon,Webseite\n"
            "C1,geomer GmbH,Im Eiselbrunnen 6,+49 6221 89458-0,https://www.geomer.de/\n",
            encoding="utf-8",
        )

    def node(self, **overrides):
        values = {
            "industry": [""], "city": ["Berlin"], "country": ["DE"], "query": [""],
            "target_count": ["10"], "mode": ["fast"], "formats": ["csv,jsonl,xlsx"],
            "enrich": ["off"], "sources": ["csv_list"], "csv_list": ["on"],
            "csv_path": [str(self.csv)],
            "csv_columns": ["Firma=title,Anschrift=address,Telefon=phone,Webseite=website"],
        }
        values.update(overrides)
        cfg = local_ui.build_config(values, base_dir=self.tmp, name="csv-entry")
        self.assertIn("csv_list", cfg.data["sources"])
        return (cfg.data.get("sources_map") or {}).get("csv_list") or {}

    def test_the_file_and_the_column_mapping_reach_the_config(self):
        node = self.node(csv_id_column=["company_id"])
        self.assertEqual(node["path"], str(self.csv))
        self.assertEqual(node["id_column"], "company_id")
        self.assertEqual(node["columns"]["Firma"], "title")
        self.assertEqual(node["columns"]["Webseite"], "website")

    def test_a_mapping_target_that_is_not_a_field_is_refused(self):
        with self.assertRaises(CollectorError) as ctx:
            self.node(csv_columns=["Firma=quartier"])
        self.assertIn("quartier", str(ctx.exception))

    def test_a_mapping_that_is_not_a_pair_is_refused(self):
        with self.assertRaises(CollectorError) as ctx:
            self.node(csv_columns=["Firma"])
        self.assertIn("字段映射", str(ctx.exception))

    def test_the_original_source_keeps_name_and_site(self):
        node = self.node(
            csv_origin_name=[" Beispielverzeichnis "],
            csv_origin_url=["https://www.example.com/list"],
        )
        self.assertEqual(node["provenance"]["origin"], "Beispielverzeichnis（example.com）")

    def test_only_the_url_known_writes_only_the_URL(self):
        # The URL as given, not a domain guessed out of it: a public-suffix
        # heuristic would be a claim about the site that the URL does not make.
        node = self.node(csv_origin_url=["https://sub.example.co.uk/list"], csv_origin_name=[""])
        self.assertEqual(node["provenance"]["origin"], "https://sub.example.co.uk/list")

    def test_an_incomplete_origin_url_is_refused(self):
        with self.assertRaises(CollectorError) as ctx:
            self.node(csv_origin_url=["www.example.com"])
        self.assertIn("http", str(ctx.exception))

    def test_no_source_stated_writes_no_origin_at_all(self):
        # Nothing is invented: no host scraped out of the file name, no
        # "unknown source" placeholder pretending to be a name.
        self.assertNotIn("provenance", self.node())

    def test_an_unknown_source_time_writes_no_time(self):
        node = self.node(csv_source_time=[""], csv_source_time_unknown=["on"])
        self.assertNotIn("observed_at", node.get("provenance", {}))

    def test_a_stated_source_time_is_written(self):
        node = self.node(csv_source_time=["2026-09-20"], csv_source_time_unknown=["off"])
        self.assertEqual(node["provenance"]["observed_at"], "2026-09-20")

    def test_the_unknown_switch_wins_over_a_stale_time_field(self):
        # The two boxes contradict each other only if a stale value survives a
        # browser's back button; "the source did not say" is the honest answer
        # then, and it must not be quietly overridden by the leftover text.
        node = self.node(csv_source_time=["2026-09-20"], csv_source_time_unknown=["on"])
        self.assertNotIn("observed_at", node.get("provenance", {}))

    def test_the_entry_channel_is_an_import(self):
        # ``acquired_via`` is not on the screen at all: a CSV is a file, so it
        # cannot be ``live``. The provider defaults it, and the config the screen
        # writes must not be able to say otherwise.
        node = self.node()
        self.assertNotIn("acquired_via", node.get("provenance", {}))

    def test_a_missing_file_is_refused_with_the_field_named(self):
        with self.assertRaises(CollectorError) as ctx:
            self.node(csv_path=[str(self.tmp / "nope.csv")])
        self.assertIn("CSV 文件路径", str(ctx.exception))

    def test_the_tab_delimiter_is_configurable(self):
        self.assertEqual(self.node(csv_delimiter=["tab"])["delimiter"], "tab")

    def test_the_screen_carries_the_block(self):
        page = local_ui._csv_block()
        self.assertIn('name="csv_path"', page)
        self.assertIn('name="csv_columns"', page)
        self.assertIn('name="csv_origin_name"', page)
        self.assertIn('name="csv_source_time_unknown"', page)
        self.assertIn("来源未给出时间", page)


class RunStatusLanguageTest(unittest.TestCase):
    """026 P3.2: the screen says 待开始/运行中/部分完成/已完成/失败 in Chinese."""

    def test_every_stored_status_has_a_chinese_label_and_a_next_step(self):
        for raw in ("pending", "running", "interrupted", "partial", "completed", "failed"):
            with self.subTest(status=raw):
                label, hint = local_ui._run_status_text(raw)
                self.assertNotEqual(label, raw)
                self.assertRegex(label, r"[\u4e00-\u9fff]")
                self.assertTrue(hint)

    def test_an_unknown_status_is_shown_as_itself(self):
        # Never invent a friendly word for a state the build does not know.
        label, _hint = local_ui._run_status_text("quarantined")
        self.assertEqual(label, "quarantined")

    def test_the_row_keeps_the_raw_value_for_colouring_and_the_label_for_reading(self):
        row = {"id": 7, "status": "partial", "started_at": "2026-10-14T01:00:00+08:00",
               "target_count": 30}
        cells = local_ui._row_cells(row)
        self.assertIn("status partial", cells)
        self.assertIn("部分完成", cells)

    def test_the_status_feed_carries_both_forms(self):
        # The poll repaints the badge in place, so the label has to be in the
        # payload: a second lookup in the browser would drift from the server.
        self.assertIn("status_text", _api_runs_source())

    def test_the_source_card_keeps_cooldown_separate_from_run_status(self):
        # 来源冷却 is a property of a site, not an outcome of a run; mixing it
        # into the run-status vocabulary would let a cooldown read as a result.
        for raw in local_ui.RUN_STATUS_LABELS:
            self.assertNotIn("冷却", local_ui.RUN_STATUS_LABELS[raw][0])

    def test_every_pipeline_stage_has_a_chinese_name_and_state(self):
        from collector.orchestrator import STAGES

        for stage in STAGES:
            with self.subTest(stage=stage):
                name, _ = local_ui._stage_text(stage, "completed")
                self.assertNotEqual(name, stage)
        for status in ("pending", "running", "completed", "partial", "failed", "skipped"):
            with self.subTest(status=status):
                _name, label = local_ui._stage_text("discover", status)
                self.assertNotEqual(label, status)


def _api_runs_source() -> str:
    """The whole ``local_ui`` module source, so the JSON contract can be pinned.

    ``_api_runs`` lives on a handler class built inside a factory function, so
    there is no attribute to introspect; reading the module source is the
    straightforward way to assert the payload shape the poll depends on.
    """
    return pathlib.Path(local_ui.__file__).read_text(encoding="utf-8")


class NarrowScreenTest(unittest.TestCase):
    """026 P3.3: the form and the download links work on a phone."""

    def _home(self) -> str:
        return local_ui.PAGE.format(
            version="test",
            industry="", city="", country="CN", query="", province="", district="",
            target_count="30", formats="csv,jsonl,xlsx", geo="",
            sources_boxes="", languages_boxes="",
            fast_selected="", browser_selected="", enrich_off=" selected", enrich_on="",
            domestic_block=local_ui._domestic_block(),
            discovery_block=local_ui._discovery_block(),
            offline_block=local_ui._offline_block(),
            csv_block=local_ui._csv_block(),
            status_block=local_ui._source_status_block([]),
            start_disabled="", running_note="", region_note="", table="", error_banner="", preflight_banner="", confirm_field="", empty_note="",
        )

    def test_the_viewport_is_declared(self):
        for page in (self._home(), local_ui.DETAIL_PAGE):
            self.assertIn("width=device-width", page)

    def test_a_narrow_screen_gets_a_single_column_form(self):
        page = self._home()
        self.assertIn("@media (max-width: 560px)", page)
        # The two-column grids collapse rather than squeezing each field.
        self.assertIn(".row { grid-template-columns: 1fr; }", page)

    def test_wide_tables_scroll_instead_of_being_clipped(self):
        self.assertIn("table-wrap", self._home())
        self.assertIn("table-wrap", local_ui.DETAIL_PAGE)
        self.assertIn("overflow-x: auto", local_ui.DETAIL_PAGE)

    def test_the_screen_says_the_task_survives_a_closed_page(self):
        # The claim has to be on the page, next to the button that starts it.
        self.assertIn("关掉这个页面（或合上手机）不会取消采集", self._home())

    def test_the_status_badge_poll_compares_the_machine_value(self):
        # Comparing a Chinese label against the API's raw status would differ on
        # every poll and reload the detail page in a loop.
        self.assertIn("data-run-status-raw", local_ui.DETAIL_PAGE)
        self.assertIn("getAttribute('data-run-status-raw')", local_ui.DETAIL_PAGE)


class OrphanedRunStatusTest(unittest.TestCase):
    """026 P3.5: a run whose process is gone must not be sold as running.

    Found by the bat end-to-end run. 「停止」 kills the whole process tree, and a
    killed process never gets to write ``interrupted``: its row keeps saying
    ``running`` forever. Closing the console window does the same thing. The
    screen is the only party that knows whether *it* still has a process for a
    configuration, so it is the screen's job to stop claiming a collection is
    under way when nothing is.
    """

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.own = self.tmp / "verification" / "ui" / "generated"
        self.own.mkdir(parents=True)

    def row(self, **overrides):
        base = {
            "id": 1,
            "status": "running",
            "config_source": str(self.own / "run.yaml"),
            "started_at": "2026-10-13T17:55:16Z",
            "target_count": 5,
        }
        base.update(overrides)
        return base

    def test_a_killed_run_is_not_reported_as_running(self):
        label, hint = local_ui.run_status_view(
            self.row(), live_config="", own_dir=self.own
        )
        self.assertIn("已中断", label)
        self.assertNotIn("运行中", label)
        # The next step has to be the one that actually works: resume keeps the
        # records already collected, a new run does not.
        self.assertIn("恢复/重跑", hint)
        self.assertIn("已采到的记录保留", hint)

    def test_a_live_process_keeps_the_run_running(self):
        path = str(self.own / "run.yaml")
        label, _hint = local_ui.run_status_view(
            self.row(config_source=path), live_config=path, own_dir=self.own
        )
        self.assertEqual(label, "运行中")

    def test_a_live_process_for_another_config_does_not_keep_this_one_running(self):
        # This screen runs one child at a time. If that child is working on a
        # different config, this row has no process.
        label, _hint = local_ui.run_status_view(
            self.row(config_source=str(self.own / "a.yaml")),
            live_config=str(self.own / "b.yaml"),
            own_dir=self.own,
        )
        self.assertIn("已中断", label)

    def test_a_run_started_outside_the_screen_is_left_alone(self):
        # A run somebody started from the terminal is not this screen's to
        # reinterpret: it cannot see that process, and saying 已中断 would be a
        # guess in the other direction.
        label, _hint = local_ui.run_status_view(
            self.row(config_source="C://elsewhere//cli.yaml"),
            live_config="",
            own_dir=self.own,
        )
        self.assertEqual(label, "运行中")

    def test_a_finished_run_is_never_rewritten(self):
        for status in ("completed", "partial", "failed", "interrupted", "pending"):
            label, _hint = local_ui.run_status_view(
                self.row(status=status), live_config="", own_dir=self.own
            )
            self.assertEqual(label, local_ui._run_status_text(status)[0], status)

    def test_the_stored_status_stays_readable_under_the_label(self):
        # The page shows the machine value in its own right; a run that says
        # `running` in the database must keep saying so.
        row = self.row()
        self.assertEqual(str(row["status"]), "running")
        cells = local_ui._row_cells(row, live_config="", own_dir=self.own)
        self.assertIn("data-run-status", cells)
        self.assertIn("已中断", cells)

    def test_a_relative_config_path_is_the_same_run_as_its_absolute_form(self):
        # The screen hands the CLI a relative config path (``..\\verification\\
        # ui\\generated\\x.yaml``) and the database stores it exactly like that,
        # so the two spellings must compare equal or every live run would look
        # abandoned.
        import os

        path = self.own / "run.yaml"
        relative = os.path.relpath(str(path), pathlib.Path.cwd())
        label, _hint = local_ui.run_status_view(
            self.row(config_source=relative), live_config=str(path), own_dir=self.own
        )
        self.assertEqual(label, "运行中")

    def test_an_empty_config_source_is_not_claimed_as_the_screens_own(self):
        label, _hint = local_ui.run_status_view(
            self.row(config_source=""), live_config="", own_dir=self.own
        )
        self.assertEqual(label, "运行中")


class OrphanedRunPageTest(unittest.TestCase):
    """The same fact, read off the rendered pages and the status feed."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from http.server import ThreadingHTTPServer

        handle = tempfile.TemporaryDirectory()
        cls.addClassCleanup(handle.cleanup)
        cls.base = pathlib.Path(handle.name) / "project"
        cls.generated = cls.base / "verification" / "ui" / "generated"
        (cls.base / "runs").mkdir(parents=True)
        cls.manager = local_ui.RunManager()
        handler = local_ui.make_handler(cls.base, cls.manager)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path: str):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=10
        ) as response:
            return response.status, response.read().decode("utf-8")

    def make_run(self) -> int:
        from collector import db

        # A readable stored config, so the summary card renders: a row with an
        # empty blob has no notes and no summary (that path has its own test).
        cfg = support.config(
            {
                "location": {
                    "query": "宠物服务 in 杭州", "country": "中国",
                    "city": "杭州", "geo": [30.2741, 120.1551],
                },
                "sources": ["google_maps"],
            }
        )
        stored = {k: v for k, v in cfg.data.items() if k != "source_path"}
        conn = db.connect(self.base / "runs" / "collector.sqlite")
        try:
            return db.create_run(
                conn,
                fingerprint="z" * 12,
                config_json=json.dumps(stored),
                config_source=str(self.generated / "killed.yaml"),
                collector_version="test",
                schema_version=1,
                go_binary="",
                go_version=None,
                target_count=5,
            )
        finally:
            conn.close()

    def test_the_list_and_the_feed_agree_that_it_is_not_running(self):
        run_id = self.make_run()
        _status, home = self.get("/")
        self.assertIn("已中断", home)
        _status, payload = self.get("/api/runs")
        row = next(r for r in json.loads(payload)["runs"] if r["id"] == run_id)
        # The raw value survives for anyone who needs to quote it ...
        self.assertEqual(row["status"], "running")
        # ... while what a person reads, and what the poll repaints, is the truth.
        self.assertIn("已中断", row["status_text"])
        self.assertFalse(row["active"])

    def test_the_detail_page_says_it_too(self):
        run_id = self.make_run()
        _status, page = self.get(f"/runs/{run_id}")
        self.assertIn("已中断", page)
        # The machine value is still visible on the page, in a code span.
        self.assertIn("机器状态", page)
        self.assertIn(">running<", page)

    def test_a_stage_of_a_dead_run_is_not_left_claiming_to_run(self):
        # The process that would have been running that stage is the same one
        # that is gone, so leaving the row at 运行中 would be the same lie one
        # level down.
        from collector import db

        run_id = self.make_run()
        conn = db.connect(self.base / "runs" / "collector.sqlite")
        try:
            db.set_stage(conn, run_id, "discover", "running")
            db.set_stage(conn, run_id, "ingest", "pending")
        finally:
            conn.close()
        _status, page = self.get(f"/runs/{run_id}")
        self.assertNotIn(">运行中<", page)
        self.assertIn("已中断（进程不在了）", page)
        # A stage that is merely pending keeps its own label: only ``running``
        # claims a process exists.
        self.assertIn(">待开始<", page)

    def test_the_detail_page_carries_the_acquisition_section(self):
        # 026 P3.1 put the acquisition facts in the workbook; 026 P3.5 found
        # that the page a person actually looks at did not have them.
        run_id = self.make_run()
        _status, page = self.get(f"/runs/{run_id}")
        self.assertIn("获取方式", page)
        self.assertIn("来源时间", page)

    def test_the_poll_keeps_polling_only_while_a_run_is_active(self):
        # The poll must stop when nothing is running any more, otherwise a
        # stopped run keeps a request every three seconds for ever.
        source = local_ui.PAGE
        self.assertIn("if (run.active)", source)
        self.assertIn("clearInterval(timer)", source)


class BusyScreenTest(unittest.TestCase):
    """026 P3.5: the second run is refused in words, not by dropping the socket.

    Found by the bat end-to-end run: `POST /runs` while a run was in progress
    reached ``RunManager.start``, whose refusal was a ``RuntimeError`` nobody
    caught. The browser got a closed connection and no explanation. Two tabs, or
    one page left open from before the button was disabled, is an ordinary way to
    get there.
    """

    class _Busy:
        """A manager that is already running one configuration."""

        def __init__(self, config: pathlib.Path):
            self.config = str(config)
            self.starts: list[str] = []

        def is_running(self) -> bool:
            return True

        def running_config(self) -> str:
            return self.config

        def start(self, config_path, base_dir):  # pragma: no cover - must not run
            self.starts.append(str(config_path))
            raise RuntimeError("a run is already in progress; stop it first")

        def stop(self) -> bool:
            return False

    def setUp(self):
        from http.server import ThreadingHTTPServer

        self.tmp = support.temp_dir(self)
        self.base = self.tmp / "project"
        self.generated = self.base / "verification" / "ui" / "generated"
        (self.base / "runs").mkdir(parents=True)
        self.manager = self._Busy(self.generated / "live.yaml")
        handler = local_ui.make_handler(self.base, self.manager)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        # LIFO: shutdown first, then close. Closing first would leave the serve
        # loop selecting on a dead socket (WinError 10038 in the thread).
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path: str):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=10
        ) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, path: str, fields: dict | None = None):
        import http.client

        pairs = [
            (key, item)
            for key, value in (fields or {}).items()
            for item in (value if isinstance(value, list) else [value])
        ]
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(
                "POST", path, body=urllib.parse.urlencode(pairs),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            response.read()
            return response.status, response.getheader("Location") or ""
        finally:
            connection.close()

    def test_a_second_run_is_refused_with_a_sentence(self):
        status, location = self.post(
            "/runs",
            {
                "industry": ["宠物服务"], "country": ["中国"], "city": ["杭州"],
                "province": ["浙江省"], "geo": ["30.2741,120.1551"],
                "target_count": ["10"], "mode": ["fast"], "formats": ["csv,xlsx"],
                "enrich": ["off"], "sources": ["local_pages"],
            },
        )
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/?error="), location)
        self.assertIn(local_ui.BUSY_NOTE, urllib.parse.unquote(location))
        # Nothing was started behind the refusal.
        self.assertEqual(self.manager.starts, [])

    def test_the_home_page_says_the_same_sentence_next_to_the_button(self):
        _status, home = self.get("/")
        self.assertIn(local_ui.BUSY_NOTE, home)
        self.assertIn('id="running-note"', home)
        # The button is disabled too, so the ordinary path does not even get to
        # the refusal.
        self.assertIn("disabled", home)

    def test_the_poll_puts_the_servers_sentence_back(self):
        # The sentence lives in one place (BUSY_NOTE). The script must reuse the
        # text the server rendered instead of carrying a second copy.
        self.assertIn("noteText", local_ui.PAGE)
        self.assertNotIn("有一个采集正在进行中", local_ui.PAGE)

    def test_resuming_from_a_stale_page_is_refused_too(self):
        from collector import db

        conn = db.connect(self.base / "runs" / "collector.sqlite")
        try:
            run_id = db.create_run(
                conn, fingerprint="y" * 12, config_json="{}",
                config_source=str(self.generated / "stale.yaml"),
                collector_version="test", schema_version=1, go_binary="",
                go_version=None, target_count=5,
            )
        finally:
            conn.close()
        status, location = self.post(f"/runs/{run_id}/resume", {})
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith(f"/runs/{run_id}?error="), location)
        self.assertIn(local_ui.BUSY_NOTE, urllib.parse.unquote(location))
        self.assertEqual(self.manager.starts, [])
        # And the reason is on the page it lands on, not just in the URL.
        _status, page = self.get(location)
        self.assertIn("无法开始", page)
        self.assertIn(local_ui.BUSY_NOTE, page)


class ConfigFileNameTest(unittest.TestCase):
    """026 P3.5: the industry field is free text, the config path is not.

    Found by the bat end-to-end run: an industry of ``宠物服务（停止/恢复夹具）``
    turned ``export_root / f"{name}-{stamp}.yaml"`` into a path whose parent was a
    directory called ``宠物服务（停止`` — which ``write_config`` then created, and
    which also made the run unrecognisable as this screen's own. The same shape
    with ``..`` would have written the config outside the project.
    """

    def test_a_slash_cannot_become_a_directory(self):
        name = local_ui.config_filename("宠物服务（停止/恢复夹具）", stamp="20261014-000000")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertEqual(pathlib.Path(name).name, name)

    def test_a_windows_separator_and_a_drive_letter_are_neutralised(self):
        for bad in ("C://Windows//x", "..\\..\\escape", "/etc/passwd"):
            name = local_ui.config_filename(bad, stamp="s")
            self.assertEqual(pathlib.Path(name).name, name, bad)

    def test_the_readable_chinese_name_survives(self):
        # The file name is also what a person reads in the generated directory,
        # so the rewrite must not turn it into a hash.
        self.assertEqual(
            local_ui.config_filename("宠物服务", stamp="s"), "宠物服务-s.yaml"
        )

    def test_an_empty_or_dot_only_name_falls_back(self):
        for bad in ("", "   ", "...", "///"):
            self.assertEqual(local_ui.config_filename(bad, stamp="s"), "run-s.yaml", bad)


class NestedConfigRecognitionTest(unittest.TestCase):
    """A config written into a subdirectory is still this screen's own run."""

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.own = self.tmp / "verification" / "ui" / "generated"
        (self.own / "宠物服务（停止").mkdir(parents=True)

    def row(self, config_source: str):
        return {"id": 1, "status": "running", "config_source": config_source}

    def test_a_config_in_a_subdirectory_is_recognised(self):
        nested = self.own / "宠物服务（停止" / "x.yaml"
        label, _hint = local_ui.run_status_view(
            self.row(str(nested)), live_config="", own_dir=self.own
        )
        self.assertIn("已中断", label)

    def test_a_sibling_directory_is_not(self):
        sibling = self.tmp / "elsewhere" / "x.yaml"
        label, _hint = local_ui.run_status_view(
            self.row(str(sibling)), live_config="", own_dir=self.own
        )
        self.assertEqual(label, "运行中")


class ScreenFailureTest(unittest.TestCase):
    """026 P3.5: the screen's own failure is a sentence, not a dropped socket.

    An unhandled exception used to close the connection: the browser showed
    nothing, and the console filled with a traceback. The status feed gets a body
    with no ``running``/``runs`` keys, because "I cannot read the database" must
    not reach the poller as "nothing is running".
    """

    def setUp(self):
        import tempfile
        from http.server import ThreadingHTTPServer
        from unittest import mock

        # The failure path prints the full stack for whoever is debugging; the
        # test records that it happened instead of flooding its own output.
        printer = mock.patch("traceback.print_exc")
        self.printed = printer.start()
        self.addCleanup(printer.stop)

        handle = tempfile.TemporaryDirectory()
        self.addCleanup(handle.cleanup)
        self.base = pathlib.Path(handle.name) / "project"
        (self.base / "runs").mkdir(parents=True)
        # A *directory* where the database file should be: sqlite cannot open it,
        # so every page hits the failure path deterministically.
        (self.base / "runs" / "collector.sqlite").mkdir()
        handler = local_ui.make_handler(self.base, local_ui.RunManager())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path: str):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        finally:
            connection.close()

    def test_a_broken_page_says_so_instead_of_dropping_the_connection(self):
        status, body = self.get("/")
        self.assertEqual(status, 500)
        self.assertIn("界面自己出错了", body)
        # And it says what is *not* affected, so the failure is not read as
        # "my collection is lost".
        self.assertIn("采集任务本身在服务端跑", body)
        # The stack still reaches the console, where it is useful.
        self.assertTrue(self.printed.called)

    def test_the_status_feed_never_reports_nothing_running_on_a_failure(self):
        status, body = self.get("/api/runs")
        self.assertEqual(status, 500)
        payload = json.loads(body)
        self.assertIn("error", payload)
        self.assertNotIn("running", payload)
        self.assertNotIn("runs", payload)

    def test_the_poll_skips_repainting_an_unreadable_feed(self):
        # The template, so the doubled braces are the escaping ``.format`` needs.
        self.assertIn("if (data.error) {{ return; }}", local_ui.PAGE)


class ResumeNoteTest(unittest.TestCase):
    """What 「恢复/重跑」 says it does, on a run that already finished.

    ``latest_resumable_run`` only returns a run that is still running /
    interrupted / failed / partial, so pressing the button on a *completed*
    run starts a new run with the same config and collects again. The detail
    page used to promise "同一配置只跑一遍，不重复采集" in that case too, which
    is false; the note now follows the run's own status and points a finished
    run at the re-export form, which really does not re-fetch.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile
        from http.server import ThreadingHTTPServer

        handle = tempfile.TemporaryDirectory()
        cls.addClassCleanup(handle.cleanup)
        cls.base = pathlib.Path(handle.name) / "project"
        (cls.base / "runs").mkdir(parents=True)
        handler = local_ui.make_handler(cls.base, local_ui.RunManager())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path: str):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=10
        ) as response:
            return response.status, response.read().decode("utf-8")

    def make_run(self, status: str) -> int:
        from collector import db

        cfg = support.config({"sources": ["google_maps"]})
        stored = {k: v for k, v in cfg.data.items() if k != "source_path"}
        conn = db.connect(self.base / "runs" / "collector.sqlite")
        try:
            run_id = db.create_run(
                conn,
                fingerprint=f"{status[:4]}123456789",
                config_json=json.dumps(stored),
                config_source="test.yaml",
                collector_version="test",
                schema_version=1,
                go_binary="",
                go_version=None,
                target_count=5,
            )
            db.finish_run(conn, run_id, status)
            return run_id
        finally:
            conn.close()

    def test_a_finished_run_is_told_it_would_collect_again(self):
        run_id = self.make_run("completed")
        _status, page = self.get(f"/runs/{run_id}")
        self.assertIn("重新开始一次采集", page)
        self.assertIn("不是接着这次继续", page)
        # The honest alternative is named in the same breath.
        self.assertIn("重新导出（不重抓）", page)
        self.assertNotIn("同一配置只跑一遍", page)

    def test_an_interrupted_run_keeps_the_continue_wording(self):
        run_id = self.make_run("interrupted")
        _status, page = self.get(f"/runs/{run_id}")
        self.assertIn("从中断处继续", page)
        self.assertIn("同一配置只跑一遍", page)
        self.assertNotIn("重新开始一次采集", page)


class RefusalEchoTest(unittest.TestCase):
    """054 items 3/4: a refusal keeps the form, and says whose error it is.

    This is the browser-side half of the fix: the server writes the message and
    the values into one redirect, so "fix one typo and resubmit" does not mean
    retyping the whole form -- and because the country is not reset to DE, the
    retry fails for the reason the person is fixing, not for a new one.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls._handle = tempfile.TemporaryDirectory()
        cls.base = pathlib.Path(cls._handle.name) / "project"
        (cls.base / "runs").mkdir(parents=True)
        cls.manager = local_ui.RunManager()
        handler = local_ui.make_handler(cls.base, cls.manager)
        from http.server import ThreadingHTTPServer

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.manager.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls._handle.cleanup()

    def submit(self, fields: dict) -> str:
        """POST the form without following the redirect; return the Location."""
        import http.client

        pairs = [
            (key, item)
            for key, value in fields.items()
            for item in (value if isinstance(value, list) else [value])
        ]
        body = urllib.parse.urlencode(pairs)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(
                "POST", "/runs", body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 303)
            return response.getheader("Location") or ""
        finally:
            connection.close()

    def page(self, location: str) -> str:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{location}", timeout=20
            ) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.read().decode("utf-8")

    def field(self, page: str, name: str) -> str | None:
        import re

        found = re.search(rf'name="{name}"[^>]*value="([^"]*)"', page)
        return found.group(1) if found else None

    def test_a_refusal_keeps_every_field_it_can(self):
        # The whole set, not just country and city: 054 asks for all of them.
        location = self.submit(
            {
                "industry": "宠物服务",
                "city": "武汉",
                "country": "德国",
                "province": "湖北省",
                "district": "洪山区",
                "target_count": "17",
                "sources": ["directory"],
                "languages": ["zh", "bilingual"],
                "directory_preset": "shunqiwang",
                "match_profile": "pet",
                "match_keywords": "水族,鸟食",
                "csv_path": "",
            }
        )
        page = self.page(location)
        for name, expected in (
            ("industry", "宠物服务"),
            ("city", "武汉"),
            ("province", "湖北省"),
            ("district", "洪山区"),
            ("target_count", "17"),
            ("match_keywords", "水族,鸟食"),
        ):
            self.assertEqual(self.field(page, name), expected, name)
        # The country field is echoed too -- and it is *not* reset to DE.
        self.assertEqual(self.field(page, "country"), "德国")
        # Ticks survive as ticks.
        self.assertIn('value="bilingual" checked', page)
        self.assertIn('value="zh" checked', page)
        # Dropdowns survive as selections.
        self.assertIn('data-preset-note="shunqiwang"', page)
        self.assertIn('<option value="shunqiwang" selected>', page)

    def test_the_country_a_person_typed_reaches_the_config(self):
        # The echo must agree with what the config builder does, or the retry
        # would fail for a *new* reason.
        from collector import regions

        self.assertEqual(regions.normalize_country("德国"), "DE")
        self.assertEqual(regions.normalize_country("意大利"), "IT")

    def test_an_unchanged_resubmit_is_not_marked_stale(self):
        # The stale *note* is a rendered paragraph; the string "stale-error"
        # also appears in the page's own script as an element id, so the
        # assertion is on the element, not on the bare word.
        fields = {"industry": "宠物服务", "city": "武汉", "country": "德国"}
        first = self.page(self.submit(fields))
        second = self.page(self.submit(fields))
        self.assertIn("无法开始", first)
        self.assertNotIn("id='stale-error'", first)
        self.assertNotIn('id="stale-error"', first)
        self.assertNotIn("id='stale-error'", second)
        self.assertNotIn('id="stale-error"', second)

    def test_an_edited_form_marks_the_old_error_as_stale(self):
        # A fingerprint left over from a submission whose values are no longer
        # on screen must be labelled, and the message must still be there:
        # "why did that fail?" is not answered by hiding it.
        location = self.submit({"industry": "宠物服务", "city": "武汉", "country": "德国"})
        tampered = location.replace(location.split("fp=")[1].split("&")[0], "000000000000")
        page = self.page(tampered)
        self.assertIn("无法开始", page)
        self.assertIn("id='stale-error'", page)
        self.assertIn("之前那次提交", page)

    def test_the_browser_marks_an_edited_error_without_a_resubmit(self):
        # The client-side half: editing any box after reading the error marks
        # the banner as describing the previous submission.
        page = self.page(self.submit({"industry": "x", "city": "武汉", "country": "德国"}))
        self.assertIn("id='submit-error'", page)
        self.assertIn("form.addEventListener('input', markStale)", page)
        self.assertIn("banner.classList.add('stale')", page)

    def test_an_unknown_city_error_names_the_country_it_checked(self):
        page = self.page(self.submit({"industry": "x", "city": "武汉", "country": "IT"}))
        self.assertIn("无法开始", page)
        self.assertIn("IT", page)


class PreflightWarningTest(unittest.TestCase):
    """054: warn about a run that will collect nothing, before it starts."""

    def warn(self, form: dict) -> str:
        return " ".join(local_ui.preflight_warnings(form))

    def test_a_directory_run_without_a_url_is_warned_about(self):
        text = self.warn({"sources": ["directory"], "directory_url": [""]})
        self.assertIn("目录列表页地址", text)
        # And it says the rest is unaffected, which is true and actionable.
        self.assertIn("其余来源不受影响", text)

    def test_a_preset_that_does_not_match_the_url_is_warned_about(self):
        # The failure this prevents is silent: wrong selectors return few or
        # zero values, which reads as "this site has no companies".
        text = self.warn(
            {
                "sources": ["directory"],
                "directory_url": ["https://www.11467.com/hangzhou/search/9092.htm"],
                "directory_preset": ["b2b168"],
            }
        )
        self.assertIn("11467", text)
        self.assertIn("shunqiwang", text)

    def test_a_matching_preset_is_not_warned_about(self):
        for url, preset in (
            ("https://www.b2b168.com/zhejiang-hangzhou-chongwu/", "b2b168"),
            ("https://www.11467.com/hangzhou/search/9092.htm", "shunqiwang"),
        ):
            self.assertEqual(self.warn({"sources": ["directory"], "directory_url": [url],
                                        "directory_preset": [preset]}), "", url)

    def test_an_unknown_host_is_named_rather_than_guessed_at(self):
        text = self.warn(
            {
                "sources": ["directory"],
                "directory_url": ["https://example.invalid/list"],
                "directory_preset": ["b2b168"],
            }
        )
        self.assertIn("example.invalid", text)
        self.assertIn("不是这个站点没有公司", text)

    def test_the_generic_preset_is_never_called_a_mismatch(self):
        # It has no host, on purpose: it is the honest "I don't know this site"
        # option, so it is not a mistake to use it.
        self.assertEqual(
            self.warn(
                {
                    "sources": ["directory"],
                    "directory_url": ["https://example.invalid/list"],
                    "directory_preset": ["generic"],
                }
            ),
            "",
        )

    def test_an_offline_page_from_another_site_is_warned_about(self):
        text = self.warn(
            {
                "sources": ["local_pages"],
                "local_listing_path": ["C:/x/list.html"],
                "local_origin_url": ["https://www.b2b168.com/zhejiang-hangzhou-chongwu/"],
                "local_preset": ["shunqiwang"],
            }
        )
        self.assertIn("页面里没有公司", text)

    def test_nothing_is_said_about_a_form_that_is_fine(self):
        self.assertEqual(self.warn({"sources": ["google_maps"]}), "")
        self.assertEqual(self.warn({}), "")

    def test_the_warning_is_not_a_refusal(self):
        # It must be possible to build a config for a warned-about directory
        # run: the warning tells the person what will happen, it does not
        # decide for them.
        form = {
            "sources": ["directory"],
            "directory_url": ["https://www.11467.com/hangzhou/search/9092.htm"],
            "directory_preset": ["b2b168"],
            "industry": "宠物服务",
            "city": "杭州",
            "country": "CN",
            "languages": ["zh"],
        }
        self.assertTrue(local_ui.preflight_warnings(form))


class PreStartConfirmationTest(unittest.TestCase):
    """054 follow-up: a warned-about run is shown the warning, then starts.

    The first version of the pre-flight banner was only rendered when an
    *earlier refusal* had echoed the form back. A preset/host mismatch is a
    legal submission -- it is not refused -- so it started the run with no
    banner at all, which made the banner's own "开始前的提醒" a promise the
    screen did not keep. These tests pin the corrected flow:

    * submit with a warning and no acknowledgement -> the form comes back with
      the warning, and the run has *not* started;
    * the reprised page carries the acknowledgement field, so one more click
      starts the run;
    * a submission with nothing to warn about is not interrupted at all.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls._handle = tempfile.TemporaryDirectory()
        cls.base = pathlib.Path(cls._handle.name) / "project"
        (cls.base / "runs").mkdir(parents=True)
        cls.manager = local_ui.RunManager()
        handler = local_ui.make_handler(cls.base, cls.manager)
        from http.server import ThreadingHTTPServer

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.manager.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls._handle.cleanup()

    def submit(self, fields: dict) -> str:
        import http.client

        pairs = [
            (key, item)
            for key, value in fields.items()
            for item in (value if isinstance(value, list) else [value])
        ]
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(
                "POST", "/runs", body=urllib.parse.urlencode(pairs),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 303)
            return response.getheader("Location") or ""
        finally:
            connection.close()

    def page(self, location: str) -> str:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{location}", timeout=20
            ) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.read().decode("utf-8")

    def warned_form(self) -> dict:
        return {
            "industry": "宠物服务",
            "city": "杭州",
            "country": "CN",
            "province": "浙江省",
            "sources": ["directory"],
            "languages": ["zh"],
            "directory_url": ["https://www.11467.com/hangzhou/search/9092.htm"],
            "directory_preset": ["b2b168"],
        }

    def test_a_warned_about_run_is_shown_the_warning_first(self):
        location = self.submit(self.warned_form())
        # Not refused (nothing to fix) and not started (it goes back to the
        # form) -- the confirmation round trip. ``error`` is present but empty:
        # a warning is not a refusal.
        self.assertTrue(location.startswith("/?"), location)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        self.assertEqual((query.get("error") or [""])[0], "")
        self.assertTrue(query.get("warn"), "the warning must ride in the URL")
        page = self.page(location)
        self.assertIn("开始前的提醒", page)
        self.assertIn("11467", page)
        # The run has not started: a fresh screen with no runs.
        self.assertNotIn("运行中", page)

    def test_the_reprised_page_carries_the_acknowledgement(self):
        page = self.page(self.submit(self.warned_form()))
        self.assertIn('name="confirmed"', page)

    def test_a_form_with_nothing_to_warn_about_is_not_interrupted(self):
        # A CSV import has no preset/URL to mismatch, so it goes straight to
        # the run: no extra click for the ordinary case.
        location = self.submit(
            {
                "industry": "宠物服务", "city": "Sondrio", "country": "意大利",
                "sources": ["csv_list"], "languages": ["zh"],
                "csv_list": "on", "csv_path": "C:/x/does-not-exist.csv",
            }
        )
        # It is refused for the *missing file*, which is a different sentence.
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        self.assertTrue((query.get("error") or [""])[0])
        self.assertFalse(query.get("warn"))
        self.assertNotIn("开始前的提醒", self.page(location))

    def test_a_first_visit_does_not_carry_the_acknowledgement(self):
        # If the hidden field were on every page, the warnings would never be
        # seen: the first submit would already claim to have read them.
        page = self.page("/")
        self.assertNotIn('name="confirmed"', page)
