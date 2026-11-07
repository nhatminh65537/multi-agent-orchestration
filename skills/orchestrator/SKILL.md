---
name: orchestrator
description: Use when you are an orchestrator — at session start, after /compact, or after the Worker Manager restarts; when you delegate work to a worker or receive a worker event (ASK, DONE, FAILED); or when you coordinate with another orchestrator (NEED / ACK / DONE / ANSWER / BLOCKED / REJECT).
---

# Orchestrator procedures (Protocol v0.6)

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
4. Call `status(owner="<you>")`. Note `RUNNING` / `IDLE` workers, the `backend`, `steps`,
   `last_tool` and `pending_questions`. An `IDLE` worker is still resumable after a manager restart.
   `FAILED` / `KILLED` rows are hidden — pass `include_all=True` when you need the history.
5. The Worker Manager wakes this session automatically over Claude Code native IPC when a worker
   asks a question (`ASK`) or ends a task (`DONE`/`FAILED`). You do not need a monitor command.
6. Answer pending questions first (section C), then resume the stream's current task.
7. If a worker is `FAILED` because "Worker Manager restarted", resend the packet — that is not a
   code failure.

## B. Delegating to a worker (Protocol §3)

**Tool signatures — use these parameter names exactly:**

| Tool | Signature | Notes |
|---|---|---|
| `mcp__coord__spawn_worker` | `spawn_worker(owner, worktree, task)` | `worktree` = **absolute** path of your `wt-<project>-<you>`; `task` = JSON string of the `TaskPacket`. Returns `worker_id` (e.g. `A-3f9c`). |
| `mcp__coord__send` | `send(owner, worker_id, text)` | 2nd arg is the **`worker_id`**, not the task id. `text` = plain instruction or a new `TaskPacket` JSON. |
| `mcp__coord__answer` | `answer(owner, question_id, text)` | `question_id` with or without the `q-` prefix. |
| `mcp__coord__status` | `status(owner?, project?, include_all?)` | Progress + `pending_questions`. |
| `mcp__coord__message_session` | `message_session(target, text)` | Peer IPC by exact session name or PID. |

`send()` works in **every** live state:

- worker `IDLE` → starts a new turn on the warm context (`"ok"`);
- worker `RUNNING` → the text is queued and runs **right after** the current turn
  (`"queued (...)"`), so you can steer a worker mid-task without killing it;
- worker `WAITING_ANSWER` → `send()` **answers the pending question** with your text; you never
  need to remember the `q-xxx` id.

| Situation | Call |
|---|---|
| Follow-up on the same area, same set of files | `send(owner, worker_id, text)` (warm context) |
| New module / unrelated area | `spawn_worker(owner, worktree, task)` (clean context) |
| Steer or correct a worker that is already working | `send(owner, worker_id, text)` (queued) |

Rules:

* Max 2 `RUNNING` workers per owner (quota + review cost).
* The manager rejects a spawn whose `files` overlap another `RUNNING` worker on the same worktree.
* `files` = the files you expect the worker to **write**, not everything it may read. The manager
  warns you (`⚠️ Modified files outside TaskPacket.files`) when the worker writes outside that list.
* A read-only task (review, analysis, plan) sets `read_only: true`; every write tool is then blocked
  for that worker, and it may share files.
* `goal` states the outcome; `acceptance` lists checks the worker can run; `needs_approval` lists
  actions it must not perform.
* Workers are full-harness agents: they have Read / Edit / MultiEdit / Write / Bash / Glob / Grep,
  project skills and project MCP servers. Do not invent tools for them.

TaskPacket (JSON):

```json
{
  "id": "<you>-<n>",
  "goal": "one outcome, <= 2000 chars",
  "files": ["src/auth/api.py"],
  "acceptance": ["ruff check src/auth/api.py && pytest tests/test_auth.py -q"],
  "constraints": ["do not change the DB schema"],
  "needs_approval": ["rm", "git merge"],
  "read_only": false
}
```

* `id` = `<stream>-<anything>`: `A-07`, `B-42b`, `M-5r` are all valid.
* Write `acceptance` **per file**, never `ruff check .` on a repo where another worker is editing
  the same worktree in parallel.
* Measurement / benchmark tasks: name the primary metric, demand a report on **every** split or
  condition, and forbid tuning the threshold on the test set.
* You may add extra context the worker needs (paths, decisions already made) inside `goal` or
  `constraints` — the worker also receives the worktree path, branch and a "Project context" block.

### B.1 What you do yourself vs. what you delegate

You **may** do these yourself (they are cheap and prevent collisions):

