"""Message sinks: where a routed announcement's text actually goes.

DryRunSink is the safe default and is implemented with no radio library at
all -- it never opens a socket. MeshtasticSink and MeshCoreSink are the two
real (transmitting) sinks; both broadcast-only (one channel, one method:
`send(text) -> bool`), both lazy-import their radio library (so a dry-run-only
user, or the test suite on system python3, never needs meshtastic/meshcore
installed), and both share pacing + reconnect-once-then-fail discipline via
`_PacedRealSink` below.

make_sink() always checks `destination.dry_run` FIRST, before anything
touches a transport -- that check alone decides whether a real library is
ever imported.
"""

import datetime
import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger("meshwars_bot.sinks")

# Every destination's DryRunSink writes to this same fixed path (make_sink()
# below never overrides it) -- named here, rather than left as a bare
# default-argument literal, so webui.py's dry-run log tail can point at the
# exact same file without guessing or duplicating the string.
DEFAULT_DRYRUN_LOG_PATH = "./meshwars-bot-dryrun.log"

# meshtastic.tcp_interface.TCPInterface's own default port.
DEFAULT_MESHTASTIC_PORT = 4403

# Pacing: the minimum gap enforced between two sends on the SAME destination
# (i.e. the same Sink instance). Jittered rather than fixed so a burst of
# announcements doesn't hammer a shared LoRa channel on a robotic cadence.
# PACING_HARD_FLOOR_SECONDS is a defensive absolute minimum: whatever the
# jittered value computes to, the enforced gap can never fall below it.
PACING_JITTER_MIN_SECONDS = 2.2
PACING_JITTER_MAX_SECONDS = 2.6
PACING_HARD_FLOOR_SECONDS = 0.25

# How many channel indices MeshCoreSink will query when resolving a channel
# NAME to the companion's channel index. Generous for typical MeshCore
# companion channel counts; a scan (not a single lookup) is required because
# the companion protocol has no "list channels by name" command -- only
# get_channel(idx). Bump this if a real deployment configures more channels
# than fit in this range.
MESHCORE_MAX_CHANNEL_SCAN = 16


class Sink(ABC):
    """Something a routed announcement's text can be sent to."""

    @abstractmethod
    def send(self, text: str) -> bool:
        """Send `text`. Returns True on success, False on failure. Never raises
        for an ordinary delivery failure."""
        raise NotImplementedError

    def close(self) -> None:
        """Best-effort clean shutdown of any held connection.

        Default no-op -- DryRunSink has nothing to close. Real sinks
        override this to close their transport cleanly.
        """
        return None


class DryRunSink(Sink):
    """Writes the message to a local log file and stdout.

    NEVER opens a socket or any other network connection -- this is the only
    sink guaranteed to work with no radio library installed, and the only
    one make_sink() may construct before checking `dry_run`.
    """

    def __init__(self, destination_name: str, log_path: str = DEFAULT_DRYRUN_LOG_PATH):
        self.destination_name = destination_name
        self.log_path = log_path

    def send(self, text: str) -> bool:
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        header = f"[{timestamp}] [{self.destination_name}]"

        # MeshWars announcements can now be multi-line (app/mesh_render.py's
        # block format on the meshwars-dev side) -- ONE record still, never
        # fragmented across radio packets (that rule lives on the transmit
        # side; this is purely a log presentation concern). A bare `text`
        # embedded verbatim would put each of its own lines on its own
        # physical log line with no marker at all, indistinguishable from
        # the NEXT send()'s entry once webui.py's _tail_lines()/the /api/log
        # pane reads the file back line-by-line. Continuation lines are
        # indented with a "    | " marker instead, so a multi-line entry
        # stays visually one record in both a raw `tail` of the file and
        # the web UI's log pane (which just joins the returned lines with
        # "\n" into a <pre> block and displays them verbatim).
        lines = text.split("\n")
        if len(lines) == 1:
            out = f"{header} {text}"
        else:
            out = "\n".join([f"{header} {lines[0]}"] + [f"    | {cont}" for cont in lines[1:]])

        print(out)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(out + "\n")
        return True


def _fits_budget(text: str, budget: int) -> bool:
    """True if `text` encoded as UTF-8 is within `budget` bytes.

    main.py already checks this before calling a sink; this is the sink's
    OWN last line of defence -- an over-budget string must be refused, never
    truncated and never fragmented (one packet per announcement is a rule of
    this project), even if a future caller forgets main.py's check.
    """
    return len(text.encode("utf-8")) <= budget


