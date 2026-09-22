"""Integration tests for per-destination, per-KIND scheduled send times --
a separate, finer-grained mechanism from the coarse send window covered by
test_send_window.py (see meshwars_bot/schedule.py's module docstring and
the schedule-conflict handling in main.py's drain phase).

Every test here runs entirely against FakeFeedClient/FakeSink -- nothing
here opens a socket or transmits. Same pattern as test_send_window.py.
"""

import datetime
import logging
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
        kinds=["daily_recap", "weekly_recap", "net_wrapup"],
        net_ids=[1, 2],
        send_after=None,
        send_before=None,
        timezone="UTC",
        schedule={},
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


class KindScheduleTestCase(unittest.TestCase):
    def setUp(self):
        self.sinks = {}
        patcher = mock.patch("meshwars_bot.main.make_sink", side_effect=self._fake_make_sink)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _fake_make_sink(self, destination):
        sink = FakeSink(destination.name)
        self.sinks[destination.name] = sink
        return sink


class TestNoScheduleRelaysImmediately(KindScheduleTestCase):
    def test_no_schedule_set_sends_in_the_same_cycle(self):
        dest = make_destination(name="a")  # schedule={} -- unchanged default behaviour
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="daily recap")
        client = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )

        run_once(config, state, client, now=_utc(3, 0))  # 3am -- irrelevant, no schedule

        self.assertEqual(self.sinks["a"].sent, ["daily recap"])
        self.assertTrue(state.has_sent("a", 1))
        self.assertEqual(state.pending, {})


class TestKindHeldUntilScheduledTime(KindScheduleTestCase):
    def test_held_before_scheduled_time_then_sent_once_reached(self):
        dest = make_destination(name="a", schedule={"daily_recap": "09:00"})
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="daily recap")

        client1 = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, state, client1, now=_utc(3, 0))  # before 09:00 -- held
        self.assertEqual(self.sinks["a"].sent, [])
        self.assertEqual([p.id for p in state.pending["a"]], [1])

        client2 = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, state, client2, now=_utc(9, 0))  # exactly 09:00 -- due

        self.assertEqual(self.sinks["a"].sent, ["daily recap"])
        self.assertTrue(state.has_sent("a", 1))
        self.assertEqual(state.pending, {})

    def test_still_held_on_a_later_poll_that_has_not_yet_reached_the_time(self):
        dest = make_destination(name="a", schedule={"daily_recap": "09:00"})
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="daily recap")

        client1 = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, state, client1, now=_utc(3, 0))
        self.assertEqual(self.sinks["a"].sent, [])

        client2 = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, state, client2, now=_utc(8, 30))  # still before 09:00
        self.assertEqual(self.sinks["a"].sent, [])
        self.assertEqual([p.id for p in state.pending["a"]], [1])


class TestOtherKindWithoutScheduleUnaffected(KindScheduleTestCase):
    def test_unscheduled_kind_on_same_destination_still_goes_immediately(self):
        dest = make_destination(
            name="a",
            kinds=["daily_recap", "net_wrapup"],
            net_ids=[1],
            schedule={"daily_recap": "09:00"},  # net_wrapup has no entry
        )
        config = make_config([dest])
        state = State(since=100)
        held = make_announcement(id=1, kind="daily_recap", text="daily recap")
        immediate = make_announcement(id=2, kind="net_wrapup", net_id=1, text="net wrapup")
        client = FakeFeedClient(
            {150: FeedPage(announcements=[held, immediate], next_since=200, poll_interval_seconds=900)}
        )

        run_once(config, state, client, now=_utc(3, 0))  # before daily_recap's 09:00

        # The unscheduled kind went out this same cycle; the scheduled one
        # is still waiting -- oldest-id-first ordering only applies among
        # items that are actually due, never used to block a later, due
        # item behind an earlier, not-yet-due one.
        self.assertEqual(self.sinks["a"].sent, ["net wrapup"])
        self.assertTrue(state.has_sent("a", 2))
        self.assertFalse(state.has_sent("a", 1))
        self.assertEqual([p.id for p in state.pending["a"]], [1])


