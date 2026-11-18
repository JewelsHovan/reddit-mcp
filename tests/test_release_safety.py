"""Offline release-hardening regression tests (no network, no real Chrome).

Covers:
1. Strict URL allowlist, origin/route/traversal/credential/port checks, `after`
   cursor pinning, off-origin document redirects, and `fetch` redirect:error.
2. FastMCP lifespan cleanup, singleton assignment only after successful start,
   failed start cleanup, stop resilience, and `reddit-mcp-smoke` lifecycle
   (`--keep-open` waiting, closing on errors, failing empty probes).
3. `expand_subtree` single-button click pacing, bounded click budget, challenge
   checks before clicking and after each click, and actual click accounting.
4. Response size bounds (5 MiB), LRU cache bounds (16 entries), timeouts,
   `expand_subtree` return annotation, and headed default preservation.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import get_type_hints

import pytest
from test_browser_session import FakeContext, FakePage, FakeResponse

import reddit_mcp.browser as browser_mod
import reddit_mcp.live_check as live_check
import reddit_mcp.server as server
from reddit_mcp.browser import (
    CLICK_MORE_JS,
    FETCH_PARTIAL_JS,
    MAX_CACHE_ENTRIES,
    MAX_RESPONSE_BYTES,
    BrowserSession,
    build_session_from_env,
    validate_reddit_url,
)
from reddit_mcp.errors import (
    BrowserLaunchError,
    DisallowedURLError,
    RedditBlockedError,
    RedditHTTPError,
    RedditRedirectError,
    RedditResponseTooLargeError,
)


def _session_with_page(page: FakePage | None = None, **kwargs) -> BrowserSession:
    p = page or FakePage()
    defaults = {
        "profile_dir": "/tmp/fake-profile",
        "headless": True,
        "min_interval": 0.0,
        "ttl": 60.0,
        "request_timeout": 5.0,
    }
    defaults.update(kwargs)
    s = BrowserSession(**defaults)
    s._ctx = FakeContext(p)
    s._page = p
    return s


@pytest.fixture(autouse=True)
def _reset_server_session():
    yield
    server._session = None


# ---------------------------------------------------------------------------
# 1. Strict URL allowlist, cursor validation, and redirect blocking
# ---------------------------------------------------------------------------


class TestURLAllowlist:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.reddit.com/r/ClaudeCode/",
            "https://www.reddit.com/r/ClaudeCode",
            "https://www.reddit.com/r/ClaudeCode/hot/",
            "https://www.reddit.com/r/ClaudeCode/new",
            "https://www.reddit.com/r/ClaudeCode/top/?t=week",
            "https://www.reddit.com/r/ClaudeCode/comments/1abc123/x?sort=best",
            "https://www.reddit.com/r/ClaudeCode/comments/1abc123/x/c1",
            "https://www.reddit.com/r/ClaudeCode/comments/1abc123/comment/19sk1/?sort=top",
            "https://www.reddit.com/r/ClaudeCode/comments/1abc123/comment/t1_c1r1/?render-mode=partial&is_lit_ssr=false&sort=top",
            "https://www.reddit.com/search/?q=mcp+security&sort=relevance&t=all",
            "https://www.reddit.com/r/ClaudeCode/search/?q=mcp&restrict_sr=on&sort=new&t=week",
            "https://www.reddit.com/svc/shreddit/community-more-posts/hot/?name=ClaudeCode&after=dDNfMQ%3D%3D",
        ],
    )
    def test_allows_known_read_routes(self, url):
        assert validate_reddit_url(url) == url

    @pytest.mark.parametrize(
        "bad_url",
        [
            "",
            "   ",
            "http://www.reddit.com/r/ClaudeCode/",
            "https://reddit.com/r/ClaudeCode/",
            "https://old.reddit.com/r/ClaudeCode/",
            "https://oauth.reddit.com/r/ClaudeCode/",
            "https://accounts.reddit.com/login",
            "https://www.reddit.com.evil.com/r/ClaudeCode/",
            "https://evilwww.reddit.com/r/ClaudeCode/",
            "https://user@www.reddit.com/r/ClaudeCode/",
            "https://user:pass@www.reddit.com/r/ClaudeCode/",
            "https://www.reddit.com@evil.com/r/ClaudeCode/",
            "https://www.reddit.com:443/r/ClaudeCode/",
            "https://www.reddit.com:8443/r/ClaudeCode/",
            "https://www.reddit.com:/r/ClaudeCode/",
            "https://www.reddit.com\\@evil.com/r/ClaudeCode/",
            "https://www.reddit.com/r/ClaudeCode/..%5c/login",
            "https://www.reddit.com/r/ClaudeCode/%5c../login",
            "https://www.reddit.com/r/ClaudeCode/%2e%2e/login",
            "https://www.reddit.com/r/ClaudeCode/../login",
            "https://www.reddit.com/r/ClaudeCode/%2f..%2flogin",
            "https://www.reddit.com/r/ClaudeCode/%252e%252e/login",
            "https://www.reddit.com/r/ClaudeCode/%00",
            "https://www.reddit.com/r/ClaudeCode/?next=%2e%2e%2flogin",
            "https://www.reddit.com/r/ClaudeCode/?next=%5cfoo",
            "https://www.reddit.com/r/ClaudeCode/#frag",
            "https://www.reddit.com/r/ClaudeCode/;x=1",
            "https://www.reddit.com/r/ClaudeCode//hot/",
            "https://www.reddit.com/login",
            "https://www.reddit.com/register",
            "https://www.reddit.com/api/vote",
            "https://www.reddit.com/submit",
            "https://www.reddit.com/r/ClaudeCode/submit",
            "https://www.reddit.com/r/ClaudeCode/comments/1abc/edit",
            "https://www.reddit.com/user/someone",
            "https://www.reddit.com/",
            "//evil.com/r/ClaudeCode/",
            "/r/ClaudeCode/",  # relative disallowed when allow_relative=False
            "javascript:alert(1)",
            "https://www.reddit.com/r/ClaudeCode/\n",
        ],
    )
    def test_rejects_disallowed_or_malformed_urls(self, bad_url):
        with pytest.raises(DisallowedURLError):
            validate_reddit_url(bad_url)

    def test_relative_allowed_only_for_safe_paths(self):
        assert (
            validate_reddit_url(
                "/svc/shreddit/community-more-posts/hot/?name=ClaudeCode",
                allow_relative=True,
            )
            == "https://www.reddit.com/svc/shreddit/community-more-posts/hot/?name=ClaudeCode"
        )
        for bad in ("//evil.com/r/a/", "/login", "/api/vote", "/\\evil.com/"):
            with pytest.raises(DisallowedURLError):
                validate_reddit_url(bad, allow_relative=True)

    async def test_session_methods_reject_before_navigation(self):
        page = FakePage()
        session = _session_with_page(page)

        for bad in (
            "https://evil.com/r/ClaudeCode/",
            "https://www.reddit.com/login",
            "https://www.reddit.com:8080/r/ClaudeCode/",
        ):
            with pytest.raises(DisallowedURLError):
                await session.load_page(bad, settle=0)
            with pytest.raises(DisallowedURLError):
                await session.fetch_partial(bad)
            with pytest.raises(DisallowedURLError):
                await session.expand_subtree(bad, "c1", max_rounds=1, round_wait=0)
            with pytest.raises(DisallowedURLError):
                await session.extract_dom("() => 1", url=bad, settle=0)

        assert page.calls == []
        assert session.requests_made == 0


class TestRedirectBlocking:
    async def test_blocks_off_origin_document_redirect_on_load_page(self):
        class RedirectPage(FakePage):
            async def goto(self, url, wait_until=None):
                self.calls.append(("goto", url))
                self.url = "https://evil.com/phish"
                return FakeResponse(200)

        session = _session_with_page(RedirectPage())
        with pytest.raises(RedditRedirectError):
            await session.load_page("https://www.reddit.com/r/ClaudeCode/", settle=0)
        assert session._cache == {}

    async def test_blocks_disallowed_same_origin_redirect_like_login(self):
        class LoginRedirectPage(FakePage):
            async def goto(self, url, wait_until=None):
                self.calls.append(("goto", url))
                self.url = "https://www.reddit.com/login"
                return FakeResponse(200)

        session = _session_with_page(LoginRedirectPage())
        with pytest.raises(RedditRedirectError):
            await session.load_page("https://www.reddit.com/r/ClaudeCode/", settle=0)
        assert session._cache == {}

    async def test_route_handler_aborts_off_origin_navigation(self):
        class FakeRequest:
            def __init__(self, url, resource_type="document", is_nav=True):
                self.url = url
                self.resource_type = resource_type
                self.frame = None
                self._is_nav = is_nav

            def is_navigation_request(self):
                return self._is_nav

        class FakeRoute:
            def __init__(self, request):
                self.request = request
                self.aborted = False
                self.continued = False

            async def abort(self, _reason=None):
                self.aborted = True

            async def continue_(self):
                self.continued = True

        session = _session_with_page()
        bad_route = FakeRoute(FakeRequest("https://accounts.reddit.com/login"))
        await session._handle_route(bad_route)
        assert bad_route.aborted is True
        assert bad_route.continued is False

        ok_route = FakeRoute(FakeRequest("https://www.reddit.com/r/ClaudeCode/"))
        await session._handle_route(ok_route)
        assert ok_route.continued is True
        assert ok_route.aborted is False

    async def test_fetch_partial_uses_redirect_error_and_rejects_redirects(self):
        assert 'redirect: "error"' in FETCH_PARTIAL_JS

        page = FakePage()
        session = _session_with_page(page)

        page.next_fetch = {
            "status": 0,
            "redirected": True,
            "url": "https://evil.com",
            "contentType": "",
            "text": "",
        }
        with pytest.raises(RedditRedirectError):
            await session.fetch_partial("/svc/shreddit/community-more-posts/hot/?name=ClaudeCode")

        page.next_fetch = {"status": 0, "error": "Failed to fetch", "contentType": "", "text": ""}
        with pytest.raises(RedditRedirectError):
            await session.fetch_partial("/svc/shreddit/community-more-posts/hot/?name=ClaudeCode")

        assert session._cache == {}

    async def test_fetch_partial_warmup_rejects_lookalike_origin(self):
        page = FakePage()
        page.url = "https://www.reddit.com.evil.com/r/ClaudeCode/"
        session = _session_with_page(page)
        await session.fetch_partial("/svc/shreddit/community-more-posts/hot/?name=ClaudeCode")
        gotos = [c for c in page.calls if c[0] == "goto"]
        assert gotos == [("goto", "https://www.reddit.com")]


class TestMCPInputAndCursorHardening:
    @pytest.mark.parametrize(
        "bad_after",
        [
            "https://evil.com/svc/shreddit/community-more-posts/hot/?name=ClaudeCode",
            "//evil.com/svc/shreddit/community-more-posts/hot/?name=ClaudeCode",
            "/api/vote",
            "/login",
            "/r/OtherSub/",
            "/svc/shreddit/community-more-posts/hot/?name=OtherSub",
            "/svc/shreddit/community-more-posts/hot/?name=ClaudeCode&name=OtherSub",
            "/svc/shreddit/community-more-posts/hot/",
            "not-a-valid-cursor",
        ],
    )
    async def test_list_posts_rejects_arbitrary_or_cross_subreddit_after(
        self, monkeypatch, bad_after
    ):
        page = FakePage()
        session = _session_with_page(page)

        async def getter():
            return session

        monkeypatch.setattr(server, "get_session", getter)

        out = await server.reddit_list_posts(subreddit="ClaudeCode", after=bad_after)
        assert out["ok"] is False
        assert page.calls == []

    async def test_list_posts_accepts_same_subreddit_after_cursor(self, monkeypatch):
        page = FakePage()
        page.next_fetch = {
            "status": 200,
            "contentType": "text/vnd.reddit.partial+html; charset=utf-8",
            "text": '<shreddit-post id="t3_p1" post-title="Hi" author="u" subreddit-name="ClaudeCode" score="1" comment-count="0" permalink="/r/ClaudeCode/comments/p1/x/"></shreddit-post>',
        }
        session = _session_with_page(page)

        async def getter():
            return session

        monkeypatch.setattr(server, "get_session", getter)

        cursor = "/svc/shreddit/community-more-posts/hot/?after=dDNfMQ%3D%3D&name=claudecode"
        out = await server.reddit_list_posts(subreddit="ClaudeCode", after=cursor)
        assert out["ok"] is True
        assert out["count"] == 1

    async def test_get_thread_and_more_comments_reject_bad_urls_and_ids(self, monkeypatch):
        page = FakePage()
        session = _session_with_page(page)

        async def getter():
            return session

        monkeypatch.setattr(server, "get_session", getter)

        bad_thread = await server.reddit_get_thread(
            "https://evil.com/r/ClaudeCode/comments/1abc/x/"
        )
        assert bad_thread["ok"] is False

        bad_anchor = await server.reddit_get_more_comments(
            anchor_url="https://evil.com/r/ClaudeCode/comments/1abc/comment/c1/"
        )
        assert bad_anchor["ok"] is False

        mismatch = await server.reddit_get_more_comments(
            subreddit="OtherSub",
            anchor_url="https://www.reddit.com/r/ClaudeCode/comments/1abc/comment/c1/",
        )
        assert mismatch["ok"] is False

        bad_cid = await server.reddit_get_more_comments(
            subreddit="ClaudeCode", post_id="1abc", comment_id="../login"
        )
        assert bad_cid["ok"] is False
        assert page.calls == []

    async def test_load_more_comments_skips_poisoned_dom_anchors(self):
        page = FakePage()
        session = _session_with_page(page)
        tree = [{"id": "c1", "depth": 0, "body": "ok", "replies": []}]
        anchors = [
            {"parent_id": "c1", "href": "https://evil.com/api/vote", "label": "bad1"},
            {"parent_id": "c1", "href": "/login", "label": "bad2"},
        ]
        merged, fetched, _remaining = await server._load_more_comments(
            session, tree, anchors, max_comments=20, max_body_chars=500, sort="top"
        )
        assert merged == 0
        assert fetched == 0
        assert page.calls == []


# ---------------------------------------------------------------------------
# 2. Lifecycle: lifespan, singleton, start/stop cleanup, smoke CLI
# ---------------------------------------------------------------------------


class TestLifecycleAndSmoke:
    async def test_get_session_only_assigns_singleton_after_successful_start(self, monkeypatch):
        attempts = 0
        stopped = []

        class FlakySession:
            def __init__(self, fail: bool):
                self.fail = fail

            async def start(self):
                if self.fail:
                    raise BrowserLaunchError("boom")

            async def stop(self):
                stopped.append(self)

        def fake_build():
            nonlocal attempts
            attempts += 1
            return FlakySession(fail=(attempts == 1))

        monkeypatch.setattr(server, "build_session_from_env", fake_build)

        with pytest.raises(BrowserLaunchError):
            await server.get_session()
        assert server._session is None
        assert len(stopped) == 1

        # Next call can recover because broken session was NOT cached as singleton.
        ok_session = await server.get_session()
        assert server._session is ok_session

    async def test_fastmcp_lifespan_stops_chrome_reliably(self, monkeypatch):
        stopped = []

        class DummySession:
            async def start(self):
                pass

            async def stop(self):
                stopped.append(True)

        monkeypatch.setattr(server, "build_session_from_env", lambda: DummySession())
        await server.get_session()
        assert server._session is not None

        async with server.mcp._mcp_server.lifespan(server.mcp._mcp_server):
            assert server._session is not None

        assert stopped == [True]
        assert server._session is None

    async def test_fastmcp_lifespan_stops_chrome_even_on_exception(self, monkeypatch):
        stopped = []

        class DummySession:
            async def start(self):
                pass

            async def stop(self):
                stopped.append(True)

        monkeypatch.setattr(server, "build_session_from_env", lambda: DummySession())
        await server.get_session()

        with pytest.raises(RuntimeError, match="crash"):
            async with server.mcp._mcp_server.lifespan(server.mcp._mcp_server):
                raise RuntimeError("crash")

        assert stopped == [True]
        assert server._session is None

    async def test_start_cleans_up_playwright_when_launch_fails(self, monkeypatch, tmp_path):
        pw_stopped = []

        class BrokenChromium:
            async def launch_persistent_context(self, **_kw):
                raise RuntimeError("chrome missing")

        class FakePW:
            chromium = BrokenChromium()

            async def stop(self):
                pw_stopped.append(True)

        class FakeStarter:
            async def start(self):
                return FakePW()

        monkeypatch.setattr(browser_mod, "async_playwright", lambda: FakeStarter())

        s = BrowserSession(profile_dir=tmp_path / "prof")
        with pytest.raises(BrowserLaunchError):
            await s.start()

        assert pw_stopped == [True]
        assert s._ctx is None
        assert s._pw is None
        assert s._page is None

    async def test_stop_still_stops_playwright_if_ctx_close_fails(self):
        events = []

        class BrokenCtx:
            async def close(self):
                events.append("ctx_close_fail")
                raise RuntimeError("ctx close boom")

        class FakePW:
            async def stop(self):
                events.append("pw_stop")

        s = BrowserSession(profile_dir="/tmp/p")
        s._ctx = BrokenCtx()
        s._page = object()
        s._pw = FakePW()

        with pytest.raises(RuntimeError, match="ctx close boom"):
            await s.stop()

        assert events == ["ctx_close_fail", "pw_stop"]
        assert s._ctx is None
        assert s._page is None
        assert s._pw is None

    async def test_smoke_fails_empty_probe_and_closes(self, monkeypatch):
        events = []

        class EmptySession:
            profile_dir = "/tmp/p"
            headless = False

            async def start(self):
                events.append("start")

            async def login_state(self):
                return "logged_out"

            async def load_page(self, _url):
                return "<html><body><div>nothing here</div></body></html>"

            async def stop(self):
                events.append("stop")

        monkeypatch.setattr(live_check, "build_session_from_env", lambda: EmptySession())
        rc = await live_check.run("ClaudeCode", keep_open=True, wait_fn=lambda: None)
        assert rc == 1
        assert events == ["start", "stop"]

    async def test_smoke_closes_on_error_even_with_keep_open(self, monkeypatch):
        events = []

        class ErrorSession:
            profile_dir = "/tmp/p"
            headless = False

            async def start(self):
                events.append("start")

            async def login_state(self):
                return "logged_out"

            async def load_page(self, _url):
                raise RedditHTTPError(403, "https://www.reddit.com/r/ClaudeCode/")

            async def stop(self):
                events.append("stop")

        monkeypatch.setattr(live_check, "build_session_from_env", lambda: ErrorSession())
        rc = await live_check.run("ClaudeCode", keep_open=True, wait_fn=lambda: None)
        assert rc == 1
        assert events == ["start", "stop"]

    async def test_smoke_keep_open_waits_then_stops(self, monkeypatch):
        events = []
        ok_html = (
            '<shreddit-post id="t3_1" post-title="Hello" author="u" '
            'subreddit-name="ClaudeCode" score="1" comment-count="0" '
            'permalink="/r/ClaudeCode/comments/1/x/"></shreddit-post>'
        )

        class OkSession:
            profile_dir = "/tmp/p"
            headless = False

            async def start(self):
                events.append("start")

            async def login_state(self):
                return "logged_in"

            async def load_page(self, _url):
                return ok_html

            async def stop(self):
                events.append("stop")

        def wait_enter():
            events.append("waited")

        monkeypatch.setattr(live_check, "build_session_from_env", lambda: OkSession())
        rc = await live_check.run("ClaudeCode", keep_open=True, wait_fn=wait_enter)
        assert rc == 0
        assert events == ["start", "waited", "stop"]

        # KeyboardInterrupt during wait still closes cleanly.
        events.clear()

        def wait_interrupt():
            events.append("interrupt")
            raise KeyboardInterrupt

        rc2 = await live_check.run("ClaudeCode", keep_open=True, wait_fn=wait_interrupt)
        assert rc2 == 0
        assert events == ["start", "interrupt", "stop"]


# ---------------------------------------------------------------------------
# 3. expand_subtree click pacing, budget cap, and challenge checks
# ---------------------------------------------------------------------------


class TestExpandSubtreeHardening:
    def test_click_more_js_caps_to_one_visible_enabled_button(self):
        # Single-button return + visibility/disabled guards present in script.
        assert "return 1;" in CLICK_MORE_JS
        assert "b.disabled" in CLICK_MORE_JS
        assert "aria-disabled" in CLICK_MORE_JS
        assert "visibility === 'hidden'" in CLICK_MORE_JS

    async def test_click_pacing_and_budget_cap(self):
        click_times = []

        class MultiButtonPage(FakePage):
            def __init__(self):
                super().__init__()
                self.buttons_remaining = 5
                self.next_html = "<html><body>ok</body></html>"

            async def evaluate(self, script, arg=None):
                if "focusId" in script or "more replies" in script:
                    if self.buttons_remaining > 0:
                        self.buttons_remaining -= 1
                        click_times.append(time.monotonic())
                        return 1
                    return 0
                return await super().evaluate(script, arg)

        page = MultiButtonPage()
        session = _session_with_page(page, min_interval=0.05, max_expand_clicks=3)

        # Requesting max_rounds=10 is still bounded by max_expand_clicks=3.
        html, (rounds, clicks) = await session.expand_subtree(
            "https://www.reddit.com/r/LocalLLaMA/comments/1abc/comment/c1/?sort=top",
            "c1",
            max_rounds=10,
            round_wait=0.0,
        )
        assert html == "<html><body>ok</body></html>"
        assert rounds == 3
        assert clicks == 3
        # 1 navigation + 3 executed clicks = 4 initiated browser actions.
        assert session.requests_made == 4
        assert len(click_times) == 3
        assert click_times[1] - click_times[0] >= 0.04
        assert click_times[2] - click_times[1] >= 0.04

    async def test_challenge_before_clicking_blocks_without_clicking(self):
        clicked_calls = []

        class ChallengePage(FakePage):
            def __init__(self):
                super().__init__()
                self.next_html = "<html><title>Reddit - Prove your humanity</title></html>"

            async def evaluate(self, script, arg=None):
                if "more replies" in script:
                    clicked_calls.append(True)
                    return 1
                return await super().evaluate(script, arg)

        session = _session_with_page(ChallengePage())
        with pytest.raises(RedditBlockedError):
            await session.expand_subtree(
                "https://www.reddit.com/r/LocalLLaMA/comments/1abc/comment/c1/?sort=top",
                "c1",
                max_rounds=3,
                round_wait=0.0,
            )
        assert clicked_calls == []
        assert session.requests_made == 1  # only the initial goto

    async def test_challenge_after_click_stops_immediately(self):
        clicks = 0

        class BlockAfterClickPage(FakePage):
            def __init__(self):
                super().__init__()
                self.next_html = "<html><body>initial ok</body></html>"

            async def evaluate(self, script, arg=None):
                nonlocal clicks
                if "more replies" in script:
                    clicks += 1
                    self.next_html = "<html><body>Whoa there, pardner!</body></html>"
                    return 1
                return await super().evaluate(script, arg)

        session = _session_with_page(BlockAfterClickPage())
        with pytest.raises(RedditBlockedError):
            await session.expand_subtree(
                "https://www.reddit.com/r/LocalLLaMA/comments/1abc/comment/c1/?sort=top",
                "c1",
                max_rounds=5,
                round_wait=0.0,
            )
        assert clicks == 1
        assert session.requests_made == 2  # 1 goto + 1 click


# ---------------------------------------------------------------------------
# 4. Response/cache bounds, timeouts, annotation, headed default
# ---------------------------------------------------------------------------


class TestBoundsTimeoutsAndMetadata:
    def test_defaults_bounds_annotation_and_headed(self, monkeypatch):
        assert MAX_RESPONSE_BYTES == 5 * 1024 * 1024
        assert MAX_CACHE_ENTRIES == 16

        hints = get_type_hints(BrowserSession.expand_subtree)
        assert hints["return"] == tuple[str, tuple[int, int]]

        monkeypatch.delenv("REDDIT_MCP_HEADLESS", raising=False)
        env_session = build_session_from_env()
        assert env_session.headless is False
        assert env_session.max_response_bytes == 5 * 1024 * 1024
        assert env_session.max_cache_entries == 16

    async def test_response_size_bound_enforced_on_page_and_partial(self):
        page = FakePage()
        page.next_html = "x" * 200
        page.next_fetch = {
            "status": 200,
            "contentType": "text/vnd.reddit.partial+html",
            "text": "y" * 200,
        }
        session = _session_with_page(page, max_response_bytes=100)

        with pytest.raises(RedditResponseTooLargeError):
            await session.load_page("https://www.reddit.com/r/a/", settle=0)
        with pytest.raises(RedditResponseTooLargeError):
            await session.fetch_partial("/svc/shreddit/community-more-posts/hot/?name=a")
        assert session._cache == {}

    async def test_lru_cache_eviction_at_capacity(self):
        session = _session_with_page(max_cache_entries=3, ttl=60.0)
        await session.load_page("https://www.reddit.com/r/sub1/", settle=0)
        await session.load_page("https://www.reddit.com/r/sub2/", settle=0)
        await session.load_page("https://www.reddit.com/r/sub3/", settle=0)

        # Touch sub1 so sub2 becomes least-recently-used.
        await session.load_page("https://www.reddit.com/r/sub1/", settle=0)
        assert session.cache_hits == 1

        # Insert sub4 -> evicts sub2, keeps sub1, sub3, sub4.
        await session.load_page("https://www.reddit.com/r/sub4/", settle=0)
        assert len(session._cache) == 3
        assert "https://www.reddit.com/r/sub2/" not in session._cache
        assert "https://www.reddit.com/r/sub1/" in session._cache

    async def test_content_read_timeout_raises_http_error(self):
        class SlowContentPage(FakePage):
            async def content(self):
                await asyncio.sleep(0.2)
                return "<html></html>"

        session = _session_with_page(SlowContentPage(), request_timeout=0.02)
        with pytest.raises(RedditHTTPError, match="timed out"):
            await session.load_page("https://www.reddit.com/r/a/", settle=0)

    async def test_start_does_not_navigate_unconditionally(self, monkeypatch, tmp_path):
        page = FakePage()
        page.url = "about:blank"

        class FakeCtx:
            def __init__(self):
                self.pages = [page]

            async def route(self, _pattern, _handler):
                pass

            async def close(self):
                pass

        class FakeChromium:
            async def launch_persistent_context(self, **_kw):
                return FakeCtx()

        class FakePW:
            chromium = FakeChromium()

            async def stop(self):
                pass

        class FakeStarter:
            async def start(self):
                return FakePW()

        monkeypatch.setattr(browser_mod, "async_playwright", lambda: FakeStarter())

        s = BrowserSession(profile_dir=tmp_path / "prof", min_interval=0.0)
        await s.start()
        assert page.calls == []
        assert s.requests_made == 0

        # fetch_partial warms up origin on demand and counts it.
        await s.fetch_partial("/svc/shreddit/community-more-posts/hot/?name=a")
        assert page.calls[0] == ("goto", "https://www.reddit.com")
        assert s.requests_made == 2  # 1 warmup + 1 partial fetch

    async def test_status_discloses_counter_semantics(self, monkeypatch):
        session = _session_with_page()

        async def getter():
            return session

        monkeypatch.setattr(server, "get_session", getter)
        out = await server.reddit_status(live=False)
        assert out["ok"] is True
        assert "counter_semantics" in out
        assert "initiated browser actions" in out["counter_semantics"]
        assert out["requests_made"] == 0

    async def test_start_enforces_0700_profile_dir(self, monkeypatch, tmp_path):
        prof = tmp_path / "private-prof"
        prof.mkdir(mode=0o755)
        os.chmod(prof, 0o755)

        page = FakePage()

        class FakeCtx:
            pages = [page]

            async def route(self, _pattern, _handler):
                pass

            async def close(self):
                pass

        class FakeChromium:
            async def launch_persistent_context(self, **_kw):
                return FakeCtx()

        class FakePW:
            chromium = FakeChromium()

            async def stop(self):
                pass

        class FakeStarter:
            async def start(self):
                return FakePW()

        monkeypatch.setattr(browser_mod, "async_playwright", lambda: FakeStarter())

        s = BrowserSession(profile_dir=prof)
        await s.start()
        if os.name == "posix":
            assert (prof.stat().st_mode & 0o777) == 0o700

    async def test_start_refuses_os_chrome_default_dirs_without_modifying(
        self, monkeypatch, tmp_path
    ):
        fake_home = tmp_path / "home"
        mac_default = fake_home / "Library" / "Application Support" / "Google" / "Chrome"
        linux_default = fake_home / ".config" / "google-chrome"
        win_local = tmp_path / "win_local"
        win_default = win_local / "Google" / "Chrome" / "User Data"

        for d in (mac_default, linux_default, win_default):
            d.mkdir(parents=True)
            os.chmod(d, 0o755)

        symlink_to_default = tmp_path / "sneaky-link"
        symlink_to_default.symlink_to(mac_default)

        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))
        monkeypatch.setenv("LOCALAPPDATA", str(win_local))

        launched = []

        def fail_if_launched():
            launched.append(True)
            raise AssertionError("playwright should not launch")

        monkeypatch.setattr(browser_mod, "async_playwright", fail_if_launched)

        for bad in (
            mac_default,
            mac_default / "Default",
            linux_default,
            win_default,
            symlink_to_default,
        ):
            s = BrowserSession(profile_dir=bad)
            with pytest.raises(BrowserLaunchError, match="default user-data-dir"):
                await s.start()

        assert launched == []
        if os.name == "posix":
            # Existing real profile directories were not modified (remained 0755).
            assert (mac_default.stat().st_mode & 0o777) == 0o755
            assert (linux_default.stat().st_mode & 0o777) == 0o755

    async def test_extract_dom_acquires_lock(self):
        session = _session_with_page()
        await session._lock.acquire()
        task = asyncio.create_task(session.extract_dom("() => ({})", settle=0))
        await asyncio.sleep(0.02)
        assert not task.done()
        session._lock.release()
        res = await task
        assert res is None
