"""MCP smoke check against a running `coord serve`.

Verifies V-11: one HTTP MCP endpoint, tools listed, `status` callable, and
protocol errors surface as tool errors instead of crashes.

Run: uv run python verify/mcp_smoke.py [--url http://127.0.0.1:30303/mcp]
"""

from __future__ import annotations

import argparse
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

EXPECTED = {
    "spawn_worker",
    "send",
    "answer",
    "status",
    "kill",
}


async def main(url: str) -> int:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {tool.name for tool in (await session.list_tools()).tools}
            print(f"tools ({len(tools)}): {', '.join(sorted(tools))}")
            if not EXPECTED <= tools:
                print(f"FAIL missing tools: {sorted(EXPECTED - tools)}")
                return 1

            status = await session.call_tool("status", {})
            text = status.content[0].text if status.content else ""
            print(f"status → {text[:200] or getattr(status, 'structuredContent', None)}")

            result = await session.call_tool(
                "spawn_worker",
                {"owner": "A", "worktree": "/tmp/coord-missing", "task": "{}"},
            )
            if not result.isError:
                print("FAIL spawn_worker accepted a worktree that does not exist")
                return 1
            detail = result.content[0].text if result.content else ""
            print(f"spawn_worker failed as expected: {detail[:140]}")

    print("PASS V-11 (MCP streamable-http, 5 tools, protocol errors returned to the client)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:30303/mcp")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.url)))
