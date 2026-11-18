"""Offline tests for Reddit rendered-DOM parsing (no network, no browser)."""

import pytest

from reddit_mcp.reddit import (
    build_comment_tree,
    build_listing_path,
    clamp,
    clean_subreddit,
    parse_about_dom,
    parse_listing_dom,
    parse_search_dom,
    parse_thread_dom,
    parse_thread_ref,
    prune_tree,
    thread_permalink,
)

LISTING_HTML = """
<html><body>
<shreddit-feed>
  <shreddit-post id="t3_abc123" post-title="A post" author="someuser"
      subreddit-name="ClaudeCode" score="42" upvote-ratio="0.95"
      comment-count="7" created-timestamp="2026-11-17T09:13:29+00:00"
      link-flair-text="Discussion" post-type="text"
      domain="self.ClaudeCode" permalink="/r/ClaudeCode/comments/abc123/a_post/">
    <a slot="full-post-link" href="/r/ClaudeCode/comments/abc123/a_post/">link</a>
    <div slot="text-body">Body text here</div>
  </shreddit-post>
  <shreddit-post id="t3_def456" post-title="Second" author="other"
      subreddit-name="ClaudeCode" score="1,024" post-type="image"
      domain="i.redd.it" content-href="https://i.redd.it/x.png"
      comment-count="2" permalink="/r/ClaudeCode/comments/def456/second/">
    <a slot="full-post-link" href="/r/ClaudeCode/comments/def456/second/">link</a>
  </shreddit-post>
  <shreddit-post id="t3_nolink"></shreddit-post>
</shreddit-feed>
<faceplate-partial slot="load-after"
  src="/svc/shreddit/community-more-posts/best/?after=dDNfMXh5cGhvcw%3D%3D&amp;name=ClaudeCode"></faceplate-partial>
</body></html>
"""


def _comment(cid, depth, body, thing_id=None):
    return f"""
    <shreddit-comment thingid="{thing_id or cid}" author="u_{cid}" score="{depth * 3}"
        depth="{depth}" permalink="/r/ClaudeCode/comments/abc123/a_post/{cid}/">
      <div id="{cid}-post-rtjson-content">{body}</div>
      <shreddit-comment thingid="{cid}_r1" author="u_{cid}_r1" score="1"
          depth="{depth + 1}" permalink="/r/ClaudeCode/comments/abc123/a_post/{cid}_r1/">
        <div id="{cid}_r1-post-rtjson-content">reply body</div>
      </shreddit-comment>
    </shreddit-comment>
    """


THREAD_HTML = f"""
<html><body>
<shreddit-post id="t3_abc123" post-title="A post" author="someuser"
    subreddit-name="ClaudeCode" score="42" upvote-ratio="0.95" comment-count="7"
    created-timestamp="2026-11-17T09:13:29+00:00" post-type="text"
    domain="self.ClaudeCode" permalink="/r/ClaudeCode/comments/abc123/a_post/">
  <div slot="text-body">Full body text</div>
</shreddit-post>
<shreddit-comment-tree>
{_comment("c1", 0, "top comment")}
{_comment("c2", 0, "second top")}
</shreddit-comment-tree>
</body></html>
"""


class TestSubredditHelpers:
    def test_clean_subreddit_strips_prefix_and_slashes(self):
        assert clean_subreddit("/r/ClaudeCode/") == "ClaudeCode"
        assert clean_subreddit("r/ClaudeCode") == "ClaudeCode"

    def test_clean_subreddit_rejects_garbage(self):
        with pytest.raises(ValueError):
            clean_subreddit("../etc/passwd")
        with pytest.raises(ValueError):
            clean_subreddit("")

    def test_clamp(self):
        assert clamp(50, 1, 25) == 25
        assert clamp(0, 1, 25) == 1
        assert clamp(10, 1, 25) == 10


