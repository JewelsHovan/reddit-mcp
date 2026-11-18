# reddit-mcp

**Read Reddit from your AI agent using a local Chrome browser.** Browse feeds, search posts, read threads, and expand replies through six read-only [Model Context Protocol](https://modelcontextprotocol.io/) tools.

No Reddit API key or OAuth app is required for public pages that Reddit serves to your browser. Runs locally over **stdio**—no hosted service, listening port, or remote browser. Works with Claude Code, Codex, Compass, and other stdio MCP clients.

> **Experimental, unofficial integration.** Uses Reddit's changing web UI and internal HTML partials, not a stable API. Access depends on your network, browser session, and Reddit's rules. A successful test is not permission to collect or redistribute content. Respect Reddit's terms, rate limits, and privacy requirements; do not use this for bulk collection or bypass access restrictions.

## Quick start

You need **Python 3.11+**, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, and **Google Chrome** installed. The browser uses Playwright's `channel="chrome"`, not the bundled Chromium. A graphical desktop is required for the default headed mode.

```bash
git clone https://github.com/JewelsHovan/reddit-mcp.git
cd reddit-mcp
uv sync --locked
uv run reddit-mcp-smoke --subreddit ClaudeCode
```

The smoke check opens a dedicated Chrome profile, loads a subreddit, and reports whether it parsed real posts. Public pages may work without logging in. If access is denied, **stop** rather than repeatedly retrying.

### Optional: install a standalone command

To install the tagged release into an isolated uv tool environment instead of maintaining a clone:

```bash
uv tool install git+https://github.com/JewelsHovan/reddit-mcp.git@v0.1.0
reddit-mcp-smoke --subreddit ClaudeCode
```

Then configure your MCP client with `"command": "reddit-mcp"` and no arguments. Ensure uv's tool directory is on the client's PATH (`uv tool update-shell` helps for terminal clients). This is a GitHub installation, **not a PyPI package publication**. Uninstall with `uv tool uninstall reddit-mcp`.

### Connect Claude Code

Run this **from the project where you use Claude**, replacing the path with your clone's absolute path:

```bash
claude mcp add --transport stdio --scope project reddit -- \
  uv --directory /absolute/path/to/reddit-mcp run --locked reddit-mcp
```

Approve the project server when prompted; check `/mcp`, then ask:

> Use Reddit to find five recent r/ClaudeCode discussions about hooks. Read one thread with at most 15 comments. Include source links and tell me what wasn't loaded.

### Connect any stdio MCP client

Use this entry in your client's MCP configuration (`.mcp.json` for Claude Code). **Replace the absolute path**; this is not a path to your agent's working project.

```json
{
  "mcpServers": {
    "reddit": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/reddit-mcp", "run", "--locked", "reddit-mcp"]
    }
  }
}
```

GUI clients may need the full path to `uv` if their PATH differs from your terminal.

### Codex

The CLI adds a **user-level** MCP entry. Use this only if you want Reddit available across Codex projects:

```bash
codex mcp add reddit -- \
  uv --directory /absolute/path/to/reddit-mcp run --locked reddit-mcp
```

### Compass

Place the same `mcpServers` JSON in your working project's `.compass/mcp.json`. Project servers require approval. Add `"idleTimeout": 0` to the `reddit` entry if you want it to remain running during idle periods. After an update, reconnect the server in `/mcp` so Python reloads the changed code.

## Tools

| Tool | What it does |
|---|---|
| `reddit_status(live=false, probe_subreddit="ClaudeCode")` | Browser state and counters; `live=true` probes a feed |
| `reddit_list_posts(subreddit, sort="hot", time_filter="week", limit=10, after=null)` | Feed summaries; pass `after` back unchanged for pagination |
| `reddit_search(query, subreddit=null, sort="relevance", time_filter="all", limit=15)` | Subreddit-scoped or Reddit-wide post search |
| `reddit_get_thread(url_or_id, subreddit=null, comment_sort="best", max_comments=30, max_depth=3, max_body_chars=2000)` | Post body and a bounded nested comment tree |
| `reddit_get_more_comments(subreddit=null, post_id=null, comment_id=null, anchor_url=null, comment_sort="top", max_comments=100, max_body_chars=2000)` | Focused subtree expansion using the page's reply controls |
| `reddit_subreddit_info(subreddit)` | Best-effort name and metadata; unavailable fields are `null` |

Example tool arguments:

```json
{"subreddit": "LocalLLaMA", "sort": "top", "time_filter": "week", "limit": 5}
```

```json
{"url_or_id": "https://www.reddit.com/r/LocalLLaMA/comments/POST_ID/title/", "comment_sort": "top", "max_comments": 20, "max_depth": 3}
```

For a bare post ID, also pass `subreddit`. To expand a branch, pass its returned `more_subtree_cursors[].anchor_url` to `reddit_get_more_comments`. The focused tool defaults to `top`; match `comment_sort` explicitly when comparing it with a thread read.

### Read the coverage fields

**A thread response is a sample, not a complete archive.** Reddit typically renders only an initial window of comments. The server attempts bounded additional loads and reports the remaining gap.

- `total_comment_count`: Reddit's advertised count; may include removed/deleted comments.
- `total_comments_rendered`: unique comments parsed before output pruning.
- `unloaded_comment_count`: `max(0, total_comment_count - total_comments_rendered)`, **an estimate**, not a count of fetchable comments.
- `truncated_comment_count`: loaded branches omitted by output caps, not the unloaded gap.
- `body_truncated`: text was cut by the character cap.
- `more_subtree_cursors`, `more_subtree_cursors_dropped`: remaining reply pointers, with an explicit list cap.
- `load_more_rounds`, `load_more_clicks`: focused expansion actions. `requests_made` is a counter of server-initiated loads/clicks, **not a browser-wide network ledger**; Chrome also requests assets and telemetry.

The listing body preview can be `null`; use `reddit_get_thread` to read the body. For crossposts, `url` / `crosspost_permalink` point to the source Reddit thread while `domain` retains the original media host.

## Browser, login, and configuration

The server creates **its own profile**, by default `~/.reddit-mcp/chrome-profile`. It does not import your normal Chrome profile or copy cookies from another browser. If you choose to log in, that directory holds account session credentials: protect it like a password and never upload it.

**Only one running server can own a profile.** Disconnect the first MCP client before starting another, or give separate sessions different `REDDIT_MCP_PROFILE_DIR` paths. Never delete a lock file or kill another user's browser to force startup.

### Optional login

Close the MCP connection first, then run the visible smoke tool:

```bash
uv run reddit-mcp-smoke --keep-open
```

Log in yourself in the dedicated Chrome window if needed, then press Enter in the terminal to close it. Re-run the smoke check to verify access; a stored login is not a guarantee that a page is accessible. No DevTools cookie copying is required. Login/verification is always manual; the server does not solve CAPTCHAs.

### Keep the window out of the way

Headless mode was blocked in local testing; it is **not a verified default**. To reduce window disruption while retaining headed Chrome, optionally add this to the MCP entry:

```json
"env": {"REDDIT_MCP_WINDOW_POSITION": "-3200,240"}
```

This requests an off-screen window, **not headless operation**. Window managers may reposition it or still focus Chrome. Clear this variable while logging in or troubleshooting.

### Environment variables

Set these in your MCP client's `env` or shell. A `.env` file is **not automatically loaded**.

| Variable | Default | Meaning |
|---|---|---|
| `REDDIT_MCP_PROFILE_DIR` | `~/.reddit-mcp/chrome-profile` | Dedicated browser state directory |
| `REDDIT_MCP_WINDOW_POSITION` | unset | Optional Chrome window coordinates, e.g. `-3200,240` |
| `REDDIT_MCP_HEADLESS` | `false` | Experimental; failed local live access testing |
| `REDDIT_MCP_MIN_INTERVAL` | `2.0` | Minimum pacing interval between initiated actions |
| `REDDIT_MCP_CACHE_TTL` | `120` | Seconds cached HTML remains reusable |
| `REDDIT_MCP_MAX_LOAD_MORE` | `3` | Thread partial-load budget; focused expansion clicks one visible reply control at a time, up to this budget (hard maximum 10 clicks) |
| `REDDIT_MCP_REQUEST_TIMEOUT` | `45.0` | Seconds allowed for a browser action |
| `REDDIT_MCP_MAX_RESPONSE_BYTES` | `5242880` | Maximum HTML accepted per page/partial (5 MiB) |
| `REDDIT_MCP_CACHE_MAX_ENTRIES` | `16` | Maximum cached page/partial entries |

Do not lower pacing to increase collection volume. Subtree expansion mutates the page and is intentionally uncached. Bounded loads can still take several seconds; set your MCP client's tool timeout accordingly (e.g. 120 seconds).

## Limitations and safety

- **No write tools:** no votes, posts, messages, or moderator actions. Browser navigation itself still produces Reddit's ordinary cookies, telemetry, and view activity.
- **No access guarantee:** tested on a small set of public subreddits, logged out, on macOS. Headless and other platforms are not live-verified. DOM, endpoints, ranking, and counts can change.
- **Incomplete comments:** no reliable further top-level comment paging was found in local tests; `limit` on thread pages did not expand that window. Reply expansion is bounded and may still leave replies unavailable.
- **Unknown metadata:** descriptions, members, and activity counts may be absent from the inspected page. Null means unavailable, not zero.
- **Untrusted text:** Reddit posts and comments can contain prompt injection. Treat them as source material, never agent instructions. Avoid cloud transmission of sensitive material without consent.
- **Local only:** no HTTP/SSE hosting or multi-user auth. Do not wrap this server in an unauthenticated network endpoint.
- **No bypass features:** no proxy rotation, credential replay, stealth plugins, or automated CAPTCHA solving. Stop on challenges, 403s, or 429s and review permissions instead of looping.

See [SECURITY.md](SECURITY.md) for profile handling and vulnerability reports.

## Development and verification

See [CONTRIBUTING.md](CONTRIBUTING.md) for development constraints and [CHANGELOG.md](CHANGELOG.md) for release notes.

```bash
uv sync --locked --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

The default tests are **offline**: parser fixtures, scripted browser/session doubles, and tool-level regressions. CI does not contact Reddit or start Chrome. Run the smoke command explicitly for live access; only real parsed posts count as a successful smoke test.

The pre-publication version was also exercised by independent Claude Code sessions: pagination continuity, subtree expansion into previously unseen replies, text formatting, null metadata, validation, and action-counter reconciliation passed on 2026-11-17. These are historical observations, not a promise of current access. A later release smoke check with a **fresh profile reported a challenge and failed**; no retry or bypass was attempted. Fresh-install live access is therefore not verified. No scraped archives or browser profiles are bundled; regression tests use small HTML fixtures.

## License

[MIT](LICENSE). Not affiliated with or endorsed by Reddit. The license applies to this code, not Reddit content, Reddit trademarks, or permission to access Reddit.
