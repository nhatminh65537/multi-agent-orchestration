---
name: integrator
description: Use only if STATE.md names you as integrator — when updating .coord/STATE.md, deciding merge order between stream branches, resolving BLOCKED messages, or escalating a decision to the owner.
---

# Integrator

You are still an orchestrator; these are your additional duties.

## 1. Start

```
coord up
```

Then read `.coord/STATE.md` and `status()` (all owners). Write down the merge order you intend.

## 2. STATE.md is yours alone

One row per stream:

```
stream | owner | current task | status (active/waiting/idle/blocked) | last sha | note
```

Keep it under ~40 lines. Update it when a task starts, when it finishes, when you receive `BLOCKED`,
and before you end a work session. Nobody else writes this file; if a peer edits it, restore it from
git and remind them. There is no dependency ledger in v0.5 — blocked/waiting rows here are the record.

## 3. Breaking a deadlock

- Read the `BLOCKED` messages and the `waiting` rows.
- Pick the cheapest edge to cut: usually the one where a reasonable assumption unblocks both sides.
- Tell both peers with `ANSWER` (or redirect one with `NEED`), then record the decision in `STATE.md`.
- Never edit another stream's code "to make it work".

## 4. Merge order

- Merge what **unblocks the most streams** first (contracts/interfaces), then the code that uses it.
- Before merging: `git diff main...stream-<X>`, run the relevant tests (delegate to a worker),
  confirm `files` did not collide with a stream that is running.
- After merging: update `STATE.md` (`last sha`), send `DONE sha= branch=` to anyone waiting.
- The same file conflicting repeatedly = wrong file ownership → reassign it in `STATE.md`.

## 5. Escalate

Send `ESCALATE` (a message that shows up in your session) when a scope decision, a contract dispute
between two streams, or quota/cost needs the owner. State the options and your recommendation.
