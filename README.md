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

Protocol **v0.5**. Design docs live in `docs/` (`1-…` → `6-…`), Vietnamese guide in
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
coord --version               # 0.5.0, protocol v0.5
```

Claude Code plugin (skills + `/coord:*` + MCP registration):

```bash
claude plugin marketplace add /path/to/multi-agent-orchestration
claude plugin install coord@coord
```

## Use it on a project

```bash
cd /path/to/my-project       # git repo with at least one commit
coord init A B C             # writes+commits bootstrap files, then creates ../wt-A ../wt-B ../wt-C
coord up                     # starts the Worker Manager in the background (idempotent)

export COORD_API_KEY=...     # or GEMINI_API_KEY; never commit it
coord doctor                 # SDKs, key, server, worktrees
```

Then, per orchestrator: open a Claude Code session in `../wt-A`, `/rename A`, and send

```
You are orchestrator A. Load the orchestrator skill (section A) now, then continue.
```

`coord down` stops the manager started by `coord up`.

## Backends

One server serves one backend + one model. `COORD_BACKEND` picks it:

| `COORD_BACKEND` | SDK | Worker capabilities | Credentials |
|---|---|---|---|
| `agy-sdk` (**default**) | `google-antigravity` | `BuiltinTools` (read/edit/create/shell/search), `skills_paths`, `policies`, `conversation_id` resume | `COORD_API_KEY` / `GEMINI_API_KEY` / Vertex |
| `claude-sdk` | `claude-agent-sdk` | Claude Code harness: Read/Edit/MultiEdit/Write/Bash/Glob/Grep, project skills + MCP, `can_use_tool`, `resume` | `COORD_API_KEY` + `COORD_BASE_URL` (Anthropic-compatible) |

| Variable | Meaning | Default |
|---|---|---|
| `COORD_BACKEND` | `agy-sdk` \| `claude-sdk` | `agy-sdk` |
| `COORD_API_KEY` | worker credential | — |
| `COORD_BASE_URL` | endpoint override (gateway / OpenAI-compatible) | backend default |
| `COORD_MODEL` | worker model | `gemini-3.8-flash` / `claude-sonnet-4-5` |
| `COORD_HOME` | where `coord.db`, `coord.pid`, `coord.log` live | `~/.local/share/coord` |
| `COORD_DIR` | override `.coord/` | found via git |
| `COORD_ASK_TIMEOUT` | seconds to wait for `answer` | `1800` |

Orchestrator models are chosen in Claude Code — coord never touches them.

## How a task flows

```
orchestrator A ──spawn_worker(TaskPacket)──► worker (full harness in wt-A)
       ▲                                            │
       │  event DONE / ASK / FAILED  ◄──────────────┘
       │  (Native IPC socket push: wakes A at 0 idle tokens)
       └── answer(qid) ──────────────────────────► unblocks ask_orchestrator
```

- Worker id is `<owner>-<hex4>`; `owner` is the session name.
- `send` reuses a warm `IDLE` worker (the SDK session is resumed after a manager restart).
- A worker must call `report_completion` once; otherwise the manager emits `FAILED`.
- The **manager** writes `.coord/logs/<task-id>.md`; workers never write outside their worktree.
- `kill` is not in the allow-list: the owner must approve stopping a worker.

Every tool call goes through `coord/policy.py` (V-9): writes outside the worktree, history-rewriting
git verbs, `needs_approval` commands and coordinator-MCP recursion are denied at the harness layer.

## Files this creates in your project

| Path | Purpose |
|---|---|
| `wt-<name>/` (next to the repo) | one worktree per stream, branch `stream-<name>` |
| `.coord/STATE.md` | stream table, written by the integrator |
| `.coord/logs/<task>.md` | manager-written report + transcript |
| `CLAUDE.md` / `AGENTS.md` | always-on orchestrator / worker rules |
| `.claude/settings.json` | `crossSessionInbound: accept` + allow-list |
| `.mcp.json` | project-local registration of the `coord` MCP server |

`coord init` commits these once ("coord: bootstrap orchestration files") so every worktree inherits
them; re-running is idempotent.

## Deliberately not included

- no `fork_worker` (spawn a new worker with a TaskPacket instead);
- no dependency ledger (`.coord/STATE.md` is the record);
- no cost tracking, log summarization or service manager in the box (a systemd unit is easy to add);
- no enforcement of "≤2 RUNNING workers per owner" (a SHOULD in the protocol).

## Develop

```bash
uv sync --extra agy
uv run python -m unittest discover -s tests -v   # 30 tests, no key needed
uv run python verify/v9_policy.py                # V-9, no key
uv run python verify/v8_restore.py               # V-8, no key
uv run coord up && uv run python verify/mcp_smoke.py
uv run python verify/v12_multi_client.py --repo <project>
```

Layout: `coord/` (package), `skills/` + `commands/` + `.claude-plugin/` (plugin),
`coord/templates/` (copied into projects), `tests/`, `verify/`, `docs/`.
