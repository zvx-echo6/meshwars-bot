import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot import webui
from meshwars_bot.config import parse_yaml_subset

EXAMPLE_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml"
)


class WebUITestCase(unittest.TestCase):
    """Base class: stands up a real ConfigUIHandler server bound to
    127.0.0.1:0 (an OS-assigned free port) against a temp copy of
    config.example.yaml, and tears it down after each test."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = os.path.join(self.tmpdir.name, "config.yaml")
        shutil.copy(EXAMPLE_CONFIG_PATH, self.config_path)

        self.status = webui.BotStatus()
        self.server = webui.build_server(self.config_path, self.status, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        # A short poll_interval here (default is 0.5s) just makes
        # server.shutdown() return quickly in tests -- no effect on
        # production, which uses start_webui()'s default.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._shutdown_server)

    def _shutdown_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            payload = None
            hdrs = dict(headers or {})
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, raw
        finally:
            conn.close()

    def get_json(self, path):
        status, raw = self.request("GET", path)
        return status, json.loads(raw.decode("utf-8"))

    def post_json(self, path, body):
        status, raw = self.request("POST", path, body=body)
        return status, json.loads(raw.decode("utf-8"))

    def read_config_raw(self):
        with open(self.config_path, "r", encoding="utf-8") as f:
            return parse_yaml_subset(f.read())


class TestIndexPage(WebUITestCase):
    def test_root_serves_self_contained_html(self):
        status, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8")
        self.assertIn("<title>meshwars-bot config</title>", html)
        # No CDN / external resources -- this must run on a LAN with no
        # internet access at all.
        self.assertNotIn("http://", html.replace("http://<host>", ""))
        self.assertNotIn("https://cdn", html.lower())
        self.assertNotIn("<script src=", html)
        self.assertNotIn("<link", html)

    def test_unknown_path_is_404(self):
        status, raw = self.request("GET", "/nope")
        self.assertEqual(status, 404)


class TestGetConfig(WebUITestCase):
    def test_api_key_value_is_never_returned(self):
        # Set a real key on disk first (bypassing the API, to be sure GET
        # really never emits it regardless of how it got there).
        raw = self.read_config_raw()
        raw["feed"]["api_key"] = "super-secret-value"
        webui.write_config(raw, self.config_path)

        status, data = self.get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertNotIn("api_key", data["feed"])
        self.assertTrue(data["feed"]["api_key_set"])
        body_text = json.dumps(data)
        self.assertNotIn("super-secret-value", body_text)

    def test_api_key_not_set_reported_false(self):
        status, data = self.get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertFalse(data["feed"]["api_key_set"])

    def test_returns_destinations_and_schema(self):
        status, data = self.get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["destinations"]), 1)
        self.assertEqual(data["destinations"][0]["name"], "mwmesh-mc")
        self.assertIn("dryrun", data["schema"]["transports"])
        self.assertIn("daily_recap", data["schema"]["kinds"])

    def test_invalid_config_on_disk_returns_500_not_a_crash(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("feed:\n\tbase_url: \"x\"\n")  # tab indentation -- a parse error
        status, data = self.get_json("/api/config")
        self.assertEqual(status, 500)
        self.assertIn("error", data)


class TestPostConfig(WebUITestCase):
    def _valid_destination(self, **overrides):
        dest = {
            "name": "mwmesh-mc",
            "transport": "dryrun",
            "host": None,
            "port": None,
            "channel": None,
            "board": "mc",
            "dry_run": True,
            "text_budget": 150,
            "kinds": ["daily_recap"],
            "net_ids": [],
        }
        dest.update(overrides)
        return dest

    def test_valid_post_saves_and_is_reflected_by_get(self):
        payload = {
            "feed": {"base_url": "https://changed.example", "timeout_seconds": 33},
            "destinations": [self._valid_destination()],
        }
        status, data = self.post_json("/api/config", payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["feed"]["base_url"], "https://changed.example")
        self.assertEqual(data["feed"]["timeout_seconds"], 33)

        status2, data2 = self.get_json("/api/config")
        self.assertEqual(data2["feed"]["base_url"], "https://changed.example")

    def test_invalid_destination_returns_400_and_names_field_and_destination(self):
        before = self.read_config_raw()

        payload = {
            "feed": {"base_url": "https://changed.example", "timeout_seconds": 20},
            "destinations": [self._valid_destination(transport="carrier-pigeon")],
        }
        status, data = self.post_json("/api/config", payload)
        self.assertEqual(status, 400)
        self.assertIn("error", data)
        self.assertIn("mwmesh-mc", data["error"])
        self.assertIn("carrier-pigeon", data["error"])

        after = self.read_config_raw()
        self.assertEqual(before, after)

    def test_invalid_kind_returns_400_and_leaves_file_unchanged(self):
        before_bytes = open(self.config_path, "rb").read()

        payload = {
            "destinations": [self._valid_destination(kinds=["not_a_real_kind"])],
        }
        status, data = self.post_json("/api/config", payload)
        self.assertEqual(status, 400)
        self.assertIn("not_a_real_kind", data["error"])

        after_bytes = open(self.config_path, "rb").read()
        self.assertEqual(before_bytes, after_bytes)

    def test_setting_api_key_then_omitting_it_keeps_it_unchanged(self):
        status, data = self.post_json("/api/config", {"feed": {"api_key": "secret123"}})
        self.assertEqual(status, 200)
        self.assertTrue(data["feed"]["api_key_set"])

        # A follow-up save that edits only base_url, with no "api_key" key
        # in the posted feed object at all, must leave the stored key alone.
        status2, data2 = self.post_json("/api/config", {"feed": {"base_url": "https://still.example"}})
        self.assertEqual(status2, 200)
        self.assertTrue(data2["feed"]["api_key_set"])
        self.assertEqual(data2["feed"]["base_url"], "https://still.example")

        raw = self.read_config_raw()
        self.assertEqual(raw["feed"]["api_key"], "secret123")

    def test_empty_string_api_key_clears_it(self):
        self.post_json("/api/config", {"feed": {"api_key": "secret123"}})
        status, data = self.post_json("/api/config", {"feed": {"api_key": ""}})
        self.assertEqual(status, 200)
        self.assertFalse(data["feed"]["api_key_set"])
        raw = self.read_config_raw()
        self.assertEqual(raw["feed"]["api_key"], "")

    def test_malformed_json_body_is_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", "/api/config", body=b"{not json", headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read())
            self.assertEqual(resp.status, 400)
            self.assertIn("error", data)
        finally:
            conn.close()

    def test_state_path_and_web_section_are_preserved_across_a_save(self):
        before = self.read_config_raw()
        self.post_json("/api/config", {"feed": {"timeout_seconds": 45}})
        after = self.read_config_raw()
        self.assertEqual(before.get("state_path"), after.get("state_path"))
        self.assertEqual(before.get("web"), after.get("web"))


class TestGetState(WebUITestCase):
    def test_defaults_before_any_poll(self):
        status, data = self.get_json("/api/state")
        self.assertEqual(status, 200)
        self.assertIsNone(data["cursor_since"])
        self.assertIsNone(data["last_poll_at"])
        self.assertIsNone(data["feed_reachable"])
        self.assertEqual(data["sent_counts"], {})

    def test_reflects_status_updates(self):
        self.status.update(
            cursor_since=42, last_poll_at="2026-09-22T00:00:00+00:00",
            last_error=None, feed_reachable=True, sent_counts={"mwmesh-mc": 3},
        )
        status, data = self.get_json("/api/state")
        self.assertEqual(data["cursor_since"], 42)
        self.assertTrue(data["feed_reachable"])
        self.assertEqual(data["sent_counts"], {"mwmesh-mc": 3})


class TestGetLog(WebUITestCase):
    def setUp(self):
        super().setUp()
        self.log_path = webui.DEFAULT_DRYRUN_LOG_PATH
        self._had_log = os.path.exists(self.log_path)
        if self._had_log:
            with open(self.log_path, "r", encoding="utf-8") as f:
                self._original_log = f.read()
        else:
            self._original_log = None
        self.addCleanup(self._restore_log)

    def _restore_log(self):
        if self._original_log is not None:
            with open(self.log_path, "w", encoding="utf-8") as f:
                f.write(self._original_log)
        elif os.path.exists(self.log_path):
            os.remove(self.log_path)

    def test_no_log_file_returns_empty_list(self):
        if os.path.exists(self.log_path):
            os.remove(self.log_path)
        status, data = self.get_json("/api/log?n=5")
        self.assertEqual(status, 200)
        self.assertEqual(data["lines"], [])

    def test_returns_last_n_lines(self):
        with open(self.log_path, "w", encoding="utf-8") as f:
            for i in range(10):
                f.write(f"line {i}\n")
        status, data = self.get_json("/api/log?n=3")
        self.assertEqual(status, 200)
        self.assertEqual(data["lines"], ["line 7", "line 8", "line 9"])


class TestGetNets(WebUITestCase):
    def setUp(self):
        super().setUp()
        self._real_urlopen = urllib.request.urlopen
        self.addCleanup(self._restore_urlopen)

    def _restore_urlopen(self):
        urllib.request.urlopen = self._real_urlopen

    def test_degrades_to_empty_list_on_404(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        urllib.request.urlopen = fake_urlopen
        status, data = self.get_json("/api/nets")
        self.assertEqual(status, 200)
        self.assertEqual(data["nets"], [])

    def test_degrades_to_empty_list_on_network_error(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        urllib.request.urlopen = fake_urlopen
        status, data = self.get_json("/api/nets")
        self.assertEqual(status, 200)
        self.assertEqual(data["nets"], [])

    def test_returns_nets_on_success(self):
        payload = {"nets": [{"id": 1, "label": "Weekly Net", "board": "mc"}]}

        class FakeResp:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            return FakeResp(json.dumps(payload).encode("utf-8"))

        urllib.request.urlopen = fake_urlopen
        status, data = self.get_json("/api/nets")
        self.assertEqual(status, 200)
        self.assertEqual(data["nets"], payload["nets"])


if __name__ == "__main__":
    unittest.main()
