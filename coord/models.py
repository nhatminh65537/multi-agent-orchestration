"""Protocol models — `TaskPacket` (orchestrator -> worker) only.

The worker ends a turn with **prose**: the manager takes the agent's final
response as the task result. There is no report schema any more (protocol v0.6):
a failed turn is a response whose first line starts with `FAILED:`.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# `B-42b`, `M-5r`, `A-07`… all fine: only the stream prefix is restricted.
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_]+-[A-Za-z0-9_.-]+$")
OWNER_RE = re.compile(r"^[A-Za-z0-9_]+$")

WorkerState = Literal["RUNNING", "IDLE", "WAITING_ANSWER", "FAILED", "KILLED"]


class TaskPacket(BaseModel):
    """Work unit handed to a worker. `worktree` is bound by the manager, not here."""

    id: str
    goal: str = Field(max_length=2000)
    acceptance: list[str] = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    needs_approval: list[str] = Field(default_factory=list)
    read_only: bool = False

    @field_validator("id")
    @classmethod
    def _task_id_format(cls, value: str) -> str:
        if not TASK_ID_RE.match(value):
            raise ValueError(
                f"task id must look like <stream>-<n> (letters, digits, '_', '.', '-'); "
                f"got {value!r}"
            )
        return value


def validate_owner(owner: str) -> str:
    if not OWNER_RE.match(owner):
        raise ValueError(f"owner must match [A-Za-z0-9_]+, got {owner!r}")
    return owner
