"""SQLite state for the Worker Manager.

Lives in `COORD_HOME` (default `~/.local/share/coord/coord.db`) so that one
server on a machine can serve every project. Project-local artefacts
(`.coord/logs/`, `.coord/STATE.md`, `deps.jsonl`) stay in the repo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
create table if not exists workers (
    id         text primary key,
    owner      text not null,
    project    text not null,
    worktree   text not null,
    task_id    text,
    state      text not null,
    parent     text,
    created_at text not null,
    updated_at text not null
);
create table if not exists questions (
    id         text primary key,
    worker     text not null,
    task_id    text,
    question   text not null,
    answer     text,
    answered   integer not null default 0,
    created_at text not null
);
create table if not exists reports (
    id         integer primary key autoincrement,
    worker     text not null,
    task_id    text,
    status     text not null,
    payload    text not null,
    created_at text not null
);
create index if not exists workers_owner on workers(owner);
create index if not exists workers_project on workers(project);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------------------------------------------------------------- workers
    def add_worker(
        self,
        worker_id: str,
        owner: str,
        project: str,
        worktree: str,
        task_id: str | None,
        parent: str | None = None,
        state: str = "RUNNING",
    ) -> None:
        ts = now()
        self.db.execute(
            "insert into workers(id, owner, project, worktree, task_id, state, parent, created_at, updated_at)"
            " values(?,?,?,?,?,?,?,?,?)",
            (worker_id, owner, project, worktree, task_id, state, parent, ts, ts),
        )
        self.db.commit()

    def worker(self, worker_id: str) -> dict[str, Any] | None:
        row = self.db.execute("select * from workers where id=?", (worker_id,)).fetchone()
        return dict(row) if row else None

    def set_state(self, worker_id: str, state: str) -> None:
        self.db.execute(
            "update workers set state=?, updated_at=? where id=?", (state, now(), worker_id)
        )
        self.db.commit()

    def set_task(self, worker_id: str, task_id: str) -> None:
        self.db.execute(
            "update workers set task_id=?, updated_at=? where id=?", (task_id, now(), worker_id)
        )
        self.db.commit()

    def list_workers(self, owner: str | None = None, project: str | None = None) -> list[dict[str, Any]]:
        sql = "select * from workers"
        clauses: list[str] = []
        params: list[Any] = []
        if owner:
            clauses.append("owner=?")
            params.append(owner)
        if project:
            clauses.append("project=?")
            params.append(project)
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by created_at"
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    # -------------------------------------------------------------- questions
    def add_question(self, qid: str, worker: str, task_id: str, question: str) -> None:
        self.db.execute(
            "insert into questions(id, worker, task_id, question, answered, created_at) values(?,?,?,?,0,?)",
            (qid, worker, task_id, question, now()),
        )
        self.db.commit()

    def question(self, qid: str) -> dict[str, Any] | None:
        row = self.db.execute("select * from questions where id=?", (qid,)).fetchone()
        return dict(row) if row else None

    def answer_question(self, qid: str, answer: str) -> None:
        self.db.execute(
            "update questions set answer=?, answered=1 where id=?", (answer, qid)
        )
        self.db.commit()

    def pending_questions(self, worker: str | None = None) -> list[str]:
        if worker:
            rows = self.db.execute(
                "select id from questions where answered=0 and worker=?", (worker,)
            )
        else:
            rows = self.db.execute("select id from questions where answered=0")
        return [r["id"] for r in rows.fetchall()]

    # ---------------------------------------------------------------- reports
    def add_report(self, worker: str, task_id: str | None, status: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "insert into reports(worker, task_id, status, payload, created_at) values(?,?,?,?,?)",
            (worker, task_id, status, json.dumps(payload, ensure_ascii=False), now()),
        )
        self.db.commit()

    def reports(self, worker: str | None = None) -> list[dict[str, Any]]:
        if worker:
            rows = self.db.execute(
                "select * from reports where worker=? order by id", (worker,)
            ).fetchall()
        else:
            rows = self.db.execute("select * from reports order by id").fetchall()
        return [dict(r) for r in rows]
