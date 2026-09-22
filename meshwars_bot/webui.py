"""Built-in web config UI: an http.server.ThreadingHTTPServer app, standard
library only -- same constraint as the rest of this repo, and the whole
point is that operators get a real interface without installing anything.

This is a LAN convenience tool, not a hardened service: there is no
authentication at all (see the bind-host warning in README.md/main.py).
It also does not, and must not, do anything that touches a radio -- it
only reads and writes config.yaml and reads the dry-run log. Validation is
never duplicated here: every write goes through config.py's own
build_config()/ConfigError, the exact same rules the process itself
enforces on startup.
"""

import http.server
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import feed as feed_module
from .config import (
    Config,
    ConfigError,
    Destination,
    FeedConfig,
    KNOWN_KINDS,
    KNOWN_TRANSPORTS,
    build_config,
    load_config,
    multi_budget_warning_info,
    parse_yaml_subset,
)
from .configwrite import ConfigWriteError, write_config
from .sinks import DEFAULT_DRYRUN_LOG_PATH

logger = logging.getLogger("meshwars_bot.webui")

NETS_PATH = "/api/v1/nets"

# The two canonical board values a destination can be configured with.
# config.py's KNOWN_BOARDS additionally accepts the aliases "meshcore" and
# "meshtastic", which normalize_board() reduces to these two -- the page
# only needs to offer the canonical pair.
CANONICAL_BOARDS = ["mc", "mt"]


# ---------------------------------------------------------------------------
# Live status, shared between the poll loop (main.py) and this server.
# ---------------------------------------------------------------------------


class BotStatus:
    """Thread-safe snapshot of the running bot's live status.

    Written by main.py's poll loop after every cycle, read by the
    /api/state handler below. A single lock is enough -- both sides only
    ever read the whole snapshot or replace it wholesale with .update(),
    never mutate a nested value across the lock boundary.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: Dict[str, Any] = {
            "cursor_since": None,
            "last_poll_at": None,
            "last_error": None,
            "feed_reachable": None,
            "sent_counts": {},
            # False until the bot's very first successful fast-forward poll
            # establishes a starting cursor. Until then the poll loop
            # relays NOTHING to any destination, however many cycles it
            # runs -- see main.py's run_cycle()/_try_fast_forward(). This
            # is the first thing an operator needs to see when the page is
            # up but the feed is not, so the UI surfaces it prominently
            # rather than as a buried field.
            "has_safe_cursor": False,
        }

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._snapshot.update(fields)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)


# ---------------------------------------------------------------------------
# Config <-> JSON shaping
# ---------------------------------------------------------------------------


def _destination_to_json(d: Destination) -> Dict[str, Any]:
    return {
        "name": d.name,
        "transport": d.transport,
        "host": d.host,
        "port": d.port,
        "channel": d.channel,
        "board": d.board,
        "dry_run": d.dry_run,
        "text_budget": d.text_budget,
        "kinds": list(d.kinds),
        "net_ids": list(d.net_ids),
    }


def _config_to_json(config: Config) -> Dict[str, Any]:
    warning = multi_budget_warning_info(config.feed, config.destinations)
    return {
        "feed": {
            "base_url": config.feed.base_url,
            "timeout_seconds": config.feed.timeout_seconds,
            # Never the value itself -- see the module docstring and the
            # GET /api/config route below.
            "api_key_set": bool(config.feed.api_key),
        },
        "destinations": [_destination_to_json(d) for d in config.destinations],
        "warning": warning,
        "schema": {
            "transports": sorted(KNOWN_TRANSPORTS),
            "boards": CANONICAL_BOARDS,
            "kinds": sorted(KNOWN_KINDS),
        },
    }


def _merge_feed(current: Dict[str, Any], posted: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a POSTed feed edit onto the feed section currently on disk.

    `api_key` is write-only end to end: omitted from the POST body means
    "leave the stored key exactly as it is" (the page never has the real
    value to send back); present -- including as "" -- means "set it to
    exactly this". `base_url`/`timeout_seconds` are simple overrides.
    """
    merged = dict(current)
    for key in ("base_url", "timeout_seconds"):
        if key in posted:
            merged[key] = posted[key]
    if "api_key" in posted:
        merged["api_key"] = posted["api_key"]
    return merged


