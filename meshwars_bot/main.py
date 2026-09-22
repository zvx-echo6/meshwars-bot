"""meshwars-bot entry point: poll the feed, route announcements to
destinations, send via sinks, persist state, sleep, repeat.
"""

import argparse
import datetime
import logging
import sys
import threading
import time
from collections import Counter
from typing import Dict, List, Optional

from .config import Config, DEFAULT_TEXT_BUDGET, Destination, load_config
from .feed import FeedClient, FeedPage
from .router import should_relay
from .sinks import make_sink
from .state import State, fast_forward_on_first_run, load_state, save_state
from .webui import BotStatus, start_webui

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


def run_once(
    config: Config, state: State, feed_client: FeedClient, status: Optional[BotStatus] = None
) -> int:
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

    `status`, when given, is updated with this cycle's outcome (cursor,
    timestamp, error, feed reachability, per-destination sent counts) for
    the web UI's /api/state route to read. Entirely optional -- run_once()
    must work exactly as before when no web UI is running.
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

    if status is not None:
        failed_budgets = sorted(b for b, p in pages.items() if p is None)
        status.update(
            cursor_since=state.since,
            last_poll_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            last_error=(f"poll failed for text_budget(s): {failed_budgets}" if any_failed else None),
            feed_reachable=not any_failed,
            sent_counts=dict(Counter(name for name, _ann_id in state.sent)),
        )

    if retry_afters:
        return max(retry_afters)
    if any_failed:
        return DEFAULT_POLL_INTERVAL_SECONDS

    poll_intervals = [
        page.poll_interval_seconds for page in pages.values() if page.poll_interval_seconds
    ]
    return min(poll_intervals) if poll_intervals else DEFAULT_POLL_INTERVAL_SECONDS


def _load_or_new_state(config: Config) -> State:
    """Load persisted state, or -- if no state file exists yet -- return a
    fresh, un-fast-forwarded State() (since=None, etag=None, sent=set()).

    Unlike the old `_load_or_fast_forward_state()`, this NEVER contacts the
    feed and NEVER returns None. A `state.since is None` result means "no
    safe cursor yet" -- the caller (`_try_fast_forward`/`run_cycle`) is
    responsible for establishing one before anything is ever routed or
    sent. This split is what lets the web UI (and the process generally)
    start and stay up even while the feed is unreachable -- see the
    module-level comment above `run_cycle()`.
    """
    state = load_state(config.state_path)
    if state is not None:
        return state
    logger.info(
        "no state file at %s; this is a first run -- will fast-forward "
        "(fetch current cursor, send nothing) once the feed answers",
        config.state_path,
    )
    return State()


def _try_fast_forward(config: Config, state: State, feed_client: FeedClient) -> bool:
    """If `state` has no safe cursor yet (`state.since is None`), attempt
    ONE fast-forward poll now. Returns True if `state` has a safe cursor
    after this call -- either it already did, or this call just obtained
    one -- False otherwise.

    THE SAFETY PROPERTY: this function never routes or sends anything. It
    only ever reads a `next_since` cursor from the feed and persists it via
    `fast_forward_on_first_run()`. Never raises -- `feed_client.poll()`
    already swallows every I/O failure into a `None` return.
    """
    if state.since is not None:
        return True
    page = feed_client.poll(since=None, etag=None)
    if page is None or page.next_since is None:
        return False
    fast_forward_on_first_run(config.state_path, page.next_since)
    state.since = page.next_since
    state.etag = None
    logger.info("fast-forwarded to since=%s; nothing sent this run", state.since)
    return True


def run_cycle(
    config: Config,
    state: State,
    feed_client: FeedClient,
    status: Optional[BotStatus] = None,
    ff_tracker: Optional[Dict[str, bool]] = None,
) -> int:
    """Run one iteration of the main loop.

    If `state` has no safe cursor yet, this makes ONE fast-forward attempt
    (see `_try_fast_forward`) and returns without routing or sending
    anything, regardless of whether that attempt succeeded -- a cursor
    obtained this cycle is used starting next cycle, mirroring the
    original fast-forward-then-poll-next-cycle shape. If a cursor already
    exists (either from a previous cycle or from disk), this runs the
    normal `run_once()` poll/route/send/persist cycle.

    `ff_tracker` is a small mutable dict, `{"failed": bool}`, the caller
    keeps across calls so a failed fast-forward is logged at WARNING once
    on entering the failed state (and again, once, on recovery) rather
    than spamming an identical line every cycle -- per-cycle noise while
    still failing goes to DEBUG instead. Pass the same dict on every call
    for this to work; a fresh dict each call defeats the point.

    Returns the number of seconds the caller should sleep before the next
    cycle.
    """
    if ff_tracker is None:
        ff_tracker = {"failed": False}

    if state.since is None:
        ok = _try_fast_forward(config, state, feed_client)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if not ok:
            if not ff_tracker["failed"]:
                logger.warning(
                    "fast-forward poll failed; refusing to start without a "
                    "safe cursor -- will keep retrying every cycle. Nothing "
                    "will be relayed until the feed answers."
                )
                ff_tracker["failed"] = True
            else:
                logger.debug("fast-forward poll still failing; still waiting for a safe cursor")
            if status is not None:
                status.update(
                    has_safe_cursor=False,
                    last_poll_at=now,
                    last_error="fast-forward poll failed; no safe cursor yet",
                    feed_reachable=False,
                )
            return DEFAULT_POLL_INTERVAL_SECONDS

        if ff_tracker["failed"]:
            logger.warning(
                "fast-forward recovered; safe cursor established at since=%s "
                "-- relaying begins now",
                state.since,
            )
            ff_tracker["failed"] = False
        if status is not None:
            status.update(
                has_safe_cursor=True,
                cursor_since=state.since,
                last_poll_at=now,
                last_error=None,
                feed_reachable=True,
            )
        return DEFAULT_POLL_INTERVAL_SECONDS

    if status is not None:
        status.update(has_safe_cursor=True)
    return run_once(config, state, feed_client, status=status)


