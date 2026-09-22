import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot.config import parse_yaml_subset
from meshwars_bot.configwrite import ConfigWriteError, write_config, write_config_text

FULL_RAW = {
    "feed": {"base_url": "https://meshwars.com", "api_key": "", "timeout_seconds": 20},
    "state_path": "./meshwars-bot-state.json",
    "web": {"enabled": False, "bind_host": "0.0.0.0", "bind_port": 8471},
    "destinations": [
        {
            "name": "mwmesh-mc",
            "transport": "meshcore",
            "host": "192.168.1.100",
            "port": 5000,
            "channel": "#meshwars",
            "board": "mc",
            "dry_run": True,
            "text_budget": 150,
            "kinds": ["daily_recap", "weekly_recap", "month_honors", "net_wrapup"],
            "net_ids": [1],
        },
        {
            "name": "mwmesh-mt",
            "transport": "meshtastic",
            "host": "192.168.1.101",
            "port": 5001,
            "channel": "LongFast",
            "board": "mt",
            "dry_run": False,
            "text_budget": 237,
            "kinds": ["daily_recap"],
            "net_ids": [],
        },
    ],
}


class TestRoundTrip(unittest.TestCase):
    """The correctness bar per the task: parse(write(parse(x))) == parse(x)."""

    def _assert_round_trips(self, raw):
        text = write_config_text(raw)
        reparsed = parse_yaml_subset(text)
        self.assertEqual(reparsed, raw)

    def test_full_config_round_trips(self):
        self._assert_round_trips(FULL_RAW)

    def test_example_yaml_round_trips(self):
        example_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml")
        with open(example_path, "r", encoding="utf-8") as f:
            raw = parse_yaml_subset(f.read())
        self._assert_round_trips(raw)

    def test_only_present_keys_are_written_no_defaults_invented(self):
        # A destination missing optional keys entirely (not null -- ABSENT)
        # must come back out exactly as absent, not filled with defaults.
        raw = {
            "feed": {"base_url": "https://x.example"},
            "destinations": [{"name": "d1", "dry_run": True}],
        }
        self._assert_round_trips(raw)
        text = write_config_text(raw)
        # The legend above `destinations:` mentions every field name in
        # prose, so check for an actual emitted FIELD LINE, not a mention
        # anywhere in the text.
        self.assertNotIn("    text_budget:", text)
        self.assertNotIn("    net_ids:", text)

    def test_explicit_none_values_round_trip_as_none(self):
        raw = {
            "destinations": [
                {"name": "d1", "host": None, "port": None, "channel": None}
            ]
        }
        self._assert_round_trips(raw)

    def test_empty_destinations_list_round_trips(self):
        self._assert_round_trips({"destinations": []})

    def test_empty_kinds_and_net_ids_round_trip(self):
        raw = {"destinations": [{"name": "d1", "kinds": [], "net_ids": []}]}
        self._assert_round_trips(raw)

    def test_multiple_destinations_round_trip(self):
        raw = {"destinations": [{"name": f"d{i}", "port": i} for i in range(5)]}
        self._assert_round_trips(raw)

    def test_unknown_top_level_key_is_preserved(self):
        raw = {"feed": {"base_url": "https://x.example"}, "some_future_field": "kept"}
        self._assert_round_trips(raw)

    def test_unknown_destination_key_is_preserved(self):
        raw = {"destinations": [{"name": "d1", "some_future_field": 42}]}
        self._assert_round_trips(raw)

    def test_values_with_hash_round_trip(self):
        raw = {"feed": {"api_key": "abc#def"}}
        self._assert_round_trips(raw)

    def test_boolean_and_int_scalars_round_trip(self):
        raw = {
            "destinations": [
                {"name": "d1", "dry_run": False, "port": 0, "text_budget": 1000}
            ]
        }
        self._assert_round_trips(raw)

    def test_send_window_fields_round_trip(self):
        raw = {
            "destinations": [
                {
                    "name": "d1",
                    "send_after": "08:00",
                    "send_before": "22:00",
                    "timezone": "America/Boise",
                }
            ]
        }
        self._assert_round_trips(raw)

    def test_send_window_fields_absent_stay_absent(self):
        raw = {"destinations": [{"name": "d1", "dry_run": True}]}
        text = write_config_text(raw)
        self.assertNotIn("    send_after:", text)
        self.assertNotIn("    send_before:", text)
        self.assertNotIn("    timezone:", text)

    def test_schedule_field_round_trips(self):
        raw = {
            "destinations": [
                {
                    "name": "d1",
                    "timezone": "America/Boise",
                    "schedule": {
                        "daily_recap": "09:00",
                        "weekly_recap": "10:00",
                        "month_honors": "10:00",
                    },
                }
            ]
        }
        self._assert_round_trips(raw)

    def test_schedule_field_absent_stays_absent(self):
        raw = {"destinations": [{"name": "d1", "dry_run": True}]}
        text = write_config_text(raw)
        self.assertNotIn("    schedule:", text)

    def test_schedule_alongside_send_window_round_trips(self):
        raw = {
            "destinations": [
                {
                    "name": "d1",
                    "send_after": "08:00",
                    "send_before": "22:00",
                    "timezone": "America/Boise",
                    "schedule": {"daily_recap": "09:00"},
                }
            ]
        }
        self._assert_round_trips(raw)

    def test_full_config_with_multiple_destinations_and_schedules_round_trips(self):
        raw = {
            "feed": {"base_url": "https://meshwars.com", "api_key": "", "timeout_seconds": 20},
            "destinations": [
                {
                    "name": "mwmesh-mc",
                    "transport": "meshcore",
                    "board": "mc",
                    "kinds": ["daily_recap", "net_wrapup", "season_close"],
                    "net_ids": [1],
                    "timezone": "America/Boise",
                    "schedule": {"daily_recap": "09:00", "season_close": "11:30"},
                },
                {
                    "name": "mwmesh-mt",
                    "transport": "meshtastic",
                    "board": "mt",
                    "kinds": ["daily_recap"],
                    "net_ids": [],
                },
            ],
        }
        self._assert_round_trips(raw)


