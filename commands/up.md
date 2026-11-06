---
description: Khởi động Worker Manager (coord serve) và kiểm tra kết nối
allowed-tools: Bash(coord:*), Bash(curl:*)
---

1. Kiểm tra server: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health`.
2. Nếu không phải `200`, chạy `coord serve` ở background (background terminal), rồi chờ tới khi
   `/health` trả `200` (tối đa ~15s).
3. Chạy `coord status` và báo lại: server đang ở đâu, model, số worker hiện có.
4. Nếu `coord` không có trên PATH, chỉ người dùng chạy `uv tool install <repo này>` trước.
