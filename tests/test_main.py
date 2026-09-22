import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot import webui
from meshwars_bot.config import Config, Destination, FeedConfig, load_config
from meshwars_bot.feed import Announcement, FeedPage
from meshwars_bot.main import (
    SinkCache,
    _reload_config,
    _resolve_web_bind,
    main,
    run_cycle,
    run_once,
    spawn_webui_thread,
)
from meshwars_bot.state import State
from meshwars_bot.webui import BotStatus


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
    def __init__(self, destination_name, raise_on_close=False):
        self.destination_name = destination_name
        self.sent = []
        self.closed = False
        self.close_calls = 0
        self._raise_on_close = raise_on_close

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self._raise_on_close:
            raise RuntimeError("close boom")


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


class TestBotStatusUpdatedByRunOnce(RunOnceTestCase):
    """run_once()'s optional `status` param is how the web UI's /api/state
    route learns about each poll cycle -- exercised here directly against
    run_once(), independent of webui.py's HTTP layer."""

    def test_status_updated_on_success(self):
        dest = make_destination(name="a", text_budget=150)
        config = make_config([dest])
        state = State(since=100, etag=None)
        client = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        status = BotStatus()

        run_once(config, state, client, status=status)

        snapshot = status.snapshot()
        self.assertEqual(snapshot["cursor_since"], 200)
        self.assertIsNotNone(snapshot["last_poll_at"])
        self.assertIsNone(snapshot["last_error"])
        self.assertTrue(snapshot["feed_reachable"])

    def test_status_reflects_a_failed_poll(self):
        dest = make_destination(name="a", text_budget=150)
        config = make_config([dest])
        state = State(since=100, etag=None)
        client = FakeFeedClient({150: None})
        status = BotStatus()

        run_once(config, state, client, status=status)

        snapshot = status.snapshot()
        self.assertFalse(snapshot["feed_reachable"])
        self.assertIsNotNone(snapshot["last_error"])
        self.assertIn("150", snapshot["last_error"])

    def test_status_reports_sent_counts_per_destination(self):
        dest = make_destination(name="a", text_budget=150)
        config = make_config([dest])
        state = State(since=100, etag=None)
        ann = make_announcement(id=1, text="hi")
        client = FakeFeedClient(
            {150: FeedPage(announcements=[ann], next_since=200, poll_interval_seconds=900)}
        )
        status = BotStatus()

        run_once(config, state, client, status=status)

        self.assertEqual(status.snapshot()["sent_counts"], {"a": 1})

    def test_run_once_works_unchanged_with_no_status(self):
        # Every existing caller (and every other test in this file) never
        # passes `status` -- confirms the parameter is truly optional.
        dest = make_destination(name="a", text_budget=150)
        config = make_config([dest])
        state = State(since=100, etag=None)
        client = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        run_once(config, state, client)  # must not raise


