---
description: Kiểm tra môi trường, credentials, server và ledger của repo
allowed-tools: Bash(coord:*)
---

Chạy `coord doctor` trong repo hiện tại và tóm tắt:

- Dòng `FAIL` → phải sửa trước khi làm gì khác.
- Dòng `WARN` → nêu hệ quả nếu bỏ qua (ví dụ: server chưa chạy thì không spawn được worker;
  `.coord/` chưa có thì chưa `coord init`).
- `api key` FAIL → nhắc đặt `COORD_API_KEY` (hoặc `OPENAI_API_KEY`) trong môi trường chạy
  `coord serve`; không hỏi giá trị key.
