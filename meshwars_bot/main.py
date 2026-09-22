"""meshwars-bot entry point: poll the feed, route announcements to
destinations, send via sinks, persist state, sleep, repeat.
"""

import argparse
import logging
import sys
import time
from typing import Dict, List, Optional

from .config import Config, DEFAULT_TEXT_BUDGET, Destination, load_config
from .feed import FeedClient, FeedPage
from .router import should_relay
from .sinks import make_sink
from .state import State, fast_forward_on_first_run, load_state, save_state

logger = logging.getLogger("meshwars_bot.main")

DEFAULT_POLL_INTERVAL_SECONDS = 900


def _group_destinations_by_budget(config: Config) -> Dict[int, List[Destination]]:
    """Group destinations by their distinct `text_budget`.

    The feed fits `text` server-side to a single text_budget PER REQUEST, so
    the only way for every destination to receive text that actually fits
    its own budget is one feed request per distinct budget value, relaying
    each response only to the destinations that asked for that budget. This
    is also, directly, the number of feed requests spent per poll cycle --
    see the multi-budget rate-limit warning in config.py and the README.
    """
    groups: Dict[int, List[Destination]] = {}
    for destination in config.destinations:
        groups.setdefault(destination.text_budget, []).append(destination)
    if not groups:
        # No destinations configured -- still poll once at the default
        # budget so the cursor/state machinery keeps working.
        groups[DEFAULT_TEXT_BUDGET] = []
    return groups


def _fits_budget(text: str, budget: int) -> bool:
    """True if `text` encoded as UTF-8 is within `budget` bytes.

    The server is asked to fit `text` to a destination's budget, but this is
    the local, authoritative check before anything is handed to a sink --
    if it somehow doesn't fit (a server bug, a mismatch, anything), we must
    never send an over-budget packet."""
    return len(text.encode("utf-8")) <= budget


