"""Worker backends for `coord` (Protocol v0.5, ADR-014/ADR-020).

A worker is a **full-harness coding agent**, never a toy chat loop. Two drivers
implement the same `WorkerSession` contract:

* ``agy-sdk``  — ``google-antigravity`` (**mặc định**). ``BuiltinTools``,
  ``skills_paths``, ``policies`` (workspace + shell guards), ``conversation_id``
  resume. Chạy Gemini / Vertex.
* ``claude-sdk`` — ``claude-agent-sdk`` (tuỳ chọn). Claude Code harness, project
  skills, project MCP servers, ``can_use_tool`` policy hook, ``resume``.

Cả hai driver đều:
* đi qua :func:`coord.policy.check_tool_policy` (V-9);
* đặt ``session.report`` đúng một lần qua ``on_report``;
* nhận một khối *Project context* trong system prompt (worktree, branch, tài
  liệu cần đọc trước khi làm).
"""

from __future__ import annotations

import abc
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from .models import TaskPacket, WorkerReport
from .paths import TEMPLATES, coord_dir, coord_home, env, git  # noqa: F401 (re-export)
from .policy import check_tool_policy, extract_command, extract_paths

DEFAULT_THINKING = "medium"

DEFAULT_MODELS = {
    "agy-sdk": "gemini-3.8-flash-medium",
    "claude-sdk": "claude-sonnet-4-5",
}
DEFAULT_BACKENDS = ("agy-sdk", "claude-sdk")

MAX_STEPS = 200

# Names of the coordinator MCP tools that a worker must never see or call (P-1).
COORD_TOOL_NAMES = ["spawn_worker", "send", "answer", "status", "kill"]
DISALLOWED_TOOL_PATTERNS = [
    *[f"mcp__coord__{name}" for name in COORD_TOOL_NAMES],
    *[f"mcp__worker-manager__{name}" for name in COORD_TOOL_NAMES],
]


class BackendUnavailable(RuntimeError):
    """The selected backend's SDK (or its credentials) is missing."""


# --------------------------------------------------------------------- config
def default_backend() -> str:
    return (env("WORKER_BACKEND", "COORD_BACKEND", default="agy-sdk") or "agy-sdk").lower()


def default_model(backend: str | None = None) -> str:
    kind = (backend or default_backend()).lower()
    model = (
        env(
            "WORKER_MODEL",
            "COORD_MODEL",
            default=DEFAULT_MODELS.get(kind, "gemini-3.8-flash-medium"),
        )
        or ""
    )
    thinking = env("WORKER_THINKING", "COORD_THINKING", default=DEFAULT_THINKING)
    if kind == "agy-sdk" and thinking and model == "gemini-3.8-flash":
        return f"{model}-{thinking}"
    return model


# ------------------------------------------------------------- system prompt
def project_context(worktree: Path, owner: str) -> str:
    """Khối ngữ cảnh project mà worker luôn thấy (không phụ thuộc prompt của ai)."""
    root = Path(worktree)
    try:
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    except Exception:  # noqa: BLE001 - không phải git repo vẫn phải chạy được
        branch = "(không phải git repo)"

    hints = [
        name
        for name in ("README.md", "CLAUDE.md", "AGENTS.md", "pyproject.toml", "package.json")
        if (root / name).is_file()
    ]
    coord_state = coord_dir(root) / "STATE.md"
    if coord_state.is_file():
        hints.append(".coord/STATE.md (trạng thái các stream)")

    lines = [
        "## Project context",
        "",
        f"- Bạn làm việc trong worktree: `{root}`",
        f"- Branch: `{branch}`",
        f"- Stream/owner: `{owner}`",
        "",
        "Trước khi bắt tay vào task, đọc vừa đủ để hiểu project:",
    ]
    if hints:
        lines += [f"- `{name}`" for name in hints]
    else:
        lines.append("- liệt kê thư mục gốc rồi đọc file mô tả project nếu có")
    lines += [
        "",
        "Bạn được phép đọc bất kỳ file nào trong worktree để hiểu ngữ cảnh; chỉ được **ghi**",
        "trong worktree và không được commit/merge (policy hook sẽ chặn).",
    ]
    return "\n".join(lines)


