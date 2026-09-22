import logging
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot.config import (
    ConfigError,
    Destination,
    build_config,
    multi_budget_warning_info,
    parse_yaml_subset,
)
from meshwars_bot.sinks import DryRunSink, MeshCoreSink, make_sink

EXAMPLE_YAML = """
feed:
  base_url: "https://meshwars.com"
  api_key: ""
  timeout_seconds: 20

state_path: "./meshwars-bot-state.json"

destinations:
  - name: "mwmesh-mc"
    transport: "meshcore"
    host: "192.168.1.100"
    port: 5000
    channel: "#meshwars"
    board: "mc"
    dry_run: true
    text_budget: 150
    kinds: ["daily_recap", "weekly_recap", "month_honors", "net_wrapup"]
    net_ids: [1]
  - name: "mwmesh-mt"
    transport: "meshtastic"
    host: "192.168.1.101"
    port: 5001
    channel: "LongFast"
    board: "meshtastic"
    text_budget: 150
    kinds: ["daily_recap"]
    net_ids: []
"""


class TestParseYamlSubset(unittest.TestCase):
    def test_parses_nested_maps_lists_and_scalars(self):
        data = parse_yaml_subset(EXAMPLE_YAML)
        self.assertEqual(data["feed"]["base_url"], "https://meshwars.com")
        self.assertEqual(data["feed"]["timeout_seconds"], 20)
        self.assertEqual(data["state_path"], "./meshwars-bot-state.json")
        self.assertEqual(len(data["destinations"]), 2)
        self.assertEqual(data["destinations"][0]["name"], "mwmesh-mc")
        self.assertEqual(data["destinations"][0]["net_ids"], [1])
        self.assertEqual(data["destinations"][1]["net_ids"], [])
        self.assertIs(data["destinations"][0]["dry_run"], True)

    def test_comments_and_blank_lines_ignored(self):
        text = """
        # a top comment
        feed:
          base_url: "https://x.example"  # trailing comment
        """.replace("        ", "")
        data = parse_yaml_subset(text)
        self.assertEqual(data["feed"]["base_url"], "https://x.example")

    def test_hash_inside_quotes_is_not_a_comment(self):
        text = 'feed:\n  base_url: "https://x.example"\n  api_key: "abc#def"\n'
        data = parse_yaml_subset(text)
        self.assertEqual(data["feed"]["api_key"], "abc#def")

    def test_empty_document_returns_empty_mapping(self):
        self.assertEqual(parse_yaml_subset(""), {})
        self.assertEqual(parse_yaml_subset("\n\n  \n"), {})

    def test_tabs_in_indentation_is_an_error(self):
        text = "feed:\n\tbase_url: \"x\"\n"
        with self.assertRaises(ConfigError):
            parse_yaml_subset(text)

    def test_missing_colon_is_an_error(self):
        with self.assertRaises(ConfigError):
            parse_yaml_subset("feed\n  base_url: \"x\"\n")

    def test_flow_mapping_is_unsupported(self):
        with self.assertRaises(ConfigError):
            parse_yaml_subset("feed: {base_url: \"x\"}\n")

    def test_unclosed_inline_list_is_an_error(self):
        with self.assertRaises(ConfigError):
            parse_yaml_subset("kinds: [\"a\", \"b\"\n")

    def test_bare_scalar_list_item_is_unsupported(self):
        with self.assertRaises(ConfigError):
            parse_yaml_subset("things:\n  - just_a_string\n")

    def test_non_mapping_top_level_is_an_error(self):
        with self.assertRaises(ConfigError):
            parse_yaml_subset("- 1\n- 2\n")


