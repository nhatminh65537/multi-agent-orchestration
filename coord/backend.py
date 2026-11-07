"""Worker backends for `coord` (Protocol v0.6, ADR-014/ADR-020).

A worker is a **full-harness coding agent**, never a toy chat loop. Two drivers
implement the same `WorkerSession` contract:

* ``agy-sdk``  — ``google-antigravity`` (**default**). ``BuiltinTools``,
  ``skills_paths``, ``policies`` (workspace + shell guards), ``conversation_id``
  resume. Runs Gemini / Vertex.
* ``claude-sdk`` — ``claude-agent-sdk`` (optional). Claude Code harness, project
  skills, project MCP servers, ``can_use_tool`` policy hook, ``resume``.

Both drivers:

* go through :func:`coord.policy.check_tool_policy` (V-9);
* **return the agent's final answer verbatim** (`run()` -> `str`) as the task
  result — there is no `report_completion` tool and no `WorkerReport`;
* switch to ``WORKER_FALLBACK_MODEL`` when a model is quota-limited, and wait
  for quota to recover (``WORKER_QUOTA_WAIT_SECONDS``) before giving up;
* receive a *Project context* block in their system prompt (worktree, branch,
  documents worth reading first).
"""

from __future__ import annotations

import abc
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import (
    DEFAULT_BACKENDS,
    api_key,
    base_url,
    default_model,
    fallback_models,
    quota_wait_seconds,
    thinking,
)
from .models import TaskPacket
from .paths import TEMPLATES, coord_dir, coord_home, git
from .policy import (
    WRITE_TOOLS,
    check_tool_policy,
    extract_paths,
    normalise_tool_name,
)

MAX_STEPS = 200

# Provider errors worth retrying: quota exhausted, rate limited, overloaded.
RETRYABLE_RE = re.compile(
    r"429|resource[ _-]?exhausted|quota|rate[ _-]?limit|too many requests|"
    r"overloaded|503|529|service unavailable|temporarily unavailable",
    re.IGNORECASE,
)

