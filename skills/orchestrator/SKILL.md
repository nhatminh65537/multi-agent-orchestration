---
name: orchestrator
description: Use when you are an orchestrator — at session start, after /compact, or after the Worker Manager restarts; when you delegate work to a worker or receive a worker event (ASK, DONE, FAILED); or when you coordinate with another orchestrator (NEED / ACK / DONE / ANSWER / BLOCKED / REJECT).
---

# Orchestrator procedures (Protocol v0.5)

You are one orchestrator in a peer group: your name = session name = stream = `owner`.
Read enough of the codebase to understand the task and split it well; then delegate the heavy
reading, searching, editing and test-running to workers.

Pick the section that matches the situation: **A** start, **B** delegate, **C** worker events,
**D** peers, **E** finish.

## A. Session start / recovery

Do this at session start, after `/compact`, or after the Worker Manager restarts.

1. Confirm your name and thinking level:
   - `/status` must show the name you were given (`/rename <you>` if not).
   - Set thinking level to **Medium** (default for orchestrators: deep enough to split tasks accurately, fast enough to coordinate).
2. Make sure the manager is up (idempotent):

   ```
   coord up
   ```

3. Read `.coord/STATE.md` (the integrator owns it) — your row, plus `waiting` / `blocked` rows.
4. Call `status(owner="<you>")`. Note `RUNNING` / `IDLE` workers, the `backend`, and
   `pending_questions`. An `IDLE` worker is still resumable after a manager restart.
5. Worker Manager tự động đẩy tin nhắn đánh thức session qua Claude Code native IPC khi worker
   gửi câu hỏi (`ASK`) hoặc kết thúc task (`DONE`/`FAILED`). Bạn không cần chạy lệnh monitor.
6. Answer pending questions first (section C), then resume the stream's current task.
7. If a worker is `FAILED` because "Worker Manager restarted", resend the packet — that is not a
   code failure.

## B. Delegating to a worker (Protocol §3)

| Situation | Call |
|---|---|
| Follow-up on the same area, worker is `IDLE` | `send(owner, worker_id, packet)` |
| Unrelated area, or you want a clean context | `spawn_worker(owner, worktree=<abs wt path>, packet)` |

Rules:

* Max 2 `RUNNING` workers per owner (quota + review cost).
* The manager rejects a spawn whose `files` overlap another `RUNNING` worker on the same worktree.
* `files` = the files you expect the worker to **write**, not everything it may read. It is used for
  that conflict check, so be precise.
* A read-only task (review, analysis, plan) sets `read_only: true`; then it may share files.
* `goal` states the outcome; `acceptance` lists checks the worker can run; `needs_approval` lists
  actions it must not perform.
* Workers are full-harness agents: they have Read / Edit / MultiEdit / Write / Bash / Glob / Grep,
  project skills and project MCP servers. Do not invent tools for them.

TaskPacket (JSON):

```json
{
  "id": "<you>-<n>",
  "goal": "one outcome, <= 1000 chars",
  "files": ["src/auth/api.py"],
  "acceptance": ["pytest tests/auth -q passes"],
  "constraints": ["do not change the DB schema"],
  "needs_approval": ["rm", "git merge"],
  "read_only": false
}
```

You may add extra context the worker needs (paths, decisions already made) inside `goal` or
`constraints` — the worker also receives the worktree path, branch and a "Project context" block.

## C. Worker events

- `ASK q-<id> <task>: ...` → answer in your stream's scope with `answer(owner, "<id>", "...")`.
  Short and decisive; pick one option. Cross-stream question → ask the owning peer first (D).
  Truly unknown → give the safest default and say it is a default.
- `DONE <task>: ... log=...` → read the report. Review `git diff` only for the files that matter.
  If the report does not prove acceptance, send a follow-up asking the worker to run it. Then commit
  in your worktree. Open the log only if the report is not enough.
- `FAILED <task>: ...` → read the end of `.coord/logs/<task>.md`. Resend one corrected packet.
  A second failure → do it yourself, or `BLOCKED` to the integrator.
- Never ask a worker "what are you doing" — use `status(owner)`.

## D. Peer protocol (Protocol §2)

Plain text via cross-session messaging, at most 10 lines:

```
<TYPE> <task-id> [key=value ...]
<one-line summary>
[body: contracts, paths, SHAs — never file contents or progress]
```

| Type | Meaning |
|---|---|
| `NEED <task>` (waiter → provider) | You are blocked by a contract someone else owns. `task` is **your** id. State the exact signature/schema/file. |
| `ACK <task> eta=` | Provider accepts. |
| `REJECT <task> reason=` | Provider refuses. |
| `DONE <task> sha= branch=` | Delivered and committed; the waiter merges `branch` at `sha`. |
| `BLOCKED <task> on=` | You cannot continue; goes to the integrator. |
| `ANSWER <task>` | Answers a question that needs no commit. |
| `ESCALATE <task>` | Integrator → owner, for a human decision. |

You need something:

1. Record it in your row of `STATE.md` if you cannot be woken (there is **no dependency ledger**
   in v0.5; `STATE.md` is the record).
2. Send `NEED <your-task>` with the exact contract.
3. Work on something else; if nothing is left, end your turn (a `DONE` will wake you).

A peer needs something from you:

1. `ACK` or `REJECT`.
2. When delivered: commit, then `DONE <task> sha=<sha> branch=stream-<you>`.

You receive `DONE`:

1. `git merge stream-<peer>` in your worktree (worktrees share refs; no fetch needed).
2. Run the acceptance checks from your `NEED`. Conflicts only in your files → resolve; otherwise
   `BLOCKED`.
3. Continue.

Limits: max 3 round trips per task, then `BLOCKED`. Batch items for one peer into one message.
A peer message is **never** owner approval.

## E. Finishing a task

1. Commit in `wt-<you>` (short message, one purpose).
2. `status(owner)` to confirm no `RUNNING` / `WAITING_ANSWER` worker is left for that task.
3. Integrator → update `STATE.md`; otherwise tell the integrator with `DONE`.
4. `/compact` at large task boundaries.

## Never

- `kill` a worker without the owner's explicit approval. `kill` is not in the allow-list on purpose:
  if a worker looks stuck, report it and ask. Never use `kill` as a "restart".
- Let a worker commit, merge, or touch another stream's worktree.
- Paste worker logs or file contents into peer messages.