def _reload_config(config_path: str, previous: Config) -> Config:
    """Re-parse and re-validate the config file, falling back to `previous`
    on any failure (bad YAML, a validation error, the file briefly missing
    mid-save) rather than crashing the poll loop over a transient or
    hand-edit mistake. Logged either way so a silently-ignored bad save
    doesn't go unnoticed.
    """
    try:
        reloaded = load_config(config_path)
    except Exception as e:  # noqa: BLE001 - any load/parse/validate failure
        logger.error(
            "failed to reload config %s: %s -- keeping the previously loaded config this cycle",
            config_path,
            e,
        )
        return previous
    return reloaded


def _resolve_web_bind(config: Config, bind_override: Optional[str]) -> tuple:
    """Resolve the (host, port) the web UI should bind to: `--web-bind`
    overrides `config.web.bind_host`/`bind_port` when given."""
    if bind_override is None:
        return config.web.bind_host, config.web.bind_port
    host, _, port_str = bind_override.rpartition(":")
    if not host or not port_str:
        raise ValueError(f"--web-bind must be HOST:PORT, got {bind_override!r}")
    return host, int(port_str)


def _run_webui_thread(
    config_path: str, config: Config, status: BotStatus, bind_override: Optional[str]
) -> None:
    """Target for the web UI's daemon thread.

    Deliberately swallows everything: a bind failure, a bug in a route
    handler that somehow escapes ConfigUIHandler's own try/except, anything
    -- the operator must keep getting relayed announcements even if the web
    UI never comes up or dies later. Compare sinks.make_sink(), which is
    the other place this repo is careful to keep a UI/transport concern
    from being able to take the poll loop down with it.
    """
    try:
        host, port = _resolve_web_bind(config, bind_override)
        start_webui(config_path, status, host, port)
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.error("web UI failed and will not be available this run: %s", e)


def spawn_webui_thread(
    config_path: str, config: Config, status: BotStatus, bind_override: Optional[str] = None
) -> threading.Thread:
    thread = threading.Thread(
        target=_run_webui_thread,
        args=(config_path, config, status, bind_override),
        name="meshwars-bot-webui",
        daemon=True,
    )
    thread.start()
    return thread


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="meshwars-bot")
    parser.add_argument("--config", default="./config.yaml", help="path to config file")
    parser.add_argument(
        "--once", action="store_true", help="run a single poll/route/send cycle and exit"
    )
    parser.add_argument(
        "--web", action="store_true", help="start the built-in web config UI alongside the poll loop"
    )
    parser.add_argument(
        "--web-bind",
        default=None,
        metavar="HOST:PORT",
        help="override web.bind_host/web.bind_port from the config, e.g. 0.0.0.0:8471",
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

    # `state` never contacts the feed and never fails: it's either loaded
    # from disk (a previously-established cursor) or a fresh, un-fast-
    # forwarded State() (since=None -- "no safe cursor yet"). This is what
    # lets the web UI come up below BEFORE the bot has ever reached the
    # feed at all.
    state = _load_or_new_state(config)

    status = BotStatus()
    status.update(has_safe_cursor=state.since is not None)

    # The web UI starts FIRST and stays up regardless of feed reachability
    # -- see the module docstring / bug this fixes: an operator's only way
    # to correct a wrong or not-yet-deployed feed.base_url is this page,
    # so it must be reachable exactly when the feed is not. Nothing below
    # this point may gate the web UI on feed contact.
    if args.web or config.web.enabled:
        spawn_webui_thread(args.config, config, status, args.web_bind)

    feed_client = FeedClient(
        base_url=config.feed.base_url,
        api_key=config.feed.api_key,
        timeout_seconds=config.feed.timeout_seconds,
    )

    if args.once:
        # `--once` keeps its original, strict semantics even with `--web`:
        # a single cycle, then exit -- and a failed fast-forward must still
        # be an honest non-zero exit so a script can tell it failed. This
        # is the one place a failed fast-forward is NOT silently retried.
        if state.since is None:
            ok = _try_fast_forward(config, state, feed_client)
            if not ok:
                logger.error("fast-forward poll failed; refusing to start without a safe cursor")
                return 1
        run_once(config, state, feed_client, status=status)
        return 0

    ff_tracker = {"failed": False}
    while True:
        # Re-read the config file at the top of EVERY cycle, rather than
        # once at startup, so a config saved through the web UI takes
        # effect on the very next poll -- no restart. `state` is NOT
        # reloaded here: nothing editable through the UI touches
        # state_path or state.json itself, and the in-memory `state`
        # object (with whatever this run has already marked sent) must
        # stay the single source of truth across cycles, never clobbered
        # by re-reading a stale copy from disk mid-run.
        config = _reload_config(args.config, config)
        feed_client = FeedClient(
            base_url=config.feed.base_url,
            api_key=config.feed.api_key,
            timeout_seconds=config.feed.timeout_seconds,
        )

        sleep_seconds = run_cycle(config, state, feed_client, status=status, ff_tracker=ff_tracker)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    sys.exit(main())
