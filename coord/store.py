"""SQLite state for the Worker Manager (`coord.db`, Protocol v0.5).

Lives in `COORD_HOME` (default `~/.local/share/coord/coord.db`) so one server on
a machine can serve every project. Project-local artefacts (`.coord/logs/`,
`.coord/STATE.md`) stay in the repo.

Schema notes:

* `session_id` is the opaque SDK session/conversation id — the basis of V-8/V-13.
* `messages_json` is unused by the SDK drivers (they persist `session_id`); it stays
  for a custom `session_factory` that keeps its own transcript.
* `files_json` backs the overlap check (ADR-019).
* `events` makes the event stream durable and replayable (ADR-017).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCHEMA = """
create table if not exists workers (
    id            text primary key,
    owner         text not null,
    project       text not null,
    worktree      text not null,
    task_id       text,
    state         text not null,
    parent        text,
    backend       text not null default 'agy-sdk',
    session_id    text,
    files_json    text not null default '[]',
    messages_json text,
    read_only     integer not null default 0,
    created_at    text not null,
    updated_at    text not null
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
create table if not exists events (
    id         integer primary key autoincrement,
    owner      text not null,
    line       text not null,
    created_at text not null
);
create index if not exists workers_owner on workers(owner);
create index if not exists workers_project on workers(project);
create index if not exists workers_worktree on workers(worktree);
create index if not exists events_owner on events(owner);
"""

# Columns added in v0.4; older databases are migrated in place.
_ADDED_COLUMNS = {
    "backend": "text not null default 'agy-sdk'",
    "session_id": "text",
    "files_json": "text not null default '[]'",
    "messages_json": "text",
    "read_only": "integer not null default 0",
}


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
        self._migrate()
        self.db.commit()

    # ------------------------------------------------------------- migration
    def _migrate(self) -> None:
        columns = {row["name"] for row in self.db.execute("pragma table_info(workers)")}
        for name, definition in _ADDED_COLUMNS.items():
            if name not in columns:
                self.db.execute(f"alter table workers add column {name} {definition}")

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
        *,
        backend: str = "agy-sdk",
        session_id: str | None = None,
        files: Sequence[str] = (),
        read_only: bool = False,
    ) -> None:
        ts = now()
        self.db.execute(
            "insert into workers(id, owner, project, worktree, task_id, state, parent, backend,"
            " session_id, files_json, read_only, created_at, updated_at)"
            " values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                worker_id,
                owner,
                project,
                worktree,
                task_id,
                state,
                parent,
                backend,
                session_id,
                json.dumps(list(files), ensure_ascii=False),
                int(read_only),
                ts,
                ts,
            ),
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

    def set_session(self, worker_id: str, session_id: str | None) -> None:
        self.db.execute(
            "update workers set session_id=?, updated_at=? where id=?",
            (session_id, now(), worker_id),
        )
        self.db.commit()

    def set_messages(self, worker_id: str, messages: list[dict[str, Any]] | None) -> None:
        payload = json.dumps(messages, ensure_ascii=False) if messages is not None else None
        self.db.execute(
            "update workers set messages_json=?, updated_at=? where id=?",
            (payload, now(), worker_id),
        )
        self.db.commit()

    def messages(self, worker_id: str) -> list[dict[str, Any]] | None:
        row = self.db.execute(
            "select messages_json from workers where id=?", (worker_id,)
        ).fetchone()
        if not row or not row["messages_json"]:
            return None
        try:
            return json.loads(row["messages_json"])
        except json.JSONDecodeError:
            return None

    def set_files(self, worker_id: str, files: Sequence[str]) -> None:
        self.db.execute(
            "update workers set files_json=?, updated_at=? where id=?",
            (json.dumps(list(files), ensure_ascii=False), now(), worker_id),
        )
        self.db.commit()

    def list_workers(
        self, owner: str | None = None, project: str | None = None
    ) -> list[dict[str, Any]]:
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

    def active_workers_on(self, worktree: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "select * from workers where worktree=? and state in ('RUNNING','WAITING_ANSWER')",
            (str(worktree),),
        ).fetchall()
        return [dict(r) for r in rows]

    def states(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.execute(
                "select id, owner, state, session_id, worktree, task_id from workers"
            ).fetchall()
        ]

    # -------------------------------------------------------------- questions
    def add_question(self, qid: str, worker: str, task_id: str, question: str) -> None:
        self.db.execute(
            "insert into questions(id, worker, task_id, question, answered, created_at)"
            " values(?,?,?,?,0,?)",
            (qid, worker, task_id, question, now()),
        )
        self.db.commit()

    def question(self, qid: str) -> dict[str, Any] | None:
        row = self.db.execute("select * from questions where id=?", (qid,)).fetchone()
        return dict(row) if row else None

    def answer_question(self, qid: str, answer: str) -> None:
        self.db.execute("update questions set answer=?, answered=1 where id=?", (answer, qid))
        self.db.commit()

    def pending_questions(self, worker: str | None = None) -> list[str]:
        if worker:
            rows = self.db.execute(
                "select id from questions where answered=0 and worker=?", (worker,)
            )
        else:
            rows = self.db.execute("select id from questions where answered=0")
        return [r["id"] for r in rows.fetchall()]

    def pending_question_rows(self) -> list[dict[str, Any]]:
        rows = self.db.execute("select * from questions where answered=0").fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- reports
    def add_report(
        self, worker: str, task_id: str | None, status: str, payload: dict[str, Any]
    ) -> None:
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

    # ----------------------------------------------------------------- events
    def add_event(self, owner: str, line: str, **kwargs: Any) -> int:
        cursor = self.db.execute(
            "insert into events(owner, line, created_at) values(?,?,?)",
            (owner, line, now()),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def events(self, owner: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Lấy danh sách audit events gần nhất."""
        if owner:
            rows = self.db.execute(
                "select * from events where owner=? order by id desc limit ?",
                (owner, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "select * from events order by id desc limit ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def prune_events(self, days: int = 7) -> int:
        """Dọn dẹp audit events cũ hơn N ngày."""
        cursor = self.db.execute(
            "delete from events where datetime(created_at) < datetime('now', ?)",
            (f"-{int(days)} day",),
        )
        self.db.commit()
        return cursor.rowcount or 0

    def close(self) -> None:
        self.db.close()
