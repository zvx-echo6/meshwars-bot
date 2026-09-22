# meshwars-bot

meshwars-bot is a standalone bot that polls the MeshWars public announcement
feed and relays each announcement onto a Meshtastic or MeshCore channel. It
is **not** part of MeshWars — MeshWars itself has no transmit capability and
never will; this bot is the only thing in this whole chain that touches a
radio.

## Status

Honest as of this writing:

- **Complete and tested:** the feed client, config loading, routing rules,
  durable state (including first-run fast-forward), and dry-run output.
- **Not implemented: real radio transports.** `sinks.make_sink()` raises
  `NotImplementedError` for any destination that isn't running in dry-run
  mode. If you clone this today, you get a bot that logs exactly what it
  *would* transmit — to a file and to stdout — and transmits nothing to any
  radio. Meshtastic and MeshCore transports are future work, not "coming
  soon" in any concrete sense yet.

## Requirements

Python 3.12, standard library only. There is nothing to `pip install` — no
`requirements.txt`, no virtualenv needed. That's deliberate: the whole point
is that this runs anywhere with zero packaging work, including the config
parser, which is a small hand-rolled YAML subset rather than a dependency on
PyYAML.

## Quick start

```
cp config.example.yaml config.yaml
$EDITOR config.yaml
python3 -m meshwars_bot.main --config config.yaml --once
```

With `dry_run: true` (the default), that run polls the feed once, decides
which announcements would go to which destinations, and writes each one as
a line to stdout and to `./meshwars-bot-dryrun.log` in the form:

```
[2026-09-22T00:00:00+00:00] [destination-name] announcement text here
```

