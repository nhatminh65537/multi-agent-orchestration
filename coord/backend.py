"""Worker backends for `coord` (Protocol v0.4, ADR-014).

A worker is a **full-harness coding agent**, never a toy chat loop. Three drivers
implement the same `WorkerSession` contract:

* ``claude-sdk``  — ``claude-agent-sdk`` (default). Native Read/Edit/Write/Bash/
  Glob/Grep, project skills, project MCP servers, ``can_use_tool`` policy hook,
  ``resume`` and ``fork_session``.
* ``agy-sdk``     — ``google-antigravity``. ``BuiltinTools``, ``skills_paths``,
  ``policies`` (workspace + shell guards), ``conversation_id`` resume.
* ``openai-loop`` — **deprecated** fallback: an OpenAI-compatible chat loop with
  six tools. Kept only so the test-suite and offline demos run without an SDK.

Every driver routes tool calls through :func:`coord.policy.check_tool_policy`
(V-9). Every driver sets ``session.report`` exactly once, via ``on_report``.
"""

from __future__ import annotations

import abc
import asyncio
import json
import os
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from .models import TaskPacket, WorkerReport
from .paths import TEMPLATES, coord_dir, coord_home, env, project_root  # noqa: F401 (re-export)
from .policy import check_tool_policy, extract_command, extract_paths

DEFAULT_MODELS = {
    "claude-sdk": "claude-sonnet-4-5",
    "agy-sdk": "gemini-3.8-flash",
    "openai-loop": "gemini-3.8-flash",
}
DEFAULT_BACKENDS = ("claude-sdk", "agy-sdk", "openai-loop")

MAX_STEPS = 200
SHELL_TIMEOUT = 600
MAX_TOOL_OUTPUT = 40_000
MAX_READ = 80_000
PROMPT_NUDGE_LIMIT = 2

# Names of the coordinator MCP tools that a worker must never see or call (P-1).
COORD_TOOL_NAMES = [
    "spawn_worker",
    "fork_worker",
    "send",
    "answer",
    "kill",
]
DISALLOWED_TOOL_PATTERNS = [
    *[f"mcp__coord__{name}" for name in COORD_TOOL_NAMES],
    *[f"mcp__worker-manager__{name}" for name in COORD_TOOL_NAMES],
]


class BackendUnavailable(RuntimeError):
    """The selected backend's SDK (or its credentials) is missing."""


_WARNED_LEGACY = False


# --------------------------------------------------------------------- config
def default_backend() -> str:
    return (env("COORD_BACKEND", default="claude-sdk") or "claude-sdk").lower()


def default_model(backend: str | None = None) -> str:
    kind = (backend or default_backend()).lower()
    return env("COORD_MODEL", default=DEFAULT_MODELS.get(kind, "claude-sonnet-4-5")) or ""


def make_client():  # noqa: ANN201 - AsyncOpenAI, imported lazily
    """OpenAI-compatible client for the legacy driver."""
    from openai import AsyncOpenAI

    key = env("COORD_API_KEY", "OPENAI_API_KEY")
    base = env("COORD_BASE_URL", "OPENAI_BASE_URL", default="https://generativelanguage.googleapis.com/v1beta/openai/")
    # A placeholder keeps the client constructible so `coord doctor` and the
    # MCP server start without credentials; calls fail loudly at request time.
    return AsyncOpenAI(api_key=key or "missing", base_url=base)