def _fetch_nets(base_url: str, api_key: str, timeout_seconds: int) -> List[Dict[str, Any]]:
    """GET {base_url}/api/v1/nets so the config page can offer net_ids by
    name instead of raw numeric IDs.

    This endpoint is landing in MeshWars in parallel with this bot, so it
    may not exist on every server yet. Same never-raise philosophy as
    feed.py's poll(): ANY failure here (404, other HTTP error, network
    error, timeout, malformed JSON, an unexpected shape) degrades to an
    empty list rather than surfacing an error, so the page just falls back
    to its manual comma-separated net_ids field.
    """
    url = base_url.rstrip("/") + NETS_PATH
    headers = {"User-Agent": feed_module.USER_AGENT, "Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read()
        data = json.loads(body.decode("utf-8"))
        nets = data.get("nets", [])
        return nets if isinstance(nets, list) else []
    except Exception:  # noqa: BLE001 - deliberately degrade, never raise
        return []


def _tail_lines(path: str, n: int) -> List[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return [line.rstrip("\n") for line in lines[-n:]]


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class ConfigUIHandler(http.server.BaseHTTPRequestHandler):
    server_version = "meshwars-bot-webui/0.1"

    # self.server carries `config_path` (str) and `status` (BotStatus),
    # set by build_server() below -- the standard way to hand per-server
    # state to BaseHTTPRequestHandler instances without a global.

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Route access logging through this module's logger instead of
        # BaseHTTPRequestHandler's default of writing straight to stderr,
        # so it behaves like every other log line this bot emits.
        logger.info("%s - %s", self.address_string(), fmt % args)

    # -- response helpers ---------------------------------------------------

    def _send_json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required name
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if path == "/":
                self._send_html(200, INDEX_HTML)
            elif path == "/api/config":
                self._handle_get_config()
            elif path == "/api/state":
                self._send_json(200, self.server.status.snapshot())
            elif path == "/api/log":
                self._handle_get_log(query)
            elif path == "/api/nets":
                self._handle_get_nets()
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001 - a bad request must never kill the server thread
            logger.exception("error handling GET %s", self.path)
            self._safe_send_json(500, {"error": str(e)})

    def do_POST(self) -> None:  # noqa: N802 - required name
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/api/config":
                self._handle_post_config()
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            logger.exception("error handling POST %s", self.path)
            self._safe_send_json(500, {"error": str(e)})

    def _safe_send_json(self, status: int, obj: Any) -> None:
        try:
            self._send_json(status, obj)
        except Exception:  # noqa: BLE001 - client already gone, nothing to do
            pass

    def _handle_get_config(self) -> None:
        try:
            config = load_config(self.server.config_path)
        except (ConfigError, OSError) as e:
            self._send_json(500, {"error": f"could not load {self.server.config_path}: {e}"})
            return
        self._send_json(200, _config_to_json(config))

    def _handle_get_log(self, query: Dict[str, List[str]]) -> None:
        n = 100
        if "n" in query and query["n"]:
            try:
                n = max(1, min(5000, int(query["n"][0])))
            except ValueError:
                n = 100
        self._send_json(200, {"lines": _tail_lines(DEFAULT_DRYRUN_LOG_PATH, n)})

    def _handle_get_nets(self) -> None:
        try:
            config = load_config(self.server.config_path)
        except (ConfigError, OSError):
            self._send_json(200, {"nets": []})
            return
        nets = _fetch_nets(config.feed.base_url, config.feed.api_key, config.feed.timeout_seconds)
        self._send_json(200, {"nets": nets})

    def _handle_post_config(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        raw_body = self.rfile.read(length) if length else b""
        try:
            posted = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "request body is not valid JSON"})
            return
        if not isinstance(posted, dict):
            self._send_json(400, {"error": "request body must be a JSON object"})
            return

        config_path = self.server.config_path
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                current_raw = parse_yaml_subset(f.read())
        except (OSError, ConfigError) as e:
            self._send_json(500, {"error": f"could not read current config: {e}"})
            return

        merged = dict(current_raw)
        merged["feed"] = _merge_feed(current_raw.get("feed") or {}, posted.get("feed") or {})
        if "destinations" in posted:
            merged["destinations"] = posted["destinations"]
        # state_path and web are never touched here -- neither is exposed
        # for editing on this page (web because reconfiguring the very
        # server you're talking to from inside itself is a bad idea;
        # state_path because nothing about it should change post-setup).

        try:
            validated = build_config(merged)
        except ConfigError as e:
            # Validation reuses config.py's own rules -- do not duplicate
            # them here. On failure: 400, and the file is never touched.
            self._send_json(400, {"error": str(e)})
            return

        try:
            write_config(merged, config_path)
        except (OSError, ConfigWriteError) as e:
            self._send_json(500, {"error": f"validated but failed to write config: {e}"})
            return

        self._send_json(200, _config_to_json(validated))


class _ConfigUIServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Set by build_server():
    config_path: str
    status: BotStatus


def build_server(config_path: str, status: BotStatus, host: str, port: int) -> _ConfigUIServer:
    """Construct (and bind) the server without running it -- split out from
    start_webui() so tests (and anything else that wants the actual
    ephemeral port, e.g. from binding to port 0) can control the serve
    loop themselves."""
    server = _ConfigUIServer((host, port), ConfigUIHandler)
    server.config_path = config_path
    server.status = status
    return server


def start_webui(config_path: str, status: BotStatus, host: str, port: int) -> None:
    """Build and run the config UI server. Blocks in serve_forever().

    The caller (main.py) runs this on a daemon thread wrapped in its own
    try/except -- nothing raised in here, including a failure to bind,
    should ever be able to take down the poll loop.
    """
    server = build_server(config_path, status, host, port)
    logger.info("web config UI listening on http://%s:%s/", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# The page itself: a single self-contained HTML document. Inline CSS and JS
# only -- no CDN links, no external fonts, no JS framework -- this has to
# run on a LAN that may have no internet access at all.
# ---------------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>meshwars-bot config</title>
<style>
  :root {
    color-scheme: light;
    --bg: #f4f4f2;
    --panel: #ffffff;
    --border: #d8d8d2;
    --text: #202020;
    --muted: #63645e;
    --accent: #2a5d8a;
    --safe: #2f7d3a;
    --live: #b3261e;
    --live-bg: #fdecea;
    --warn-bg: #fff4d9;
    --warn-border: #d8a418;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px 16px 64px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 14px;
    line-height: 1.45;
  }
  .wrap { max-width: 900px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 15px; margin: 0 0 12px; }
  .subtitle { color: var(--muted); margin: 0 0 20px; }
  section.panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 18px;
    margin-bottom: 18px;
  }
  .row { display: flex; flex-wrap: wrap; gap: 12px 16px; align-items: flex-end; }
  .field { display: flex; flex-direction: column; gap: 4px; min-width: 160px; }
  .field.grow { flex: 1 1 240px; }
  label { font-weight: 600; font-size: 12px; color: var(--muted); }
  input[type="text"], input[type="number"], input[type="password"], select {
    padding: 6px 8px;
    border: 1px solid var(--border);
    border-radius: 5px;
    font-size: 14px;
    background: #fff;
    color: var(--text);
  }
  input[type="checkbox"] { width: 16px; height: 16px; }
  button {
    padding: 7px 14px;
    border-radius: 5px;
    border: 1px solid var(--accent);
    background: var(--accent);
    color: #fff;
    font-size: 13px;
    cursor: pointer;
  }
  button.secondary { background: #fff; color: var(--accent); }
  button.danger { background: #fff; color: var(--live); border-color: var(--live); }
  button:disabled { opacity: 0.5; cursor: default; }
  .hint { color: var(--muted); font-size: 12px; }
  .banner {
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 16px;
    font-size: 13px;
    display: none;
  }
  .banner.show { display: block; }
  .banner.warn { background: var(--warn-bg); border: 1px solid var(--warn-border); }
  .banner.error { background: var(--live-bg); border: 1px solid var(--live); color: var(--live); }
  .banner.ok { background: #e9f5ea; border: 1px solid var(--safe); color: var(--safe); }
  .destination {
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px 14px;
    margin-bottom: 14px;
  }
  .destination-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .destination-head input[type="text"] { font-weight: 600; font-size: 14px; }
  .safety {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border-radius: 5px;
    margin: 10px 0;
  }
  .safety.dry { background: #eaf3ea; border: 1px solid var(--safe); }
  .safety.live { background: var(--live-bg); border: 1px solid var(--live); }
  .safety .label { font-weight: 700; }
  .safety.dry .label { color: var(--safe); }
  .safety.live .label { color: var(--live); }
  .kinds, .nets { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 4px; }
  .kinds label, .nets label { font-weight: 400; color: var(--text); display: flex; align-items: center; gap: 4px; }
  .net-fallback { display: none; }
  table.status-table { border-collapse: collapse; width: 100%; }
  table.status-table td { padding: 4px 8px 4px 0; vertical-align: top; }
  table.status-table td.k { color: var(--muted); white-space: nowrap; }
  pre#log {
    background: #1c1c1a;
    color: #d8d8d2;
    padding: 10px 12px;
    border-radius: 6px;
    max-height: 320px;
    overflow: auto;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 12px;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 700;
  }
  .pill.up { background: #e9f5ea; color: var(--safe); }
  .pill.down { background: var(--live-bg); color: var(--live); }
  .pill.unknown { background: #eee; color: var(--muted); }
  .footer-actions { display: flex; gap: 10px; align-items: center; margin-top: 6px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>meshwars-bot config</h1>
  <p class="subtitle">Hand-editing config.yaml over SSH, but with a form. No authentication -- LAN only.</p>

  <div id="error-banner" class="banner error"></div>
  <div id="warning-banner" class="banner warn"></div>
  <div id="save-banner" class="banner ok"></div>
  <div id="nocursor-banner" class="banner error"></div>

  <section class="panel" id="status-panel">
    <h2>Status</h2>
    <table class="status-table">
      <tr><td class="k">Feed</td><td id="st-reachable">-</td></tr>
      <tr><td class="k">Cursor (since)</td><td id="st-cursor">-</td></tr>
      <tr><td class="k">Last poll</td><td id="st-lastpoll">-</td></tr>
      <tr><td class="k">Last error</td><td id="st-lasterror">-</td></tr>
      <tr><td class="k">Sent counts</td><td id="st-sentcounts">-</td></tr>
    </table>
  </section>

  <section class="panel" id="feed-panel">
    <h2>Feed</h2>
    <div class="row">
      <div class="field grow">
        <label for="base_url">feed.base_url</label>
        <input type="text" id="base_url" placeholder="https://meshwars.com">
      </div>
      <div class="field">
        <label for="timeout_seconds">feed.timeout_seconds</label>
        <input type="number" id="timeout_seconds" min="1" step="1">
      </div>
    </div>
    <div class="row" style="margin-top:12px;">
      <div class="field">
        <label>API key</label>
        <span id="api-key-status" class="hint">unknown</span>
      </div>
      <div class="field grow">
        <label for="api-key-new">Set new key (leave blank to keep current)</label>
        <input type="password" id="api-key-new" autocomplete="off">
      </div>
      <div class="field">
        <label for="api-key-clear">&nbsp;</label>
        <label style="font-weight:400;"><input type="checkbox" id="api-key-clear"> Clear stored key</label>
      </div>
    </div>
    <p class="hint">The stored key is write-only -- this page never shows it. Leaving the new-key field
      blank and the clear box unchecked keeps whatever is currently saved.</p>
  </section>

  <section class="panel" id="destinations-panel">
    <div class="bar">
      <h2 style="margin:0;">Destinations</h2>
      <button type="button" id="add-destination" class="secondary">Add destination</button>
    </div>
    <p class="hint">net_ids is an explicit allow-list per destination. <strong>Ticking no nets means
      no net_wrapup announcements are relayed to that destination at all</strong> -- it is opt-in, never
      a wildcard for "all nets".</p>
    <div id="destinations-list"></div>
  </section>

  <div class="footer-actions">
    <button type="button" id="save-config">Save config</button>
    <span class="hint">Takes effect on the bot's next poll cycle -- no restart needed.</span>
  </div>

  <section class="panel" id="log-panel" style="margin-top:18px;">
    <div class="bar">
      <h2 style="margin:0;">Dry-run log (last 200 lines, auto-refreshing)</h2>
    </div>
    <pre id="log">(loading...)</pre>
  </section>
</div>

<script>
(function () {
  "use strict";

  var schema = { transports: ["dryrun"], boards: ["mc", "mt"], kinds: [] };
  var netsAvailable = [];

  function qs(id) { return document.getElementById(id); }

  function escapeText(s) {
    var d = document.createElement("div");
    d.textContent = (s === null || s === undefined) ? "" : String(s);
    return d.innerHTML;
  }

  function showBanner(id, message, extraClass) {
    var el = qs(id);
    if (!message) { el.classList.remove("show"); el.textContent = ""; return; }
    el.textContent = message;
    el.classList.add("show");
  }

  function hideBanner(id) { showBanner(id, ""); }

  // -- config load / render ------------------------------------------------

  function renderConfig(cfg) {
    schema = cfg.schema || schema;

    qs("base_url").value = cfg.feed.base_url || "";
    qs("timeout_seconds").value = cfg.feed.timeout_seconds;
    qs("api-key-status").textContent = cfg.feed.api_key_set ? "currently set" : "not set (keyless polling)";
    qs("api-key-new").value = "";
    qs("api-key-clear").checked = false;

    if (cfg.warning) {
      showBanner(
        "warning-banner",
        "Warning: " + cfg.warning.budgets.length + " distinct text_budget values configured (" +
        cfg.warning.budgets.join(", ") + ") with no feed.api_key set. Each distinct budget costs one " +
        "extra feed request per poll cycle (~" + cfg.warning.requests_per_hour + " requests/hour at the " +
        "default interval), which will exceed the keyless feed tier's 6 requests/hour/IP limit. Set an " +
        "API key, or give every destination the same text_budget."
      );
    } else {
      hideBanner("warning-banner");
    }

    var list = qs("destinations-list");
    list.innerHTML = "";
    (cfg.destinations || []).forEach(function (dest) {
      list.appendChild(buildDestinationCard(dest));
    });
  }

  function buildDestinationCard(dest) {
    dest = dest || {
      name: "", transport: schema.transports[0] || "dryrun", host: "", port: "",
      channel: "", board: schema.boards[0] || "mc", dry_run: true, text_budget: 150,
      kinds: [], net_ids: []
    };

    var card = document.createElement("div");
    card.className = "destination";

    var head = document.createElement("div");
    head.className = "destination-head";
    var nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.setAttribute("data-field", "name");
    nameInput.placeholder = "destination name";
    nameInput.value = dest.name || "";
    var removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "danger";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", function () { card.remove(); });
    head.appendChild(nameInput);
    head.appendChild(removeBtn);
    card.appendChild(head);

    var row1 = document.createElement("div");
    row1.className = "row";
    row1.appendChild(fieldSelect("transport", "transport", schema.transports, dest.transport));
    row1.appendChild(fieldSelect("board", "board", schema.boards, dest.board));
    row1.appendChild(fieldText("host", "host", dest.host));
    row1.appendChild(fieldNumber("port", "port", dest.port));
    row1.appendChild(fieldText("channel", "channel", dest.channel));
    row1.appendChild(fieldNumber("text_budget", "text_budget", dest.text_budget, 20, 1000));
    card.appendChild(row1);

    // Safety switch -- rendered as exactly that, not a bare checkbox.
    var safety = document.createElement("div");
    safety.setAttribute("data-field", "dry_run_wrap");
    var dryCheckbox = document.createElement("input");
    dryCheckbox.type = "checkbox";
    dryCheckbox.setAttribute("data-field", "dry_run");
    dryCheckbox.checked = dest.dry_run !== false;
    var safetyLabel = document.createElement("span");
    safetyLabel.className = "label";
    function refreshSafety() {
      var isDry = dryCheckbox.checked;
      safety.className = "safety " + (isDry ? "dry" : "live");
      safetyLabel.textContent = isDry
        ? "Dry run: ON -- nothing transmits, messages only logged"
        : "LIVE -- this destination WILL transmit (real transports are not yet implemented; will fail until one exists)";
    }
    dryCheckbox.addEventListener("change", refreshSafety);
    safety.appendChild(dryCheckbox);
    safety.appendChild(safetyLabel);
    refreshSafety();
    card.appendChild(safety);

    // Kinds
    var kindsWrap = document.createElement("div");
    var kindsLabel = document.createElement("label");
    kindsLabel.textContent = "kinds";
    kindsWrap.appendChild(kindsLabel);
    var kindsRow = document.createElement("div");
    kindsRow.className = "kinds";
    kindsRow.setAttribute("data-field", "kinds");
    (schema.kinds.length ? schema.kinds : ["daily_recap", "weekly_recap", "month_honors", "net_wrapup"]).forEach(function (kind) {
      var lbl = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = kind;
      cb.checked = (dest.kinds || []).indexOf(kind) !== -1;
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(" " + kind));
      kindsRow.appendChild(lbl);
    });
    kindsWrap.appendChild(kindsRow);
    card.appendChild(kindsWrap);

    // net_ids: checklist by name when available, else manual comma field.
    var netsWrap = document.createElement("div");
    var netsLabel = document.createElement("label");
    netsLabel.textContent = "net_ids (opt-in -- none ticked = no net announcements)";
    netsWrap.appendChild(netsLabel);

    var netsRow = document.createElement("div");
    netsRow.className = "nets";
    netsRow.setAttribute("data-field", "net_ids_checklist");
    netsAvailable.forEach(function (net) {
      var lbl = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = String(net.id);
      cb.checked = (dest.net_ids || []).indexOf(net.id) !== -1;
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(" " + (net.label || ("net " + net.id)) + " [" + (net.board || "?") + "]"));
      netsRow.appendChild(lbl);
    });
    netsWrap.appendChild(netsRow);

    var fallback = document.createElement("div");
    fallback.className = "net-fallback";
    fallback.setAttribute("data-field", "net_ids_fallback_wrap");
    var fallbackLabel = document.createElement("label");
    fallbackLabel.textContent = "net_ids (manual, comma-separated -- net list unavailable)";
    var fallbackInput = document.createElement("input");
    fallbackInput.type = "text";
    fallbackInput.setAttribute("data-field", "net_ids_fallback");
    fallbackInput.placeholder = "e.g. 1, 2";
    fallbackInput.value = (dest.net_ids || []).join(", ");
    fallback.appendChild(fallbackLabel);
    fallback.appendChild(fallbackInput);
    netsWrap.appendChild(fallback);

    if (netsAvailable.length === 0) {
      netsRow.style.display = "none";
      fallback.style.display = "block";
    }

    card.appendChild(netsWrap);

    return card;
  }

  function fieldText(id, field, value) {
    var wrap = document.createElement("div");
    wrap.className = "field";
    var lbl = document.createElement("label");
    lbl.textContent = field;
    var input = document.createElement("input");
    input.type = "text";
    input.setAttribute("data-field", field);
    input.value = (value === null || value === undefined) ? "" : value;
    wrap.appendChild(lbl);
    wrap.appendChild(input);
    return wrap;
  }

  function fieldNumber(id, field, value, min, max) {
    var wrap = document.createElement("div");
    wrap.className = "field";
    var lbl = document.createElement("label");
    lbl.textContent = field;
    var input = document.createElement("input");
    input.type = "number";
    input.setAttribute("data-field", field);
    if (min !== undefined) input.min = min;
    if (max !== undefined) input.max = max;
    input.value = (value === null || value === undefined) ? "" : value;
    wrap.appendChild(lbl);
    wrap.appendChild(input);
    return wrap;
  }

  function fieldSelect(id, field, options, selected) {
    var wrap = document.createElement("div");
    wrap.className = "field";
    var lbl = document.createElement("label");
    lbl.textContent = field;
    var select = document.createElement("select");
    select.setAttribute("data-field", field);
    (options || []).forEach(function (opt) {
      var o = document.createElement("option");
      o.value = opt;
      o.textContent = opt;
      if (opt === selected) o.selected = true;
      select.appendChild(o);
    });
    wrap.appendChild(lbl);
    wrap.appendChild(select);
    return wrap;
  }

  // -- collecting the form back into a payload -----------------------------

  function collectDestinations() {
    var cards = qs("destinations-list").querySelectorAll(".destination");
    var out = [];
    cards.forEach(function (card) {
      function val(field) {
        var input = card.querySelector('[data-field="' + field + '"]');
        return input ? input.value : "";
      }
      var portRaw = val("port");
      var kinds = [];
      card.querySelectorAll('[data-field="kinds"] input[type="checkbox"]:checked').forEach(function (cb) {
        kinds.push(cb.value);
      });

      var netIds = [];
      var checklistBoxes = card.querySelectorAll('[data-field="net_ids_checklist"] input[type="checkbox"]');
      if (checklistBoxes.length > 0) {
        checklistBoxes.forEach(function (cb) {
          if (cb.checked) netIds.push(parseInt(cb.value, 10));
        });
      } else {
        var manual = val("net_ids_fallback");
        manual.split(",").forEach(function (piece) {
          piece = piece.trim();
          if (piece === "") return;
          var n = parseInt(piece, 10);
          if (!isNaN(n)) netIds.push(n);
        });
      }

      out.push({
        name: val("name"),
        transport: val("transport"),
        host: val("host") || null,
        port: portRaw === "" ? null : parseInt(portRaw, 10),
        channel: val("channel") || null,
        board: val("board"),
        dry_run: card.querySelector('[data-field="dry_run"]').checked,
        text_budget: parseInt(val("text_budget") || "150", 10),
        kinds: kinds,
        net_ids: netIds
      });
    });
    return out;
  }

  // -- network calls --------------------------------------------------------

  function loadAll() {
    hideBanner("error-banner");
    fetch("/api/nets").then(function (r) { return r.json(); }).then(function (data) {
      netsAvailable = data.nets || [];
      return fetch("/api/config");
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (result) {
      if (!result.ok) {
        showBanner("error-banner", result.data.error || "failed to load config");
        return;
      }
      renderConfig(result.data);
    }).catch(function (err) {
      showBanner("error-banner", "failed to load config: " + err);
    });
  }

  function refreshState() {
    fetch("/api/state").then(function (r) { return r.json(); }).then(function (st) {
      qs("st-cursor").textContent = (st.cursor_since === null || st.cursor_since === undefined) ? "(none yet)" : st.cursor_since;
      qs("st-lastpoll").textContent = st.last_poll_at || "(never)";
      qs("st-lasterror").textContent = st.last_error || "(none)";
      var counts = st.sent_counts || {};
      var keys = Object.keys(counts);
      qs("st-sentcounts").textContent = keys.length
        ? keys.map(function (k) { return k + ": " + counts[k]; }).join(", ")
        : "(none sent yet)";
      var reach = qs("st-reachable");
      if (st.feed_reachable === true) { reach.innerHTML = '<span class="pill up">reachable</span>'; }
      else if (st.feed_reachable === false) { reach.innerHTML = '<span class="pill down">unreachable</span>'; }
      else { reach.innerHTML = '<span class="pill unknown">unknown (no poll yet)</span>'; }

      // The single most important thing on this page: whether the bot has
      // ever obtained a safe starting cursor. Until it has, it is relaying
      // NOTHING to any destination, however many cycles it runs -- make
      // that impossible to miss rather than a buried status-table field.
      if (st.has_safe_cursor) {
        hideBanner("nocursor-banner");
      } else {
        showBanner(
          "nocursor-banner",
          "Waiting for first successful poll -- nothing will be relayed " +
          "until the feed is reachable." +
          (st.last_error ? " Last feed error: " + st.last_error : "")
        );
      }
    }).catch(function () { /* state polling failures are quiet -- not worth a banner every 5s */ });
  }

  function refreshLog() {
    fetch("/api/log?n=200").then(function (r) { return r.json(); }).then(function (data) {
      var lines = data.lines || [];
      var pre = qs("log");
      var atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
      pre.textContent = lines.length ? lines.join("\\n") : "(no dry-run log entries yet)";
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }).catch(function () { /* quiet -- log tail is best-effort */ });
  }

  function save() {
    var feedPayload = {
      base_url: qs("base_url").value,
      timeout_seconds: parseInt(qs("timeout_seconds").value || "20", 10)
    };
    if (qs("api-key-clear").checked) {
      feedPayload.api_key = "";
    } else if (qs("api-key-new").value.trim() !== "") {
      feedPayload.api_key = qs("api-key-new").value;
    }

    var payload = { feed: feedPayload, destinations: collectDestinations() };

    hideBanner("error-banner");
    hideBanner("save-banner");

    fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (result) {
      if (!result.ok) {
        showBanner("error-banner", "Save failed: " + (result.data.error || "unknown error"));
        return;
      }
      showBanner("save-banner", "Saved. Takes effect on the bot's next poll cycle.");
      renderConfig(result.data);
    }).catch(function (err) {
      showBanner("error-banner", "Save failed: " + err);
    });
  }

  qs("add-destination").addEventListener("click", function () {
    qs("destinations-list").appendChild(buildDestinationCard(null));
  });
  qs("save-config").addEventListener("click", save);

  loadAll();
  refreshState();
  refreshLog();
  setInterval(refreshState, 5000);
  setInterval(refreshLog, 4000);
})();
</script>
</body>
</html>
"""
