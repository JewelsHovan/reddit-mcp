"""Browser-session backend for Reddit access via rendered pages.

In testing (2026-11-17), Reddit's edge frequently returned HTTP 403/block pages
to plain HTTP clients (such as ``requests``, even with session cookies) and
gated ``/r/*.json`` endpoints for automated browsers, while serving rendered
HTML pages to a real headed Chrome session. This backend therefore drives a
real system Chrome through Playwright with a dedicated persistent profile,
navigates normal Reddit pages, and returns the rendered DOM.

Design rules:
- One persistent Chrome profile (``~/.reddit-mcp/chrome-profile`` by default):
  works logged-out for public content in testing; log in once for more.
- All navigations are serialized and paced (default >= 2.0s apart).
- A bounded LRU+TTL cache absorbs repeated reads of the same URL.
- Any anti-bot challenge or non-200 status fails fast, with no retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.async_api import async_playwright

from .errors import (
    BrowserLaunchError,
    BrowserNotStartedError,
    DisallowedURLError,
    RedditAccessError,
    RedditBlockedError,
    RedditHTTPError,
    RedditRedirectError,
    RedditResponseTooLargeError,
)

BASE_URL = "https://www.reddit.com"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB bound per page/partial response
MAX_CACHE_ENTRIES = 16
MAX_EXPAND_CLICKS = 10

_ALLOWED_PATH_RES = (
    # Subreddit feed: /r/<sub>/ (len 1-21 supports test fixtures like /r/a/)
    re.compile(r"^/r/[A-Za-z0-9_]{1,21}/?$"),
    # Subreddit sorted listing: /r/<sub>/<sort>/
    re.compile(r"^/r/[A-Za-z0-9_]{1,21}/(?:hot|new|top|rising|best)/?$"),
    # Thread read routes, including Reddit's canonical title-slug redirects.
    re.compile(
        r"^/r/[A-Za-z0-9_]{1,21}/comments/[A-Za-z0-9]+"
        r"(?:/(?!(?:edit|delete|submit|report)(?:/|$))[A-Za-z0-9_-]+"
        r"(?:/[A-Za-z0-9_]+)?)?/?$"
    ),
    # Comment deeplink & partial form: /r/<sub>/comments/<id>/comment/<cid>/
    re.compile(r"^/r/[A-Za-z0-9_]{1,21}/comments/[A-Za-z0-9]+/comment/[A-Za-z0-9_]+/?$"),
    # Global and subreddit search
    re.compile(r"^/(?:r/[A-Za-z0-9_]{1,21}/)?search/?$"),
    # Subreddit infinite-scroll listing partial
    re.compile(r"^/svc/shreddit/community-more-posts/(?:hot|new|top|rising|best)/?$"),
)
_SEARCH_PATH_RE = re.compile(r"^/(?:r/[A-Za-z0-9_]{1,21}/)?search/?$")
_FOCUS_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _is_reddit_origin(url: str) -> bool:
    """Return True iff url has exact origin https://www.reddit.com (no creds/port)."""
    if not isinstance(url, str) or not url:
        return False
    if "\\" in url or any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in url):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == "www.reddit.com"
        and parsed.hostname == "www.reddit.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and "@" not in parsed.netloc
        and ":" not in parsed.netloc
    )


