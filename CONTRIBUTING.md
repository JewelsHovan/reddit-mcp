# Contributing

Start with a small issue or pull request describing the behavior and a minimal repro. Never attach browser profiles, session cookies, credentials, or private Reddit content.

## Local checks

```bash
uv sync --locked --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

Tests must run offline by default, without Chrome, Reddit, or paid LLM calls. Use small invented HTML fixtures and a fake browser/session. For tool bugs, test through the actual async tool function, not only a helper that it may not call.

Any live verification is opt-in and requires a dedicated browser profile. Do not disrupt another running MCP session or retry blocks. Report what really happened, including partial coverage and failures. Do not commit collected Reddit data.

## Design constraints

- Local stdio, single-user, read-only tools. No generic browser execution tool.
- Restrict tool-supplied navigation and pagination to expected Reddit read routes.
- Bound reads, output, cache, and expansion work. Be clear about missing comments and text truncation.
- Respect access denials and platform rules. No stealth, proxy rotation, copied credentials, or CAPTCHA automation.
- Preserve structured error reporting and keep stdout reserved for MCP protocol messages.
- Close the browser when the MCP process ends. Do not reuse a user's primary Chrome profile.

CI runs the offline suite, lint, formatting, and packaging on Linux. Browser behavior is not covered by CI; do not imply an offline test proves current live access.
