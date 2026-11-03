"""Configuration loading, validation and fingerprinting."""

from __future__ import annotations

import json
import pathlib
import unittest

from collector import config as config_module
from collector.errors import ConfigError

from . import support


class ValidationTest(unittest.TestCase):
    def test_base_config_is_accepted_and_normalised(self):
        cfg = support.config()
        self.assertEqual(cfg.industry, "dentists")
        self.assertEqual(cfg.query, "dentists in Berlin")
        self.assertEqual(cfg.country, "DE")
        self.assertIsNone(cfg.bbox)
        self.assertEqual(cfg.mode, "fast")
        self.assertEqual(cfg.target_count, 50)

    def test_country_is_upper_cased(self):
        cfg = support.config({"location": {"country": "de"}})
        self.assertEqual(cfg.country, "DE")

    def test_unknown_top_level_key_is_rejected_with_its_path(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"target": 5})
        self.assertIn("<root>: unknown key(s) ['target']", str(ctx.exception))

    def test_unknown_nested_key_is_rejected_with_its_path(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"collect": {"mode": "fast", "fields": ["title"], "depth": 2}})
        self.assertIn("collect: unknown key(s) ['depth']", str(ctx.exception))

    def test_unknown_validation_rule_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"validation": {"rules": {"require_email": True}}})
        self.assertIn("validation.rules: unknown key(s) ['require_email']", str(ctx.exception))

    def test_missing_required_value_names_the_path(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"target_count": None})
        self.assertIn("target_count: required, but missing", str(ctx.exception))

    def test_an_absent_industry_is_allowed_and_stays_blank(self):
        # 045: a batch whose records come from a named seed list has no search
        # industry, and must not be given a fabricated one. Blank is a real
        # value and survives validation unchanged.
        for value in (None, "", "   "):
            with self.subTest(value=value):
                cfg = support.config({"industry": value})
                self.assertEqual(cfg.industry, "")

    def test_a_stated_industry_is_kept(self):
        cfg = support.config({"industry": "  dentists  "})
        self.assertEqual(cfg.industry, "dentists")

    def test_missing_required_nested_value_names_the_path(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"location": {"country": None}})
        self.assertIn("location.country: required, but missing", str(ctx.exception))

    def test_bad_country_code_is_rejected(self):
        # Stage 019 widened what counts as a *known* country: a code, a Chinese
        # name or an English name all resolve (``Germany`` -> ``DE``). The
        # refusal must still fire for something that resolves to nothing, and it
        # must quote what was typed rather than substitute a country.
        with self.assertRaises(ConfigError) as ctx:
            support.config({"location": {"country": "Narnia"}})
        message = str(ctx.exception)
        self.assertIn("location.country: expected a two-letter ISO-3166 code", message)
        self.assertIn("Narnia", message)

    def test_country_names_resolve_to_iso_codes(self):
        for typed, expected in (("中国", "CN"), ("CN", "CN"), ("cn", "CN"), ("Germany", "DE")):
            with self.subTest(typed=typed):
                cfg = support.config({"location": {"country": typed}})
                self.assertEqual(cfg.data["location"]["country"], expected)
                # The text the operator wrote is kept for display only...
                self.assertEqual(cfg.data["location"]["country_input"], typed)
                # ...and must not change the collection fingerprint.
                self.assertEqual(cfg.fingerprint(), support.config(
                    {"location": {"country": expected}}
                ).fingerprint())

    def test_target_count_must_be_positive(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"target_count": 0})
        self.assertIn("target_count: must be >= 1", str(ctx.exception))

    def test_boolean_is_not_accepted_as_an_integer(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"target_count": True})
        self.assertIn("target_count: expected an integer", str(ctx.exception))

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"sources": ["osm"]})
        self.assertIn("sources[0]: unknown value 'osm'", str(ctx.exception))

    def test_unknown_collect_field_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"collect": {"fields": ["title", "e-mail"]}})
        self.assertIn("collect.fields[1]: unknown value 'e-mail'", str(ctx.exception))

    def test_duplicate_collect_field_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"collect": {"fields": ["title", "title"]}})
        self.assertIn("collect.fields[1]: duplicate value 'title'", str(ctx.exception))

    def test_title_is_required_in_collect_fields(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"collect": {"fields": ["address", "phone"]}})
        self.assertIn("collect.fields: 'title' is required", str(ctx.exception))

    def test_unknown_export_field_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"export": {"fields": ["title", "nope"]}})
        self.assertIn("export.fields[1]: unknown value 'nope'", str(ctx.exception))

    def test_unknown_export_format_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"export": {"formats": ["pdf"]}})
        self.assertIn("export.formats[0]: unknown value 'pdf'", str(ctx.exception))


