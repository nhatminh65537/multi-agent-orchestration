---
description: Khởi động Worker Manager (coord up) và kiểm tra kết nối
allowed-tools: Bash(coord:*), Bash(curl:*)
---

1. Chạy `coord up` (idempotent: nếu server đã chạy thì chỉ in URL).
2. Xác nhận `/health` trả `ok`:
   `curl -s http://127.0.0.1:30303/health`.
3. Chạy `coord status` và báo lại: server ở đâu, backend/model, số worker hiện có.
4. Nếu `coord` không có trên PATH, đề nghị người dùng chạy `uv tool install '.[agy]'` trước.
5. Nếu `coord up` báo thiếu API key, nhắc đặt `COORD_API_KEY` (hoặc `GEMINI_API_KEY`) trong môi
   trường của service; không hỏi giá trị key.
