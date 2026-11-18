"""Offline protocol test: the packaged entry point talks MCP without Chrome.

Only tool discovery and invalid arguments are exercised. No Reddit/browser
requests belong in the default test suite.
"""

import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_discovers_tools_and_rejects_invalid_input(tmp_path):
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "reddit_mcp.server"],
        cwd=str(tmp_path),
        env={**os.environ, "REDDIT_MCP_PROFILE_DIR": str(tmp_path / "unused-profile")},
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == {
            "reddit_status",
            "reddit_list_posts",
            "reddit_search",
            "reddit_get_thread",
            "reddit_get_more_comments",
            "reddit_subreddit_info",
        }
        result = await client.call_tool(
            "reddit_list_posts", {"subreddit": "ClaudeCode", "sort": "invalid"}
        )
        assert not result.isError
        data = result.structuredContent or json.loads(result.content[0].text)
        assert data["ok"] is False
        assert "sort" in data["error"]
    # Discovery/validation must never create a browser profile.
    assert not (tmp_path / "unused-profile").exists()
