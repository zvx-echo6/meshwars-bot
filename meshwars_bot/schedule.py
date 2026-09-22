"""Per-destination send-window scheduling, plus per-kind scheduled send
times.

The MeshWars feed publishes an announcement the instant its period closes
(a daily recap lands right after THAT OPERATOR's local midnight) -- correct
for a public feed consumed by meshes in other timezones, which must not
inherit this operator's clock, but a strange hour to key up a radio. This
bot polls the feed and decides for itself when to actually send. This
module answers two related but separate questions, both at send/drain time:

  - in_window(): is `now`, in a given destination's own local time, inside
    that destination's configured [send_after, send_before] coarse quiet-
    hours window? One window per destination, applying equally to every
    announcement kind.
  - is_kind_due(): has `now`, in local time, reached a given KIND's own
    configured scheduled clock time for that destination (Destination.
    schedule)? A finer-grained, per-kind, optional setting -- most kinds on
    most destinations have none, and are always "due".

PRECEDENCE, when both apply to the same item: a kind's scheduled time
decides WHEN it becomes due; the coarser window, if also configured, can
still hold it further (it must ALSO be in_window() for the item to
actually go out) -- see main.py's drain phase, which checks both. If the
two are configured such that they can never simultaneously admit the same
moment (the kind's scheduled clock time itself falls outside the window),
that is a misconfiguration: the window wins and the item is held forever,
never a silent immediate-send -- see schedule_conflicts_with_window()
below, and main.py's once-per-destination+kind warning for it.

Validation of the raw `send_after` / `send_before` / `timezone` /
`schedule` config values lives in config.py, alongside every other
destination field's validation and where ConfigError already lives -- by
the time a Destination reaches this module, those fields are either
None/{} or already known-good (valid "HH:MM" strings / a valid IANA zone
name).
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


def _time_in_window(destination, current: datetime.time) -> bool:
    """The actual window-membership test, factored out of in_window() so
    schedule_conflicts_with_window() can ask the same question about a
    fixed clock time (a kind's scheduled send time) instead of "now" --
    see that function below.

    No window configured -> always True (see in_window()'s docstring for
    the one-bound-only and midnight-wrap rules; unchanged here).
    """
    if not has_window(destination):
        return True

    after = _hhmm_to_time(destination.send_after) if destination.send_after else datetime.time(0, 0)
    before = (
        _hhmm_to_time(destination.send_before)
        if destination.send_before
        else datetime.time(23, 59, 59, 999999)
    )

    if after <= before:
        return after <= current <= before
    return current >= after or current <= before


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

    return _time_in_window(destination, _local_time(destination, now))


def is_kind_due(destination, kind: Optional[str], now: Optional[datetime.datetime] = None) -> bool:
    """True if `kind`'s configured scheduled send time for `destination`
    (Destination.schedule[kind], see config.py's _validate_schedule) has
    been reached, in `destination`'s local time, as of `now`.

    `kind` absent from `destination.schedule` (including `kind=None`, and
    the always-true case of an empty `destination.schedule` entirely) ->
    always True -- send immediately on poll, exactly today's behaviour.

    Deliberately just a same-day clock-time comparison, with no memory of
    WHEN the item first arrived: an item queued well before its scheduled
    time is held (current local time < scheduled) until a later poll's
    local time reaches it; an item that only arrives (or is first checked)
    AFTER its scheduled time on the same day is immediately due -- current
    local time is already >= scheduled, so this returns True the very
    first time it's checked. That is the deliberate choice per this
    task's spec: being late is better than holding a whole extra day for
    the next occurrence, and it falls out of this function for free
    without tracking arrival time anywhere.
    """
    if not kind:
        return True
    hhmm = destination.schedule.get(kind) if destination.schedule else None
    if not hhmm:
        return True

    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    scheduled = _hhmm_to_time(hhmm)
    current = _local_time(destination, now)
    return current >= scheduled


def schedule_conflicts_with_window(destination, kind: Optional[str]) -> bool:
    """True if `kind` has a configured scheduled time for `destination`
    AND `destination` also has a coarser send window configured, AND that
    scheduled time -- as a fixed point in the day, independent of "now" --
    would never satisfy the window (see in_window()'s rules). This is a
    structural misconfiguration: whenever local time reaches the kind's
    scheduled time (is_kind_due() becomes True), in_window() at that same
    instant is guaranteed False, so the item can NEVER go out. The caller
    (main.py's drain phase) treats this as "the window wins, stays
    pending forever" and logs a warning once rather than silently holding
    it with no explanation.

    False whenever there's nothing to conflict: no schedule entry for this
    kind, or no window configured at all.
    """
    if not kind:
        return False
    hhmm = destination.schedule.get(kind) if destination.schedule else None
    if not hhmm or not has_window(destination):
        return False
    return not _time_in_window(destination, _hhmm_to_time(hhmm))
