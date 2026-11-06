"""V-12: N sessions share one MCP server, and inbound cross-session messages are accepted.

Needs a running `coord serve` (no API key required).

Run: uv run python verify/v12_multi_client.py [--clients 3] [--repo .]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def _probe(url: str, index: int) -> tuple[int, bool]:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            result = await session.call_tool("status", {})
            return len(tools), (result.isError or False)


async def main(url: str, clients: int, repo: Path) -> int:
    outcomes = await asyncio.gather(
        *[_probe(url, i) for i in range(clients)], return_exceptions=True
    )

    ok = True
    for index, outcome in enumerate(outcomes):
        if isinstance(outcome, BaseException):
            print(f"client {index}: FAIL {type(outcome).__name__}: {outcome}")
            ok = False
        else:
            tools, is_error = outcome
            print(f"client {index}: tools={tools} status_error={is_error}")
            ok = ok and tools == 6 and not is_error

    settings_path = repo / ".claude" / "settings.json"
    accept = False
    if settings_path.is_file():
        try:
            accept = json.loads(settings_path.read_text()).get("crossSessionInbound") == "accept"
        except json.JSONDecodeError:
            accept = False
    print(f"crossSessionInbound=accept trong {settings_path}: {'có' if accept else 'KHÔNG'}")
    if not accept:
        ok = False

    print("PASS V-12 (N client song song + crossSessionInbound)" if ok else "FAIL V-12")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:30303/mcp")
    parser.add_argument("--clients", type=int, default=3)
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.url, args.clients, Path(args.repo).resolve())))