class TestArrivingAfterScheduledTime(KindScheduleTestCase):
    def test_announcement_arriving_after_its_scheduled_time_sends_same_cycle(self):
        # Being late is better than a whole extra day -- see schedule.py's
        # is_kind_due() docstring. An item first seen well after its
        # scheduled clock time is immediately due, not held until
        # tomorrow's occurrence of that time.
        dest = make_destination(name="a", schedule={"daily_recap": "09:00"})
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="late daily recap")
        client = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )

        run_once(config, state, client, now=_utc(15, 0))  # well after 09:00

        self.assertEqual(self.sinks["a"].sent, ["late daily recap"])
        self.assertTrue(state.has_sent("a", 1))
        self.assertEqual(state.pending, {})

    def test_held_then_sent_on_a_later_poll_the_same_day_not_the_next_days_occurrence(self):
        dest = make_destination(name="a", schedule={"daily_recap": "09:00"})
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="daily recap")

        client1 = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, state, client1, now=_utc(8, 0))  # before 09:00 -- held
        self.assertEqual(self.sinks["a"].sent, [])

        # Next poll happens to land well past 09:00 (e.g. a missed cycle,
        # or a longer poll interval) -- still the SAME day, must send now,
        # not wait for 09:00 to come around again tomorrow.
        client2 = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, state, client2, now=_utc(20, 0))

        self.assertEqual(self.sinks["a"].sent, ["daily recap"])
        self.assertTrue(state.has_sent("a", 1))


class TestScheduleConflictsWithWindow(KindScheduleTestCase):
    def test_window_wins_item_stays_pending_and_warning_logged_once(self):
        # The scheduled time (10:00) can never fall inside the window
        # (08:00-09:30) -- a structural misconfiguration. The window wins:
        # the item is held forever, and a warning names both settings,
        # logged only ONCE across repeated cycles (not once per poll).
        dest = make_destination(
            name="a",
            send_after="08:00",
            send_before="09:30",
            schedule={"daily_recap": "10:00"},
        )
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="daily recap")
        warnings = set()

        client1 = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )
        with self.assertLogs("meshwars_bot.main", level="WARNING") as ctx:
            run_once(config, state, client1, now=_utc(10, 0), schedule_conflict_warnings=warnings)
        joined = "\n".join(ctx.output)
        self.assertIn("a", joined)
        self.assertIn("daily_recap", joined)
        self.assertIn("10:00", joined)
        self.assertEqual(self.sinks["a"].sent, [])
        self.assertEqual([p.id for p in state.pending["a"]], [1])

        # A second cycle, still past the (unreachable) scheduled time --
        # still held, but no SECOND warning: assertNoLogs raises if any
        # WARNING (or higher) is emitted on this logger during the block.
        client2 = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        with self.assertNoLogs("meshwars_bot.main", level="WARNING"):
            run_once(config, state, client2, now=_utc(10, 5), schedule_conflict_warnings=warnings)
        self.assertEqual(self.sinks["a"].sent, [])
        self.assertEqual([p.id for p in state.pending["a"]], [1])

    def test_a_fresh_warnings_set_warns_again_deduplication_is_caller_scoped(self):
        # Confirms the de-duplication genuinely lives in the caller-owned
        # set (mirroring run_cycle()'s ff_tracker pattern), not some
        # hidden global -- a fresh set warns again.
        dest = make_destination(
            name="a",
            send_after="08:00",
            send_before="09:30",
            schedule={"daily_recap": "10:00"},
        )
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="daily recap")

        client = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )
        with self.assertLogs("meshwars_bot.main", level="WARNING"):
            run_once(config, state, client, now=_utc(10, 0), schedule_conflict_warnings=set())