def validate_reddit_url(url_or_path: str, *, allow_relative: bool = False) -> str:
    """Validate and normalize a URL/path against the strict Reddit read allowlist.

    Enforces:
    - Origin strictly https://www.reddit.com (no credentials, port, or lookalike host)
    - No whitespace, control chars, fragments, backslashes, or encoded traversal
    - Path matches one of the known read-only Reddit routes
    """
    if not isinstance(url_or_path, str):
        raise DisallowedURLError("URL must be a string.")
    raw = url_or_path
    if not raw or raw != raw.strip():
        raise DisallowedURLError(f"Disallowed empty or padded URL: {url_or_path!r}")
    if any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in raw):
        raise DisallowedURLError(f"Disallowed control/whitespace in URL: {url_or_path!r}")
    if "\\" in raw or "#" in raw:
        raise DisallowedURLError(f"Disallowed backslash or fragment in URL: {url_or_path!r}")
    if "%00" in raw.lower():
        raise DisallowedURLError(f"Disallowed null byte in URL: {url_or_path!r}")

    if raw.startswith("/"):
        if not allow_relative:
            raise DisallowedURLError(f"Relative path not allowed here: {url_or_path!r}")
        if raw.startswith("//"):
            raise DisallowedURLError(f"Protocol-relative URL not allowed: {url_or_path!r}")
        candidate = f"{BASE_URL}{raw}"
    else:
        candidate = raw

    if not _is_reddit_origin(candidate):
        raise DisallowedURLError(
            f"Disallowed origin (only https://www.reddit.com without credentials/port): {url_or_path!r}"
        )

    parsed = urlsplit(candidate)
    if parsed.fragment:
        raise DisallowedURLError(f"Disallowed fragment in URL: {url_or_path!r}")

    path = parsed.path
    lowered_path = path.lower()
    if (
        ";" in path
        or "//" in path
        or ".." in path
        or "%2e" in lowered_path
        or "%2f" in lowered_path
        or "%5c" in lowered_path
        or "%25" in lowered_path
    ):
        raise DisallowedURLError(f"Disallowed traversal or encoding in path: {url_or_path!r}")
    if unquote(path) != path:
        raise DisallowedURLError(f"Disallowed encoded characters in path: {url_or_path!r}")

    if not any(pattern.fullmatch(path) for pattern in _ALLOWED_PATH_RES):
        raise DisallowedURLError(f"URL path is not an allowed read-only Reddit route: {path!r}")

    query = parsed.query
    if query:
        lowered_query = query.lower()
        if _SEARCH_PATH_RE.fullmatch(path):
            # Allow user search text in q=, but forbid traversal/backslashes in other params.
            for key, values in parse_qs(query, keep_blank_values=True).items():
                if key == "q":
                    continue
                for val in values:
                    lv = val.lower()
                    if "\\" in val or "%5c" in lv or "%2e" in lv or ".." in val:
                        raise DisallowedURLError(
                            f"Disallowed query parameter value in search URL: {url_or_path!r}"
                        )
        elif "%5c" in lowered_query or "%2e" in lowered_query or ".." in query:
            raise DisallowedURLError(
                f"Disallowed traversal or backslash in query string: {url_or_path!r}"
            )

    return f"{BASE_URL}{path}?{query}" if query else f"{BASE_URL}{path}"


# Click at most ONE visible, enabled "N more replies"-style button inside the
# focus comment's subtree per evaluation. Returning 1/0 lets Python pace each
# click individually and check for block/challenge pages before and after.
CLICK_MORE_JS = """
async (focusId) => {
  const focus = [...document.querySelectorAll('shreddit-comment')].find(c => {
    const tid = c.getAttribute('thingid') || c.getAttribute('id') || '';
    return tid === ('t1_' + focusId) || tid === focusId;
  });
  if (!focus) return 0;
  for (const b of focus.querySelectorAll('button')) {
    const label = b.getAttribute('aria-label') || b.innerText || '';
    if (!/more replies|more comments|load more|view all/i.test(label)) continue;
    if (b.disabled || b.getAttribute('aria-disabled') === 'true' || b.hidden) continue;
    const style = window.getComputedStyle ? window.getComputedStyle(b) : null;
    if (style && (style.display === 'none' || style.visibility === 'hidden')) continue;
    const visible = b.offsetParent !== null || b.offsetWidth > 0 || b.offsetHeight > 0 || b.getClientRects().length > 0;
    if (!visible) continue;
    b.click();
    return 1;
  }
  return 0;
}
"""

LOGIN_STATE_JS = """
() => {
  const login = document.querySelector('a[href*="/login"]');
  if (login) return "logged_out";
  const userMenu = document.querySelector(
    'button[aria-label*="user menu" i], a[href^="/notifications"]'
  );
  if (userMenu) return "logged_in";
  return "unknown";
}
"""

# In-page fetch of the app's own infinite-scroll partial endpoint: the exact
# call shreddit makes on scroll, same accept header, same session. No
# navigation, no full-page render. Redirects are explicitly rejected.
FETCH_PARTIAL_JS = """
async (url) => {
  try {
    const r = await fetch(url, {
      headers: {accept: "text/vnd.reddit.partial+html"},
      credentials: "include",
      redirect: "error"
    });
    if (r.redirected) {
      return {status: 0, redirected: true, url: r.url || "", contentType: "", text: ""};
    }
    const text = await r.text();
    return {
      status: r.status,
      redirected: false,
      url: r.url || "",
      contentType: r.headers.get("content-type") || "",
      text
    };
  } catch (err) {
    return {
      status: 0,
      error: String(err && err.message ? err.message : err),
      contentType: "",
      text: ""
    };
  }
}
"""


