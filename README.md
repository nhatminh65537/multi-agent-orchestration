# coord

Peer-group orchestration for Claude Code: **N orchestrator sessions**, one shared
**Worker Manager**, and **full-harness SDK workers** (Claude Agent SDK or Google Antigravity)
with their own provider, gateway and quota.

This repo is the *mechanism*, not a project. Install it once, then `coord init` any repo.

```
Claude Code plugin  ──►  /coord:* commands, skills (orchestrator, integrator),
                         MCP registration for `coord`
coord (Python CLI)  ──►  init | serve | watch | status | doctor | deps
coord serve         ──►  MCP streamable-http @ 127.0.0.1:8765
                         (spawn/send/fork/answer/status/kill)
                         backend: claude-sdk | agy-sdk | openai-loop
```

Protocol: Interface & Protocol Specification **v0.4**
([doc 3](docs/3-interface-protocol-specification.md)).
Design docs: [1](docs/1-software-architecture-document.md) ·
[2](docs/2-architecture-decision-records.md) ·
[3](docs/3-interface-protocol-specification.md) ·
[4](docs/4-operations-runbook.md) ·
[5](docs/5-roadmap-verification-risks.md) ·
[6](docs/6-skills-catalog.md) · [plan](docs/plan/refactor-plan.md).
Hướng dẫn tiếng Việt: **[HUONG-DAN.md](docs/HUONG-DAN.md)**.

## Install

```bash
uv tool install '.[claude]'     # default backend (claude-agent-sdk)
# or: uv tool install '.[agy]'  # Gemini / Vertex / OpenAI-compatible
# or: uv tool install '.[both]'
coord --version                 # 0.4.0, protocol v0.4
```

Claude Code plugin (adds skills + `/coord:*` + the coord MCP server):

```bash
claude plugin marketplace add /path/to/multi-agent-orchestration
claude plugin install coord@coord
```

## Use it on a project

```bash
cd /path/to/my-project          # must be a git repo with at least one commit
coord init A B C                # writes files, commits them, then creates ../wt-A ../wt-B ../wt-C
coord serve                     # one manager for the whole machine (or use systemd, see below)
```

`coord init` order matters: project files (`CLAUDE.md`, `AGENTS.md`, `.claude/settings.json`,
`.mcp.json`, `.gitignore`) are written **and committed** on the current branch first, so every
worktree inherits them. Use `--no-commit` to skip that (worktrees then get a copy instead).
Re-running `init` never creates a second commit.

Then, per orchestrator: open a Claude Code session in `../wt-A`, `/rename A`, and send

```
You are orchestrator A. Load the orchestrator skill (section A) now, then continue.
```

## Model, backend & credentials

The **orchestrator** model is chosen in Claude Code — coord never touches it. The **worker**
model is chosen by the backend, configured with environment variables read by `coord serve`.
One server serves one backend + one model; run a second server (`--port 8766`) if you need two.

| `COORD_BACKEND` | SDK | What the worker gets | Credentials |
|---|---|---|---|
| `claude-sdk` (default) | `claude-agent-sdk` | Claude Code harness: Read/Edit/MultiEdit/Write/Bash/Glob/Grep, project skills, project MCP, `can_use_tool` policy, `resume`/`fork_session` | `COORD_API_KEY` + `COORD_BASE_URL` (Anthropic Messages compatible) |
| `agy-sdk` | `google-antigravity` | `BuiltinTools`, `skills_paths`, `policies`, `conversation_id` resume | `COORD_API_KEY` / `GEMINI_API_KEY` / Vertex |
| `openai-loop` | `openai` | **deprecated** 6-tool chat loop, for tests/offline only | `COORD_API_KEY` |

```bash
export COORD_BACKEND=claude-sdk
 export COORD_API_KEY=...            # never commit this
export COORD_BASE_URL=...           # any Anthropic-compatible gateway (LiteLLM, Bifrost, OpenRouter)
export COORD_MODEL=claude-sonnet-4-5
coord serve
```

`coord doctor` reports which SDKs are installed, which backend is selected, and whether a key is
set. `coord status` lists workers (state, task, parent, backend, pending questions).
`coord watch --to A` prints A's event stream (that is what the Monitor tool runs).

## Claude Desktop (Code tab): one coordinator

Requirements: Claude Code ≥ 2.1.236 (for `notify_when_idle`), and **everything on the same
machine** — cross-session messages do not cross containers, WSL↔Windows, or remote hosts.

1. Terminal: `coord serve` (leave it running). Check `curl -s http://127.0.0.1:8765/health` → `ok`.
2. MCP is registered either by the plugin (`claude plugin install coord@coord`) or by the project's
   `.mcp.json` that `coord init` wrote — both work with Desktop.
3. Desktop → **Code** tab → open the **worktree** folder `../wt-A` (not the main repo).
4. `/rename A` — this name is the stream name and the `owner` passed to every tool.
5. `/mcp` must list `coord` with 6 tools; `/status` must show a `Peer address` (if `unavailable`,
   the session is not in a worktree with `.claude/settings.json`, or `crossSessionInbound` is not
   `accept`).
6. Send the bootstrap prompt (above). Section A of the skill starts the Monitor with
   `coord watch --to A`.
7. For peers: second Code tab on `../wt-B`, `/rename B`. Both sessions now see each other in
   `/list-agents`; A can message B directly.

## P0 verification (doc 5)

