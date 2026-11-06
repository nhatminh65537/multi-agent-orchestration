"""MCP smoke check against a running `coord serve`.

Verifies V-11: one HTTP MCP endpoint, tools listed, `status` callable, and
protocol errors surface as tool errors instead of crashes.

Run: uv run python verify/mcp_smoke.py [--url http://127.0.0.1:8765/mcp]
"""

from __future__ import annotations

import argparse
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

EXPECTED = {
    "spawn_worker",
    "fork_worker",
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
                print(f"FAIL thiếu tool: {sorted(EXPECTED - tools)}")
                return 1

            status = await session.call_tool("status", {})
            text = status.content[0].text if status.content else ""
            print(f"status → {text[:200] or getattr(status, 'structuredContent', None)}")

            result = await session.call_tool(
                "spawn_worker",
                {"owner": "A", "worktree": "/tmp/coord-missing", "task": "{}"},
            )
            if not result.isError:
                print("FAIL spawn_worker với worktree không tồn tại lại không lỗi")
                return 1
            detail = result.content[0].text if result.content else ""
            print(f"spawn_worker lỗi đúng như mong đợi: {detail[:140]}")

    print("PASS V-11 (MCP streamable-http, 6 tool, lỗi protocol trả về client)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.url)))
