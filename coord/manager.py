"""Worker Manager: MCP tools, owner checks, durable event stream.

Một tiến trình phục vụ toàn máy. Tool nhận `worktree` (đường dẫn tuyệt đối) nên
một server quản được nhiều project; `coord.db` nằm trong `COORD_HOME`, log nằm ở
`.coord/logs/` của từng project.

Hành vi Protocol v0.5:

* chọn backend + khôi phục session (V-8);
* event bền vững, fan-out, replay (ADR-017);
* dọn trạng thái khi khởi động lại (BUG-04);
* chặn xung đột `files` trên cùng worktree (ADR-019);
* **Manager** ghi `.coord/logs/<task>.md`, worker không ghi (ADR-016);
* `kill` không bị `finally` của `ask_orchestrator` ghi đè (BUG-06a).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .backend import (
    BackendUnavailable,
    WorkerSession,
    build_session,
    default_backend,
    default_model,
)
from .ipc import async_send_session_message
from .models import TaskPacket, WorkerReport, validate_owner
from .paths import coord_dir, coord_home, project_root  # noqa: F401 (re-export)
from .policy import describe_policy
from .store import Store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 30303

RESTART_ANSWER = "SERVER_RESTARTED"

SessionFactory = Callable[..., WorkerSession]


class Manager:
    def __init__(
        self,
        store: Store,
        *,
        backend: str | None = None,
        model: str | None = None,
        ask_timeout: int = 1800,
        restore: bool = True,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.store = store
        self.backend = (backend or default_backend()).lower()
        self.model = model or default_model(self.backend)
        self.ask_timeout = ask_timeout
        # Chỉ dùng cho test; production để None và dùng build_session.
        self.session_factory = session_factory

        self.sessions: dict[str, WorkerSession] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.pending: dict[str, asyncio.Future[str]] = {}

        if restore:
            self.restore()

    # ------------------------------------------------------------------ restore
    def restore(self) -> None:
        """Đồng bộ DB với RAM rỗng (V-8, BUG-04)."""
        for question in self.store.pending_question_rows():
            self.store.answer_question(question["id"], RESTART_ANSWER)
        for row in self.store.states():
            if row["state"] in {"RUNNING", "WAITING_ANSWER"}:
                self.store.set_state(row["id"], "FAILED")
                self.store.add_event(
                    row.get("owner") or "?",
                    f"[{row['id']}] FAILED {row.get('task_id') or '-'}: "
                    "Worker Manager restarted while task was in progress",
                )

    # ------------------------------------------------------------------ events
    def emit(self, owner: str, line: str, push_message: str | None = None) -> None:
        """Lưu event vào store và đẩy thẳng vào Claude Code session (ADR-021)."""
        self.store.add_event(owner, line, delivered=True)
        self._notify_session(owner, push_message or line)

    def _notify_session(self, owner: str, text: str) -> None:
        """Đẩy event trực tiếp vào session Claude Code tương ứng qua IPC (ADR-021)."""
        if not owner or owner == "?":
            return

        async def _dispatch() -> None:
            try:
                await async_send_session_message(owner, text)
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_dispatch())
        except RuntimeError:
            pass

    # ----------------------------------------------------------------- workers
    def _require(self, worker_id: str, owner: str) -> dict[str, Any]:
        row = self.store.worker(worker_id)
        if row is None:
            raise ValueError(f"worker không tồn tại: {worker_id}")
        if row["owner"] != owner:
            raise ValueError(f"{worker_id} thuộc owner {row['owner']!r}, không phải {owner!r}")
        return row

    def _assert_no_overlap(self, worktree: Path, packet: TaskPacket) -> None:
        """ADR-019 / R-13: hai worker ghi trên cùng worktree không được trùng file."""
        if packet.is_read_only or not packet.files:
            return
        mine = {str(item).strip() for item in packet.files if str(item).strip()}
        for row in self.store.active_workers_on(str(worktree)):
            if row.get("read_only"):
                continue
            try:
                theirs = set(json.loads(row["files_json"] or "[]"))
            except json.JSONDecodeError:
                theirs = set()
            overlap = mine & {str(item).strip() for item in theirs}
            if overlap:
                raise ValueError(
                    f"Xung đột file trên cùng worktree với worker {row['id']}: "
                    f"{', '.join(sorted(overlap))}. Chia lại `files` hoặc đặt read_only=true."
                )

    def _new_session(
        self,
        worker_id: str,
        owner: str,
        worktree: Path,
        packet: TaskPacket | None,
        *,
        session_id: str | None = None,
    ) -> WorkerSession:
        factory = self.session_factory or build_session
        return factory(
            self.backend,
            worker_id=worker_id,
            owner=owner,
            worktree=worktree,
            model=self.model,
            packet=packet,
            ask=lambda task_id, question: self._ask(worker_id, owner, task_id, question),
            on_report=lambda report: self._record_report(worker_id, report),
            session_id=session_id,
        )

    def _session_for(self, worker_id: str) -> WorkerSession:
        """Lấy session đang sống, hoặc dựng lại từ DB (V-8)."""
        session = self.sessions.get(worker_id)
        if session is not None:
            return session
        row = self.store.worker(worker_id)
        if row is None:
            raise ValueError(f"worker không tồn tại: {worker_id}")
        if not row.get("session_id"):
            raise ValueError(
                f"không còn conversation cho {worker_id}; dùng spawn_worker cho task mới"
            )
        session = self._new_session(
            worker_id,
            row["owner"],
            Path(row["worktree"]),
            None,
            session_id=row.get("session_id"),
        )
        self.sessions[worker_id] = session
        return session

    def _start(self, worker_id: str, text: str) -> None:
        previous = self.tasks.get(worker_id)
        if previous is not None and not previous.done():
            raise ValueError(f"{worker_id} đang chạy; chờ report hoặc dùng answer")
        self.tasks[worker_id] = asyncio.create_task(self._run(worker_id, text))

    async def _run(self, worker_id: str, text: str) -> None:
        session = self.sessions[worker_id]
        owner = session.owner
        self.store.set_state(worker_id, "RUNNING")
        try:
            await session.run(text)
        except asyncio.CancelledError:
            raise
        except BackendUnavailable as exc:
            self.store.set_state(worker_id, "FAILED")
            self.emit(owner, f"[{worker_id}] FAILED {session.task_id or '-'}: {str(exc)[:200]}")
            self._write_log(session, None, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - một worker hỏng không được giết manager
            self.store.set_state(worker_id, "FAILED")
            self.emit(owner, f"[{worker_id}] FAILED {session.task_id or '-'}: {str(exc)[:200]}")
            self._write_log(session, None, error=str(exc))
        finally:
            self._persist(worker_id, session)

    def _persist(self, worker_id: str, session: WorkerSession) -> None:
        if session.sdk_session_id:
            self.store.set_session(worker_id, session.sdk_session_id)
        messages = session.persistable_messages()
        if messages is not None:
            self.store.set_messages(worker_id, messages)

    # --------------------------------------------------------------------- ask
    async def _ask(self, worker_id: str, owner: str, task_id: str, question: str) -> str:
        qid = uuid.uuid4().hex[:6]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self.pending[qid] = future
        self.store.add_question(qid, worker_id, task_id, question)
        self.store.set_state(worker_id, "WAITING_ANSWER")
        line = f"[{worker_id}] ASK q-{qid} {task_id}: {question[:120]}"
        prompt = (
            f"[{worker_id}] ASK q-{qid} {task_id}: {question}\n\n"
            f"Trả lời bằng tool: `mcp__coord__answer(owner=\"{owner}\", question_id=\"{qid}\", text=\"...\")`"
        )
        self.emit(owner, line, push_message=prompt)
        try:
            return await asyncio.wait_for(future, timeout=self.ask_timeout)
        except asyncio.TimeoutError:
            return "NO_ANSWER: proceed with a stated assumption."
        finally:
            self.pending.pop(qid, None)
            row = self.store.worker(worker_id)
            # Không "hồi sinh" worker đã bị kill khi đang chờ (BUG-06a).
            if row and row["state"] == "WAITING_ANSWER":
                self.store.set_state(worker_id, "RUNNING")

    # ------------------------------------------------------------------ report
    async def _record_report(self, worker_id: str, report: WorkerReport) -> None:
        row = self.store.worker(worker_id)
        owner = row["owner"] if row else "?"
        session = self.sessions.get(worker_id)
        state = "IDLE" if report.status == "DONE" else "FAILED"
        self.store.set_state(worker_id, state)
        self.store.add_report(worker_id, report.id, report.status, report.model_dump())
        log_path = self._write_log(session, report) if session else "-"
        if session is not None:
            self._persist(worker_id, session)
        if report.status == "DONE":
            line = f"[{worker_id}] DONE {report.id}: {report.summary[:200]} log={log_path}"
            push = f"[{worker_id}] DONE {report.id}: {report.summary}\nlog={log_path}"
            self.emit(owner, line, push_message=push)
        else:
            line = f"[{worker_id}] FAILED {report.id}: {report.summary[:200]}"
            self.emit(owner, line)

    def _write_log(
        self,
        session: WorkerSession,
        report: WorkerReport | None,
        error: str | None = None,
    ) -> str:
        """ADR-016: Manager sở hữu `.coord/logs/<task-id>.md`."""
        logs = coord_dir(session.worktree) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        task = (report.id if report else session.task_id) or "task"
        path = logs / f"{task}.md"

        lines = [
            f"# {task}",
            "",
            f"- worker: `{session.worker_id}`",
            f"- owner: `{session.owner}`",
            f"- worktree: `{session.worktree}`",
            f"- backend: `{session.backend_name}`",
            f"- model: `{session.model}`",
            f"- status: `{(report.status if report else 'FAILED')}`",
            f"- session: `{session.sdk_session_id or '-'}`",
            "",
            "## policy",
            "",
            describe_policy(session.worktree, session._needs_approval),  # noqa: SLF001
        ]
        if error:
            lines += ["", "## error", "", "```", error, "```"]
        if report:
            lines += ["", "## report", "", "```json", report.model_dump_json(indent=2), "```"]
        transcript = session.transcript()
        if transcript:
            lines += ["", "## transcript", "", transcript]

        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    # -------------------------------------------------------------- operations
    async def spawn_worker(self, owner: str, worktree: str, task: str) -> str:
        validate_owner(owner)
        wt = Path(worktree).expanduser().resolve()
        if not wt.is_dir():
            raise ValueError(f"worktree không tồn tại: {wt}")
        packet = TaskPacket.model_validate_json(task)
        self._assert_no_overlap(wt, packet)

        worker_id = f"{owner}-{uuid.uuid4().hex[:4]}"
        session = self._new_session(worker_id, owner, wt, packet)
        self.sessions[worker_id] = session
        self.store.add_worker(
            worker_id,
            owner,
            str(project_root(wt)),
            str(wt),
            packet.id,
            backend=self.backend,
            files=packet.files,
            read_only=packet.is_read_only,
        )
        self._start(worker_id, task)
        return worker_id

    async def send(self, owner: str, worker_id: str, text: str) -> str:
        validate_owner(owner)
        row = self._require(worker_id, owner)
        if row["state"] in {"RUNNING", "WAITING_ANSWER"}:
            raise ValueError(f"{worker_id} đang {row['state']}; chờ report hoặc dùng answer")

        try:
            packet: TaskPacket | None = TaskPacket.model_validate_json(text)
        except ValueError:
            packet = None

        session = self._session_for(worker_id)
        if packet is not None:
            self._assert_no_overlap(Path(row["worktree"]), packet)
            session.task_id = packet.id
            session._needs_approval = list(packet.needs_approval)  # noqa: SLF001
            session._read_only = packet.is_read_only  # noqa: SLF001
            self.store.set_task(worker_id, packet.id)
            self.store.set_files(worker_id, packet.files)

        self._start(worker_id, text)
        return "ok"

    async def answer(self, owner: str, question_id: str, text: str) -> str:
        validate_owner(owner)
        qid = question_id[2:] if question_id.startswith("q-") else question_id
        row = self.store.question(qid)
        if row is None:
            raise ValueError(f"không có câu hỏi {question_id}")
        self._require(row["worker"], owner)
        self.store.answer_question(qid, text)

        future = self.pending.get(qid)
        if future is not None and not future.done():
            future.set_result(text)
        return "ok"

    async def kill(self, owner: str, worker_id: str) -> str:
        validate_owner(owner)
        self._require(worker_id, owner)

        task = self.tasks.pop(worker_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        session = self.sessions.pop(worker_id, None)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

        for qid in self.store.pending_questions(worker_id):
            future = self.pending.pop(qid, None)
            if future is not None and not future.done():
                future.set_result("KILLED")
            self.store.answer_question(qid, "KILLED")

        self.store.set_state(worker_id, "KILLED")
        self.store.set_files(worker_id, [])
        return "ok"

    def status(self, owner: str | None = None, project: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.list_workers(owner=owner, project=project)
        out = []
        for row in rows:
            out.append(
                {
                    "worker_id": row["id"],
                    "owner": row["owner"],
                    "project": row["project"],
                    "state": row["state"],
                    "task_id": row["task_id"],
                    "worktree": row["worktree"],
                    "backend": row.get("backend"),
                    "session_id": row.get("session_id"),
                    "files": json.loads(row.get("files_json") or "[]"),
                    # BUG-04: đọc từ SQLite, không lọc theo future trong RAM.
                    "pending_questions": self.store.pending_questions(row["id"]),
                }
            )
        return out


# --------------------------------------------------------------------- MCP app
def build_app(manager: Manager, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> FastMCP:
    mcp = FastMCP("coord", host=host, port=port)

    @mcp.tool()
    async def spawn_worker(owner: str, worktree: str, task: str) -> str:
        """Spawn a full-harness worker bound to `worktree` (absolute path) with a TaskPacket (JSON).

        `owner` is your session name. Returns the new worker id immediately.
        """
        return await manager.spawn_worker(owner, worktree, task)

    @mcp.tool()
    async def send(owner: str, worker_id: str, text: str) -> str:
        """Send a follow-up message or a new TaskPacket into the same conversation."""
        return await manager.send(owner, worker_id, text)

    @mcp.tool()
    async def answer(owner: str, question_id: str, text: str) -> str:
        """Answer a pending ask_orchestrator question (id with or without the 'q-' prefix)."""
        return await manager.answer(owner, question_id, text)

    @mcp.tool()
    async def status(owner: str | None = None, project: str | None = None) -> list[dict[str, Any]]:
        """List workers: state, backend, current task, pending questions."""
        return manager.status(owner=owner, project=project)

    @mcp.tool()
    async def kill(owner: str, worker_id: str) -> str:
        """Stop a worker. Orchestrators must have the owner's approval before calling this."""
        return await manager.kill(owner, worker_id)

    @mcp.tool()
    async def message_session(target: str, text: str) -> str:
        """Send a message directly into a peer Claude Code session by name, PID, or session ID."""
        ok, err = await async_send_session_message(target, text)
        if ok:
            return f"sent to {target}"
        return f"failed: {err}"

    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(request: Request) -> JSONResponse:
        owner = request.query_params.get("owner") or None
        project = request.query_params.get("project") or None
        return JSONResponse(manager.status(owner=owner, project=project))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp


def build_manager(
    model: str | None = None, backend: str | None = None, restore: bool = True
) -> Manager:
    home = coord_home()
    home.mkdir(parents=True, exist_ok=True)
    return Manager(Store(home / "coord.db"), backend=backend, model=model, restore=restore)
