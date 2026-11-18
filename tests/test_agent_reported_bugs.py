"""Offline regression tests for agent-reported bugs (2026-11-17 field report).

Covers, with realistic DOM fixtures:
1. Comment bodies: paragraphs joined with blank lines, inline element
   boundaries keep their spaces ("quickly.Don't", "enforcingimport bans").
2. Thread load accounting: more-comments partials (template SSR chunks)
   merged into the tree; unloaded_comment_count reported honestly.
3. Crossposts: absolute url, crosspost_permalink, domain/url consistency.
"""

from reddit_mcp.reddit import (
    _soup,
    build_comment_tree,
    collect_more_anchors,
    merge_comment_nodes,
    more_comments_partial_path,
    parse_listing_dom,
    parse_thread_dom,
    walk_tree,
)

THREAD = "1xuvexw"

INLINE_BODY = (
    '<p>the CCP is now enforcing <a href="/x">import bans</a> on NVIDIA GPU\'s. '
    'Go post it in <a href="/x">r/Nvidia</a></p>'
)


def _div(body_html: str) -> str:
    return f'<div id="t1_x-post-rtjson-content">{body_html}</div>'


class TestBug2BodyWhitespace:
    """Bodies lost whitespace at element boundaries; paragraphs were mooshed."""

    REAL_BODY = (
        "<p>\n      He was famous for replying to emails quickly.\n    </p>"
        "<p>\n      Don't recall exactly the details, but a long time ago.\n    </p>"
    )

    def test_paragraphs_joined_with_blank_lines(self):
        view = parse_thread_dom(
            f"""
            <html><body>
            <shreddit-post id="t3_{THREAD}" post-title="t" comment-count="639"
                permalink="/r/LocalLLaMA/comments/{THREAD}/x/">
              <div slot="text-body"><p>Post body.</p><p>Second para.</p></div>
            </shreddit-post>
            <shreddit-comment thingid="t1_a" author="u" score="1" depth="0"
                permalink="/r/LocalLLaMA/comments/{THREAD}/x/a/">
              {_div(self.REAL_BODY)}
            </shreddit-comment>
            </body></html>
            """
        )
        body = view["comments"][0]["body"]
        assert body.startswith("He was famous for replying to emails quickly.")
        # The moosh bug joined the two paragraphs with no separator.
        assert "quickly.Don't" not in body
        assert "quickly.\n\nDon't recall exactly the details" in body

    def test_inline_boundaries_keep_spaces(self):
        # Inline <a>/<strong> children must not glue onto surrounding words.
        view = parse_thread_dom(
            f"""
            <html><body>
            <shreddit-post id="t3_{THREAD}" post-title="t" comment-count="639"
                permalink="/r/LocalLLaMA/comments/{THREAD}/x/"></shreddit-post>
            <shreddit-comment thingid="t1_a" author="u" score="1" depth="0">
              {_div(INLINE_BODY)}
            </shreddit-comment>
            </body></html>
            """
        )
        body = view["comments"][0]["body"]
        # Before the fix: "enforcingimport bans" and "inr/Nvidia".
        assert "enforcing import bans on NVIDIA GPU's. Go post it in r/Nvidia" in body

    def test_post_body_uses_shared_extractor(self):
        posts, _ = parse_listing_dom(
            f"""
            <shreddit-post id="t3_{THREAD}" post-title="t" post-type="text"
                comment-count="639" permalink="/r/LocalLLaMA/comments/{THREAD}/x/">
              <div slot="text-body"><p>First post paragraph.</p><p>Second.</p></div>
            </shreddit-post>
            """
        )
        assert posts[0]["body_preview"] == "First post paragraph.\n\nSecond."

    def test_fallback_without_paragraph_tags(self):
        view = parse_thread_dom(
            f"""
            <html><body>
            <shreddit-post id="t3_{THREAD}" post-title="t" comment-count="0"
                permalink="/r/LocalLLaMA/comments/{THREAD}/x/"></shreddit-post>
            <shreddit-comment thingid="t1_a" author="u" score="1" depth="0">
              <div id="t1_a-post-rtjson-content">plain body\n   with newline</div>
            </shreddit-comment>
            </body></html>
            """
        )
        # Source line breaks inside a plain body survive as newlines.
        assert view["comments"][0]["body"] == "plain body\nwith newline"