class _PacedRealSink(Sink):
    """Shared machinery for the real (transmitting) sinks: byte-budget
    last-line-of-defence, jittered inter-send pacing, and
    reconnect-once-then-fail -- so MeshtasticSink and MeshCoreSink only need
    to implement the transport-specific pieces.

    Subclasses implement:
      - `_precheck() -> bool`: optional, cheap, connection-independent
        validation that should fail fast -- no pacing, no connect attempt,
        no retry -- logging its own reason and returning False on failure.
        Default: always True (nothing to precheck).
      - `_ensure_connected()`: make sure a live connection exists, opening
        one if not. May raise.
      - `_reconnect()`: tear down and re-establish the connection. May raise.
      - `_transmit(text) -> bool`: actually send `text` over the live
        connection. Return True/False for a DEFINITIVE outcome (no retry
        will help, e.g. MeshCoreSink's unresolved-channel-name case); RAISE
        for a transport-level failure worth reconnecting and retrying for.
    """

    def __init__(self, destination):
        self.destination = destination
        self.name = destination.name
        self.host = destination.host
        self.port = destination.port
        self.channel = destination.channel
        self.text_budget = destination.text_budget
        self._last_send_at: Optional[float] = None

    # ---- pacing -----------------------------------------------------------
    def _pace(self) -> None:
        gap = max(
            PACING_HARD_FLOOR_SECONDS,
            random.uniform(PACING_JITTER_MIN_SECONDS, PACING_JITTER_MAX_SECONDS),
        )
        if self._last_send_at is not None:
            remaining = gap - (time.monotonic() - self._last_send_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_send_at = time.monotonic()

    # ---- subclass hooks -----------------------------------------------------
    def _precheck(self) -> bool:
        return True

    def _ensure_connected(self) -> None:
        raise NotImplementedError

    def _reconnect(self) -> None:
        raise NotImplementedError

    def _transmit(self, text: str) -> bool:
        raise NotImplementedError

    # ---- public API ---------------------------------------------------------
    def send(self, text: str) -> bool:
        try:
            if not _fits_budget(text, self.text_budget):
                logger.error(
                    "refusing to send to destination=%s: text is %d bytes, over "
                    "its text_budget of %d bytes -- refusing to send an "
                    "over-budget packet (never truncated, never fragmented)",
                    self.name,
                    len(text.encode("utf-8")),
                    self.text_budget,
                )
                return False

            if not self._precheck():
                return False

            self._pace()

            try:
                self._ensure_connected()
                return bool(self._transmit(text))
            except Exception as e:  # noqa: BLE001 - transport failure, try once more
                logger.warning(
                    "send to destination=%s failed (%s); reconnecting and retrying once",
                    self.name,
                    e,
                )
                try:
                    self._reconnect()
                    return bool(self._transmit(text))
                except Exception as e2:  # noqa: BLE001 - give up, never raise into caller
                    logger.error(
                        "send to destination=%s failed again after reconnect; giving up: %s",
                        self.name,
                        e2,
                    )
                    return False
        except Exception:  # noqa: BLE001 - absolute backstop: send() never raises
            logger.exception("unexpected error sending to destination=%s", self.name)
            return False


def _parse_meshtastic_channel_index(channel: Any, dest_name: str) -> int:
    """`channel` is a channel INDEX for meshtastic: an int, or a string of
    digits. Anything else is a clear, named config error."""
    if isinstance(channel, bool):
        raise ValueError(
            f"meshtastic destination '{dest_name}': channel must be an integer "
            f"index or a digit string, got a boolean ({channel!r})"
        )
    if isinstance(channel, int):
        return channel
    if isinstance(channel, str) and channel.strip().isdigit():
        return int(channel.strip())
    raise ValueError(
        f"meshtastic destination '{dest_name}': channel must be an integer "
        f'index (e.g. 0) or a digit string (e.g. "0"), got {channel!r}'
    )


class MeshtasticSink(_PacedRealSink):
    """Broadcasts to one Meshtastic channel over TCP.

    `channel` is a channel INDEX, not a name. `host`/`port` connect to the
    node's TCP API (default port 4403 when `port` is unset).
    """

    def __init__(self, destination):
        super().__init__(destination)
        self._interface = None
        self._channel_index: Optional[int] = None

    def _precheck(self) -> bool:
        try:
            self._channel_index = _parse_meshtastic_channel_index(self.channel, self.name)
            return True
        except ValueError as e:
            logger.error(str(e))
            return False

    def _connect(self) -> None:
        try:
            import meshtastic.tcp_interface  # lazy: never imported for a dry-run destination
        except ImportError as e:
            raise RuntimeError(
                "the 'meshtastic' package is not installed (pip install meshtastic) "
                "-- required for a non-dry-run meshtastic destination"
            ) from e

        if not self.host:
            raise RuntimeError(f"meshtastic destination '{self.name}' is missing 'host'")
        port = self.port if self.port is not None else DEFAULT_MESHTASTIC_PORT
        self._interface = meshtastic.tcp_interface.TCPInterface(hostname=self.host, portNumber=port)

    def _ensure_connected(self) -> None:
        if self._interface is None:
            self._connect()

    def _reconnect(self) -> None:
        self.close()
        self._connect()

    def _transmit(self, text: str) -> bool:
        self._interface.sendText(text=text, channelIndex=self._channel_index)
        return True

    def close(self) -> None:
        if self._interface is not None:
            try:
                self._interface.close()
            except Exception:  # noqa: BLE001 - best-effort shutdown, never raise
                logger.warning(
                    "error closing meshtastic interface for destination=%s", self.name, exc_info=True
                )
            self._interface = None


class MeshCoreSink(_PacedRealSink):
    """Broadcasts to one MeshCore channel (by NAME) over TCP to a companion.

    The `meshcore` library is async; this sink owns its own event loop
    (never assumes an ambient running one) and bridges every call through
    it. A MeshCore companion port typically accepts exactly ONE client, so
    reconnecting REPLACES the existing connection rather than adding a
    second one -- `_connect()` always disconnects any prior connection
    first.
    """

    def __init__(self, destination):
        super().__init__(destination)
        self._mc = None
        self._loop = None
        self._resolved_channel_index: Optional[int] = None

    # ---- event loop plumbing ----------------------------------------------
    def _ensure_loop(self):
        import asyncio

        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run(self, coro):
        return self._ensure_loop().run_until_complete(coro)

    # ---- connection ---------------------------------------------------------
    def _connect(self) -> None:
        try:
            from meshcore import MeshCore  # lazy: never imported for a dry-run destination
        except ImportError as e:
            raise RuntimeError(
                "the 'meshcore' package is not installed (pip install meshcore) "
                "-- required for a non-dry-run meshcore destination"
            ) from e

        if not self.host or not self.port:
            raise RuntimeError(f"meshcore destination '{self.name}' is missing 'host'/'port'")

        # Only ONE client at a time on a MeshCore companion port -- always
        # drop any existing connection before opening a new one.
        self._disconnect_existing()
        self._ensure_loop()
        mc = self._run(MeshCore.create_tcp(self.host, self.port))
        if mc is None:
            raise RuntimeError(
                f"meshcore companion at {self.host}:{self.port} did not respond to connect"
            )
        self._mc = mc
        self._resolved_channel_index = None  # re-resolve against the new connection

    def _disconnect_existing(self) -> None:
        if self._mc is not None:
            try:
                self._run(self._mc.disconnect())
            except Exception:  # noqa: BLE001 - best-effort, we're replacing it anyway
                pass
            self._mc = None

    def _ensure_connected(self) -> None:
        connected = False
        if self._mc is not None:
            try:
                connected = bool(self._mc.is_connected())
            except Exception:  # noqa: BLE001 - treat as not connected
                connected = False
        if not connected:
            self._connect()

    def _reconnect(self) -> None:
        self._connect()

    # ---- channel name -> index -----------------------------------------------
    def _resolve_channel_index(self) -> Optional[int]:
        if self._resolved_channel_index is not None:
            return self._resolved_channel_index

        from meshcore import EventType  # already imported successfully in _connect()

        for idx in range(MESHCORE_MAX_CHANNEL_SCAN):
            event = self._run(self._mc.commands.get_channel(idx))
            if event is None or getattr(event, "type", None) != EventType.CHANNEL_INFO:
                continue
            payload = event.payload or {}
            if payload.get("channel_name") == self.channel:
                self._resolved_channel_index = idx
                return idx
        return None

    def _transmit(self, text: str) -> bool:
        from meshcore import EventType  # already imported successfully in _connect()

        idx = self._resolve_channel_index()
        if idx is None:
            logger.warning(
                "meshcore channel %r not found on companion for destination=%s "
                "-- refusing to blind-send to an unresolved channel name",
                self.channel,
                self.name,
            )
            return False

        event = self._run(self._mc.commands.send_chan_msg(idx, text))
        if event is None or getattr(event, "type", None) != EventType.OK:
            raise RuntimeError(f"meshcore send_chan_msg did not confirm delivery (event={event!r})")
        return True

    def close(self) -> None:
        self._disconnect_existing()
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001 - best-effort shutdown, never raise
                logger.warning("error closing meshcore event loop for destination=%s", self.name, exc_info=True)
        self._loop = None


def make_sink(destination) -> Sink:
    """Build the Sink for a destination.

    destination.dry_run == True (the default) -> DryRunSink, checked FIRST
    and before anything else touches a transport or imports a radio library.
    Otherwise, the real sink matching destination.transport.
    """
    if destination.dry_run:
        return DryRunSink(destination_name=destination.name)
    if destination.transport == "meshtastic":
        return MeshtasticSink(destination)
    if destination.transport == "meshcore":
        return MeshCoreSink(destination)
    raise NotImplementedError(
        f"no real transport implemented for destination '{destination.name}' "
        f"(transport={destination.transport!r})"
    )
