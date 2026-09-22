import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot.config import Config, Destination, FeedConfig
from meshwars_bot.feed import Announcement, FeedPage
from meshwars_bot.main import run_once
from meshwars_bot.state import State


def make_destination(**overrides) -> Destination:
    defaults = dict(
        name="dest",
        transport="dryrun",
        host=None,
        port=None,
        channel=None,
        board="mc",
        dry_run=True,
        text_budget=150,
        kinds=["daily_recap", "net_wrapup"],
        net_ids=[1, 2],
    )
    defaults.update(overrides)
    return Destination(**defaults)


def make_announcement(**overrides) -> Announcement:
    defaults = dict(
        id=1,
        kind="daily_recap",
        key="k1",
        board="mc",
        net_id=None,
        created_at="2026-09-21T00:00:00Z",
        content={},
        text="hello",
    )
    defaults.update(overrides)
    return Announcement(**defaults)


def _temp_state_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)  # run_once/save_state must be able to create it fresh
    return path


def make_config(destinations, api_key="", state_path=None) -> Config:
    return Config(
        feed=FeedConfig(base_url="https://meshwars.example", api_key=api_key),
        state_path=state_path or _temp_state_path(),
        destinations=destinations,
    )


class FakeFeedClient:
    """Records every poll() call and returns a canned page per text_budget.

    `pages_by_budget` maps budget -> a single FeedPage/None (reused for
    every call at that budget) OR a list of FeedPage/None consumed one per
    call, in order (for tests that need different results across cycles).
    """

    def __init__(self, pages_by_budget):
        self._pages_by_budget = pages_by_budget
        self.calls = []

    def poll(self, since=None, etag=None, text_budget=150, **kwargs):
        self.calls.append({"since": since, "etag": etag, "text_budget": text_budget})
        entry = self._pages_by_budget.get(text_budget)
        if isinstance(entry, list):
            return entry.pop(0)
        return entry


class FakeSink:
    def __init__(self, destination_name):
        self.destination_name = destination_name
        self.sent = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


class RunOnceTestCase(unittest.TestCase):
    """Base class that patches make_sink with an in-memory fake so run_once
    never touches the filesystem or stdout via DryRunSink."""

    def setUp(self):
        self.sinks = {}
        patcher = self._patch_make_sink()
        self.addCleanup(patcher.stop)
        patcher.start()

    def _patch_make_sink(self):
        from unittest import mock

        def fake_make_sink(destination):
            sink = FakeSink(destination.name)
            self.sinks[destination.name] = sink
            return sink

        return mock.patch("meshwars_bot.main.make_sink", side_effect=fake_make_sink)


class TestPerDestinationBudget(RunOnceTestCase):
    def test_two_destinations_with_different_budgets_each_get_their_own_fitted_text(self):
        dest_small = make_destination(name="small", text_budget=150)
        dest_large = make_destination(name="large", text_budget=237)
        config = make_config([dest_small, dest_large])
        state = State(since=100, etag=None)

        ann_small = make_announcement(id=1, text="short text fitted to 150")
        ann_large = make_announcement(id=1, text="a longer text fitted all the way out to 237 bytes of budget")

        client = FakeFeedClient(
            {
                150: FeedPage(announcements=[ann_small], next_since=200, poll_interval_seconds=900, etag="e150"),
                237: FeedPage(announcements=[ann_large], next_since=200, poll_interval_seconds=900, etag="e237"),
            }
        )

        run_once(config, state, client)

        self.assertEqual(self.sinks["small"].sent, ["short text fitted to 150"])
        self.assertEqual(
            self.sinks["large"].sent,
            ["a longer text fitted all the way out to 237 bytes of budget"],
        )

    def test_number_of_feed_requests_per_cycle_equals_distinct_budgets(self):
        destinations = [
            make_destination(name="a", text_budget=150),
            make_destination(name="b", text_budget=150),
            make_destination(name="c", text_budget=237),
        ]
        config = make_config(destinations)
        state = State(since=100, etag=None)

        client = FakeFeedClient(
            {
                150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900),
                237: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900),
            }
        )

        run_once(config, state, client)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual({c["text_budget"] for c in client.calls}, {150, 237})

    def test_single_shared_budget_costs_one_request(self):
        destinations = [
            make_destination(name="a", text_budget=150),
            make_destination(name="b", text_budget=150),
        ]
        config = make_config(destinations)
        state = State(since=100, etag=None)
        client = FakeFeedClient({150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)})

        run_once(config, state, client)

        self.assertEqual(len(client.calls), 1)


