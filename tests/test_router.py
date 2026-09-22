import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot.config import Destination
from meshwars_bot.feed import Announcement
from meshwars_bot.router import should_relay
from meshwars_bot.state import State


def make_destination(**overrides) -> Destination:
    defaults = dict(
        name="mwmesh-mc",
        transport="meshcore",
        host="127.0.0.1",
        port=5000,
        channel="#meshwars",
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


class TestShouldRelay(unittest.TestCase):
    def setUp(self):
        self.state = State()

    def test_relays_when_all_rules_pass(self):
        dest = make_destination()
        ann = make_announcement()
        self.assertTrue(should_relay(ann, dest, self.state))

    def test_blocks_when_kind_not_in_destination_kinds(self):
        dest = make_destination(kinds=["net_wrapup"])
        ann = make_announcement(kind="daily_recap")
        self.assertFalse(should_relay(ann, dest, self.state))

    def test_blocks_when_board_does_not_match(self):
        dest = make_destination(board="mt")
        ann = make_announcement(board="mc")
        self.assertFalse(should_relay(ann, dest, self.state))

    def test_board_alias_forms_still_match(self):
        # destination configured with canonical "mc"; announcement board
        # arrives as the alias "meshcore" -- should still match.
        dest = make_destination(board="mc")
        ann = make_announcement(board="meshcore")
        self.assertTrue(should_relay(ann, dest, self.state))

    def test_net_announcement_blocked_when_net_id_not_in_destination_net_ids(self):
        dest = make_destination(net_ids=[1, 2])
        ann = make_announcement(net_id=3)
        self.assertFalse(should_relay(ann, dest, self.state))

    def test_net_announcement_allowed_when_net_id_in_destination_net_ids(self):
        dest = make_destination(net_ids=[1, 2])
        ann = make_announcement(net_id=2)
        self.assertTrue(should_relay(ann, dest, self.state))

    def test_empty_net_ids_blocks_all_net_announcements(self):
        dest = make_destination(net_ids=[])
        ann = make_announcement(net_id=1)
        self.assertFalse(should_relay(ann, dest, self.state))

    def test_non_net_announcement_ignores_net_ids_entirely(self):
        dest = make_destination(net_ids=[])
        ann = make_announcement(net_id=None)
        self.assertTrue(should_relay(ann, dest, self.state))

    def test_already_sent_announcement_is_never_resent(self):
        dest = make_destination()
        ann = make_announcement(id=42)
        self.state.mark_sent(dest.name, 42)
        self.assertFalse(should_relay(ann, dest, self.state))

    def test_already_sent_to_other_destination_does_not_block_this_one(self):
        dest = make_destination(name="other-dest")
        ann = make_announcement(id=42)
        self.state.mark_sent("mwmesh-mc", 42)
        self.assertTrue(should_relay(ann, dest, self.state))

    # -- BUG 2: net_wrapup with a null net_id must not bypass the allowlist --

    def test_net_wrapup_with_null_net_id_is_never_relayed_even_with_open_net_ids(self):
        # Regression for the exact bypass: a net_wrapup with net_id: null
        # used to skip net-ID gating entirely (since the old check only
        # fired when announcement.net_id was set) and reach every
        # destination. It must now be dropped for everyone, fail closed,
        # even when the destination's net_ids would otherwise be generous.
        dest = make_destination(kinds=["net_wrapup"], net_ids=[1, 2, 3])
        ann = make_announcement(kind="net_wrapup", net_id=None)
        self.assertFalse(should_relay(ann, dest, self.state))

    def test_net_wrapup_with_null_net_id_dropped_even_with_empty_net_ids(self):
        dest = make_destination(kinds=["net_wrapup"], net_ids=[])
        ann = make_announcement(kind="net_wrapup", net_id=None)
        self.assertFalse(should_relay(ann, dest, self.state))

    def test_net_wrapup_with_net_id_not_in_destination_net_ids_is_not_relayed(self):
        dest = make_destination(kinds=["net_wrapup"], net_ids=[1, 2])
        ann = make_announcement(kind="net_wrapup", net_id=99)
        self.assertFalse(should_relay(ann, dest, self.state))

    def test_net_wrapup_with_net_id_in_destination_net_ids_is_relayed(self):
        dest = make_destination(kinds=["net_wrapup"], net_ids=[1, 2])
        ann = make_announcement(kind="net_wrapup", net_id=2)
        self.assertTrue(should_relay(ann, dest, self.state))


if __name__ == "__main__":
    unittest.main()
