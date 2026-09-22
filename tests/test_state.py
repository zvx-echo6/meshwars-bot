import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot.state import (
    State,
    fast_forward_on_first_run,
    load_state,
    save_state,
)


class TestState(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmpdir.name, "state.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_load_state_returns_none_when_no_file(self):
        self.assertIsNone(load_state(self.state_path))

    def test_save_and_load_roundtrip(self):
        state = State(since=42, etag='"abc123"', sent={("mwmesh-mc", 1), ("mwmesh-mc", 2)})
        save_state(self.state_path, state)
        loaded = load_state(self.state_path)
        self.assertEqual(loaded.since, 42)
        self.assertEqual(loaded.etag, '"abc123"')
        self.assertEqual(loaded.sent, {("mwmesh-mc", 1), ("mwmesh-mc", 2)})

    def test_cursor_advances_across_saves(self):
        state = State(since=1)
        save_state(self.state_path, state)
        state.since = 2
        save_state(self.state_path, state)
        loaded = load_state(self.state_path)
        self.assertEqual(loaded.since, 2)

    def test_has_sent_and_mark_sent(self):
        state = State()
        self.assertFalse(state.has_sent("d1", 7))
        state.mark_sent("d1", 7)
        self.assertTrue(state.has_sent("d1", 7))
        self.assertFalse(state.has_sent("d2", 7))

    def test_fast_forward_on_first_run_sends_nothing_and_persists_cursor(self):
        self.assertFalse(os.path.exists(self.state_path))
        state = fast_forward_on_first_run(self.state_path, current_next_since=999)
        self.assertEqual(state.since, 999)
        self.assertEqual(state.sent, set())
        self.assertIsNone(state.etag)

        # Persisted to disk, and re-loading gives the same fast-forwarded state.
        self.assertTrue(os.path.exists(self.state_path))
        reloaded = load_state(self.state_path)
        self.assertEqual(reloaded.since, 999)
        self.assertEqual(reloaded.sent, set())

    def test_no_tmp_files_left_behind_after_save(self):
        save_state(self.state_path, State(since=1))
        leftovers = [f for f in os.listdir(self.tmpdir.name) if f != "state.json"]
        self.assertEqual(leftovers, [])

    def test_save_is_atomic_original_survives_failed_write(self):
        # Write a good state first.
        good_state = State(since=1, etag="e1", sent={("d1", 1)})
        save_state(self.state_path, good_state)
        with open(self.state_path, "r", encoding="utf-8") as f:
            original_bytes = f.read()

        # Now simulate a crash mid-write: json.dump raises partway through.
        import meshwars_bot.state as state_mod

        real_dump = state_mod.json.dump

        def exploding_dump(*args, **kwargs):
            raise RuntimeError("simulated crash mid-write")

        state_mod.json.dump = exploding_dump
        try:
            with self.assertRaises(RuntimeError):
                save_state(self.state_path, State(since=2))
        finally:
            state_mod.json.dump = real_dump

        # The original file must be untouched, and no leftover temp file.
        with open(self.state_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), original_bytes)
        leftovers = [f for f in os.listdir(self.tmpdir.name) if f != "state.json"]
        self.assertEqual(leftovers, [])

    def test_load_state_tolerates_missing_optional_fields(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"since": 5}, f)
        loaded = load_state(self.state_path)
        self.assertEqual(loaded.since, 5)
        self.assertIsNone(loaded.etag)
        self.assertEqual(loaded.sent, set())


if __name__ == "__main__":
    unittest.main()
