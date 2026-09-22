"""Config loading for meshwars-bot: a strictly-limited YAML subset parser plus
validation into typed Config/Destination objects.

Why a hand-rolled parser: the task this repo was built for forbids installing
any third-party package (including PyYAML) — everything must run on the
system python3 standard library. There is no YAML parser in the stdlib, so
`parse_yaml_subset()` below implements just enough of YAML to read
config.example.yaml and configs shaped like it.

--------------------------------------------------------------------------
THIS IS NOT A GENERAL YAML PARSER. Its supported subset, and only that
subset, is:

- Block mappings: `key: value` lines, nesting by leading-space indentation.
  Indentation must use spaces only (a leading tab is a hard error).
- Block sequences of flat mappings, e.g.:
      destinations:
        - name: "a"
          port: 1
        - name: "b"
          port: 2
  A sequence item may itself only be a flat mapping of scalars / inline
  lists — it may NOT contain a further nested mapping or sequence.
  Sequences of bare scalars (`- foo`) are NOT supported.
- Inline flow sequences of scalars only, e.g. `kinds: ["a", "b"]` or
  `net_ids: [1, 2]`. No nested flow lists and no flow mappings (`{...}`).
- Scalars: double- or single-quoted strings, bare (unquoted) strings,
  integers, and booleans (`true`/`false`, `True`/`False`, case-insensitive).
  There is no float, null, date, or multiline-string support.
- Comments: a `#` starts a line comment, but only outside of a quoted
  string; blank lines are ignored.
- No anchors/aliases, no tags, no multi-document (`---`) files.
- The document root must be a mapping.

Anything outside this subset raises ConfigError naming the offending line,
rather than silently guessing at a value.
--------------------------------------------------------------------------
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("meshwars_bot.config")


class ConfigError(Exception):
    """Raised for any config parsing or validation failure."""


# ---------------------------------------------------------------------------
# YAML subset tokenizer / parser
# ---------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """Strip a trailing `# ...` comment, respecting quoted strings."""
    out = []
    in_single = False
    in_double = False
    for ch in line:
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
        elif ch == "#" and not in_single and not in_double:
            break
        else:
            out.append(ch)
    return "".join(out)


def _parse_scalar(text: str) -> Any:
    text = text.strip()
    if text == "":
        return None
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1]
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    return text


def _split_flow_list(inner: str) -> List[Any]:
    items = []
    cur = []
    in_single = False
    in_double = False
    for ch in inner:
        if ch == "'" and not in_double:
            in_single = not in_single
            cur.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            cur.append(ch)
        elif ch == "," and not in_single and not in_double:
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail != "":
        items.append(tail)
    return [_parse_scalar(item) for item in items]


def _parse_value(rest: str, lineno: int) -> Any:
    if rest.startswith("["):
        if not rest.endswith("]"):
            raise ConfigError(f"line {lineno}: inline list must open and close on the same line")
        return _split_flow_list(rest[1:-1])
    if rest.startswith("{"):
        raise ConfigError(f"line {lineno}: flow mappings '{{...}}' are not supported")
    return _parse_scalar(rest)


def _tokenize(text: str) -> List[tuple]:
    tokens = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        leading = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in leading:
            raise ConfigError(f"line {lineno}: tabs are not supported for indentation, use spaces")
        line = _strip_comment(raw).rstrip()
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        tokens.append((indent, content, lineno))
    return tokens


def _parse_block(tokens: List[tuple], idx: int, indent: int):
    if idx < len(tokens) and tokens[idx][0] == indent and tokens[idx][1].startswith("- "):
        return _parse_sequence(tokens, idx, indent)
    return _parse_mapping(tokens, idx, indent)


def _parse_mapping(tokens: List[tuple], idx: int, indent: int):
    result: Dict[str, Any] = {}
    while idx < len(tokens):
        cur_indent, content, lineno = tokens[idx]
        if cur_indent != indent or content.startswith("- "):
            break
        if ":" not in content:
            raise ConfigError(f"line {lineno}: expected 'key: value' (no ':' found)")
        key, _, rest = content.partition(":")
        key = key.strip()
        rest = rest.strip()
        if not key:
            raise ConfigError(f"line {lineno}: empty key")
        idx += 1
        if rest == "":
            if idx < len(tokens) and tokens[idx][0] > indent:
                nested_indent = tokens[idx][0]
                value, idx = _parse_block(tokens, idx, nested_indent)
            else:
                value = None
        else:
            value = _parse_value(rest, lineno)
        result[key] = value
    return result, idx


def _parse_sequence(tokens: List[tuple], idx: int, indent: int):
    result: List[Any] = []
    while idx < len(tokens):
        cur_indent, content, lineno = tokens[idx]
        if cur_indent != indent or not content.startswith("- "):
            break
        item_content = content[2:].strip()
        item_col = indent + 2
        if ":" not in item_content:
            raise ConfigError(
                f"line {lineno}: sequence items must be 'key: value' mappings "
                "(bare scalar list items are not supported)"
            )
        item: Dict[str, Any] = {}
        key, _, rest = item_content.partition(":")
        key = key.strip()
        rest = rest.strip()
        item[key] = None if rest == "" else _parse_value(rest, lineno)
        idx += 1
        while idx < len(tokens) and tokens[idx][0] == item_col:
            _, kcontent, klineno = tokens[idx]
            if kcontent.startswith("- "):
                raise ConfigError(
                    f"line {klineno}: nested sequences inside a sequence item are not supported"
                )
            if ":" not in kcontent:
                raise ConfigError(f"line {klineno}: expected 'key: value'")
            k, _, v = kcontent.partition(":")
            k = k.strip()
            v = v.strip()
            item[k] = None if v == "" else _parse_value(v, klineno)
            idx += 1
        result.append(item)
    return result, idx


def parse_yaml_subset(text: str) -> Dict[str, Any]:
    """Parse the limited YAML subset described in this module's docstring.

    Raises ConfigError on anything outside that subset.
    """
    tokens = _tokenize(text)
    if not tokens:
        return {}
    if tokens[0][0] != 0:
        raise ConfigError(f"line {tokens[0][2]}: top-level document must start at column 0")
    value, idx = _parse_mapping(tokens, 0, 0)
    if idx != len(tokens):
        _, _, lineno = tokens[idx]
        raise ConfigError(f"line {lineno}: unexpected indentation")
    if not isinstance(value, dict):
        raise ConfigError("top-level document must be a mapping")
    return value


# ---------------------------------------------------------------------------
# Typed config
# ---------------------------------------------------------------------------

KNOWN_TRANSPORTS = {"meshcore", "meshtastic", "dryrun"}

# Board aliases the feed's `board` query param accepts, canonicalized to the
# short form the announcements themselves carry.
_BOARD_ALIASES = {"mc": "mc", "meshcore": "mc", "mt": "mt", "meshtastic": "mt"}
KNOWN_BOARDS = set(_BOARD_ALIASES)

# The announcement kinds documented for this bot as of this task. Extend this
# set (honestly) if the feed grows more kinds.
KNOWN_KINDS = {"daily_recap", "weekly_recap", "month_honors", "net_wrapup"}

DEFAULT_TEXT_BUDGET = 150
MIN_TEXT_BUDGET = 20
MAX_TEXT_BUDGET = 1000
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_STATE_PATH = "./meshwars-bot-state.json"

# Defaults for the optional built-in web config UI (see webui.py). Disabled
# by default -- it binds with NO authentication, so it must be an explicit
# opt-in (config `web.enabled: true` or the `--web` CLI flag), never on by
# default just because a config file exists.
DEFAULT_WEB_ENABLED = False
DEFAULT_WEB_BIND_HOST = "0.0.0.0"
DEFAULT_WEB_BIND_PORT = 8471


def normalize_board(board: str) -> str:
    """Canonicalize a board value ('mc'/'meshcore'/'mt'/'meshtastic') to 'mc' or 'mt'."""
    return _BOARD_ALIASES.get(str(board).strip().lower(), str(board).strip().lower())


@dataclass
class FeedConfig:
    base_url: str
    api_key: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


@dataclass
class Destination:
    name: str
    transport: str
    host: Optional[str]
    port: Optional[int]
    channel: Optional[str]
    board: str
    dry_run: bool
    text_budget: int
    kinds: List[str] = field(default_factory=list)
    net_ids: List[int] = field(default_factory=list)


@dataclass
class WebConfig:
    enabled: bool = DEFAULT_WEB_ENABLED
    bind_host: str = DEFAULT_WEB_BIND_HOST
    bind_port: int = DEFAULT_WEB_BIND_PORT


@dataclass
class Config:
    feed: FeedConfig
    state_path: str
    destinations: List[Destination] = field(default_factory=list)
    web: WebConfig = field(default_factory=WebConfig)


def _clamp_text_budget(value: Any, dest_name: str) -> int:
    if value is None:
        value = DEFAULT_TEXT_BUDGET
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"destination '{dest_name}': text_budget must be an integer")
    return max(MIN_TEXT_BUDGET, min(MAX_TEXT_BUDGET, value))


def _build_feed_config(raw: Dict[str, Any]) -> FeedConfig:
    if not isinstance(raw, dict):
        raise ConfigError("'feed' section must be a mapping")
    base_url = raw.get("base_url")
    if not base_url or not isinstance(base_url, str):
        raise ConfigError("'feed.base_url' is required and must be a string")
    api_key = raw.get("api_key", "") or ""
    timeout_seconds = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        raise ConfigError("'feed.timeout_seconds' must be an integer")
    return FeedConfig(base_url=base_url, api_key=api_key, timeout_seconds=timeout_seconds)


def _build_destination(raw: Dict[str, Any], index: int) -> Destination:
    if not isinstance(raw, dict):
        raise ConfigError(f"destination #{index}: must be a mapping")
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ConfigError(f"destination #{index}: 'name' is required and must be a string")

    transport = raw.get("transport")
    if transport not in KNOWN_TRANSPORTS:
        raise ConfigError(
            f"destination '{name}': unknown transport {transport!r} "
            f"(known: {sorted(KNOWN_TRANSPORTS)})"
        )

    board_raw = raw.get("board")
    if board_raw not in KNOWN_BOARDS:
        raise ConfigError(
            f"destination '{name}': unknown board {board_raw!r} (known: {sorted(KNOWN_BOARDS)})"
        )
    board = normalize_board(board_raw)

    kinds = raw.get("kinds", [])
    if not isinstance(kinds, list):
        raise ConfigError(f"destination '{name}': 'kinds' must be a list")
    for kind in kinds:
        if kind not in KNOWN_KINDS:
            raise ConfigError(
                f"destination '{name}': unknown kind {kind!r} (known: {sorted(KNOWN_KINDS)})"
            )

    net_ids = raw.get("net_ids", [])
    if not isinstance(net_ids, list):
        raise ConfigError(f"destination '{name}': 'net_ids' must be a list")
    for net_id in net_ids:
        if not isinstance(net_id, int) or isinstance(net_id, bool):
            raise ConfigError(f"destination '{name}': 'net_ids' entries must be integers")

    dry_run = raw.get("dry_run", True)
    if dry_run is None:
        dry_run = True
    if not isinstance(dry_run, bool):
        raise ConfigError(f"destination '{name}': 'dry_run' must be a boolean")

    text_budget = _clamp_text_budget(raw.get("text_budget"), name)

    port = raw.get("port")
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ConfigError(f"destination '{name}': 'port' must be an integer")

    return Destination(
        name=name,
        transport=transport,
        host=raw.get("host"),
        port=port,
        channel=raw.get("channel"),
        board=board,
        dry_run=dry_run,
        text_budget=text_budget,
        kinds=list(kinds),
        net_ids=list(net_ids),
    )


def multi_budget_warning_info(
    feed: FeedConfig, destinations: List[Destination]
) -> Optional[Dict[str, Any]]:
    """Pure (no logging) check for the multi-budget/no-api-key condition.

    Returns None when the condition doesn't hold, else a dict describing it
    -- {"budgets": [...], "requests_per_hour": N}. Split out from
    `_warn_on_multi_budget_without_api_key` below so a caller that isn't
    reading logs (namely webui.py, which needs to show this same warning in
    the config page) can ask the question directly instead of duplicating
    the arithmetic. See that function's docstring for why this matters.
    """
    budgets = sorted({d.text_budget for d in destinations})
    if len(budgets) > 1 and not feed.api_key:
        return {"budgets": budgets, "requests_per_hour": len(budgets) * 4}
    return None


def _warn_on_multi_budget_without_api_key(feed: FeedConfig, destinations: List[Destination]) -> None:
    """Warn (not error) at load time when a config both:
      - has more than one distinct `text_budget` across its destinations, and
      - has no `feed.api_key` set (keyless polling).

    Why this matters: the bot makes one feed request per distinct
    `text_budget` per poll cycle (each destination's text must be fitted to
    ITS OWN budget -- see main.py's per-budget fetch). The keyless feed
    tier allows only 6 requests/hour/IP. Two distinct budgets already
    double the request count; at a typical 900s (15 minute) poll interval
    that's 4 cycles/hour, so N distinct budgets costs N*4 requests/hour --
    2 budgets = 8/hour, already over the keyless limit and good for 429s.
    An API key raises that limit; a single shared `text_budget` avoids the
    multiplier entirely.
    """
    info = multi_budget_warning_info(feed, destinations)
    if info is not None:
        logger.warning(
            "config has %d distinct text_budget values across destinations "
            "(%s) and no feed.api_key is set. Each distinct text_budget costs "
            "one additional feed request per poll cycle -- at a 900s poll "
            "interval that's %d requests/hour, which will exceed the keyless "
            "feed tier's 6 requests/hour/IP limit and start getting 429s. "
            "Set feed.api_key, or make every destination share a single "
            "text_budget, to avoid this.",
            len(info["budgets"]),
            info["budgets"],
            info["requests_per_hour"],
        )


def _build_web_config(raw: Any) -> WebConfig:
    """Validate the optional `web` section (the built-in config UI's own
    enabled/bind_host/bind_port). Absent entirely -> all defaults, i.e. the
    UI stays off unless someone opts in."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("'web' section must be a mapping")

    enabled = raw.get("enabled", DEFAULT_WEB_ENABLED)
    if not isinstance(enabled, bool):
        raise ConfigError("'web.enabled' must be a boolean")

    bind_host = raw.get("bind_host", DEFAULT_WEB_BIND_HOST)
    if not isinstance(bind_host, str) or not bind_host:
        raise ConfigError("'web.bind_host' must be a non-empty string")

    bind_port = raw.get("bind_port", DEFAULT_WEB_BIND_PORT)
    try:
        bind_port = int(bind_port)
    except (TypeError, ValueError):
        raise ConfigError("'web.bind_port' must be an integer")
    if not (1 <= bind_port <= 65535):
        raise ConfigError("'web.bind_port' must be between 1 and 65535")

    return WebConfig(enabled=enabled, bind_host=bind_host, bind_port=bind_port)


def build_config(raw: Dict[str, Any]) -> Config:
    """Validate a parsed config mapping (as returned by parse_yaml_subset) into a Config."""
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")

    feed = _build_feed_config(raw.get("feed", {}))
    state_path = raw.get("state_path") or DEFAULT_STATE_PATH
    if not isinstance(state_path, str):
        raise ConfigError("'state_path' must be a string")

    raw_destinations = raw.get("destinations", [])
    if not isinstance(raw_destinations, list):
        raise ConfigError("'destinations' must be a list")

    destinations = [
        _build_destination(item, i) for i, item in enumerate(raw_destinations)
    ]

    names = [d.name for d in destinations]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"duplicate destination name(s): {sorted(duplicates)}")

    _warn_on_multi_budget_without_api_key(feed, destinations)

    web = _build_web_config(raw.get("web", {}))

    return Config(feed=feed, state_path=state_path, destinations=destinations, web=web)


def load_config(path: str) -> Config:
    """Read, parse, and validate a config file at `path`."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    raw = parse_yaml_subset(text)
    return build_config(raw)
