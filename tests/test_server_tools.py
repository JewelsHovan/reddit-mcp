"""Offline tests for the FastMCP tool layer (no network, fake session)."""

import pytest
from test_browser_session import FakeContext, FakePage

import reddit_mcp.server as server
from reddit_mcp.browser import BrowserSession
from reddit_mcp.errors import RedditHTTPError

PARTIAL_HTML = """
<shreddit-post id="t3_p1" post-title="Probe post" author="u" subreddit-name="ClaudeCode"
    score="1" comment-count="0" permalink="/r/ClaudeCode/comments/p1/probe_post/">
</shreddit-post>
"""


def _fake_session() -> BrowserSession:
    page = FakePage()
    page.next_fetch = {
        "status": 200,
        "contentType": "text/vnd.reddit.partial+html; charset=utf-8",
        "text": PARTIAL_HTML,
    }
    s = BrowserSession(profile_dir="/tmp/fake", headless=True, min_interval=0, ttl=1)
    s._ctx = FakeContext(page)
    s._page = page
    return s


def _async_getter(value):
    async def getter():
        return value

    return getter


@pytest.fixture(autouse=True)
def reset_global_session():
    yield
    server._session = None


class TestRedditStatus:
    async def test_probe_runs_before_counter_snapshot(self, monkeypatch):
        """requests_made must reflect the live probe, not predate it."""
        session = _fake_session()
        monkeypatch.setattr(server, "get_session", _async_getter(session))

        out = await server.reddit_status(live=True, probe_subreddit="ClaudeCode")

        assert out["ok"] is True
        assert out["probe"]["ok"] is True
        assert out["probe"]["posts_seen"] == 1
        assert out["requests_made"] >= 1
        assert out["cache_hits"] == 0

    async def test_no_probe_no_requests(self, monkeypatch):
        session = _fake_session()
        monkeypatch.setattr(server, "get_session", _async_getter(session))

        out = await server.reddit_status(live=False)

        assert out["ok"] is True
        assert "probe" not in out
        assert out["requests_made"] == 0

    async def test_probe_failure_reported_not_raised(self, monkeypatch):
        session = _fake_session()

        async def boom(_path):
            raise RedditHTTPError(403, "/x")

        monkeypatch.setattr(session, "fetch_partial", boom)
        monkeypatch.setattr(server, "get_session", _async_getter(session))

        out = await server.reddit_status(live=True, probe_subreddit="ClaudeCode")

        assert out["ok"] is True  # status itself is fine; the probe failed
        assert out["probe"]["ok"] is False
        assert "403" in out["probe"]["error"]


class TestListPosts:
    async def test_lists_via_partial_and_truncates(self, monkeypatch):
        session = _fake_session()
        monkeypatch.setattr(server, "get_session", _async_getter(session))

        out = await server.reddit_list_posts(subreddit="ClaudeCode", sort="hot", limit=1)

        assert out["ok"] is True
        assert out["count"] == 1
        assert out["posts"][0]["title"] == "Probe post"
        assert out["after"] is None

    async def test_rejects_bad_sort(self, monkeypatch):
        session = _fake_session()
        monkeypatch.setattr(server, "get_session", _async_getter(session))

        out = await server.reddit_list_posts(subreddit="ClaudeCode", sort="bogus")

        assert out["ok"] is False
        assert "sort" in out["error"]
