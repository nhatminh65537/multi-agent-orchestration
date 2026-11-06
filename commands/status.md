---
description: Xem worker của bạn (hoặc toàn bộ) và các câu hỏi đang chờ
argument-hint: [--owner A] [--project /path/to/repo]
allowed-tools: Bash(coord:*)
---

Chạy `coord status $ARGUMENTS`.

Diễn giải kết quả:

- `RUNNING` — đang chạy; đừng `send` thêm.
- `IDLE` — có thể `send` task mới hoặc `fork_worker`.
- `WAITING_ANSWER` — có câu hỏi trong cột `asks`; trả lời bằng `answer`, hoặc `kill` nếu đã cũ.
- `FAILED` — xem `log=` trong event `FAILED` gần nhất; quyết định `send` sửa hay `spawn` lại.
- `KILLED` — đã dừng; `spawn_worker` mới nếu cần.

Không đề xuất đọc log trừ khi người dùng hỏi chi tiết.