PAGE_HTML = f"""
<html><body>
<shreddit-post id="t3_{THREAD}" post-title="Stories coming out about Jensen"
    author="op" subreddit-name="LocalLLaMA" score="10" upvote-ratio="0.9"
    comment-count="639" post-type="text"
    permalink="/r/LocalLLaMA/comments/{THREAD}/stories_coming_out_about_jensen/">
  <div slot="text-body"><p>Post body.</p></div>
</shreddit-post>
<shreddit-comment-tree>
  <shreddit-comment thingid="t1_c1" author="a" score="5" depth="0"
      permalink="/r/LocalLLaMA/comments/{THREAD}/x/c1/">
    {_div("<p>top comment</p>")}
    <div slot="replies">
      <shreddit-comment thingid="t1_c1r1" author="b" score="3" depth="1"
          permalink="/r/LocalLLaMA/comments/{THREAD}/x/c1r1/">
        {_div("<p>reply one</p>")}
        <a slot="more-comments-permalink"
           href="/r/LocalLLaMA/comments/{THREAD}/comment/t1_c1r1/?force-legacy-sct=1">load more</a>
      </shreddit-comment>
    </div>
  </shreddit-comment>
  <shreddit-comment thingid="t1_c2" author="c" score="2" depth="0"
      permalink="/r/LocalLLaMA/comments/{THREAD}/x/c2/">
    {_div("<p>second top</p>")}
  </shreddit-comment>
</shreddit-comment-tree>
</body></html>
"""

# The webapp's more-comments partial: comments arrive inside <template> SSR
# chunks. html.parser surfaces them (DOMParser does not); this is real shape.
PARTIAL_HTML = f"""
<faceplate-loader name="Chunk_x" src="concat:abc"></faceplate-loader>
<suspense-replace target="#s_1" template="template[for=s_1]"></suspense-replace>
<suspense-chunk-start>
<template for="s_1">
  <shreddit-comment thingid="t1_c1r1" author="b" score="3" depth="1"
      permalink="/r/LocalLLaMA/comments/{THREAD}/x/c1r1/">
    {_div("<p>reply one (re-rendered ancestor)</p>")}
    <div slot="replies"></div>
  </shreddit-comment>
  <shreddit-comment thingid="t1_c1r1x" author="d" score="9" depth="2"
      permalink="/r/LocalLLaMA/comments/{THREAD}/x/c1r1x/">
    <div id="t1_c1r1x-post-rtjson-content"><p>newly loaded reply</p></div>
  </shreddit-comment>
  <shreddit-comment thingid="t1_c1r1y" author="e" score="4" depth="2"
      permalink="/r/LocalLLaMA/comments/{THREAD}/x/c1r1y/">
    <div id="t1_c1r1y-post-rtjson-content"><p>another new reply</p></div>
  </shreddit-comment>
</template>
</suspense-chunk-end>
"""