def build_instructions(worktree: Path, *, owner: str, with_skill_body: bool) -> str:
    """AGENTS.md + Project context (+ worker-protocol nếu backend không tự nạp)."""
    root = Path(worktree)
    parts: list[str] = []

    agents = root / "AGENTS.md"
    if agents.is_file():
        parts.append(agents.read_text(encoding="utf-8"))

    parts.append(project_context(root, owner))

    if with_skill_body:
        protocol = TEMPLATES / "agents" / "skills" / "worker-protocol" / "SKILL.md"
        if protocol.is_file():
            parts.append(protocol.read_text(encoding="utf-8"))

    return "\n\n".join(parts)


def _project_mcp_servers(worktree: Path) -> dict[str, Any]:
    """`.mcp.json` của worktree, bỏ mọi server điều phối (P-1)."""
    data: dict[str, Any] = {}
    path = Path(worktree) / ".mcp.json"
    if path.is_file():
        try:
            data.update(
                json.loads(path.read_text(encoding="utf-8") or "{}").get("mcpServers", {})
            )
        except (json.JSONDecodeError, OSError):
            pass
    return {
        key: value
        for key, value in data.items()
        if key not in {"coord", "worker-manager"} and isinstance(value, dict)
    }


# ------------------------------------------------------------------ interface
class WorkerSession(abc.ABC):
    backend_name = "?"

    #: backend này tự nạp `.agents/skills` nên không cần chèn skill vào prompt
    loads_skills_natively = False

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

    def system_prompt(self) -> str:
        # `agy-sdk` nạp `.agents/skills` native; chỉ bỏ phần chèn khi project thực sự có skill.
        native = self.loads_skills_natively and any(
            (self.worktree / ".agents" / "skills").glob("*/SKILL.md")
        )
        return build_instructions(
            self.worktree,
            owner=self.owner,
            with_skill_body=not native,
        )

    # --------------------------------------------------------------- contract
    @abc.abstractmethod
    async def run(self, text: str) -> None:
        """Chạy một lượt tới khi xong. Set `self.report` hoặc raise."""

    async def close(self) -> None:  # noqa: D401 - optional hook
        return None

    def persistable_messages(self) -> list[dict[str, Any]] | None:
        """Chỉ dùng cho test/khôi phục thủ công; SDK drivers trả None."""
        return None

    def transcript(self) -> str:
        return ""


# ---------------------------------------------------------------- claude sdk
class ClaudeSDKWorkerSession(WorkerSession):
    backend_name = "claude-sdk"

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

        key = env("WORKER_API_KEY", "COORD_API_KEY", "ANTHROPIC_API_KEY")
        base = env("WORKER_BASE_URL", "COORD_BASE_URL", "ANTHROPIC_BASE_URL")
        if not key:
            raise BackendUnavailable(
                "Backend `claude-sdk` cần WORKER_API_KEY hoặc COORD_API_KEY (hoặc ANTHROPIC_API_KEY). "
                "Đặt biến này trong file config/env hoặc shell."
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
            name="coord_worker", version="0.5.0", tools=self._tool_functions()
        )
        thinking_effort = env("WORKER_THINKING", "COORD_THINKING", default=DEFAULT_THINKING)
        return ClaudeAgentOptions(
            cwd=str(self.worktree),
            model=self.model,
            effort=thinking_effort,  # type: ignore[arg-type]
            env=environment,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self.system_prompt(),
            },
            mcp_servers={"coord_worker": server, **_project_mcp_servers(self.worktree)},
            strict_mcp_config=True,
            # Chỉ project: không mang skill/MCP cá nhân của người dùng vào worker.
            setting_sources=["project"],
            skills="all",
            disallowed_tools=DISALLOWED_TOOL_PATTERNS,
            allowed_tools=[
                "mcp__coord_worker__ask_orchestrator",
                "mcp__coord_worker__report_completion",
            ],
            permission_mode="default",
            can_use_tool=can_use_tool,
            resume=self.sdk_session_id,
            max_turns=MAX_STEPS,
        )

    # ---------------------------------------------------------------- run
    async def run(self, text: str) -> None:
        from claude_agent_sdk import ResultMessage, query

        self.report = None
        async for message in query(prompt=text, options=self._options()):
            if isinstance(message, ResultMessage):
                if message.session_id:
                    self.sdk_session_id = message.session_id
                if message.is_error:
                    raise RuntimeError((message.result or "Claude SDK turn failed")[:500])

        if self.report is None:
            raise RuntimeError("worker kết thúc lượt mà không gọi report_completion")


