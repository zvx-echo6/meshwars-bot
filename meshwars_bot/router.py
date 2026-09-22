"""Pure routing logic: decide whether an announcement should be relayed to a
destination. No I/O other than logging, no side effects other than reading
`state` -- easy to unit test in isolation.
"""

import logging

from .config import normalize_board

logger = logging.getLogger("meshwars_bot.router")

# The one announcement kind that is inherently tied to a specific net, even
# when the field carrying that tie (`net_id`) is missing/null on a given
# announcement. See the net-ID gating comment in should_relay() below.
NET_TIED_KIND = "net_wrapup"


def should_relay(announcement, destination, state) -> bool:
    """Return True if `announcement` should be sent to `destination` right now.

    Rules (all must hold):
    1. `destination.kinds` must contain `announcement.kind`.
    2. `destination.board` must match `announcement.board`.
    3. Net-ID gating applies whenever the announcement IS net-tied: its
       `kind` is "net_wrapup", OR it carries a non-null `net_id` (covers
       any other kind that might someday carry one too). Gating only on
       `announcement.net_id is not None` -- the old rule -- let a
       `net_wrapup` with a NULL `net_id` bypass the allowlist entirely and
       reach every destination, which is exactly the opt-in `net_ids`
       exists to prevent. So: a `net_wrapup` with no `net_id` at all is
       dropped for EVERYONE (fail closed, logged) rather than treated as
       "no net filtering applies." Otherwise, `destination.net_ids` must
       contain the announcement's `net_id`. An EMPTY `destination.net_ids`
       means NO net announcement ever reaches that destination -- this is
       an explicit opt-in list, not a wildcard.
    4. An announcement already recorded (in `state`) as sent to that
       destination is never resent.
    """
    if announcement.kind not in destination.kinds:
        return False

    if announcement.board is None or normalize_board(announcement.board) != destination.board:
        return False

    is_net_tied = announcement.kind == NET_TIED_KIND or announcement.net_id is not None
    if is_net_tied:
        if announcement.net_id is None:
            logger.error(
                "dropping %s id=%s for destination=%s: no net_id present, so it "
                "cannot be checked against any destination's net_ids allowlist "
                "-- failing closed instead of broadcasting to everyone",
                announcement.kind,
                announcement.id,
                destination.name,
            )
            return False
        if announcement.net_id not in destination.net_ids:
            return False

    if state.has_sent(destination.name, announcement.id):
        return False

    return True
