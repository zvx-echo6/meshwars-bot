"""Per-destination send-window scheduling.

The MeshWars feed publishes an announcement the instant its period closes
(a daily recap lands right after THAT OPERATOR's local midnight) -- correct
for a public feed consumed by meshes in other timezones, which must not
inherit this operator's clock, but a strange hour to key up a radio. This
bot polls the feed and decides for itself when to actually send: this
module answers exactly one question, at send/drain time -- is `now`, in a
given destination's own local time, inside that destination's configured
[send_after, send_before] window?

Validation of the raw `send_after` / `send_before` / `timezone` config
values lives in config.py, alongside every other destination field's
validation and where ConfigError already lives -- by the time a
Destination reaches this module, those three fields are either None or
already known-good (a valid "HH:MM" string / a valid IANA zone name).
"""

import datetime
from typing import Optional

from zoneinfo import ZoneInfo


def has_window(destination) -> bool:
    """True if `destination` has a send window configured at all (either
    bound present). No window -> always "inside" -- today's unchanged
    immediate-relay behaviour."""
    return destination.send_after is not None or destination.send_before is not None


def _hhmm_to_time(value: str) -> datetime.time:
    hour_str, _, minute_str = value.partition(":")
    return datetime.time(int(hour_str), int(minute_str))


def _local_time(destination, now: datetime.datetime) -> datetime.time:
    """`now` (a timezone-aware datetime) converted to `destination`'s local
    time: its configured `timezone`, or the host's local timezone when
    unset. Resolved fresh from `now` every call (never cached) so DST
    transitions are handled correctly regardless of when this runs."""
    if destination.timezone:
        return now.astimezone(ZoneInfo(destination.timezone)).time()
    return now.astimezone().time()


def in_window(destination, now: Optional[datetime.datetime] = None) -> bool:
    """True if `now` falls inside `destination`'s configured send window.

    No window configured (`send_after` and `send_before` both absent) ->
    always True -- unchanged default behaviour, relay immediately.

    Only one bound configured -> the other is treated as fully open:
    `send_after` alone admits anything from then through the end of the
    day; `send_before` alone admits anything from the start of the day
    through then.

    `send_after > send_before` means the window WRAPS midnight (e.g.
    send_after="22:00", send_before="06:00") -- admits times >= send_after
    OR <= send_before, instead of the normal inclusive range check.

    Both bounds are inclusive (send_before="22:00" still admits exactly
    22:00:00).
    """
    if not has_window(destination):
        return True

    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    after = _hhmm_to_time(destination.send_after) if destination.send_after else datetime.time(0, 0)
    before = (
        _hhmm_to_time(destination.send_before)
        if destination.send_before
        else datetime.time(23, 59, 59, 999999)
    )
    current = _local_time(destination, now)

    if after <= before:
        return after <= current <= before
    return current >= after or current <= before
