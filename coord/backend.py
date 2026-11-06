"""OpenAI-compatible worker backend.

A worker is a chat loop: OpenAI-compatible endpoint + six tools + a transcript.
`report_completion` ends the loop; everything else is ordinary tool use inside
the worker's worktree.

This is deliberately thin. No sandbox, no policy hook: the worker does what the
TaskPacket says (see README "What is intentionally missing").
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI
from pydantic import ValidationError

from .models import WorkerReport

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-3.8-flash"

MAX_STEPS = 200
SHELL_TIMEOUT = 600
MAX_TOOL_OUTPUT = 40_000
MAX_READ = 80_000
PROMPT_NUDGE_LIMIT = 2

TEMPLATES = Path(__file__).parent / "templates"


# --------------------------------------------------------------------- config
def make_client() -> AsyncOpenAI:
    key = os.environ.get("COORD_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base = (
        os.environ.get("COORD_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    # A placeholder keeps the client constructible so `coord doctor` and the
    # MCP server start without credentials; calls fail loudly at request time.
    return AsyncOpenAI(api_key=key or "missing", base_url=base)


def default_model() -> str:
    return os.environ.get("COORD_MODEL") or DEFAULT_MODEL


# ------------------------------------------------------------- .coord location
def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(worktree), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def project_root(worktree: Path) -> Path:
    """Main repo that owns `worktree` (shared git common dir). Falls back to it."""
    try:
        return Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path(worktree).resolve()


def coord_dir(worktree: Path) -> Path:
    override = os.environ.get("COORD_DIR")
    if override:
        return Path(override)
    return project_root(Path(worktree)) / ".coord"


# ------------------------------------------------------------- instructions
def build_instructions(worktree: Path) -> str:
    """AGENTS.md + project skills + the packaged worker-protocol skill."""
    root = Path(worktree)
    parts: list[str] = []

    agents = root / "AGENTS.md"
    if agents.is_file():
        parts.append(agents.read_text(encoding="utf-8"))

    for skill in sorted((root / ".agents" / "skills").glob("*/SKILL.md")):
        parts.append(f"<!-- from {skill.relative_to(root)} -->\n{skill.read_text(encoding='utf-8')}")

    protocol = TEMPLATES / "agents" / "skills" / "worker-protocol" / "SKILL.md"
    if protocol.is_file():
        parts.append(protocol.read_text(encoding="utf-8"))

    return "\n\n".join(parts)


# --------------------------------------------------------------- tool schemas
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
        {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
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
        {
            "task_id": {"type": "string", "description": "Task đang làm, dạng <stream>-<n>"},
            "question": {"type": "string"},
        },
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


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [cắt bớt {len(text) - limit} ký tự]"


class WorkerSession:
    """One conversation with one worker. `run()` is one task turn."""

    def __init__(
        self,
        *,
        worker_id: str,
        owner: str,
        worktree: Path,
        model: str,
        client: AsyncOpenAI,
        ask: Callable[[str, str], Awaitable[str]],
        on_report: Callable[[WorkerReport], Awaitable[None]],
    ) -> None:
        self.worker_id = worker_id
        self.owner = owner
        self.worktree = Path(worktree)
        self.model = model
        self.client = client
        self._ask = ask
        self._on_report = on_report
        self.task_id: str = ""
        self.report: WorkerReport | None = None
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_instructions(self.worktree)}
        ]

    # ------------------------------------------------------------- lifecycle
    def clone_messages(self) -> list[dict[str, Any]]:
        return json.loads(json.dumps(self.messages))

    async def run(self, text: str) -> None:
        """Run one turn to completion. Raises if the worker never reports."""
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

            for call in message.tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    output = f"ERROR: arguments không phải JSON hợp lệ: {exc}"
                else:
                    output = await self._dispatch(name, args)
                self.messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": output}
                )
                if self.report is not None:
                    return

        raise RuntimeError(f"vượt {MAX_STEPS} bước mà chưa report_completion")

    # ------------------------------------------------------------------ tools
    def _resolve(self, path: str) -> Path:
        target = Path(path).expanduser()
        return target if target.is_absolute() else (self.worktree / target)

    async def _dispatch(self, name: str, args: dict[str, Any]) -> str:
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
                return await self._report(args.get("report"))
            return f"ERROR: tool không tồn tại: {name}"
        except ValidationError as exc:
            return f"ERROR: report không hợp lệ, sửa và gọi lại:\n{exc}"
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not the process
            return f"ERROR: {type(exc).__name__}: {exc}"

    def _read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"ERROR: không tìm thấy file {path}"
        text = target.read_text(encoding="utf-8", errors="replace")
        return _clip(text, MAX_READ)

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
        lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries[:500]]
        return "\n".join(lines) or "(trống)"

    async def _run_shell(self, command: str) -> str:
        if not command.strip():
            return "ERROR: command rỗng"
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.worktree),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"ERROR: lệnh vượt quá {SHELL_TIMEOUT}s và bị dừng"
        out = stdout.decode("utf-8", errors="replace")
        return _clip(f"exit={proc.returncode}\n{out}" if out else f"exit={proc.returncode}")

    async def _report(self, raw: Any) -> str:
        if self.report is not None:
            return "ERROR: report_completion đã được gọi cho task này"
        report = WorkerReport.model_validate(raw)
        self.report = report
        await self._on_report(report)
        return "recorded"
