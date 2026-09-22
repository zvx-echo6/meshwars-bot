"""Message sinks: where a routed announcement's text actually goes.

Only DryRunSink is implemented in this repo. Real radio transports
(meshcore, meshtastic) are a LATER task, on purpose: this repo writes no
radio code and imports no radio library, so make_sink() raises
NotImplementedError for any non-dry-run destination rather than guessing at
a transport.
"""

import datetime
from abc import ABC, abstractmethod

# Every destination's DryRunSink writes to this same fixed path (make_sink()
# below never overrides it) -- named here, rather than left as a bare
# default-argument literal, so webui.py's dry-run log tail can point at the
# exact same file without guessing or duplicating the string.
DEFAULT_DRYRUN_LOG_PATH = "./meshwars-bot-dryrun.log"


class Sink(ABC):
    """Something a routed announcement's text can be sent to."""

    @abstractmethod
    def send(self, text: str) -> bool:
        """Send `text`. Returns True on success, False on failure. Never raises
        for an ordinary delivery failure."""
        raise NotImplementedError


class DryRunSink(Sink):
    """Writes the message to a local log file and stdout.

    NEVER opens a socket or any other network connection -- this is the only
    sink this repo implements, and it is required to stay that way until a
    later task adds real transports.
    """

    def __init__(self, destination_name: str, log_path: str = DEFAULT_DRYRUN_LOG_PATH):
        self.destination_name = destination_name
        self.log_path = log_path

    def send(self, text: str) -> bool:
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        line = f"[{timestamp}] [{self.destination_name}] {text}"
        print(line)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True


def make_sink(destination) -> Sink:
    """Build the Sink for a destination.

    destination.dry_run == True (the default) -> DryRunSink.
    Anything else -> NotImplementedError, deliberately: real transports
    (meshcore/meshtastic) land in a later task, not this one.
    """
    if destination.dry_run:
        return DryRunSink(destination_name=destination.name)
    raise NotImplementedError("real transports land in a later task")
