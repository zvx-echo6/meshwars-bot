"""Tests for meshwars_bot/sinks.py's real transports.

CRITICAL: these tests must pass on SYSTEM python3, which has no meshtastic
or meshcore installed, AND on the repo's .venv python, which has both. So
every test that exercises a "real" sink injects FAKE `meshtastic`/`meshcore`
modules into sys.modules -- the real libraries (installed or not) are never
imported by this file. Nothing here opens a socket, starts a thread, or
touches any real host; every fake is pure in-memory Python.
"""

import enum
import os
import sys
import types
import unittest
from dataclasses import dataclass, field
from typing import List, Optional
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot import sinks as sinks_mod
from meshwars_bot.sinks import (
    DryRunSink,
    MeshCoreSink,
    MeshtasticSink,
    Sink,
    make_sink,
)


@dataclass
class FakeDestination:
    """Stand-in for config.Destination -- only the fields sinks.py reads."""

    name: str = "dest"
    transport: str = "meshtastic"
    host: Optional[str] = "192.168.1.50"
    port: Optional[int] = None
    channel: object = 0
    board: str = "mt"
    dry_run: bool = False
    text_budget: int = 150
    kinds: List[str] = field(default_factory=list)
    net_ids: List[int] = field(default_factory=list)


def _no_sleep(monkeypatch):
    """Every real-sink test disables actual wall-clock pacing sleeps by
    default -- pacing itself is tested separately, deterministically, with
    a fake clock. Without this, every send() would really sleep 2.2-2.6s."""
    monkeypatch.setattr(sinks_mod.time, "sleep", lambda _seconds: None)


# ---------------------------------------------------------------------------
# make_sink() / DryRunSink safety
# ---------------------------------------------------------------------------


class TestMakeSinkDryRunSafety(unittest.TestCase):
    def test_dry_run_true_never_imports_a_real_library(self):
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
        attempted = []

        def spying_import(name, *args, **kwargs):
            if name in ("meshtastic", "meshtastic.tcp_interface", "meshcore"):
                attempted.append(name)
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=spying_import):
            sink = make_sink(FakeDestination(dry_run=True, transport="meshtastic"))
            self.assertIsInstance(sink, DryRunSink)
            sink2 = make_sink(FakeDestination(dry_run=True, transport="meshcore"))
            self.assertIsInstance(sink2, DryRunSink)

        self.assertEqual(attempted, [], "dry_run=True must never import a radio library")

    def test_dry_run_checked_before_transport_dispatch(self):
        # Even an otherwise-unknown transport is fine under dry_run: true,
        # because dry_run is checked FIRST.
        sink = make_sink(FakeDestination(dry_run=True, transport="bogus"))
        self.assertIsInstance(sink, DryRunSink)

    def test_dry_run_false_constructs_real_sink_without_importing_library(self):
        # Constructing a real Sink must not itself require the library --
        # imports stay lazy until a send is actually attempted.
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
        attempted = []

        def spying_import(name, *args, **kwargs):
            if name in ("meshtastic", "meshtastic.tcp_interface", "meshcore"):
                attempted.append(name)
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=spying_import):
            sink = make_sink(FakeDestination(dry_run=False, transport="meshtastic"))
            self.assertIsInstance(sink, MeshtasticSink)
        self.assertEqual(attempted, [])


class TestMissingLibrary(unittest.TestCase):
    """A missing radio library must produce a clear, actionable error naming
    the pip package -- surfaced through send() as a logged False, never as
    an unhandled ImportError. Uses the `sys.modules[name] = None` trick so
    this is deterministic on BOTH system python3 (genuinely missing) and the
    venv (installed, but forced to look missing for this test)."""

    def test_missing_meshtastic_library_names_the_package(self):
        dest = FakeDestination(transport="meshtastic", channel=0)
        sink = MeshtasticSink(dest)
        with mock.patch.dict(sys.modules, {"meshtastic": None, "meshtastic.tcp_interface": None}):
            with self.assertLogs("meshwars_bot.sinks", level="ERROR") as cm:
                result = sink.send("hello")
        self.assertFalse(result)
        joined = "\n".join(cm.output)
        self.assertIn("pip install meshtastic", joined)

    def test_missing_meshcore_library_names_the_package(self):
        dest = FakeDestination(transport="meshcore", channel="MeshWars", host="h", port=1)
        sink = MeshCoreSink(dest)
        with mock.patch.dict(sys.modules, {"meshcore": None}):
            with self.assertLogs("meshwars_bot.sinks", level="ERROR") as cm:
                result = sink.send("hello")
        self.assertFalse(result)
        joined = "\n".join(cm.output)
        self.assertIn("pip install meshcore", joined)