class TestOverBudgetGuard(RunOnceTestCase):
    def test_over_budget_text_is_dropped_not_sent(self):
        dest = make_destination(name="tight", text_budget=20)
        config = make_config([dest])
        state = State(since=100, etag=None)

        # Simulate a server mismatch: text far exceeds the destination's
        # 20-byte budget.
        ann = make_announcement(id=1, text="this text is way over the twenty byte budget")
        client = FakeFeedClient(
            {20: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )

        run_once(config, state, client)

        self.assertEqual(self.sinks["tight"].sent, [])
        self.assertFalse(state.has_sent("tight", 1))

    def test_within_budget_text_is_sent(self):
        dest = make_destination(name="tight", text_budget=20)
        config = make_config([dest])
        state = State(since=100, etag=None)

        ann = make_announcement(id=1, text="fits fine")
        client = FakeFeedClient(
            {20: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )

        run_once(config, state, client)

        self.assertEqual(self.sinks["tight"].sent, ["fits fine"])
        self.assertTrue(state.has_sent("tight", 1))


class TestCursorOrderingAcrossBudgetGroups(RunOnceTestCase):
    def test_cursor_does_not_advance_when_one_budget_group_fails(self):
        dest_a = make_destination(name="a", text_budget=150)
        dest_b = make_destination(name="b", text_budget=237)
        config = make_config([dest_a, dest_b])
        state = State(since=100, etag=None)

        ann = make_announcement(id=7, text="net result")

        # Group "a" (budget 150) fails outright this cycle; group "b"
        # (budget 237) succeeds and sees the announcement.
        client_cycle1 = FakeFeedClient(
            {
                150: None,
                237: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900, etag="e-b1"),
            }
        )

        run_once(config, state, client_cycle1)

        # The shared cursor must NOT have advanced -- group "a" never got
        # a chance to see anything at since=100 yet.
        self.assertEqual(state.since, 100)
        # But the group that DID succeed already delivered and recorded it.
        self.assertTrue(state.has_sent("b", 7))
        self.assertFalse(state.has_sent("a", 7))
        self.assertEqual(self.sinks["b"].sent, ["net result"])
        self.assertEqual(self.sinks["a"].sent, [])
        sink_b_cycle1 = self.sinks["b"]

        # Cycle 2: both groups now succeed, still polling from since=100
        # (unchanged), and the server (correctly) hands the same
        # announcement back since group "a" never advanced past it.
        client_cycle2 = FakeFeedClient(
            {
                150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900, etag="e-a2"),
                237: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900, etag="e-b2"),
            }
        )

        run_once(config, state, client_cycle2)

        # Group "a" never skipped the announcement -- it's delivered here.
        self.assertTrue(state.has_sent("a", 7))
        self.assertEqual(self.sinks["a"].sent, ["net result"])
        # Group "b" does not re-send -- already marked sent in cycle 1, and
        # its cycle-1 sink (captured above) still shows exactly one send.
        self.assertEqual(self.sinks["b"].sent, [])
        self.assertEqual(sink_b_cycle1.sent, ["net result"])
        # Both groups succeeded this cycle, so the cursor now advances.
        self.assertEqual(state.since, 200)

        for call in client_cycle2.calls:
            self.assertEqual(call["since"], 100)

    def test_cursor_advances_when_all_budget_groups_succeed(self):
        dest_a = make_destination(name="a", text_budget=150)
        dest_b = make_destination(name="b", text_budget=237)
        config = make_config([dest_a, dest_b])
        state = State(since=100, etag=None)

        client = FakeFeedClient(
            {
                150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900),
                237: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900),
            }
        )

        run_once(config, state, client)

        self.assertEqual(state.since, 200)


if __name__ == "__main__":
    unittest.main()
