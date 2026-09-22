"""Durable JSON state for meshwars-bot: the feed cursor, the ETag, and the set
of (destination_name, announcement_id) pairs already sent.

State file layout (JSON):
    {
      "version": 1,
      "since": <int|null>,
      "etag": <str|null>,
      "sent": ["<destination_name>:<announcement_id>", ...]
    }

Writes are atomic: a temp file is written in the same directory as the state
file and then moved into place with os.replace(), which is atomic on the
same filesystem. A crash mid-write leaves either the old state file intact
or the new one fully written -- never a half-written, corrupt file.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional, Set, Tuple

STATE_VERSION = 1


@dataclass
class State:
    since: Optional[int] = None
    etag: Optional[str] = None
    sent: Set[Tuple[str, int]] = field(default_factory=set)

    def has_sent(self, destination_name: str, announcement_id: int) -> bool:
        return (destination_name, announcement_id) in self.sent

    def mark_sent(self, destination_name: str, announcement_id: int) -> None:
        self.sent.add((destination_name, announcement_id))


def load_state(state_path: str) -> Optional[State]:
    """Load state from disk.

    Returns None if no state file exists yet -- the caller MUST treat that as
    a first run and go through fast_forward_on_first_run(), never by polling
    with since=None and then sending whatever comes back.
    """
    if not os.path.exists(state_path):
        return None
    with open(state_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    sent: Set[Tuple[str, int]] = set()
    for item in raw.get("sent", []):
        name, _, ann_id = item.rpartition(":")
        sent.add((name, int(ann_id)))
    return State(since=raw.get("since"), etag=raw.get("etag"), sent=sent)


def save_state(state_path: str, state: State) -> None:
    """Atomically persist `state` to `state_path` (write temp file + os.replace)."""
    payload = {
        "version": STATE_VERSION,
        "since": state.since,
        "etag": state.etag,
        "sent": sorted(f"{name}:{ann_id}" for (name, ann_id) in state.sent),
    }
    directory = os.path.dirname(os.path.abspath(state_path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".meshwars-bot-state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, state_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def fast_forward_on_first_run(state_path: str, current_next_since: int) -> State:
    """
    THE MOST IMPORTANT BEHAVIOUR IN THIS REPO.

    On first run -- no state file present -- the bot MUST NOT replay history
    onto a live radio channel. Instead it fast-forwards: fetch the current
    feed (a poll with since=None), take the `next_since` cursor it returns,
    persist that cursor as the new starting point, and send NOTHING for this
    cycle.

    This exact mistake -- a fresh install replaying days of old announcements
    in front of real users -- shipped once on a sibling integration. Any code
    path that creates a brand-new state file MUST go through this function
    rather than hand-rolling an initial State().
    """
    state = State(since=current_next_since, etag=None, sent=set())
    save_state(state_path, state)
    return state