class BBoxTest(unittest.TestCase):
    def test_bbox_must_have_four_numbers(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config(
                {"collect": {"mode": "browser", "max_depth": 1},
                 "location": {"bbox": [52.5, 13.4, 52.6]}}
            )
        self.assertIn("location.bbox: expected [minLat, minLon, maxLat, maxLon]", str(ctx.exception))

    def test_bbox_latitude_order_is_checked(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config(
                {"collect": {"mode": "browser", "max_depth": 1},
                 "location": {"bbox": [52.6, 13.4, 52.5, 13.5]}}
            )
        self.assertIn("minLat must be < maxLat", str(ctx.exception))

    def test_bbox_is_parsed_in_order(self):
        cfg = support.config(
            {"collect": {"mode": "browser", "max_depth": 1},
             "location": {"bbox": [52.515, 13.385, 52.535, 13.415]}}
        )
        self.assertEqual(cfg.bbox, [52.515, 13.385, 52.535, 13.415])

    def test_fast_mode_requires_a_search_centre(self):
        cfg = support.config()
        raw = support.deep_merge(support.BASE_CONFIG, {})
        raw["location"] = {k: v for k, v in raw["location"].items() if k != "geo"}
        with self.assertRaises(ConfigError) as ctx:
            config_module.validate(raw)
        self.assertIn("location.geo: fast mode needs a search centre", str(ctx.exception))

    def test_geo_is_parsed(self):
        self.assertEqual(support.config().geo, [52.52, 13.405])

    def test_geo_must_be_a_lat_lon_pair(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"location": {"geo": [52.52]}})
        self.assertIn("location.geo: expected [lat, lon]", str(ctx.exception))

    def test_geo_latitude_range_is_checked(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"location": {"geo": [120.0, 13.4]}})
        self.assertIn("location.geo[0]: must be <= 90.0", str(ctx.exception))

    def test_fast_mode_rejects_a_bbox(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"location": {"bbox": [52.515, 13.385, 52.535, 13.415]}})
        self.assertIn("fast mode cannot be combined with a bbox", str(ctx.exception))

    def test_fast_mode_rejects_depth_other_than_one(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"collect": {"max_depth": 2}})
        self.assertIn("fast mode has no paging support", str(ctx.exception))


class EnrichTest(unittest.TestCase):
    def test_enrichment_is_accepted_in_stage_d(self):
        cfg = support.config(
            {"enrich": {"enabled": True, "fields": ["emails"], "pages_per_site": 2}}
        )
        self.assertTrue(cfg.enrich_enabled)
        self.assertEqual(cfg.enrich_fields, ["emails"])
        self.assertEqual(cfg.enrich_pages_per_site, 2)

    def test_enrich_fields_without_enabling_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"enrich": {"enabled": False, "fields": ["emails"]}})
        self.assertIn("enrich.enabled is false", str(ctx.exception))

    def test_enabling_without_fields_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config({"enrich": {"enabled": True, "fields": [], "pages_per_site": 2}})
        self.assertIn("no field is requested", str(ctx.exception))

    def test_enabling_without_pages_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config(
                {"enrich": {"enabled": True, "fields": ["emails"], "pages_per_site": 0}}
            )
        self.assertIn("enrich.pages_per_site: must be >= 1", str(ctx.exception))

    def test_a_field_the_extractor_cannot_produce_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            support.config(
                {"enrich": {"enabled": True, "fields": ["latitude"], "pages_per_site": 1}}
            )
        self.assertIn("enrich.fields[0]: unknown value 'latitude'", str(ctx.exception))

    def test_enrich_budget_falls_back_to_the_run_budget(self):
        cfg = support.config(
            {
                "enrich": {"enabled": True, "fields": ["emails"], "pages_per_site": 1},
                "runtime": {"budget": {"max_requests": 7, "max_seconds": 11}},
            }
        )
        self.assertEqual(cfg.enrich_request_budget, 7)
        self.assertEqual(cfg.enrich_time_budget_seconds, 11)

    def test_enrich_budget_overrides_the_run_budget(self):
        cfg = support.config(
            {
                "enrich": {
                    "enabled": True,
                    "fields": ["emails"],
                    "pages_per_site": 1,
                    "budget": {"max_requests": 3, "max_seconds": 4},
                },
                "runtime": {"budget": {"max_requests": 7, "max_seconds": 11}},
            }
        )
        self.assertEqual(cfg.enrich_request_budget, 3)
        self.assertEqual(cfg.enrich_time_budget_seconds, 4)


