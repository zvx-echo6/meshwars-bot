"""Integration tests for the per-destination send window: the bot polls the
feed and decides for itself when to relay, holding anything that arrives
outside a destination's configured window in a durable pending queue (see
meshwars_bot/schedule.py and the pending-queue additions to state.py/main.py).

Every test here runs entirely against FakeFeedClient/FakeSink (or, for the
dry_run-guarantee test, the real make_sink() with dry_run=True and no
network client at all) -- nothing here opens a socket or transmits.
"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot.config import Config, Destination, FeedConfig
from meshwars_bot.feed import Announcement, FeedPage
from meshwars_bot.main import SinkCache, run_once
from meshwars_bot.state import State, save_state, load_state


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
        send_after=None,
        send_before=None,
        timezone=None,
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
    os.remove(path)
    return path


def make_config(destinations, state_path=None) -> Config:
    return Config(
        feed=FeedConfig(base_url="https://meshwars.example", api_key=""),
        state_path=state_path or _temp_state_path(),
        destinations=destinations,
    )


def _utc(hour, minute, day=15):
    return datetime.datetime(2026, 6, day, hour, minute, tzinfo=datetime.timezone.utc)


class FakeFeedClient:
    """Same shape as test_main.py's fake -- one canned page (or a queue of
    them) per text_budget."""

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

    def close(self) -> None:
        pass


class SendWindowTestCase(unittest.TestCase):
    """Patches make_sink with an in-memory fake, same pattern as
    test_main.py's RunOnceTestCase, so nothing here ever touches
    DryRunSink's filesystem/stdout side effects."""

    def setUp(self):
        self.sinks = {}
        patcher = mock.patch("meshwars_bot.main.make_sink", side_effect=self._fake_make_sink)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _fake_make_sink(self, destination):
        sink = FakeSink(destination.name)
        self.sinks[destination.name] = sink
        return sink


class TestNoWindowRelaysImmediately(SendWindowTestCase):
    def test_no_window_configured_sends_in_the_same_cycle(self):
        dest = make_destination(name="a")  # send_after/send_before/timezone all None
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, text="daily recap")
        client = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )

        run_once(config, state, client, now=_utc(3, 0))  # 3am -- irrelevant, no window

        self.assertEqual(self.sinks["a"].sent, ["daily recap"])
        self.assertTrue(state.has_sent("a", 1))
        self.assertEqual(state.pending, {})


class TestHeldOutsideWindow(SendWindowTestCase):
    def test_announcement_outside_window_is_held_not_sent(self):
        dest = make_destination(name="a", send_after="08:00", send_before="22:00", timezone="UTC")
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, text="daily recap")
        client = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )

        run_once(config, state, client, now=_utc(3, 0))  # 3am -- outside 08:00-22:00

        self.assertEqual(self.sinks["a"].sent, [])
        self.assertFalse(state.has_sent("a", 1))
        self.assertEqual([p.id for p in state.pending["a"]], [1])
        # The cursor still advances -- the feed will never hand this
        # announcement back; only the pending queue remembers it now.
        self.assertEqual(state.since, 200)

    def test_relayed_on_the_first_poll_inside_the_window(self):
        dest = make_destination(name="a", send_after="08:00", send_before="22:00", timezone="UTC")
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, text="daily recap")

        client_cycle1 = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, state, client_cycle1, now=_utc(3, 0))
        self.assertEqual(self.sinks["a"].sent, [])

        # Next poll, nothing new from the feed, but now inside the window.
        client_cycle2 = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, state, client_cycle2, now=_utc(9, 0))

        self.assertEqual(self.sinks["a"].sent, ["daily recap"])
        self.assertTrue(state.has_sent("a", 1))
        self.assertEqual(state.pending, {})


