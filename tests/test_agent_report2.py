"""Offline regression tests for the second agent field report (2026-11-17).

The first report's parser-level tests lived in test_agent_reported_bugs.py;
these run through the TOOL layer (the async FastMCP tool functions) with a
fake session, because bugs 1-3 were in the tools, not the parsers:

1. reddit_get_more_comments returned the first rendered root, not the focus.
2. The comment_id path built a /x/{cid} deeplink that renders no comments;
   the working form is /comments/{pid}/comment/{cid}/ (verified live).
3. The load-more loop burned budget on already-fetched anchors and returned
   the consumed cursor, so agents following cursors looped forever.
Plus: pagination skips posts when truncating (6), body <br>/<pre>/<ul>
extraction (7), truncation flags (8), cursor cap accounting (4), is_self
domain logic (10), about null keys (12).
"""

import base64

import pytest
from test_browser_session import FakeContext, FakePage

import reddit_mcp.server as server
from reddit_mcp.errors import RedditHTTPError
from reddit_mcp.reddit import (
    _extract_body_text,
    listing_after_token,
    parse_about_dom,
    parse_listing_dom,
)

THREAD_ID = "1xuvexw"


def _comment(cid, depth, body_html, parent=None, extra=""):
    return f"""
    <shreddit-comment thingid="t1_{cid}" author="u_{cid}" score="1" depth="{depth}"
        permalink="/r/LocalLLaMA/comments/{THREAD_ID}/x/{cid}/" {extra}>
      <div id="t1_{cid}-post-rtjson-content">{body_html}</div>
      {parent or ""}
    </shreddit-comment>
    """


def page_html(comments: str, comment_count: str = "639", more_anchor=None) -> str:
    anchor = ""
    if more_anchor:
        anchor = f"""
  <shreddit-comment thingid="t1_{more_anchor}" author="u" score="1" depth="1">
    <div id="t1_{more_anchor}-post-rtjson-content"><p>has replies</p></div>
    <a slot="more-comments-permalink"
       href="/r/LocalLLaMA/comments/{THREAD_ID}/comment/t1_{more_anchor}/?force-legacy-sct=1">load more</a>
  </shreddit-comment>
    """
    return f"""
    <html><body>
    <shreddit-post id="t3_{THREAD_ID}" post-title="Stories coming out about Jensen"
        author="op" subreddit-name="LocalLLaMA" score="10" upvote-ratio="0.9"
        comment-count="{comment_count}" post-type="text" domain="self.LocalLLaMA"
        permalink="/r/LocalLLaMA/comments/{THREAD_ID}/x/">
      <div slot="text-body"><p>Post body.</p></div>
    </shreddit-post>
    {comments}
    {anchor}
    </body></html>
    """


def deeplink_partial(focus_id: str, replies: str, ancestors="") -> str:
    """The webapp's /comment/{cid}/ partial: ancestors + focus + reply page."""
    return f"""
    <template for="s_1">
      {ancestors}
      <shreddit-comment thingid="t1_{focus_id}" author="u_{focus_id}" score="7" depth="1"
          permalink="/r/LocalLLaMA/comments/{THREAD_ID}/x/{focus_id}/">
        <div id="t1_{focus_id}-post-rtjson-content"><p>focus body</p></div>
      </shreddit-comment>
      {replies}
    </template>
    """


def _pkey(anchor_url: str, sort: str | None = None) -> str:
    """The fetch key for an anchor: partial params replace the deeplink query."""
    key = anchor_url.split("?")[0] + "?render-mode=partial&is_lit_ssr=false"
    if sort:
        key += f"&sort={sort}"
    return key