class SiteDiscoveryTest(unittest.TestCase):
    """The ``sitediscovery`` section (round 026 P1).

    The section decides *what is collected*, so unlike ``match`` it takes part
    in the fingerprint -- except when it is switched off, which is pruned back
    to nothing so a historical configuration keeps the value it had.
    """

    def section(self, **node):
        overrides = {"sitediscovery": node}
        return support.config(overrides)

    def test_discovery_is_accepted_and_normalised(self):
        cfg = self.section(
            enabled=True,
            sources=["user_list", "search"],
            user_list={"path": "sites.csv"},
            pages_per_site=1,
            budget={"max_requests": 12, "max_seconds": 34},
        )
        self.assertTrue(cfg.sitediscovery_enabled)
        self.assertEqual(cfg.sitediscovery_sources, ["user_list", "search"])
        self.assertEqual(cfg.sitediscovery_pages_per_site, 1)
        self.assertEqual(cfg.sitediscovery_request_budget, 12)
        self.assertEqual(cfg.sitediscovery_seconds_budget, 34)

    def test_pages_per_site_defaults_to_two(self):
        self.assertEqual(self.section(enabled=True, sources=["user_list"], user_list={"path": "a"}).sitediscovery_pages_per_site, 2)

    # -- the retry policy (042) ----------------------------------------------

    def test_a_failed_candidate_may_be_retried_and_the_bounds_are_the_defaults(self):
        cfg = self.section(enabled=True, sources=["user_list"], user_list={"path": "a"})
        self.assertEqual(cfg.sitediscovery_retry_max_attempts, 2)
        self.assertEqual(cfg.sitediscovery_retry_cooldown_seconds, 600)

    def test_the_retry_bounds_can_be_set(self):
        cfg = self.section(
            enabled=True, sources=["user_list"], user_list={"path": "a"},
            retry={"max_attempts": 4, "cooldown_seconds": 0},
        )
        self.assertEqual(cfg.sitediscovery_retry_max_attempts, 4)
        self.assertEqual(cfg.sitediscovery_retry_cooldown_seconds, 0)

    def test_asking_forever_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            self.section(
                enabled=True, sources=["user_list"], user_list={"path": "a"},
                retry={"max_attempts": 0},
            )
        self.assertIn("max_attempts", str(ctx.exception))

    def test_an_unknown_retry_key_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            self.section(
                enabled=True, sources=["user_list"], user_list={"path": "a"},
                retry={"forever": True},
            )
        self.assertIn("retry", str(ctx.exception))

    def test_leaving_the_retry_policy_out_keeps_the_fingerprint(self):
        # A configuration written before the retry policy existed must not
        # become a different run: the node is only emitted when it is written.
        node = {"enabled": True, "sources": ["user_list"], "user_list": {"path": "a"}}
        without = self.section(**node).fingerprint()
        from collector.sitediscovery import (
            DEFAULT_RETRY_COOLDOWN_SECONDS,
            DEFAULT_RETRY_MAX_ATTEMPTS,
        )

        explicit = self.section(
            **node,
            retry={
                "max_attempts": DEFAULT_RETRY_MAX_ATTEMPTS,
                "cooldown_seconds": DEFAULT_RETRY_COOLDOWN_SECONDS,
            },
        )
        self.assertNotEqual(without, explicit.fingerprint())
        self.assertNotIn("retry", self.section(**node).data["sitediscovery"])

    def test_settings_without_the_switch_are_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            self.section(enabled=False, user_list={"path": "sites.csv"})
        self.assertIn("enabled is false", str(ctx.exception))

    def test_enabling_without_sources_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            self.section(enabled=True)
        self.assertIn("names no source", str(ctx.exception))

    def test_enabling_with_nothing_actually_configured_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            self.section(enabled=True, sources=["search"])
        self.assertIn("none of them is configured", str(ctx.exception))

    def test_an_unknown_finder_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            self.section(enabled=True, sources=["google"])
        self.assertIn("unknown value 'google'", str(ctx.exception))

    def test_an_unknown_key_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            self.section(enabled=True, sources=["user_list"], user_list={"path": "a"}, x=1)
        self.assertIn("unknown key", str(ctx.exception))

    def test_a_page_entry_needs_a_target_and_a_path(self):
        with self.assertRaises(ConfigError) as ctx:
            self.section(
                enabled=True, sources=["page_links"], page_links={"pages": [{"entity": "A"}]}
            )
        self.assertIn("path: required", str(ctx.exception))
        with self.assertRaises(ConfigError) as ctx:
            self.section(
                enabled=True, sources=["page_links"], page_links={"pages": [{"path": "a.html"}]}
            )
        self.assertIn("entity: required", str(ctx.exception))

    def test_search_may_be_listed_while_unconfigured(self):
        # Its absence is then *reported* rather than hidden -- the whole point of
        # declaring the finder at all.
        cfg = self.section(enabled=True, sources=["user_list", "search"], user_list={"path": "a"})
        self.assertTrue(cfg.sitediscovery_enabled)

    def test_a_disabled_section_is_pruned_from_the_fingerprint(self):
        plain = support.config()
        off = self.section(enabled=False)
        self.assertNotIn("sitediscovery", off.collection_view())
        self.assertEqual(off.fingerprint(), plain.fingerprint())

    def test_an_enabled_section_changes_the_fingerprint(self):
        plain = support.config()
        on = self.section(enabled=True, sources=["user_list"], user_list={"path": "sites.csv"})
        self.assertIn("sitediscovery", on.collection_view())
        self.assertNotEqual(on.fingerprint(), plain.fingerprint())

    def test_the_normalised_section_validates_again(self):
        normalised = config_module.validate(
            support.deep_merge(
                support.BASE_CONFIG,
                {"sitediscovery": {"enabled": True, "sources": ["user_list"], "user_list": {"path": "a"}}},
            ),
            source="<test>",
        )
        again = config_module.validate(normalised, source="<test>")
        self.assertEqual(again, normalised)


