"""Policy hook (V-9) — one implementation shared by every backend driver.

The hook is deliberately a pure function so it can be unit-tested without any
SDK, and so `claude-sdk`, `agy-sdk` and the legacy `openai-loop` driver all
enforce exactly the same rules.

Rules (Protocol v0.4 §3.3.1):

* P-1  the worker may not call the coordinator MCP tools, nor spawn subagents;
* P-2  write tools may only touch paths inside the assigned worktree;
* P-3  shell commands may not run history-rewriting / publishing git verbs;
* P-4  shell commands may not contain anything listed in `needs_approval`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------- names
WRITE_TOOLS = frozenset(
    {
        "write",
        "edit",
        "multiedit",
        "multi_edit",
        "notebookedit",
        "notebook_edit",
        "str_replace",
        "str_replace_editor",
        "apply_patch",
        "write_file",
        "edit_file",
        "create_file",
        "delete_file",
        "move_file",
        "remove_file",
        "create_directory",
    }
)

READ_TOOLS = frozenset(
    {
        "read",
        "read_file",
        "view_file",
        "grep",
        "glob",
        "search_directory",
        "find_file",
        "list_dir",
        "list_directory",
        "web_search",
        "websearch",
        "web_fetch",
        "webfetch",
        "search_web",
        "read_url_content",
    }
)

SHELL_TOOLS = frozenset(
    {"bash", "run_shell", "run_command", "shell", "execute_command", "terminal"}
)

DELEGATION_TOOLS = frozenset({"agent", "task", "invoke_subagent"})

COORD_MCP_PREFIXES = ("mcp__coord__", "mcp__worker-manager__")

FORBIDDEN_GIT_RE = re.compile(
    r"\bgit\s+(commit|merge|push|rebase|checkout|switch|reset|clean|worktree)\b",
    re.IGNORECASE,
)

_REDIRECT_RE = re.compile(r"(?:^|[^>0-9&])>>?\s*([^\s;|&<>]+)")
_MUTATING_CMDS = ("rm", "mv", "cp", "touch", "mkdir", "tee", "dd", "chmod", "chown", "ln", "truncate")

_PATH_KEYS = (
    "file_path",
    "filePath",
    "path",
    "Path",
    "target_file",
    "targetFile",
    "notebook_path",
    "absolute_path",
    "AbsolutePath",
    "directory",
    "DirectoryPath",
    "cwd",
    "workspace",
)


# ------------------------------------------------------------------- helpers
def normalise_tool_name(tool_name: str) -> str:
    name = (tool_name or "").strip()
    if name.startswith("mcp__"):
        return name
    # Claude uses `Bash(ls:*)` style matchers in allow-lists, not here, but be safe.
    if "(" in name:
        name = name.split("(", 1)[0]
    return name.strip().lower().replace("-", "_")


def resolve_target(path: str | Path, worktree: Path) -> Path:
    """Absolute, symlink-resolved path; relative paths are resolved inside `worktree`."""
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
    for key in ("command", "cmd", "CommandLine", "command_line", "script", "shell"):
        value = (tool_input or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value
    commands = (tool_input or {}).get("commands")
    if isinstance(commands, (list, tuple)):
        return " && ".join(str(c) for c in commands)
    return ""


def _shell_escapes_worktree(command: str, worktree: Path) -> str | None:
    """Best-effort detection of writes aimed outside the worktree."""
    for match in _REDIRECT_RE.finditer(command):
        target = match.group(1)
        if target.startswith(("/dev/", "/proc/")):
            continue
        if any(ch in target for ch in ("/", "~")) and not is_path_inside_worktree(
            target, worktree
        ):
            return f"chuyển hướng output ra ngoài worktree: {target}"

    for segment in re.split(r"[;&|\n]+", command):
        tokens = segment.strip().split()
        if not tokens:
            continue
        head = Path(tokens[0]).name
        if head not in _MUTATING_CMDS:
            continue
        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            if token.startswith(("/", "~", "..")) and not is_path_inside_worktree(
                token, worktree
            ):
                return f"{head} nhắm ra ngoài worktree: {token}"
    return None


# ---------------------------------------------------------------------- hook
def check_tool_policy(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    worktree: Path,
    needs_approval: Sequence[str] = (),
    *,
    allow_read_outside: bool = False,
) -> tuple[bool, str]:
    """Return `(allowed, reason)`. `reason` is shown to the model when denied."""
    raw = tool_name or ""
    name = normalise_tool_name(raw)
    data = tool_input or {}
    worktree = Path(worktree)

    # P-1: no recursion into the coordinator, no spawning subagents.
    lowered = raw.lower()
    if lowered.startswith(COORD_MCP_PREFIXES) or name in DELEGATION_TOOLS:
        return (
            False,
            f"Policy Hook (V-9): worker không được gọi tool điều phối {raw!r} "
            "(chống đệ quy spawn_worker).",
        )

    # P-2a: write inside the worktree only.
    if name in WRITE_TOOLS:
        paths = extract_paths(data)
        if not paths:
            return (
                False,
                f"Policy Hook (V-9): tool ghi {raw!r} thiếu đường dẫn, không thể kiểm chứng.",
            )
        for path in paths:
            if not is_path_inside_worktree(path, worktree):
                return (
                    False,
                    f"Policy Hook (V-9): từ chối ghi {path!r} nằm ngoài worktree {worktree}.",
                )

    # P-2b: reads stay inside the worktree unless the task says otherwise.
    if name in READ_TOOLS and not allow_read_outside:
        for path in extract_paths(data):
            if path.startswith("<") or path in {".", "./"}:
                continue
            if not is_path_inside_worktree(path, worktree):
                return (
                    False,
                    f"Policy Hook (V-9): từ chối đọc {path!r} ngoài worktree {worktree}.",
                )

    # P-3 / P-4: shell commands.
    if name in SHELL_TOOLS or "command" in data or "CommandLine" in data:
        command = extract_command(data)
        if FORBIDDEN_GIT_RE.search(command):
            return (
                False,
                "Policy Hook (V-9): worker không được chạy git "
                "commit/merge/push/rebase/checkout/switch/reset/clean/worktree. "
                "Orchestrator sẽ review `git diff` và tự commit.",
            )
        for forbidden in needs_approval:
            if forbidden and forbidden in command:
                return (
                    False,
                    f"Policy Hook (V-9): lệnh chứa hành động nằm trong needs_approval "
                    f"({forbidden!r}). Hãy gọi ask_orchestrator nếu thật sự cần.",
                )
        escape = _shell_escapes_worktree(command, worktree)
        if escape:
            return False, f"Policy Hook (V-9): {escape}."

    return True, ""


def describe_policy(worktree: Path, needs_approval: Sequence[str] = ()) -> str:
    """Short human-readable summary, used in logs and `coord doctor`."""
    extra = ", ".join(needs_approval) or "-"
    return (
        f"worktree={worktree}; cấm ghi ngoài worktree; cấm git commit/merge/push/"
        f"rebase/checkout/switch/reset/clean/worktree; needs_approval={extra}"
    )