class TestDrainOrdering(SendWindowTestCase):
    def test_pending_drains_oldest_id_first_and_before_newly_fetched(self):
        dest = make_destination(name="a", send_after="08:00", send_before="22:00", timezone="UTC")
        config = make_config([dest])
        state = State(since=100)

        held = make_announcement(id=1, text="held-first")
        client_cycle1 = FakeFeedClient(
            {150: FeedPage(announcements=[held], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, state, client_cycle1, now=_utc(3, 0))  # outside window: just queued
        self.assertEqual(self.sinks["a"].sent, [])

        # Now inside the window, AND the feed hands back a brand new,
        # higher-id announcement in the same cycle the backlog drains.
        fresh = make_announcement(id=2, text="new-second")
        client_cycle2 = FakeFeedClient(
            {150: FeedPage(announcements=[fresh], next_since=300, poll_interval_seconds=900)}
        )
        run_once(config, state, client_cycle2, now=_utc(9, 0))

        # Oldest id (the held one) went out first, the newly fetched one
        # second -- never the other way around.
        self.assertEqual(self.sinks["a"].sent, ["held-first", "new-second"])
        self.assertTrue(state.has_sent("a", 1))
        self.assertTrue(state.has_sent("a", 2))
        self.assertEqual(state.pending, {})


class TestWrapsMidnightIntegration(SendWindowTestCase):
    def test_wrapping_window_holds_and_releases_correctly(self):
        dest = make_destination(name="a", send_after="22:00", send_before="06:00", timezone="UTC")
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, text="late report")
        client = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )

        # Midday -- outside a window that wraps midnight.
        run_once(config, state, client, now=_utc(12, 0))
        self.assertEqual(self.sinks["a"].sent, [])

        client2 = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        # 23:30 -- inside the wrapped window.
        run_once(config, state, client2, now=_utc(23, 30))
        self.assertEqual(self.sinks["a"].sent, ["late report"])


class TestRestartDurability(SendWindowTestCase):
    def test_pending_items_survive_a_simulated_restart(self):
        dest = make_destination(name="a", send_after="08:00", send_before="22:00", timezone="UTC")
        state_path = _temp_state_path()
        config = make_config([dest], state_path=state_path)
        state = State(since=100)
        ann = make_announcement(id=1, text="held across restart")
        client = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )

        run_once(config, state, client, now=_utc(3, 0))  # outside window; persists to disk
        self.assertEqual(self.sinks["a"].sent, [])

        # Simulate a process restart: throw away the in-memory State and
        # load a fresh one from disk, exactly as main() does on startup.
        reloaded_state = load_state(state_path)
        self.assertEqual([p.id for p in reloaded_state.pending["a"]], [1])

        # Fresh sink cache too (a real restart rebuilds everything).
        self.sinks = {}
        client2 = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, reloaded_state, client2, now=_utc(9, 0))

        self.assertEqual(self.sinks["a"].sent, ["held across restart"])
        self.assertTrue(reloaded_state.has_sent("a", 1))


class TestNeverSentTwice(SendWindowTestCase):
    def test_draining_an_already_empty_pending_queue_sends_nothing_again(self):
        dest = make_destination(name="a", send_after="08:00", send_before="22:00", timezone="UTC")
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, text="daily recap")
        cache = SinkCache()  # shared across both cycles, like the real poll loop

        run_once(
            config,
            state,
            FakeFeedClient({150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}),
            sinks_cache=cache,
            now=_utc(9, 0),  # inside window: sent immediately
        )
        self.assertEqual(self.sinks["a"].sent, ["daily recap"])

        # A later poll, still inside the window, with nothing new.
        run_once(
            config,
            state,
            FakeFeedClient({150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}),
            sinks_cache=cache,
            now=_utc(10, 0),
        )
        self.assertEqual(self.sinks["a"].sent, ["daily recap"])  # unchanged

    def test_feed_handing_back_an_already_sent_id_is_never_requeued_or_resent(self):
        # Simulates a buggy/overlapping feed response repeating an id the
        # bot already delivered -- should_relay()'s state.has_sent() guard
        # must stop it before it's ever queued again.
        dest = make_destination(name="a")  # no window: sends immediately
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, text="daily recap")
        cache = SinkCache()  # shared across both cycles, like the real poll loop

        run_once(
            config,
            state,
            FakeFeedClient({150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}),
            sinks_cache=cache,
            now=_utc(9, 0),
        )
        self.assertEqual(self.sinks["a"].sent, ["daily recap"])

        # Same announcement id reappears in a later response.
        run_once(
            config,
            state,
            FakeFeedClient({150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}),
            sinks_cache=cache,
            now=_utc(9, 5),
        )
        self.assertEqual(self.sinks["a"].sent, ["daily recap"])  # still just one
        self.assertEqual(state.pending, {})


