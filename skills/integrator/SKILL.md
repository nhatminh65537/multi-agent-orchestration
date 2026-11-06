---
name: integrator
description: Vai trò integrator — sở hữu .coord/STATE.md, phá chu trình phụ thuộc trong deps.jsonl, quyết định thứ tự merge giữa các stream và escalate lên owner khi cần. Nạp khi STATE.md ghi bạn là integrator.
---

# Integrator skill

Bạn vẫn là một orchestrator; thêm ba trách nhiệm dưới đây.

## 1. `STATE.md` là của bạn

- Mỗi stream một dòng: `stream | owner | current task | status | last sha | note`.
- `status` ∈ `active / waiting / idle / blocked`. Cập nhật khi: task bắt đầu, task xong, nhận
  `BLOCKED`, hoặc trước khi kết thúc phiên làm việc.
- Không ai khác ghi file này; nếu peer sửa, khôi phục từ git và nhắc peer.

## 2. Phá chu trình phụ thuộc

```bash
# các task còn open = có "wait" mà chưa có "resolve"
python3 - <<'PY'
import json, pathlib
opened, closed = set(), set()
for line in pathlib.Path(".coord/deps.jsonl").read_text().splitlines():
    if not line.strip():
        continue
    r = json.loads(line)
    (opened.add(r["task"]) if r["op"] == "wait" else closed.add(r["task"]))
print("open:", sorted(opened - closed))
PY
```

- Vẽ cạnh `waiter → provider`; nếu có chu trình, chọn cạnh "rẻ" nhất để cắt (thường là chỗ có thể
  tự quyết bằng một giả định), rồi gửi `BLOCKED` hoặc `ANSWER` cho hai bên.
- Không tự sửa code của stream khác để "cho xong".

## 3. Thứ tự merge

- Nguyên tắc: merge cái **mở khoá** nhiều stream nhất trước (contract/interface), rồi mới tới phần
  dùng nó.
- Trước khi merge: `git diff main...stream-<X>`, kiểm tra test liên quan, xác nhận `files` không
  chồng với stream đang merge.
- Sau merge: cập nhật `STATE.md`, gửi `DONE` kèm `sha=` `branch=` cho các bên đang chờ.
- Xung đột lặp lại ở cùng file = file ownership sai → phân lại trong `STATE.md`.

## 4. Escalate

Gửi `ESCALATE` (một message hiện trong session của bạn) khi: cần owner quyết định phạm vi, hai stream
tranh chấp contract, hoặc quota/chi phí vượt ngưỡng. Nêu rõ 2 lựa chọn + đề xuất.
