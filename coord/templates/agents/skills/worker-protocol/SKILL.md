---
name: worker-protocol
description: Use during every task — when the TaskPacket does not tell you something you need (decide between assuming and calling ask_orchestrator), and at the end of the task (call report_completion exactly once with an accurate report).
---

# Worker protocol

## 1. Task packet

```json
{
  "id": "A-07",
  "goal": "one sentence describing the required outcome",
  "files": ["src/auth/api.py"],
  "acceptance": ["POST /auth/refresh returns {token, exp}"],
  "constraints": ["do not change the DB schema"],
  "needs_approval": ["git merge", "rm"],
  "read_only": false
}
```

- `id` is `<stream>-<n>`.
- `files` lists the files you intend to **write**; the manager uses it to stop two workers on the
  same worktree from overwriting each other.
- `read_only: true` means analysis only: do not modify files, just report.
- If `acceptance` is missing, ask once; if there is still no answer, set your own criteria and record
  them in `assumptions`.

## 2. Working

1. Read the *Project context* block, then the files in `files`; widen only when needed.
2. Edit surgically (`Edit` / `MultiEdit`), not by rewriting whole files.
3. Verify with `Bash` (tests / lint if the repo has them) and keep the exact result line.
4. Need a decision you cannot make? Call `ask_orchestrator(task_id, question)` — one question,
   ≤ 2 sentences, with the options you see and the one you would pick.
5. `NO_ANSWER: proceed with a stated assumption.` → take your pick, record it in `assumptions`,
   continue.
6. At most 3 question rounds per task; then decide and record the assumption.

## 3. Finishing

1. Leave the worktree in the state you are reporting: run the acceptance commands yourself.
2. **Do not write any log file.** The Worker Manager writes `.coord/logs/<task-id>.md` from your
   transcript and report; you are not allowed to write outside your worktree anyway.
3. Call `report_completion` exactly once:

```json
{
  "id": "A-07",
  "status": "DONE",
  "summary": "<= 600 chars: outcome first, no file contents, no code blocks",
  "assumptions": ["..."],
  "files_changed": ["src/auth/api.py"],
  "diff_stat": "2 files changed, +48 -6",
  "tests": "pytest tests/auth: 41 passed"
}
```

- `status` is `DONE` or `FAILED`; `FAILED` is a valid result — say what blocked you.
- If `report_completion` returns a validation error, fix the fields and call it again.

## 4. Boundaries (enforced by the harness, not by this prompt)

- Never commit, merge, push, rebase, checkout, reset or clean.
- Never write, edit or delete files outside your worktree.
- Never run commands listed in `TaskPacket.needs_approval`.
- Never call the coordinator MCP tools (they are filtered out of your tool list).

If you think you need one of these, call `ask_orchestrator`.
