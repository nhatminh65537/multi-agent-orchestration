"""Dependency ledger `.coord/deps.jsonl` and cycle detection.

Protocol v0.4 §4.1: a dependency is keyed by `(waiter, provider, task)`; the
task id belongs to the **waiter** (the agent that is blocked), not to the
provider. The ledger is append-only.

Also exposed as `coord deps …` so orchestrators never depend on a relative
script path (ADR-018).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .paths import coord_dir


# ------------------------------------------------------------------ storage
def ledger_path(worktree: Path) -> Path:
    return coord_dir(Path(worktree)) / "deps.jsonl"


def load(worktree: Path) -> list[dict[str, Any]]:
    path = ledger_path(worktree)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn final line must not break every reader
    return records


def append(worktree: Path, record: dict[str, Any]) -> dict[str, Any]:
    path = ledger_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **record}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


# ------------------------------------------------------------- open/close
def _resolved(records: Sequence[dict[str, Any]]) -> set[tuple[str | None, str]]:
    keys: set[tuple[str | None, str]] = set()
    for record in records:
        if record.get("op") != "resolve":
            continue
        task = record.get("task")
        if not task:
            continue
        keys.add((record.get("waiter"), task))
    return keys


def open_waits(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Waits that have no matching resolve for `(waiter, task)`."""
    resolved = _resolved(records)
    out: list[dict[str, Any]] = []
    for record in records:
        if record.get("op") != "wait":
            continue
        task = record.get("task")
        waiter = record.get("waiter")
        if (waiter, task) in resolved or (None, task) in resolved:
            continue
        out.append(record)
    return out


def edges(records: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(r["waiter"], r["provider"]) for r in open_waits(records) if r.get("waiter") and r.get("provider")]


def waits_on(edges_: Iterable[tuple[str, str]], start: str, target: str) -> bool:
    """True when `start` waits on `target` directly or transitively."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(provider for waiter, provider in edges_ if waiter == node)
    return False


def would_cycle(records: Sequence[dict[str, Any]], waiter: str, provider: str) -> str | None:
    """Return a description of the cycle, or None when the wait is safe."""
    if waiter == provider:
        return f"{waiter} không thể chờ chính mình"
    if waits_on(edges(records), provider, waiter):
        return f"{provider} đã (trực tiếp hoặc gián tiếp) chờ {waiter}"
    return None


# ------------------------------------------------------------------ actions
def add_wait(worktree: Path, waiter: str, provider: str, task: str) -> tuple[bool, str]:
    records = load(worktree)
    cycle = would_cycle(records, waiter, provider)
    if cycle:
        return False, (
            f"CYCLE: {cycle}. Không gửi NEED; gửi BLOCKED {task} on={provider} cho integrator."
        )
    append(worktree, {"op": "wait", "waiter": waiter, "provider": provider, "task": task})
    return True, "ok"


def add_resolve(
    worktree: Path, task: str, sha: str, branch: str, waiter: str | None = None
) -> tuple[bool, str]:
    record: dict[str, Any] = {"op": "resolve", "task": task, "sha": sha, "branch": branch}
    if waiter:
        record["waiter"] = waiter
    append(worktree, record)
    return True, "ok"


def format_open(worktree: Path, who: str | None = None) -> str:
    rows = open_waits(load(worktree))
    if who:
        rows = [r for r in rows if who in (r.get("waiter"), r.get("provider"))]
    if not rows:
        return "(không có dependency nào đang mở)"
    return "\n".join(
        f'{r.get("task")}: {r.get("waiter")} waits on {r.get("provider")} since {r.get("ts", "-")}'
        for r in rows
    )


def state_text(worktree: Path) -> str:
    path = coord_dir(Path(worktree)) / "STATE.md"
    if not path.is_file():
        return "(chưa có STATE.md; integrator tạo)"
    return path.read_text(encoding="utf-8")
