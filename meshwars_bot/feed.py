"""Polling client for the MeshWars public announcement feed.

    GET {base_url}/api/v1/announcements

Uses urllib.request ONLY -- no third-party HTTP library. This module never
raises out of poll() into the caller's loop: any HTTP error, network error,
or malformed response is logged and turned into either a `FeedPage`
carrying no announcements, or `None`. The caller's loop must stay alive
across a flaky feed.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("meshwars_bot.feed")

USER_AGENT = "meshwars-bot/0.1 (+https://github.com/meshwars/meshwars-bot)"

ANNOUNCEMENTS_PATH = "/api/v1/announcements"

DEFAULT_LIMIT = 20
DEFAULT_TEXT_BUDGET = 150


@dataclass
class Announcement:
    id: int
    kind: str
    key: Optional[str]
    board: str
    net_id: Optional[int]
    created_at: Optional[str]
    content: Dict[str, Any] = field(default_factory=dict)
    text: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Announcement":
        return cls(
            id=d["id"],
            kind=d.get("kind"),
            key=d.get("key"),
            board=d.get("board"),
            net_id=d.get("net_id"),
            created_at=d.get("created_at"),
            content=d.get("content") or {},
            text=d.get("text", ""),
        )


@dataclass
class FeedPage:
    announcements: List[Announcement] = field(default_factory=list)
    next_since: Optional[int] = None
    poll_interval_seconds: Optional[int] = None
    etag: Optional[str] = None
    not_modified: bool = False
    retry_after: Optional[int] = None


def _parse_retry_after(headers) -> Optional[int]:
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class FeedClient:
    def __init__(self, base_url: str, api_key: str = "", timeout_seconds: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.timeout_seconds = timeout_seconds

    def _build_request(
        self,
        since: Optional[int],
        etag: Optional[str],
        kinds: Optional[List[str]],
        board: Optional[str],
        net_id: Optional[int],
        limit: int,
        text_budget: int,
    ) -> urllib.request.Request:
        params: Dict[str, Any] = {}
        if since is not None:
            params["since"] = since
        if kinds:
            params["kinds"] = ",".join(kinds)
        if board is not None:
            params["board"] = board
        if net_id is not None:
            params["net_id"] = net_id
        params["limit"] = limit
        params["text_budget"] = text_budget

        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{ANNOUNCEMENTS_PATH}?{query}"

        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if etag:
            headers["If-None-Match"] = etag

        return urllib.request.Request(url, headers=headers, method="GET")

    def poll(
        self,
        since: Optional[int] = None,
        etag: Optional[str] = None,
        kinds: Optional[List[str]] = None,
        board: Optional[str] = None,
        net_id: Optional[int] = None,
        limit: int = DEFAULT_LIMIT,
        text_budget: int = DEFAULT_TEXT_BUDGET,
    ) -> Optional[FeedPage]:
        """Poll the feed once. Never raises -- returns None on any failure."""
        req = self._build_request(since, etag, kinds, board, net_id, limit, text_budget)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                status = getattr(resp, "status", 200)
                resp_headers = getattr(resp, "headers", None)
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 304:
                logger.debug("feed: 304 not modified")
                return FeedPage(next_since=since, etag=etag, not_modified=True)
            if e.code == 429:
                retry_after = _parse_retry_after(e.headers)
                logger.warning("feed: 429 rate limited, retry_after=%s", retry_after)
                return FeedPage(next_since=since, etag=etag, retry_after=retry_after)
            logger.warning("feed: HTTP error %s %s", e.code, e.reason)
            return None
        except urllib.error.URLError as e:
            logger.warning("feed: network error: %s", e.reason)
            return None
        except Exception as e:  # noqa: BLE001 - never raise into the poll loop
            logger.warning("feed: poll failed: %s", e)
            return None

        if status == 304:
            return FeedPage(next_since=since, etag=etag, not_modified=True)

        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("feed: invalid JSON response: %s", e)
            return None

        try:
            announcements = [Announcement.from_dict(a) for a in data.get("announcements", [])]
        except (KeyError, TypeError) as e:
            logger.warning("feed: malformed announcement in response: %s", e)
            return None

        new_etag = resp_headers.get("ETag") if resp_headers is not None else None

        return FeedPage(
            announcements=announcements,
            next_since=data.get("next_since", since),
            poll_interval_seconds=data.get("poll_interval_seconds"),
            etag=new_etag or etag,
        )
