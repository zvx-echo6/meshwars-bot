import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshwars_bot.feed import FeedClient


class FakeHeaders:
    def __init__(self, headers=None):
        self._headers = headers or {}

    def get(self, key, default=None):
        for k, v in self._headers.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers=None):
        self._body = body
        self.status = status
        self.headers = FakeHeaders(headers)

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class TestFeedClient(unittest.TestCase):
    def setUp(self):
        self._real_urlopen = urllib.request.urlopen
        self.client = FeedClient(base_url="https://meshwars.example", api_key="", timeout_seconds=5)

    def tearDown(self):
        urllib.request.urlopen = self._real_urlopen

    def test_successful_poll_parses_announcements_and_etag(self):
        payload = {
            "announcements": [
                {
                    "id": 10,
                    "kind": "daily_recap",
                    "key": "k10",
                    "board": "mc",
                    "net_id": None,
                    "created_at": "2026-09-21T00:00:00Z",
                    "content": {"foo": "bar"},
                    "text": "Daily recap text",
                }
            ],
            "next_since": 10,
            "poll_interval_seconds": 900,
        }
        body = json.dumps(payload).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            self.assertNotIn("If-none-match", req.headers)  # no etag stored yet, none sent
            return FakeResponse(body, status=200, headers={"ETag": '"v1"'})

        urllib.request.urlopen = fake_urlopen

        page = self.client.poll(since=None, etag=None)
        self.assertIsNotNone(page)
        self.assertEqual(len(page.announcements), 1)
        self.assertEqual(page.announcements[0].id, 10)
        self.assertEqual(page.announcements[0].text, "Daily recap text")
        self.assertEqual(page.next_since, 10)
        self.assertEqual(page.poll_interval_seconds, 900)
        self.assertEqual(page.etag, '"v1"')
        self.assertFalse(page.not_modified)
        self.assertIsNone(page.retry_after)

    def test_if_none_match_header_sent_when_etag_present(self):
        seen_headers = {}

        def fake_urlopen(req, timeout=None):
            seen_headers.update(req.headers)
            body = json.dumps({"announcements": [], "next_since": 5}).encode("utf-8")
            return FakeResponse(body, status=200, headers={})

        urllib.request.urlopen = fake_urlopen
        self.client.poll(since=5, etag='"cached-etag"')
        # urllib.request.Request title-cases header names
        self.assertEqual(seen_headers.get("If-none-match"), '"cached-etag"')

    def test_api_key_sent_as_header_when_configured(self):
        seen_headers = {}

        def fake_urlopen(req, timeout=None):
            seen_headers.update(req.headers)
            body = json.dumps({"announcements": [], "next_since": 1}).encode("utf-8")
            return FakeResponse(body, status=200, headers={})

        urllib.request.urlopen = fake_urlopen
        client = FeedClient(base_url="https://meshwars.example", api_key="secret123")
        client.poll(since=None, etag=None)
        self.assertEqual(seen_headers.get("X-api-key"), "secret123")

    def test_304_not_modified_returns_page_with_no_announcements(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)

        urllib.request.urlopen = fake_urlopen
        page = self.client.poll(since=100, etag='"same"')
        self.assertIsNotNone(page)
        self.assertTrue(page.not_modified)
        self.assertEqual(page.announcements, [])
        self.assertEqual(page.next_since, 100)

    def test_429_returns_retry_after_seconds(self):
        def fake_urlopen(req, timeout=None):
            headers = {"Retry-After": "37"}
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", headers, None)

        urllib.request.urlopen = fake_urlopen
        page = self.client.poll(since=1, etag=None)
        self.assertIsNotNone(page)
        self.assertEqual(page.retry_after, 37)
        self.assertEqual(page.announcements, [])

    def test_network_error_returns_none_not_raises(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        urllib.request.urlopen = fake_urlopen
        page = self.client.poll(since=1, etag=None)
        self.assertIsNone(page)

    def test_other_http_error_returns_none_not_raises(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

        urllib.request.urlopen = fake_urlopen
        page = self.client.poll(since=1, etag=None)
        self.assertIsNone(page)

    def test_malformed_json_returns_none_not_raises(self):
        def fake_urlopen(req, timeout=None):
            return FakeResponse(b"not json{{{", status=200, headers={})

        urllib.request.urlopen = fake_urlopen
        page = self.client.poll(since=1, etag=None)
        self.assertIsNone(page)

    def test_user_agent_header_set(self):
        seen_headers = {}

        def fake_urlopen(req, timeout=None):
            seen_headers.update(req.headers)
            body = json.dumps({"announcements": [], "next_since": 1}).encode("utf-8")
            return FakeResponse(body, status=200, headers={})

        urllib.request.urlopen = fake_urlopen
        self.client.poll(since=None, etag=None)
        self.assertIn("meshwars-bot", seen_headers.get("User-agent", ""))


if __name__ == "__main__":
    unittest.main()
