# coord

Peer-group orchestration for Claude Code: **N orchestrator sessions**, one shared **Worker
Manager**, and **full-harness SDK workers** with their own provider, gateway and quota.

This repo is the *mechanism*, not a project. Install it once, then `coord init` any repo.

```
Claude Code plugin  ──►  /coord:* commands, skills (orchestrator, integrator),
                         MCP registration for `coord`
coord (Python CLI)  ──►  init | up | down | serve | msg | status | doctor
coord serve         ──►  MCP streamable-http @ 127.0.0.1:30303
                         spawn_worker · send · answer · status · kill
```

Protocol **v0.6**. Design docs live in `docs/` (`1-…` → `6-…`), Vietnamese guide in
[`docs/HUONG-DAN.md`](docs/HUONG-DAN.md).

## Requirements

- Claude Code ≥ 2.1.224 for cross-session messaging (≥ 2.1.236 for `notify_when_idle`).
- Python 3.10+, `git` 2.31+, `uv`.
- Everything runs on **one machine and one environment** (no WSL↔Windows, no container↔host).

## Install

```bash
uv tool install '.[agy]'      # default backend (google-antigravity / Gemini)
# uv tool install '.[claude]' # optional: claude-agent-sdk
# uv tool install '.[both]'
coord --version               # 0.6.0, protocol v0.6
```

Claude Code plugin (skills + `/coord:*` + MCP registration):

```bash
claude plugin marketplace add /path/to/multi-agent-orchestration
claude plugin install coord@coord
```

## Use it on a project

```bash
cd /path/to/my-project       # git repo with at least one commit
coord init A B C             # writes+commits bootstrap files, then creates ../wt-my-project-A|B|C
coord up                     # starts the Worker Manager in the background (idempotent)

export WORKER_API_KEY=...    # never commit it; or put it in .env
coord doctor                 # SDKs, key, server, worktrees
```

Then, per orchestrator: open a Claude Code session in `../wt-my-project-A`, `/rename A`, and send

```
You are orchestrator A. Load the orchestrator skill (section A) now, then continue.
```

`coord down` stops the manager started by `coord up`.

## Backends

One server serves one backend + one model. `WORKER_BACKEND` picks it:

| `WORKER_BACKEND` | SDK | Worker capabilities | Credentials |
|---|---|---|---|
| `agy-sdk` (**default**) | `google-antigravity` | `BuiltinTools` (read/edit/create/shell/search), `skills_paths`, `policies`, `conversation_id` resume | `WORKER_API_KEY` (+ optional `WORKER_BASE_URL`) |
| `claude-sdk` | `claude-agent-sdk` | Claude Code harness: Read/Edit/MultiEdit/Write/Bash/Glob/Grep, project skills + MCP, `can_use_tool`, `resume` | `WORKER_API_KEY` + `WORKER_BASE_URL` (Anthropic-compatible) |

Exactly seven `WORKER_*` variables (plus the internal `COORD_HOME` / `COORD_DIR`):

| Variable | Meaning | Default |
|---|---|---|
| `WORKER_BACKEND` | `agy-sdk` \| `claude-sdk` | `agy-sdk` |
| `WORKER_MODEL` | worker model | `gemini-3.8-flash-medium` / `claude-sonnet-4-5` |
| `WORKER_FALLBACK_MODEL` | comma-separated models tried when the primary errors (429/5xx/model) | — |
| `WORKER_QUOTA_WAIT_SECONDS` | how long a quota-limited worker waits for the API to recover before failing | `900` (`0` disables waiting) |
| `WORKER_THINKING` | `low` \| `medium` \| `high` | `medium` |
| `WORKER_BASE_URL` | endpoint override (gateway / proxy) | backend default |
| `WORKER_API_KEY` | worker credential | — |
| `COORD_HOME` | where `coord.db`, `coord.pid`, `coord.log` live | `~/.local/share/coord` |
| `COORD_DIR` | override `.coord/` | found via git |

Orchestrator models are chosen in Claude Code — coord never touches them.

## How a task flows

```
orchestrator A ──spawn_worker(TaskPacket)──► worker (full harness in wt-my-project-A)
       ▲                                            │
       │  event DONE / ASK / FAILED  ◄──────────────┘
       │  (Native IPC socket push: wakes A at 0 idle tokens)
       └── answer(qid) / send(worker_id) ─────────► unblocks ask_orchestrator
```

- Worker id is `<owner>-<hex4>`; `owner` is the session name. `send()` takes the **worker id**.
- `send` reuses a warm `IDLE` worker (the SDK session is resumed after a manager restart). While a
  worker is `RUNNING`, `send` queues the text and it runs right after the current turn; while it is
  `WAITING_ANSWER`, `send` answers the pending question.
- **A worker's final message is the result.** There is no `report_completion` tool: the manager
  takes the agent's last response as the outcome, marks the worker `IDLE`, and writes
  `.coord/logs/<task-id>.md`. A response starting with `FAILED:` marks the task failed.
- **Provider errors are reported, not swallowed.** A 429/5xx/overload is classified, retried on
  `WORKER_FALLBACK_MODEL`, then waited out for `WORKER_QUOTA_WAIT_SECONDS` (each wait is pushed to
  the orchestrator). If it never recovers, the worker is marked `FAILED` with the original provider
  message in the event, the IPC push and `.coord/logs/<task-id>.md`.
- **The manager** writes `.coord/logs/<task-id>.md`; workers never write outside their worktree.
- `kill` is not in the allow-list: the owner must approve stopping a worker.

Every tool call goes through `coord/policy.py` (V-9): writes and reads outside the worktree,
`read_only` violations, history-rewriting git verbs, `needs_approval` commands and coordinator-MCP
recursion are denied at the harness layer.

## Files this creates in your project

| Path | Purpose |
|---|---|
| `wt-<project>-<name>/` (next to the repo) | one worktree per stream, branch `stream-<name>` |
| `.coord/STATE.md` | stream table, written by the integrator |
| `.coord/logs/<task>.md` | manager-written result + touched files |
| `CLAUDE.md` / `AGENTS.md` | always-on orchestrator / worker rules |
| `.claude/settings.json` | `crossSessionInbound: accept` + allow-list + `deny: [Agent, Task]` |
| `.mcp.json` | project-local registration of the `coord` MCP server |

`coord init` commits these once ("coord: bootstrap orchestration files") so every worktree inherits
them. Re-running it is an **update**, not an append: the marked blocks are replaced, the worker
skill is rewritten, `.mcp.json`/settings are repaired, and all five bootstrap files are re-synced
into every existing `wt-<project>-<name>` worktree.

## Deliberately not included

- no `fork_worker` (spawn a new worker with a TaskPacket instead);
- no dependency ledger (`.coord/STATE.md` is the record);
- no cost tracking, log summarization or service manager in the box (a systemd unit is easy to add);
- no enforcement of "≤2 RUNNING workers per owner" (a SHOULD in the protocol).

## Develop

```bash
uv sync --extra agy
uv run python -m unittest discover -s tests -v   # no key needed
uv run python verify/v9_policy.py                # V-9, no key
uv run python verify/v8_restore.py               # V-8, no key
uv run coord up && uv run python verify/mcp_smoke.py
uv run python verify/v12_multi_client.py --repo <project>
```

Layout: `coord/` (package), `skills/` + `commands/` + `.claude-plugin/` (plugin),
`coord/templates/` (copied into projects), `tests/`, `verify/`, `docs/`.