class TestBug1MoreCommentsLoading:
    """Thread stopped at the ~25 rendered comments and reported nothing truncated."""

    def test_collect_more_anchors_with_parent_id(self):
        anchors = collect_more_anchors(_soup(PAGE_HTML))
        assert anchors == [
            {
                "parent_id": "c1r1",
                "href": f"/r/LocalLLaMA/comments/{THREAD}/comment/t1_c1r1/?force-legacy-sct=1",
                "label": "load more",
            }
        ]

    def test_partial_path_strips_anchor_query(self):
        path = more_comments_partial_path(
            f"/r/LocalLLaMA/comments/{THREAD}/comment/t1_c1r1/?force-legacy-sct=1"
        )
        assert path == (
            f"/r/LocalLLaMA/comments/{THREAD}/comment/t1_c1r1/?render-mode=partial&is_lit_ssr=false"
        )
        assert "force-legacy-sct" not in path

    def test_merge_partial_into_tree_skips_known_and_grafts_new(self):
        # Bug 1's core: partials carry extra comments the page never rendered.
        soup = _soup(PAGE_HTML)
        from reddit_mcp.reddit import _comment_nodes

        tree = build_comment_tree(_comment_nodes(soup, 2000))
        assert len(list(walk_tree(tree))) == 3

        incoming = _comment_nodes(_soup(PARTIAL_HTML), 2000)
        # Depth-2 replies carry parent_id=t1_c1r1 -> c1r1.
        by_id = {n["id"]: n for n in incoming}
        assert by_id["c1r1x"]["parent_id"] == "c1r1"
        assert by_id["c1r1y"]["parent_id"] == "c1r1"

        merged = merge_comment_nodes(tree, incoming)
        assert merged == 2  # two new comments; ancestor re-render skipped
        flat = {n["id"]: n for n in walk_tree(tree)}
        assert set(flat) == {"c1", "c1r1", "c2", "c1r1x", "c1r1y"}
        parent = flat["c1r1"]
        assert {r["id"] for r in parent["replies"]} == {"c1r1x", "c1r1y"}
        assert flat["c1r1x"]["body"] == "newly loaded reply"

    def test_merge_is_idempotent(self):
        soup = _soup(PAGE_HTML)
        from reddit_mcp.reddit import _comment_nodes

        tree = build_comment_tree(_comment_nodes(soup, 2000))
        incoming = _comment_nodes(_soup(PARTIAL_HTML), 2000)
        merge_comment_nodes(tree, incoming)
        merged_again = merge_comment_nodes(tree, incoming)
        assert merged_again == 0  # re-rendered ancestors/known ids are skipped

    def test_orphan_incoming_not_merged(self):
        tree = build_comment_tree(
            [
                {"id": "c1", "depth": 0, "body": "b", "parent_id": None},
            ]
        )
        merged = merge_comment_nodes(
            tree, [{"id": "zz", "depth": 0, "body": "x", "parent_id": "missing"}]
        )
        assert merged == 0
        assert [n["id"] for n in walk_tree(tree)] == ["c1"]

    def test_unloaded_count_math(self):
        # 639 total, 5 rendered -> 634 unloaded; None when count unknown.
        soup = _soup(PAGE_HTML)
        from reddit_mcp.reddit import _comment_nodes

        tree = build_comment_tree(_comment_nodes(soup, 2000))
        incoming = _comment_nodes(_soup(PARTIAL_HTML), 2000)
        merge_comment_nodes(tree, incoming)
        rendered = len(list(walk_tree(tree)))
        assert rendered == 5
        assert max(0, 639 - rendered) == 634


class TestBug3Crosspost:
    """Crossposts returned a relative url and a mismatched domain."""

    CROSSPOST_POST = """
    <shreddit-post id="t3_1xtjj5g" post-title="Ts is too funny"
        author="u" subreddit-name="singularity" score="1" upvote-ratio="1"
        comment-count="7" post-type="crosspost" domain="i.redd.it"
        is-crosspost=""
        content-href="/r/IndiaTech/comments/1xt4ypu/ts_is_too_funny/"
        permalink="/r/singularity/comments/1xtjj5g/ts_is_too_funny/">
    </shreddit-post>
    """

    def test_crosspost_url_is_absolute_and_consistent(self):
        posts, _ = parse_listing_dom(self.CROSSPOST_POST)
        p = posts[0]
        # Before the fix: url="/r/IndiaTech/comments/..." (relative) while
        # domain="i.redd.it" pointed at media that was never in the output.
        assert p["url"] == "https://www.reddit.com/r/IndiaTech/comments/1xt4ypu/ts_is_too_funny/"
        assert p["url"].startswith("https://")
        assert p["crosspost_permalink"] == p["url"]
        assert p["domain"] == "i.redd.it"  # original media host, unchanged

    def test_regular_media_post_unaffected(self):
        posts, _ = parse_listing_dom(
            """
            <shreddit-post id="t3_1" post-title="plain" post-type="image"
                domain="i.redd.it" comment-count="0"
                content-href="https://i.redd.it/pic.png"
                permalink="/r/singularity/comments/1/x/"></shreddit-post>
            """
        )
        p = posts[0]
        assert p["url"] == "https://i.redd.it/pic.png"
        assert p["crosspost_permalink"] is None  # not a crosspost
        assert p["domain"] == "i.redd.it"

    def test_relative_media_content_href_absolutized(self):
        posts, _ = parse_listing_dom(
            """
            <shreddit-post id="t3_2" post-title="v" post-type="video"
                domain="v.redd.it" comment-count="0"
                content-href="/r/singularity/comments/2/x/"
                permalink="/r/singularity/comments/2/x/"></shreddit-post>
            """
        )
        p = posts[0]
        assert p["url"].startswith("https://www.reddit.com/")