def build_instructions(worktree: Path, *, with_skill_body: bool = True) -> str:
    """AGENTS.md + the packaged worker-protocol skill."""
    root = Path(worktree)
    parts: list[str] = []
    agents = root / "AGENTS.md"
    if agents.is_file():
        parts.append(agents.read_text(encoding="utf-8"))
    if with_skill_body:
        protocol = TEMPLATES / "agents" / "skills" / "worker-protocol" / "SKILL.md"
        if protocol.is_file():
            parts.append(protocol.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [cắt bớt {len(text) - limit} ký tự]"


def _project_mcp_servers(worktree: Path) -> dict[str, Any]:
    """`.mcp.json` of the worktree, minus any coordinator server (P-1)."""
    data: dict[str, Any] = {}
    for name in (".mcp.json",):
        path = Path(worktree) / name
        if not path.is_file():
            continue
        try:
            data.update(json.loads(path.read_text(encoding="utf-8") or "{}").get("mcpServers", {}))
        except (json.JSONDecodeError, OSError):
            continue
    return {
        key: value
        for key, value in data.items()
        if key not in {"coord", "worker-manager"} and isinstance(value, dict)
    }


# ------------------------------------------------------------------ interface
class WorkerSession(abc.ABC):
    backend_name = "?"

    def __init__(
        self,
        *,
        worker_id: str,
        owner: str,
        worktree: Path,
        model: str,
        packet: TaskPacket | None = None,
        ask: Callable[[str, str], Awaitable[str]],
        on_report: Callable[[WorkerReport], Awaitable[None]],
        session_id: str | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.owner = owner
        self.worktree = Path(worktree)
        self.model = model
        self.task_id = packet.id if packet else ""
        self._ask = ask
        self._on_report = on_report
        self._needs_approval = list(packet.needs_approval) if packet else []
        self._read_only = packet.is_read_only if packet else False
        self.report: WorkerReport | None = None
        self.sdk_session_id = session_id

    # ---------------------------------------------------------------- helpers
    def _allowed(self, tool_name: str, tool_input: dict[str, Any] | None) -> tuple[bool, str]:
        return check_tool_policy(
            tool_name,
            tool_input,
            self.worktree,
            self._needs_approval,
            allow_read_outside=False,
        )

    async def _accept_report(self, raw: Any) -> str:
        if self.report is not None:
            return "ERROR: report_completion đã được gọi cho task này"
        try:
            report = WorkerReport.model_validate(raw)
        except ValidationError as exc:
            return f"ERROR: report không hợp lệ, sửa và gọi lại:\n{exc}"
        self.report = report
        await self._on_report(report)
        return "recorded"

    def system_prompt(self, *, with_skill_body: bool = True) -> str:
        return build_instructions(self.worktree, with_skill_body=with_skill_body)

    # --------------------------------------------------------------- contract
    @abc.abstractmethod
    async def run(self, text: str) -> None:
        """Run one turn to completion. Sets `self.report` or raises."""

    @abc.abstractmethod
    def fork(self, *, new_worker_id: str, packet: TaskPacket) -> "WorkerSession":
        """Return a new session that inherits this conversation."""

    async def close(self) -> None:  # noqa: D401 - optional hook
        return None

    def persistable_messages(self) -> list[dict[str, Any]] | None:
        """Only the legacy driver stores raw messages."""
        return None

    def transcript(self) -> str:
        return ""


# ------------------------------------------------------------ legacy: openai
def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SPECS: list[dict[str, Any]] = [
    _tool(
        "read_file",
        "Đọc một file văn bản trong worktree. Trả về nội dung (có thể bị cắt bớt).",
        {"path": {"type": "string", "description": "Đường dẫn tương đối so với worktree"}},
        ["path"],
    ),
    _tool(
        "write_file",
        "Ghi (tạo mới hoặc ghi đè) một file trong worktree.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _tool(
        "list_dir",
        "Liệt kê thư mục trong worktree. Thư mục con có hậu tố '/'.",
        {"path": {"type": "string", "description": "Mặc định '.'"}},
        [],
    ),
    _tool(
        "run_shell",
        "Chạy một lệnh shell với cwd = worktree (tối đa 10 phút). Trả về stdout+stderr.",
        {"command": {"type": "string"}},
        ["command"],
    ),
    _tool(
        "ask_orchestrator",
        (
            "Hỏi orchestrator khi thiếu thông tin không thể tự quyết. Blocking tới 30 phút. "
            "Với khoảng trống nhỏ, hãy tự giả định và ghi vào assumptions thay vì gọi tool này."
        ),
        {"task_id": {"type": "string"}, "question": {"type": "string"}},
        ["task_id", "question"],
    ),
    _tool(
        "report_completion",
        "Gọi đúng MỘT lần khi task kết thúc. Không chứa nội dung file, chỉ tóm tắt + đường dẫn.",
        {
            "report": {
                "type": "object",
                "required": ["id", "status", "summary"],
                "properties": {
                    "id": {"type": "string"},
                    "status": {"enum": ["DONE", "FAILED"]},
                    "summary": {"type": "string", "maxLength": 600},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                    "files_changed": {"type": "array", "items": {"type": "string"}},
                    "diff_stat": {"type": "string"},
                    "tests": {"type": "string"},
                    "log": {"type": "string"},
                },
            }
        },
        ["report"],
    ),
]


class OpenAILoopSession(WorkerSession):
    """Deprecated driver. Kept for tests and offline demos; still policy-guarded."""

    backend_name = "openai-loop"

    def __init__(self, *, client: Any = None, messages: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.client = client or make_client()
        self.messages: list[dict[str, Any]] = messages or [
            {"role": "system", "content": self.system_prompt()}
        ]

    # ------------------------------------------------------------------ run
    async def run(self, text: str) -> None:
        self.report = None
        self.messages.append({"role": "user", "content": text})
        nudges = 0

        for _ in range(MAX_STEPS):
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=TOOL_SPECS,
                tool_choice="auto",
            )
            message = response.choices[0].message
            self.messages.append(message.model_dump(exclude_none=True))

            if not message.tool_calls:
                if self.report is not None:
                    return
                if nudges >= PROMPT_NUDGE_LIMIT:
                    raise RuntimeError("worker kết thúc lượt mà không gọi report_completion")
                nudges += 1
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Bạn chưa gọi report_completion. Gọi nó ngay bây giờ "
                            "với {id, status, summary} (và assumptions/tests nếu có)."
                        ),
                    }
                )
                continue

            # Bug B-1 (0.3): every tool_call_id must get a matching tool message,
            # even when report_completion arrives in the same assistant message.
            for call in message.tool_calls:
                if self.report is not None:
                    output = "skipped: task đã được report_completion kết thúc"
                else:
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError as exc:
                        output = f"ERROR: arguments không phải JSON hợp lệ: {exc}"
                    else:
                        output = await self._dispatch(call.function.name, args)
                self.messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": output}
                )

            if self.report is not None:
                return

        raise RuntimeError(f"vượt {MAX_STEPS} bước mà chưa report_completion")

    # ---------------------------------------------------------------- tools
    async def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        allowed, reason = self._allowed(name, args)
        if not allowed:
            return reason
        try:
            if name == "read_file":
                return self._read_file(str(args.get("path", "")))
            if name == "write_file":
                return self._write_file(str(args.get("path", "")), str(args.get("content", "")))
            if name == "list_dir":
                return self._list_dir(str(args.get("path", ".")))
            if name == "run_shell":
                return await self._run_shell(str(args.get("command", "")))
            if name == "ask_orchestrator":
                return await self._ask(
                    str(args.get("task_id", self.task_id)), str(args.get("question", ""))
                )
            if name == "report_completion":
                return await self._accept_report(args.get("report"))
            return f"ERROR: tool không tồn tại: {name}"
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not the process
            return f"ERROR: {type(exc).__name__}: {exc}"

    def _resolve(self, path: str) -> Path:
        target = Path(path).expanduser()
        return target if target.is_absolute() else (self.worktree / target)

    def _read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"ERROR: không tìm thấy file {path}"
        return _clip(target.read_text(encoding="utf-8", errors="replace"), MAX_READ)

    def _write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"đã ghi {len(content)} ký tự vào {path}"

    def _list_dir(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_dir():
            return f"ERROR: không tìm thấy thư mục {path}"
        entries = sorted(target.iterdir())
        return "\n".join(f"{e.name}/" if e.is_dir() else e.name for e in entries[:500]) or "(trống)"

    async def _run_shell(self, command: str) -> str:
        if not command.strip():
            return "ERROR: command rỗng"
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.worktree),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT)
        except asyncio.TimeoutError:
            _kill_tree(proc)
            await proc.wait()
            return f"ERROR: lệnh vượt quá {SHELL_TIMEOUT}s và bị dừng"
        except asyncio.CancelledError:
            # Bug B-2 (0.3): a killed worker must not leave the child running.
            _kill_tree(proc)
            await proc.wait()
            raise
        out = stdout.decode("utf-8", errors="replace") if stdout else ""
        return _clip(f"exit={proc.returncode}\n{out}" if out else f"exit={proc.returncode}")

    # --------------------------------------------------------------- persist
    def persistable_messages(self) -> list[dict[str, Any]] | None:
        return json.loads(json.dumps(self.messages))

    def clone_messages(self) -> list[dict[str, Any]]:
        """Backwards-compatible helper used by the verify scripts."""
        return json.loads(json.dumps(self.messages))

    def transcript(self) -> str:
        lines: list[str] = []
        for message in self.messages:
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
        return "\n".join(lines)

    def fork(self, *, new_worker_id: str, packet: TaskPacket) -> "OpenAILoopSession":
        child = OpenAILoopSession(
            worker_id=new_worker_id,
            owner=self.owner,
            worktree=self.worktree,
            model=self.model,
            packet=packet,
            ask=self._ask,
            on_report=self._on_report,
            client=self.client,
            messages=json.loads(json.dumps(self.messages)),
        )
        return child


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the shell *and* its children (pytest, builds, …)."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover - Windows
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------- claude sdk
class ClaudeSDKWorkerSession(WorkerSession):
    backend_name = "claude-sdk"

    def __init__(self, *, fork_from: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fork_from = fork_from

    # ------------------------------------------------------------- tools
    def _tool_functions(self):  # noqa: ANN202 - SDK types imported lazily
        from claude_agent_sdk import tool

        @tool(
            "ask_orchestrator",
            "Ask your orchestrator a question when information is missing. Blocks up to 30 minutes.",
            {"task_id": str, "question": str},
        )
        async def ask_orchestrator_tool(args: dict[str, Any]) -> dict[str, Any]:
            answer = await self._ask(
                str(args.get("task_id") or self.task_id), str(args.get("question") or "")
            )
            return {"content": [{"type": "text", "text": answer}]}

        @tool(
            "report_completion",
            "Call exactly once when the task ends, with a WorkerReport JSON object.",
            {"report": dict},
        )
        async def report_completion_tool(args: dict[str, Any]) -> dict[str, Any]:
            raw = args.get("report", args)
            result = await self._accept_report(raw)
            failed = result.startswith("ERROR")
            return {
                "content": [{"type": "text", "text": result}],
                **({"is_error": True} if failed else {}),
            }

        return [ask_orchestrator_tool, report_completion_tool]

    # ------------------------------------------------------------- options
    def _options(self):  # noqa: ANN202
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

        key = env("COORD_API_KEY", "ANTHROPIC_API_KEY")
        base = env("COORD_BASE_URL", "ANTHROPIC_BASE_URL")
        if not key:
            raise BackendUnavailable(
                "Backend `claude-sdk` cần COORD_API_KEY (hoặc ANTHROPIC_API_KEY). "
                "Đặt biến này cho `coord serve`, hoặc dùng COORD_BACKEND=agy-sdk."
            )

        environment = {"ANTHROPIC_API_KEY": key}
        if base:
            environment["ANTHROPIC_BASE_URL"] = base

        async def can_use_tool(tool_name: str, input_data: dict[str, Any], _context: Any = None):
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

            allowed, reason = self._allowed(tool_name, input_data)
            if allowed:
                return PermissionResultAllow()
            return PermissionResultDeny(message=reason, interrupt=False)

        server = create_sdk_mcp_server(
            name="coord_worker", version="0.4.0", tools=self._tool_functions()
        )
        resume = self._fork_from or self.sdk_session_id
        return ClaudeAgentOptions(
            cwd=str(self.worktree),
            model=self.model,
            env=environment,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self.system_prompt(),
            },
            mcp_servers={"coord_worker": server, **_project_mcp_servers(self.worktree)},
            strict_mcp_config=True,
            setting_sources=["project", "user"],
            skills="all",
            disallowed_tools=DISALLOWED_TOOL_PATTERNS,
            allowed_tools=[
                "mcp__coord_worker__ask_orchestrator",
                "mcp__coord_worker__report_completion",
            ],
            permission_mode="default",
            can_use_tool=can_use_tool,
            resume=resume,
            fork_session=bool(self._fork_from),
            max_turns=MAX_STEPS,
        )

    # ---------------------------------------------------------------- run
    async def run(self, text: str) -> None:
        from claude_agent_sdk import ResultMessage, query

        self.report = None
        options = self._options()
        try:
            async for message in query(prompt=text, options=options):
                if isinstance(message, ResultMessage):
                    if message.session_id:
                        self.sdk_session_id = message.session_id
                    if message.is_error:
                        raise RuntimeError(
                            (message.result or "Claude SDK turn failed")[:500]
                        )
        finally:
            self._fork_from = None

        if self.report is None:
            raise RuntimeError("worker kết thúc lượt mà không gọi report_completion")

    def fork(self, *, new_worker_id: str, packet: TaskPacket) -> "ClaudeSDKWorkerSession":
        return ClaudeSDKWorkerSession(
            worker_id=new_worker_id,
            owner=self.owner,
            worktree=self.worktree,
            model=self.model,
            packet=packet,
            ask=self._ask,
            on_report=self._on_report,
            session_id=self.sdk_session_id,
            fork_from=self.sdk_session_id,
        )


