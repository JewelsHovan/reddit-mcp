"""Guard regressions for canonical redirects and human-only smoke login."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reddit_mcp import live_check
from reddit_mcp.browser import BrowserSession, validate_reddit_url
from reddit_mcp.errors import RedditBlockedError


def test_canonical_thread_title_slug_is_a_read_route():
    url = (
        "https://www.reddit.com/r/LocalLLaMA/comments/1xuvexw/"
        "stories_coming_out_about_jensen/?sort=top"
    )
    assert validate_reddit_url(url) == url


async def test_manual_login_phase_only_removes_own_guard(tmp_path):
    session = BrowserSession(tmp_path / "profile")
    context = SimpleNamespace(unroute=AsyncMock())
    session._ctx = context
    await session.enable_manual_browsing()
    context.unroute.assert_awaited_once_with("**/*", session._handle_route)


@pytest.mark.parametrize("blocked", [True, False])
async def test_keep_open_allows_human_inspection_on_failed_probe(monkeypatch, blocked):
    events = []

    class Session:
        profile_dir = "/tmp/fake-manual-profile"
        headless = False

        async def start(self):
            events.append("start")

        async def load_page(self, _url):
            if blocked:
                raise RedditBlockedError("challenge")
            return "<html><body>no posts</body></html>"

        async def login_state(self):
            return "logged_out"

        async def enable_manual_browsing(self):
            events.append("manual")

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr(live_check, "build_session_from_env", Session)
    rc = await live_check.run("ClaudeCode", keep_open=True, wait_fn=lambda: events.append("wait"))
    assert rc == 1  # manual inspection never turns the failed probe into success
    assert events == ["start", "manual", "wait", "stop"]
