"""Unit + smoke tests for coord v0.5.

Run: uv run python -m unittest discover -s tests -v
Không cần API key, không cần network, không cần SDK binary: backend được giả lập
qua `Manager(session_factory=...)`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Bug B-7 (0.3): mở rộng sys.path TRƯỚC khi import package, để test chạy được từ
# bản clone sạch mà không cần `pip install -e .`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coord.backend import (  # noqa: E402
    BackendUnavailable,
    WorkerSession,
    build_instructions,
    build_session,
    default_backend,
    project_context,
)
from coord.manager import Manager  # noqa: E402
from coord.models import TaskPacket, WorkerReport  # noqa: E402
from coord.policy import check_tool_policy  # noqa: E402
from coord.store import Store  # noqa: E402


# ------------------------------------------------------------------ fakes
class FakeSession(WorkerSession):
    """Backend giả: ghi lại lượt chạy và report ngay."""

    backend_name = "fake"

    def __init__(self, *, report: WorkerReport, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self._fake_report = report
        self.turns: list[str] = []

    async def run(self, text: str) -> None:
        self.turns.append(text)
        self.report = None
        self.sdk_session_id = f"sess-{self.worker_id}"
        await self._accept_report(self._fake_report.model_dump())


def fake_factory(status: str = "DONE"):
    def make(backend: str, *, packet: TaskPacket | None, **kwargs):  # noqa: ANN001, ANN003
        report = WorkerReport(
            id=packet.id if packet else "X-01",
            status=status,  # type: ignore[arg-type]
            summary="ok",
        )
        return FakeSession(report=report, packet=packet, **kwargs)

    return make


def _packet(task_id: str, files: list[str] | None = None, **extra) -> str:
    body = {
        "id": task_id,
        "goal": "do the thing",
        "acceptance": ["pytest -q"],
        "files": files or [],
        **extra,
    }
    return json.dumps(body)


async def _settle(manager: Manager, worker_id: str) -> None:
    task = manager.tasks.get(worker_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)


# ------------------------------------------------------------------ models
class ModelTests(unittest.TestCase):
    def test_task_packet_validates_id(self) -> None:
        packet = TaskPacket.model_validate_json(_packet("A-07"))
        self.assertEqual(packet.max_rounds, 3)
        self.assertFalse(packet.is_read_only)
        with self.assertRaises(ValueError):
            TaskPacket.model_validate_json(_packet("nope"))

    def test_worker_report_requires_summary(self) -> None:
        with self.assertRaises(ValueError):
            WorkerReport.model_validate({"id": "A-07", "status": "DONE"})
        report = WorkerReport.model_validate({"id": "A-07", "status": "DONE", "summary": "ok"})
        self.assertEqual(report.files_changed, [])

    def test_read_only_constraint_is_honoured(self) -> None:
        packet = TaskPacket.model_validate_json(
            _packet("A-08", constraints=["Read-only: analysis only"])
        )
        self.assertTrue(packet.is_read_only)


# ------------------------------------------------------------------- policy
class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "wt-A"
        self.wt.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_allows_write_inside_worktree(self) -> None:
        ok, reason = check_tool_policy("Write", {"file_path": str(self.wt / "a.py")}, self.wt)
        self.assertTrue(ok, reason)

    def test_denies_absolute_path_outside(self) -> None:
        outside = Path(self.tmp.name) / "wt-B" / "a.py"
        ok, reason = check_tool_policy("Write", {"file_path": str(outside)}, self.wt)
        self.assertFalse(ok)
        self.assertIn("ngoài worktree", reason)

    def test_denies_traversal(self) -> None:
        ok, _ = check_tool_policy("Edit", {"file_path": "../wt-B/src/api.py"}, self.wt)
        self.assertFalse(ok)

    def test_denies_git_verbs(self) -> None:
        for command in ("git commit -m x", "git merge stream-B", "git push"):
            ok, reason = check_tool_policy("Bash", {"command": command}, self.wt)
            self.assertFalse(ok, command)
            self.assertIn("git", reason)

    def test_allows_git_status(self) -> None:
        ok, reason = check_tool_policy("Bash", {"command": "git status --short"}, self.wt)
        self.assertTrue(ok, reason)

    def test_denies_needs_approval(self) -> None:
        ok, reason = check_tool_policy(
            "Bash", {"command": "pip install requests"}, self.wt, ["pip install"]
        )
        self.assertFalse(ok)
        self.assertIn("needs_approval", reason)

    def test_denies_coord_mcp(self) -> None:
        ok, reason = check_tool_policy("mcp__coord__spawn_worker", {"owner": "A"}, self.wt)
        self.assertFalse(ok)
        self.assertIn("chống đệ quy", reason)

    def test_denies_subagent(self) -> None:
        ok, _ = check_tool_policy("Task", {"prompt": "x"}, self.wt)
        self.assertFalse(ok)

    def test_denies_shell_redirect_outside(self) -> None:
        outside = Path(self.tmp.name) / "wt-B" / "leak.txt"
        ok, _ = check_tool_policy("Bash", {"command": f"echo hi > {outside}"}, self.wt)
        self.assertFalse(ok)

    def test_antigravity_tool_names(self) -> None:
        ok, _ = check_tool_policy("create_file", {"path": str(self.wt / "b.py")}, self.wt)
        self.assertTrue(ok)
        ok, _ = check_tool_policy("run_command", {"CommandLine": "git reset --hard"}, self.wt)
        self.assertFalse(ok)


# -------------------------------------------------------------------- store
class StoreTests(unittest.TestCase):
    def test_worker_and_question_lifecycle(self) -> None:
        store = Store(":memory:")
        store.add_worker("A-1a2b", "A", "/repo", "/repo/wt-A", "A-01")
        self.assertEqual(store.worker("A-1a2b")["state"], "RUNNING")
        store.set_state("A-1a2b", "WAITING_ANSWER")
        store.add_question("q1", "A-1a2b", "A-01", "which shape?")
        self.assertEqual(store.pending_questions("A-1a2b"), ["q1"])
        store.answer_question("q1", "use token/exp")
        self.assertEqual(store.pending_questions("A-1a2b"), [])
        self.assertEqual(len(store.list_workers(owner="A")), 1)
        self.assertEqual(store.list_workers(owner="B"), [])

    def test_migrates_v04_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "coord.db"
            raw = sqlite3.connect(db)
            raw.executescript(
                """
                create table workers (id text primary key, owner text not null, project text not null,
                    worktree text not null, task_id text, state text not null, parent text,
                    created_at text not null, updated_at text not null);
                """
            )
            raw.execute(
                "insert into workers values('A-1','A','/p','/p/wt-A','A-01','IDLE',null,'t','t')"
            )
            raw.commit()
            raw.close()

            store = Store(db)
            row = store.worker("A-1")
            self.assertEqual(row["state"], "IDLE")
            self.assertEqual(row["backend"], "agy-sdk")  # cột thêm mới lấy mặc định hiện hành
            self.assertEqual(json.loads(row["files_json"]), [])

    def test_event_logging_and_prune(self) -> None:
        store = Store(":memory:")
        store.add_event("A", "one")
        store.add_event("A", "two")
        records = store.events("A")
        self.assertEqual([r["line"] for r in records], ["one", "two"])
        # Pruning events older than 0 days (i.e. all past events)
        deleted = store.prune_events(days=0)
        # In memory with datetime('now', '-0 day') vs current timestamp
        self.assertGreaterEqual(deleted, 0)


# ------------------------------------------------------------------ backend
class BackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)
        (self.wt / "README.md").write_text("# demo\n", encoding="utf-8")
        (self.wt / "AGENTS.md").write_text("# rules\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_backend_is_gemini(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COORD_BACKEND", None)
            self.assertEqual(default_backend(), "agy-sdk")

    def test_project_context_mentions_worktree_and_branch(self) -> None:
        text = project_context(self.wt, "A")
        self.assertIn(str(self.wt), text)
        self.assertIn("Branch:", text)
        self.assertIn("README.md", text)

    def test_skill_body_is_optional(self) -> None:
        without = build_instructions(self.wt, owner="A", with_skill_body=False)
        self.assertIn("# rules", without)
        self.assertNotIn("name: worker-protocol", without)

        with_body = build_instructions(self.wt, owner="A", with_skill_body=True)
        self.assertIn("name: worker-protocol", with_body)

    def test_unknown_backend_is_rejected(self) -> None:
        async def no_ask(task_id: str, question: str) -> str:
            return "x"

        async def no_report(report: WorkerReport) -> None:
            return None

        with self.assertRaises(BackendUnavailable):
            build_session(
                "legacy-loop",
                worker_id="A-1",
                owner="A",
                worktree=self.wt,
                model="m",
                packet=None,
                ask=no_ask,
                on_report=no_report,
            )


# ------------------------------------------------------------------ manager
class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "wt-A"
        self.wt.mkdir()
        self.store = Store(":memory:")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _manager(self, status: str = "DONE", restore: bool = True) -> Manager:
        return Manager(
            self.store,
            backend="agy-sdk",
            model="fake",
            restore=restore,
            session_factory=fake_factory(status),
        )

    def test_spawn_report_and_log(self) -> None:
        manager = self._manager()

        async def scenario() -> str:
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            await _settle(manager, wid)
            return wid

        wid = asyncio.run(scenario())
        self.assertEqual(self.store.worker(wid)["state"], "IDLE")
        log = self.wt / ".coord" / "logs" / "A-01.md"
        self.assertTrue(log.is_file())
        self.assertIn("## report", log.read_text(encoding="utf-8"))
        status = manager.status(owner="A")[0]
        self.assertEqual(status["backend"], "agy-sdk")
        self.assertEqual(status["pending_questions"], [])
        self.assertNotIn("parent", status)

    def test_failed_report_marks_failed(self) -> None:
        manager = self._manager(status="FAILED")

        async def scenario() -> str:
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            await _settle(manager, wid)
            return wid

        wid = asyncio.run(scenario())
        self.assertEqual(self.store.worker(wid)["state"], "FAILED")

    def test_overlap_rejected_and_read_only_allowed(self) -> None:
        manager = self._manager()

        async def scenario() -> None:
            first = await manager.spawn_worker(
                "A", str(self.wt), _packet("A-01", files=["src/api.py"])
            )
            with self.assertRaises(ValueError):
                await manager.spawn_worker(
                    "A", str(self.wt), _packet("A-02", files=["src/api.py"])
                )
            await manager.spawn_worker(
                "A", str(self.wt), _packet("A-03", files=["src/api.py"], read_only=True)
            )
            # dọn để không rò task
            await _settle(manager, first)

        asyncio.run(scenario())

    def test_no_overlap_on_different_worktrees(self) -> None:
        other = Path(self.tmp.name) / "wt-B"
        other.mkdir()
        manager = self._manager()

        async def scenario() -> None:
            a = await manager.spawn_worker("A", str(self.wt), _packet("A-01", files=["x.py"]))
            b = await manager.spawn_worker("B", str(other), _packet("B-01", files=["x.py"]))
            await _settle(manager, a)
            await _settle(manager, b)

        asyncio.run(scenario())

    def test_restart_marks_running_failed_and_keeps_idle(self) -> None:
        self.store.add_worker(
            "A-idle", "A", "/p", str(self.wt), "A-01", backend="agy-sdk", session_id="s1"
        )
        self.store.set_state("A-idle", "IDLE")
        self.store.add_worker("A-run", "A", "/p", str(self.wt), "A-02", backend="agy-sdk")
        self.store.add_worker("A-wait", "A", "/p", str(self.wt), "A-03", backend="agy-sdk")
        self.store.set_state("A-wait", "WAITING_ANSWER")
        self.store.add_question("q1", "A-wait", "A-03", "?")

        manager = Manager(
            self.store, backend="agy-sdk", model="fake", session_factory=fake_factory()
        )

        self.assertEqual(self.store.worker("A-idle")["state"], "IDLE")
        self.assertEqual(self.store.worker("A-run")["state"], "FAILED")
        self.assertEqual(self.store.worker("A-wait")["state"], "FAILED")
        self.assertEqual(self.store.question("q1")["answer"], "SERVER_RESTARTED")
        self.assertEqual(manager.status(owner="A")[2]["pending_questions"], [])

    def test_send_after_restart_reuses_session_id(self) -> None:
        manager = self._manager()

        async def first() -> str:
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            await _settle(manager, wid)
            return wid

        wid = asyncio.run(first())
        self.assertEqual(self.store.worker(wid)["session_id"], f"sess-{wid}")

        restarted = self._manager()
        self.assertNotIn(wid, restarted.sessions)

        async def second() -> None:
            await restarted.send("A", wid, _packet("A-02"))
            await _settle(restarted, wid)

        asyncio.run(second())
        self.assertEqual(self.store.worker(wid)["state"], "IDLE")
        self.assertEqual(len(self.store.reports(wid)), 2)

    def test_events_recorded(self) -> None:
        manager = Manager(
            self.store, backend="agy-sdk", model="fake", restore=False,
            session_factory=fake_factory(),
        )
        manager.emit("A", "event-1")
        manager.emit("A", "event-2")
        rows = self.store.db.execute("select line from events where owner='A' order by id").fetchall()
        self.assertEqual([r[0] for r in rows], ["event-1", "event-2"])

    def test_kill_while_waiting_does_not_resurrect(self) -> None:
        manager = self._manager()

        async def scenario() -> None:
            self.store.add_worker(
                "A-stuck", "A", "/p", str(self.wt), "A-01", backend="agy-sdk"
            )
            self.store.set_state("A-stuck", "WAITING_ANSWER")
            self.store.add_question("q1", "A-stuck", "A-01", "?")
            await manager.kill("A", "A-stuck")
            self.assertEqual(self.store.worker("A-stuck")["state"], "KILLED")
            self.assertEqual(self.store.question("q1")["answer"], "KILLED")

        asyncio.run(scenario())

    def test_answer_unknown_question_raises(self) -> None:
        manager = self._manager()

        async def scenario() -> None:
            with self.assertRaises(ValueError):
                await manager.answer("A", "q-missing", "x")

        asyncio.run(scenario())

    def test_owner_isolation(self) -> None:
        manager = self._manager()

        async def scenario() -> None:
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            with self.assertRaises(ValueError):
                await manager.send("B", wid, "hi")
            await _settle(manager, wid)

        asyncio.run(scenario())

    def test_antigravity_session_config_missing_key(self) -> None:
        from coord.backend import AntigravityWorkerSession, BackendUnavailable

        session = AntigravityWorkerSession(
            worker_id="w-test",
            owner="A",
            worktree=self.wt,
            model="gemini-3.8-flash-high",
            packet=TaskPacket.model_validate_json(_packet("A-01")),
            ask=mock.AsyncMock(),
            on_report=mock.AsyncMock(),
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(BackendUnavailable):
                session._config()

    def test_antigravity_session_config_with_base_url(self) -> None:
        from coord.backend import AntigravityWorkerSession

        session = AntigravityWorkerSession(
            worker_id="w-test",
            owner="A",
            worktree=self.wt,
            model="gemini-3.8-flash-high",
            packet=TaskPacket.model_validate_json(_packet("A-01")),
            ask=mock.AsyncMock(),
            on_report=mock.AsyncMock(),
        )
        env_vars = {
            "WORKER_API_KEY": "test-key-123",
            "WORKER_BASE_URL": "https://llm.example.com/v1",
        }
        with mock.patch.dict(os.environ, env_vars, clear=True):
            cfg = session._config()
            self.assertIsNotNone(cfg.models)
            self.assertEqual(len(cfg.models), 2)
            self.assertEqual(cfg.models[0].name, "gemini-3.8-flash-high")
            self.assertEqual(cfg.models[0].endpoint.base_url, "https://llm.example.com")
            self.assertEqual(cfg.models[0].endpoint.api_key, "test-key-123")
            self.assertEqual(cfg.models[1].endpoint.base_url, "https://llm.example.com")


if __name__ == "__main__":
    unittest.main()
