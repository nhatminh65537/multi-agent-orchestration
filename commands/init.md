---
description: Create the worktrees + .coord/ + templates for a peer group (any number of orchestrators)
argument-hint: [A B C] [--repo <path>]
allowed-tools: Bash(coord:*), Bash(git:*)
---

Run `coord init $ARGUMENTS` in the current repo (defaults to `A B C` when no names are given).

After it runs:

1. Report the worktrees that were created or kept, plus the status of `.gitignore`, `CLAUDE.md`,
   `AGENTS.md`, `.claude/settings.json`, `.mcp.json`, the worker skill, and the **bootstrap commit**
   (committed / nothing to commit / skipped / FAILED).
2. If the bootstrap commit `FAILED` (missing `user.name`/`user.email`), tell the user to configure
   git and re-run; explain that the worktrees only carry an unversioned copy for now.
3. Give the next steps: `coord up`, open N sessions in `wt-<project>-<name>`, `/rename <name>`.
   Re-running `coord init` is an **update** (it rewrites the marked block, worker skill, settings and
   `.mcp.json`) and re-syncs all five bootstrap files into every existing worktree.
