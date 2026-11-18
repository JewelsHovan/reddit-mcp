"""Offline tests for browser-session internals: pacing, cache, block detection.

Uses a fake Playwright page: no network, no real Chrome.
"""

import asyncio
import time

import pytest

from reddit_mcp.browser import BrowserSession, is_challenge_text


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakePage:
    def __init__(self):
        self.url = "https://www.reddit.com/r/ClaudeCode/"
        self.calls = []
        self.next_status = 200
        self.next_html = "<html><body>ok</body></html>"
        self.next_fetch = {
            "status": 200,
            "contentType": "text/vnd.reddit.partial+html; charset=utf-8",
            "text": "<html><body>partial</body></html>",
        }

    def is_closed(self):
        return False

    async def goto(self, url, wait_until=None):
        self.calls.append(("goto", url))
        self.url = url
        return FakeResponse(self.next_status)

    async def wait_for_load_state(self, state, timeout=None):
        return True

    async def wait_for_selector(self, sel, timeout=None):
        return True

    async def content(self):
        return self.next_html

    async def evaluate(self, script, arg=None):
        if "login" in script:
            return "logged_in"
        if "fetch" in script:
            self.calls.append(("fetch_partial", arg))
            return self.next_fetch
        return None


class FakeContext:
    def __init__(self, page):
        self.pages = [page]

    async def close(self):
        pass


@pytest.fixture
def session():
    s = BrowserSession(
        profile_dir="/tmp/fake-profile",
        headless=True,
        min_interval=0.05,  # fast for tests
        ttl=1.0,
        request_timeout=5.0,
    )
    s._ctx = FakeContext(FakePage())
    s._page = s._ctx.pages[0]
    return s


class TestChallengeDetection:
    def test_detects_prove_your_humanity(self):
        assert is_challenge_text("<html><title>Reddit - Prove your humanity</title>") is True

    def test_detects_recaptcha_script(self):
        assert is_challenge_text('<script src="https://www.google.com/recaptcha/api.js">') is True

    def test_detects_network_security_block_page(self):
        # Reddit's edge block page served to headless clients (verified live
        # 2026-11-17); must be surfaced as a block, not a silent 0-post page.
        assert (
            is_challenge_text(
                "You've been blocked by network security.\nIf you think you've "
                "been blocked by mistake, file a ticket below"
            )
            is True
        )

    def test_normal_html_is_not_challenge(self):
        assert is_challenge_text("<html><body>normal listing</body></html>") is False


class TestPacing:
    async def test_min_interval_between_requests(self, session):
        start = time.monotonic()
        await session.load_page("https://www.reddit.com/r/a/", settle=0)
        await session.load_page("https://www.reddit.com/r/b/", settle=0)
        elapsed = time.monotonic() - start

        assert elapsed >= session.min_interval  # two navigations, one gap
        assert session.requests_made == 2


class TestCache:
    async def test_second_identical_call_is_cache_hit(self, session):
        first = await session.load_page("https://www.reddit.com/r/a/", settle=0)
        second = await session.load_page("https://www.reddit.com/r/a/", settle=0)

        assert first == second
        assert session.requests_made == 1
        assert session.cache_hits == 1
        assert len(session._page.calls) == 1

    async def test_cache_expires_after_ttl(self, session):
        session.ttl = 0.05
        await session.load_page("https://www.reddit.com/r/a/", settle=0)
        await asyncio.sleep(0.08)
        await session.load_page("https://www.reddit.com/r/a/", settle=0)

        assert session.requests_made == 2

    async def test_different_urls_no_hit(self, session):
        await session.load_page("https://www.reddit.com/r/a/", settle=0)
        await session.load_page("https://www.reddit.com/r/a/?t=all", settle=0)
        assert session.cache_hits == 0
        assert session.requests_made == 2


class TestErrorHandling:
    async def test_non_200_raises_http_error(self, session):
        from reddit_mcp.errors import RedditHTTPError

        session._page.next_status = 403
        with pytest.raises(RedditHTTPError) as exc_info:
            await session.load_page("https://www.reddit.com/r/a/", settle=0)
        assert exc_info.value.status == 403

    async def test_challenge_body_raises_blocked_error(self, session):
        from reddit_mcp.errors import RedditBlockedError

        session._page.next_html = "<html><title>Reddit - Prove your humanity</title></html>"
        with pytest.raises(RedditBlockedError):
            await session.load_page("https://www.reddit.com/r/a/", settle=0)

    async def test_no_retry_after_block(self, session):
        """A blocked call must not be retried: next call starts fresh."""
        from reddit_mcp.errors import RedditBlockedError

        session._page.next_html = "Prove your humanity"
        with pytest.raises(RedditBlockedError):
            await session.load_page("https://www.reddit.com/r/a/", settle=0)
        assert session.requests_made == 1
        # Nothing was cached from the failed call
        assert session._cache == {}


class TestPartialFetch:
    async def test_partial_fetch_returns_text_and_caches(self, session):
        first = await session.fetch_partial("/svc/shreddit/community-more-posts/hot/?name=a")
        await session.fetch_partial("/svc/shreddit/community-more-posts/hot/?name=a")
        assert first == "<html><body>partial</body></html>"
        assert session.requests_made == 1
        assert session.cache_hits == 1
        assert session._page.calls[0][0] == "fetch_partial"

    async def test_partial_fetch_403_raises(self, session):
        from reddit_mcp.errors import RedditHTTPError

        session._page.next_fetch = {"status": 403, "contentType": "text/html", "text": "nope"}
        with pytest.raises(RedditHTTPError):
            await session.fetch_partial("/svc/shreddit/community-more-posts/hot/?name=a")

    async def test_partial_fetch_block_page_raises(self, session):
        from reddit_mcp.errors import RedditBlockedError

        # a block page comes back as plain text/html instead of the partial type
        session._page.next_fetch = {
            "status": 200,
            "contentType": "text/html",
            "text": "<body class=theme-beta>blocked</body>",
        }
        with pytest.raises(RedditBlockedError):
            await session.fetch_partial("/svc/shreddit/community-more-posts/hot/?name=a")

    async def test_partial_fetch_warms_origin_if_off_reddit(self, session):
        session._page.url = "about:blank"
        await session.fetch_partial("/svc/shreddit/community-more-posts/hot/?name=a")
        gotos = [c for c in session._page.calls if c[0] == "goto"]
        assert gotos and gotos[0][1] == "https://www.reddit.com"


class TestStatus:
    async def test_status_reports_state(self, session):
        status = await session.status()
        assert status["browser_running"] is True
        assert status["login_state"] in {"logged_in", "logged_out", "unknown"}
        assert status["min_interval_s"] == 0.05
