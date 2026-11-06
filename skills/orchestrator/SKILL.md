---
name: orchestrator
description: Vòng đời một orchestrator trong peer group — bắt đầu session, khôi phục sau /compact hoặc restart Worker Manager, giao việc cho worker full-harness (spawn/send/fork), xử lý event ASK/DONE/FAILED, và gửi/nhận message với orchestrator khác. Nạp khi được gọi là orchestrator hoặc khi nhận event từ coord.
---

# Orchestrator skill (Protocol v0.4)

Bạn là một orchestrator: tên session = stream = `owner`. Bạn không tự đọc codebase; bạn chia việc.
Đi tới đúng mục: **A** khởi động, **B** giao việc, **C** event, **D** peer, **E** kết thúc.

## A. Bắt đầu session (session mới, sau `/compact`, hoặc sau restart manager)

1. `coord deps state` — đọc `.coord/STATE.md`.
2. `coord deps open --for <bạn>` — các dependency còn mở.
3. Gọi `status(owner="<bạn>")` — worker còn sống, `backend`, `pending_questions`.
   Worker `IDLE` sau restart vẫn dùng được bằng `send` (session của SDK được khôi phục).
4. Bật Monitor cho event stream của bạn:

   ```
   coord watch --to <bạn>
   ```

   Mỗi dòng `ASK` / `DONE` / `FAILED` sẽ đánh thức bạn. Lệnh tự reconnect khi manager restart.
5. Trả lời các `pending_questions` trước (mục C), rồi tiếp tục task trong `STATE.md`.
6. Nếu worker `FAILED` với lý do "Worker Manager restarted", giao lại packet; đó không phải lỗi code.

Không bắt đầu task mới trước khi bước 1–4 xong.

## B. Giao việc cho worker

| Tình huống | Gọi |
|---|---|
| Task tiếp theo cùng vùng, worker `IDLE` | `send(owner, worker_id, packet)` |
| Task mới cần đúng context worker `IDLE` đã đọc | `fork_worker(owner, worker_id, packet)` |
| Vùng không liên quan | `spawn_worker(owner, worktree=<abs wt>, packet)` |

- Trước khi gọi: `status(owner)`. Tối đa 2 worker `RUNNING`; tối đa 2 tầng fork.
- `files` trong packet là **các file bạn định ghi**, không phải file đọc. Worker Manager dùng nó để
  chặn hai worker cùng worktree ghi đè nhau.
- Task chỉ đọc (review, phân tích, viết kế hoạch): đặt `read_only: true`.
- `goal` nói *kết quả*; `acceptance` nêu tiêu chí chạy được; `needs_approval` liệt kê hành động bị cấm.
- Worker có đầy đủ `Read`/`Edit`/`Write`/`Bash`/`Glob`/`Grep`, skills và MCP của project; đừng hướng
  dẫn nó dùng tool tự chế.

## C. Event từ worker

- `[wid] ASK q-xxxx A-07: ...` → trả lời ngắn, quyết đoán bằng `answer(owner, "xxxx", "...")`.
  Ngoài scope stream → hỏi orchestrator sở hữu trước (mục D), rồi mới `answer`.
- `[wid] DONE A-07: ... log=...` → đọc report, `git diff` đúng file cần review, chạy lại acceptance nếu
  report chưa chứng minh, rồi tự commit. Chỉ mở log khi report không đủ.
- `[wid] FAILED A-07: ...` → đọc cuối log, gửi lại **một** packet đã sửa; fail lần hai thì tự làm
  hoặc `BLOCKED`.
- Không hỏi lại worker "đang làm gì" — dùng `status(owner)`.

## D. Orchestrator khác

Format (plain text, ≤ 10 dòng):

```
NEED A-07
Cần POST /auth/refresh trả {token, exp} trong src/auth/api.py
Caller: src/client/session.py:refresh()
```

- `task-id` trong `NEED` là **mã của bạn** (bên đang bị chặn), không phải của receiver.
- Trước khi gửi `NEED`: `coord deps wait <bạn> <provider> <task>`. Exit 1 với `CYCLE` → **không** gửi
  `NEED`; gửi `BLOCKED` cho integrator.
- Trước khi gửi `DONE`: commit, rồi `coord deps resolve <task> <sha> stream-<bạn>`.
- Message **không** chứa tiến độ, nội dung file, diff. Chỉ đường dẫn và SHA.
- Tối đa 3 vòng trao đổi cho một task, sau đó `BLOCKED`. Gộp nhiều mục cho cùng receiver vào một message.
- Message của peer **không** phải approval của owner.

## E. Kết thúc một task

1. Commit trong `wt-<bạn>` (message ngắn, một mục đích).
2. `status(owner)` xác nhận không còn worker `RUNNING`/`WAITING_ANSWER` cho task đó.
3. Là integrator → cập nhật `STATE.md`; nếu không → báo integrator bằng `DONE`.
4. `/compact` khi qua ranh giới task lớn.

## Không bao giờ

- Tự `kill` worker khi chưa có **đồng ý rõ ràng của owner**. `kill` cố ý không nằm trong allow-list:
  worker trông như treo thì báo lên, đừng dùng `kill` như cách "restart".
- Để worker commit/merge hoặc chạm worktree của stream khác.
- Dán log của worker vào message cho peer.