class TestThreadRef:
    def test_parses_full_permalink(self):
        sub, pid = parse_thread_ref(
            "https://www.reddit.com/r/ClaudeCode/comments/1xyfuh3/four_ultras/"
        )
        assert (sub, pid) == ("ClaudeCode", "1xyfuh3")

    def test_bare_id_requires_subreddit(self):
        assert parse_thread_ref("1xyfuh3", subreddit="ClaudeCode") == ("ClaudeCode", "1xyfuh3")
        with pytest.raises(ValueError, match="without a subreddit"):
            parse_thread_ref("1xyfuh3")

    def test_permalink_helper(self):
        assert (
            thread_permalink("ClaudeCode", "1xy", "c9")
            == "https://www.reddit.com/r/ClaudeCode/comments/1xy/x/c9"
        )
        assert (
            thread_permalink("ClaudeCode", "1xy")
            == "https://www.reddit.com/r/ClaudeCode/comments/1xy/x"
        )


class TestListingPath:
    def test_build_listing_path_shape(self):
        path = build_listing_path("ClaudeCode", sort="hot", time_filter="day", feed_length=25)
        assert path.startswith("/svc/shreddit/community-more-posts/hot/")
        assert "name=ClaudeCode" in path
        assert "t=DAY" in path
        assert "feedLength=25" in path
        assert "after=" in path

    def test_build_listing_path_is_deterministic(self):
        a = build_listing_path("ClaudeCode", sort="top", time_filter="week")
        b = build_listing_path("ClaudeCode", sort="top", time_filter="week")
        assert a == b  # same logical request -> same URL -> cacheable

    def test_build_listing_path_after_token_varies(self):
        a = build_listing_path("ClaudeCode", after="dDNfMQ%3D%3D")
        b = build_listing_path("ClaudeCode")
        assert a != b


class TestListingParsing:
    def test_parses_posts_and_cursor(self):
        posts, after = parse_listing_dom(LISTING_HTML)
        assert len(posts) == 2
        assert after == (
            "/svc/shreddit/community-more-posts/best/?after=dDNfMXh5cGhvcw%3D%3D&name=ClaudeCode"
        )
        first = posts[0]
        assert first["id"] == "abc123"  # t3_ prefix stripped
        assert first["title"] == "A post"
        assert first["score"] == 42
        assert first["upvote_ratio"] == 0.95
        assert first["permalink"].startswith("https://www.reddit.com/r/ClaudeCode/comments/abc123")
        assert first["body_preview"] == "Body text here"
        assert first["is_self"] is True

    def test_missing_attrs_read_as_false_or_none(self):
        # The listing fixture posts carry no is-nsfw attr and one has no text
        # body: absent nsfw must be False (not True) and missing body None.
        posts, _ = parse_listing_dom(LISTING_HTML)
        assert all(p["nsfw"] is False for p in posts)
        assert posts[1]["body_preview"] is None

    def test_nsfw_flag_true_when_marked(self):
        html = LISTING_HTML.replace(
            '<shreddit-post id="t3_abc123"', '<shreddit-post is-nsfw="true" id="t3_abc123"'
        )
        posts, _ = parse_listing_dom(html)
        assert posts[0]["nsfw"] is True
        assert posts[1]["nsfw"] is False

    def test_score_with_comma(self):
        posts, _ = parse_listing_dom(LISTING_HTML)
        assert posts[1]["score"] == 1024

    def test_image_post_marks_not_self(self):
        posts, _ = parse_listing_dom(LISTING_HTML)
        assert posts[1]["is_self"] is False
        assert posts[1]["url"] == "https://i.redd.it/x.png"

    def test_body_preview_truncated(self):
        long_body = "z" * 500
        html = LISTING_HTML.replace("Body text here", long_body)
        posts, _ = parse_listing_dom(html, preview_chars=50)
        assert len(posts[0]["body_preview"]) == 50


