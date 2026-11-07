---
description: Start the Worker Manager (coord up) and verify the connection
allowed-tools: Bash(coord:*), Bash(curl:*)
---

1. Run `coord up` (idempotent: if the server is already running it only prints the URL).
2. Confirm `/health` returns `ok`:
   `curl -s http://127.0.0.1:30303/health`.
3. Run `coord status` and report: where the server is, the backend/model, and how many workers exist.
4. If `coord` is not on PATH, ask the user to run `uv tool install '.[agy]'` first.
5. If `coord up` reports a missing API key, tell the user to set `WORKER_API_KEY` in `.env` or in the
   service environment; never ask for the key value.
