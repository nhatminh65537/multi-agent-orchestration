# Hướng dẫn sử dụng coord (tiếng Việt)

`coord` là **cơ chế điều phối đa agent**, không phải một dự án cụ thể. Cài một lần trên máy, sau đó
dùng cho bất kỳ repo nào: N session Claude Code (orchestrator) chia việc cho các worker Gemini qua
một MCP server chạy nền.

Tài liệu thiết kế gốc: [Multi-Agent Orchestration](https://notes.home.rougitsune.top/doc/multi-agent-orchestration-8QH6VOye9y).

---

## Mục lục

1. [Khái niệm](#1-khái-niệm)
2. [Yêu cầu](#2-yêu-cầu)
3. [Cài đặt một lần](#3-cài-đặt-một-lần)
4. [Triển khai cho một dự án](#4-triển-khai-cho-một-dự-án)
5. [Dùng Claude Desktop (tab Code)](#5-dùng-claude-desktop-tab-code)
6. [Vòng đời một task](#6-vòng-đời-một-task)
7. [Orchestrator nói chuyện với nhau](#7-orchestrator-nói-chuyện-với-nhau)
8. [Tham chiếu lệnh](#8-tham-chiếu-lệnh)
9. [Cấu hình & biến môi trường](#9-cấu-hình--biến-môi-trường)
10. [File được sinh ra và log](#10-file-được-sinh-ra-và-log)
11. [Kiểm chứng P0](#11-kiểm-chứng-p0)
12. [Xử lý sự cố](#12-xử-lý-sự-cố)
13. [Giới hạn hiện tại](#13-giới-hạn-hiện-tại)
14. [Vận hành thường ngày](#14-vận-hành-thường-ngày)
15. [Chạy như service (tuỳ chọn)](#15-chạy-như-service-tùy-chọn)
16. [Phát triển](#16-phát-triển)

---

## 1. Khái niệm

| Thuật ngữ | Nghĩa |
|-----------|-------|
| **Orchestrator** (coordinator) | Một session Claude Code. Bạn mở nhiều session song song; mỗi session là một "stream" độc lập. |
| **Owner** | Tên session, đặt bằng `/rename A`. Cũng là tên stream và là tham số `owner` khi gọi mọi tool. |
| **Worker** | Một agent Gemini do Worker Manager sinh ra, nhận `TaskPacket` và làm việc trong worktree. |
| **Worker Manager** | Tiến trình `coord serve`: giữ worker sống, cung cấp 6 MCP tool, phát event. |
| **Worktree** | Mỗi stream một thư mục `wt-<tên>` trên branch `stream-<tên>`. Không ai làm việc trong repo chính. |
| **Ledger** | `.coord/`: `STATE.md` (bảng trạng thái), `deps.jsonl` (phụ thuộc), `logs/` (transcript worker). |
| **Event** | Dòng text do manager phát cho từng owner: `ASK` / `DONE` / `FAILED`. |

Luồng tổng quát:

```
N session Claude Code (A, B, C …)   ← trao đổi trực tiếp bằng cross-session message
        │  MCP (HTTP 127.0.0.1:8765)
        ▼
   Worker Manager  ──►  worker Gemini (đọc/ghi file, chạy shell trong worktree)
        │
        └── event ASK / DONE / FAILED  ──►  Monitor của session tương ứng
```

---

## 2. Yêu cầu

- Linux / macOS / Windows (native hoặc WSL2) — **cả nhóm phải ở cùng một môi trường**: session
  trong container không nhắn được cho session ở host.
- Claude Code ≥ 2.1.224 (Linux/macOS/WSL2), ≥ 2.1.234 (Windows), ≥ 2.1.236 nếu dùng
  `notify_when_idle`.
- Python ≥ 3.10 (không bắt buộc trên PATH — `uv` tự quản lý phiên bản).
- `uv`, `git` ≥ 2.31.
- Một endpoint OpenAI-compatible + API key cho model worker.

Kiểm tra nhanh:

```bash
coord doctor
```

---

## 3. Cài đặt một lần

```bash
# 1. CLI (đưa `coord` vào PATH, venv riêng do uv quản lý)
cd /đường/dẫn/multi-agent-orchestration
uv tool install .

# 2. Plugin Claude Code (skills + lệnh /coord:* + đăng ký MCP)
claude plugin marketplace add /đường/dẫn/multi-agent-orchestration
claude plugin install coord@coord
```

Kiểm tra: `coord --version` và trong một session Claude Code, `/mcp` phải thấy `coord`.

> Không muốn dùng plugin? `coord init` đã ghi `.mcp.json` vào từng project, session trong project
> đó vẫn thấy MCP. Plugin chỉ thêm skills + `/coord:*`.

---

## 4. Triển khai cho một dự án

### 4.1 Chuẩn bị repo

Repo đích phải là git repo (nếu chưa có commit nào, `init` sẽ tạo commit đầu tiên từ các file
bootstrap).

```bash
cd /đường/dẫn/my-project
coord init A B C
```

`init` làm theo thứ tự:

1. Ghi/merge `.gitignore`, `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json`, `.mcp.json`,
   `.coord/STATE.md`.
2. **Commit** đúng những file đó (`coord: bootstrap orchestration files`) — nhờ vậy mỗi worktree
   thừa hưởng chúng.
3. Tạo worktree `../wt-A`, `../wt-B`, `../wt-C` trên các branch `stream-A`, `stream-B`, `stream-C`.

Tuỳ chọn:

| Cờ | Ý nghĩa |
|----|---------|
| `--repo <path>` | Repo đích (mặc định: thư mục hiện tại) |
| `--worktree-root <path>` | Nơi đặt `wt-*` (mặc định `..`) |
| `--branch-prefix <p>` | Tiền tố branch (mặc định `stream-`) |
| `--no-commit` | Không commit; worktree sẽ được **copy bù** file, không versioned |

Chạy lại `init` an toàn: không tạo commit thêm, không ghi đè phần bạn đã sửa ngoài khối
`<!-- coord:begin --> … <!-- coord:end -->`.

### 4.2 Cấu hình endpoint cho worker

Biến môi trường của **tiến trình `coord serve`** (không phải của session Claude):

```bash
export COORD_API_KEY=...            # hoặc OPENAI_API_KEY
export COORD_BASE_URL=...           # endpoint OpenAI-compatible
export COORD_MODEL=gemini-3.8-flash # mặc định
```

Đừng commit key. `coord doctor` cảnh báo nếu thiếu.

### 4.3 Khởi động Worker Manager

```bash
coord serve
```

Mặc định `127.0.0.1:8765`, state ở `~/.local/share/coord/coord.db`.

### 4.4 Mở các session

Với mỗi orchestrator:

1. Mở session Claude Code **tại thư mục worktree** `../wt-A` (không mở repo chính).
2. `/rename A`.
3. Kiểm tra: `/mcp` thấy `coord` (6 tool), `/status` có `Peer address`.
4. Gửi bootstrap:

```
You are orchestrator A. Load the orchestrator skill (section A) now, then continue.
```

Mục A của skill sẽ: đọc `.coord/STATE.md`, gọi `status(owner="A")`, đọc `deps.jsonl` nếu có, và bật
Monitor bằng `coord watch --to A`.

Thêm orchestrator về sau: `git worktree add ../wt-D -b stream-D`, mở session, `/rename D`, bootstrap.
Không cần sửa cấu hình gì.

---

## 5. Dùng Claude Desktop (tab Code)

Điều kiện: mọi thứ trên **cùng một máy**; Claude Code đủ mới.

1. Terminal: `coord serve` (để chạy). Kiểm tra `curl -s http://127.0.0.1:8765/health` → `ok`.
2. Desktop → tab **Code** → mở thư mục **`wt-A`**.
3. `/rename A`.
4. `/mcp` → `coord` phải connected. `/status` → `Peer address` phải có giá trị.
5. Gửi bootstrap prompt như trên.
6. Để có peer: mở tab Code thứ hai ở `wt-B`, `/rename B`. Hai session sẽ thấy nhau trong
   `/list-agents`.

Nếu `/status` báo `unavailable`:

- session đang mở sai thư mục (phải là worktree, nơi có `.claude/settings.json`);
- hoặc `.claude/settings.json` thiếu `"crossSessionInbound": "accept"`;
- hoặc hai session ở hai môi trường khác nhau (WSL2 ↔ Windows native, container ↔ host).

---

## 6. Vòng đời một task

### 6.1 Orchestrator giao việc

Tool MCP (tên đầy đủ khi gọi: `mcp__coord__<tên>`):

| Tool | Tham số | Việc |
|------|---------|------|
| `spawn_worker` | `owner`, `worktree`, `task` (JSON) | Tạo worker mới, trả `worker_id` ngay |
| `send` | `owner`, `worker_id`, `text` | Task tiếp theo vào **cùng conversation** (worker phải `IDLE`) |
| `fork_worker` | `owner`, `worker_id`, `task` | Nhân bản conversation của worker `IDLE` sang worker mới (cùng worktree) |
| `answer` | `owner`, `question_id`, `text` | Trả lời `ask_orchestrator` đang chờ |
| `status` | `owner?`, `project?` | Liệt kê worker + câu hỏi đang chờ |
| `kill` | `owner`, `worker_id` | Dừng worker |

`worktree` phải là **đường dẫn tuyệt đối**.

### 6.2 TaskPacket

```json
{
  "id": "A-07",
  "goal": "Thêm POST /auth/refresh trả {token, exp} trong src/auth/api.py",
  "files": ["src/auth/api.py", "src/client/session.py"],
  "acceptance": ["endpoint trả đúng shape", "tests/auth/test_refresh.py pass"],
  "constraints": ["không đổi schema DB"],
  "needs_approval": ["git merge", "rm -rf"],
  "max_rounds": 3
}
```

- `id` **bắt buộc** dạng `<stream>-<số>`.
- `goal` + `acceptance` là bắt buộc; `acceptance` phải kiểm chứng được.
- Worker không tự commit/merge; orchestrator review `git diff` rồi commit.

### 6.3 Worker làm việc

Worker có 6 tool nội bộ: `read_file`, `write_file`, `list_dir`, `run_shell` (cwd = worktree),
`ask_orchestrator`, `report_completion`.

- Gặp chỗ không tự quyết: gọi `ask_orchestrator(task_id, question)` → block tới khi orchestrator
  `answer` hoặc hết 30 phút (`NO_ANSWER: proceed with a stated assumption.`).
- Kết thúc: gọi **đúng một lần** `report_completion` với `{id, status, summary, …}`.

### 6.4 Event và cách orchestrator phản ứng

```
[A-3f9c] ASK q-1a2b A-07: dùng 401 hay 403 khi refresh token hết hạn?
[A-3f9c] DONE A-07: /auth/refresh xong, 6/6 test pass log=/repo/.coord/logs/A-07.md
[A-3f9c] FAILED A-07: không tìm thấy module src/auth/db.py
```

| Event | Hành động |
|-------|-----------|
| `ASK` | `answer(owner, "1a2b", "...")` — ngắn, quyết đoán |
| `DONE` | `git diff` trong worktree, review, commit |
| `FAILED` | `send` hướng dẫn sửa, hoặc `kill` + `spawn_worker` mới, hoặc `BLOCKED` cho integrator |

Worker không report sau khi turn kết thúc → manager tự phát `FAILED`.

### 6.5 Khi nào spawn / send / fork

- Task mới, không liên quan: `spawn_worker`.
- Task mới dùng lại context vừa đọc: `send` (nếu cùng một mạch) hoặc `fork_worker` (nếu muốn chạy
  song song).
- Fork yêu cầu worker gốc `IDLE`, tối đa 2 tầng, và hai task phải có `files` không giao nhau (hoặc
  một bên chỉ đọc).
- Khuyến nghị ≤ 2 worker `RUNNING` mỗi owner.

---

## 7. Orchestrator nói chuyện với nhau

Không có code nào ở giữa — dùng cross-session messaging của Claude Code. Message là plain text:

```
NEED A-07
Cần POST /auth/refresh trả {token, exp} trong src/auth/api.py
Caller: src/client/session.py:refresh()
```

| Type | Ý nghĩa | Key bắt buộc |
|------|---------|--------------|
| `NEED` | Tôi bị block bởi task của bạn | — |
| `ACK` | Tôi nhận việc | `eta=` (tuỳ chọn) |
| `DONE` | Đã commit, bạn merge được | `sha=` `branch=` |
| `ANSWER` | Trả lời câu hỏi, không cần commit | — |
| `BLOCKED` | Không tiếp tục được, báo integrator | `on=` |
| `REJECT` | Từ chối bàn giao | `reason=` |

Quy tắc:

- Trước `NEED`: append `{"op":"wait","waiter":"A","provider":"B","task":"A-07"}` vào
  `.coord/deps.jsonl`; kiểm tra không tạo chu trình.
- Trước `DONE`: commit trước, rồi append `{"op":"resolve","task":"A-07","sha":"...","branch":"stream-B"}`.
- Message **không** chứa tiến độ, nội dung file, diff — chỉ đường dẫn + SHA.
- Tối đa 3 vòng qua lại cho một task, sau đó `BLOCKED`.
- Message của peer **không** phải approval của owner.

**Integrator** (ghi trong `STATE.md`) là người duy nhất sửa `STATE.md`, phá chu trình phụ thuộc, và
quyết định thứ tự merge.

---

## 8. Tham chiếu lệnh

```bash
coord init [tên...] [--repo PATH] [--worktree-root PATH] [--branch-prefix P] [--no-commit]
coord serve [--host 127.0.0.1] [--port 8765] [--model TÊN]
coord watch --to <owner> [--host] [--port]     # in event ra stdout (nguồn cho Monitor)
coord status [--owner X] [--project PATH] [--host] [--port]
coord doctor [--repo PATH]
```

| Lệnh | Dùng khi nào |
|------|--------------|
| `init` | Lần đầu (hoặc thêm orchestrator mới bằng tay) |
| `serve` | Mỗi lần khởi động máy / sau khi sửa code coord |
| `watch` | Trong Monitor của session, để được đánh thức khi worker xong |
| `status` | Xem worker đang chạy, worker `IDLE`, câu hỏi đang chờ |
| `doctor` | Trước khi báo lỗi — kiểm tra key, server, repo, worktree, template |

---

## 9. Cấu hình & biến môi trường

| Biến | Mặc định | Ý nghĩa |
|------|----------|---------|
| `COORD_API_KEY` (hoặc `OPENAI_API_KEY`) | — | Key cho endpoint worker |
| `COORD_BASE_URL` (hoặc `OPENAI_BASE_URL`) | endpoint Gemini OpenAI-compatible | Base URL |
| `COORD_MODEL` | `gemini-3.8-flash` | Model cho **mọi** worker |
| `COORD_HOME` | `~/.local/share/coord` | Nơi chứa `coord.db` |
| `COORD_DIR` | tự suy ra từ git | Ghi đè vị trí `.coord/` (hiếm khi cần) |
| `WM_PORT` | `8765` | **Không dùng** — dùng `--port` |

Điểm quan trọng: **một server = một model**. Không có cấu hình model theo owner hay theo task.
Model của orchestrator do Claude Code quyết định, coord không can thiệp.

---

## 10. File được sinh ra và log

| Đường dẫn | Nội dung | Commit? |
|-----------|----------|---------|
| `wt-<tên>/` | Worktree của stream | — |
| `.coord/STATE.md` | Bảng trạng thái stream (integrator ghi) | Không (bị ignore) |
| `.coord/deps.jsonl` | Nhật ký `wait` / `resolve` | Không |
| `.coord/logs/<task-id>.md` | Transcript + report của worker | Không |
| `CLAUDE.md` | Khối luật orchestrator (trong marker) | Có |
| `AGENTS.md` | Khối luật worker + trỏ tới skill | Có |
| `.claude/settings.json` | `crossSessionInbound` + allow-list `mcp__coord__*` | Có |
| `.mcp.json` | Đăng ký MCP `coord` cho project | Có |
| `~/.local/share/coord/coord.db` | SQLite: worker, câu hỏi, report | — |

Log worker được ghi khi task kết thúc (DONE/FAILED). Không phát event tiến độ.

---

## 11. Kiểm chứng P0

Trước khi tin hệ thống, chạy các kiểm tra tương ứng doc 5:

| Kiểm tra | Lệnh | Cần |
|----------|------|-----|
| V-5 endpoint + model | `uv run python verify/p0.py` | API key |
| V-6 tool calling + schema report | `uv run python verify/p0.py` | API key |
| V-13 fork bằng replay history | `uv run python verify/p0.py` | API key |
| V-7 tool block dài (30 phút) | `uv run python verify/v7_block.py --seconds 1800` | API key |
| V-10 N worker song song | `uv run python verify/v10_concurrency.py --workers 6` | API key |
| V-11 MCP qua HTTP | `uv run python verify/mcp_smoke.py` | server đang chạy |
| V-12 N session dùng chung server | `uv run python verify/v12_multi_client.py --clients 3 --repo <project>` | server đang chạy |
| V-1…V-4, V-14, V-15 | thủ công: `/list-agents`, PING, Monitor, skill load, OS thứ hai | — |

Script nào thiếu key sẽ in `SKIP` và thoát 0.

---

## 12. Xử lý sự cố

| Triệu chứng | Nguyên nhân | Cách sửa |
|-------------|-------------|----------|
| `/mcp` không có `coord` | `coord serve` chưa chạy, sai port, hoặc project chưa có `.mcp.json` và chưa cài plugin | `curl http://127.0.0.1:8765/health`; chạy `coord serve`; chạy lại `coord init` |
| `/status` → `Peer address: unavailable` | Session không ở worktree, thiếu `crossSessionInbound`, hoặc khác môi trường | Mở đúng `wt-<tên>`; kiểm tra `.claude/settings.json`; đưa mọi session về cùng máy |
| Gửi message nhưng bên kia không phản hồi | Lệch permission mode; Desktop/IDE không hiện dialog → message hết hạn 5 phút | `crossSessionInbound: accept` và cùng permission mode |
| Worker chạy nhưng không bao giờ `DONE` | Model không gọi `report_completion` hoặc bị cắt giữa turn | Xem log gần nhất; thử `send` nhắc; nếu vẫn vậy `kill` + `spawn` |
| Worker kẹt `WAITING_ANSWER` | Event `ASK` bị lỡ | `coord status --owner A` → `answer` câu trong cột `asks` |
| `fork_worker` bị từ chối | Worker chưa `IDLE`, hoặc quá 2 tầng, hoặc server đã restart (mất conversation) | Chờ `DONE`, hoặc `spawn_worker`; sau restart luôn spawn mới |
| `send` bị từ chối | Worker đang `RUNNING` / `WAITING_ANSWER` | Chờ report, hoặc `answer` nếu đang chờ |
| `api key FAIL` trong `coord doctor` | Chưa export trước khi chạy `coord serve` | Export rồi restart server |
| Worker trả `Error code: 400` ngay | Sai `COORD_BASE_URL` hoặc tên model | Kiểm tra base URL kết thúc bằng `/v1beta/openai/` (Gemini) hoặc `/v1` |
| Worktree thiếu `AGENTS.md`/`CLAUDE.md` | `init --no-commit` hoặc commit bootstrap fail | `git config user.name/user.email` rồi chạy lại `coord init` |
| Server in `ClosedResourceError` khi client ngắt | Noise của thư viện `mcp` 1.x khi đóng SSE | Bỏ qua, không ảnh hưởng |
| Merge conflict liên tục | File ownership chồng nhau | Phân lại `files`/stream trong `STATE.md` |

---

## 13. Giới hạn hiện tại

Đây là **MVP**, khác tài liệu thiết kế ở các điểm có chủ ý:

1. **Không có guardrail** (V-9 bị bỏ): worker làm đúng những gì task nói, không sandbox, không chặn
   `git commit`/ghi ngoài worktree. Rào chắn là quy ước trong `AGENTS.md` + orchestrator review diff.
2. **Không có model theo owner/task**: 1 server = 1 model.
3. **State toàn máy** (`COORD_HOME/coord.db`) thay vì `<repo>/.coord/wm.db`.
4. **Không khôi phục conversation sau restart**: worker cũ mất context, phải `spawn_worker` mới.
5. **Không có service manager, cost tracking, retry/backoff** (P4).
6. **Không ép** ≤2 worker `RUNNING`/owner — chỉ là khuyến nghị trong skill.
7. `fork` = replay history; fork sâu làm prompt dài và tốn token.

---

## 14. Vận hành thường ngày

| Việc | Cách làm |
|------|----------|
| Xem worker đang làm gì | `coord status --owner A`, hoặc mở `.coord/logs/<task>.md` |
| Tạm dừng một stream | Bảo orchestrator dừng sau task hiện tại; peer đang chờ nhận `BLOCKED` |
| Reset context worker | `kill(worker_id)` rồi `spawn_worker` mới |
| Restart manager | Dừng/khởi động lại `coord serve`; mỗi session chạy lại mục A của skill (bật lại Monitor) |
| Thêm orchestrator D | `git worktree add ../wt-D -b stream-D` → `/rename D` → bootstrap |
| Kết thúc phiên làm việc | Đóng session, dừng server; `.coord/`, `coord.db`, git vẫn còn |
| Dọn state | Xoá `~/.local/share/coord/coord.db` (mất lịch sử worker) |

---

## 15. Chạy như service (tuỳ chọn)

Linux (systemd user):

```ini
# ~/.config/systemd/user/coord.service
[Unit]
Description=coord Worker Manager

[Service]
ExecStart=%h/.local/bin/coord serve
EnvironmentFile=%h/.config/coord.env
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
install -m 600 /dev/null ~/.config/coord.env   # rồi thêm COORD_API_KEY=...
systemctl --user daemon-reload
systemctl --user enable --now coord
journalctl --user -u coord -f
```

macOS: launchd agent với `KeepAlive=true`. Windows: NSSM hoặc Task Scheduler "At log on" +
"Restart on failure".

---

## 16. Phát triển

```bash
uv sync
uv run python -m unittest discover -s tests -v
uv run coord serve
```

Cấu trúc:

```
coord/            package: cli, manager (MCP), backend (worker loop), store (SQLite), models
coord/templates/  file copy vào project khi init
skills/           skill của plugin (orchestrator, integrator, repo-conventions)
commands/         /coord:init, /coord:up, /coord:status, /coord:doctor
.claude-plugin/   manifest plugin + marketplace
verify/           kiểm chứng P0
tests/            smoke tests (không cần API key)
```

Khi sửa protocol hoặc schema, tăng version trong `coord/__init__.py` (`PROTOCOL_VERSION`) và cập
nhật doc 3.