class TestThreadParsing:
    def test_parses_post_and_nested_tree(self):
        view = parse_thread_dom(THREAD_HTML, comment_sort="best")
        assert view["ok"] is True
        post = view["post"]
        assert post["id"] == "abc123"
        assert post["body"] == "Full body text"
        assert len(view["comments"]) == 2
        top = view["comments"][0]
        assert top["id"] == "c1"  # t1_ prefix stripped
        assert top["depth"] == 0
        assert top["body"] == "top comment"
        assert top["replies"][0]["id"] == "c1_r1"
        assert top["replies"][0]["depth"] == 1
        assert view["total_comments_rendered"] == 4

    def test_depth_cap_stubbed_with_flag(self):
        view = parse_thread_dom(THREAD_HTML, max_depth=1)
        top = view["comments"][0]
        assert top["replies"] == []
        assert top["replies_truncated"] is True
        assert view["truncated_comment_count"] == 2  # both reply sets pruned

    def test_comment_cap_counts_truncation(self):
        view = parse_thread_dom(THREAD_HTML, max_comments=2)
        # c1 and c1_r1 consume the budget; c2 (with its subtree) is truncated
        assert view["truncated_comment_count"] == 1
        assert len(view["comments"]) == 1

    def test_body_char_cap(self):
        view = parse_thread_dom(THREAD_HTML, max_body_chars=5)
        assert view["comments"][0]["body"] == "top c"

    def test_missing_post_raises(self):
        with pytest.raises(ValueError, match="No shreddit-post"):
            parse_thread_dom("<html><body>nothing</body></html>")


class TestCommentTree:
    def test_stack_rebuild_nests_by_depth(self):
        flat = [
            {"id": "a", "depth": 0, "body": "a"},
            {"id": "b", "depth": 1, "body": "b"},
            {"id": "c", "depth": 2, "body": "c"},
            {"id": "d", "depth": 0, "body": "d"},
        ]
        tree = build_comment_tree(flat)
        assert [n["id"] for n in tree] == ["a", "d"]
        assert tree[0]["replies"][0]["id"] == "b"
        assert tree[0]["replies"][0]["replies"][0]["id"] == "c"

    def test_prune_tree_keeps_more_href_stub(self):
        tree = [
            {
                "id": "a",
                "depth": 0,
                "body": "a",
                "more_href": "https://x/y",
                "replies": [{"id": "b", "depth": 1, "body": "b", "replies": []}],
            },
        ]
        pruned, truncated = prune_tree(tree, max_comments=1, max_depth=3)
        assert pruned[0]["replies_truncated"] is True
        assert pruned[0]["more_href"] == "https://x/y"
        assert truncated == 1


class TestAboutParsing:
    def test_parses_about_dom(self):
        html = """
        <html><head>
        <title>ClaudeCode - Reddit</title>
        <meta name="description" content="A subreddit about Claude Code.">
        </head><body>
        <shreddit-title>ClaudeCode</shreddit-title>
        </body></html>
        """
        info = parse_about_dom(html)
        assert info["ok"] is True
        assert info["name"] == "ClaudeCode"
        assert info["title"] == "ClaudeCode"
        assert info["description_preview"] == "A subreddit about Claude Code."

    def test_about_dom_tolerates_missing_pieces(self):
        info = parse_about_dom("<html><body>empty</body></html>")
        assert info["ok"] is True
        assert info["name"] is None


SEARCH_HTML = """
<html><body>
<a href="/r/ClaudeCode/comments/1xylnge/a_review_subagent/">a review subagent detached HEAD</a>
<a href="/r/ClaudeCode/comments/1xykpw2/any_good_system/?pid=x&amp;impressionid=1">ad-ish related</a>
<a href="https://alb.reddit.com/cr?za=1">sponsored</a>
<a href="/r/ClaudeAI/comments/1rqxzlp/two_claude_code/">Two Claude Code features I slept on</a>
<a href="/r/ClaudeCode/comments/1xylnge/a_review_subagent/">duplicate of first</a>
</body></html>
"""


class TestSearchDomParsing:
    def test_parses_dedupes_and_filters_ads(self):
        results = parse_search_dom(SEARCH_HTML)
        assert len(results) == 2
        first = results[0]
        assert first["id"] == "1xylnge"
        assert first["subreddit"] == "ClaudeCode"
        assert first["permalink"] == (
            "https://www.reddit.com/r/ClaudeCode/comments/1xylnge/a_review_subagent/"
        )
        assert results[1]["subreddit"] == "ClaudeAI"

    def test_limit(self):
        assert len(parse_search_dom(SEARCH_HTML, limit=1)) == 1