def is_challenge_text(text: str) -> bool:
    """Return True if an HTML body is an anti-bot/CAPTCHA or forbidden block page."""
    lowered = text[:4096].lower()
    return (
        "prove your humanity" in lowered
        or "www.google.com/recaptcha/api.js" in lowered
        # Reddit's edge block page for headless/automated clients.
        or "blocked by network security" in lowered
        or "whoa there, pardner" in lowered
        or "you've been blocked" in lowered
        or "you have been blocked" in lowered
        or "<title>403 forbidden</title>" in lowered
        or "<title>access denied</title>" in lowered
    )


def default_profile_dir() -> Path:
    return Path(os.getenv("REDDIT_MCP_PROFILE_DIR", "~/.reddit-mcp/chrome-profile")).expanduser()


def _known_chrome_default_dirs() -> list[Path]:
    """Return known OS default Chrome/Chromium user-data-dir locations."""
    home = Path.home()
    candidates: list[Path] = [
        # macOS
        home / "Library" / "Application Support" / "Google" / "Chrome",
        home / "Library" / "Application Support" / "Google" / "Chrome Beta",
        home / "Library" / "Application Support" / "Google" / "Chrome Canary",
        home / "Library" / "Application Support" / "Google" / "Chrome Dev",
        home / "Library" / "Application Support" / "Chromium",
        # Linux / XDG
        home / ".config" / "google-chrome",
        home / ".config" / "google-chrome-beta",
        home / ".config" / "google-chrome-unstable",
        home / ".config" / "chromium",
        home / "snap" / "chromium" / "common" / "chromium",
        # Windows
        home / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
        home / "AppData" / "Local" / "Chromium" / "User Data",
    ]
    xdg_config = os.getenv("XDG_CONFIG_HOME", "").strip()
    if xdg_config:
        xdg = Path(xdg_config).expanduser()
        candidates.extend([xdg / "google-chrome", xdg / "chromium"])
    local_appdata = os.getenv("LOCALAPPDATA", "").strip()
    if local_appdata:
        la = Path(local_appdata).expanduser()
        candidates.extend(
            [
                la / "Google" / "Chrome" / "User Data",
                la / "Chromium" / "User Data",
            ]
        )
    return candidates


def _ensure_private_profile_dir(profile_dir: Path) -> Path:
    """Refuse OS Chrome default user-data-dirs without modifying them; enforce 0700."""
    resolved = profile_dir.expanduser().resolve()
    norm_resolved = os.path.normcase(str(resolved))
    for bad in _known_chrome_default_dirs():
        norm_bad = os.path.normcase(str(bad.resolve())).rstrip(os.sep)
        if norm_resolved == norm_bad or norm_resolved.startswith(norm_bad + os.sep):
            raise BrowserLaunchError(
                f"Refusing to use OS Chrome default user-data-dir ({profile_dir!s}). "
                "Configure a dedicated profile directory via REDDIT_MCP_PROFILE_DIR."
            )
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(profile_dir, 0o700)
    return profile_dir


def build_session_from_env() -> BrowserSession:
    """Construct a BrowserSession from REDDIT_MCP_* environment variables."""
    # Note: REDDIT_MCP_HEADLESS=true is supported mechanically, though in testing
    # (2026-11-17) Reddit's edge blocked headless Chrome ("You've been blocked by
    # network security"). Keep it headed by default; use REDDIT_MCP_WINDOW_POSITION
    # to move the window off-screen instead of hiding the browser.
    window_position = os.getenv("REDDIT_MCP_WINDOW_POSITION", "").strip()
    return BrowserSession(
        profile_dir=default_profile_dir(),
        headless=os.getenv("REDDIT_MCP_HEADLESS", "false").lower() in ("1", "true", "yes"),
        window_position=window_position or None,
        min_interval=float(os.getenv("REDDIT_MCP_MIN_INTERVAL", "2.0")),
        ttl=float(os.getenv("REDDIT_MCP_CACHE_TTL", "120")),
        request_timeout=float(os.getenv("REDDIT_MCP_REQUEST_TIMEOUT", "45.0")),
        max_response_bytes=int(os.getenv("REDDIT_MCP_MAX_RESPONSE_BYTES", str(MAX_RESPONSE_BYTES))),
        max_cache_entries=int(os.getenv("REDDIT_MCP_CACHE_MAX_ENTRIES", str(MAX_CACHE_ENTRIES))),
    )


