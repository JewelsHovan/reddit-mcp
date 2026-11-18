"""FastMCP server exposing read-only Reddit tools for agents.

Transport: stdio. Register with your agent as the `reddit-mcp` command.

Backend: a real Chrome browser session reading Reddit's rendered pages (see
browser.py). All tools are read-only, paced, cached, and fail fast on
blocks. Outputs are bounded so a single tool result cannot blow up an
agent's context window.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, quote_plus, urlsplit

from mcp.server.fastmcp import FastMCP

from .browser import (
    BASE_URL,
    BrowserSession,
    _is_reddit_origin,
    build_session_from_env,
    validate_reddit_url,
)
from .errors import DisallowedURLError, RedditAccessError
from .reddit import (
    COMMENT_SORTS,
    EXTRACT_SIDEBAR_JS,
    LIST_SORTS,
    SEARCH_SORTS,
    TIME_FILTERS,
    _to_int,
    build_listing_path,
    clamp,
    clean_subreddit,
    merge_comment_nodes,
    more_comments_partial_path,
    parse_about_dom,
    parse_listing_dom,
    parse_search_dom,
    parse_thread_dom,
    parse_thread_ref,
    thread_permalink,
)

_session: BrowserSession | None = None
_session_lock = asyncio.Lock()


async def get_session() -> BrowserSession:
    global _session
    async with _session_lock:
        if _session is None:
            candidate = build_session_from_env()
            try:
                await candidate.start()
            except Exception:
                with contextlib.suppress(Exception):
                    await candidate.stop()
                raise
            _session = candidate
        return _session


async def close_session() -> None:
    """Stop the singleton browser session cleanly if running."""
    global _session
    async with _session_lock:
        if _session is not None:
            try:
                await _session.stop()
            finally:
                _session = None


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    try:
        yield {}
    finally:
        await close_session()


mcp = FastMCP(
    "reddit",
    instructions=(
        "Read-only Reddit access through a real Chrome browser session reading "
        "rendered pages. Tools: check reddit_status first; browse feeds with "
        "reddit_list_posts, search with reddit_search, read full threads with "
        "reddit_get_thread, drill into pruned subtrees with reddit_get_more_comments, "
        "and get community metadata with reddit_subreddit_info. "
        "All requests are paced (~2s) and briefly cached; repeated reads are cheap. "
        "If a tool reports a block, stop and check the Chrome window rather than retrying."
    ),
    lifespan=_lifespan,
)


def _err(e: Exception, hint: str | None = None) -> dict:
    out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if hint:
        out["hint"] = hint
    return out


MAX_LOAD_MORE_PARTIALS = int(os.getenv("REDDIT_MCP_MAX_LOAD_MORE", "3"))
MAX_CURSOR_LIST = 10
_LISTING_CURSOR_PATH_RE = re.compile(
    r"^/svc/shreddit/community-more-posts/(?:hot|new|top|rising|best)/?$"
)
_COMMENT_DEEPLINK_PATH_RE = re.compile(
    r"^/r/([A-Za-z0-9_]{2,21})/comments/([A-Za-z0-9]+)/comment/((?:t1_)?[A-Za-z0-9_]+)/?$"
)


def _validate_listing_after(after: str, subreddit: str) -> str:
    """Validate an MCP-supplied listing `after` cursor against route and subreddit."""
    canonical = validate_reddit_url(after, allow_relative=True)
    parsed = urlsplit(canonical)
    if not _LISTING_CURSOR_PATH_RE.fullmatch(parsed.path):
        raise DisallowedURLError(
            f"after cursor must point to /svc/shreddit/community-more-posts/<sort>/: {after!r}"
        )
    names = parse_qs(parsed.query, keep_blank_values=True).get("name", [])
    if len(names) != 1 or names[0].lower() != subreddit.lower():
        raise DisallowedURLError(
            f"after cursor subreddit mismatch (expected {subreddit!r}): {after!r}"
        )
    return f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path


def _safe_partial_path(anchor_href: str, sort: str | None = None) -> str | None:
    """Build a partial path from a DOM anchor href and return it iff allowlisted."""
    candidate = more_comments_partial_path(anchor_href, sort)
    try:
        validate_reddit_url(candidate, allow_relative=True)
    except DisallowedURLError:
        return None
    return candidate


def _cursor_list(anchors: list[dict]) -> tuple[list[dict], int]:
    """Build the honest cursor list: entries + how many were dropped by the cap."""
    cursors = [
        {"parent_id": a["parent_id"], "anchor_url": f"{BASE_URL}{a['href']}"} for a in anchors
    ]
    return cursors[:MAX_CURSOR_LIST], max(0, len(cursors) - MAX_CURSOR_LIST)


def _iter_tree(tree: list[dict]):
    """Yield every node in a nested comment tree, depth-first."""
    for node in tree:
        yield node
        yield from _iter_tree(node.get("replies", []))


async def _load_more_comments(
    session: BrowserSession,
    tree: list[dict],
    anchors: list[dict],
    max_comments: int,
    max_body_chars: int,
    sort: str | None = None,
) -> tuple[int, int, list[dict]]:
    """Fetch up to MAX_LOAD_MORE_PARTIALS UNFETCHED lazy partials; graft them.

    Uses the webapp's own lazy-load deeplinks (render-mode=partial). Each
    partial re-renders ancestors plus a fresh page of replies; merging by
    parent_id is idempotent. An anchor already fetched is never re-fetched
    and never burns a budget slot. Mutates tree in place. Returns
    (merged_count, partials_fetched, remaining_anchors) — remaining are
    unfetched lazy subtree pointers for the caller to report honestly.
    """
    from .reddit import (
        _comment_nodes,
        _soup,
        collect_more_anchors,
    )

    merged_total = 0
    partials_fetched = 0
    fetched: set[str] = set()
    pending = [
        a
        for a in anchors
        if (path := _safe_partial_path(a["href"], sort)) is not None and path not in fetched
    ]
    while partials_fetched < MAX_LOAD_MORE_PARTIALS and pending:
        tree_ids = {n["id"] for n in _iter_tree(tree)}
        anchor = next((a for a in pending if a["parent_id"] in tree_ids), None)
        if anchor is None:
            break
        path = _safe_partial_path(anchor["href"], sort)
        if path is None:
            pending = [a for a in pending if a["href"] != anchor["href"]]
            continue
        fetched.add(path)
        partials_fetched += 1
        try:
            html = await session.fetch_partial(path)
        except RedditAccessError:
            partials_fetched -= 1  # a failed fetch does not consume the budget
            break  # honest: stop loading, report what we have
        soup = _soup(html)
        merged_total += merge_comment_nodes(tree, _comment_nodes(soup, max_body_chars))
        # The partial may carry further lazy pointers; keep unfetched ones only.
        pending = [
            a
            for a in pending
            if a["href"] != anchor["href"]
            and (p := _safe_partial_path(a["href"], sort)) is not None
            and p not in fetched
        ]
        for fresh in collect_more_anchors(soup):
            fresh_path = _safe_partial_path(fresh["href"], sort)
            if (
                fresh_path is not None
                and fresh["href"] not in {a["href"] for a in pending}
                and fresh_path not in fetched
            ):
                pending.append(fresh)
    return merged_total, partials_fetched, pending


# --------------------------------------------------------------------- tools


@mcp.tool()
async def reddit_status(live: bool = False, probe_subreddit: str = "ClaudeCode") -> dict:
    """Report the browser-session health and optionally make one live probe.

    Args:
        live: If true, navigate once to the probe subreddit's feed to verify
            end-to-end access right now.
        probe_subreddit: Subreddit used for the live probe.

    Returns:
        Session state (browser running, profile dir, login state, action/cache
        counts, and counter_semantics explaining that requests_made counts
        initiated browser actions including navigations, partial fetches, and
        executed clicks rather than a raw network ledger) and, when live=true,
        the probe outcome with the first post title.
    """
    try:
        session = await get_session()
    except RedditAccessError as e:
        return _err(e, "Chrome could not launch. Is Google Chrome installed?")

    probe: dict | None = None
    if live:
        # Run the probe BEFORE snapshotting counters, so requests_made/cache_hits
        # reflect it (an agent caught them reporting pre-probe values).
        try:
            sub = clean_subreddit(probe_subreddit)
            path = build_listing_path(sub, sort="hot", time_filter="day", feed_length=25)
            html = await session.fetch_partial(path)
            posts, _cursor = parse_listing_dom(html, preview_chars=120)
            probe = {
                "ok": True,
                "subreddit": sub,
                "posts_seen": len(posts),
                "first_title": posts[0]["title"] if posts else None,
            }
        except RedditAccessError as e:
            probe = {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "hint": "If blocked, open the Chrome window and check for a challenge page.",
            }

    status: dict = {"ok": True, **(await session.status())}
    if probe is not None:
        status["probe"] = probe
    return status


@mcp.tool()
async def reddit_list_posts(
    subreddit: str,
    sort: str = "hot",
    time_filter: str = "week",
    limit: int = 10,
    after: str | None = None,
) -> dict:
    """List posts from a subreddit feed (hot/new/top/rising/best).

    Uses the webapp's own infinite-scroll partial endpoint (fast: no full
    page load). Pagination cursors come back from Reddit itself.
    Feed partials do not include post bodies: body_preview is null here;
    call reddit_get_thread for the full body.

    Args:
        subreddit: Bare subreddit name, e.g. "ClaudeCode".
        sort: One of hot, new, top, rising, best.
        time_filter: hour, day, week, month, year, all (used by top/rising).
        limit: Max posts to return (1-25).
        after: Opaque pagination cursor from a previous call's "after" field.
            Pass it through exactly as received.

    Returns:
        Compact post summaries plus the next "after" cursor, or null when done.
        Note: the cursor is a URL path; repeated calls with the same arguments
        are served from a short-lived cache.
    """
    try:
        sub = clean_subreddit(subreddit)
        if sort not in LIST_SORTS:
            return _err(ValueError(f"sort must be one of {sorted(LIST_SORTS)}"))
        if time_filter not in TIME_FILTERS:
            return _err(ValueError(f"time_filter must be one of {sorted(TIME_FILTERS)}"))
        n = clamp(limit, 1, 25)

        # Cursors are partial listing URLs for the same subreddit: validate strictly.
        if after is not None and after != "":
            path = _validate_listing_after(after, sub)
        else:
            path = build_listing_path(sub, sort=sort, time_filter=time_filter, feed_length=n)

        session = await get_session()
        html = await session.fetch_partial(path)
        posts_all, natural_after = parse_listing_dom(html)
        posts = posts_all[:n]
        if len(posts_all) > n and posts:
            # Reddit's own load-after cursor points past its FULL page (it
            # ignores feedLength), so truncating to `limit` would silently
            # skip posts 11..25. Build the cursor from the last returned id
            # instead (agent found skipped posts between pages).
            from .reddit import listing_after_token

            next_after = build_listing_path(
                sub,
                sort=sort,
                time_filter=time_filter,
                after=listing_after_token(posts[-1]["id"]),
                feed_length=n,
            )
        else:
            next_after = natural_after
        return {
            "ok": True,
            "subreddit": sub,
            "sort": sort,
            "time_filter": time_filter,
            "count": len(posts),
            "posts": posts,
            "after": next_after,
        }
    except RedditAccessError as e:
        return _err(e, "Check reddit_status(live=true); a block means stop, not retry.")
    except ValueError as e:
        return _err(e)


@mcp.tool()
async def reddit_get_thread(
    url_or_id: str,
    subreddit: str | None = None,
    comment_sort: str = "best",
    max_comments: int = 30,
    max_depth: int = 3,
    max_body_chars: int = 2000,
) -> dict:
    """Read a full post with a pruned comment tree.

    Reddit lazily renders ~25 comments per thread page. This tool loads up to
    REDDIT_MCP_MAX_LOAD_MORE (default 3) additional "more comments" partials
    via the webapp's own lazy-load deeplinks before pruning, and reports
    honestly how much of the thread it could NOT load.

    Args:
        url_or_id: Thread permalink (/r/{sub}/comments/{id}/...) or bare post id
            (then subreddit is required).
        subreddit: Required only when url_or_id is a bare post id.
        comment_sort: best, top, new, controversial, old.
        max_comments: Cap on returned comments (1-100).
        max_depth: Max reply nesting depth (1-10); deeper branches are stubbed
            with "replies_truncated" for reddit_get_more_comments.
        max_body_chars: Cap per comment body (100-8000).

    Returns:
        Post details plus a bounded nested comment tree. Load accounting:
        total_comment_count (the post's own comment-count),
        total_comments_rendered (parsed from pages), truncated_comment_count
        (cut by the caps above), unloaded_comment_count (not loaded because
        Reddit renders lazily and the partial budget ran out) and
        more_subtree_cursors (unfetched lazy deeplinks; pass one to
        reddit_get_more_comments's anchor_url to continue).
    """
    try:
        raw_ref = url_or_id.strip()
        if "://" in raw_ref or raw_ref.startswith("/"):
            if "\\" in raw_ref or "#" in raw_ref or any(c.isspace() for c in raw_ref):
                return _err(DisallowedURLError(f"Malformed thread URL: {url_or_id!r}"))
            lowered_ref = raw_ref.lower()
            if any(tok in lowered_ref for tok in ("%00", "%2e", "%2f", "%5c", "%25", "..")):
                return _err(
                    DisallowedURLError(f"Disallowed traversal or encoding in URL: {url_or_id!r}")
                )
            if raw_ref.startswith("//"):
                return _err(DisallowedURLError(f"Protocol-relative URL not allowed: {url_or_id!r}"))
            if "://" in raw_ref and not _is_reddit_origin(raw_ref):
                return _err(DisallowedURLError(f"Disallowed origin in thread URL: {url_or_id!r}"))

        sub, post_id = parse_thread_ref(url_or_id, subreddit)
        sub = clean_subreddit(sub)
        if not re.fullmatch(r"[A-Za-z0-9]+", post_id):
            return _err(ValueError(f"Invalid post id: {post_id!r}"))
        if comment_sort not in COMMENT_SORTS:
            return _err(ValueError(f"comment_sort must be one of {sorted(COMMENT_SORTS)}"))
        max_c = clamp(max_comments, 1, 100)
        depth = clamp(max_depth, 1, 10)
        body_chars = clamp(max_body_chars, 100, 8000)

        url = f"{thread_permalink(sub, post_id)}?sort={comment_sort}"
        session = await get_session()
        html = await session.load_page(url)
        view = parse_thread_dom(
            html,
            comment_sort=comment_sort,
            max_comments=max_c,
            max_depth=depth,
            max_body_chars=body_chars,
        )

        # Reddit lazily renders ~25 comments; the view exposes the page's own
        # "more comments" deeplinks. Load a bounded number of them, merge into
        # the tree, then re-prune with the user's caps.
        anchors = view.pop("more_anchors", [])
        view["total_comment_count"] = view["post"].get("num_comments")
        if anchors:
            from .reddit import _comment_nodes, _soup, build_comment_tree, prune_tree

            soup = _soup(html)
            full_tree = build_comment_tree(_comment_nodes(soup, body_chars))
            merged, partials, remaining = await _load_more_comments(
                session,
                full_tree,
                anchors,
                max_comments=max_c,
                max_body_chars=body_chars,
                sort=comment_sort,
            )
            pruned, truncated = prune_tree(full_tree, max_comments=max_c, max_depth=depth)
            view["comments"] = pruned
            view["truncated_comment_count"] = truncated
            view["total_comments_rendered"] = len(list(_iter_tree(full_tree)))
            view["loaded_more_comments"] = merged
            view["loaded_more_partials"] = partials
            cursors, dropped = _cursor_list(remaining)
            view["more_subtree_cursors"] = cursors
            view["more_subtree_cursors_dropped"] = dropped
        else:
            view["loaded_more_comments"] = 0
            view["loaded_more_partials"] = 0
            view["more_subtree_cursors"] = []
            view["more_subtree_cursors_dropped"] = 0
        rendered = view["total_comments_rendered"]
        total = view["total_comment_count"]
        # Note: Reddit's comment-count includes deleted/removed comments that
        # no page will ever render, so unloaded may never reach zero.
        view["unloaded_comment_count"] = (
            max(0, (total or 0) - rendered) if total is not None else None
        )
        view["ok"] = True
        return view
    except RedditAccessError as e:
        return _err(e, "Check reddit_status(live=true); a block means stop, not retry.")
    except ValueError as e:
        return _err(e)


@mcp.tool()
async def reddit_get_more_comments(
    subreddit: str | None = None,
    post_id: str | None = None,
    comment_id: str | None = None,
    anchor_url: str | None = None,
    comment_sort: str = "top",
    max_comments: int = 100,
    max_body_chars: int = 2000,
) -> dict:
    """Load one comment's subtree by navigating its deeplink and clicking.

    Two ways to point at the subtree:
    1. anchor_url: pass one of reddit_get_thread's "more_subtree_cursors"
       entries verbatim (the webapp's own lazy-load link).
    2. comment_id: with subreddit and post_id; the tool builds the same
       deeplink. (The /x/{cid} form renders nothing; /comment/{cid}/ is the
       real one — verified live.)

    How it loads: navigates the webapp's comment deeplink in comment_sort
    (default top — the deeplink's default confidence window DROPS replies
    a sort=top thread shows, e.g. 19sk2yjtdhw4j), then clicks up to
    REDDIT_MCP_MAX_LOAD_MORE visible, enabled "N more replies" buttons one
    paced click at a time, then scrapes the focus subtree. Only cursors INSIDE
    the focus subtree are returned, so following them terminates.

    Platform limits (observed in testing 2026-11-17): no control loads
    further TOP-LEVEL comments (the thread page had no such button and ?limit=
    was ignored), and the webapp's /svc more-comments endpoint required
    client-version headers we do not send — clicking the page's own buttons
    is the supported path.

    Args:
        subreddit: Bare subreddit name (only with comment_id).
        post_id: Thread post id (only with comment_id).
        comment_id: The comment whose subtree you want (only without anchor_url).
        anchor_url: A more_subtree_cursors entry from reddit_get_thread.
        comment_sort: best, top, new, controversial, old. Top recommended:
            renders the top-reply window, matching a sort=top thread read.
        max_comments: Cap on returned comments (1-100).
        max_body_chars: Cap per comment body (100-8000).

    Returns:
        The focus comment subtree plus honest load accounting:
        total_comments_rendered (subtree size after clicks), load_more_rounds
        and load_more_clicks (click rounds used / actual buttons clicked,
        capped at one visible enabled button per paced step; each executed
        click is counted into reddit_status requests_made as an initiated
        browser action, not a guessed HTTP count), more_replies_available (sum
        of the remaining "N more replies" labels inside the subtree),
        truncated_comment_count (cut by the caps), more_subtree_cursors +
        more_subtree_cursors_dropped (remaining lazy-load pointers inside the
        subtree, capped at 10).
    """
    import re as _re

    from .reddit import (
        _comment_nodes,
        _soup,
        build_comment_tree,
        collect_more_anchors,
        comment_permalink,
        prune_tree,
    )

    try:
        if not anchor_url and not (subreddit and post_id and comment_id):
            return _err(
                ValueError(
                    "Provide anchor_url (from reddit_get_thread's more_subtree_cursors) "
                    "or subreddit + post_id + comment_id."
                )
            )
        if comment_sort not in COMMENT_SORTS:
            return _err(ValueError(f"comment_sort must be one of {sorted(COMMENT_SORTS)}"))
        max_c = clamp(max_comments, 1, 100)
        body_chars = clamp(max_body_chars, 100, 8000)

        if anchor_url:
            canonical_anchor = validate_reddit_url(anchor_url, allow_relative=True)
            m = _COMMENT_DEEPLINK_PATH_RE.fullmatch(urlsplit(canonical_anchor).path)
            if not m:
                return _err(
                    DisallowedURLError(
                        f"anchor_url is not a valid comment deeplink: {anchor_url!r}"
                    )
                )
            sub = clean_subreddit(m.group(1))
            if subreddit and clean_subreddit(subreddit).lower() != sub.lower():
                return _err(
                    DisallowedURLError(
                        f"anchor_url subreddit {sub!r} does not match subreddit {subreddit!r}"
                    )
                )
            pid = m.group(2)
            focus_id = m.group(3).removeprefix("t1_")
        else:
            if not subreddit or not post_id or not comment_id:
                return _err(
                    ValueError(
                        "Provide anchor_url (from reddit_get_thread's more_subtree_cursors) "
                        "or subreddit + post_id + comment_id."
                    )
                )
            sub = clean_subreddit(subreddit)
            pid = post_id.strip()
            if not _re.fullmatch(r"[A-Za-z0-9]+", pid):
                return _err(ValueError(f"Invalid post_id: {post_id!r}"))
            focus_id = comment_id.strip().removeprefix("t1_")
            if not _re.fullmatch(r"[A-Za-z0-9_]+", focus_id):
                return _err(ValueError(f"Invalid comment_id: {comment_id!r}"))

        url = comment_permalink(sub, pid, focus_id, sort=comment_sort)
        session = await get_session()
        html, (rounds_used, clicks_made) = await session.expand_subtree(
            url, focus_id, max_rounds=MAX_LOAD_MORE_PARTIALS
        )
        soup = _soup(html)
        focus_elem = soup.find(
            "shreddit-comment",
            attrs={"thingid": f"t1_{focus_id}"},
        )
        if focus_elem is None:
            # Some renders use id= instead of thingid=.
            focus_elem = soup.find("shreddit-comment", attrs={"id": f"t1_{focus_id}"})
        if focus_elem is None:
            return _err(
                ValueError(
                    f"Focus comment {focus_id!r} was not rendered by the deeplink. "
                    "It may be deleted or removed; re-read the thread to check."
                )
            )

        # Scrape the focus subtree (scoping node collection to the focus element
        # also scopes the returned cursors to inside the subtree).
        flat = _comment_nodes(focus_elem, body_chars)
        if not flat:
            return _err(
                ValueError(f"Focus comment {focus_id!r} rendered but its subtree is empty.")
            )
        focus_depth = flat[0]["depth"] or 0
        for node in flat:
            node["depth"] = (node["depth"] or 0) - focus_depth  # normalize to 0
        tree = build_comment_tree(flat)
        if not tree or tree[0]["id"] != focus_id:
            return _err(
                ValueError(
                    f"Focus comment {focus_id!r} was not rendered by the deeplink. "
                    "It may be deleted or removed; re-read the thread to check."
                )
            )

        remaining = collect_more_anchors(focus_elem)
        more_available = sum(
            int(m.group(1)) for a in remaining if (m := _re.search(r"(\d+)", a.get("label", "")))
        )
        pruned, truncated = prune_tree([tree[0]], max_comments=max_c, max_depth=50)
        cursors, dropped = _cursor_list(
            [{**a, "href": f"/r/{sub}/comments/{pid}/comment/{a['parent_id']}/"} for a in remaining]
        )
        return {
            "ok": True,
            "comment": pruned[0] if pruned else {},
            "focus_id": focus_id,
            "comment_sort": comment_sort,
            "total_comments_rendered": len(flat),
            "load_more_rounds": rounds_used,
            "load_more_clicks": clicks_made,
            "more_replies_available": more_available,
            "truncated_comment_count": truncated,
            "more_subtree_cursors": [
                {"parent_id": c["parent_id"], "anchor_url": c["anchor_url"]} for c in cursors
            ],
            "more_subtree_cursors_dropped": dropped,
        }
    except RedditAccessError as e:
        return _err(e, "Check reddit_status(live=true); a block means stop, not retry.")
    except ValueError as e:
        return _err(e)


@mcp.tool()
async def reddit_search(
    query: str,
    subreddit: str | None = None,
    sort: str = "relevance",
    time_filter: str = "all",
    limit: int = 15,
) -> dict:
    """Search Reddit (or one subreddit) and return matching posts.

    Uses the rendered search results page, since Reddit's search JSON endpoint
    was observed gated in testing even for browser sessions.

    Args:
        query: Search query.
        subreddit: Restrict search to this subreddit (omit for all of Reddit).
        sort: relevance, new, top, comments.
        time_filter: hour, day, week, month, year, all.
        limit: Max results (1-25).

    Returns:
        Matching posts: id, subreddit, title, permalink.
    """
    try:
        if not query or not query.strip():
            return _err(ValueError("query is required."))
        if sort not in SEARCH_SORTS:
            return _err(ValueError(f"sort must be one of {sorted(SEARCH_SORTS)}"))
        if time_filter not in TIME_FILTERS:
            return _err(ValueError(f"time_filter must be one of {sorted(TIME_FILTERS)}"))
        n = clamp(limit, 1, 25)

        if subreddit:
            sub = clean_subreddit(subreddit)
            url = (
                f"{BASE_URL}/r/{sub}/search/?"
                f"q={quote_plus(query)}&restrict_sr=on&sort={sort}&t={time_filter}"
            )
        else:
            url = f"{BASE_URL}/search/?q={quote_plus(query)}&sort={sort}&t={time_filter}"

        session = await get_session()
        html = await session.load_page(url, wait_selector='a[href*="/comments/"]')
        results = parse_search_dom(html, limit=n)
        return {
            "ok": True,
            "query": query,
            "subreddit": subreddit,
            "sort": sort,
            "time_filter": time_filter,
            "count": len(results),
            "results": results,
        }
    except RedditAccessError as e:
        return _err(e, "Check reddit_status(live=true); a block means stop, not retry.")
    except ValueError as e:
        return _err(e)


@mcp.tool()
async def reddit_subreddit_info(subreddit: str) -> dict:
    """Get best-effort subreddit metadata (name, members, active users, description).

    Stats are client-rendered and only appear for logged-in sessions; this
    loads the feed page and extracts them from the live DOM, so members may
    be null when logged out. Best-effort.

    Args:
        subreddit: Bare subreddit name, e.g. "ClaudeCode".
    """
    try:
        sub = clean_subreddit(subreddit)
        session = await get_session()
        html = await session.load_page(f"{BASE_URL}/r/{sub}/")
        info = parse_about_dom(html)
        sidebar = await session.extract_dom(EXTRACT_SIDEBAR_JS, url=f"{BASE_URL}/r/{sub}/")
        if isinstance(sidebar, dict):
            if sidebar.get("subscribers"):
                info["members"] = _to_int(sidebar["subscribers"])
            if sidebar.get("display_name"):
                info["name"] = sidebar["display_name"]
            if sidebar.get("description"):
                info["description_preview"] = sidebar["description"][:500]
        return info
    except RedditAccessError as e:
        return _err(e, "Check reddit_status(live=true); a block means stop, not retry.")
    except ValueError as e:
        return _err(e)


def main() -> None:
    """stdio entry point."""
    try:
        mcp.run()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