# ----------------------------------------------------------- antigravity sdk
class AntigravityWorkerSession(WorkerSession):
    backend_name = "agy-sdk"

    def __init__(self, *, fork_from: "AntigravityWorkerSession | None" = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._brief_from = fork_from
        self._agent: Any = None

    # ------------------------------------------------------------- config
    def _policies(self):  # noqa: ANN202
        from google.antigravity import BuiltinTools
        from google.antigravity.hooks import policy

        def outside(args: Any) -> bool:
            data = args if isinstance(args, dict) else getattr(args, "model_dump", lambda: {})()
            paths = extract_paths(data)
            if not paths:
                return False
            return any(not self._allowed("write", {"path": p})[0] for p in paths)

        def bad_command(args: Any) -> bool:
            data = args if isinstance(args, dict) else getattr(args, "model_dump", lambda: {})()
            command = extract_command(data)
            if not command:
                return False
            return not self._allowed("bash", {"command": command})[0]

        return [
            policy.allow_all(),
            policy.deny(BuiltinTools.INVOKE_SUBAGENT.value, name="coord-no-subagents"),
            policy.deny(BuiltinTools.CREATE_FILE.value, when=outside, name="coord-worktree-write"),
            policy.deny(BuiltinTools.EDIT_FILE.value, when=outside, name="coord-worktree-edit"),
            policy.deny(BuiltinTools.VIEW_FILE.value, when=outside, name="coord-worktree-read"),
            policy.deny(BuiltinTools.RUN_COMMAND.value, when=bad_command, name="coord-shell"),
        ]

    def _mcp_servers(self) -> list[Any]:
        try:
            from google.antigravity.types import McpStdioServer, McpStreamableHttpServer
        except Exception:  # pragma: no cover - SDK drift
            return []

        servers: list[Any] = []
        for name, spec in _project_mcp_servers(self.worktree).items():
            try:
                if str(spec.get("type", "")).startswith("http") or spec.get("url"):
                    servers.append(McpStreamableHttpServer(name=name, url=str(spec["url"])))
                elif spec.get("command"):
                    servers.append(
                        McpStdioServer(
                            name=name,
                            command=str(spec["command"]),
                            args=[str(a) for a in spec.get("args", [])],
                            env=spec.get("env") or None,
                        )
                    )
            except Exception:  # pragma: no cover - malformed project config
                continue
        return servers

    def _config(self):  # noqa: ANN202
        from google.antigravity import LocalAgentConfig

        key = env("COORD_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        if not key:
            raise BackendUnavailable(
                "Backend `agy-sdk` cần COORD_API_KEY / GEMINI_API_KEY. "
                "Đặt biến này cho `coord serve`, hoặc dùng COORD_BACKEND=claude-sdk."
            )

        session_id = self.sdk_session_id or uuid.uuid4().hex
        self.sdk_session_id = session_id

        try:
            from google.antigravity.types import SessionContinuationMode

            mode = (
                SessionContinuationMode.RESUME
                if self._resume
                else SessionContinuationMode.CREATE_ONLY
            )
        except Exception:  # pragma: no cover - SDK drift
            mode = "RESUME" if self._resume else "CREATE_ONLY"

        saves = coord_home() / "conversations"
        saves.mkdir(parents=True, exist_ok=True)

        return LocalAgentConfig(
            model=self.model,
            system_instructions=self.system_prompt(),
            tools=[self._tool_ask, self._tool_report],
            policies=self._policies(),
            workspaces=[str(self.worktree)],
            skills_paths=[str(self.worktree / ".agents" / "skills")],
            mcp_servers=self._mcp_servers() or None,
            conversation_id=session_id,
            session_continuation_mode=mode,
            save_dir=str(saves),
            app_data_dir=str(saves),
            api_key=key,
        )

    _resume = False

    # ------------------------------------------------------------- tools
    async def _tool_ask(self, task_id: str, question: str) -> str:
        """Ask your orchestrator a question. Blocks up to 30 minutes."""
        return await self._ask(task_id or self.task_id, question)

    async def _tool_report(self, report: dict[str, Any]) -> str:
        """Call exactly once when the task ends, with a WorkerReport JSON object."""
        return await self._accept_report(report)

    # ---------------------------------------------------------------- run
    async def run(self, text: str) -> None:
        from google.antigravity import Agent

        self.report = None
        if self._brief_from is not None:
            try:
                text = f"Context from a sibling worker:\n{await self._brief_from.brief()}\n\n{text}"
            except Exception:  # noqa: BLE001 - a failed brief must not block the task
                pass
            self._brief_from = None

        agent = Agent(self._config())
        self._agent = agent
        async with agent:
            response = await agent.chat(text)
            try:
                await response.text()
            except TypeError:  # some SDK builds return a plain string
                pass
            except Exception:  # noqa: BLE001 - the report is what matters
                pass

        if self.report is None:
            raise RuntimeError("worker kết thúc lượt mà không gọi report_completion")

    async def brief(self) -> str:
        """Cheap fork fallback (ADR-011 option 3): ≤400-word context brief."""
        from google.antigravity import Agent

        agent = Agent(self._config())
        async with agent:
            response = await agent.chat(
                "Write a context brief (<= 400 words) for a sibling worker continuing in this "
                "area: files you read, key facts, conventions, pitfalls. Do not call "
                "report_completion."
            )
            try:
                return str(await response.text())
            except TypeError:
                return str(getattr(response, "text", ""))

    async def close(self) -> None:
        self._agent = None

    def fork(self, *, new_worker_id: str, packet: TaskPacket) -> "AntigravityWorkerSession":
        # Antigravity has no documented fork; use the context-brief fallback, and
        # keep the parent conversation id free (a resumed id must be single-owner).
        return AntigravityWorkerSession(
            worker_id=new_worker_id,
            owner=self.owner,
            worktree=self.worktree,
            model=self.model,
            packet=packet,
            ask=self._ask,
            on_report=self._on_report,
            session_id=None,
            fork_from=self,
        )


# ------------------------------------------------------------------- factory
def build_session(
    backend: str,
    *,
    worker_id: str,
    owner: str,
    worktree: Path,
    model: str | None,
    packet: TaskPacket | None,
    ask: Callable[[str, str], Awaitable[str]],
    on_report: Callable[[WorkerReport], Awaitable[None]],
    session_id: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    restore: bool = False,
    client: Any = None,
) -> WorkerSession:
    kind = backend.lower()
    kwargs: dict[str, Any] = dict(
        worker_id=worker_id,
        owner=owner,
        worktree=Path(worktree),
        model=model or default_model(kind),
        packet=packet,
        ask=ask,
        on_report=on_report,
        session_id=session_id,
    )
    if kind == "claude-sdk":
        return ClaudeSDKWorkerSession(**kwargs)
    if kind == "agy-sdk":
        session = AntigravityWorkerSession(**kwargs)
        session._resume = bool(session_id) or restore
        return session
    if kind in {"openai-loop", "openai", "legacy"}:
        global _WARNED_LEGACY
        if not _WARNED_LEGACY:
            _WARNED_LEGACY = True
            print(
                "coord: COORD_BACKEND=openai-loop là driver LEGACY (không full-harness). "
                "Chỉ dùng cho test/khẩn cấp; xem ADR-014.",
                file=sys.stderr,
            )
        return OpenAILoopSession(messages=messages, client=client, **kwargs)
    raise BackendUnavailable(
        f"COORD_BACKEND={backend!r} không hợp lệ; chọn một trong {list(DEFAULT_BACKENDS)}."
    )


@dataclass
class BackendStatus:
    name: str
    available: bool
    detail: str


def backend_status() -> list[BackendStatus]:
    """Used by `coord doctor`; never raises."""
    out: list[BackendStatus] = []
    try:
        import claude_agent_sdk  # noqa: F401

        out.append(BackendStatus("claude-sdk", True, "claude-agent-sdk đã cài"))
    except Exception as exc:  # noqa: BLE001
        out.append(BackendStatus("claude-sdk", False, f"{type(exc).__name__}: {exc}"))
    try:
        import google.antigravity  # noqa: F401

        out.append(BackendStatus("agy-sdk", True, "google-antigravity đã cài"))
    except Exception as exc:  # noqa: BLE001
        out.append(BackendStatus("agy-sdk", False, f"{type(exc).__name__}: {exc}"))
    try:
        import openai  # noqa: F401

        out.append(BackendStatus("openai-loop", True, "openai đã cài (deprecated)"))
    except Exception as exc:  # noqa: BLE001
        out.append(BackendStatus("openai-loop", False, f"{type(exc).__name__}: {exc}"))
    return out