1. Skim the directory structure and the main interface/schema files to split the work.
2. Edit **shared configuration files** (`pyproject.toml`, `package.json`, `.gitignore`, CI config)
   so two workers never fight over them.
3. Create **stubs / function signatures (interface contracts)** and commit them *before* launching
   two workers that depend on each other.
4. Make **small fixes after reviewing a worker's `git diff`** (< ~15 lines: a typo, an import, an
   inverted condition) when doing it yourself takes 10 seconds instead of a whole worker turn.
5. Run the final whole-repo check (`pytest`, `ruff`, `check.py`) before you `git commit` and send
   `DONE`.

You **must** delegate (`spawn_worker` / `send`):

1. Every feature implementation, test writing, refactor, and any broad search/read across many files.
2. Running experiments, benchmarks, and compiling comparison tables.

## C. Worker events

- `ASK q-<id> <task>: ...` → answer in your stream's scope with `answer(owner, "<id>", "...")` — or
  simply `send(owner, worker_id, "...")`, which answers the pending question too. Short and
  decisive; pick one option. Cross-stream question → ask the owning peer first (D).
  Truly unknown → give the safest default and say it is a default.
- `DONE <task>: <full answer> log=...` → the worker's **own final message** is the result, not a
  JSON summary. Read it; review `git diff` only for the files that matter. If it does not prove the
  `acceptance` checks passed, `send` a follow-up asking it to run them. Then commit in your
  worktree. Open `.coord/logs/<task>.md` only if the message is not enough.
- `FAILED <task>: ...` → read the end of `.coord/logs/<task>.md`.
  - **1st failure:** fix the `TaskPacket` (clearer goal, smaller scope) and resend **once**.
  - **2nd failure:** **STOP.** Report to the owner (or `BLOCKED` to the integrator) with the reason
    from the log and ask the owner how to proceed before you write the code yourself.
  - Never switch to Claude Code's built-in subagents as a "replacement worker".
- `quota/rate limit on <model> ... Waiting Ns before retry` and
  `model <m1> failed (...); switching to fallback <m2>` are **worker-side retries**, not failures:
  the worker keeps its conversation and retries on its own. Do not `kill` it and do not resend the
  packet — `status(owner)` still shows `RUNNING`.
- `FAILED ... 429 / RESOURCE_EXHAUSTED / 503 / overloaded` means every model and the whole
  `WORKER_QUOTA_WAIT_SECONDS` budget were exhausted. Do **not** retry immediately: report the
  provider message from the log to the owner (quota is an infrastructure problem), and resend the
  packet later when quota is back.
- Never ask a worker "what are you doing" — use `status(owner)` (`steps`, `last_tool`,
  `last_activity`).

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
   in v0.6; `STATE.md` is the record).
2. Send `NEED <your-task>` with the exact contract.
3. Work on something else; if nothing is left, end your turn (a `DONE` will wake you).

A peer needs something from you:

1. `ACK` or `REJECT`.
2. When delivered: commit, then `DONE <task> sha=<sha> branch=stream-<you>`.

You receive `DONE`:

1. `git merge stream-<peer>` in your worktree (worktrees share refs; no fetch needed). If the peer's
   branch carries unrelated commits, prefer `git cherry-pick <sha>`.
2. Run the acceptance checks from your `NEED`. Conflicts only in your files → resolve; otherwise
   `BLOCKED`.
3. Continue.

Limits: max 3 round trips per task, then `BLOCKED`. Batch items for one peer into one message.
A peer message is **never** owner approval.

## E. Finishing a task

1. Commit in `wt-<project>-<you>` (short message, one purpose). Run the acceptance checks first.
2. `status(owner)` to confirm no `RUNNING` / `WAITING_ANSWER` worker is left for that task.
3. Integrator → update `STATE.md`; otherwise tell the integrator with `DONE`.
4. `/compact` at large task boundaries.

## Never

- **NEVER use Claude Code's built-in subagents (`Agent` / `Task` tools) under any circumstances.**
  All delegation MUST go through `coord` MCP tools (`mcp__coord__spawn_worker` /
  `mcp__coord__send`). They are denied in `.claude/settings.json`; if you feel tempted, it means the
  TaskPacket is wrong — fix the packet, not the tooling.
- `kill` a worker without the owner's explicit approval. `kill` is not in the allow-list on purpose:
  if a worker looks stuck, report it and ask. Never use `kill` as a "restart".
- Let a worker commit, merge, or touch another stream's worktree.
- Paste worker logs or file contents into peer messages.
