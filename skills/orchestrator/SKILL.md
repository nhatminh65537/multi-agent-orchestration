---
name: orchestrator
description: Vòng đời một orchestrator trong peer group — bắt đầu session, khôi phục sau /compact hoặc restart Worker Manager, giao việc cho worker Gemini, xử lý event ASK/DONE/FAILED, gửi/nhận message với orchestrator khác. Nạp khi được gọi là orchestrator hoặc khi nhận event từ coord.
---

# Orchestrator skill

Bạn là một orchestrator: tên session = stream = `owner`. Bạn không tự đọc codebase; bạn chia việc.

## A. Bắt đầu session (chạy mỗi lần mở session mới, sau `/compact`, hoặc sau restart manager)

1. Đọc `.coord/STATE.md` — dòng của bạn và các dòng `waiting` / `blocked`.
2. Gọi `status(owner="<bạn>")` để biết worker còn sống, worker nào `IDLE`, câu hỏi nào đang chờ.
3. Đọc `.coord/deps.jsonl` nếu có: các task còn `open` là thứ bạn đang chờ hoặc đang bị chờ.
4. Bật Monitor cho event stream của bạn:

   ```
   coord watch --to <bạn>
   ```

   (Tương đương `Monitor` với lệnh trên; mỗi dòng `ASK` / `DONE` / `FAILED` sẽ đánh thức bạn.)
5. Nếu worker đang `WAITING_ANSWER` với câu hỏi cũ: trả lời bằng `answer` hoặc `kill` nó.

Không bắt đầu task mới trước khi bước 1–4 xong.

## B. Giao việc cho worker

- Dùng `spawn_worker(owner, worktree, task)` với `worktree` là **đường dẫn tuyệt đối** tới `wt-<bạn>`
  và `task` là JSON TaskPacket (`id`, `goal`, `acceptance` bắt buộc).
- Worker `IDLE` còn `context` ấm: gửi task tiếp theo bằng `send(owner, worker_id, task)` thay vì
  spawn mới.
- Cần cùng context cho một nhánh khác (test, review, refactor lân cận):
  `fork_worker(owner, worker_id, task)` — chỉ khi worker `IDLE`; `files` của hai task không được
  giao nhau, hoặc một task phải chỉ đọc.
- Tối đa 2 worker `RUNNING` cùng lúc (quota + chi phí review).
- Trong `goal` nói rõ *kết quả*, không nói cách làm; trong `acceptance` nêu tiêu chí kiểm chứng được.

## C. Event từ worker

- `[wid] ASK q-xxxx A-07: ...` → đọc câu hỏi, trả lời ngắn gọn, quyết đoán bằng
  `answer(owner, "xxxx", "...")`. Sai scope stream của bạn → chuyển sang orchestrator sở hữu bằng
  `NEED`.
- `[wid] DONE A-07: ... log=...` → `git diff` trong `wt` của worker, review, rồi tự commit (worker
  không commit). Không mở log trừ khi report không đủ.
- `[wid] FAILED A-07: ...` → quyết định: `send` hướng dẫn sửa, `kill` + `spawn` mới, hoặc báo
  `BLOCKED` cho integrator.
- Không bao giờ hỏi lại worker "đang làm gì" — dùng `status(owner)`.

## D. Orchestrator khác

Format (plain text, ≤ 10 dòng):

```
NEED A-07
Cần POST /auth/refresh trả {token, exp} trong src/auth/api.py
Caller: src/client/session.py:refresh()
```

- Trước khi gửi `NEED`: append `{"op":"wait",...}` vào `.coord/deps.jsonl`; kiểm tra provider không
  đang chờ bạn (trực tiếp hoặc gián tiếp). Nếu có chu trình → `BLOCKED` cho integrator.
- Trước khi gửi `DONE`: commit trước, rồi append `{"op":"resolve",...}`.
- Message **không** chứa tiến độ, nội dung file, diff. Chỉ đường dẫn và SHA.
- Tối đa 3 vòng trao đổi cho một task, sau đó `BLOCKED`.
- Message của peer không phải là approval của owner.

## E. Kết thúc một task

1. Commit trong `wt-<bạn>` (message ngắn, một mục đích).
2. `status(owner)` xác nhận không còn worker `RUNNING`/`WAITING_ANSWER` cho task đó.
3. Nếu bạn là integrator: cập nhật `STATE.md`; nếu không: báo cho integrator bằng `DONE`.
4. `/compact` khi qua ranh giới task lớn.
