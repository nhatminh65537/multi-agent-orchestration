"""Protocol models — Interface & Protocol Specification v0.3.

Only the two payloads the Worker Manager validates live here: `TaskPacket`
(orchestrator -> worker) and `WorkerReport` (worker -> manager). Field names
and constraints are copied from the JSON Schema in doc 3, §3.2 and §3.3.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_]+-[0-9]+$")
OWNER_RE = re.compile(r"^[A-Za-z0-9_]+$")

WorkerState = Literal["RUNNING", "IDLE", "WAITING_ANSWER", "FAILED", "KILLED"]


class TaskPacket(BaseModel):
    """Work unit handed to a worker. `worktree` is bound by the manager, not here."""

    id: str
    goal: str = Field(max_length=1000)
    acceptance: list[str] = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    needs_approval: list[str] = Field(default_factory=list)
    max_rounds: int = 3

    @field_validator("id")
    @classmethod
    def _task_id_format(cls, value: str) -> str:
        if not TASK_ID_RE.match(value):
            raise ValueError(f"task id phải có dạng <stream>-<n>, nhận được {value!r}")
        return value


class WorkerReport(BaseModel):
    """End-of-task report (≈10 lines). Never carries file contents."""

    id: str
    status: Literal["DONE", "FAILED"]
    summary: str = Field(max_length=600)
    assumptions: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    diff_stat: str = ""
    tests: str = ""
    log: str = ""


def validate_owner(owner: str) -> str:
    if not OWNER_RE.match(owner):
        raise ValueError(f"owner phải khớp [A-Za-z0-9_]+, nhận được {owner!r}")
    return owner
