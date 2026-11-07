---
description: Show your workers (or all workers) and the questions waiting for an answer
argument-hint: [--owner A] [--project /path/to/repo] [--all]
allowed-tools: Bash(coord:*)
---

Run `coord status $ARGUMENTS`.

How to read the result:

- `RUNNING` — working; do not `send` anything else and do not `kill` it. A quota retry also shows as
  `RUNNING` (the worker reports "Waiting Ns before retry" in its event stream).
- `IDLE` — a warm worker; `send` a new task to keep its context.
- `WAITING_ANSWER` — there is a question in the `asks` column; answer it with `answer`, or with
  `send` (which answers the pending question), or escalate to the owner if it is stale.
- `FAILED` — read the `log=` path of the last `FAILED` event; decide between `send` (fix and retry)
  and `spawn_worker` (fresh context). A `FAILED` carrying `429` / `RESOURCE_EXHAUSTED` / `503` is a
  quota/infrastructure problem, not a code problem.
- `KILLED` — stopped; `spawn_worker` if the work is still needed.

Do not paste log contents unless the user asks for details.
