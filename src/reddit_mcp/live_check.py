"""One-request live smoke check for the browser-session backend.

Usage:
    uv run reddit-mcp-smoke [--subreddit ClaudeCode] [--keep-open]

Launches the persistent Chrome profile, loads one subreddit feed page, parses
the rendered DOM, prints the result honestly, and exits. Exit code 0 =
end-to-end access works; 1 = blocked or error.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import sys
from collections.abc import Callable

from .browser import BASE_URL, build_session_from_env
from .errors import RedditAccessError
from .reddit import clean_subreddit, parse_listing_dom


async def _wait_for_manual_browser(session, wait_fn: Callable[[], object] | None) -> None:
    """Human-only inspection/login phase; never retries the failed probe."""
    if hasattr(session, "enable_manual_browsing"):
        await session.enable_manual_browsing()
    print("\nChrome remains open for manual inspection/login; no automatic retry.")
    try:
        if wait_fn is not None:
            result = wait_fn()
            if inspect.isawaitable(result):
                await result
        else:
            await asyncio.to_thread(input, "Press Enter to close Chrome and exit... ")
    except (EOFError, KeyboardInterrupt):
        pass


async def run(
    subreddit: str,
    keep_open: bool,
    wait_fn: Callable[[], object] | None = None,
) -> int:
    session = build_session_from_env()
    print(f"profile_dir: {session.profile_dir}")
    print(f"headless:    {session.headless}")
    print("launching Chrome with the persistent profile...")

    try:
        await session.start()
    except RedditAccessError as e:
        with contextlib.suppress(Exception):
            await session.stop()
        print(f"FAIL: {e}")
        return 1
    except Exception as e:
        with contextlib.suppress(Exception):
            await session.stop()
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1

    try:
        try:
            sub = clean_subreddit(subreddit)
        except ValueError as e:
            print(f"FAIL: {e}")
            return 1

        print(f"probe: loading {BASE_URL}/r/{sub}/ ...")
        try:
            html = await session.load_page(f"{BASE_URL}/r/{sub}/")
            posts, after = parse_listing_dom(html, preview_chars=120)
        except RedditAccessError as e:
            print(f"BLOCKED: {type(e).__name__}: {e}")
            if keep_open:
                await _wait_for_manual_browser(session, wait_fn)
            else:
                print("For manual inspection/login, use --keep-open; no automatic retry.")
            return 1
        except Exception as e:
            print(f"FAIL: {type(e).__name__}: {e}")
            return 1

        state = await session.login_state()
        print(f"login_state: {state}")

        if not posts:
            print(
                f"FAIL: page loaded from {BASE_URL}/r/{sub}/ but 0 posts were parsed (empty probe)."
            )
            if keep_open:
                await _wait_for_manual_browser(session, wait_fn)
            return 1

        print(f"OK: page rendered and parsed; posts seen: {len(posts)}; next cursor: {bool(after)}")
        for p in posts[:5]:
            print(f"  - [{p['id']}] {p['title'][:70]!r} by u/{p['author']} score={p['score']}")

        if keep_open:
            await _wait_for_manual_browser(session, wait_fn)
        return 0
    finally:
        await session.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="One-request Reddit browser-session smoke check")
    parser.add_argument("--subreddit", "-s", default="ClaudeCode")
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Leave the Chrome window open after the check (for logging in).",
    )
    args = parser.parse_args()

    try:
        sys.exit(asyncio.run(run(args.subreddit, args.keep_open)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