def run_once(config: Config, state: State, feed_client: FeedClient) -> int:
    """Run a single poll -> route -> send -> persist cycle.

    Makes ONE feed request per distinct `text_budget` configured across
    destinations, so each destination's text is fitted to the budget IT
    asked for -- never a sibling destination's larger budget. All requests
    in a cycle share the same `since`/`etag`, read once at the top of this
    function, and the persisted cursor only ever advances after every
    budget group has been fetched and routed (see the comment above the
    cursor-advance logic below for why).

    Returns the number of seconds the caller should wait before the next
    cycle (honouring the server's poll_interval_seconds -- the smallest one
    reported across groups -- or a 429's Retry-After, in preference to any
    local default).
    """
    budget_groups = _group_destinations_by_budget(config)

    sinks_cache = {}
    for destination in config.destinations:
        if destination.name not in sinks_cache:
            sinks_cache[destination.name] = make_sink(destination)

    # --- Fetch phase -----------------------------------------------------
    # `since`/`etag` are read ONCE, before any group is fetched, and every
    # group's request uses this same pair. If we instead advanced
    # `state.since` after each group's fetch, a later group in the SAME
    # cycle would be polling with a newer cursor than an earlier group used
    # -- and if that later group then failed, retrying it next cycle from
    # the *new* cursor would never ask the server for whatever announcements
    # existed in the gap. Fetching every group against one fixed, unmoved
    # cursor guarantees every group in a cycle sees exactly the same window.
    since = state.since
    etag = state.etag

    pages: Dict[int, Optional[FeedPage]] = {}
    for budget in budget_groups:
        page = feed_client.poll(since=since, etag=etag, text_budget=budget)
        pages[budget] = page
        if page is None:
            logger.warning("poll failed for text_budget=%s this cycle", budget)
        elif page.retry_after is not None:
            logger.info(
                "rate limited (429) for text_budget=%s; honouring Retry-After=%ss",
                budget,
                page.retry_after,
            )
        elif page.not_modified:
            logger.info("no new announcements for text_budget=%s (304 not modified)", budget)
        else:
            logger.info(
                "fetched %d announcement(s) for text_budget=%s",
                len(page.announcements),
                budget,
            )

    # --- Route phase -------------------------------------------------------
    # Each budget group's announcements are only ever offered to the
    # destinations that configured that exact budget.
    for budget, destinations in budget_groups.items():
        page = pages[budget]
        if page is None or not page.announcements:
            continue
        for announcement in page.announcements:
            for destination in destinations:
                if not should_relay(announcement, destination, state):
                    continue
                if not _fits_budget(announcement.text, destination.text_budget):
                    logger.error(
                        "dropping announcement id=%s -> destination=%s: text is "
                        "%d bytes, over its text_budget of %d bytes -- refusing "
                        "to send an over-budget packet",
                        announcement.id,
                        destination.name,
                        len(announcement.text.encode("utf-8")),
                        destination.text_budget,
                    )
                    continue
                sink = sinks_cache[destination.name]
                sent_ok = sink.send(announcement.text)
                if sent_ok:
                    state.mark_sent(destination.name, announcement.id)
                    logger.info(
                        "sent announcement id=%s kind=%s -> destination=%s",
                        announcement.id,
                        announcement.kind,
                        destination.name,
                    )
                else:
                    logger.warning(
                        "send failed for announcement id=%s -> destination=%s",
                        announcement.id,
                        destination.name,
                    )

    # --- Cursor / etag advance --------------------------------------------
    # Only advance the single shared cursor once EVERY budget group this
    # cycle succeeded (200 or 304). If any group failed outright or was
    # rate-limited, we deliberately leave `state.since`/`state.etag`
    # untouched: the next cycle will re-fetch every group from the exact
    # same `since`, so the failed group gets a real second chance to see
    # whatever it missed. This can never double-send, because any group
    # that DID succeed this cycle already called `state.mark_sent()` for
    # what it routed, and `should_relay()` skips already-sent
    # (destination, announcement_id) pairs on the retry. So: no group can
    # ever see a `since` that another group in the same cycle did not also
    # see, and a group that fails is retried, never skipped.
    any_failed = any(page is None for page in pages.values())
    retry_afters = [
        page.retry_after for page in pages.values() if page is not None and page.retry_after is not None
    ]

    if not any_failed and not retry_afters:
        next_sinces = {
            page.next_since for page in pages.values() if page.next_since is not None
        }
        if next_sinces:
            # All groups poll the same underlying `since`, so next_since
            # should agree across groups; take the max as a defensive
            # tie-breaker so the cursor can never move backwards.
            state.since = max(next_sinces)

        # Representations (and therefore ETags) can differ per text_budget,
        # so different groups' responses may carry different ETags. We keep
        # a single shared value, mirroring the single shared cursor, taking
        # the last group's ETag. Worst case a stale ETag doesn't match a
        # group's representation next cycle and that group gets a full 200
        # instead of a 304 -- never a missed announcement, since
        # correctness here depends on `since`, not on ETag.
        etags = [page.etag for page in pages.values() if page.etag is not None]
        if etags:
            state.etag = etags[-1]

    save_state(config.state_path, state)

    if retry_afters:
        return max(retry_afters)
    if any_failed:
        return DEFAULT_POLL_INTERVAL_SECONDS

    poll_intervals = [
        page.poll_interval_seconds for page in pages.values() if page.poll_interval_seconds
    ]
    return min(poll_intervals) if poll_intervals else DEFAULT_POLL_INTERVAL_SECONDS


def _load_or_fast_forward_state(config: Config, feed_client: FeedClient) -> Optional[State]:
    state = load_state(config.state_path)
    if state is not None:
        return state

    logger.info(
        "no state file at %s; this is a first run -- fast-forwarding "
        "(fetching current cursor, sending nothing)",
        config.state_path,
    )
    page = feed_client.poll(since=None, etag=None)
    if page is None or page.next_since is None:
        logger.error("fast-forward poll failed; refusing to start without a safe cursor")
        return None

    state = fast_forward_on_first_run(config.state_path, page.next_since)
    logger.info("fast-forwarded to since=%s; nothing sent this run", state.since)
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="meshwars-bot")
    parser.add_argument("--config", default="./config.yaml", help="path to config file")
    parser.add_argument(
        "--once", action="store_true", help="run a single poll/route/send cycle and exit"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error("failed to load config %s: %s", args.config, e)
        return 1

    feed_client = FeedClient(
        base_url=config.feed.base_url,
        api_key=config.feed.api_key,
        timeout_seconds=config.feed.timeout_seconds,
    )

    state = _load_or_fast_forward_state(config, feed_client)
    if state is None:
        return 1

    if args.once:
        run_once(config, state, feed_client)
        return 0

    while True:
        sleep_seconds = run_once(config, state, feed_client)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    sys.exit(main())