class BrowserSession:
    """A single-tab persistent Chrome session used for all Reddit reads."""

    def __init__(
        self,
        profile_dir: Path | str,
        headless: bool = False,
        min_interval: float = 2.0,
        ttl: float = 120.0,
        request_timeout: float = 45.0,
        window_position: str | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_cache_entries: int = MAX_CACHE_ENTRIES,
        max_expand_clicks: int = MAX_EXPAND_CLICKS,
    ):
        self.profile_dir = Path(profile_dir).expanduser()
        self.headless = headless
        # e.g. "-3200,240": real headed Chrome (the fingerprint Reddit serves),
        # just opened off-screen so it never covers the desktop.
        self.window_position = window_position
        self.min_interval = max(0.0, min_interval)
        self.ttl = max(0.0, ttl)
        self.request_timeout = max(0.01, float(request_timeout))
        self.max_response_bytes = max(1, int(max_response_bytes))
        self.max_cache_entries = max(0, int(max_cache_entries))
        self.max_expand_clicks = max(0, int(max_expand_clicks))

        self._pw = None
        self._ctx: Any = None
        self._page: Any = None

        self._lock = asyncio.Lock()
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._last_request = 0.0
        self._requests_made = 0
        self._cache_hits = 0
        self._allowing_origin_warmup = False
        self._last_blocked_redirect: str | None = None

    # ------------------------------------------------------------------ stats

    @property
    def requests_made(self) -> int:
        """Total browser actions initiated (navigations, partial fetches, executed clicks)."""
        return self._requests_made

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    # ----------------------------------------------------------- lifecycle

    def _is_allowed_navigation_target(self, url: str) -> bool:
        if self._allowing_origin_warmup and url in (BASE_URL, f"{BASE_URL}/"):
            return True
        try:
            validate_reddit_url(url)
            return True
        except DisallowedURLError:
            return False

    async def _handle_route(self, route: Any) -> None:
        request = route.request
        if request.is_navigation_request() and request.resource_type == "document":
            frame = getattr(request, "frame", None)
            is_main = frame is None or getattr(frame, "parent_frame", None) is None
            if is_main:
                target = str(getattr(request, "url", "") or "")
                if not self._is_allowed_navigation_target(target):
                    self._last_blocked_redirect = target
                    await route.abort("blockedbyclient")
                    return
        await route.continue_()

    async def start(self) -> None:
        """Launch Chrome with the persistent profile (navigations happen on demand)."""
        if self._ctx is not None:
            return
        _ensure_private_profile_dir(self.profile_dir)
        launch_args: list[str] = []
        if self.window_position and not self.headless:
            launch_args.append(f"--window-position={self.window_position}")
        try:
            self._pw = await asyncio.wait_for(async_playwright().start(), self.request_timeout)
            self._ctx = await asyncio.wait_for(
                self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    channel="chrome",  # real system Chrome, not a bundled build
                    headless=self.headless,
                    viewport=None,
                    args=launch_args,
                ),
                self.request_timeout,
            )
            if hasattr(self._ctx, "route"):
                await self._ctx.route("**/*", self._handle_route)
            pages = self._ctx.pages
            self._page = pages[0] if pages else await self._ctx.new_page()
        except Exception as e:  # pragma: no cover - environment dependent
            with contextlib.suppress(Exception):
                await self.stop()
            raise BrowserLaunchError(
                f"Could not launch Chrome (channel='chrome'). Is Google Chrome installed? "
                f"Error: {e}"
            ) from e

    async def enable_manual_browsing(self) -> None:
        """Release the document-route guard for the smoke CLI's human login.

        Never used by MCP tools. The CLI waits for human input and then
        closes this context; no agent requests run in this phase.
        """
        if self._ctx is not None:
            await self._ctx.unroute("**/*", self._handle_route)

    async def stop(self) -> None:
        """Close the browser context and Playwright driver."""
        ctx = self._ctx
        pw = self._pw
        self._ctx = None
        self._page = None
        self._pw = None
        try:
            if ctx is not None:
                await asyncio.wait_for(ctx.close(), timeout=10.0)
        finally:
            if pw is not None:
                await asyncio.wait_for(pw.stop(), timeout=10.0)

    async def _page_or_new(self):
        if self._ctx is None:
            raise BrowserNotStartedError(
                "Browser session not started. Call start() first (or run reddit-mcp-smoke)."
            )
        if self._page is None or self._page.is_closed():
            self._page = await self._ctx.new_page()
        return self._page

    # ------------------------------------------------------------- network

    async def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_request
        wait = self.min_interval - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    def _cached(self, url: str) -> str | None:
        hit = self._cache.get(url)
        if hit is None:
            return None
        if time.monotonic() - hit[0] >= self.ttl:
            self._cache.pop(url, None)
            return None
        self._cache.move_to_end(url)
        return hit[1]

    def _cache_put(self, url: str, value: str) -> None:
        if self.max_cache_entries <= 0 or self.ttl <= 0:
            return
        now = time.monotonic()
        if url in self._cache:
            self._cache.move_to_end(url)
        self._cache[url] = (now, value)
        while len(self._cache) > self.max_cache_entries:
            self._cache.popitem(last=False)

    def _check_response_size(self, text: str, url: str) -> None:
        size = len(text.encode("utf-8"))
        if size > self.max_response_bytes:
            raise RedditResponseTooLargeError(
                f"Response for {url} exceeded {self.max_response_bytes} bytes ({size} bytes)."
            )

    async def _bounded_content(self, page: Any, url: str) -> str:
        try:
            html = await asyncio.wait_for(page.content(), self.request_timeout)
        except TimeoutError as e:
            raise RedditHTTPError(-1, f"{url} (content read timed out)") from e
        self._check_response_size(html, url)
        return html

    async def _goto_checked(self, page: Any, url: str, *, allow_warmup: bool = False) -> Any:
        self._last_blocked_redirect = None
        if allow_warmup:
            self._allowing_origin_warmup = True
        try:
            try:
                resp = await asyncio.wait_for(
                    page.goto(url, wait_until="domcontentloaded"), self.request_timeout
                )
            except TimeoutError as e:
                raise RedditHTTPError(-1, f"{url} (navigation timed out)") from e
            except Exception as e:
                if self._last_blocked_redirect:
                    blocked = self._last_blocked_redirect
                    raise RedditRedirectError(
                        f"Blocked redirect to disallowed URL: {blocked}"
                    ) from e
                raise RedditHTTPError(-1, f"{url} (navigation failed: {e})") from e
            finally:
                if allow_warmup:
                    self._allowing_origin_warmup = False

            self._requests_made += 1

            if self._last_blocked_redirect:
                blocked = self._last_blocked_redirect
                raise RedditRedirectError(f"Blocked redirect to disallowed URL: {blocked}")

            final_url = str(getattr(resp, "url", None) or getattr(page, "url", "") or "")
            if allow_warmup:
                if final_url and not _is_reddit_origin(final_url):
                    raise RedditRedirectError(
                        f"Blocked off-origin redirect during warmup: {final_url!r}"
                    )
            else:
                if not _is_reddit_origin(final_url):
                    raise RedditRedirectError(f"Blocked off-origin redirect to {final_url!r}")
                try:
                    validate_reddit_url(final_url)
                except DisallowedURLError as e:
                    raise RedditRedirectError(
                        f"Blocked redirect to disallowed route: {final_url!r}"
                    ) from e
            return resp
        finally:
            self._last_blocked_redirect = None

    async def load_page(
        self,
        url: str,
        wait_selector: str | None = None,
        settle: float = 1.5,
    ) -> str:
        """Navigate the tab to a URL and return the rendered HTML.

        Paced, cached, and fail-fast: a non-200 response or a challenge page
        raises immediately. No retries, ever.

        Args:
            url: Full URL to navigate to.
            wait_selector: Optional CSS selector to wait for before reading.
            settle: Extra seconds to let client-rendered content appear.

        Returns:
            The rendered page HTML.
        """
        url = validate_reddit_url(url)
        cached = self._cached(url)
        if cached is not None:
            self._cache_hits += 1
            return cached

        async with self._lock:
            # Re-check cache after acquiring the lock (another call may have won).
            cached = self._cached(url)
            if cached is not None:
                self._cache_hits += 1
                return cached

            page = await self._page_or_new()
            await self._pace()
            resp = await self._goto_checked(page, url)

            status = resp.status if resp else None
            if status is not None and status != 200:
                raise RedditHTTPError(status, url)

            if settle > 0:
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=int(settle * 1000))
            if wait_selector:
                with contextlib.suppress(Exception):
                    # empty results are a valid outcome; parse what we got
                    await page.wait_for_selector(wait_selector, timeout=6000)

            html = await self._bounded_content(page, url)
            if is_challenge_text(html):
                raise RedditBlockedError(
                    "anti-bot challenge page ('Prove your humanity'). "
                    "Open the Chrome window and complete any required check, "
                    "or wait before retrying. Do not retry in a loop."
                )

            self._cache_put(url, html)
            return html

    async def extract_dom(
        self, js: str, url: str | None = None, settle: float = 2.0
    ) -> dict | list | None:
        """Evaluate JS on a page to extract client-rendered data.

        Only for structured extraction (attribute pulls), never page actions.
        If url is given and the tab is not currently on it, navigates first
        (paced like any other request) — a cache hit elsewhere must not leave
        the tab on the wrong page.
        """
        validated_url = validate_reddit_url(url) if url is not None else None
        async with self._lock:
            page = await self._page_or_new()
            if validated_url is not None:
                current = str(getattr(page, "url", "") or "")
                cur_parts = urlsplit(current)
                tgt_parts = urlsplit(validated_url)
                same_page = (
                    _is_reddit_origin(current)
                    and cur_parts.path.rstrip("/") == tgt_parts.path.rstrip("/")
                    and cur_parts.query == tgt_parts.query
                )
                if not same_page:
                    await self._pace()
                    try:
                        resp = await self._goto_checked(page, validated_url)
                    except RedditAccessError:
                        return None
                    if resp is not None and resp.status != 200:
                        return None
            if settle > 0:
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=int(settle * 1000))
            try:
                return await asyncio.wait_for(page.evaluate(js), min(10.0, self.request_timeout))
            except Exception:
                return None

    async def fetch_partial(self, path_or_url: str) -> str:
        """Fetch a shreddit partial endpoint in-page and return its HTML.

        The partial endpoints (community-more-posts, comments) are the webapp's
        own infinite-scroll data path: they answered in-page fetches in testing
        even when logged out, unlike the gated /r/*.json endpoints.
        The tab must be on a reddit.com page (navigates to BASE_URL if not).

        Paced, cached, and fail-fast like load_page: a non-200 status or a
        block page raises immediately. No retries.

        Args:
            path_or_url: Same-origin path ("/svc/shreddit/...") or full URL.

        Returns:
            The partial HTML text.
        """
        url = validate_reddit_url(path_or_url, allow_relative=True)
        cached = self._cached(url)
        if cached is not None:
            self._cache_hits += 1
            return cached

        async with self._lock:
            # Re-check cache after acquiring the lock (another call may have won).
            cached = self._cached(url)
            if cached is not None:
                self._cache_hits += 1
                return cached

            page = await self._page_or_new()
            if not _is_reddit_origin(str(getattr(page, "url", "") or "")):
                await self._pace()
                resp = await self._goto_checked(page, BASE_URL, allow_warmup=True)
                if resp is not None and resp.status != 200:
                    raise RedditHTTPError(resp.status, BASE_URL)

            await self._pace()
            try:
                result = await asyncio.wait_for(
                    page.evaluate(FETCH_PARTIAL_JS, url), self.request_timeout
                )
            except TimeoutError as e:
                raise RedditHTTPError(-1, f"{url} (partial fetch timed out)") from e
            self._requests_made += 1

            if not isinstance(result, dict):
                raise RedditHTTPError(-1, f"{url} (unexpected partial fetch result)")
            if result.get("redirected"):
                raise RedditRedirectError(
                    f"Blocked partial redirect from {url} to {result.get('url') or 'unknown'}"
                )
            if result.get("error"):
                raise RedditRedirectError(
                    f"Partial fetch aborted (redirect or network error) for {url}: {result['error']}"
                )
            final_url = str(result.get("url") or "")
            if final_url and not _is_reddit_origin(final_url):
                raise RedditRedirectError(f"Blocked off-origin partial response from {final_url!r}")

            status = result["status"]
            content_type = result["contentType"]
            text = result["text"]

            if status != 200:
                raise RedditHTTPError(status, url)
            self._check_response_size(text, url)
            if is_challenge_text(text) or "text/html" in content_type:
                raise RedditBlockedError(
                    "block page served instead of partial content. Open the Chrome "
                    "window and check for a challenge page; do not retry in a loop."
                )

            self._cache_put(url, text)
            return text

    # ------------------------------------------------------------- session

    async def expand_subtree(
        self,
        url: str,
        focus_id: str,
        max_rounds: int = 3,
        round_wait: float = 2.5,
    ) -> tuple[str, tuple[int, int]]:
        """Navigate to a comment deeplink, then click its subtree's lazy-load
        buttons one paced click at a time up to the click budget.

        Uses the page's own "N more replies" controls (what a user clicks):
        clicks at most ONE visible, enabled button per paced step and checks
        for challenge/forbidden pages before clicking and after each click.

        NOT TTL-cached: clicking mutates page state, so every call navigates
        fresh (still paced). Fails fast on non-200/challenge like load_page.

        Args:
            url: Comment deeplink (with ?sort=).
            focus_id: The focus comment's bare id (thingid without t1_).
            max_rounds: Maximum total click steps (bounded by max_expand_clicks).
            round_wait: Seconds to wait after each click for newly loaded replies
                to render.

        Returns:
            Tuple of (final rendered HTML, (rounds_used, clicks_made)), where
            clicks_made is the actual number of executed clicks.
        """
        url = validate_reddit_url(url)
        if not isinstance(focus_id, str) or not _FOCUS_ID_RE.fullmatch(focus_id):
            raise DisallowedURLError(f"Invalid focus comment id: {focus_id!r}")

        budget = max(0, min(int(max_rounds), self.max_expand_clicks))

        async with self._lock:
            page = await self._page_or_new()
            await self._pace()
            resp = await self._goto_checked(page, url)
            if resp is not None and resp.status != 200:
                raise RedditHTTPError(resp.status, url)
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=3000)

            # Check for challenge/forbidden page BEFORE executing any clicks.
            html = await self._bounded_content(page, url)
            if is_challenge_text(html):
                raise RedditBlockedError(
                    "anti-bot challenge page ('Prove your humanity'). Open the "
                    "Chrome window and check for a challenge; do not retry in a loop."
                )

            rounds_used = 0
            clicks_made = 0
            for _ in range(budget):
                await self._pace()
                try:
                    clicked = await asyncio.wait_for(
                        page.evaluate(CLICK_MORE_JS, focus_id),
                        min(15.0, self.request_timeout),
                    )
                except Exception:
                    clicked = 0
                if not clicked:
                    break
                # One visible enabled button per evaluation; count actual executed click.
                executed = 1
                rounds_used += 1
                clicks_made += executed
                self._requests_made += executed
                if round_wait > 0:
                    await asyncio.sleep(round_wait)

                current = str(getattr(page, "url", "") or "")
                if current and not _is_reddit_origin(current):
                    raise RedditRedirectError(
                        f"Blocked off-origin navigation after click to {current!r}"
                    )
                html = await self._bounded_content(page, url)
                if is_challenge_text(html):
                    raise RedditBlockedError(
                        "anti-bot challenge page ('Prove your humanity'). Open the "
                        "Chrome window and check for a challenge; do not retry in a loop."
                    )

            return html, (rounds_used, clicks_made)

    async def login_state(self) -> str:
        """Best-effort login state from the current page DOM.

        Returns "logged_in", "logged_out", or "unknown". Never a guarantee;
        the live probe in reddit_status is the real check.
        """
        if self._page is None:
            return "unknown"
        try:
            return await asyncio.wait_for(self._page.evaluate(LOGIN_STATE_JS), 5.0)
        except Exception:
            return "unknown"

    async def status(self) -> dict:
        """Cheap local status (no network).

        requests_made counts initiated browser actions (page navigations,
        in-page partial fetches, and executed load-more button clicks).
        """
        return {
            "browser_running": self._ctx is not None,
            "headless": self.headless,
            "profile_dir": str(self.profile_dir),
            "window_position": self.window_position,
            "min_interval_s": self.min_interval,
            "cache_ttl_s": self.ttl,
            "requests_made": self._requests_made,
            "cache_hits": self._cache_hits,
            "counter_semantics": (
                "requests_made counts initiated browser actions (page navigations, "
                "in-page partial fetches, and executed load-more button clicks), "
                "not a raw browser HTTP/XHR network ledger."
            ),
            "login_state": await self.login_state(),
        }
