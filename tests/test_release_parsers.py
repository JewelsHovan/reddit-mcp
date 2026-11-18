"""Release-time parser guards, independent of a browser or a real account."""

import pytest

from reddit_mcp.reddit import parse_listing_dom, parse_thread_ref


@pytest.mark.parametrize(
    "value",
    [
        "/r/..%2f..%2fapi?x=#/comments/abcd",
        "/r/../comments/abcd/",
        "/r/a?x=1/comments/abcd/",
        "/r/a#x/comments/abcd/",
        "/r/ClaudeCode/comments/abcd%2fapi/",
    ],
)
def test_rejects_path_query_and_encoded_subreddit_injection(value):
    with pytest.raises(ValueError):
        parse_thread_ref(value)


def test_boolean_crosspost_attribute_is_recognized():
    posts, _ = parse_listing_dom(
        '<shreddit-post id="t3_abc123" post-title="A crosspost" is-crosspost '
        'content-href="/r/example/comments/abc123/title/" domain="i.redd.it">'
        "</shreddit-post>"
    )
    assert posts[0]["crosspost_permalink"] == (
        "https://www.reddit.com/r/example/comments/abc123/title/"
    )