class TestHotReload(unittest.TestCase):
    """A config saved through the web UI (or hand-edited) must take effect
    on the bot's next poll cycle without restarting the process -- see the
    comment above the reload call in main()'s loop."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")

    def _write(self, base_url):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(
                'feed:\n  base_url: "%s"\n  timeout_seconds: 20\n'
                'destinations: []\n' % base_url
            )

    def test_reload_picks_up_a_change_saved_between_cycles(self):
        self._write("https://a.example")
        config1 = load_config(self.config_path)
        self.assertEqual(config1.feed.base_url, "https://a.example")

        self._write("https://b.example")
        config2 = _reload_config(self.config_path, config1)
        self.assertEqual(config2.feed.base_url, "https://b.example")

    def test_reload_keeps_previous_config_when_the_file_is_now_invalid(self):
        self._write("https://a.example")
        config1 = load_config(self.config_path)

        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("feed:\n\tbase_url: \"broken\"\n")  # tab indentation -- a parse error

        config2 = _reload_config(self.config_path, config1)
        self.assertIs(config2, config1)
        self.assertEqual(config2.feed.base_url, "https://a.example")

    def test_reload_keeps_previous_config_when_file_briefly_missing(self):
        self._write("https://a.example")
        config1 = load_config(self.config_path)

        os.remove(self.config_path)

        config2 = _reload_config(self.config_path, config1)
        self.assertIs(config2, config1)


class TestWebUIThreadCannotStopPollLoop(unittest.TestCase):
    """spawn_webui_thread() must never let a failure in the web UI (a bind
    failure, an exception anywhere in start_webui) escape onto the thread
    that runs the poll loop."""

    def test_start_webui_raising_does_not_propagate(self):
        from unittest import mock

        with mock.patch("meshwars_bot.main.start_webui", side_effect=RuntimeError("boom, bind failed")):
            config = make_config([make_destination(name="a")])
            status = BotStatus()
            thread = spawn_webui_thread("./config.yaml", config, status)
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        # No exception escaped this test -- that IS the assertion: a crash
        # in the web UI thread's target must be caught internally, never
        # raised into (or observable as a thread death that takes down)
        # the caller.

    def test_poll_loop_keeps_running_when_webui_thread_dies(self):
        from unittest import mock

        with mock.patch("meshwars_bot.main.start_webui", side_effect=RuntimeError("boom")):
            config = make_config([make_destination(name="a", text_budget=150)])
            status = BotStatus()
            thread = spawn_webui_thread("./config.yaml", config, status)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

            # The poll loop -- represented here by run_once(), same as
            # every other test in this file -- must still work fine after
            # the web UI thread has already died.
            state = State(since=100, etag=None)
            client = FakeFeedClient(
                {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
            )
            with mock.patch("meshwars_bot.main.make_sink", side_effect=lambda d: FakeSink(d.name)):
                run_once(config, state, client, status=status)
            self.assertEqual(state.since, 200)


class TestNoSafeCursorNeverRelays(RunOnceTestCase):
    """The chicken-and-egg fix: a failed fast-forward must not exit the
    process, and must never let anything reach a sink -- however many
    cycles the loop runs while the feed stays unreachable."""

    def test_loop_survives_repeated_fast_forward_failures_and_relays_nothing(self):
        dest = make_destination(name="a", text_budget=150)
        config = make_config([dest])
        state = State()  # fresh -- no state file, no cursor yet
        status = BotStatus()
        client = FakeFeedClient({150: None})  # feed always unreachable
        ff_tracker = {"failed": False}

        for _ in range(5):
            sleep_seconds = run_cycle(config, state, client, status=status, ff_tracker=ff_tracker)
            self.assertIsInstance(sleep_seconds, int)
            self.assertIsNone(state.since)

        # run_once() (and therefore make_sink()/Sink.send()) was never
        # reached -- no sink was even constructed for "a".
        self.assertEqual(self.sinks, {})
        snapshot = status.snapshot()
        self.assertFalse(snapshot["has_safe_cursor"])
        self.assertFalse(snapshot["feed_reachable"])
        self.assertIsNotNone(snapshot["last_error"])

    def test_once_mode_keeps_state_since_none_and_does_not_route(self):
        # Same guarantee, exercised through _try_fast_forward() directly as
        # --once uses it: several failed attempts, never a cursor, never a
        # route.
        from meshwars_bot.main import _try_fast_forward

        dest = make_destination(name="a", text_budget=150)
        config = make_config([dest])
        state = State()
        client = FakeFeedClient({150: None})

        for _ in range(3):
            ok = _try_fast_forward(config, state, client)
            self.assertFalse(ok)
            self.assertIsNone(state.since)


class TestFastForwardRecovery(RunOnceTestCase):
    def test_recovers_once_feed_answers_and_relays_only_announcements_after_cursor(self):
        dest = make_destination(name="a", text_budget=150)
        config = make_config([dest])
        state = State()
        status = BotStatus()
        ff_tracker = {"failed": False}

        pre_cursor_ann = make_announcement(id=1, text="should never be seen")
        post_cursor_ann = make_announcement(id=2, text="relayed after recovery")

        client = FakeFeedClient(
            {
                150: [
                    None,  # cycle 1: fast-forward fails
                    None,  # cycle 2: fast-forward fails again
                    FeedPage(announcements=[pre_cursor_ann], next_since=100, poll_interval_seconds=900),
                    # cycle 3: fast-forward succeeds -- next_since=100 is the
                    # cursor; announcements on THIS page are the fast-forward
                    # poll's own response and must never be routed.
                    FeedPage(announcements=[post_cursor_ann], next_since=200, poll_interval_seconds=900),
                    # cycle 4: state.since=100 now, run_once() polls for real
                    # and this announcement is routed.
                ]
            }
        )

        run_cycle(config, state, client, status=status, ff_tracker=ff_tracker)  # cycle 1: fails
        run_cycle(config, state, client, status=status, ff_tracker=ff_tracker)  # cycle 2: fails
        self.assertIsNone(state.since)
        self.assertEqual(self.sinks, {})

        run_cycle(config, state, client, status=status, ff_tracker=ff_tracker)  # cycle 3: fast-forwards
        self.assertEqual(state.since, 100)
        # The fast-forward poll's own response must not have been routed --
        # nothing sent yet, no sink even built.
        self.assertEqual(self.sinks, {})
        self.assertTrue(status.snapshot()["has_safe_cursor"])

        run_cycle(config, state, client, status=status, ff_tracker=ff_tracker)  # cycle 4: routes for real
        self.assertEqual(self.sinks["a"].sent, ["relayed after recovery"])
        self.assertEqual(state.since, 200)


class TestOnceHonestExitCode(unittest.TestCase):
    """`--once` (without `--web`) must still exit non-zero on a failed
    fast-forward -- this fix must not mask a genuine failure in that mode.
    Exercises the real `main()` entrypoint end-to-end; `base_url` points at
    a port nothing listens on, so the feed request fails fast and locally,
    with no real network dependency."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")
        self.state_path = os.path.join(self.tmpdir.name, "state.json")
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(
                'feed:\n'
                '  base_url: "http://127.0.0.1:1"\n'
                '  timeout_seconds: 2\n'
                'state_path: "%s"\n'
                'destinations: []\n' % self.state_path
            )

    def test_once_without_web_exits_nonzero_on_failed_fast_forward(self):
        rc = main(["--config", self.config_path, "--once"])
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(self.state_path))


