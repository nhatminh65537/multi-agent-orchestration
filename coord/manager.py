"""Worker Manager: MCP tools, owner checks, durable event stream.

One process serves the whole machine. Tools take `worktree` (absolute path) so a
single server manages many projects; `coord.db` lives in `COORD_HOME`, logs live
in each project's `.coord/logs/`.

Protocol v0.6 behaviour:

* workers **answer in free-form prose** at the end of a turn; `session.run()` ->
  `str` is the task result (a leading `FAILED:` means failure) — there is no
  `report_completion` any more;
* `send()` accepts an `IDLE` worker (new turn), a `RUNNING` worker (queued in
  `inbox`, runs right after the current turn) and a `WAITING_ANSWER` worker
  (the text answers the pending question);
* provider/API errors (quota, rate limit, overload) are classified, waited out
  by the worker and forwarded to the orchestrator when the worker gives up;
* durable, replayable event stream (ADR-017);
* state cleanup on restart (BUG-04);
* `files` overlap guard on the same worktree (ADR-019);
* the **manager** writes `.coord/logs/<task>.md`; workers never write it (ADR-016);
* `kill` is not overridden by the `finally` of `ask_orchestrator` (BUG-06a).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .backend import BackendUnavailable, WorkerSession, build_session, classify_error
from .config import default_backend, default_model
from .ipc import async_send_session_message
from .models import TaskPacket, validate_owner
from .paths import coord_dir, coord_home, project_root
from .policy import describe_policy, resolve_target
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
        # Tests inject a fake factory; production leaves this None and uses build_session.
        self.session_factory = session_factory

        self.sessions: dict[str, WorkerSession] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.pending: dict[str, asyncio.Future[str]] = {}

        if restore:
            self.restore()

    # ------------------------------------------------------------------ restore
    def restore(self) -> None:
        """Bring the DB in sync with an empty RAM (V-8, BUG-04)."""
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
        """Persist an event and push it straight into the Claude Code session (ADR-021)."""
        self.store.add_event(owner, line)
        self._notify_session(owner, push_message or line)

    def _notify_session(self, owner: str, text: str) -> None:
        """Push an event into the matching Claude Code session over IPC (ADR-021)."""
        if not owner or owner == "?":
            return

        async def _dispatch() -> None:
            try:
                ok, error = await async_send_session_message(owner, text)
            except Exception as exc:  # noqa: BLE001 - must never kill the manager
                ok, error = False, f"{type(exc).__name__}: {exc}"
            if not ok:
                # A silently lost DONE/ASK is exactly the failure seen in the field reports.
                print(f"[coord] IPC delivery to session {owner!r} failed: {error}", file=sys.stderr)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_dispatch())
        except RuntimeError:
            pass

    # ----------------------------------------------------------------- workers
    def _require(self, worker_id: str, owner: str) -> dict[str, Any]:
        row = self.store.worker(worker_id)
        if row is None:
            raise ValueError(f"no such worker: {worker_id}")
        if row["owner"] != owner:
            raise ValueError(f"{worker_id} belongs to owner {row['owner']!r}, not {owner!r}")
        return row

    def _assert_no_overlap(self, worktree: Path, packet: TaskPacket) -> None:
        """ADR-019 / R-13: two workers on one worktree may not write the same files."""
        if packet.read_only or not packet.files:
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
                    f"File conflict on the same worktree with worker {row['id']}: "
                    f"{', '.join(sorted(overlap))}. Re-split `files` or set read_only=true."
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
            ask=lambda question: self._ask(worker_id, owner, question),
            notify=lambda line: self.emit(owner, line),
            session_id=session_id,
        )

    def _session_for(self, worker_id: str) -> WorkerSession:
        """Return the live session, or rebuild it from the DB (V-8)."""
        session = self.sessions.get(worker_id)
        if session is not None:
            return session
        row = self.store.worker(worker_id)
        if row is None:
            raise ValueError(f"no such worker: {worker_id}")
        if not row.get("session_id"):
            raise ValueError(
                f"no conversation left for {worker_id}; use spawn_worker for a new task"
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
            raise ValueError(
                f"{worker_id} is already running; use send to queue a message for the next turn"
            )
        self.tasks[worker_id] = asyncio.create_task(self._run(worker_id, text))

    async def _run(self, worker_id: str, text: str) -> None:
        """Run the current turn, then continue with anything queued in `session.inbox`."""
        session = self.sessions[worker_id]
        self.store.set_state(worker_id, "RUNNING")
        self._persist(worker_id, session)

        responses: list[str] = []
        current = text
        try:
            while True:
                try:
                    response = await session.run(current)
                except asyncio.CancelledError:
                    raise
                except BackendUnavailable as exc:
                    self._fail(session, str(exc))
                    return
                except Exception as exc:  # noqa: BLE001 - one broken worker must not kill the manager
                    # Keep the provider's own error text; it is forwarded verbatim to the orchestrator.
                    self._fail(session, str(classify_error(exc)))
                    return

                response = (response or "").strip()
                if not response:
                    self._fail(
                        session,
                        "worker ended the turn without returning any answer "
                        "(possible provider/quota error; see the worker log)",
                    )
                    return

                responses.append(response)
                if not session.inbox:
                    break
                current = session.inbox.pop(0)
        finally:
            self._persist(worker_id, session)

        self._finish(session, "\n\n".join(responses))
        if session.inbox:
            # A message arrived exactly as the turn ended: run the extra turn now.
            self.tasks[worker_id] = asyncio.create_task(
                self._run(worker_id, session.inbox.pop(0))
            )

    def _persist(self, worker_id: str, session: WorkerSession) -> None:
        if session.sdk_session_id:
            self.store.set_session(worker_id, session.sdk_session_id)

    # ---------------------------------------------------------------- outcome
    def _fail(self, session: WorkerSession, error: str) -> None:
        self.store.set_state(session.worker_id, "FAILED")
        log_path = self._write_log(session, None, passed=False, error=error)
        task = session.task_id or "-"
        line = f"[{session.worker_id}] FAILED {task}: {error[:200]}"
        self.emit(
            session.owner,
            line,
            push_message=f"[{session.worker_id}] FAILED {task}: {error}\nlog={log_path}",
        )

    def _finish(self, session: WorkerSession, response: str) -> None:
        """A leading `FAILED:` means failure; anything else is a DONE."""
        failed = response.lstrip().upper().startswith("FAILED:")
        warning = self._outside_files_warning(session)
        if warning:
            response = f"{response}\n\n{warning}"

        self.store.set_state(session.worker_id, "FAILED" if failed else "IDLE")
        log_path = self._write_log(session, response, passed=not failed)
        task = session.task_id or "-"
        if failed:
            first_line = response.splitlines()[0][:200]
            self.emit(
                session.owner,
                f"[{session.worker_id}] FAILED {task}: {first_line}",
                push_message=f"[{session.worker_id}] FAILED {task}: {response}\nlog={log_path}",
            )
            return
        self.emit(
            session.owner,
            f"[{session.worker_id}] DONE {task}: {response[:200]} log={log_path}",
            push_message=f"[{session.worker_id}] DONE {task}: {response}\nlog={log_path}",
        )

    def _outside_files_warning(self, session: WorkerSession) -> str:
        """Warn (never block) when the worker writes outside `TaskPacket.files`."""
        if not session.packet_files or not session.touched_files:
            return ""
        declared = {str(resolve_target(f, session.worktree)) for f in session.packet_files}
        touched = {str(resolve_target(f, session.worktree)) for f in session.touched_files}
        extra = sorted(touched - declared)
        if not extra:
            return ""
        return "⚠️ Modified files outside TaskPacket.files: " + ", ".join(extra)

    # --------------------------------------------------------------------- ask
    async def _ask(self, worker_id: str, owner: str, question: str) -> str:
        session = self.sessions.get(worker_id)
        task_id = session.task_id if session is not None else ""
        if not task_id:
            row = self.store.worker(worker_id) or {}
            task_id = row.get("task_id") or ""
        qid = uuid.uuid4().hex[:6]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self.pending[qid] = future
        self.store.add_question(qid, worker_id, task_id, question)
        self.store.set_state(worker_id, "WAITING_ANSWER")
        line = f"[{worker_id}] ASK q-{qid} {task_id}: {question[:120]}"
        prompt = (
            f"[{worker_id}] ASK q-{qid} {task_id}: {question}\n\n"
            f'Answer with: `mcp__coord__answer(owner="{owner}", '
            f'question_id="{qid}", text="...")` — or just '
            f'`send(owner="{owner}", worker_id="{worker_id}", text="...")`.'
        )
        self.emit(owner, line, push_message=prompt)
        try:
            return await asyncio.wait_for(future, timeout=self.ask_timeout)
        except asyncio.TimeoutError:
            return "NO_ANSWER: proceed with a stated assumption."
        finally:
            self.pending.pop(qid, None)
            row = self.store.worker(worker_id)
            # Never resurrect a worker that was killed while waiting (BUG-06a).
            if row and row["state"] == "WAITING_ANSWER":
                self.store.set_state(worker_id, "RUNNING")

    # -------------------------------------------------------------------- log
    def _write_log(
        self,
        session: WorkerSession,
        response: str | None,
        *,
        passed: bool,
        error: str | None = None,
    ) -> str:
        """ADR-016: the Manager owns `.coord/logs/<task-id>.md`."""
        logs = coord_dir(session.worktree) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        task = session.task_id or "task"
        path = logs / f"{task}.md"

        lines = [
            f"# {task}",
            "",
            f"- worker: `{session.worker_id}`",
            f"- owner: `{session.owner}`",
            f"- worktree: `{session.worktree}`",
            f"- backend: `{session.backend_name}`",
            f"- model: `{session.model}`",
            f"- status: `{'DONE' if passed else 'FAILED'}`",
            f"- session: `{session.sdk_session_id or '-'}`",
            "",
            "## policy",
            "",
            describe_policy(session.worktree, session._needs_approval),  # noqa: SLF001
        ]
        if error:
            lines += ["", "## error", "", "```", error, "```"]
        if response:
            lines += ["", "## response", "", response]
        if session.touched_files:
            lines += ["", "## touched files", ""]
            lines += [f"- `{item}`" for item in sorted(session.touched_files)]

        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    # -------------------------------------------------------------- operations
    async def spawn_worker(self, owner: str, worktree: str, task: str) -> str:
        validate_owner(owner)
        wt = Path(worktree).expanduser().resolve()
        if not wt.is_dir():
            raise ValueError(f"worktree does not exist: {wt}")
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
            session_id=session.sdk_session_id,
            files=packet.files,
            read_only=packet.read_only,
        )
        self._start(worker_id, task)
        return worker_id

    async def send(self, owner: str, worker_id: str, text: str) -> str:
        """Send a message or a new task — works in every live worker state."""
        validate_owner(owner)
        row = self._require(worker_id, owner)
        state = row["state"]

        if state == "WAITING_ANSWER":
            pending = self.store.pending_questions(worker_id)
            if pending:
                # The orchestrator never has to remember the q-xxx id: `send` answers for it.
                return await self.answer(owner, pending[0], text)

        if state == "RUNNING":
            session = self.sessions.get(worker_id)
            if session is None:
                raise ValueError(
                    f"{worker_id} is RUNNING but the Manager holds no session "
                    "(restarted?); use spawn_worker for a new task"
                )
            session.inbox.append(text)
            return "queued (delivered to the worker right after the current turn)"

        if state == "KILLED":
            raise ValueError(f"{worker_id} was killed; use spawn_worker for a new task")

        try:
            packet: TaskPacket | None = TaskPacket.model_validate_json(text)
        except ValueError:
            packet = None

        session = self._session_for(worker_id)
        if packet is not None:
            self._assert_no_overlap(Path(row["worktree"]), packet)
            session.apply_packet(packet)
            self.store.set_task(
                worker_id, packet.id, files=packet.files, read_only=packet.read_only
            )

        self._start(worker_id, text)
        return "ok"

    async def answer(self, owner: str, question_id: str, text: str) -> str:
        validate_owner(owner)
        qid = question_id[2:] if question_id.startswith("q-") else question_id
        row = self.store.question(qid)
        if row is None:
            raise ValueError(f"no such question: {question_id}")
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

    def status(
        self,
        owner: str | None = None,
        project: str | None = None,
        include_all: bool = False,
    ) -> list[dict[str, Any]]:
        """Worker list with progress. `FAILED`/`KILLED` rows are hidden by default."""
        rows = self.store.list_workers(owner=owner, project=project)
        out: list[dict[str, Any]] = []
        for row in rows:
            if not include_all and row["state"] in {"FAILED", "KILLED"}:
                continue
            session = self.sessions.get(row["id"])
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
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    # Progress comes from the live session; costs no tokens.
                    "steps": session.steps if session else 0,
                    "last_tool": session.last_tool if session else None,
                    "last_activity": session.last_activity if session else None,
                    # BUG-04: read from SQLite, not from in-RAM futures.
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
        """Send a message or a new TaskPacket to a worker.

        Works in every live state: `IDLE` starts a new turn, `RUNNING` queues the
        text for right after the current turn, `WAITING_ANSWER` answers the
        pending ask_orchestrator question.
        """
        return await manager.send(owner, worker_id, text)

    @mcp.tool()
    async def answer(owner: str, question_id: str, text: str) -> str:
        """Answer a pending ask_orchestrator question (id with or without the 'q-' prefix)."""
        return await manager.answer(owner, question_id, text)

    @mcp.tool()
    async def status(
        owner: str | None = None,
        project: str | None = None,
        include_all: bool = False,
    ) -> list[dict[str, Any]]:
        """List workers: state, progress, backend, current task, pending questions."""
        return manager.status(owner=owner, project=project, include_all=include_all)

    @mcp.tool()
    async def kill(owner: str, worker_id: str) -> str:
        """Stop a worker. Orchestrators must have the owner's approval before calling this."""
        return await manager.kill(owner, worker_id)

    @mcp.tool()
    async def message_session(target: str, text: str) -> str:
        """Send a message directly into a peer Claude Code session by exact name, PID, or session ID."""
        ok, err = await async_send_session_message(target, text)
        if ok:
            return f"sent to {target}"
        return f"failed: {err}"

    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(request: Request) -> JSONResponse:
        owner = request.query_params.get("owner") or None
        project = request.query_params.get("project") or None
        include_all = request.query_params.get("include_all", "").lower() in {"1", "true", "yes"}
        return JSONResponse(
            manager.status(owner=owner, project=project, include_all=include_all)
        )

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