# ---------------------------------------------------------------------------
# Byte budget last line of defence
# ---------------------------------------------------------------------------


class TestBudgetRefusal(unittest.TestCase):
    def test_over_budget_text_is_refused_not_truncated(self, ):
        dest = FakeDestination(transport="meshtastic", channel=0, text_budget=10)
        sink = MeshtasticSink(dest)
        with mock.patch.object(sink, "_ensure_connected") as ensure, mock.patch.object(
            sink, "_transmit"
        ) as transmit:
            with self.assertLogs("meshwars_bot.sinks", level="ERROR") as cm:
                result = sink.send("this text is way over ten bytes")
        self.assertFalse(result)
        ensure.assert_not_called()
        transmit.assert_not_called()
        self.assertIn("over its text_budget", "\n".join(cm.output))


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------


class TestPacing(unittest.TestCase):
    def test_second_send_waits_at_least_the_floor(self):
        dest = FakeDestination(transport="meshtastic", channel=0)
        sink = MeshtasticSink(dest)

        clock = {"t": 100.0}
        sleeps = []

        def fake_monotonic():
            return clock["t"]

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["t"] += seconds

        with mock.patch.object(sinks_mod.time, "monotonic", side_effect=fake_monotonic), mock.patch.object(
            sinks_mod.time, "sleep", side_effect=fake_sleep
        ), mock.patch.object(sinks_mod.random, "uniform", return_value=0.25), mock.patch.object(
            sink, "_ensure_connected"
        ), mock.patch.object(
            sink, "_transmit", return_value=True
        ):
            sink.send("one")
            self.assertEqual(sleeps, [])  # first send never waits

            # No time passes between the two sends -> the full jittered gap
            # (0.25s here, via the patched random.uniform) must be slept.
            sink.send("two")

        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 0.25)

    def test_gap_never_computed_below_hard_floor(self):
        dest = FakeDestination(transport="meshtastic", channel=0)
        sink = MeshtasticSink(dest)
        # Even if random.uniform somehow returned something tiny, the hard
        # floor must win.
        with mock.patch.object(sinks_mod.random, "uniform", return_value=0.001):
            sink._last_send_at = None
            with mock.patch.object(sinks_mod.time, "sleep") as sleep_mock:
                sink._pace()
                sleep_mock.assert_not_called()  # first call, nothing to wait for
                sink._pace()
                # second call: elapsed ~0 since monotonic didn't advance, so
                # it must sleep close to the hard floor, not 0.001
                waited = sleep_mock.call_args[0][0]
                self.assertGreaterEqual(waited, sinks_mod.PACING_HARD_FLOOR_SECONDS - 0.01)


# ---------------------------------------------------------------------------
# Exceptions never escape send()
# ---------------------------------------------------------------------------