class TestWebServesWhileFeedUnreachable(RunOnceTestCase):
    """The core fix, end to end against a real HTTP server: the web UI must
    come up and answer requests while the feed is unreachable and no safe
    cursor has ever been established, and /api/state must say so."""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml"
        )
        self.status = BotStatus()
        self.server = webui.build_server(self.config_path, self.status, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._shutdown_server)

    def _shutdown_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, raw
        finally:
            conn.close()

    def test_page_and_state_available_before_any_safe_cursor(self):
        config = make_config([make_destination(name="a", text_budget=150)])
        state = State()
        client = FakeFeedClient({150: None})
        ff_tracker = {"failed": False}

        # Simulate a few unreachable poll cycles against the running server,
        # same as the poll loop thread would.
        for _ in range(3):
            run_cycle(config, state, client, status=self.status, ff_tracker=ff_tracker)

        status_code, body = self._get("/")
        self.assertEqual(status_code, 200)
        self.assertIn(b"meshwars-bot config", body)

        status_code, body = self._get("/api/state")
        self.assertEqual(status_code, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertFalse(data["has_safe_cursor"])
        self.assertIsNotNone(data["last_error"])
        self.assertIsNone(state.since)
        self.assertEqual(self.sinks, {})


class TestSinkCache(unittest.TestCase):
    """Unit tests for SinkCache.reconcile()/close_all() -- the reuse-across-
    cycles fix. make_sink() itself is faked here so these tests are about
    the cache's own bookkeeping (identity comparison, close-on-change,
    close-on-removal), not about what a real sink does."""

    def setUp(self):
        from unittest import mock

        self.built = []  # destination names, in the order make_sink() was called

        def fake_make_sink(destination):
            self.built.append(destination.name)
            return FakeSink(destination.name)

        patcher = mock.patch("meshwars_bot.main.make_sink", side_effect=fake_make_sink)
        self.addCleanup(patcher.stop)
        self.mock_make_sink = patcher.start()

    def test_unchanged_destination_builds_sink_once_across_many_cycles(self):
        cache = SinkCache()
        dest = make_destination(name="a", transport="dryrun", dry_run=True)

        sinks1 = cache.reconcile([dest])
        sinks2 = cache.reconcile([dest])
        sinks3 = cache.reconcile([dest])

        self.assertEqual(self.mock_make_sink.call_count, 1)
        self.assertEqual(self.built, ["a"])
        self.assertIs(sinks1["a"], sinks2["a"])
        self.assertIs(sinks2["a"], sinks3["a"])
        self.assertFalse(sinks2["a"].closed)

    def test_changing_any_identity_field_closes_old_and_builds_new(self):
        base = dict(
            name="a", transport="meshtastic", host="10.0.0.1", port=4403, channel="general", dry_run=False
        )
        variants = {
            "host": dict(base, host="10.0.0.2"),
            "port": dict(base, port=4404),
            "channel": dict(base, channel="ops"),
            "transport": dict(base, transport="meshcore"),
            "dry_run": dict(base, dry_run=True),
        }
        for field_name, changed in variants.items():
            with self.subTest(field=field_name):
                cache = SinkCache()
                dest_v1 = make_destination(**base)
                dest_v2 = make_destination(**changed)

                old_sink = cache.reconcile([dest_v1])["a"]
                new_sink = cache.reconcile([dest_v2])["a"]

                self.assertIsNot(old_sink, new_sink)
                self.assertTrue(old_sink.closed, f"old sink not closed when {field_name} changed")
                self.assertFalse(new_sink.closed)

    def test_unrelated_field_change_does_not_rebuild(self):
        # kinds/net_ids/board are not part of connection identity -- a
        # routing-only edit must not churn the connection.
        cache = SinkCache()
        dest_v1 = make_destination(name="a", kinds=["daily_recap"])
        dest_v2 = make_destination(name="a", kinds=["net_wrapup"])

        sink1 = cache.reconcile([dest_v1])["a"]
        sink2 = cache.reconcile([dest_v2])["a"]

        self.assertIs(sink1, sink2)
        self.assertEqual(self.mock_make_sink.call_count, 1)
        self.assertFalse(sink1.closed)

    def test_removed_destination_closes_its_sink_and_drops_it(self):
        cache = SinkCache()
        dest_a = make_destination(name="a")
        dest_b = make_destination(name="b")

        sinks1 = cache.reconcile([dest_a, dest_b])
        sink_a, sink_b = sinks1["a"], sinks1["b"]

        sinks2 = cache.reconcile([dest_b])

        self.assertTrue(sink_a.closed)
        self.assertFalse(sink_b.closed)
        self.assertEqual(set(sinks2.keys()), {"b"})
        self.assertIs(sinks2["b"], sink_b)

        # And the removed destination coming back is treated as brand new
        # (rebuilt), not resurrected from anywhere.
        sinks3 = cache.reconcile([dest_a, dest_b])
        self.assertIsNot(sinks3["a"], sink_a)

    def test_close_all_closes_every_sink(self):
        cache = SinkCache()
        cache.reconcile([make_destination(name="a"), make_destination(name="b")])

        cache.close_all()

        self.assertEqual(self.built, ["a", "b"])
        # A subsequent reconcile() for the same destinations must rebuild --
        # close_all() drops everything from the cache.
        rebuilt = cache.reconcile([make_destination(name="a"), make_destination(name="b")])
        self.assertEqual(self.mock_make_sink.call_count, 4)
        self.assertTrue(rebuilt["a"])

    def test_close_all_closes_every_sink_even_if_one_raises(self):
        cache = SinkCache()
        sinks = cache.reconcile([make_destination(name="a"), make_destination(name="b")])
        sinks["a"]._raise_on_close = True

        cache.close_all()  # must not raise

        self.assertEqual(sinks["a"].close_calls, 1)
        self.assertTrue(sinks["a"].closed)
        self.assertTrue(sinks["b"].closed)

    def test_reconcile_close_failure_does_not_stop_the_new_sink_being_built(self):
        cache = SinkCache()
        sinks1 = cache.reconcile([make_destination(name="a", host="10.0.0.1", dry_run=False, transport="meshtastic")])
        sinks1["a"]._raise_on_close = True

        sinks2 = cache.reconcile(
            [make_destination(name="a", host="10.0.0.2", dry_run=False, transport="meshtastic")]
        )

        self.assertIn("a", sinks2)
        self.assertIsNot(sinks2["a"], sinks1["a"])


class TestSinkCachePreservesDryRunGuarantee(unittest.TestCase):
    """SinkCache must still go through the real make_sink(), so the
    dry_run:true safety guarantee (never import/construct a real transport)
    holds exactly as it does for a direct make_sink() call -- see
    tests/test_sinks.py's TestMakeSinkDryRunSafety for the same check
    against make_sink() itself."""

    def test_dry_run_true_never_imports_a_real_library_through_the_cache(self):
        from meshwars_bot.sinks import DryRunSink

        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
        attempted = []

        def spying_import(name, *args, **kwargs):
            if name in ("meshtastic", "meshtastic.tcp_interface", "meshcore"):
                attempted.append(name)
            return real_import(name, *args, **kwargs)

        from unittest import mock

        cache = SinkCache()
        dest = make_destination(name="a", transport="meshtastic", dry_run=True)

        with mock.patch("builtins.__import__", side_effect=spying_import):
            sinks1 = cache.reconcile([dest])
            sinks2 = cache.reconcile([dest])

        self.assertIsInstance(sinks1["a"], DryRunSink)
        self.assertIs(sinks1["a"], sinks2["a"])
        self.assertEqual(attempted, [], "dry_run=True must never import a radio library, cache or not")


class TestRunOnceSinkReuse(unittest.TestCase):
    """run_once()'s `sinks_cache` param is how the real fix (reuse across
    poll cycles) reaches production -- exercised here directly against
    run_once(), same as SinkCache's own unit tests but through the actual
    poll/route/send entry point."""

    def setUp(self):
        from unittest import mock

        self.make_sink_calls = []

        def fake_make_sink(destination):
            self.make_sink_calls.append(destination.name)
            return FakeSink(destination.name)

        patcher = mock.patch("meshwars_bot.main.make_sink", side_effect=fake_make_sink)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_sink_built_once_across_multiple_run_once_cycles_with_shared_cache(self):
        dest = make_destination(name="a", text_budget=150)
        config = make_config([dest])
        state = State(since=100, etag=None)
        client = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )
        cache = SinkCache()

        run_once(config, state, client, sinks_cache=cache)
        run_once(config, state, client, sinks_cache=cache)
        run_once(config, state, client, sinks_cache=cache)

        self.assertEqual(self.make_sink_calls, ["a"])

    def test_without_a_shared_cache_each_call_gets_its_own_fresh_sink(self):
        # Documents the deliberate fallback: run_once() with no sinks_cache
        # builds a throwaway SinkCache for that one call -- what every other
        # test in this file relies on. The actual reuse-across-cycles fix
        # lives in main()'s persistent SinkCache, threaded through
        # run_cycle().
        dest = make_destination(name="a", text_budget=150)
        config = make_config([dest])
        state = State(since=100, etag=None)
        client = FakeFeedClient(
            {150: FeedPage(announcements=[], next_since=200, poll_interval_seconds=900)}
        )

        run_once(config, state, client)
        run_once(config, state, client)

        self.assertEqual(self.make_sink_calls, ["a", "a"])