class TestPendingCap(SendWindowTestCase):
    def test_cap_drops_the_oldest_and_logs_a_warning(self):
        dest = make_destination(name="a", send_after="08:00", send_before="22:00", timezone="UTC")
        config = make_config([dest])
        state = State(since=100)

        # One cycle, well over the default cap (50), all held (outside the
        # window) so nothing drains and every one is queued.
        anns = [make_announcement(id=i, text=f"ann-{i}") for i in range(1, 61)]
        client = FakeFeedClient(
            {150: FeedPage(announcements=anns, next_since=200, poll_interval_seconds=900)}
        )

        with self.assertLogs("meshwars_bot.state", level="WARNING") as ctx:
            run_once(config, state, client, now=_utc(3, 0))

        self.assertTrue(any("cap" in line.lower() for line in ctx.output))
        pending_ids = [p.id for p in state.pending["a"]]
        self.assertEqual(len(pending_ids), 50)
        # The oldest (lowest ids) were dropped; the newest 50 survive.
        self.assertEqual(pending_ids, list(range(11, 61)))
        self.assertEqual(self.sinks["a"].sent, [])


class TestDryRunGuaranteeUnderSendWindow(unittest.TestCase):
    """The send-window feature must never be the thing that causes a real
    transport library to get imported for a dry_run destination -- same
    guarantee tests/test_main.py's TestSinkCachePreservesDryRunGuarantee
    checks for make_sink()/SinkCache directly, exercised here end-to-end
    through run_once() with an actual hold-then-drain cycle."""

    def test_hold_then_drain_never_imports_a_real_radio_library(self):
        from meshwars_bot.sinks import DryRunSink

        dest = make_destination(
            name="a",
            transport="meshcore",  # a REAL transport name, but dry_run wins
            dry_run=True,
            send_after="08:00",
            send_before="22:00",
            timezone="UTC",
        )
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, text="daily recap")
        cache = SinkCache()

        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
        attempted = []

        def spying_import(name, *args, **kwargs):
            if name in ("meshtastic", "meshtastic.tcp_interface", "meshcore"):
                attempted.append(name)
            return real_import(name, *args, **kwargs)

        # DryRunSink.send() really does write a line to disk (never a
        # socket) -- its log_path defaults to sinks.DEFAULT_DRYRUN_LOG_PATH,
        # which is THIS REPO'S REAL, LIVE dry-run log (config.yaml here is
        # the operator's in-use config -- see this task's constraints, and
        # the .gitignore entry for meshwars-bot-dryrun.log). That default
        # can't be redirected per-instance from here (make_sink() never
        # passes log_path, and it's bound at __init__ definition time, not
        # read from the module constant on each call), so instead this
        # stubs DryRunSink.send() itself -- the class under test stays the
        # REAL DryRunSink (the isinstance check below is still meaningful),
        # it just never touches any file.
        sent_texts = []

        def fake_send(self, text):
            sent_texts.append(text)
            return True

        with mock.patch("builtins.__import__", side_effect=spying_import), mock.patch.object(
            DryRunSink, "send", fake_send
        ):
            # Cycle 1: outside window -- held, never sent.
            client1 = FakeFeedClient(
                {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
            )
            run_once(config, state, client1, sinks_cache=cache, now=_utc(3, 0))
            self.assertEqual(sent_texts, [])

            # Cycle 2: inside window -- drains.
            client2 = FakeFeedClient(
                {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
            )
            run_once(config, state, client2, sinks_cache=cache, now=_utc(9, 0))

        self.assertEqual(attempted, [], "dry_run=True must never import a radio library")
        self.assertIsInstance(cache._sinks["a"], DryRunSink)
        self.assertEqual(sent_texts, ["daily recap"])
        self.assertTrue(state.has_sent("a", 1))


if __name__ == "__main__":
    unittest.main()
