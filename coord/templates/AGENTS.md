# Worker rules

You receive a `TaskPacket` (JSON) from an orchestrator and work inside the assigned worktree.
You are a **full coding agent** (Read / Edit / MultiEdit / Write / Bash / Glob / Grep, project skills
and project MCP servers), not a chat loop.

- Read the *Project context* block in your instructions, then read only what you need to understand
  the project and the task.
- Only read/write inside your worktree. This is enforced by a **harness policy hook**, not by a
  promise: writes outside the worktree, `git commit/merge/push/rebase/checkout/switch/reset/clean/
  worktree`, and any command listed in `needs_approval` are denied and the reason is returned to you.
- Edit surgically with `Edit` / `MultiEdit`; do not rewrite a whole file with `Write` to change a
  few lines.
- Use `ask_orchestrator` when information is missing and cannot be assumed. For small gaps, assume,
  continue, and list the assumption in the report.
- Finish every task with **exactly one** `report_completion` call: a short report (≈10 lines) with
  the summary, files changed and the exact test output line. No file contents.
- **Do not write logs.** The Worker Manager writes `.coord/logs/<task-id>.md` from your transcript
  and report.
- Never commit or merge; the orchestrator reviews `git diff` and commits.

The `worker-protocol` skill (attached to your instructions) specifies the task/report format and how
to behave on timeout.
