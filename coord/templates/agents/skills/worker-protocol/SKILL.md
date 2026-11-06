---
name: worker-protocol
description: Cách một worker nhận task, hỏi ngược orchestrator, làm việc trong worktree và báo cáo kết thúc. Nạp khi bắt đầu bất kỳ TaskPacket nào.
---

# Worker protocol

## 1. Task packet

Task đến dưới dạng JSON:

```json
{
  "id": "A-07",
  "goal": "Một câu mô tả kết quả cần có",
  "files": ["src/auth/api.py"],
  "acceptance": ["POST /auth/refresh trả {token, exp}"],
  "constraints": ["không đổi schema DB"],
  "needs_approval": ["git merge", "rm"],
  "read_only": false,
  "max_rounds": 3
}
```

`id` bắt buộc dạng `<stream>-<n>`. `files` là các file bạn **định ghi** (dùng để chống ghi đè giữa
các worker cùng worktree). Nếu packet thiếu `acceptance`, hỏi lại orchestrator một lần rồi tự đặt
tiêu chí và ghi vào `assumptions`.

`read_only: true` nghĩa là chỉ đọc: không sửa file, chỉ phân tích và báo cáo.

## 2. Làm việc

1. Đọc đúng những file trong `files` trước; chỉ mở rộng khi thật cần.
2. Sửa bằng `Edit` / `MultiEdit` (sửa đúng vùng), không ghi đè cả file lớn bằng `Write`.
3. Kiểm chứng bằng `Bash` (test/lint nếu repo có) và giữ lại **đúng dòng kết quả**.
4. Gặp chỗ không thể quyết (contract, API shape, hành vi mong đợi): gọi
   `ask_orchestrator(task_id, question)` — một câu hỏi cụ thể, kèm phương án bạn đề xuất.
5. Nếu nhận `NO_ANSWER: proceed with a stated assumption.` thì tiếp tục và ghi giả định đó vào
   `assumptions`.
6. Tối đa 3 vòng hỏi cho một task; sau đó tự quyết + ghi giả định.

## 3. Kết thúc

Gọi **đúng một lần** `report_completion` với:

```json
{
  "id": "A-07",
  "status": "DONE",
  "summary": "≤ 600 ký tự, mô tả kết quả và cách kiểm chứng",
  "assumptions": ["..."],
  "files_changed": ["src/auth/api.py"],
  "diff_stat": "2 files changed, +48 −6",
  "tests": "pytest tests/auth: 41 passed",
  "log": ""
}
```

- `status` chỉ là `DONE` hoặc `FAILED`.
- Không dán nội dung file vào `summary`; chỉ đường dẫn.
- **Không tự ghi log.** Worker Manager tự ghi `.coord/logs/<task-id>.md` từ transcript + report.
- Không commit, không merge: orchestrator review `git diff` rồi tự commit.

## 4. Ranh giới (harness cưỡng chế, không chỉ prompt)

- Không `git commit/merge/push/rebase/checkout/switch/reset/clean/worktree`.
- Không ghi/xoá file ngoài worktree (kể cả đường dẫn tuyệt đối hay `../`).
- Không chạy lệnh nằm trong `needs_approval`.
- Không gọi MCP tools của `coord` (đã bị lọc khỏi danh sách tool của bạn).
- Nếu bạn nghĩ mình cần một trong các thứ trên, gọi `ask_orchestrator`.

## 5. Khi bị chặn

- Không sửa được vì thiếu quyền / file bị khoá / test hỏng sẵn: `status = "FAILED"` với `summary`
  nêu nguyên nhân và bước tiếp theo đề xuất.
- Không bao giờ kết thúc im lặng: manager coi việc thiếu report là `FAILED`.
