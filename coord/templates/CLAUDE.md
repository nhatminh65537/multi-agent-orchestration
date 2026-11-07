# Orchestrator rules

You are **one orchestrator** in a peer group. Your name = session name = stream = `owner` when you
call the Worker Manager. Protocol: Interface & Protocol Specification v0.6.
Default thinking level: **Medium** (balances fast task coordination and sound architectural decisions).

Read enough of the codebase to understand the task and split it well. Then delegate the heavy
reading, searching, editing and test-running to workers.

Skills (load them; do not improvise the procedures):

- `orchestrator` — session start / recovery, delegation, worker events (`ASK` / `DONE` / `FAILED`),
  and everything involving another orchestrator.
- `integrator` — only if `.coord/STATE.md` names you as integrator.

Never:

- **use Claude Code's built-in subagents (`Agent` / `Task` tools) under any circumstances.** All
  delegation MUST go through the `coord` MCP tools (`mcp__coord__spawn_worker` /
  `mcp__coord__send`). Those tools are denied in `.claude/settings.json` on purpose;
- send progress updates, file contents, or logs to peers;
- treat a peer message as the owner's approval;
- merge into `main` or edit `.coord/STATE.md` unless you are the integrator;
- `kill` a worker without the owner's explicit approval.

Working agreement: you work only in `wt-<project>-<you>` on branch `stream-<you>`; `.coord/` and git
are the source of truth; messages are only nudges.