class FieldGatingTest(unittest.TestCase):
    def test_emails_add_the_email_fetch_flag(self):
        cfg = support.config({"collect": {"fields": ["title", "emails"]}})
        self.assertIn("-email", cfg.fetch_flags())

    def test_no_email_flag_when_emails_are_not_requested(self):
        cfg = support.config({"collect": {"fields": ["title", "address"]}})
        self.assertNotIn("-email", cfg.fetch_flags())
        self.assertEqual(cfg.fetch_flags(), [])

    def test_extra_reviews_flag_follows_the_extended_field(self):
        cfg = support.config({"collect": {"fields": ["title", "user_reviews_extended"]}})
        self.assertEqual(cfg.fetch_flags(), ["-extra-reviews"])


class FingerprintTest(unittest.TestCase):
    def test_same_config_gives_the_same_fingerprint(self):
        self.assertEqual(support.config().fingerprint(), support.config().fingerprint())

    def test_collection_change_changes_the_fingerprint(self):
        base = support.config()
        changed = support.config({"collect": {"max_depth": 1}, "target_count": 51})
        self.assertNotEqual(base.fingerprint(), changed.fingerprint())

    def test_export_and_output_changes_keep_the_fingerprint(self):
        base = support.config()
        changed = support.config(
            {
                "export": {"formats": ["jsonl"], "directory": "elsewhere"},
                "output": {"directory": "other-runs", "database": "other.sqlite"},
            }
        )
        self.assertEqual(base.fingerprint(), changed.fingerprint())

    def test_fingerprint_is_stable_across_key_order(self):
        first = support.config()
        reordered = dict(reversed(list(support.BASE_CONFIG.items())))
        second = support.config(reordered)
        self.assertEqual(first.fingerprint(), second.fingerprint())


class FileLoadingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)

    def test_yaml_file_loads(self):
        path = self.tmp / "c.yaml"
        path.write_text(
            "industry: dentists\n"
            "location: {query: 'dentists in Berlin', country: DE, geo: [52.52, 13.405]}\n"
            "target_count: 5\n"
            "sources: [google_maps]\n"
            "collect: {mode: fast, fields: [title, address]}\n"
            "export: {fields: [entity_id, title]}\n",
            encoding="utf-8",
        )
        cfg = config_module.load(path)
        self.assertEqual(cfg.target_count, 5)
        self.assertEqual(cfg.collect_fields, ["title", "address"])

    def test_toml_file_loads(self):
        path = self.tmp / "c.toml"
        path.write_text(
            'industry = "dentists"\n'
            "target_count = 5\n"
            'sources = ["google_maps"]\n'
            "[location]\n"
            'query = "dentists in Berlin"\n'
            'country = "DE"\n'
            "geo = [52.52, 13.405]\n"
            "[collect]\n"
            'mode = "fast"\n'
            'fields = ["title", "address"]\n'
            "[export]\n"
            'fields = ["entity_id", "title"]\n',
            encoding="utf-8",
        )
        cfg = config_module.load(path)
        self.assertEqual(cfg.industry, "dentists")

    def test_json_file_loads(self):
        path = self.tmp / "c.json"
        path.write_text(json.dumps(support.BASE_CONFIG), encoding="utf-8")
        cfg = config_module.load(path)
        self.assertEqual(cfg.query, "dentists in Berlin")

    def test_missing_file_is_a_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            config_module.load(self.tmp / "nope.yaml")
        self.assertIn("config file not found", str(ctx.exception))

    def test_unsupported_extension_is_rejected(self):
        path = self.tmp / "c.ini"
        path.write_text("industry=dentists\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            config_module.load(path)
        self.assertIn("unsupported config extension", str(ctx.exception))

    def test_broken_yaml_is_reported_as_a_config_error(self):
        path = self.tmp / "c.yaml"
        path.write_text("industry: [unclosed\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            config_module.load(path)
        self.assertIn("could not parse", str(ctx.exception))

    def test_shipped_example_configs_are_valid(self):
        configs = pathlib.Path(support.COLLECTOR_DIR) / "configs"
        for path in sorted(configs.glob("*.yaml")):
            with self.subTest(path=path.name):
                cfg = config_module.load(path)
                self.assertTrue(cfg.industry)


class RoundTripTest(unittest.TestCase):
    """validate() output must be loadable again (stage F writes configs back)."""

    def base(self):
        import copy

        return copy.deepcopy(support.BASE_CONFIG)

    def test_the_normalised_form_validates_again(self):
        normalised = config_module.validate(self.base(), source="<test>")
        again = config_module.validate(normalised, source="<test>")
        self.assertEqual(again["location"]["queries"], ["dentists in Berlin"])
        self.assertEqual(again["fingerprint"] if "fingerprint" in again else again,
                         normalised)

    def test_multi_query_plus_query_still_rejects_a_disagreement(self):
        raw = self.base()
        raw["location"]["queries"] = ["dentists in Hamburg"]
        with self.assertRaises(ConfigError):
            config_module.validate(raw, source="<test>")


if __name__ == "__main__":
    unittest.main()