# ----------------------------------------------------------- antigravity sdk
class AntigravityWorkerSession(WorkerSession):
    backend_name = "agy-sdk"
    loads_skills_natively = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._agent: Any = None
        self._resume = bool(self.sdk_session_id)

    # ------------------------------------------------------------- config
    def _policies(self):  # noqa: ANN202
        from google.antigravity import BuiltinTools
        from google.antigravity.hooks import policy

        def outside(args: Any) -> bool:
            data = args if isinstance(args, dict) else getattr(args, "model_dump", lambda: {})()
            paths = extract_paths(data)
            return any(not self._allowed("write", {"path": p})[0] for p in paths)

        def bad_command(args: Any) -> bool:
            data = args if isinstance(args, dict) else getattr(args, "model_dump", lambda: {})()
            command = extract_command(data)
            return bool(command) and not self._allowed("bash", {"command": command})[0]

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
                if spec.get("url"):
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

        key = env("WORKER_API_KEY", "COORD_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        if not key:
            raise BackendUnavailable(
                "Backend `agy-sdk` cần WORKER_API_KEY hoặc COORD_API_KEY (hoặc GEMINI_API_KEY). "
                "Đặt biến này trong file config/env hoặc shell."
            )

        base = env("WORKER_BASE_URL", "COORD_BASE_URL", "GEMINI_BASE_URL")
        harness_env: dict[str, str] = {}
        models = None
        if base:
            from google.antigravity import types

            # Gemini SDK / localharness tự động append /v1beta/..., chuẩn hoá bỏ trailing /v1 hoặc /
            norm_base = base.rstrip("/")
            if norm_base.endswith("/v1"):
                norm_base = norm_base[:-3].rstrip("/")
            elif norm_base.endswith("/v1beta"):
                norm_base = norm_base[:-7].rstrip("/")

            harness_env["GEMINI_BASE_URL"] = norm_base
            harness_env["GOOGLE_GENAI_BASE_URL"] = norm_base
            harness_env["BASE_URL"] = norm_base
            os.environ.setdefault("GEMINI_BASE_URL", norm_base)
            os.environ.setdefault("GOOGLE_GENAI_BASE_URL", norm_base)

            model_name = self.model or default_model("agy-sdk")
            endpoint = types.GeminiAPIEndpoint(base_url=norm_base, api_key=key)
            models = [
                types.ModelTarget(name=model_name, types=[types.ModelType.TEXT], endpoint=endpoint),
                types.ModelTarget(name=model_name, types=[types.ModelType.IMAGE], endpoint=endpoint),
            ]

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

        config_kwargs: dict[str, Any] = dict(
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
            env=harness_env or None,
        )
        if models:
            config_kwargs["models"] = models
        else:
            config_kwargs["model"] = self.model

        return LocalAgentConfig(**config_kwargs)

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
        agent = Agent(self._config())
        self._agent = agent
        async with agent:
            response = await agent.chat(text)
            try:
                await response.text()
            except TypeError:  # một số bản SDK trả về str
                pass
            except Exception:  # noqa: BLE001 - report mới là thứ quan trọng
                pass

        if self.report is None:
            raise RuntimeError("worker kết thúc lượt mà không gọi report_completion")

    async def close(self) -> None:
        self._agent = None


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
    if kind == "agy-sdk":
        return AntigravityWorkerSession(**kwargs)
    if kind == "claude-sdk":
        return ClaudeSDKWorkerSession(**kwargs)
    raise BackendUnavailable(
        f"COORD_BACKEND={backend!r} không hợp lệ; chọn một trong {list(DEFAULT_BACKENDS)}."
    )


def backend_status() -> list[tuple[str, bool, str]]:
    """Dùng bởi `coord doctor`; không bao giờ raise."""
    out: list[tuple[str, bool, str]] = []
    for name, module in (("agy-sdk", "google.antigravity"), ("claude-sdk", "claude_agent_sdk")):
        try:
            __import__(module)
            out.append((name, True, f"{module} đã cài"))
        except Exception as exc:  # noqa: BLE001
            out.append((name, False, f"{type(exc).__name__}: {exc}"))
    return out