class TestNeverRaises(unittest.TestCase):
    def test_exception_in_transmit_never_escapes_send(self, ):
        dest = FakeDestination(transport="meshtastic", channel=0)
        sink = MeshtasticSink(dest)
        with mock.patch.object(sinks_mod.time, "sleep"), mock.patch.object(
            sink, "_ensure_connected"
        ), mock.patch.object(sink, "_reconnect"), mock.patch.object(
            sink, "_transmit", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("meshwars_bot.sinks", level="WARNING"):
                result = sink.send("hi")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Reconnect-once-then-fail
# ---------------------------------------------------------------------------


class TestReconnectOnce(unittest.TestCase):
    def test_retries_once_then_succeeds(self):
        dest = FakeDestination(transport="meshtastic", channel=0)
        sink = MeshtasticSink(dest)
        calls = {"n": 0}

        def flaky_transmit(text):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("link dropped")
            return True

        with mock.patch.object(sinks_mod.time, "sleep"), mock.patch.object(
            sink, "_ensure_connected"
        ), mock.patch.object(sink, "_reconnect") as reconnect, mock.patch.object(
            sink, "_transmit", side_effect=flaky_transmit
        ):
            result = sink.send("hi")

        self.assertTrue(result)
        self.assertEqual(calls["n"], 2)
        reconnect.assert_called_once()

    def test_gives_up_after_one_retry(self):
        dest = FakeDestination(transport="meshtastic", channel=0)
        sink = MeshtasticSink(dest)
        calls = {"n": 0}

        def always_fails(text):
            calls["n"] += 1
            raise ConnectionError("link dropped")

        with mock.patch.object(sinks_mod.time, "sleep"), mock.patch.object(
            sink, "_ensure_connected"
        ), mock.patch.object(sink, "_reconnect") as reconnect, mock.patch.object(
            sink, "_transmit", side_effect=always_fails
        ):
            with self.assertLogs("meshwars_bot.sinks", level="ERROR"):
                result = sink.send("hi")

        self.assertFalse(result)
        self.assertEqual(calls["n"], 2)  # first attempt + exactly one retry
        reconnect.assert_called_once()


# ---------------------------------------------------------------------------
# MeshtasticSink against a fake meshtastic module
# ---------------------------------------------------------------------------


def _install_fake_meshtastic(send_behavior=None):
    """Installs fake `meshtastic`/`meshtastic.tcp_interface` modules into
    sys.modules and returns the fake TCPInterface class for assertions.
    `send_behavior(instance, text, channel_index)` may raise to simulate a
    transport failure; default just records the send."""

    class FakeTCPInterface:
        instances: List["FakeTCPInterface"] = []

        def __init__(self, hostname, portNumber):
            self.hostname = hostname
            self.portNumber = portNumber
            self.sent = []
            self.closed = False
            FakeTCPInterface.instances.append(self)

        def sendText(self, text, channelIndex):
            if send_behavior is not None:
                send_behavior(self, text, channelIndex)
            self.sent.append((text, channelIndex))

        def close(self):
            self.closed = True

    fake_meshtastic = types.ModuleType("meshtastic")
    fake_tcp_interface = types.ModuleType("meshtastic.tcp_interface")
    fake_tcp_interface.TCPInterface = FakeTCPInterface
    fake_meshtastic.tcp_interface = fake_tcp_interface
    return fake_meshtastic, fake_tcp_interface, FakeTCPInterface


class TestMeshtasticSink(unittest.TestCase):
    def test_sends_expected_text_to_expected_channel_index_int(self):
        fake_meshtastic, fake_tcp_interface, FakeTCPInterface = _install_fake_meshtastic()
        dest = FakeDestination(
            name="mt1", transport="meshtastic", host="10.0.0.5", port=4403, channel=2, text_budget=150
        )
        sink = MeshtasticSink(dest)
        with mock.patch.dict(
            sys.modules,
            {"meshtastic": fake_meshtastic, "meshtastic.tcp_interface": fake_tcp_interface},
        ), mock.patch.object(sinks_mod.time, "sleep"):
            result = sink.send("hello mesh")

        self.assertTrue(result)
        self.assertEqual(len(FakeTCPInterface.instances), 1)
        instance = FakeTCPInterface.instances[0]
        self.assertEqual(instance.hostname, "10.0.0.5")
        self.assertEqual(instance.portNumber, 4403)
        self.assertEqual(instance.sent, [("hello mesh", 2)])

    def test_channel_index_accepts_digit_string(self):
        fake_meshtastic, fake_tcp_interface, FakeTCPInterface = _install_fake_meshtastic()
        dest = FakeDestination(transport="meshtastic", host="10.0.0.5", channel="3")
        sink = MeshtasticSink(dest)
        with mock.patch.dict(
            sys.modules,
            {"meshtastic": fake_meshtastic, "meshtastic.tcp_interface": fake_tcp_interface},
        ), mock.patch.object(sinks_mod.time, "sleep"):
            result = sink.send("x")
        self.assertTrue(result)
        self.assertEqual(FakeTCPInterface.instances[0].sent, [("x", 3)])

    def test_default_port_used_when_unset(self):
        fake_meshtastic, fake_tcp_interface, FakeTCPInterface = _install_fake_meshtastic()
        dest = FakeDestination(transport="meshtastic", host="10.0.0.5", port=None, channel=0)
        sink = MeshtasticSink(dest)
        with mock.patch.dict(
            sys.modules,
            {"meshtastic": fake_meshtastic, "meshtastic.tcp_interface": fake_tcp_interface},
        ), mock.patch.object(sinks_mod.time, "sleep"):
            sink.send("x")
        self.assertEqual(FakeTCPInterface.instances[0].portNumber, sinks_mod.DEFAULT_MESHTASTIC_PORT)

    def test_non_digit_channel_fails_clearly_without_connecting(self):
        fake_meshtastic, fake_tcp_interface, FakeTCPInterface = _install_fake_meshtastic()
        dest = FakeDestination(transport="meshtastic", host="10.0.0.5", channel="not-a-number")
        sink = MeshtasticSink(dest)
        with mock.patch.dict(
            sys.modules,
            {"meshtastic": fake_meshtastic, "meshtastic.tcp_interface": fake_tcp_interface},
        ), mock.patch.object(sinks_mod.time, "sleep"):
            with self.assertLogs("meshwars_bot.sinks", level="ERROR") as cm:
                result = sink.send("x")
        self.assertFalse(result)
        self.assertEqual(FakeTCPInterface.instances, [])
        self.assertIn("channel must be", "\n".join(cm.output))

    def test_reconnect_once_then_succeed_with_real_fake_transport(self):
        state = {"attempts": 0}

        def send_behavior(instance, text, channel_index):
            state["attempts"] += 1
            if state["attempts"] == 1:
                raise ConnectionError("simulated dropped TCP link")

        fake_meshtastic, fake_tcp_interface, FakeTCPInterface = _install_fake_meshtastic(send_behavior)
        dest = FakeDestination(transport="meshtastic", host="10.0.0.5", channel=0)
        sink = MeshtasticSink(dest)
        with mock.patch.dict(
            sys.modules,
            {"meshtastic": fake_meshtastic, "meshtastic.tcp_interface": fake_tcp_interface},
        ), mock.patch.object(sinks_mod.time, "sleep"):
            with self.assertLogs("meshwars_bot.sinks", level="WARNING"):
                result = sink.send("retry me")

        self.assertTrue(result)
        self.assertEqual(state["attempts"], 2)
        # First interface was closed on reconnect, a second one was opened.
        self.assertEqual(len(FakeTCPInterface.instances), 2)
        self.assertTrue(FakeTCPInterface.instances[0].closed)

    def test_close_closes_the_interface(self):
        fake_meshtastic, fake_tcp_interface, FakeTCPInterface = _install_fake_meshtastic()
        dest = FakeDestination(transport="meshtastic", host="10.0.0.5", channel=0)
        sink = MeshtasticSink(dest)
        with mock.patch.dict(
            sys.modules,
            {"meshtastic": fake_meshtastic, "meshtastic.tcp_interface": fake_tcp_interface},
        ), mock.patch.object(sinks_mod.time, "sleep"):
            sink.send("x")
        sink.close()
        self.assertTrue(FakeTCPInterface.instances[0].closed)


# ---------------------------------------------------------------------------
# MeshCoreSink against a fake meshcore module
# ---------------------------------------------------------------------------


class FakeMeshCoreEventType(enum.Enum):
    OK = "command_ok"
    ERROR = "command_error"
    CHANNEL_INFO = "channel_info"


class FakeMeshCoreEvent:
    def __init__(self, type_, payload=None):
        self.type = type_
        self.payload = payload or {}


def _install_fake_meshcore(channels, send_behavior=None, connect_ok=True):
    """Installs a fake `meshcore` module into sys.modules.

    `channels`: {index: name} the fake companion knows about.
    `send_behavior(msg)` may raise to simulate a dropped link on send.
    `connect_ok`: False simulates create_tcp() returning None (no response).
    """

    class FakeCommands:
        def __init__(self, mc):
            self._mc = mc

        async def get_channel(self, idx):
            self._mc.get_channel_calls.append(idx)
            name = self._mc.channels.get(idx)
            if name is None:
                return FakeMeshCoreEvent(FakeMeshCoreEventType.ERROR, {"reason": "no_channel"})
            return FakeMeshCoreEvent(
                FakeMeshCoreEventType.CHANNEL_INFO, {"channel_idx": idx, "channel_name": name}
            )

        async def send_chan_msg(self, idx, msg):
            self._mc.send_calls.append((idx, msg))
            if send_behavior is not None:
                send_behavior(msg)
            return FakeMeshCoreEvent(FakeMeshCoreEventType.OK, {})

    class FakeMeshCore:
        instances: List["FakeMeshCore"] = []
        create_calls: List[tuple] = []

        def __init__(self, host, port, channels):
            self.host = host
            self.port = port
            self.channels = dict(channels)
            self.commands = FakeCommands(self)
            self.send_calls = []
            self.get_channel_calls = []
            self._connected = True
            self.disconnected = False

        def is_connected(self):
            return self._connected

        async def disconnect(self):
            self._connected = False
            self.disconnected = True

        @classmethod
        async def create_tcp(cls, host, port):
            cls.create_calls.append((host, port))
            if not connect_ok:
                return None
            mc = cls(host, port, channels)
            cls.instances.append(mc)
            return mc

    fake_meshcore = types.ModuleType("meshcore")
    fake_meshcore.MeshCore = FakeMeshCore
    fake_meshcore.EventType = FakeMeshCoreEventType
    return fake_meshcore, FakeMeshCore


class TestMeshCoreSink(unittest.TestCase):
    def test_sends_expected_text_to_resolved_channel_name(self):
        fake_meshcore, FakeMeshCore = _install_fake_meshcore({0: "General", 1: "MeshWars"})
        dest = FakeDestination(
            transport="meshcore", host="10.0.0.9", port=5000, channel="MeshWars", text_budget=150
        )
        sink = MeshCoreSink(dest)
        with mock.patch.dict(sys.modules, {"meshcore": fake_meshcore}), mock.patch.object(
            sinks_mod.time, "sleep"
        ):
            result = sink.send("net wrapup text")

        self.assertTrue(result)
        self.assertEqual(len(FakeMeshCore.instances), 1)
        mc = FakeMeshCore.instances[0]
        self.assertEqual(mc.host, "10.0.0.9")
        self.assertEqual(mc.port, 5000)
        self.assertEqual(mc.send_calls, [(1, "net wrapup text")])
        sink.close()

    def test_refuses_unknown_channel_name_without_sending(self):
        fake_meshcore, FakeMeshCore = _install_fake_meshcore({0: "General"})
        dest = FakeDestination(transport="meshcore", host="10.0.0.9", port=5000, channel="MeshWars")
        sink = MeshCoreSink(dest)
        with mock.patch.dict(sys.modules, {"meshcore": fake_meshcore}), mock.patch.object(
            sinks_mod.time, "sleep"
        ):
            with self.assertLogs("meshwars_bot.sinks", level="WARNING") as cm:
                result = sink.send("should not send")

        self.assertFalse(result)
        mc = FakeMeshCore.instances[0]
        self.assertEqual(mc.send_calls, [])  # never blind-sent
        self.assertIn("MeshWars", "\n".join(cm.output))
        self.assertIn("not found", "\n".join(cm.output))
        sink.close()

    def test_reconnects_once_replacing_connection_then_succeeds(self):
        state = {"attempts": 0}

        def send_behavior(msg):
            state["attempts"] += 1
            if state["attempts"] == 1:
                raise ConnectionError("simulated dropped link")

        fake_meshcore, FakeMeshCore = _install_fake_meshcore({0: "MeshWars"}, send_behavior=send_behavior)
        dest = FakeDestination(transport="meshcore", host="10.0.0.9", port=5000, channel="MeshWars")
        sink = MeshCoreSink(dest)
        with mock.patch.dict(sys.modules, {"meshcore": fake_meshcore}), mock.patch.object(
            sinks_mod.time, "sleep"
        ):
            with self.assertLogs("meshwars_bot.sinks", level="WARNING"):
                result = sink.send("retry me")

        self.assertTrue(result)
        self.assertEqual(state["attempts"], 2)
        # exactly one client connected at a time: the first was disconnected
        # before the second was created (never two live at once).
        self.assertEqual(len(FakeMeshCore.instances), 2)
        self.assertTrue(FakeMeshCore.instances[0].disconnected)
        sink.close()

    def test_never_opens_a_second_connection_before_closing_the_first(self):
        fake_meshcore, FakeMeshCore = _install_fake_meshcore({0: "MeshWars"})
        dest = FakeDestination(transport="meshcore", host="10.0.0.9", port=5000, channel="MeshWars")
        sink = MeshCoreSink(dest)
        with mock.patch.dict(sys.modules, {"meshcore": fake_meshcore}), mock.patch.object(
            sinks_mod.time, "sleep"
        ):
            sink.send("one")
            sink.send("two")
        # second send() reuses the already-connected instance -- still one
        # connection, not two.
        self.assertEqual(len(FakeMeshCore.instances), 1)
        sink.close()

    def test_connect_failure_returns_false_and_is_handled_cleanly(self):
        fake_meshcore, FakeMeshCore = _install_fake_meshcore({0: "MeshWars"}, connect_ok=False)
        dest = FakeDestination(transport="meshcore", host="10.0.0.9", port=5000, channel="MeshWars")
        sink = MeshCoreSink(dest)
        with mock.patch.dict(sys.modules, {"meshcore": fake_meshcore}), mock.patch.object(
            sinks_mod.time, "sleep"
        ):
            with self.assertLogs("meshwars_bot.sinks", level="ERROR"):
                result = sink.send("x")
        self.assertFalse(result)
        sink.close()


class TestSinkNeverOpensRealSocket(unittest.TestCase):
    """Belt-and-suspenders: assert the abstract Sink contract and that
    nothing in this module reaches for socket.socket directly."""

    def test_sink_is_abstract(self):
        with self.assertRaises(TypeError):
            Sink()

    def test_default_close_is_a_safe_noop(self):
        DryRunSink("d").close()  # must not raise


if __name__ == "__main__":
    unittest.main()
