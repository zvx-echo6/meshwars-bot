import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot.state import (
    PendingItem,
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
        self.assertEqual(loaded.pending, {})


class TestPending(unittest.TestCase):
    """State.pending: the durable per-destination held-announcement queue
    that backs the send-window feature."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmpdir.name, "state.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_new_state_has_no_pending(self):
        self.assertEqual(State().pending, {})

    def test_add_pending_queues_item(self):
        state = State()
        state.add_pending("d1", PendingItem(id=1, text="hello", kind="daily_recap"))
        self.assertEqual(len(state.pending["d1"]), 1)
        self.assertEqual(state.pending["d1"][0].id, 1)
        self.assertEqual(state.pending["d1"][0].text, "hello")

    def test_remove_pending_drops_the_item_and_the_key_when_empty(self):
        state = State()
        state.add_pending("d1", PendingItem(id=1, text="hello"))
        state.remove_pending("d1", 1)
        self.assertNotIn("d1", state.pending)

    def test_remove_pending_leaves_other_items_for_same_destination(self):
        state = State()
        state.add_pending("d1", PendingItem(id=1, text="a"))
        state.add_pending("d1", PendingItem(id=2, text="b"))
        state.remove_pending("d1", 1)
        self.assertEqual([p.id for p in state.pending["d1"]], [2])

    def test_remove_pending_is_a_noop_for_unknown_destination_or_id(self):
        state = State()
        state.remove_pending("nope", 1)  # must not raise
        state.add_pending("d1", PendingItem(id=1, text="a"))
        state.remove_pending("d1", 999)  # must not raise, must not drop id=1
        self.assertEqual([p.id for p in state.pending["d1"]], [1])

    def test_pending_is_isolated_per_destination(self):
        state = State()
        state.add_pending("d1", PendingItem(id=1, text="a"))
        self.assertNotIn("d2", state.pending)

    def test_pending_survives_save_and_load_round_trip(self):
        state = State(since=100, etag="e1")
        state.add_pending("d1", PendingItem(id=1, text="held one", kind="daily_recap"))
        state.add_pending("d1", PendingItem(id=2, text="held two", kind="daily_recap"))
        state.add_pending("d2", PendingItem(id=5, text="other dest", kind="weekly_recap"))
        save_state(self.state_path, state)

        loaded = load_state(self.state_path)

        self.assertEqual([p.id for p in loaded.pending["d1"]], [1, 2])
        self.assertEqual(loaded.pending["d1"][0].text, "held one")
        self.assertEqual(loaded.pending["d1"][0].kind, "daily_recap")
        self.assertEqual([p.id for p in loaded.pending["d2"]], [5])

    def test_pending_order_is_preserved_across_a_restart(self):
        state = State()
        for i in (3, 1, 2):
            state.add_pending("d1", PendingItem(id=i, text=f"item {i}"))
        save_state(self.state_path, state)

        loaded = load_state(self.state_path)

        # Insertion order preserved exactly -- ordering-by-id is the
        # caller's job (main.py sorts at drain time); state.py itself just
        # keeps whatever order items were added in.
        self.assertEqual([p.id for p in loaded.pending["d1"]], [3, 1, 2])

    def test_empty_pending_destination_does_not_round_trip_as_empty_list(self):
        # remove_pending() already drops the key once empty; this just
        # confirms an empty dict overall never appears in the written file
        # as spurious per-destination empty lists (defensive against a
        # future direct dict mutation that leaves one behind).
        state = State()
        state.pending["ghost"] = []
        save_state(self.state_path, state)
        loaded = load_state(self.state_path)
        self.assertNotIn("ghost", loaded.pending)

    def test_add_pending_respects_cap_and_drops_oldest(self):
        state = State()
        for i in range(5):
            state.add_pending("d1", PendingItem(id=i, text=f"item {i}"), cap=3)
        self.assertEqual([p.id for p in state.pending["d1"]], [2, 3, 4])

    def test_add_pending_over_cap_logs_a_warning(self):
        state = State()
        for i in range(3):
            state.add_pending("d1", PendingItem(id=i, text=f"item {i}"), cap=3)
        with self.assertLogs("meshwars_bot.state", level="WARNING") as ctx:
            state.add_pending("d1", PendingItem(id=99, text="newest"), cap=3)
        joined = "\n".join(ctx.output)
        self.assertIn("d1", joined)
        self.assertIn("cap", joined.lower())
        # The newest item survives; the oldest (id=0) was the one dropped.
        self.assertEqual([p.id for p in state.pending["d1"]], [1, 2, 99])

    def test_cap_is_per_destination_not_global(self):
        state = State()
        for i in range(3):
            state.add_pending("d1", PendingItem(id=i, text="x"), cap=2)
        for i in range(3):
            state.add_pending("d2", PendingItem(id=i, text="x"), cap=2)
        self.assertEqual(len(state.pending["d1"]), 2)
        self.assertEqual(len(state.pending["d2"]), 2)


if __name__ == "__main__":
    unittest.main()
