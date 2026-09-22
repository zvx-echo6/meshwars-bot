"""Durable JSON state for meshwars-bot: the feed cursor, the ETag, the set
of (destination_name, announcement_id) pairs already sent, and each
destination's PENDING list -- announcements already fetched (past the
cursor, so the feed will never hand them back) but not yet relayed because
the destination is currently outside its configured send window.

State file layout (JSON):
    {
      "version": 1,
      "since": <int|null>,
      "etag": <str|null>,
      "sent": ["<destination_name>:<announcement_id>", ...],
      "pending": {
        "<destination_name>": [{"id": <int>, "text": <str>, "kind": <str|null>}, ...]
      }
    }

`pending` entries are stored in the order they should be relayed (oldest
first) -- see main.py's drain phase, which additionally sorts by id
defensively before sending.

Writes are atomic: a temp file is written in the same directory as the state
file and then moved into place with os.replace(), which is atomic on the
same filesystem. A crash mid-write leaves either the old state file intact
or the new one fully written -- never a half-written, corrupt file.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("meshwars_bot.state")

STATE_VERSION = 1

# Cap on each destination's durable pending list. Chosen generously for the
# intended dominant case -- a destination held outside its window for at
# most a day at a time carries at most a handful of items -- while still
# bounding the state file if a destination is left permanently
# misconfigured outside its window (e.g. a window that can never be
# reached, or a destination nobody is watching). 50 items at a ~1000-byte
# text_budget worst case is ~50KB per destination, nothing to worry about.
# When the cap is hit, the OLDEST held item is dropped and a warning is
# logged -- stale news is exactly the thing worth losing, never the newest.
DEFAULT_PENDING_CAP = 50


@dataclass
class PendingItem:
    """One announcement queued for a destination but not yet sent -- either
    because should_relay() just approved it this cycle and the destination
    is outside its window, or because it was already queued in a previous
    cycle and is still waiting. `kind` is carried along only for nicer
    logging; `id` and `text` are the fields that actually matter."""

    id: int
    text: str
    kind: Optional[str] = None


@dataclass
class State:
    since: Optional[int] = None
    etag: Optional[str] = None
    sent: Set[Tuple[str, int]] = field(default_factory=set)
    pending: Dict[str, List[PendingItem]] = field(default_factory=dict)

    def has_sent(self, destination_name: str, announcement_id: int) -> bool:
        return (destination_name, announcement_id) in self.sent

    def mark_sent(self, destination_name: str, announcement_id: int) -> None:
        self.sent.add((destination_name, announcement_id))

    def add_pending(
        self, destination_name: str, item: PendingItem, cap: int = DEFAULT_PENDING_CAP
    ) -> None:
        """Queue `item` for `destination_name`, enforcing `cap`: once the
        list exceeds it, the OLDEST item(s) are dropped (never the one
        just added) and a warning is logged naming what was lost."""
        lst = self.pending.setdefault(destination_name, [])
        lst.append(item)
        while len(lst) > cap:
            dropped = lst.pop(0)
            logger.warning(
                "pending queue for destination=%s exceeded cap of %d; dropping oldest "
                "held announcement id=%s (kind=%s) to keep the state file bounded",
                destination_name,
                cap,
                dropped.id,
                dropped.kind,
            )

    def remove_pending(self, destination_name: str, announcement_id: int) -> None:
        """Remove one item (by id) from `destination_name`'s pending list,
        e.g. once it has actually been sent. A no-op if it isn't there."""
        lst = self.pending.get(destination_name)
        if not lst:
            return
        remaining = [p for p in lst if p.id != announcement_id]
        if remaining:
            self.pending[destination_name] = remaining
        else:
            del self.pending[destination_name]


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
    pending: Dict[str, List[PendingItem]] = {}
    for name, items in (raw.get("pending") or {}).items():
        parsed = [
            PendingItem(id=int(it["id"]), text=it.get("text", ""), kind=it.get("kind"))
            for it in items
        ]
        if parsed:
            pending[name] = parsed
    return State(since=raw.get("since"), etag=raw.get("etag"), sent=sent, pending=pending)


def save_state(state_path: str, state: State) -> None:
    """Atomically persist `state` to `state_path` (write temp file + os.replace)."""
    payload = {
        "version": STATE_VERSION,
        "since": state.since,
        "etag": state.etag,
        "sent": sorted(f"{name}:{ann_id}" for (name, ann_id) in state.sent),
        "pending": {
            name: [{"id": item.id, "text": item.text, "kind": item.kind} for item in items]
            for name, items in state.pending.items()
            if items
        },
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
