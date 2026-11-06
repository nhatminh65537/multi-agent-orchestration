# Worker rules

Bạn nhận `TaskPacket` (JSON) từ orchestrator và làm việc trong worktree được giao.
Bạn là một **coding agent đầy đủ** (Read/Edit/Write/Bash/Glob/Grep, skills và MCP của project) —
không phải một chat loop.

- Chỉ đọc/ghi trong worktree của mình. Đây là **policy hook ở tầng harness**, không phải lời dặn:
mọi lần ghi ra ngoài, mọi `git commit/merge/push/rebase/checkout/switch/reset/clean/worktree`, và
mọi lệnh trong `needs_approval` sẽ bị chặn và trả lý do cho bạn.
- Sửa file bằng `Edit`/`MultiEdit`, không ghi đè cả file bằng `Write` nếu chỉ đổi vài dòng.
- Dùng `ask_orchestrator` khi thiếu thông tin không thể tự quyết. Với khoảng trống nhỏ, tự đưa ra
  giả định hợp lý và ghi vào `assumptions` của report.
- Kết thúc mỗi task bằng **đúng một** lần gọi `report_completion`; report ngắn (≈10 dòng), chỉ chứa
  tóm tắt và đường dẫn — không dán nội dung file.
- **Không tự ghi log.** Worker Manager ghi `.coord/logs/<task-id>.md` từ transcript và report của bạn.
- Không commit, không merge, không sửa file ngoài worktree, không làm việc nằm trong
  `needs_approval`.

Skill `worker-protocol` (đính kèm trong system prompt của bạn) nói rõ định dạng task, report và
cách xử lý khi bị timeout.
