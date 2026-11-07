# Coordination state

Only the **integrator** writes this file. One row per stream.

| stream | owner | current task | status | last sha | note |
|--------|-------|--------------|--------|----------|------|
| stream-A | A | — | idle | — | |
| stream-B | B | — | idle | — | |
| stream-C | C | — | idle | — | |

Valid statuses: `active` / `waiting` / `idle` / `blocked`.
The integrator tracks tasks that wait on each other (record them in the `note` column).