class TestSinksClosedOnShutdown(unittest.TestCase):
    """End-to-end through main(): every sink built during a run must be
    close()'d on the way out, whether that's --once completing normally or
    the poll loop being interrupted."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")
        self.state_path = os.path.join(self.tmpdir.name, "state.json")
        self.sinks = {}

    def _write_config(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(
                'feed:\n'
                '  base_url: "https://meshwars.example"\n'
                '  timeout_seconds: 5\n'
                'state_path: "%s"\n'
                'destinations:\n'
                '  - name: a\n'
                '    transport: dryrun\n'
                '    board: mc\n'
                '    dry_run: true\n'
                '    text_budget: 150\n' % self.state_path
            )

    def _write_state(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "since": 100, "etag": None, "sent": []}, f)

    def _fake_make_sink(self, destination):
        sink = FakeSink(destination.name)
        self.sinks[destination.name] = sink
        return sink

    def test_once_closes_all_sinks_on_completion(self):
        from unittest import mock

        self._write_config()
        self._write_state()

        with mock.patch("meshwars_bot.main.make_sink", side_effect=self._fake_make_sink), mock.patch(
            "meshwars_bot.main.FeedClient"
        ) as MockFeedClient:
            MockFeedClient.return_value.poll.return_value = FeedPage(
                announcements=[], next_since=200, poll_interval_seconds=900
            )
            rc = main(["--config", self.config_path, "--once"])

        self.assertEqual(rc, 0)
        self.assertIn("a", self.sinks)
        self.assertTrue(self.sinks["a"].closed)

    def test_keyboard_interrupt_in_poll_loop_closes_all_sinks(self):
        from unittest import mock

        self._write_config()
        self._write_state()

        with mock.patch("meshwars_bot.main.make_sink", side_effect=self._fake_make_sink), mock.patch(
            "meshwars_bot.main.FeedClient"
        ) as MockFeedClient, mock.patch("meshwars_bot.main.time.sleep", side_effect=KeyboardInterrupt):
            MockFeedClient.return_value.poll.return_value = FeedPage(
                announcements=[], next_since=200, poll_interval_seconds=900
            )
            rc = main(["--config", self.config_path])

        self.assertEqual(rc, 0)
        self.assertIn("a", self.sinks)
        self.assertTrue(self.sinks["a"].closed)


class TestResolveWebBindIPv6(unittest.TestCase):
    """--web-bind must handle a bracketed IPv6 literal correctly, and never
    silently mangle one -- see _resolve_web_bind()'s docstring."""

    def test_no_override_uses_config_defaults(self):
        config = make_config([])
        config.web.bind_host = "127.0.0.1"
        config.web.bind_port = 9000
        self.assertEqual(_resolve_web_bind(config, None), ("127.0.0.1", 9000))

    def test_plain_hostport_unchanged(self):
        config = make_config([])
        self.assertEqual(_resolve_web_bind(config, "0.0.0.0:8471"), ("0.0.0.0", 8471))

    def test_bracketed_ipv6_loopback(self):
        config = make_config([])
        self.assertEqual(_resolve_web_bind(config, "[::1]:8471"), ("::1", 8471))

    def test_bracketed_ipv6_full_literal(self):
        config = make_config([])
        self.assertEqual(_resolve_web_bind(config, "[2001:db8::1]:8471"), ("2001:db8::1", 8471))

    def test_unbracketed_ipv6_literal_is_rejected_not_mangled(self):
        # A naive rpartition(":") would silently produce host='::1',
        # port='8471' here by luck, or worse for other literals -- it must
        # be rejected outright, never guessed at.
        config = make_config([])
        with self.assertRaises(ValueError):
            _resolve_web_bind(config, "::1:8471")

    def test_empty_bracketed_host_is_rejected(self):
        config = make_config([])
        with self.assertRaises(ValueError):
            _resolve_web_bind(config, "[]:8471")

    def test_non_integer_port_is_rejected_clearly(self):
        config = make_config([])
        with self.assertRaises(ValueError):
            _resolve_web_bind(config, "0.0.0.0:notaport")

    def test_missing_port_is_rejected(self):
        config = make_config([])
        with self.assertRaises(ValueError):
            _resolve_web_bind(config, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
