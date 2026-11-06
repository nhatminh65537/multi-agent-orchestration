"""Worker Manager: MCP tools, owner checks, event stream.

One process serves the whole machine. Tools take a `worktree` (absolute path),
so a single server can manage workers for several projects; the SQLite file
lives in `COORD_HOME`, logs live in each project's `.coord/logs/`.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .backend import (
    WorkerSession,
    coord_dir,
    default_model,
    make_client,
    project_root,
)
from .models import TaskPacket, WorkerReport, validate_owner
from .store import Store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def coord_home() -> Path:
    override = os.environ.get("COORD_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "coord"


class Manager:
    def __init__(
        self,
        store: Store,
        *,
        model: str | None = None,
        ask_timeout: int = 1800,
        max_fork_depth: int = 2,
    ) -> None:
        self.store = store
        self.model = model or default_model()
        self.ask_timeout = ask_timeout
        self.max_fork_depth = max_fork_depth
        self.client = make_client()

        self.sessions: dict[str, WorkerSession] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.queues: dict[str, asyncio.Queue[str]] = {}
        self.pending: dict[str, asyncio.Future[str]] = {}

    # ------------------------------------------------------------------ events
    def queue(self, owner: str) -> asyncio.Queue[str]:
        return self.queues.setdefault(owner, asyncio.Queue())

    def emit(self, owner: str, line: str) -> None:
        self.queue(owner).put_nowait(line)

    # ----------------------------------------------------------------- workers
    def _require(self, worker_id: str, owner: str) -> dict[str, Any]:
        row = self.store.worker(worker_id)
        if row is None:
            raise ValueError(f"worker không tồn tại: {worker_id}")
        if row["owner"] != owner:
            raise ValueError(f"{worker_id} thuộc owner {row['owner']!r}, không phải {owner!r}")
        return row

    def _depth(self, worker_id: str) -> int:
        depth, cursor = 0, worker_id
        while (row := self.store.worker(cursor)) and row["parent"]:
            depth += 1
            cursor = row["parent"]
        return depth

    def _new_session(self, worker_id: str, owner: str, worktree: Path) -> WorkerSession:
        return WorkerSession(
            worker_id=worker_id,
            owner=owner,
            worktree=worktree,
            model=self.model,
            client=self.client,
            ask=lambda task_id, question: self._ask(worker_id, owner, task_id, question),
            on_report=lambda report: self._record_report(worker_id, owner, worktree, report),
        )

    def _start(self, worker_id: str, text: str) -> None:
        self.tasks[worker_id] = asyncio.create_task(self._run(worker_id, text))

    async def _run(self, worker_id: str, text: str) -> None:
        session = self.sessions[worker_id]
        owner = session.owner
        self.store.set_state(worker_id, "RUNNING")
        try:
            await session.run(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken worker must not kill the manager
            self.store.set_state(worker_id, "FAILED")
            self.emit(owner, f"[{worker_id}] FAILED {session.task_id or '-'}: {str(exc)[:200]}")
            self._write_log(session, None, error=str(exc))

    # --------------------------------------------------------------------- ask
    async def _ask(self, worker_id: str, owner: str, task_id: str, question: str) -> str:
        qid = uuid.uuid4().hex[:6]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self.pending[qid] = future
        self.store.add_question(qid, worker_id, task_id, question)
        self.store.set_state(worker_id, "WAITING_ANSWER")
        self.emit(owner, f"[{worker_id}] ASK q-{qid} {task_id}: {question[:120]}")
        try:
            return await asyncio.wait_for(future, timeout=self.ask_timeout)
        except asyncio.TimeoutError:
            return "NO_ANSWER: proceed with a stated assumption."
        finally:
            self.pending.pop(qid, None)
            row = self.store.worker(worker_id)
            if row and row["state"] == "WAITING_ANSWER":
                self.store.set_state(worker_id, "RUNNING")

    # ------------------------------------------------------------------ report
    async def _record_report(
        self, worker_id: str, owner: str, worktree: Path, report: WorkerReport
    ) -> None:
        state = "IDLE" if report.status == "DONE" else "FAILED"
        self.store.set_state(worker_id, state)
        self.store.add_report(worker_id, report.id, report.status, report.model_dump())
        log_path = self._write_log(self.sessions[worker_id], report)
        if report.status == "DONE":
            self.emit(owner, f"[{worker_id}] DONE {report.id}: {report.summary[:200]} log={log_path}")
        else:
            self.emit(owner, f"[{worker_id}] FAILED {report.id}: {report.summary[:200]}")

    def _write_log(
        self, session: WorkerSession, report: WorkerReport | None, error: str | None = None
    ) -> str:
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
            f"- status: `{(report.status if report else 'FAILED')}`",
            f"- model: `{session.model}`",
        ]
        if error:
            lines += ["", "## error", "", "```", error, "```"]
        if report:
            lines += ["", "## report", "", "```json", report.model_dump_json(indent=2), "```"]

        lines += ["", "## transcript", ""]
        for message in session.messages:
            role = message.get("role")
            if role == "system":
                continue
            content = message.get("content") or ""
            if role == "tool":
                lines.append(f"**tool result**\n\n```\n{str(content)[:4000]}\n```\n")
            elif role == "assistant":
                if message.get("tool_calls"):
                    calls = json.dumps(message["tool_calls"], ensure_ascii=False)[:4000]
                    lines.append(f"**assistant → tool**\n\n```json\n{calls}\n```\n")
                if content:
                    lines.append(f"{content}\n")
            else:
                lines.append(f"**{role}**\n\n{str(content)[:4000]}\n")

        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    # -------------------------------------------------------------- operations
    async def spawn_worker(self, owner: str, worktree: str, task: str) -> str:
        validate_owner(owner)
        wt = Path(worktree).expanduser().resolve()
        if not wt.is_dir():
            raise ValueError(f"worktree không tồn tại: {wt}")
        packet = TaskPacket.model_validate_json(task)

        worker_id = f"{owner}-{uuid.uuid4().hex[:4]}"
        session = self._new_session(worker_id, owner, wt)
        session.task_id = packet.id
        self.sessions[worker_id] = session
        self.store.add_worker(
            worker_id, owner, str(project_root(wt)), str(wt), packet.id, parent=None
        )
        self._start(worker_id, task)
        return worker_id

    async def fork_worker(self, owner: str, worker_id: str, task: str) -> str:
        validate_owner(owner)
        row = self._require(worker_id, owner)
        if row["state"] != "IDLE":
            raise ValueError(f"{worker_id} đang {row['state']}; chỉ fork worker IDLE")
        if self._depth(worker_id) + 1 > self.max_fork_depth:
            raise ValueError(f"vượt giới hạn fork {self.max_fork_depth} tầng; dùng spawn_worker")
        packet = TaskPacket.model_validate_json(task)

        source = self.sessions.get(worker_id)
        if source is None:
            raise ValueError(f"không còn conversation trong bộ nhớ cho {worker_id}")

        new_id = f"{owner}-{uuid.uuid4().hex[:4]}"
        wt = Path(row["worktree"])
        session = self._new_session(new_id, owner, wt)
        session.messages = source.clone_messages()
        session.task_id = packet.id
        self.sessions[new_id] = session
        self.store.add_worker(
            new_id, owner, row["project"], str(wt), packet.id, parent=worker_id
        )
        self._start(new_id, task)
        return new_id

    async def send(self, owner: str, worker_id: str, text: str) -> str:
        validate_owner(owner)
        row = self._require(worker_id, owner)
        if row["state"] in {"RUNNING", "WAITING_ANSWER"}:
            raise ValueError(f"{worker_id} đang {row['state']}; chờ report hoặc dùng answer")
        session = self.sessions.get(worker_id)
        if session is None:
            raise ValueError(f"không còn conversation trong bộ nhớ cho {worker_id}; spawn worker mới")

        try:
            packet = TaskPacket.model_validate_json(text)
        except ValueError:
            packet = None
        if packet is not None:
            session.task_id = packet.id
            self.store.set_task(worker_id, packet.id)

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
        for qid in self.store.pending_questions(worker_id):
            future = self.pending.pop(qid, None)
            if future is not None and not future.done():
                future.cancel()
            self.store.answer_question(qid, "KILLED")
        self.store.set_state(worker_id, "KILLED")
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
                    "parent": row["parent"],
                    "worktree": row["worktree"],
                    "pending_questions": [
                        q for q in self.store.pending_questions(row["id"]) if q in self.pending
                    ],
                }
            )
        return out


# --------------------------------------------------------------------- MCP app
def build_app(manager: Manager, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> FastMCP:
    mcp = FastMCP("coord", host=host, port=port)

    @mcp.tool()
    async def spawn_worker(owner: str, worktree: str, task: str) -> str:
        """Spawn a worker bound to `worktree` (absolute path) with a TaskPacket (JSON).

        `owner` is your session name. Returns the new worker id immediately.
        """
        return await manager.spawn_worker(owner, worktree, task)

    @mcp.tool()
    async def fork_worker(owner: str, worker_id: str, task: str) -> str:
        """Clone an IDLE worker's conversation into a new worker on the same worktree."""
        return await manager.fork_worker(owner, worker_id, task)

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
        """List workers: state, parent, current task, pending questions."""
        return manager.status(owner=owner, project=project)

    @mcp.tool()
    async def kill(owner: str, worker_id: str) -> str:
        """Stop a worker gracefully."""
        return await manager.kill(owner, worker_id)

    @mcp.custom_route("/events/{owner}", methods=["GET"])
    async def events(request: Request):  # noqa: ANN202 - starlette Response
        owner = request.path_params["owner"]
        queue = manager.queue(owner)

        async def generate():  # noqa: ANN202
            while True:
                yield (await queue.get()) + "\n"

        return StreamingResponse(generate(), media_type="text/plain")

    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(request: Request) -> JSONResponse:
        owner = request.query_params.get("owner") or None
        project = request.query_params.get("project") or None
        return JSONResponse(manager.status(owner=owner, project=project))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp


def build_manager(model: str | None = None) -> Manager:
    home = coord_home()
    home.mkdir(parents=True, exist_ok=True)
    return Manager(Store(home / "coord.db"), model=model)
