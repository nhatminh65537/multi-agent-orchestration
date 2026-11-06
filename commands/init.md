---
description: Tạo worktree + .coord/ + template cho một peer group (N orchestrator bất kỳ)
argument-hint: [A B C] [--repo <path>]
allowed-tools: Bash(coord:*), Bash(git:*)
---

Chạy `coord init $ARGUMENTS` trong repo hiện tại (nếu không có tên, mặc định `A B C`).

Sau khi chạy:

1. In lại danh sách worktree vừa tạo và trạng thái của `.gitignore`, `CLAUDE.md`, `AGENTS.md`,
   `.claude/settings.json` (created / updated / unchanged).
2. Nếu repo chưa có `.agents/skills/repo-conventions/SKILL.md`, nhắc người dùng copy template từ
   plugin (`skills/repo-conventions/SKILL.md`) và sửa cho đúng repo.
3. Nhắc bước tiếp theo: `coord serve`, mở N session ở `wt-<name>`, `/rename <name>`.