Nothing is sent anywhere. To run continuously instead of once, drop
`--once`; the process polls, sleeps for the interval the server tells it to
(or 15 minutes if that's unavailable), and repeats forever.

Run the test suite with `python3 -m pytest tests/ -q` — currently 51 tests,
all passing.

## Configuration reference

All fields, as they appear in `config.example.yaml`:

| Field | Where | Meaning |
|---|---|---|
| `feed.base_url` | top | Base URL of the MeshWars-compatible server. The bot GETs `{base_url}/api/v1/announcements`. |
| `feed.api_key` | top | Optional, sent as the `X-API-Key` header. Empty string means keyless polling. |
| `feed.timeout_seconds` | top | HTTP timeout per poll request. Default 20. |
| `state_path` | top | Where the cursor/ETag/sent-record JSON file is written. Must be writable and persistent. Default `./meshwars-bot-state.json`. |
| `destinations[].name` | destination | Unique identifier for this destination. Used in logs, in the dry-run log lines, and as the key under which sent-announcement IDs are tracked. |
| `destinations[].transport` | destination | `meshcore`, `meshtastic`, or `dryrun`. Only meaningful once real transports exist — see Status above. |
| `destinations[].host` / `.port` / `.channel` | destination | Connection details for the (future) real transport. Unused while `dry_run` is true. |
| `destinations[].board` | destination | Which board's announcements this destination carries: `mc`/`meshcore` or `mt`/`meshtastic` (the pairs are aliases for the same two boards). An announcement is only relayed here if its board matches. |
| `destinations[].dry_run` | destination | Safety switch. **Defaults to `true` when the key is absent entirely.** While true, nothing transmits — messages only go to the local log and stdout. You must set this to `false` deliberately, and only once a real transport is implemented for that destination (today, doing so raises `NotImplementedError`). |
| `destinations[].text_budget` | destination | Upper bound, in bytes, the feed is asked to fit each announcement's `text` to. Clamped to 20-1000. See the note on per-budget requests below. |
| `destinations[].kinds` | destination | Explicit allow-list of announcement kinds relayed here. A kind not in this list is never sent to this destination. |
| `destinations[].net_ids` | destination | Explicit allow-list of net IDs whose net-tied announcements are relayed here. |

Two fields that will bite you if you don't read this carefully:

- **`net_ids: []` means NO net announcements reach that destination** — it
  is not a wildcard for "all nets." This is opt-in on purpose: a bot
  relaying onto one community's channel shouldn't also relay another
  community's net results just because the list was left empty. List the
  net IDs you actually want, e.g. `net_ids: [1, 2]`.
- **`dry_run` defaults to `true`** if you omit the key. You have to write
  `dry_run: false` yourself to have any hope of transmitting — and today
  that will fail anyway, since no real transport is implemented yet.

One more thing worth knowing about `text_budget`: the bot makes **one feed
request per distinct `text_budget`** found across your destinations, each
asking the server to fit `text` to that specific budget, and relays each
response only to the destinations that configured that budget. So every
destination gets text fitted to its *own* setting — a tight LoRa channel at
150 bytes is not exposed to a roomier sibling's 237. Before sending, the
bot also verifies each destination's text fits its budget in bytes and
drops (logging an error) any message that somehow doesn't, rather than
transmitting an over-budget packet. The bot never re-fits text locally
otherwise — it relays exactly what the server fitted.

This has a real cost, though: **each distinct `text_budget` is an
additional feed request every poll cycle.** The keyless feed tier allows
only 6 requests/hour/IP. At the default 900s (15 minute) poll interval —
4 cycles/hour — a single shared budget costs 4 requests/hour, but two
distinct budgets costs 8/hour, already over the keyless limit and good for
`429`s. If your destinations need different budgets, either set
`feed.api_key` to raise your rate limit, or give every destination the same
`text_budget`. `config.py` logs a warning at load time when a config has
more than one distinct `text_budget` and no `feed.api_key` set, naming the
budgets it found.

## How polling works

The bot tracks a cursor (`since`) in its state file. Each poll sends that
cursor to the server, which returns any newer announcements plus a
`next_since` value that becomes the cursor for the following poll. The
server also returns `poll_interval_seconds`, which the bot honors as its
sleep time between cycles (falling back to 15 minutes if the server doesn't
supply one, or if a poll fails outright).

The bot also sends the ETag from the previous response as `If-None-Match`;
a `304 Not Modified` response means no new announcements and costs the
server nothing extra to generate. If the server responds `429 Too Many
Requests`, the bot honors any `Retry-After` header as the wait before the
next attempt.

Polling works two ways: keyless, subject to a per-IP rate limit, or with an
API key (`feed.api_key`) issued by the server's operator, which raises that
limit. Keyless is fine to start with; ask for a key if you're going to run
this continuously against someone else's server.

## First run fast-forwards (read this before pointing it at a live channel)

On first run — no state file present yet — the bot does not fetch and send
whatever announcements currently exist. Instead it fetches only the current
cursor (`next_since`), saves that as its starting point, and sends nothing
for that cycle. This is deliberate: a fresh install should never dump days
of old announcements onto a live radio channel the moment it starts.

If you ever want to reset a destination back to this safe state — for
example after reconfiguring it — delete the state file (`state_path`) and
restart. That re-triggers fast-forward: the bot silently re-syncs to the
current cursor instead of replaying history.

## Announcement kinds

The feed emits four kinds, each independently allow-listed per destination
via `kinds`:

- `daily_recap` — a recap of the previous day.
- `weekly_recap` — a recap of the previous week.
- `month_honors` — end-of-month honors/summary.
- `net_wrapup` — wrap-up of a specific net (see `net_ids` above).

A quiet period simply produces no announcement of that kind — the bot has
nothing special to do there; it just won't see one come through the feed.

## Using it against a different server

`feed.base_url` can point at any server exposing the same contract this bot
speaks — it doesn't have to be the official MeshWars instance. The contract
is:

```
GET {base_url}/api/v1/announcements
    ?since=<int>            (optional, the cursor)
    &kinds=<comma-separated> (optional)
    &board=<mc|mt>           (optional)
    &net_id=<int>            (optional)
    &limit=<int>
    &text_budget=<int, 20-1000>
Headers: Accept: application/json, X-API-Key (optional), If-None-Match (optional)
```

A `200` response body looks like:

```json
{
  "announcements": [
    {"id": 1, "kind": "daily_recap", "key": "...", "board": "mc",
     "net_id": null, "created_at": "...", "content": {}, "text": "..."}
  ],
  "next_since": 2,
  "poll_interval_seconds": 900
}
```

The server should also support `ETag` / `If-None-Match` with `304`
responses, and `429` with an optional `Retry-After` header. Any other
response is treated as a failed poll (logged, nothing sent, retried next
cycle).

## Adding a real transport

This is the obvious first contribution, and currently the missing piece.
Look at `meshwars_bot/sinks.py`: everything a routed announcement's text
gets sent to implements the `Sink` abstract base class, which has one
method — `send(self, text: str) -> bool`. Implement a new `Sink` subclass
for your transport, and wire it into `make_sink()` for the destinations
that aren't running dry-run.

## License

AGPL-3.0. Full text in [`LICENSE`](LICENSE).
