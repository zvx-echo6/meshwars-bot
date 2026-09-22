import datetime
import os
import sys
import unittest
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot import schedule


@dataclass
class FakeDestination:
    """Stand-in for config.Destination -- only the fields schedule.py reads."""

    send_after: Optional[str] = None
    send_before: Optional[str] = None
    timezone: Optional[str] = None


def _utc(hour, minute, month=6, day=15, year=2026):
    # June, to dodge Boise DST edge cases in tests that don't care about them.
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)


class TestHasWindow(unittest.TestCase):
    def test_no_bounds_has_no_window(self):
        self.assertFalse(schedule.has_window(FakeDestination()))

    def test_send_after_alone_has_a_window(self):
        self.assertTrue(schedule.has_window(FakeDestination(send_after="08:00")))

    def test_send_before_alone_has_a_window(self):
        self.assertTrue(schedule.has_window(FakeDestination(send_before="22:00")))


class TestInWindowNoWindowConfigured(unittest.TestCase):
    def test_always_true_regardless_of_time(self):
        dest = FakeDestination()
        self.assertTrue(schedule.in_window(dest, _utc(3, 0)))
        self.assertTrue(schedule.in_window(dest, _utc(23, 59)))


class TestInWindowSimpleRange(unittest.TestCase):
    """send_after < send_before -- the normal, non-wrapping case. UTC is
    used as the destination's timezone throughout so the boundary math is
    exact and independent of the host running the tests."""

    def _dest(self):
        return FakeDestination(send_after="08:00", send_before="22:00", timezone="UTC")

    def test_before_window_is_excluded(self):
        self.assertFalse(schedule.in_window(self._dest(), _utc(7, 59)))

    def test_at_lower_bound_is_included(self):
        self.assertTrue(schedule.in_window(self._dest(), _utc(8, 0)))

    def test_middle_of_window_is_included(self):
        self.assertTrue(schedule.in_window(self._dest(), _utc(12, 0)))

    def test_at_upper_bound_is_included(self):
        self.assertTrue(schedule.in_window(self._dest(), _utc(22, 0)))

    def test_after_window_is_excluded(self):
        self.assertFalse(schedule.in_window(self._dest(), _utc(22, 1)))


class TestInWindowWrapsMidnight(unittest.TestCase):
    """send_after > send_before -- the window wraps midnight, e.g.
    "after 22:00 or before 06:00"."""

    def _dest(self):
        return FakeDestination(send_after="22:00", send_before="06:00", timezone="UTC")

    def test_late_evening_is_admitted(self):
        self.assertTrue(schedule.in_window(self._dest(), _utc(23, 30)))

    def test_exactly_at_send_after_is_admitted(self):
        self.assertTrue(schedule.in_window(self._dest(), _utc(22, 0)))

    def test_early_morning_is_admitted(self):
        self.assertTrue(schedule.in_window(self._dest(), _utc(3, 0)))

    def test_exactly_at_send_before_is_admitted(self):
        self.assertTrue(schedule.in_window(self._dest(), _utc(6, 0)))

    def test_midday_is_excluded(self):
        self.assertFalse(schedule.in_window(self._dest(), _utc(12, 0)))

    def test_just_after_send_before_is_excluded(self):
        self.assertFalse(schedule.in_window(self._dest(), _utc(6, 1)))

    def test_just_before_send_after_is_excluded(self):
        self.assertFalse(schedule.in_window(self._dest(), _utc(21, 59)))


class TestInWindowOneBoundOnly(unittest.TestCase):
    def test_send_after_only_admits_rest_of_day(self):
        dest = FakeDestination(send_after="20:00", timezone="UTC")
        self.assertFalse(schedule.in_window(dest, _utc(19, 59)))
        self.assertTrue(schedule.in_window(dest, _utc(20, 0)))
        self.assertTrue(schedule.in_window(dest, _utc(23, 59)))

    def test_send_before_only_admits_start_of_day(self):
        dest = FakeDestination(send_before="06:00", timezone="UTC")
        self.assertTrue(schedule.in_window(dest, _utc(0, 0)))
        self.assertTrue(schedule.in_window(dest, _utc(6, 0)))
        self.assertFalse(schedule.in_window(dest, _utc(6, 1)))


class TestInWindowTimezone(unittest.TestCase):
    def test_destination_timezone_is_honoured_not_utc(self):
        # 08:00 America/Boise (Mountain, UTC-6 in June DST) is 14:00 UTC.
        dest = FakeDestination(send_after="08:00", send_before="22:00", timezone="America/Boise")
        self.assertFalse(schedule.in_window(dest, _utc(13, 59)))  # 07:59 Boise
        self.assertTrue(schedule.in_window(dest, _utc(14, 0)))  # 08:00 Boise

    def test_no_timezone_falls_back_to_host_local_time(self):
        # Can't assert a specific boundary without knowing the test host's
        # zone, but it must not raise and must return a bool either way.
        dest = FakeDestination(send_after="00:00", send_before="23:59")
        result = schedule.in_window(dest, _utc(12, 0))
        self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main()