| Check | Automation | Needs |
|---|---|---|
| V-5 endpoint + model | `verify/p0.py` | API key |
| V-6 tool calling + report schema | `verify/p0.py` | API key |
| V-13 fork by history replay | `verify/p0.py` | API key |
| V-7 tool blocks for a long time | `verify/v7_block.py --seconds 60` (`1800` for the full check) | API key |
| V-10 N workers concurrently | `verify/v10_concurrency.py --workers 6` | API key |
| V-11 MCP over HTTP | `verify/mcp_smoke.py` | server running |
| V-12 N sessions on one server | `verify/v12_multi_client.py --clients 3 --repo <project>` | server running |
| V-1…V-4, V-14, V-15 | manual: `/list-agents`, PING A↔B, Monitor wake-up, skill load, second OS | — |
| V-8 (restore after restart) | `verify/v8_restore.py` (no key) | — |
| V-9 (policy hook) | `verify/v9_policy.py` (no key) | — |


## How a task flows

```
orchestrator A ──spawn_worker(TaskPacket)──► worker (full harness in wt-A)
       ▲                                            │
       │  event DONE / ASK / FAILED  ◄──────────────┘
       │  (coord watch --to A  → Monitor)
       └── answer(qid) ──────────────────────────► unblocks ask_orchestrator

A ──NEED/DONE (cross-session message)──► B          (no code involved)
```

Rules that matter:

- worker id is `<owner>-<hex4>`; `owner` is the session name.
- `send` reuses a warm `IDLE` worker; `fork_worker` clones its conversation (same worktree).
- worker must call `report_completion` once; otherwise the manager emits `FAILED`.
- logs: `<repo>/.coord/logs/<task-id>.md`, state: `~/.local/share/coord/coord.db`.

## Files this creates in your project

| Path | Purpose |
|------|---------|
| `wt-<name>/` (next to the repo) | one worktree per stream, branch `stream-<name>` |
| `.coord/STATE.md` | stream table, written by the integrator |
| `.coord/logs/<task>.md` | full worker transcript + report |
| `.coord/deps.jsonl` | append-only dependency ledger (`wait` / `resolve`) |
| `CLAUDE.md` | marked block: orchestrator rules |
| `AGENTS.md` | marked block: worker rules |
| `.claude/settings.json` | `crossSessionInbound: accept` + allow-list for `mcp__coord__*` |
| `.mcp.json` | project-local registration of the `coord` MCP server (also used by Desktop) |

All of these are committed by `init` (one "coord: bootstrap orchestration files" commit) so each
worktree starts with them. `/coord:init` is idempotent: marked blocks are refreshed, the rest of each
file is untouched, and a second run creates no extra commit.

## Design notes / deviations from the runbook

1. **One server per machine, project-aware.** State lives in `COORD_HOME`
   (`~/.local/share/coord/coord.db`) so a single process serves every project; tools take an
   absolute `worktree`, and `.coord/logs/` stays project-local. Set `COORD_HOME` to relocate.
2. **Pluggable full-harness backends (ADR-014).** `claude-sdk` is the default; `agy-sdk` is a peer
   driver. `openai-loop` survives only as a deprecated test/offline fallback. All three share
   `coord/policy.py`, `coord/models.py` and the same `WorkerReport`.
3. **Policy hook is mandatory (ADR-015).** Writes outside the worktree, history-rewriting git
   verbs and `needs_approval` commands are denied at the harness layer, not in the prompt.
4. **The Manager writes logs (ADR-016).** Workers never write `.coord/logs/`; that would be outside
   their worktree.
5. **Events are durable and fanned out (ADR-017).** Two Monitors on one owner both see every
   event; undelivered events are replayed on reconnect.
6. **MCP registration ships both ways**: the plugin (user scope) and a project `.mcp.json` written
   by `init`. **Skills live in the plugin**; only `repo-conventions` belongs to the project.

## What this deliberately does not do

- no service manager in the box (a systemd unit is below), no cost tracking, no log summarization;
- no enforcement of "≤2 RUNNING workers per owner" (it is a SHOULD in the protocol).

## Optional: systemd user unit

```ini
# ~/.config/systemd/user/coord.service
[Unit]
Description=coord Worker Manager

[Service]
ExecStart=%h/.local/bin/coord serve
EnvironmentFile=%h/.config/coord.env
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
install -m 600 /dev/null ~/.config/coord.env   # COORD_API_KEY=...
systemctl --user enable --now coord
```

## Develop

```bash
uv sync
uv run python -m unittest discover -s tests -v
uv run python verify/p0.py              # V-5/V-6/V-13, needs an API key (SKIPs otherwise)
uv run python verify/v7_block.py        # V-7, needs an API key
uv run python verify/v8_restore.py      # V-8, no key needed
uv run python verify/v9_policy.py       # V-9, no key needed
uv run python verify/v10_concurrency.py # V-10, needs an API key
uv run coord serve                      # then, in another shell:
uv run python verify/mcp_smoke.py       # V-11
uv run python verify/v12_multi_client.py --repo <project>   # V-12
```

`mcp` is pinned `<2` on purpose: 2.x renamed `FastMCP` to `MCPServer`, while the protocol doc and
runbook target the 1.x API.

Layout: `coord/` (package), `skills/` + `commands/` + `.claude-plugin/` (plugin),
`coord/templates/` (copied into projects), `tests/`, `verify/`.
