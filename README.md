# coord

Peer-group orchestration for Claude Code: **N orchestrator sessions**, one shared
**Worker Manager**, Gemini workers driven through an **OpenAI-compatible** endpoint.

This repo is the *mechanism*, not a project. Install it once, then `coord init` any repo.

```
Claude Code plugin  ──►  /coord:* commands, skills (orchestrator, integrator),
                         MCP registration for `coord`
coord (Python CLI)  ──►  init | serve | watch | status | doctor
coord serve         ──►  MCP streamable-http @ 127.0.0.1:8765 (spawn/send/fork/answer/status/kill)
```

Protocol: Interface & Protocol Specification **v0.3**
([doc 3](https://notes.home.rougitsune.top/doc/3-interface-protocol-specification-0zspVJmdTj)).
Design docs: [Multi-Agent Orchestration](https://notes.home.rougitsune.top/doc/multi-agent-orchestration-8QH6VOye9y).

## Install

```bash
uv tool install .            # puts `coord` on PATH, managed venv, reproducible
coord --version
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

## Model & credentials

Workers are driven by an OpenAI-compatible endpoint. Configuration is **environment only**, read by
`coord serve` at startup — there is no config file, and **one server serves one model**:

```bash
export COORD_API_KEY=...            # or OPENAI_API_KEY   (never commit this)
export COORD_BASE_URL=...           # default: Gemini OpenAI-compatible endpoint
export COORD_MODEL=gemini-3.8-flash # default; `coord serve --model ...` overrides it
coord serve
```

Everything the server spawns inherits that model. A per-owner or per-task model is not implemented.
Orchestrator models (Opus, etc.) are chosen in Claude Code itself — coord never touches them.

`coord doctor` tells you what is missing. `coord status` lists workers.
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
| V-8 (restore after restart) | not implemented (P4) | — |
| V-9 (policy hook) | dropped by design | — |


## How a task flows

```
orchestrator A ──spawn_worker(TaskPacket)──► worker (read/write/shell in wt-A)
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

1. **One server per machine, project-aware.** The runbook puts `wm.db` in `.coord/` and runs
   `uv run --project .coord/wm`. Here state lives in `COORD_HOME` (`~/.local/share/coord/coord.db`)
   so a single process serves every project; tools take an absolute `worktree`, and
   `.coord/logs/` stays project-local. Set `COORD_HOME` to relocate.
2. **OpenAI-compatible backend instead of the Antigravity SDK.** Workers are a chat loop with six
   tools; `fork_worker` replays the stored message history. `verify/p0.py` re-checks V-5/V-6/V-13.
3. **No policy hook (V-9 dropped).** There is no sandbox: the worker does what the TaskPacket says.
   Guardrails are conventions in `AGENTS.md` plus orchestrator review of `git diff`. This is a
   deliberate MVP scope decision — record it as an ADR before relying on isolation.
4. **MCP registration ships both ways**: the plugin (user scope, all projects) and a project
   `.mcp.json` written by `init`. **Skills live in the plugin**; only `repo-conventions` belongs to
   the project (`.agents/skills/repo-conventions/SKILL.md`, template in `skills/repo-conventions/`).

## What this deliberately does not do

- no service manager, no restart recovery (P4) — run `coord serve` by hand or write a unit;
- no cost tracking, no log summarization, no retry/backoff shaping;
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
uv run python verify/v10_concurrency.py # V-10, needs an API key
uv run coord serve                      # then, in another shell:
uv run python verify/mcp_smoke.py       # V-11
uv run python verify/v12_multi_client.py --repo <project>   # V-12
```

`mcp` is pinned `<2` on purpose: 2.x renamed `FastMCP` to `MCPServer`, while the protocol doc and
runbook target the 1.x API.

Layout: `coord/` (package), `skills/` + `commands/` + `.claude-plugin/` (plugin),
`coord/templates/` (copied into projects), `tests/`, `verify/`.