class TestEmptyScheduleMapping(unittest.TestCase):
    """Same reasoning as TestEmptyMappingSections -- an empty `schedule`
    mapping cannot round-trip in this YAML subset (no flow-mapping
    support), so it must fail loudly rather than silently write something
    that reads back as None instead of {}."""

    def test_empty_schedule_dict_raises(self):
        with self.assertRaises(ConfigWriteError):
            write_config_text({"destinations": [{"name": "d1", "schedule": {}}]})


class TestUnsafeValues(unittest.TestCase):
    def test_double_quote_in_value_raises(self):
        with self.assertRaises(ConfigWriteError):
            write_config_text({"feed": {"base_url": 'https://x.example/"quoted"'}})

    def test_backslash_in_value_raises(self):
        with self.assertRaises(ConfigWriteError):
            write_config_text({"feed": {"base_url": "https://x.example\\bad"}})


class TestEmptyMappingSections(unittest.TestCase):
    """parse_yaml_subset() can never actually produce an empty-but-present
    dict for a mapping key (a blank `key:` parses as None, and flow
    mappings aren't supported at all) -- so this can only be hit from a
    hand-built raw dict, and there's no way to write it that round-trips.
    It must fail loudly rather than silently write something that reads
    back differently."""

    def test_empty_feed_dict_raises(self):
        with self.assertRaises(ConfigWriteError):
            write_config_text({"feed": {}})

    def test_empty_web_dict_raises(self):
        with self.assertRaises(ConfigWriteError):
            write_config_text({"web": {}})


class TestWriteConfigFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "config.yaml")

    def test_writes_a_file_that_parses_back(self):
        write_config(FULL_RAW, self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            reparsed = parse_yaml_subset(f.read())
        self.assertEqual(reparsed, FULL_RAW)

    def test_write_is_atomic_no_leftover_temp_files(self):
        write_config(FULL_RAW, self.path)
        leftovers = [f for f in os.listdir(self.tmpdir.name) if f != "config.yaml"]
        self.assertEqual(leftovers, [])

    def test_creates_missing_parent_directory(self):
        nested = os.path.join(self.tmpdir.name, "nested", "dir", "config.yaml")
        write_config(FULL_RAW, nested)
        self.assertTrue(os.path.exists(nested))

    def test_original_file_untouched_on_write_failure(self):
        write_config(FULL_RAW, self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            original = f.read()

        with self.assertRaises(ConfigWriteError):
            write_config({"feed": {}}, self.path)

        with open(self.path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), original)
        leftovers = [f for f in os.listdir(self.tmpdir.name) if f != "config.yaml"]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
