"""Pure functions for parsing Reddit's rendered shreddit DOM.

No network here: everything takes already-rendered page HTML, which keeps
the MCP tools thin and the parsers unit-testable offline.

In local tests some JSON endpoints were unavailable while browser pages
loaded, so the backend reads web pages and internal HTML partials instead.
Neither those observations nor this parser guarantee current Reddit access.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

SITE_URL = "https://www.reddit.com"

LIST_SORTS = {"hot", "new", "top", "rising", "best"}
SEARCH_SORTS = {"relevance", "new", "top", "comments"}
COMMENT_SORTS = {"best", "top", "new", "controversial", "old"}
TIME_FILTERS = {"hour", "day", "week", "month", "year", "all"}

THREAD_URL_RE = re.compile(r"/r/([A-Za-z0-9_]{2,21})/comments/([a-z0-9]+)(?:/|$)")


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _to_int(value) -> int | None:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _absolutize(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("http"):
        return href
    return f"{SITE_URL}{href}"


def _collapse_line(text: str) -> str:
    """Collapse runs of whitespace except newlines; trim ends."""
    return re.sub(r"[^\n\S]+", " ", text).strip()


def _inline_text(tag: Tag) -> str:
    """Render a tag's descendants to text: <br> becomes a newline.

    Text nodes are joined as-is (source whitespace preserved), so inline
    elements (<a>, <strong>, ...) keep their surrounding spaces instead of
    gluing onto neighbouring words.
    """
    out: list[str] = []

    def walk(el) -> None:
        for child in el.children:
            if isinstance(child, NavigableString):
                out.append(str(child))
            elif getattr(child, "name", None) == "br":
                out.append("\n")
            else:
                walk(child)

    walk(tag)
    return "".join(out)


def _extract_body_text(tag: Tag) -> str:
    """Extract readable text from a post/comment body element.

    Reddit renders markdown bodies as <p> paragraphs (with inline <a>/<strong>
    and <br> line breaks), <pre><code> blocks and <ul>/<ol> lists. Blocks are
    joined with blank lines; <br> becomes a newline; code keeps its own
    newlines; each list item gets its own line. Falls back to whole-element
    inline text when no block elements are present.
    """
    if not isinstance(tag, Tag):
        return ""

    blocks: list[str] = []
    for el in tag.find_all(["p", "pre", "ul", "ol"]):
        if el.name == "pre":
            # Code: keep source newlines; do not collapse inner whitespace.
            blocks.append(_inline_text(el).strip("\n"))
        elif el.name in ("ul", "ol"):
            items = [
                "- " + _collapse_line(_inline_text(li)).replace("\n", " ")
                for li in el.find_all("li", recursive=False)
            ]
            blocks.append("\n".join(i for i in items if i.strip("- ")))
        else:
            # Paragraph: keep intentional <br> line breaks as single newlines.
            lines = [_collapse_line(line) for line in _inline_text(el).split("\n")]
            blocks.append("\n".join(line for line in lines if line))

    if blocks:
        return "\n\n".join(b for b in blocks if b)
    return "\n".join(
        line for line in (_collapse_line(part) for part in _inline_text(tag).split("\n")) if line
    )


def clean_subreddit(subreddit: str) -> str:
    """Normalize a subreddit name (strip 'r/', slashes, spaces; lowercase)."""
    name = subreddit.strip().strip("/")
    if name.lower().startswith("r/"):
        name = name[2:]
    if not name or not re.fullmatch(r"[A-Za-z0-9_]{2,21}", name):
        raise ValueError(
            f"Invalid subreddit name: {subreddit!r}. Use the bare name, e.g. 'ClaudeCode'."
        )
    return name


def parse_thread_ref(url_or_id: str, subreddit: str | None = None) -> tuple[str, str]:
    """Extract (subreddit, post_id) from a thread URL or bare post id.

    Accepts:
      - Full permalink: https://www.reddit.com/r/ClaudeCode/comments/1xyzabc/...
      - Relative path:  /r/ClaudeCode/comments/1xyzabc/...
      - Bare post id (requires subreddit): "1xyzabc" with subreddit="ClaudeCode"
    """
    value = url_or_id.strip()
    match = THREAD_URL_RE.search(value)
    if match:
        return clean_subreddit(match.group(1)), match.group(2)
    if re.fullmatch(r"[a-z0-9]{4,10}", value):
        if not subreddit:
            raise ValueError(
                "Bare post id given without a subreddit. Pass the permalink, "
                "or provide the subreddit parameter."
            )
        return clean_subreddit(subreddit), value
    raise ValueError(
        f"Could not parse thread reference: {url_or_id!r}. "
        "Expected /r/{subreddit}/comments/{post_id} URL or a bare post id."
    )


def thread_permalink(subreddit: str, post_id: str, comment_id: str | None = None) -> str:
    """Canonical thread permalink navigable in the browser."""
    base = f"{SITE_URL}/r/{subreddit}/comments/{post_id}"
    return f"{base}/x/{comment_id}" if comment_id else f"{base}/x"


def comment_permalink(
    subreddit: str, post_id: str, comment_id: str, sort: str | None = None
) -> str:
    """The webapp's comment deeplink (NOT thread_permalink's /x/ form).

    /r/{sub}/comments/{pid}/comment/{cid}/ renders the comment's ancestors,
    the comment itself and a window of its replies; the /comments/{pid}/x/{cid}
    form 200s but renders no comments (verified live 2026-11-17).

    Without ?sort=, the deeplink renders replies in Reddit's default
    confidence window, which can DROP replies a sort=top thread shows
    (verified: 19sk2yjtdhw4j). Pass sort (top recommended) to render the
    top-reply window instead.
    """
    url = f"{SITE_URL}/r/{subreddit}/comments/{post_id}/comment/{comment_id}/"
    return f"{url}?sort={sort}" if sort else url


def listing_after_token(post_id: str) -> str:
    """Reddit's feed cursor for a post id: URL-encoded base64 of 't3_<id>'.

    Matches the tokens Reddit itself returns (padding kept, %3D-encoded),
    e.g. t3_1xyphos -> dDNfMXh5cGhvcw%3D%3D.
    """
    import base64
    from urllib.parse import quote

    return quote(base64.b64encode(f"t3_{post_id}".encode()).decode())


def build_listing_path(
    subreddit: str,
    sort: str = "hot",
    time_filter: str = "all",
    after: str | None = None,
    feed_length: int = 25,
) -> str:
    """Build the webapp's own infinite-scroll partial URL for a feed page.

    The partial endpoint is what shreddit itself calls on scroll; it answers
    in-page fetches even for an automated logged-out browser. The navigation
    session id is derived deterministically from the query so identical
    logical requests share the backend cache.

    Args:
        subreddit: Bare subreddit name.
        sort: One of LIST_SORTS.
        time_filter: One of TIME_FILTERS.
        after: Opaque cursor from a previous call (already URL-encoded; passed
            through verbatim). None/empty means the first page.
        feed_length: Hint for posts per page (Reddit may override; cap client-side).

    Returns:
        Same-origin path usable with BrowserSession.fetch_partial.
    """
    import uuid

    nav_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"reddit-mcp:{subreddit}:{sort}:{time_filter}:{after or ''}:{feed_length}",
        )
    )
    return (
        f"/svc/shreddit/community-more-posts/{sort}/"
        f"?feedViewType=compactView&after={after or ''}&t={time_filter.upper()}"
        f"&name={subreddit}&feedLength={feed_length}&navigationSessionId={nav_id}"
    )


def _soup(html: str) -> BeautifulSoup:
    """Parse shreddit HTML/partial HTML into a queryable tree.

    BeautifulSoup treats <template> children as non-rendered template strings,
    so text lookups inside partial SSR chunks come back empty. More-comments
    partials deliver their comments inside <template> chunks, so unwrap them
    into plain divs first (the tag itself carries no semantics we need).
    """
    unwrapped = html.replace("<template", '<div data-template="true"').replace(
        "</template>", "</div>"
    )
    return BeautifulSoup(unwrapped, "html.parser")


def _post_from_shreddit(post: Tag, preview_chars: int) -> dict:
    post_type = post.get("post-type")
    body_div = post.find("div", {"slot": "text-body"})
    body = _extract_body_text(body_div) if isinstance(body_div, Tag) else ""
    permalink = _absolutize(post.get("permalink"))
    if not permalink:
        link = post.find("a", {"slot": "full-post-link"})
        permalink = _absolutize(link.get("href")) if isinstance(link, Tag) else None
    content_href = post.get("content-href")
    # Only post-type marks a crosspost (agent found text posts whose
    # content-href is their own permalink); is-crosspost attribute agrees.
    is_crosspost = post_type == "crosspost" or post.has_attr("is-crosspost")
    # A text post is self-posted even when it carries media (content-href then
    # points at its own permalink); domain self.{sub} marks the same thing.
    is_self = (
        post_type == "text" or str(post.get("domain") or "").startswith("self.")
    ) and not is_crosspost
    return {
        "id": str(post.get("id") or "").removeprefix("t3_"),
        "title": post.get("post-title") or "",
        "author": post.get("author"),
        "subreddit": post.get("subreddit-name"),
        "score": _to_int(post.get("score")),
        "upvote_ratio": _to_float(post.get("upvote-ratio")),
        "num_comments": _to_int(post.get("comment-count")),
        "created_utc": post.get("created-timestamp"),
        "flair": post.get("link-flair-text"),
        "permalink": permalink,
        "is_self": is_self,
        # Crossposts: `url` is the crossposted thread's permalink (content-href
        # may be relative); `domain` keeps the ORIGINAL media host of the
        # crossposted source, so it can differ from url on purpose.
        "domain": post.get("domain"),
        "url": None if is_self else (_absolutize(content_href) if content_href else None),
        "crosspost_permalink": _absolutize(content_href) if is_crosspost else None,
        # Absent attribute must read as false, not true (an agent found the bug).
        "nsfw": str(post.get("is-nsfw") or "").lower() == "true",
        # Feed partials do not render post bodies; None until a thread page is read.
        "body_preview": body[:preview_chars] if body else None,
        "body_truncated": bool(body) and len(body) > preview_chars,
    }


def parse_listing_dom(html: str, preview_chars: int = 200) -> tuple[list[dict], str | None]:
    """Parse a rendered subreddit feed page into (post summaries, next cursor).

    The cursor is the faceplate load-after src URL: opaque; pass it back to
    reddit_list_posts as the after parameter.
    """
    soup = _soup(html)
    posts = [
        _post_from_shreddit(p, preview_chars)
        for p in soup.find_all("shreddit-post")
        if p.get("post-title")
    ]
    load_after = soup.find("faceplate-partial", attrs={"slot": "load-after"})
    next_cursor = None
    if isinstance(load_after, Tag):
        src = load_after.get("src")
        if isinstance(src, str) and src.strip():
            next_cursor = src.strip()
    return posts, next_cursor


def _comment_nodes(soup_or_within, max_body_chars: int) -> list[dict]:
    """Flatten shreddit-comment elements in document order with depth info.

    Each node carries parent_id, used to graft more-comments partial loads
    into an existing tree. Inside a partial, comments arrive inside <template>
    SSR chunks whose DOM nesting may be stripped, so the DOM parent is only
    the first candidate: when it is missing (or not itself a comment), fall
    back to the previous node at depth-1 (document order holds for shreddit).
    """
    nodes = []
    last_seen_by_depth: dict[int, str] = {}
    elements = soup_or_within.find_all("shreddit-comment")
    if isinstance(soup_or_within, Tag) and soup_or_within.name == "shreddit-comment":
        # Scoped to one comment element: it is the subtree's first node.
        elements = [soup_or_within] + [e for e in elements if e is not soup_or_within]
    for comment in elements:
        thing_id = str(comment.get("thingid") or comment.get("id") or "").removeprefix("t1_")
        if not thing_id:
            continue
        depth = _to_int(comment.get("depth")) or 0
        text_div = comment.find("div", id=lambda x: bool(x) and x.endswith("-post-rtjson-content"))
        body = _extract_body_text(text_div) if isinstance(text_div, Tag) else ""
        more = comment.find("a", {"slot": "more-comments-permalink"})
        more_href = more.get("href") if isinstance(more, Tag) else None
        parent_elem = comment.find_parent("shreddit-comment")
        parent_id = (
            str(parent_elem.get("thingid") or parent_elem.get("id") or "").removeprefix("t1_")
            if isinstance(parent_elem, Tag)
            else None
        )
        if not parent_id:
            # Template chunk: no DOM parent. Use the last seen node at
            # depth-1; top-level (depth 0) comments have no parent.
            parent_id = last_seen_by_depth.get(depth - 1) if depth > 0 else None
        nodes.append(
            {
                "id": str(thing_id),
                "author": comment.get("author"),
                "score": _to_int(comment.get("score")),
                "depth": depth,
                "body": body[:max_body_chars] if body else None,
                # Honest truncation flag (agent: bodies cut with no marker).
                "body_truncated": bool(body) and len(body) > max_body_chars,
                "permalink": _absolutize(comment.get("permalink")),
                # Lazily-loaded subtree cursor (the webapp's own more-comments
                # link). Kept relative+unqualified; absolutized at fetch time.
                "more_href": more_href,
                "parent_id": parent_id or None,
            }
        )
        last_seen_by_depth[depth] = str(thing_id)
    return nodes


def collect_more_anchors(soup_or_within) -> list[dict]:
    """Collect "load more comments" anchors, in document order.

    Accepts a whole-page soup or a Tag (e.g. one shreddit-comment) to scope
    collection to a subtree. Returns [{parent_id, href, label}] where
    parent_id is the shreddit-comment that contains the anchor (the subtree
    to expand), href is the webapp's own more-comments deeplink and label is
    the anchor's text (e.g. "47 more replies" — an honest remaining count).
    """
    anchors = []
    for a in soup_or_within.select('shreddit-comment a[slot="more-comments-permalink"]'):
        href = a.get("href")
        parent = a.find_parent("shreddit-comment")
        if not href or not isinstance(parent, Tag):
            continue
        parent_id = str(parent.get("thingid") or parent.get("id") or "").removeprefix("t1_")
        label = a.get_text(" ", strip=True)
        anchors.append({"parent_id": parent_id, "href": str(href), "label": label})
    return anchors


def more_comments_partial_path(anchor_href: str, sort: str | None = None) -> str:
    """Turn a more-comments deeplink into the partial fetch path.

    Strips the anchor's own query (force-legacy-sct etc.) and appends the
    webapp's partial render parameters, matching what shreddit itself loads.
    Pass sort to render the partial's replies in that order (top recommended:
    the default confidence window can drop replies sort=top shows).
    """
    path = anchor_href.split("?", 1)[0]
    if not path.startswith("/") and not path.startswith("http"):
        path = f"/{path}"
    out = f"{path}?render-mode=partial&is_lit_ssr=false"
    if sort:
        out += f"&sort={sort}"
    return out


def walk_tree(tree: list[dict]):
    """Yield every node in a nested comment tree, depth-first."""
    for node in tree:
        yield node
        yield from walk_tree(node.get("replies", []))


def merge_comment_nodes(tree: list[dict], incoming: list[dict]) -> int:
    """Graft flat incoming comment nodes into an existing tree; return merged count.

    Incoming nodes arrive in document order (ancestors first) from a
    more-comments partial; each carries parent_id. Nodes already present (by
    id) are skipped, so partials that re-render ancestors merge cleanly.
    """
    by_id = {node["id"]: node for node in walk_tree(tree)}
    merged = 0
    for node in incoming:
        nid = node.get("id")
        if not nid or nid in by_id:
            continue
        parent = by_id.get(node.get("parent_id"))
        if parent is None:
            continue  # orphaned (parent pruned away): not mergeable here
        parent.setdefault("replies", []).append({**node, "replies": []})
        by_id[nid] = parent["replies"][-1]
        merged += 1
    return merged


def build_comment_tree(nodes: list[dict]) -> list[dict]:
    """Rebuild a nested tree from flat depth-annotated comment nodes.

    Reddit renders comments in document order with a depth attribute; a
    stack-based rebuild is robust to DOM container changes.
    """
    root: list[dict] = []
    stack: list[dict] = []
    for node in nodes:
        depth = node["depth"] if node["depth"] is not None else 0
        item = {**node, "depth": depth, "replies": []}
        while stack and stack[-1]["depth"] >= depth:
            stack.pop()
        if not stack:
            root.append(item)
        else:
            stack[-1]["replies"].append(item)
        stack.append(item)
    return root


def prune_tree(
    tree: list[dict],
    max_comments: int = 30,
    max_depth: int = 3,
) -> tuple[list[dict], int]:
    """Bound a nested comment tree; truncated branches keep a more_href stub.

    Returns (pruned_tree, truncated_count).
    """
    truncated = 0

    def walk(nodes: list[dict], depth: int, budget: list[int]) -> list[dict]:
        nonlocal truncated
        out: list[dict] = []
        for node in nodes:
            if budget[0] <= 0 or depth >= max_depth:
                truncated += 1
                continue
            budget[0] -= 1
            replies = node.get("replies", [])
            pruned_replies = walk(replies, depth + 1, budget)
            stub = {k: v for k, v in node.items() if k not in ("replies", "more_href")}
            if replies and not pruned_replies:
                stub["replies_truncated"] = True
                if node.get("more_href"):
                    stub["more_href"] = node["more_href"]
            stub["replies"] = pruned_replies
            out.append(stub)
        return out

    pruned = walk(tree, 0, [max_comments])
    return pruned, truncated


def parse_thread_dom(
    html: str,
    comment_sort: str | None = None,
    max_comments: int = 30,
    max_depth: int = 3,
    max_body_chars: int = 2000,
    body_chars: int = 4000,
) -> dict:
    """Parse a rendered thread page into a post + pruned comment tree.

    Returns unparsed load state: more_anchors are the page's own
    "load more comments" deeplinks (parent_id + href), for the caller to
    fetch partials and merge; total from the post's comment-count.
    """
    soup = _soup(html)
    post_elem = soup.find("shreddit-post")
    if not isinstance(post_elem, Tag):
        raise ValueError(
            "No shreddit-post element on the page. The post may be removed, "
            "quarantined, or the URL wrong."
        )
    post = _post_from_shreddit(post_elem, preview_chars=body_chars)
    post["body"] = post.pop("body_preview")

    flat = _comment_nodes(soup, max_body_chars)
    tree = build_comment_tree(flat)
    pruned, truncated = prune_tree(tree, max_comments=max_comments, max_depth=max_depth)
    return {
        "ok": True,
        "post": post,
        "comments": pruned,
        "total_comments_rendered": len(flat),
        "truncated_comment_count": truncated,
        "comment_sort": comment_sort,
        "comment_limits": {"max_comments": max_comments, "max_depth": max_depth},
        # Lazily-loaded subtree pointers (the webapp's own more-comments
        # deeplinks): caller fetches these partials and merges.
        "more_anchors": collect_more_anchors(soup),
    }


def parse_about_dom(html: str) -> dict:
    """Best-effort subreddit metadata from a rendered feed page.

    The members/active widgets are client-rendered and may be absent from
    HTML captured right after domcontentloaded; extract_sidebar_js() grabs
    them from the live DOM instead. Everything here is best-effort.
    """
    soup = _soup(html)
    title = soup.find("shreddit-title")
    description = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    desc_text = description.get("content", "") if isinstance(description, Tag) else ""
    return {
        "ok": True,
        "name": title.get_text(strip=True) if isinstance(title, Tag) else None,
        "title": (
            soup.title.get_text(strip=True).removesuffix(" - Reddit") if soup.title else None
        ),
        "description_preview": desc_text[:500] or None,
        # Client-rendered widgets: keys always present, None when not rendered
        # (logged out). server.reddit_subreddit_info fills them live when it can.
        "members": None,
        "active_users": None,
    }


EXTRACT_SIDEBAR_JS = """
() => {
  const out = {};
  const post = document.querySelector('shreddit-post');
  if (post) {
    out.subscribers = post.getAttribute("subscribers") || null;
  }
  const title = document.querySelector('shreddit-title');
  if (title) {
    out.display_name = title.getAttribute('title') || (title.innerText || "").trim() || null;
  }
  const desc = document.querySelector('div[data-slot="description"], shreddit-description');
  if (desc) out.description = (desc.innerText || "").trim().slice(0, 500) || null;
  return out;
}
"""


def parse_search_dom(html: str, limit: int = 25) -> list[dict]:
    """Parse the rendered Reddit search results page into result summaries.

    Ad/related-post hrefs carry 'impressionid' query params and are skipped.
    """
    soup = _soup(html)
    seen: dict[tuple[str, str], dict] = {}
    for anchor in soup.select('a[href*="/comments/"]'):
        href = anchor.get("href") or ""
        if "impressionid" in href or "alb.reddit.com" in href:
            continue
        title = anchor.get_text(" ", strip=True)
        match = THREAD_URL_RE.search(href)
        if not match or not title:
            continue
        sub, post_id = match.groups()
        key = (sub.lower(), post_id)
        if key in seen:
            continue
        seen[key] = {
            "id": post_id,
            "subreddit": sub,
            "title": title[:300],
            "permalink": f"{SITE_URL}{href.split('?')[0]}",
        }
    return list(seen.values())[:limit]