# `retryDelay: "37s"`, `retry in 37.5s`, `retry-after: 30`, `please wait 12 seconds`.
_RETRY_AFTER_RE = re.compile(
    r"(?:retry[_ -]?(?:after|delay)[\"' :=]*|retry in\s+|please wait\s+)(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Names of the coordinator MCP tools that a worker must never see or call (P-1).
COORD_TOOL_NAMES = ["spawn_worker", "send", "answer", "status", "kill"]
DISALLOWED_TOOL_PATTERNS = [
    *[f"mcp__coord__{name}" for name in COORD_TOOL_NAMES],
    *[f"mcp__worker-manager__{name}" for name in COORD_TOOL_NAMES],
]


class BackendUnavailable(RuntimeError):
    """The selected backend's SDK (or its credentials) is missing."""


class WorkerAPIError(RuntimeError):
    """A provider/API failure, classified so the manager can report it accurately.

    `retryable` marks quota/rate-limit/overload errors; `retry_after` is the
    delay the provider itself asked for, when it told us.
    """

    def __init__(
        self, message: str, *, retryable: bool, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def parse_retry_after(message: str) -> float | None:
    """Seconds the provider asked us to wait, parsed out of the error text."""
    match = _RETRY_AFTER_RE.search(message or "")
    if not match:
        return None
    try:
        return max(0.0, float(match.group(1)))
    except ValueError:  # pragma: no cover - regex only yields numbers
        return None


def classify_error(exc: BaseException) -> WorkerAPIError:
    """Wrap any SDK/HTTP exception so the original provider text is preserved."""
    if isinstance(exc, WorkerAPIError):
        return exc
    message = f"{type(exc).__name__}: {exc}".strip()
    return WorkerAPIError(
        message[:2000],
        retryable=bool(RETRYABLE_RE.search(message)),
        retry_after=parse_retry_after(message),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------- system prompt
def project_context(worktree: Path, owner: str) -> str:
    """Project context block every worker sees, independent of its own prompt."""
    root = Path(worktree)
    try:
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    except Exception:  # noqa: BLE001 - must work outside a git repo too
        branch = "(not a git repository)"

    hints = [
        name
        for name in ("README.md", "CLAUDE.md", "AGENTS.md", "pyproject.toml", "package.json")
        if (root / name).is_file()
    ]
    coord_state = coord_dir(root) / "STATE.md"
    if coord_state.is_file():
        hints.append(".coord/STATE.md (per-stream status)")

    lines = [
        "## Project context",
        "",
        f"- You work in the worktree: `{root}`",
        f"- Branch: `{branch}`",
        f"- Stream/owner: `{owner}`",
        "",
        "Before starting the task, read just enough to understand the project:",
    ]
    if hints:
        lines += [f"- `{name}`" for name in hints]
    else:
        lines.append("- list the repository root, then read the project description files if any")
    lines += [
        "",
        "You may read any file inside the worktree to understand the context; you may only **write**",
        "inside the worktree, and you may not commit/merge (the policy hook blocks it).",
    ]
    return "\n".join(lines)


def build_instructions(worktree: Path, *, owner: str, with_skill_body: bool) -> str:
    """AGENTS.md + Project context (+ worker-protocol when the backend does not load it)."""
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
    """The worktree's `.mcp.json`, minus every orchestration server (P-1)."""
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

    #: True when the backend loads `.agents/skills` itself, so the skill body is not inlined.
    loads_skills_natively = False

    def __init__(
        self,
        *,
        worker_id: str,
        owner: str,
        worktree: Path,
        model: str,
        packet: TaskPacket | None = None,
        ask: Callable[[str], Awaitable[str]],
        notify: Callable[[str], None] | None = None,
        session_id: str | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.owner = owner
        self.worktree = Path(worktree)
        self.model = model
        self.task_id = packet.id if packet else ""
        self.packet_files = list(packet.files) if packet else []
        self._ask = ask
        self._notify_cb = notify
        self._needs_approval = list(packet.needs_approval) if packet else []
        self._read_only = packet.read_only if packet else False
        self.sdk_session_id = session_id
        # Progress + inbox: the manager reads these directly, no tokens spent.
        self.inbox: list[str] = []
        self.touched_files: set[str] = set()
        # Denied tool calls (tool + argument keys + reason). The SDK only tells
        # the model the policy *name*, so the manager log carries the detail.
        self.denials: list[str] = []
        self.steps = 0
        self.last_tool = ""
        self.last_activity = _now()

    def __deepcopy__(self, memo: Any) -> WorkerSession:
        """Never let pydantic/SDK code deep-copy a live session.

        ``LocalAgentConfig.model_copy(deep=True)`` recurses into bound-method
        tools and policy closures that reference ``self``. Copying the live
        agent (``asyncio.Lock`` / ``_thread.RLock``) made the second turn on an
        ``IDLE`` worker die with ``cannot pickle '_thread.RLock' object``.
        """
        return self

    # ---------------------------------------------------------------- helpers
    def _check(self, tool_name: str, tool_input: dict[str, Any] | None) -> tuple[bool, str]:
        """Pure policy check — used inside SDK policy predicates."""
        return check_tool_policy(
            tool_name,
            tool_input,
            self.worktree,
            self._needs_approval,
            allow_read_outside=False,
            read_only=self._read_only,
        )

    def _allowed(self, tool_name: str, tool_input: dict[str, Any] | None) -> tuple[bool, str]:
        """Policy check + progress bookkeeping (steps, last tool, touched files)."""
        allowed, reason = self._check(tool_name, tool_input)
        if allowed:
            self._record(tool_name, tool_input)
        return allowed, reason

    def _record(self, tool_name: str, tool_input: dict[str, Any] | None) -> None:
        """Track progress and written files; costs no tokens at all."""
        self.steps += 1
        self.last_tool = tool_name
        self.last_activity = _now()
        if normalise_tool_name(tool_name) in WRITE_TOOLS:
            self.touched_files.update(extract_paths(tool_input or {}))

    def _notify(self, line: str) -> None:
        """Forward a worker-side event (fallback, quota wait) to the orchestrator."""
        if self._notify_cb is not None:
            self._notify_cb(line)

    def apply_packet(self, packet: TaskPacket) -> None:
        """Adopt a new TaskPacket sent through `send()` on a warm worker."""
        self.task_id = packet.id
        self.packet_files = list(packet.files)
        self._needs_approval = list(packet.needs_approval)
        self._read_only = packet.read_only
        self.touched_files.clear()
        self.denials.clear()

    def system_prompt(self) -> str:
        # `agy-sdk` loads `.agents/skills` natively; only skip inlining when the project has skills.
        native = self.loads_skills_natively and any(
            (self.worktree / ".agents" / "skills").glob("*/SKILL.md")
        )
        return build_instructions(
            self.worktree,
            owner=self.owner,
            with_skill_body=not native,
        )

    # --------------------------------------------------------------- contract
    async def run(self, text: str) -> str:
        """Run one turn and return the agent's final answer.

        A failing model is retried on the next ``WORKER_FALLBACK_MODEL``. When
        every model is quota-limited/rate-limited/overloaded, the worker waits
        for the provider to recover (``WORKER_QUOTA_WAIT_SECONDS``) and retries,
        reporting each wait to the orchestrator. Cancellation (`kill`) and
        missing credentials are never retried.
        """
        candidates: list[str] = []
        for model in [self.model, *fallback_models()]:
            if model and model not in candidates:
                candidates.append(model)

        wait_left = quota_wait_seconds()
        attempt = 0
        while True:
            attempt += 1
            failure: WorkerAPIError | None = None
            for index, model in enumerate(candidates):
                self.model = model
                try:
                    return await self._run_turn(text, model)
                except (asyncio.CancelledError, BackendUnavailable):
                    raise
                except Exception as exc:  # noqa: BLE001 - every SDK/API error is reported
                    failure = classify_error(exc)
                    if failure.retryable and index + 1 < len(candidates):
                        self._notify(
                            f"[{self.worker_id}] model `{model}` failed ({failure}); "
                            f"switching to fallback `{candidates[index + 1]}`"
                        )
                        continue
                    break

            if failure is None or not failure.retryable or wait_left <= 0:
                raise failure or BackendUnavailable("no model configured")

            # Provider told us how long to wait, otherwise back off 5s, 10s, 20s... (max 60s).
            delay = failure.retry_after or min(60.0, 5.0 * (2 ** min(attempt - 1, 4)))
            delay = max(0.5, min(delay, wait_left))
            self._notify(
                f"[{self.worker_id}] quota/rate limit on `{self.model}` ({failure}). "
                f"Waiting {delay:.0f}s before retry #{attempt} "
                f"({wait_left:.0f}s of quota budget left)"
            )
            await asyncio.sleep(delay)
            wait_left -= delay

    @abc.abstractmethod
    async def _run_turn(self, text: str, model: str) -> str:
        """Run one SDK turn with `model` and return the agent's final text."""

    async def close(self) -> None:  # noqa: D401 - optional hook
        return None


# ---------------------------------------------------------------- claude sdk
class ClaudeSDKWorkerSession(WorkerSession):
    backend_name = "claude-sdk"

    # ------------------------------------------------------------- tools
    def _tool_functions(self):  # noqa: ANN202 - SDK types imported lazily
        from claude_agent_sdk import tool

        @tool(
            "ask_orchestrator",
            "Ask your orchestrator a question when information is missing. "
            "Blocks up to 30 minutes.",
            {"question": str},
        )
        async def ask_orchestrator_tool(args: dict[str, Any]) -> dict[str, Any]:
            answer = await self._ask(str(args.get("question") or ""))
            return {"content": [{"type": "text", "text": answer}]}

        return [ask_orchestrator_tool]

    # ------------------------------------------------------------- options
    def _options(self, model: str):  # noqa: ANN202
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

        key = api_key()
        if not key:
            raise BackendUnavailable(
                "Backend `claude-sdk` requires WORKER_API_KEY in .env or the shell."
            )

        environment = {"ANTHROPIC_API_KEY": key}
        if base := base_url():
            environment["ANTHROPIC_BASE_URL"] = base

        async def can_use_tool(tool_name: str, input_data: dict[str, Any], _context: Any = None):
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

            allowed, reason = self._allowed(tool_name, input_data)
            if allowed:
                return PermissionResultAllow()
            return PermissionResultDeny(message=reason, interrupt=False)

        server = create_sdk_mcp_server(
            name="coord_worker", version="0.6.0", tools=self._tool_functions()
        )
        return ClaudeAgentOptions(
            cwd=str(self.worktree),
            model=model,
            effort=thinking(),  # type: ignore[arg-type]
            env=environment,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self.system_prompt(),
            },
            mcp_servers={"coord_worker": server, **_project_mcp_servers(self.worktree)},
            strict_mcp_config=True,
            # Project scope only: never pull the user's personal skills/MCP servers into a worker.
            setting_sources=["project"],
            skills="all",
            disallowed_tools=DISALLOWED_TOOL_PATTERNS,
            allowed_tools=["mcp__coord_worker__ask_orchestrator"],
            permission_mode="default",
            can_use_tool=can_use_tool,
            resume=self.sdk_session_id,
            max_turns=MAX_STEPS,
        )

    # ---------------------------------------------------------------- run
    async def _run_turn(self, text: str, model: str) -> str:
        from claude_agent_sdk import ResultMessage, query

        result_text = ""
        async for message in query(prompt=text, options=self._options(model)):
            if isinstance(message, ResultMessage):
                if message.session_id:
                    self.sdk_session_id = message.session_id
                if message.is_error:
                    raise RuntimeError((message.result or "Claude SDK turn failed")[:500])
                result_text = message.result or ""
        return result_text


# ----------------------------------------------------------- antigravity sdk
class AntigravityWorkerSession(WorkerSession):
    backend_name = "agy-sdk"
    loads_skills_natively = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Mint the conversation_id at construction time: `status()` shows
        # `session_id` while RUNNING, and CREATE_OR_RESUME reuses it next turn.
        self.sdk_session_id = self.sdk_session_id or uuid.uuid4().hex

    # ------------------------------------------------------------- config
    def _policies(self):  # noqa: ANN202
        from google.antigravity import BuiltinTools
        from google.antigravity.hooks import policy

        def guard(tool_name: str, args: Any) -> bool:
            """Run the policy check, record progress, return `deny?`."""
            data = args if isinstance(args, dict) else getattr(args, "model_dump", lambda: {})()
            allowed, reason = self._allowed(tool_name, data)
            if not allowed:
                # The harness shows the model only `Denied by policy '<name>'`,
                # so keep the real reason for `.coord/logs/<task>.md`.
                self.denials.append(f"`{tool_name}` args={sorted(data) or '-'} -> {reason}")
                del self.denials[:-50]  # keep the tail bounded on long turns
            return not allowed

        write_reason = (
            f"coord: a write may only target a path inside the worktree {self.worktree}. "
            "Use a relative path (it resolves inside the worktree); if the task needs "
            "another file, call ask_orchestrator. read_only tasks may not write at all."
        )
        read_reason = (
            f"coord: reads must stay inside the worktree {self.worktree}; "
            "call ask_orchestrator if you need another file."
        )
        shell_reason = (
            "coord: workers may not run git commit/merge/push/rebase/checkout/switch/"
            "reset/clean/worktree, nor a command listed in needs_approval. The "
            "orchestrator reviews `git diff` and commits."
        )

        return [
            policy.allow_all(),
            policy.deny(
                BuiltinTools.INVOKE_SUBAGENT.value,
                name="coord-no-subagents",
                reason="coord: workers may not spawn subagents; do the work yourself.",
            ),
            policy.deny(
                BuiltinTools.CREATE_FILE.value,
                when=lambda args: guard(BuiltinTools.CREATE_FILE.value, args),
                name="coord-worktree-write",
                reason=write_reason,
            ),
            policy.deny(
                BuiltinTools.EDIT_FILE.value,
                when=lambda args: guard(BuiltinTools.EDIT_FILE.value, args),
                name="coord-worktree-edit",
                reason=write_reason,
            ),
            policy.deny(
                BuiltinTools.VIEW_FILE.value,
                when=lambda args: guard(BuiltinTools.VIEW_FILE.value, args),
                name="coord-worktree-read",
                reason=read_reason,
            ),
            policy.deny(
                BuiltinTools.RUN_COMMAND.value,
                when=lambda args: guard(BuiltinTools.RUN_COMMAND.value, args),
                name="coord-shell",
                reason=shell_reason,
            ),
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

    def _config(self, model: str):  # noqa: ANN202
        from google.antigravity import LocalAgentConfig, types

        key = api_key()
        if not key:
            raise BackendUnavailable(
                "Backend `agy-sdk` requires WORKER_API_KEY in .env or the shell."
            )

        base = base_url()
        harness_env: dict[str, str] = {}
        models = None
        if base:
            # The Gemini SDK appends /v1beta/ itself; strip a trailing /v1 or /v1beta.
            norm_base = base.rstrip("/")
            if norm_base.endswith("/v1"):
                norm_base = norm_base[:-3].rstrip("/")
            elif norm_base.endswith("/v1beta"):
                norm_base = norm_base[:-7].rstrip("/")

            harness_env = {
                "GEMINI_BASE_URL": norm_base,
                "GOOGLE_GENAI_BASE_URL": norm_base,
                "BASE_URL": norm_base,
            }
            endpoint = types.GeminiAPIEndpoint(base_url=norm_base, api_key=key)
            models = [
                types.ModelTarget(
                    name=model, types=[types.ModelType.TEXT], endpoint=endpoint
                ),
                types.ModelTarget(
                    name=model, types=[types.ModelType.IMAGE], endpoint=endpoint
                ),
            ]

        try:
            from google.antigravity.types import SessionContinuationMode

            mode: Any = SessionContinuationMode.CREATE_OR_RESUME
        except Exception:  # pragma: no cover - SDK drift
            mode = "create_or_resume"

        saves = coord_home() / "conversations"
        saves.mkdir(parents=True, exist_ok=True)

        config_kwargs: dict[str, Any] = dict(
            system_instructions=self.system_prompt(),
            tools=[self._tool_ask],
            policies=self._policies(),
            workspaces=[str(self.worktree)],
            skills_paths=[str(self.worktree / ".agents" / "skills")],
            mcp_servers=self._mcp_servers() or None,
            conversation_id=self.sdk_session_id,
            session_continuation_mode=mode,
            save_dir=str(saves),
            app_data_dir=str(saves),
            api_key=key,
            env=harness_env or None,
        )
        if models:
            config_kwargs["models"] = models
        else:
            config_kwargs["model"] = model

        return LocalAgentConfig(**config_kwargs)

    # ------------------------------------------------------------- tools
    async def _tool_ask(self, question: str) -> str:
        """Ask your orchestrator a question. Blocks up to 30 minutes."""
        return await self._ask(question)

    # ---------------------------------------------------------------- run
    async def _run_turn(self, text: str, model: str) -> str:
        from google.antigravity import Agent

        async with Agent(self._config(model)) as agent:
            response = await agent.chat(text)
            return await response.text()


# ------------------------------------------------------------------- factory
def build_session(
    backend: str,
    *,
    worker_id: str,
    owner: str,
    worktree: Path,
    model: str | None,
    packet: TaskPacket | None,
    ask: Callable[[str], Awaitable[str]],
    notify: Callable[[str], None] | None = None,
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
        notify=notify,
        session_id=session_id,
    )
    if kind == "agy-sdk":
        return AntigravityWorkerSession(**kwargs)
    if kind == "claude-sdk":
        return ClaudeSDKWorkerSession(**kwargs)
    raise BackendUnavailable(
        f"WORKER_BACKEND={backend!r} is invalid; pick one of {list(DEFAULT_BACKENDS)}."
    )


def backend_status() -> list[tuple[str, bool, str]]:
    """Used by `coord doctor`; never raises."""
    out: list[tuple[str, bool, str]] = []
    for name, module in (("agy-sdk", "google.antigravity"), ("claude-sdk", "claude_agent_sdk")):
        try:
            __import__(module)
            out.append((name, True, f"{module} installed"))
        except Exception as exc:  # noqa: BLE001
            out.append((name, False, f"{type(exc).__name__}: {exc}"))
    return out