class TestKindSchedulePendingSurviveRestart(KindScheduleTestCase):
    def test_pending_items_held_on_a_kind_schedule_survive_a_simulated_restart(self):
        dest = make_destination(name="a", schedule={"daily_recap": "09:00"})
        state_path = _temp_state_path()
        config = make_config([dest], state_path=state_path)
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="held across restart")
        client = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )

        run_once(config, state, client, now=_utc(3, 0))  # before 09:00; persists to disk
        self.assertEqual(self.sinks["a"].sent, [])

        reloaded_state = load_state(state_path)
        self.assertEqual([p.id for p in reloaded_state.pending["a"]], [1])
        self.assertEqual(reloaded_state.pending["a"][0].kind, "daily_recap")

        self.sinks = {}
        client2 = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, reloaded_state, client2, now=_utc(9, 0))

        self.assertEqual(self.sinks["a"].sent, ["held across restart"])
        self.assertTrue(reloaded_state.has_sent("a", 1))


class TestKindScheduleNeverSentTwice(KindScheduleTestCase):
    def test_draining_an_already_sent_scheduled_item_never_resends(self):
        dest = make_destination(name="a", schedule={"daily_recap": "09:00"})
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="daily recap")
        cache = SinkCache()

        run_once(
            config,
            state,
            FakeFeedClient({150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}),
            sinks_cache=cache,
            now=_utc(9, 0),  # already due: sent immediately
        )
        self.assertEqual(self.sinks["a"].sent, ["daily recap"])

        run_once(
            config,
            state,
            FakeFeedClient({150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}),
            sinks_cache=cache,
            now=_utc(9, 30),
        )
        self.assertEqual(self.sinks["a"].sent, ["daily recap"])  # unchanged

    def test_feed_repeating_an_already_sent_id_is_never_requeued_or_resent(self):
        dest = make_destination(name="a", schedule={"daily_recap": "09:00"})
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="daily recap")
        cache = SinkCache()

        run_once(
            config,
            state,
            FakeFeedClient({150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}),
            sinks_cache=cache,
            now=_utc(9, 0),
        )
        self.assertEqual(self.sinks["a"].sent, ["daily recap"])

        run_once(
            config,
            state,
            FakeFeedClient({150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}),
            sinks_cache=cache,
            now=_utc(9, 5),
        )
        self.assertEqual(self.sinks["a"].sent, ["daily recap"])  # still just one
        self.assertEqual(state.pending, {})


class TestKindScheduleDryRunGuarantee(unittest.TestCase):
    """Same guarantee as test_send_window.py's
    TestDryRunGuaranteeUnderSendWindow, exercised for a per-kind schedule
    hold-then-drain cycle instead of a coarse window one: dry_run=True
    must never let a real transport library get imported."""

    def test_hold_then_drain_never_imports_a_real_radio_library(self):
        from meshwars_bot.sinks import DryRunSink

        dest = make_destination(
            name="a",
            transport="meshcore",  # a REAL transport name, but dry_run wins
            dry_run=True,
            schedule={"daily_recap": "09:00"},
        )
        config = make_config([dest])
        state = State(since=100)
        ann = make_announcement(id=1, kind="daily_recap", text="daily recap")
        cache = SinkCache()

        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
        attempted = []

        def spying_import(name, *args, **kwargs):
            if name in ("meshtastic", "meshtastic.tcp_interface", "meshcore"):
                attempted.append(name)
            return real_import(name, *args, **kwargs)

        sent_texts = []

        def fake_send(self, text):
            sent_texts.append(text)
            return True

        with mock.patch("builtins.__import__", side_effect=spying_import), mock.patch.object(
            DryRunSink, "send", fake_send
        ):
            client1 = FakeFeedClient(
                {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
            )
            run_once(config, state, client1, sinks_cache=cache, now=_utc(3, 0))  # held
            self.assertEqual(sent_texts, [])

            client2 = FakeFeedClient(
                {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
            )
            run_once(config, state, client2, sinks_cache=cache, now=_utc(9, 0))  # drains

        self.assertEqual(attempted, [], "dry_run=True must never import a radio library")
        self.assertIsInstance(cache._sinks["a"], DryRunSink)
        self.assertEqual(sent_texts, ["daily recap"])
        self.assertTrue(state.has_sent("a", 1))


if __name__ == "__main__":
    unittest.main()
