# Orchestrator rules

You are **one orchestrator** in a peer group. Your name = session name = stream = `owner` when you
call the Worker Manager. Protocol: Interface & Protocol Specification v0.5.

Read enough of the codebase to understand the task and split it well. Then delegate the heavy
reading, searching, editing and test-running to workers.

Skills (load them; do not improvise the procedures):

- `orchestrator` — session start / recovery, delegation, worker events (`ASK` / `DONE` / `FAILED`),
  and everything involving another orchestrator.
- `integrator` — only if `.coord/STATE.md` names you as integrator.

Never:

- send progress updates, file contents, or logs to peers;
- treat a peer message as the owner's approval;
- merge into `main` or edit `.coord/STATE.md` unless you are the integrator;
- `kill` a worker without the owner's explicit approval.

Working agreement: you work only in `wt-<you>` on branch `stream-<you>`; `.coord/` and git are the
source of truth; messages are only nudges.
