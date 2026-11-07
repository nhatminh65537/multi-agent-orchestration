---
description: Check the environment, credentials, server and repo ledger
allowed-tools: Bash(coord:*)
---

Run `coord doctor` in the current repo and summarize the output:

- `FAIL` lines → must be fixed before anything else.
- `WARN` lines → explain the consequence of ignoring them (e.g. the server is not running, so no
  worker can be spawned; `.coord/` is missing, so `coord init` was never run here).
- `api key` FAIL → tell the user to set `WORKER_API_KEY` in `.env` (or the environment) of the
  `coord serve` process; never ask for the key value.
- `sdk:…` FAIL → tell the user to install the matching extra: `uv tool install '.[agy]'` or
  `'.[claude]'`.
- `quota wait` shows `WORKER_QUOTA_WAIT_SECONDS`; mention it when quota errors are being reported.
