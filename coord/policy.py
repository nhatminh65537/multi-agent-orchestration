"""Policy hook (V-9) — one pure function shared by every backend driver.

Only the tool names the two real harnesses use are listed here (`agy-sdk`
`BuiltinTools` and the Claude Code harness). Rules:

* P-1  the worker may not call the coordinator MCP tools, nor spawn subagents;
* P-2  reads and writes stay inside the assigned worktree;
* P-3  a `read_only` task may not use any write tool;
* P-4  shell commands may not run history-rewriting / publishing git verbs, nor
       anything listed in `needs_approval`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Sequence

# Only the tool names the two real harnesses use: `claude-sdk` (Write/Edit/Read/Bash…)
# and `agy-sdk` `BuiltinTools` (create_file/edit_file/view_file/run_command…).
WRITE_TOOLS = frozenset(
    {
        "write", "edit", "multiedit", "notebookedit",  # claude-sdk
        "create_file", "edit_file",  # agy-sdk
        "write_file", "delete_file", "move_file", "create_directory",  # MCP-style
    }
)
READ_TOOLS = frozenset(
    {
        "read", "grep", "glob", "websearch", "webfetch",  # claude-sdk
        "view_file", "list_directory", "search_directory",  # agy-sdk
        "find_file", "search_web", "read_url_content",
    }
)
SHELL_TOOLS = frozenset({"bash", "run_command", "run_shell"})
DELEGATION_TOOLS = frozenset({"agent", "task", "invoke_subagent"})
COORD_MCP_PREFIXES = ("mcp__coord__", "mcp__worker-manager__")

FORBIDDEN_GIT_RE = re.compile(
    r"\bgit\s+(commit|merge|push|rebase|checkout|switch|reset|clean|worktree)\b",
    re.IGNORECASE,
)

_PATH_KEYS = ("file_path", "filePath", "path", "Path", "target_file", "notebook_path", "directory")
_COMMAND_KEYS = ("command", "cmd", "CommandLine", "command_line", "script", "shell")


# ------------------------------------------------------------------- helpers
def normalise_tool_name(tool_name: str) -> str:
    name = (tool_name or "").strip()
    if name.startswith("mcp__"):
        return name
    if "(" in name:  # Claude `Bash(ls:*)` style matchers
        name = name.split("(", 1)[0]
    return name.strip().lower().replace("-", "_")


def resolve_target(path: str | Path, worktree: Path) -> Path:
    """Absolute, symlink-resolved path; relative paths resolve inside `worktree`."""
    target = Path(str(path)).expanduser()
    if not target.is_absolute():
        target = worktree / target
    return target.resolve()


def is_path_inside_worktree(path: str | Path, worktree: Path) -> bool:
    root = Path(worktree).expanduser().resolve()
    try:
        target = resolve_target(path, root)
    except (OSError, ValueError):
        return False
    return target == root or target.is_relative_to(root)


def _iter_path_values(tool_input: dict[str, Any]) -> Iterable[str]:
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            yield value
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    yield item


def extract_paths(tool_input: dict[str, Any]) -> list[str]:
    return list(_iter_path_values(tool_input or {}))


def extract_command(tool_input: dict[str, Any]) -> str:
    for key in _COMMAND_KEYS:
        value = (tool_input or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value
    commands = (tool_input or {}).get("commands")
    if isinstance(commands, (list, tuple)):
        return " && ".join(str(c) for c in commands)
    return ""


# ---------------------------------------------------------------------- hook
def check_tool_policy(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    worktree: Path,
    needs_approval: Sequence[str] = (),
    *,
    allow_read_outside: bool = False,
    read_only: bool = False,
) -> tuple[bool, str]:
    """Return `(allowed, reason)`. `reason` is shown to the model when denied."""
    raw = tool_name or ""
    name = normalise_tool_name(raw)
    data = tool_input or {}
    worktree = Path(worktree)

    # P-1: no recursion into the coordinator, no spawning subagents.
    if raw.lower().startswith(COORD_MCP_PREFIXES) or name in DELEGATION_TOOLS:
        return (
            False,
            f"Policy Hook (V-9): workers may not call the coordinator tool {raw!r} "
            "(recursive spawn_worker protection).",
        )

    # P-3: a read-only task may not write anything.
    if read_only and name in WRITE_TOOLS:
        return (
            False,
            f"Policy Hook (V-9): this task is read_only, so the write tool {raw!r} is blocked.",
        )

    # P-2a: writes inside the worktree only.
    if name in WRITE_TOOLS:
        paths = extract_paths(data)
        if not paths:
            return (
                False,
                f"Policy Hook (V-9): write tool {raw!r} has no path, so it cannot be verified.",
            )
        for path in paths:
            if not is_path_inside_worktree(path, worktree):
                return (
                    False,
                    f"Policy Hook (V-9): refusing to write {path!r} outside worktree {worktree}.",
                )

    # P-2b: reads stay inside the worktree unless the task says otherwise.
    if name in READ_TOOLS and not allow_read_outside:
        for path in extract_paths(data):
            if path.startswith("<") or path in {".", "./"}:
                continue
            if not is_path_inside_worktree(path, worktree):
                return (
                    False,
                    f"Policy Hook (V-9): refusing to read {path!r} outside worktree {worktree}.",
                )

    # P-4: shell commands.
    if name in SHELL_TOOLS or "command" in data or "CommandLine" in data:
        command = extract_command(data)
        if FORBIDDEN_GIT_RE.search(command):
            return (
                False,
                "Policy Hook (V-9): workers may not run git "
                "commit/merge/push/rebase/checkout/switch/reset/clean/worktree. "
                "The orchestrator reviews `git diff` and commits.",
            )
        for forbidden in needs_approval:
            if forbidden and forbidden in command:
                return (
                    False,
                    f"Policy Hook (V-9): the command uses an action listed in "
                    f"needs_approval ({forbidden!r}). Call ask_orchestrator if you really need it.",
                )

    return True, ""


def describe_policy(worktree: Path, needs_approval: Sequence[str] = ()) -> str:
    """Short human-readable summary, used in logs and `coord doctor`."""
    extra = ", ".join(needs_approval) or "-"
    return (
        f"worktree={worktree}; no writes outside the worktree; no git "
        f"commit/merge/push/rebase/checkout/switch/reset/clean/worktree; needs_approval={extra}"
    )
