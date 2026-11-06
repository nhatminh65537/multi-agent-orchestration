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
cd /path/to/my-project
coord init A B C          # worktrees ../wt-A ../wt-B ../wt-C, .coord/, CLAUDE.md, AGENTS.md
coord serve               # one manager for the whole machine (or use systemd, see below)
```

Then, per orchestrator: open a Claude Code session in `../wt-A`, `/rename A`, and send

```
You are orchestrator A. Load the orchestrator skill (section A) now, then continue.
```

Workers need the endpoint configured for the **server** process:

```bash
export COORD_API_KEY=...            # or OPENAI_API_KEY
export COORD_BASE_URL=...           # default: Gemini OpenAI-compatible endpoint
export COORD_MODEL=gemini-3.8-flash # optional
coord serve
```

`coord doctor` tells you what is missing. `coord status` lists workers.
`coord watch --to A` prints A's event stream (that is what the Monitor tool runs).

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

`/coord:init` is idempotent: the marked blocks are refreshed, the rest of each file is untouched.

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
4. **Skills live in the plugin**; only `repo-conventions` belongs to the project
   (`.agents/skills/repo-conventions/SKILL.md`, template in `skills/repo-conventions/`).

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
uv run python verify/p0.py          # needs an API key; SKIPs otherwise
uv run python -m coord.cli serve    # or: uv run coord serve
```

Layout: `coord/` (package), `skills/` + `commands/` + `.claude-plugin/` (plugin),
`coord/templates/` (copied into projects), `tests/`, `verify/`.
