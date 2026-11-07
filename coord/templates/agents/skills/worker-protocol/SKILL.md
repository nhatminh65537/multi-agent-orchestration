---
name: worker-protocol
description: Use during every task — when the TaskPacket does not tell you something you need (decide between assuming and calling ask_orchestrator), and at the end of the task (write the final answer in plain prose; start it with FAILED: if you could not finish).
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

- `id` is `<stream>-<anything>`: `A-07`, `B-42b`, `M-5r` are all valid.
- `files` lists the files you intend to **write**; the manager uses it to stop two workers on the
  same worktree from overwriting each other, and to warn the orchestrator if you touch others.
- `read_only: true` means analysis only: every write tool is blocked by the policy hook.
- If `acceptance` is missing, ask once; if there is still no answer, set your own criteria and state
  them in your final answer.

## 2. Working

1. Read the *Project context* block, then the files in `files`; widen only when needed.
2. Edit surgically (`Edit` / `MultiEdit`), not by rewriting whole files.
3. Verify with `Bash` (tests / lint if the repo has them) and keep the exact result line.
4. Need a decision you cannot make? Call `ask_orchestrator(question)` — one question, ≤ 2 sentences,
   with the options you see and the one you would pick.
5. `NO_ANSWER: proceed with a stated assumption.` → take your pick, write it down, continue.
6. At most 3 question rounds per task; then decide and state the assumption.

## 3. Finishing

Your **final assistant message is the result**. No report tool exists any more.

1. Leave the worktree in the state you are describing: run the acceptance commands yourself.
2. **Do not write any log file.** The Worker Manager writes `.coord/logs/<task-id>.md` from your
   final answer; you are not allowed to write outside your worktree anyway.
3. Answer in plain prose — as long or as short as the work needs:

```
Implemented refresh-token rotation in src/auth/api.py and tests/test_auth.py.

Files changed: src/auth/api.py, tests/test_auth.py
Tests: pytest tests/auth -q -> 41 passed
Assumption: reused the existing `exp` claim instead of adding a new one.
```

4. If you could **not** finish, start the message with the failure line:

```
FAILED: cannot rotate tokens without a migration; DB schema change needs approval.
```

then explain what you tried and what you need. `FAILED:` on the first line is what marks the task
failed — be honest about it instead of reporting success.

## 4. Boundaries (enforced by the harness, not by this prompt)

- Never commit, merge, push, rebase, checkout, reset or clean.
- Never write, edit or delete files outside your worktree.
- Never run commands listed in `TaskPacket.needs_approval`.
- Never call the coordinator MCP tools (they are filtered out of your tool list).

If you think you need one of these, call `ask_orchestrator`.
