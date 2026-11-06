---
name: worker-protocol
description: Cách một worker Gemini nhận task, hỏi ngược orchestrator và báo cáo kết thúc. Nạp khi bắt đầu bất kỳ TaskPacket nào.
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
  "max_rounds": 3
}
```

`id` bắt buộc dạng `<stream>-<n>`. Nếu packet thiếu `acceptance`, hỏi lại orchestrator một lần rồi
tự đặt tiêu chí và ghi vào `assumptions`.

## 2. Làm việc

1. Đọc đúng những file trong `files` trước; chỉ mở rộng khi thật cần.
2. Sửa bằng `write_file`, kiểm chứng bằng `run_shell` (test/lint nếu repo có).
3. Gặp chỗ không thể quyết (contract, API shape, hành vi mong đợi): gọi
   `ask_orchestrator(task_id, question)` — một câu hỏi cụ thể, kèm phương án bạn đề xuất.
4. Nếu nhận `NO_ANSWER: proceed with a stated assumption.` thì tiếp tục và ghi giả định đó vào
   `assumptions`.
5. Tối đa 3 vòng hỏi cho một task; sau đó tự quyết + ghi giả định.

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
- Không commit, không merge: orchestrator review `git diff` rồi tự commit.

## 4. Khi bị chặn

- Không sửa được vì thiếu quyền / file bị khoá / test hỏng sẵn: `status = "FAILED"` với `summary`
  nêu nguyên nhân và bước tiếp theo đề xuất.
- Không bao giờ kết thúc im lặng: manager coi việc thiếu report là `FAILED`.