class TestBuildConfig(unittest.TestCase):
    def test_builds_valid_config(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        config = build_config(raw)
        self.assertEqual(config.feed.base_url, "https://meshwars.com")
        self.assertEqual(len(config.destinations), 2)
        self.assertEqual(config.destinations[0].name, "mwmesh-mc")
        self.assertEqual(config.destinations[0].board, "mc")
        self.assertEqual(config.destinations[1].board, "mt")  # "meshtastic" normalized

    def test_dry_run_defaults_true_when_absent(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        # second destination in EXAMPLE_YAML has no dry_run key at all
        config = build_config(raw)
        self.assertTrue(config.destinations[1].dry_run)

    def test_dry_run_explicit_false_is_respected(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["dry_run"] = False
        config = build_config(raw)
        self.assertFalse(config.destinations[0].dry_run)

    def test_unknown_transport_raises_naming_destination(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["transport"] = "carrier-pigeon"
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("mwmesh-mc", str(ctx.exception))
        self.assertIn("carrier-pigeon", str(ctx.exception))

    def test_unknown_board_raises_naming_destination(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["board"] = "shortwave"
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("mwmesh-mc", str(ctx.exception))

    def test_unknown_kind_raises_naming_destination(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["kinds"] = ["not_a_real_kind"]
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("mwmesh-mc", str(ctx.exception))
        self.assertIn("not_a_real_kind", str(ctx.exception))

    def test_missing_base_url_raises(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        del raw["feed"]["base_url"]
        with self.assertRaises(ConfigError):
            build_config(raw)

    def test_text_budget_clamps_low(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["text_budget"] = 1
        config = build_config(raw)
        self.assertEqual(config.destinations[0].text_budget, 20)

    def test_text_budget_clamps_high(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["text_budget"] = 5000
        config = build_config(raw)
        self.assertEqual(config.destinations[0].text_budget, 1000)

    def test_text_budget_defaults_when_absent(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        del raw["destinations"][0]["text_budget"]
        config = build_config(raw)
        self.assertEqual(config.destinations[0].text_budget, 150)

    def test_duplicate_destination_names_raise(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][1]["name"] = "mwmesh-mc"
        with self.assertRaises(ConfigError):
            build_config(raw)

    def test_empty_net_ids_is_valid_not_an_error(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        config = build_config(raw)
        self.assertEqual(config.destinations[1].net_ids, [])


class TestSendWindowValidation(unittest.TestCase):
    """send_after / send_before / timezone -- all optional, all absent by
    default (see TestBuildConfig.test_builds_valid_config's destinations,
    neither of which set any of these three)."""

    def test_absent_fields_default_to_none(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        config = build_config(raw)
        self.assertIsNone(config.destinations[0].send_after)
        self.assertIsNone(config.destinations[0].send_before)
        self.assertIsNone(config.destinations[0].timezone)

    def test_valid_send_after_and_send_before_are_kept(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["send_after"] = "08:00"
        raw["destinations"][0]["send_before"] = "22:00"
        config = build_config(raw)
        self.assertEqual(config.destinations[0].send_after, "08:00")
        self.assertEqual(config.destinations[0].send_before, "22:00")

    def test_valid_timezone_is_kept(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["timezone"] = "America/Boise"
        config = build_config(raw)
        self.assertEqual(config.destinations[0].timezone, "America/Boise")

    def test_send_after_wrong_format_raises_naming_destination_and_field(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["send_after"] = "8am"
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("mwmesh-mc", str(ctx.exception))
        self.assertIn("send_after", str(ctx.exception))

    def test_send_before_out_of_range_hour_raises(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["send_before"] = "24:00"
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("send_before", str(ctx.exception))

    def test_send_after_out_of_range_minute_raises(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["send_after"] = "08:60"
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("send_after", str(ctx.exception))

    def test_send_after_not_a_string_raises(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["send_after"] = 800
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("send_after", str(ctx.exception))

    def test_invalid_timezone_raises_naming_destination(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["timezone"] = "Mars/Cydonia"
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("mwmesh-mc", str(ctx.exception))
        self.assertIn("timezone", str(ctx.exception))

    def test_empty_string_timezone_raises(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["timezone"] = ""
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("timezone", str(ctx.exception))


class TestKindScheduleValidation(unittest.TestCase):
    """Per-destination, per-kind `schedule` mapping -- a separate, finer-
    grained mechanism from send_after/send_before (see schedule.py). Every
    entry optional; absent entirely means every kind relays immediately,
    exactly as an existing config with no `schedule` key at all."""

    def test_absent_schedule_defaults_to_empty(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        config = build_config(raw)
        self.assertEqual(config.destinations[0].schedule, {})

    def test_valid_schedule_entries_are_kept(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["schedule"] = {
            "daily_recap": "09:00",
            "weekly_recap": "10:00",
        }
        config = build_config(raw)
        self.assertEqual(
            config.destinations[0].schedule,
            {"daily_recap": "09:00", "weekly_recap": "10:00"},
        )

    def test_empty_string_entry_means_immediate_and_is_dropped(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["schedule"] = {"daily_recap": "09:00", "net_wrapup": ""}
        config = build_config(raw)
        self.assertEqual(config.destinations[0].schedule, {"daily_recap": "09:00"})

    def test_null_entry_means_immediate_and_is_dropped(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["schedule"] = {"daily_recap": "09:00", "net_wrapup": None}
        config = build_config(raw)
        self.assertEqual(config.destinations[0].schedule, {"daily_recap": "09:00"})

    def test_season_close_is_a_known_schedulable_kind(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["schedule"] = {"season_close": "11:00"}
        config = build_config(raw)
        self.assertEqual(config.destinations[0].schedule, {"season_close": "11:00"})

    def test_unknown_kind_raises_naming_destination_and_kind(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["schedule"] = {"not_a_real_kind": "09:00"}
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("mwmesh-mc", str(ctx.exception))
        self.assertIn("not_a_real_kind", str(ctx.exception))

    def test_bad_hhmm_raises_naming_destination_and_kind(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["schedule"] = {"daily_recap": "9am"}
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("mwmesh-mc", str(ctx.exception))
        self.assertIn("daily_recap", str(ctx.exception))

    def test_out_of_range_hhmm_raises_naming_destination_and_kind(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["schedule"] = {"weekly_recap": "24:00"}
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("mwmesh-mc", str(ctx.exception))
        self.assertIn("weekly_recap", str(ctx.exception))

    def test_non_string_value_raises_naming_destination_and_kind(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["schedule"] = {"daily_recap": 900}
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("mwmesh-mc", str(ctx.exception))
        self.assertIn("daily_recap", str(ctx.exception))

    def test_schedule_must_be_a_mapping(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["schedule"] = ["daily_recap"]
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("mwmesh-mc", str(ctx.exception))

    def test_nested_schedule_mapping_parses_inside_a_destination(self):
        # The one nesting exception this repo's hand-rolled YAML subset
        # parser allows inside a sequence item -- see config.py's
        # parse_yaml_subset docstring and _parse_sequence.
        text = """
destinations:
  - name: "d1"
    transport: "dryrun"
    board: "mc"
    kinds: ["daily_recap"]
    net_ids: []
    schedule:
      daily_recap: "09:00"
      weekly_recap: "10:00"
    timezone: "America/Boise"
"""
        raw = parse_yaml_subset(text)
        self.assertEqual(
            raw["destinations"][0]["schedule"],
            {"daily_recap": "09:00", "weekly_recap": "10:00"},
        )
        self.assertEqual(raw["destinations"][0]["timezone"], "America/Boise")


class TestMultiBudgetWarning(unittest.TestCase):
    """BUG 1's rate-limit consequence: config.py must warn (not error) when
    a config has more than one distinct text_budget and no feed.api_key --
    each distinct budget costs one additional feed request per poll cycle,
    which can exceed the keyless feed tier's 6 requests/hour/IP limit."""

    def test_warns_when_multiple_budgets_and_no_api_key(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["text_budget"] = 150
        raw["destinations"][1]["text_budget"] = 237
        raw["feed"]["api_key"] = ""
        with self.assertLogs("meshwars_bot.config", level="WARNING") as ctx:
            build_config(raw)
        joined = "\n".join(ctx.output)
        self.assertIn("150", joined)
        self.assertIn("237", joined)
        self.assertIn("api_key", joined)

    def test_no_warning_when_api_key_is_set(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["text_budget"] = 150
        raw["destinations"][1]["text_budget"] = 237
        raw["feed"]["api_key"] = "secret123"
        logger = logging.getLogger("meshwars_bot.config")
        with mock.patch.object(logger, "warning") as warn:
            build_config(raw)
        warn.assert_not_called()

    def test_no_warning_when_all_budgets_match(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["text_budget"] = 150
        raw["destinations"][1]["text_budget"] = 150
        raw["feed"]["api_key"] = ""
        logger = logging.getLogger("meshwars_bot.config")
        with mock.patch.object(logger, "warning") as warn:
            build_config(raw)
        warn.assert_not_called()


class TestWebConfig(unittest.TestCase):
    """The optional `web` section (the built-in config UI's own
    enabled/bind_host/bind_port), validated the same defensive way as
    every other section -- absent entirely means all defaults."""

    def test_absent_web_section_uses_defaults(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        config = build_config(raw)
        self.assertFalse(config.web.enabled)
        self.assertEqual(config.web.bind_host, "0.0.0.0")
        self.assertEqual(config.web.bind_port, 8471)

    def test_explicit_web_section_is_respected(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["web"] = {"enabled": True, "bind_host": "127.0.0.1", "bind_port": 9000}
        config = build_config(raw)
        self.assertTrue(config.web.enabled)
        self.assertEqual(config.web.bind_host, "127.0.0.1")
        self.assertEqual(config.web.bind_port, 9000)

    def test_web_enabled_must_be_boolean(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["web"] = {"enabled": "yes"}
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("web.enabled", str(ctx.exception))

    def test_web_bind_port_must_be_in_range(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["web"] = {"bind_port": 70000}
        with self.assertRaises(ConfigError) as ctx:
            build_config(raw)
        self.assertIn("web.bind_port", str(ctx.exception))

    def test_web_bind_port_must_be_an_integer(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["web"] = {"bind_port": "not-a-port"}
        with self.assertRaises(ConfigError):
            build_config(raw)

    def test_web_bind_host_must_be_non_empty_string(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["web"] = {"bind_host": ""}
        with self.assertRaises(ConfigError):
            build_config(raw)

    def test_web_section_must_be_a_mapping(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["web"] = "not-a-mapping"
        with self.assertRaises(ConfigError):
            build_config(raw)


class TestMultiBudgetWarningInfo(unittest.TestCase):
    """Pure (no-logging) form of the multi-budget/no-api-key check, used by
    webui.py to surface the same warning in the config page."""

    def test_returns_none_when_condition_does_not_hold(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["text_budget"] = 150
        raw["destinations"][1]["text_budget"] = 150
        config = build_config(raw)
        self.assertIsNone(multi_budget_warning_info(config.feed, config.destinations))

    def test_returns_info_dict_when_condition_holds(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["text_budget"] = 150
        raw["destinations"][1]["text_budget"] = 237
        raw["feed"]["api_key"] = ""
        config = build_config(raw)
        info = multi_budget_warning_info(config.feed, config.destinations)
        self.assertIsNotNone(info)
        self.assertEqual(info["budgets"], [150, 237])
        self.assertEqual(info["requests_per_hour"], 8)

    def test_none_when_api_key_set_even_with_multiple_budgets(self):
        raw = parse_yaml_subset(EXAMPLE_YAML)
        raw["destinations"][0]["text_budget"] = 150
        raw["destinations"][1]["text_budget"] = 237
        raw["feed"]["api_key"] = "secret"
        config = build_config(raw)
        self.assertIsNone(multi_budget_warning_info(config.feed, config.destinations))


class TestMakeSink(unittest.TestCase):
    """make_sink() lives in sinks.py but is exercised here against real
    Destination objects built via config, since its critical behaviour
    (dry_run always wins, checked before anything else) is a config-driven
    safety property. Full coverage of the real sinks themselves (pacing,
    reconnect, transport specifics) lives in test_sinks.py."""

    def _destination(self, dry_run: bool, transport: str = "meshcore") -> Destination:
        return Destination(
            name="d1",
            transport=transport,
            host="127.0.0.1",
            port=1234,
            channel="#x",
            board="mc",
            dry_run=dry_run,
            text_budget=150,
            kinds=["daily_recap"],
            net_ids=[],
        )

    def test_dry_run_true_returns_dry_run_sink(self):
        sink = make_sink(self._destination(dry_run=True))
        self.assertIsInstance(sink, DryRunSink)

    def test_dry_run_false_returns_real_sink_and_never_raises(self):
        # This is the behaviour this repo's later task delivers: make_sink()
        # no longer raises for a real, non-dry-run destination -- it builds
        # the matching real Sink instead (which imports no radio library at
        # construction time; that stays lazy until a real send is attempted).
        sink = make_sink(self._destination(dry_run=False))
        self.assertIsInstance(sink, MeshCoreSink)

    def test_dry_run_false_unknown_transport_still_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            make_sink(self._destination(dry_run=False, transport="dryrun"))


if __name__ == "__main__":
    unittest.main()
