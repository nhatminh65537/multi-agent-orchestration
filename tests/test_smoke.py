"""Unit + smoke tests for coord v0.4.

Run: uv run python -m unittest discover -s tests -v
(No API key, no network, no SDK binary required.)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Bug B-7 (0.3): extend sys.path BEFORE importing the package, so the suite runs
# from a fresh clone without `pip install -e .`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coord import deps as ledger  # noqa: E402
from coord.backend import OpenAILoopSession  # noqa: E402
from coord.manager import Manager  # noqa: E402
from coord.models import TaskPacket, WorkerReport  # noqa: E402
from coord.policy import check_tool_policy  # noqa: E402
from coord.store import Store  # noqa: E402


# ------------------------------------------------------------------ fakes
class _Function:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index: int, name: str, arguments: str) -> None:
        self.id = f"call_{index}"
        self.function = _Function(name, arguments)


class _Message:
    def __init__(self, content: str | None = None, tool_calls: list[_ToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none: bool = False) -> dict:
        data: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in self.tool_calls
            ]
        elif not exclude_none:
            data["tool_calls"] = None
        return data


class _Choice:
    def __init__(self, message: _Message) -> None:
        self.message = message


class _Response:
    def __init__(self, message: _Message) -> None:
        self.choices = [_Choice(message)]


class _Completions:
    def __init__(self, script: list[_Response]) -> None:
        self.script = script
        self.calls: list[dict] = []

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("model được gọi quá số lần kịch bản")
        return self.script.pop(0)


class _FakeClient:
    def __init__(self, script: list[_Response]) -> None:
        self.chat = type("Chat", (), {"completions": _Completions(script)})()


def _call(index: int, name: str, args: str) -> _Message:
    return _Message(tool_calls=[_ToolCall(index, name, args)])


def _report_call(task_id: str, status: str = "DONE", index: int = 0) -> _Message:
    payload = json.dumps({"report": {"id": task_id, "status": status, "summary": "ok"}})
    return _call(index, "report_completion", payload)


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
        ok, reason = check_tool_policy(
            "mcp__coord__spawn_worker", {"owner": "A"}, self.wt
        )
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

    def test_migrates_v03_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "coord.db"
            import sqlite3

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
            self.assertEqual(row["backend"], "openai-loop")
            self.assertEqual(json.loads(row["files_json"]), [])

    def test_event_replay_and_prune(self) -> None:
        store = Store(":memory:")
        store.add_event("A", "one", delivered=False)
        store.add_event("A", "two", delivered=False)
        pending = store.undelivered_events("A")
        self.assertEqual([r["line"] for r in pending], ["one", "two"])
        store.mark_delivered([r["id"] for r in pending])
        self.assertEqual(store.undelivered_events("A"), [])


# --------------------------------------------------------------------- deps
class DepsTests(unittest.TestCase):
    def test_cycle_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"COORD_DIR": tmp}):
                root = Path(tmp)
                ok, message = ledger.add_wait(root, "A", "B", "A-07")
                self.assertTrue(ok, message)
                ok, message = ledger.add_wait(root, "B", "C", "B-01")
                self.assertTrue(ok, message)
                ok, message = ledger.add_wait(root, "C", "A", "C-02")
                self.assertFalse(ok)
                self.assertIn("CYCLE", message)

                # Resolving the first edge unlocks the third.
                ledger.add_resolve(root, "A-07", "3f2a1c", "stream-B", waiter="A")
                ok, message = ledger.add_wait(root, "C", "A", "C-02")
                self.assertTrue(ok, message)

    def test_open_waits_are_keyed_by_waiter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"COORD_DIR": tmp}):
                root = Path(tmp)
                ledger.add_wait(root, "A", "B", "A-07")
                ledger.add_wait(root, "C", "B", "C-07")
                ledger.add_resolve(root, "A-07", "sha", "stream-B", waiter="A")
                tasks = [r["task"] for r in ledger.open_waits(ledger.load(root))]
                self.assertEqual(tasks, ["C-07"])

    def test_self_wait_is_a_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"COORD_DIR": tmp}):
                ok, message = ledger.add_wait(Path(tmp), "A", "A", "A-01")
                self.assertFalse(ok)
                self.assertIn("chính mình", message)


# ------------------------------------------------------------- worker loops
class WorkerLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _session(self, script: list[_Response], reports: list[WorkerReport] | None = None):
        client = _FakeClient(script)
        captured = reports if reports is not None else []

        async def on_report(report: WorkerReport) -> None:
            captured.append(report)

        async def ask(task_id: str, question: str) -> str:
            return "yes"

        session = OpenAILoopSession(
            worker_id="A-1",
            owner="A",
            worktree=self.wt,
            model="fake",
            packet=TaskPacket.model_validate_json(_packet("A-07")),
            ask=ask,
            on_report=on_report,
            client=client,
        )
        return session, client, captured

    def test_report_with_sibling_tool_call_keeps_history_consistent(self) -> None:
        """BUG B-1: every tool_call_id needs a tool message, even after report."""
        script = [
            _Response(
                _Message(
                    tool_calls=[
                        _ToolCall(0, "report_completion", json.dumps(
                            {"report": {"id": "A-07", "status": "DONE", "summary": "ok"}}
                        )),
                        _ToolCall(1, "write_file", json.dumps({"path": "leak.py", "content": "x"})),
                    ]
                )
            )
        ]
        session, _, captured = self._session(script)
        asyncio.run(session.run("go"))

        assistant = [m for m in session.messages if m["role"] == "assistant"][-1]
        tool_messages = [m for m in session.messages if m["role"] == "tool"]
        self.assertEqual(len(assistant["tool_calls"]), 2)
        self.assertEqual(len(tool_messages), 2)
        self.assertEqual(
            {m["tool_call_id"] for m in tool_messages},
            {call["id"] for call in assistant["tool_calls"]},
        )
        self.assertFalse((self.wt / "leak.py").exists(), "tool sau report phải bị bỏ qua")
        self.assertEqual(len(captured), 1)

    def test_policy_blocks_write_outside_worktree(self) -> None:
        outside = self.wt.parent / "wt-B" / "x.py"
        script = [
            _Response(
                _Message(
                    tool_calls=[_ToolCall(0, "write_file", json.dumps({"path": str(outside), "content": "x"}))]
                )
            ),
            _Response(_report_call("A-07", index=1)),
        ]
        session, client, _ = self._session(script)
        asyncio.run(session.run("go"))
        tool_outputs = [m["content"] for m in session.messages if m["role"] == "tool"]
        self.assertIn("ngoài worktree", tool_outputs[0])
        self.assertFalse(outside.exists())

    def test_shell_cancel_kills_the_child(self) -> None:
        """BUG B-2: cancelling a turn must not leave the shell running."""
        session, _, _ = self._session([])

        async def scenario() -> None:
            task = asyncio.create_task(session._run_shell("sleep 30; echo coord-cancel-marker"))
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        if subprocess.call(["which", "pgrep"], stdout=subprocess.DEVNULL) != 0:
            self.skipTest("pgrep không có")
        asyncio.run(scenario())
        result = subprocess.run(
            ["pgrep", "-f", "coord-cancel-marker"], capture_output=True, text=True
        )
        self.assertEqual(result.stdout.strip(), "", "tiến trình con vẫn còn sống")


# ------------------------------------------------------------------ manager
class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "wt-A"
        self.wt.mkdir()
        self.store = Store(":memory:")

    def tearDown(self) -> None:
        for task in list(getattr(self, "_tasks", [])):
            task.cancel()
        self.tmp.cleanup()

    def _manager(self, script: list[_Response] | None = None, restore: bool = True) -> Manager:
        factory = lambda: _FakeClient(script or [_Response(_report_call("A-01"))])  # noqa: E731
        manager = Manager(
            self.store,
            backend="openai-loop",
            model="fake",
            restore=restore,
            client_factory=factory,
        )
        return manager

    def test_spawn_and_report_writes_log(self) -> None:
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
        self.assertEqual(status["backend"], "openai-loop")
        self.assertEqual(status["pending_questions"], [])

    def test_overlap_rejected_and_read_only_allowed(self) -> None:
        manager = self._manager()

        async def scenario() -> None:
            await manager.spawn_worker("A", str(self.wt), _packet("A-01", files=["src/api.py"]))
            with self.assertRaises(ValueError):
                await manager.spawn_worker("A", str(self.wt), _packet("A-02", files=["src/api.py"]))
            # read-only tasks may share files with a running writer
            await manager.spawn_worker(
                "A", str(self.wt), _packet("A-03", files=["src/api.py"], read_only=True)
            )

        asyncio.run(scenario())

    def test_no_overlap_on_different_worktrees(self) -> None:
        other = Path(self.tmp.name) / "wt-B"
        other.mkdir()
        manager = self._manager()

        async def scenario() -> None:
            await manager.spawn_worker("A", str(self.wt), _packet("A-01", files=["src/api.py"]))
            await manager.spawn_worker("B", str(other), _packet("B-01", files=["src/api.py"]))

        asyncio.run(scenario())

    def test_restart_marks_running_failed_and_keeps_idle(self) -> None:
        self.store.add_worker(
            "A-idle", "A", "/p", str(self.wt), "A-01", backend="openai-loop", session_id="s1"
        )
        self.store.set_state("A-idle", "IDLE")
        self.store.add_worker("A-run", "A", "/p", str(self.wt), "A-02", backend="openai-loop")
        self.store.add_worker("A-wait", "A", "/p", str(self.wt), "A-03", backend="openai-loop")
        self.store.set_state("A-wait", "WAITING_ANSWER")
        self.store.add_question("q1", "A-wait", "A-03", "?")

        manager = Manager(self.store, backend="openai-loop", model="fake")

        self.assertEqual(self.store.worker("A-idle")["state"], "IDLE")
        self.assertEqual(self.store.worker("A-run")["state"], "FAILED")
        self.assertEqual(self.store.worker("A-wait")["state"], "FAILED")
        self.assertEqual(self.store.question("q1")["answer"], "SERVER_RESTARTED")
        self.assertEqual(manager.status(owner="A")[2]["pending_questions"], [])

    def test_send_after_restart_reuses_persisted_message_history(self) -> None:
        manager = self._manager([_Response(_report_call("A-01"))])

        async def first() -> str:
            wid = await manager.spawn_worker("A", str(self.wt), _packet("A-01"))
            await _settle(manager, wid)
            return wid

        wid = asyncio.run(first())
        self.assertIsNotNone(self.store.messages(wid))

        restarted = self._manager([_Response(_report_call("A-02"))])
        self.assertNotIn(wid, restarted.sessions)

        async def second() -> None:
            await restarted.send("A", wid, _packet("A-02"))
            await _settle(restarted, wid)

        asyncio.run(second())
        self.assertEqual(self.store.worker(wid)["state"], "IDLE")
        self.assertEqual(len(self.store.reports(wid)), 2)

    def test_events_fanout_and_replay(self) -> None:
        manager = Manager(self.store, backend="openai-loop", model="fake", restore=False)

        async def scenario() -> None:
            manager.emit("A", "offline-event")
            self.assertEqual([e["line"] for e in self.store.undelivered_events("A")], ["offline-event"])

            first = asyncio.create_task(manager.stream("A").__anext__())
            second = asyncio.create_task(manager.stream("A").__anext__())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(len(manager.subscribers["A"]), 2)
            manager.emit("A", "live-event")

            results = await asyncio.gather(first, second)
            self.assertEqual(sorted(results), ["live-event", "offline-event"])

        asyncio.run(scenario())

    def test_kill_while_waiting_does_not_resurrect(self) -> None:
        manager = self._manager()

        async def scenario() -> None:
            self.store.add_worker(
                "A-stuck", "A", "/p", str(self.wt), "A-01", backend="openai-loop"
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


if __name__ == "__main__":
    unittest.main()
