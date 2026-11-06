---
description: Tạo worktree + .coord/ + template cho một peer group (N orchestrator bất kỳ)
argument-hint: [A B C] [--repo <path>]
allowed-tools: Bash(coord:*), Bash(git:*)
---

Chạy `coord init $ARGUMENTS` trong repo hiện tại (nếu không có tên, mặc định `A B C`).

Sau khi chạy:

1. In lại danh sách worktree vừa tạo và trạng thái của `.gitignore`, `CLAUDE.md`, `AGENTS.md`,
   `.claude/settings.json`, `.mcp.json`, và **bootstrap commit** (committed / nothing to commit /
   skipped / FAILED).
2. Nếu bootstrap commit `FAILED` (thiếu `user.name`/`user.email`), nhắc người dùng cấu hình git rồi
   chạy lại; nói rõ worktree hiện chỉ có bản copy, không versioned.
3. Nhắc bước tiếp theo: `coord up`, mở N session ở `wt-<name>`, `/rename <name>`.
