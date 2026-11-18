"""Errors for Reddit access through the browser-session backend.

All failures are surfaced honestly: a block is a block, an HTTP error is an
HTTP error. Nothing retries around anti-bot responses.
"""


class RedditAccessError(Exception):
    """Base error for Reddit access."""


class RedditHTTPError(RedditAccessError):
    """Reddit returned a non-200 HTTP status."""

    def __init__(self, status: int, path: str):
        self.status = status
        self.path = path
        super().__init__(f"HTTP {status} for {path}")


class RedditBlockedError(RedditAccessError):
    """Reddit served an anti-bot / CAPTCHA challenge page instead of content."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"Reddit blocked the request: {detail}")


class BrowserNotStartedError(RedditAccessError):
    """The browser session has not been started yet."""


class BrowserLaunchError(RedditAccessError):
    """Chrome could not be launched for the persistent session."""


class DisallowedURLError(ValueError):
    """A URL or path failed the strict Reddit read-only allowlist."""


class RedditRedirectError(RedditAccessError):
    """Reddit redirected a request off-origin or to a disallowed route."""


class RedditResponseTooLargeError(RedditAccessError):
    """A rendered page or partial response exceeded the configured byte limit."""