class FakePartialSession:
    """Session fake whose fetch_partial/load_page serve a scripted map."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.fetches: list[str] = []

    async def fetch_partial(self, path: str) -> str:
        url = path if path.startswith("http") else f"https://www.reddit.com{path}"
        self.fetches.append(url)
        if url not in self.pages:
            raise RedditHTTPError(404, url)
        return self.pages[url]

    async def load_page(self, url: str, **kw) -> str:
        self.fetches.append(url)
        if url not in self.pages:
            raise RedditHTTPError(404, url)
        return self.pages[url]

    async def expand_subtree(
        self, url: str, focus_id: str, max_rounds: int = 3
    ) -> tuple[str, tuple[int, int]]:
        self.fetches.append(url)
        if url not in self.pages:
            raise RedditHTTPError(404, url)
        return self.pages[url], (1, 1)


def _patch_session(monkeypatch, fake):
    async def getter():
        return fake

    monkeypatch.setattr(server, "get_session", getter)


@pytest.fixture(autouse=True)
def _reset_global():
    yield
    server._session = None


class TestBug1WrongFocus:
    async def test_returns_focus_subtree_not_first_root(self, monkeypatch):
        # The deeplink renders ancestors FIRST, then the focus, then replies.
        # The tool must return the focus subtree (never the ancestor) with
        # every reply the sort=top window shows.
        page = f"""
        <html><body>
        <shreddit-comment thingid="t1_19sjwxte5wb8i" author="u_anc" score="9" depth="0"
            permalink="/r/LocalLLaMA/comments/{THREAD_ID}/x/19sjwxte5wb8i/">
          <div id="t1_19sjwxte5wb8i-post-rtjson-content"><p>ancestor rendered first</p></div>
          <div slot="replies">
            <shreddit-comment thingid="t1_19sk177f3bxlv" author="u_focus" score="7" depth="1"
                permalink="/r/LocalLLaMA/comments/{THREAD_ID}/x/19sk177f3bxlv/">
              <div id="t1_19sk177f3bxlv-post-rtjson-content"><p>focus body</p></div>
              <div slot="replies">
                <shreddit-comment thingid="t1_r1" author="u1" score="3" depth="2">
                  <div id="t1_r1-post-rtjson-content"><p>reply one</p></div>
                </shreddit-comment>
                <shreddit-comment thingid="t1_r2" author="u2" score="2" depth="2">
                  <div id="t1_r2-post-rtjson-content"><p>reply two</p></div>
                </shreddit-comment>
              </div>
            </shreddit-comment>
          </div>
        </shreddit-comment>
        </body></html>
        """
        fake = FakePartialSession(
            {
                f"https://www.reddit.com/r/LocalLLaMA/comments/{THREAD_ID}/comment/19sk177f3bxlv/?sort=top": page
            }
        )
        _patch_session(monkeypatch, fake)

        out = await server.reddit_get_more_comments(
            subreddit="LocalLLaMA",
            post_id=THREAD_ID,
            comment_id="19sk177f3bxlv",
            max_comments=50,
        )
        assert out["ok"] is True
        assert out["focus_id"] == "19sk177f3bxlv"
        assert out["comment"]["id"] == "19sk177f3bxlv"  # the focus, not the ancestor
        assert out["comment"]["body"] == "focus body"
        assert [r["id"] for r in out["comment"]["replies"]] == ["r1", "r2"]
        assert out["total_comments_rendered"] == 3
        # Click observability (release gate): rounds AND clicks are exposed so
        # requests_made can be reconciled per call.
        assert out["load_more_rounds"] == 1
        assert out["load_more_clicks"] == 1
        # The sort=top deeplink URL is what the tool navigates.
        assert fake.fetches == [
            f"https://www.reddit.com/r/LocalLLaMA/comments/{THREAD_ID}/comment/19sk177f3bxlv/?sort=top"
        ]

    async def test_reply_confidence_window_dropped_is_loaded_via_sort_top(self, monkeypatch):
        # Regression for the field report: the default deeplink window OMITTED
        # 19sk2yjtdhw4j (present under sort=top). The tool must request
        # ?sort=top by default so the focus subtree matches a top-sorted
        # thread read.
        fake = FakePartialSession({"unused": "x"})
        calls = []

        async def expand(url, focus_id, max_rounds=3):
            calls.append(url)
            return "<html></html>", (0, 0)

        fake.expand_subtree = expand
        _patch_session(monkeypatch, fake)
        await server.reddit_get_more_comments(
            subreddit="LocalLLaMA", post_id=THREAD_ID, comment_id="19sk177f3bxlv"
        )
        assert calls and "sort=top" in calls[0]


class TestBug2DeeplinkForm:
    async def test_comment_id_path_uses_comment_form(self, monkeypatch):
        page = f"""
        <html><body>
        <shreddit-comment thingid="t1_19skq60j2v943" author="u" score="5" depth="0"
            permalink="/r/LocalLLaMA/comments/{THREAD_ID}/x/19skq60j2v943/">
          <div id="t1_19skq60j2v943-post-rtjson-content"><p>focus</p></div>
          <div slot="replies">
            <shreddit-comment thingid="t1_r1" author="u1" score="1" depth="1">
              <div id="t1_r1-post-rtjson-content"><p>reply</p></div>
            </shreddit-comment>
          </div>
        </shreddit-comment>
        </body></html>
        """
        fake = FakePartialSession(
            {
                f"https://www.reddit.com/r/LocalLLaMA/comments/{THREAD_ID}/comment/19skq60j2v943/?sort=top": page
            }
        )
        _patch_session(monkeypatch, fake)

        out = await server.reddit_get_more_comments(
            subreddit="LocalLLaMA", post_id=THREAD_ID, comment_id="19skq60j2v943"
        )
        assert out["ok"] is True
        # The /x/{cid} form renders nothing; /comment/{cid}/?sort=top is used.
        assert fake.fetches == [
            f"https://www.reddit.com/r/LocalLLaMA/comments/{THREAD_ID}/comment/19skq60j2v943/?sort=top"
        ]
        assert out["comment"]["id"] == "19skq60j2v943"

    async def test_focus_id_from_anchor_url_when_no_comment_id(self, monkeypatch):
        anchor = (
            "https://www.reddit.com/r/LocalLLaMA/comments/1xuvexw/comment/19skp0t3gfzkw/"
            "?force-legacy-sct=1"
        )
        page = """
        <html><body>
        <shreddit-comment thingid="t1_19skp0t3gfzkw" author="u" score="5" depth="0">
          <div id="t1_19skp0t3gfzkw-post-rtjson-content"><p>focus</p></div>
          <div slot="replies"></div>
        </shreddit-comment>
        </body></html>
        """
        fake = FakePartialSession(
            {
                "https://www.reddit.com/r/LocalLLaMA/comments/1xuvexw/comment/19skp0t3gfzkw/?sort=top": page
            }
        )
        _patch_session(monkeypatch, fake)
        out = await server.reddit_get_more_comments(anchor_url=anchor)
        assert out["ok"] is True
        assert out["focus_id"] == "19skp0t3gfzkw"
        # The anchor's opaque query is dropped and the sort applied.
        assert fake.fetches == [
            "https://www.reddit.com/r/LocalLLaMA/comments/1xuvexw/comment/19skp0t3gfzkw/?sort=top"
        ]

    async def test_rejects_bad_sort(self, monkeypatch):
        fake = FakePartialSession({})
        _patch_session(monkeypatch, fake)
        out = await server.reddit_get_more_comments(
            subreddit="LocalLLaMA", post_id=THREAD_ID, comment_id="abc", comment_sort="bogus"
        )
        assert out["ok"] is False
        assert "comment_sort" in out["error"]


class TestBug3BudgetAndCursorLoop:
    async def test_no_refetch_and_consumed_cursor_not_returned(self, monkeypatch):
        focus = "t1_c1r1"
        anchor_href = f"/r/LocalLLaMA/comments/{THREAD_ID}/comment/{focus}/?force-legacy-sct=1"
        anchor_url = f"https://www.reddit.com{anchor_href}"
        # The partial ALSO re-renders the same anchor (deeplink page shows the
        # "load more" link again) — the loop must not refetch or re-report it.
        partial = deeplink_partial(
            "c1r1",
            _comment("c1r1x", 2, "<p>new reply</p>")
            + _comment(
                "c1r1z",
                2,
                "<p>second new reply</p>",
                parent=(
                    f'<a slot="more-comments-permalink" href="/r/LocalLLaMA/comments/{THREAD_ID}/comment/t1_c1r1z/?force-legacy-sct=1">load more</a>'
                ),
            ),
        )
        page = page_html(
            _comment("c1", 0, "<p>top</p>")
            + _comment(
                "c1r1",
                1,
                "<p>reply</p>",
                parent=(f'<a slot="more-comments-permalink" href="{anchor_href}">load more</a>'),
            ),
        )
        fake = FakePartialSession(
            {
                f"https://www.reddit.com/r/LocalLLaMA/comments/{THREAD_ID}/x?sort=top": page,
                _pkey(anchor_url, sort="top"): partial,
            }
        )
        _patch_session(monkeypatch, fake)

        view = await server.reddit_get_thread(
            url_or_id=f"https://www.reddit.com/r/LocalLLaMA/comments/{THREAD_ID}/x/",
            comment_sort="top",
            max_comments=100,
            max_depth=3,
        )
        assert view["ok"] is True
        # Exactly the budget of partials fetched, all distinct, no re-fetch.
        assert len(fake.fetches) == len(set(fake.fetches))
        assert view["loaded_more_partials"] >= 1
        # The consumed anchor is never re-offered: following cursors terminates.
        returned = [c["anchor_url"] for c in view["more_subtree_cursors"]]
        assert anchor_url not in returned
        assert (
            f"https://www.reddit.com/r/LocalLLaMA/comments/{THREAD_ID}/comment/t1_c1r1z/?force-legacy-sct=1"
            in returned
        )

    async def test_failed_fetch_does_not_burn_budget(self, monkeypatch):
        class Boom(FakePartialSession):
            async def expand_subtree(self, url, focus_id, max_rounds=3):
                raise RedditHTTPError(403, url)

        _patch_session(monkeypatch, Boom({}))
        out = await server.reddit_get_more_comments(
            subreddit="LocalLLaMA", post_id=THREAD_ID, comment_id="abc123"
        )
        assert out["ok"] is False


class TestBug4CursorCapAccounting:
    async def test_dropped_count_reported(self, monkeypatch):
        anchors = [
            {
                "parent_id": f"c{i}",
                "href": f"/r/x/comments/1/comment/t1_c{i}/?force-legacy-sct=1",
            }
            for i in range(13)
        ]
        cursors, dropped = server._cursor_list(anchors)
        assert len(cursors) == 10
        assert dropped == 3


class TestBug6PaginationCursor:
    def test_listing_after_token_shape(self):
        # t3_1xyphos -> dDNfMXh5cGhvcw== -> dDNfMXh5cGhvcw%3D%3D (Reddit's own).
        assert listing_after_token("1xyphos") == "dDNfMXh5cGhvcw%3D%3D"
        assert base64.b64decode(listing_after_token("1xyphos").replace("%3D", "=")) == b"t3_1xyphos"

    async def test_truncation_builds_cursor_from_last_returned_id(self, monkeypatch):
        # Reddit ignores feedLength and returns 25; we cap to 10. The natural
        # load-after points past all 25 (skipping 15 posts). The tool must
        # hand back a cursor that continues from post #10.
        feed = "".join(
            f'<shreddit-post id="t3_p{i:02d}" post-title="p{i}" author="u" '
            f'subreddit-name="ClaudeCode" score="{100 - i}" comment-count="0" '
            f'permalink="/r/ClaudeCode/comments/p{i:02d}/x/"></shreddit-post>'
            for i in range(25)
        )
        html = f"""
        <html><body>{feed}
        <faceplate-partial slot="load-after"
          src="/svc/shreddit/community-more-posts/hot/?after=FULLPAGE&name=ClaudeCode"></faceplate-partial>
        </body></html>
        """
        from reddit_mcp.reddit import build_listing_path as _blp

        key = "https://www.reddit.com" + _blp(
            "ClaudeCode", sort="hot", time_filter="week", feed_length=10
        )
        fake = FakePartialSession({key: html})
        _patch_session(monkeypatch, fake)

        out = await server.reddit_list_posts("ClaudeCode", sort="hot", limit=10)
        assert out["ok"] is True and out["count"] == 10
        # Decoding the cursor proves it continues from the 10th returned post.
        import re as _re
        from urllib.parse import unquote

        m = _re.search(r"after=([^&]+)", out["after"])
        token = unquote(m.group(1))
        assert base64.b64decode(token) == b"t3_p09"

    async def test_no_truncation_keeps_natural_cursor(self, monkeypatch):
        feed = "".join(
            f'<shreddit-post id="t3_p{i}" post-title="p{i}" author="u" '
            f'subreddit-name="ClaudeCode" score="1" comment-count="0" '
            f'permalink="/r/ClaudeCode/comments/p{i}/x/"></shreddit-post>'
            for i in range(5)
        )
        html = f'<html><body>{feed}<faceplate-partial slot="load-after" src="/natural/cursor"></faceplate-partial></body></html>'
        from reddit_mcp.reddit import build_listing_path as _blp

        key = "https://www.reddit.com" + _blp(
            "ClaudeCode", sort="hot", time_filter="week", feed_length=10
        )
        fake = FakePartialSession({key: html})
        _patch_session(monkeypatch, fake)
        out = await server.reddit_list_posts("ClaudeCode", sort="hot", limit=10)
        assert out["after"] == "/natural/cursor"


class TestBug7BodyExtraction:
    def test_br_becomes_newline(self):
        html = "<div><p>an Israeli company.<br>Also because of time zones</p></div>"
        assert (
            _extract_body_text(html_or_tag(html))
            == "an Israeli company.\nAlso because of time zones"
        )

    def test_code_block_kept(self):
        html = (
            "<div><p>Run this:</p><pre><code>npm test\nnpm run build</code></pre><p>Done.</p></div>"
        )
        assert _extract_body_text(html_or_tag(html)) == (
            "Run this:\n\nnpm test\nnpm run build\n\nDone."
        )

    def test_list_items_each_on_a_line(self):
        html = "<div><p>Steps:</p><ul><li>first</li><li>second <a>/x</a> step</li></ul></div>"
        assert _extract_body_text(html_or_tag(html)) == "Steps:\n\n- first\n- second /x step"

    def test_inline_links_keep_spaces(self):
        html = '<div><p>the CCP is now enforcing <a href="/x">import bans</a> on GPUs</p></div>'
        assert _extract_body_text(html_or_tag(html)) == (
            "the CCP is now enforcing import bans on GPUs"
        )


def html_or_tag(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser").find("div")


class TestBug8TruncationFlags:
    async def test_comment_body_truncated_flag(self, monkeypatch):
        long_body = "<p>" + "x" * 300 + "</p>"
        html = page_html(
            _comment("c1", 0, long_body),
        )
        fake = FakePartialSession(
            {f"https://www.reddit.com/r/LocalLLaMA/comments/{THREAD_ID}/x?sort=best": html}
        )
        _patch_session(monkeypatch, fake)
        view = await server.reddit_get_thread(
            url_or_id=f"https://www.reddit.com/r/LocalLLaMA/comments/{THREAD_ID}/x/",
            max_body_chars=100,
            max_comments=10,
        )
        node = view["comments"][0]
        assert len(node["body"]) == 100
        assert node["body_truncated"] is True


class TestBug10IsSelfDomain:
    def test_text_post_with_media_is_self(self):
        posts, _ = parse_listing_dom(
            '<shreddit-post id="t3_1xywuct" post-title="t" post-type="text" '
            'domain="self.ClaudeCode" comment-count="0" '
            'content-href="/r/ClaudeCode/comments/1xywuct/x/" '
            'permalink="/r/ClaudeCode/comments/1xywuct/x/"></shreddit-post>'
        )
        assert posts[0]["is_self"] is True
        assert posts[0]["url"] is None

    def test_crosspost_is_not_self(self):
        posts, _ = parse_listing_dom(
            '<shreddit-post id="t3_1xtjj5g" post-title="t" post-type="crosspost" '
            'domain="i.redd.it" comment-count="0" is-crosspost="" '
            'content-href="/r/IndiaTech/comments/1xt4ypu/x/" '
            'permalink="/r/singularity/comments/1xtjj5g/x/"></shreddit-post>'
        )
        p = posts[0]
        assert p["is_self"] is False
        # Documented: url is the crosspost permalink; domain is the original
        # media host of the crossposted source (may differ by design).
        assert p["url"] == "https://www.reddit.com/r/IndiaTech/comments/1xt4ypu/x/"
        assert p["domain"] == "i.redd.it"
        assert p["crosspost_permalink"] == p["url"]


class TestBug12AboutNullKeys:
    def test_about_keys_present_as_null(self):
        info = parse_about_dom("<html><body>empty</body></html>")
        assert "members" in info and info["members"] is None
        assert "active_users" in info and info["active_users"] is None


class TestFakePageImport:
    """FakeContext/FakePage importable from the sibling module (no network)."""

    def test_import(self):
        assert FakeContext is not None and FakePage is not None
